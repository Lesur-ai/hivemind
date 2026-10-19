"""Temporal annotation of RAG chunks (T05 / #922, contract sections 2, 4, 5).

A chunk keeps the exact extracted-text ranges of its passage, the context a
reader needs to interpret it (ancestor headings, introductions, selected anchor
proofs) and the complete occurrence records whose fact evidence overlaps that
passage. Association is by source offsets only: never by description, filename
or date similarity. Everything here is pure; nothing calls a model, reads the
clock or touches a store, and a stored annotation is re-validated strictly
before it is shown.
"""
from __future__ import annotations

import re
from bisect import bisect_left
from hashlib import sha256
from typing import Any

from .chunk_render import canonical, worst_status
from .temporal import (
    OCCURRENCE_BYTES_PER_RESPONSE,
    OCCURRENCES_PER_RELATION,
    ExtractionSource,
    Scope,
    SourceRef,
    Span,
    TemporalOutputError,
    check_against_source,
    load_record,
    record_spans,
)

# Contract section 2 fixed v1 enrichment bounds; not MCP parameters.
INTRODUCTION_LIMIT = 2_048
CONTEXT_LIMIT = 8_192

ANNOTATION_KEYS = (
    "temporal_schema_version", "source_ref", "source_spans", "source_context",
    "temporal_annotations", "annotation_origin", "context_status",
    "occurrences_partial", "temporal_warnings",
)
_ROLES = {"heading", "introduction", "anchor"}
# ``context_only``: occurrences were not requested for this generation (#999);
# placement and context are kept, no record can be attached.
ORIGINS = ("extracted", "context_only")
_WARNINGS = {"context_budget_exceeded", "occurrence_budget_exceeded"}


# --- ingestion: attach the payload fragment to each chunk -------------------

def _span(text, start, end):
    return Span(start=start, end=end, quote=text[start:end])


def _dump(span):
    return {"start": span.start, "end": span.end, "quote": span.quote}


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


class _EvidenceIndex:
    """Fact-evidence ranges sorted by start, for overlap queries per passage."""

    def __init__(self, occurrences):
        entries = sorted(
            (span.start, span.end, position)
            for position, row in enumerate(occurrences) for span in row.fact_evidence)
        self.rows = occurrences
        self.starts = [entry[0] for entry in entries]
        self.entries = entries
        self.max_end = []
        running = 0
        for _, end, _ in entries:
            running = max(running, end)
            self.max_end.append(running)

    def linked(self, spans):
        found = set()
        for span in spans:
            index = bisect_left(self.starts, span.end) - 1
            while index >= 0 and self.max_end[index] > span.start:
                start, end, position = self.entries[index]
                if _overlaps(start, end, span.start, span.end):
                    found.add(position)
                index -= 1
        return sorted((self.rows[p] for p in found), key=lambda row: row.occurrence_id)


def _clip(start, limit, covered, remaining):
    """Longest stop <= limit whose characters outside ``covered`` fit ``remaining``."""
    cursor, used = start, 0
    for a, b in sorted(covered):
        if b <= cursor:
            continue
        if a >= limit:
            break
        if a > cursor:
            gap = min(a, limit) - cursor
            if used + gap > remaining:
                return cursor + (remaining - used), remaining
            used += gap
        cursor = max(cursor, b)
        if cursor >= limit:
            return limit, used
    if cursor < limit:
        gap = limit - cursor
        if used + gap > remaining:
            return cursor + (remaining - used), remaining
        used += gap
    return limit, used


def _uncovered(text, start, end, covered):
    """Whether any non-whitespace character of [start, end) lies outside ``covered``."""
    cursor = start
    for a, b in sorted(covered):
        if b <= cursor:
            continue
        if a >= end:
            break
        if a > cursor and text[cursor:a].strip():
            return True
        cursor = max(cursor, b)
        if cursor >= end:
            return False
    return cursor < end and bool(text[cursor:end].strip())


def _anchors(row):
    for time in (row.source_time, row.event_time, row.valid_time):
        if time is not None:
            yield from ([time.anchor] if time.anchor else []) + time.anchor_candidates


def _context(source, passage, records):
    """Required context closure of one passage (contract section 2), bounded."""
    text = source.text
    covered = [(s.start, s.end) for s in passage]
    entries, required = [], []
    remaining = CONTEXT_LIMIT
    for scope in source.touching(min(s.start for s in passage), max(s.end for s in passage)):
        for role, span in (("heading", scope.heading), ("introduction", scope.introduction)):
            if span is None:
                continue
            required.append((span.start, span.end))
            limit = span.end if role == "heading" else min(span.end, span.start + INTRODUCTION_LIMIT)
            stop, used = _clip(span.start, limit, covered, remaining)
            if stop > span.start:
                evidence = _span(text, span.start, stop)
                entries.append({"role": role, "scope": {"start": scope.start, "end": scope.end},
                                "evidence": [_dump(evidence)]})
                covered.append((span.start, stop))
                remaining -= used
    partial = any(_uncovered(text, a, b, covered) for a, b in required)
    seen = set()
    for row in records:
        for anchor in _anchors(row):
            for proof in anchor.expression_evidence:
                if (proof.start, proof.end) in seen or not _uncovered(text, proof.start, proof.end, covered):
                    continue
                seen.add((proof.start, proof.end))
                stop, used = _clip(proof.start, proof.end, covered, remaining)
                if stop < proof.end:
                    partial = True  # an unaffordable proof is never claimed as complete context
                    continue
                entries.append({"role": "anchor", "scope": anchor.scope.model_dump(),
                                "evidence": [_dump(proof)]})
                covered.append((proof.start, proof.end))
                remaining -= used
    return entries, partial


def _bounded(linked):
    """Whole records in ascending ID order within the per-unit bounds."""
    kept, used = [], 0
    for row in linked:
        size = len(canonical(row.model_dump(mode="json")).encode("utf-8"))
        if len(kept) >= OCCURRENCES_PER_RELATION or used + size > OCCURRENCE_BYTES_PER_RESPONSE:
            return kept, True
        kept.append(row)
        used += size
    return kept, False


def annotate_chunks(chunks, source: ExtractionSource, occurrences, source_ref: SourceRef,
                    *, origin: str = "extracted") -> None:
    """Attach the temporal payload fragment to every chunk, in place.

    ``chunk.text`` (the embedded string) is not touched. A chunk without a
    source range cannot be placed in the extracted text, so the generation
    fails instead of publishing an unplaced unit as annotated. Every record
    must satisfy the parser's source rules against ``source``
    (``check_against_source``): reindex reads the records back from storage,
    and a record's identity does not cover its times or anchors, so an altered
    quote or a borrowed, widened sibling anchor would otherwise be published
    as verified context. ``origin`` is the generation's, derived from its
    retained-text record by the caller; ``context_only`` admits no record.
    """
    occurrences = list(occurrences)
    if origin not in ORIGINS or origin == "context_only" and occurrences:
        raise TemporalOutputError("invalid_annotation_origin")
    for row in occurrences:
        check_against_source(row, source)
    index = _EvidenceIndex(occurrences)
    ref = source_ref.model_dump()
    for chunk in chunks:
        ranges = getattr(chunk, "source_spans", None)
        if not ranges:
            raise TemporalOutputError("unplaced_chunk")
        try:
            passage = [_span(source.text, r["start"], r["end"]) for r in ranges]
        except (KeyError, TypeError, ValueError):
            raise TemporalOutputError("unplaced_chunk") from None
        if passage[-1].end > len(source.text):
            raise TemporalOutputError("unplaced_chunk")
        records, overflow = _bounded(index.linked(passage))
        entries, context_partial = _context(source, passage, records)
        warnings = (["context_budget_exceeded"] if context_partial else []) + (
            ["occurrence_budget_exceeded"] if overflow else [])
        chunk.annotation = {
            "temporal_schema_version": 1,
            "source_ref": dict(ref),
            "source_spans": [_dump(s) for s in passage],
            "source_context": entries,
            "temporal_annotations": [row.model_dump(mode="json") for row in records],
            "annotation_origin": origin,
            "context_status": "partial" if warnings else "complete",
            "occurrences_partial": overflow,
            "temporal_warnings": sorted(warnings),
        }


# --- strict validation of a stored fragment ----------------------------------

def _spans(raw, *, inside: Scope | None = None):
    if type(raw) is not list or not raw:
        raise TemporalOutputError("invalid_temporal_association")
    spans = [Span.model_validate(item) for item in raw]
    if any(len(s.quote) != s.end - s.start or inside and not (inside.start <= s.start and s.end <= inside.end)
           for s in spans):
        raise TemporalOutputError("invalid_temporal_association")
    return spans


_GENERATED_PREFIX = re.compile(r"\[[^\n]*\] ")


def _collapse(value):
    return re.sub(r"\s+", " ", value).strip()


def _agree(spans):
    """Quotes that share a coordinate must share its characters.

    A forged quote of the right length is caught as soon as anything else in the
    fragment quotes the same place differently.
    """
    active = []  # (end, start, quote) of spans that may still overlap the next one
    for span in sorted(spans, key=lambda s: (s.start, s.end)):
        active = [entry for entry in active if entry[0] > span.start]
        for end, start, quote in active:
            a, b = span.start, min(end, span.end)
            if quote[a - start:b - start] != span.quote[:b - a]:
                raise TemporalOutputError("invalid_temporal_association")
        active.append((span.end, span.start, span.quote))


def _matches_index(passage, indexed_text):
    """The stored passage is the embedded string without its generated prefix."""
    quoted, embedded = _collapse("\n".join(s.quote for s in passage)), _collapse(indexed_text)
    if not embedded.endswith(quoted):
        raise TemporalOutputError("invalid_temporal_association")
    prefix = embedded[:len(embedded) - len(quoted)]
    if prefix and not _GENERATED_PREFIX.fullmatch(prefix):
        raise TemporalOutputError("invalid_temporal_association")


def check_fragment(fragment: Any, memory_id: str, doc_id: str | None = None, *,
                   indexed_text: str | None = None, source_text: str | None = None,
                   source: ExtractionSource | None = None) -> dict:
    """Validate a payload fragment end to end; raises ``TemporalOutputError``.

    Complete key set, version 1, namespace/document/version binding of the
    source reference and of every record (recomputed identity included), spans
    consistent with their quotes, each record's fact evidence overlapping the
    passage it is attached to, and all quotes agreeing wherever they share a
    coordinate. With ``indexed_text`` (the embedded string stored beside the
    fragment) the passage must be that string; with ``source_text`` (the
    retained text, hash-checked by the caller) every quote must be the exact
    slice of it. With ``source`` (that text parsed as at ingestion) every
    record must also satisfy the parser's source rules and the stored context
    must be exactly the closure that source yields for the passage and its
    records.
    """
    if source is not None:
        source_text = source.text
    try:
        return _check_fragment(fragment, memory_id, doc_id, indexed_text, source_text, source)
    except TemporalOutputError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise TemporalOutputError("unsupported_temporal_schema") from None


def verify_origins(payloads, origins: dict) -> None:
    """Each passage claims the origin its document's retained-text record implies.

    ``origins`` maps a document ID to that origin (#999): occurrences either
    were or were not requested for a generation, whatever a payload says.
    Passages of documents without a record are not concerned.
    """
    for payload in payloads:
        doc_id = payload.get("doc_id")
        if doc_id in origins and payload.get("annotation_origin") != origins[doc_id]:
            raise TemporalOutputError("annotation_origin_mismatch")


def verify_against_texts(memory_id: str, payloads, texts: dict, *, filenames: dict) -> None:
    """Imported passages must quote the retained text of their own document.

    ``texts`` maps a document ID to its hash-verified retained text; payloads of
    documents without a known text are not checked here (the vector boundary
    still validates them for internal consistency). ``filenames`` maps a
    document ID to the file name of its document record, the metadata reindex
    parses by: the text is parsed as ingestion parsed it (Markdown structure
    only for a ``.md`` name), and a passage whose own file name is missing or
    different is refused, since the payload being checked must never choose
    the structure it is checked against.
    """
    digests, sources = {}, {}
    for payload in payloads:
        if not any(key in payload for key in ANNOTATION_KEYS):
            continue
        text = texts.get(payload.get("doc_id"))
        if text is None:
            continue
        digest = digests.get(payload["doc_id"])
        if digest is None:  # hashing the whole document once, never per passage
            digest = digests[payload["doc_id"]] = sha256(text.encode("utf-8")).hexdigest()
        fragment = {key: payload[key] for key in ANNOTATION_KEYS if key in payload}
        ref = fragment.get("source_ref")
        if type(ref) is dict and ref.get("text_sha256") != digest:
            raise TemporalOutputError("source_version_mismatch")
        filename = filenames.get(payload["doc_id"])
        if type(filename) is not str or not filename or payload.get("filename") != filename:
            raise TemporalOutputError("document_filename_mismatch")
        extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        key = (payload["doc_id"], extension == "md")
        if key not in sources:  # parsed once per document, never per passage
            sources[key] = ExtractionSource(text, markdown=key[1])
        check_fragment(fragment, memory_id, payload["doc_id"], indexed_text=payload.get("text"), source=sources[key])


def _check_fragment(fragment, memory_id, doc_id, indexed_text=None, source_text=None, source=None):
    if type(fragment) is not dict or set(fragment) != set(ANNOTATION_KEYS):
        raise TemporalOutputError("unsupported_temporal_schema")
    version = fragment["temporal_schema_version"]
    if type(version) is not int or version != 1:
        raise TemporalOutputError("unsupported_temporal_schema")
    ref = SourceRef.model_validate(fragment["source_ref"])
    if ref.memory_id != memory_id or doc_id is not None and ref.doc_id != doc_id:
        raise TemporalOutputError("foreign_temporal_occurrence")
    passage = _spans(fragment["source_spans"])
    if [(s.start, s.end) for s in passage] != sorted({(s.start, s.end) for s in passage}):
        raise TemporalOutputError("invalid_temporal_association")
    contexts = fragment["source_context"]
    if type(contexts) is not list:
        raise TemporalOutputError("invalid_temporal_association")
    for entry in contexts:
        if type(entry) is not dict or set(entry) != {"role", "scope", "evidence"} or entry["role"] not in _ROLES:
            raise TemporalOutputError("invalid_temporal_association")
        scope = Scope.model_validate(entry["scope"])
        if scope.end <= scope.start:
            raise TemporalOutputError("invalid_temporal_association")
        _spans(entry["evidence"], inside=scope)
    stored = fragment["temporal_annotations"]
    if type(stored) is not list or len(stored) > OCCURRENCES_PER_RELATION:
        raise TemporalOutputError("invalid_temporal_association")
    total, previous, rows = 0, None, []
    for raw in stored:
        record_id = raw.get("occurrence_id") if type(raw) is dict else None
        if type(record_id) is not str or previous is not None and record_id <= previous:
            raise TemporalOutputError("invalid_temporal_association")
        previous = record_id
        encoded = canonical(raw)
        total += len(encoded.encode("utf-8"))
        row = load_record(encoded, memory_id, ref.doc_id, record_id)
        rows.append(row)
        if (row.source_ref.sha256, row.source_ref.text_sha256) != (ref.sha256, ref.text_sha256):
            raise TemporalOutputError("source_version_mismatch")
        if not any(_overlaps(f.start, f.end, s.start, s.end) for f in row.fact_evidence for s in passage):
            raise TemporalOutputError("invalid_temporal_association")
    if total > OCCURRENCE_BYTES_PER_RESPONSE:
        raise TemporalOutputError("invalid_temporal_association")
    warnings = fragment["temporal_warnings"]
    partial = fragment["occurrences_partial"]
    status = fragment["context_status"]
    origin = fragment["annotation_origin"]
    if origin == "context_only" and (rows or fragment["occurrences_partial"] is not False
                                     or any(entry["role"] == "anchor" for entry in contexts)):
        raise TemporalOutputError("invalid_temporal_association")  # nothing was requested to attach
    if (origin not in ORIGINS or status not in {"complete", "partial"}
            or type(partial) is not bool or type(warnings) is not list
            or warnings != sorted(set(warnings)) or not set(warnings) <= _WARNINGS
            or (status == "complete") != (not warnings and not partial)
            or partial != ("occurrence_budget_exceeded" in warnings)):
        raise TemporalOutputError("invalid_temporal_association")
    spans = list(passage) + [e for entry in contexts for e in _spans(entry["evidence"])]
    for row in rows:
        spans += record_spans(row)
    _agree(spans)
    if indexed_text is not None:
        if type(indexed_text) is not str:
            raise TemporalOutputError("invalid_temporal_association")
        _matches_index(passage, indexed_text)
    if source_text is not None and any(
            s.end > len(source_text) or source_text[s.start:s.end] != s.quote for s in spans):
        raise TemporalOutputError("invalid_temporal_association")
    if source is not None:
        for row in rows:
            check_against_source(row, source)
        # The context is derived, never trusted: it must be exactly what this
        # source yields for this passage and these records.
        entries, context_partial = _context(source, passage, rows)
        if entries != contexts or context_partial != ("context_budget_exceeded" in warnings):
            raise TemporalOutputError("invalid_temporal_association")
    return fragment


# --- retrieval: structured fields of one returned chunk ----------------------

def _unaligned(indexed_text, *, origin, reason):
    """A passage whose source placement is not known: kept, labelled, never claimed."""
    return {
        "temporal_schema_version": 1, "indexed_text": indexed_text, "chunk_text": None,
        "source_ref": None, "source_spans": [], "source_context": [], "temporal_annotations": [],
        "annotation_origin": origin, "context_status": "unavailable", "occurrences_partial": False,
    }, {reason}, True


def read_chunk(chunk, memory_id: str, budget):
    """Response fields for one annotated chunk: ``(fields, warning reasons, partial)``.

    ``budget`` is the response-wide ``OccurrenceBudget``. A passage without the
    extension is legacy and belongs to the bounded legacy context reader, never
    to this function. A malformed or foreign extension is reported as
    unsupported, never as legacy or as known time.
    """
    indexed_text = chunk.text
    fragment = getattr(chunk, "annotation", None)
    if not fragment:
        raise ValueError("read_chunk reads annotated passages; legacy passages use the legacy context reader")
    try:
        check_fragment(fragment, memory_id, getattr(chunk, "doc_id", None), indexed_text=indexed_text)
    except TemporalOutputError:
        return _unaligned(indexed_text, origin=None, reason="unsupported_temporal_schema")
    ref = fragment["source_ref"]
    rows = [(raw["occurrence_id"], ref["doc_id"], canonical(raw)) for raw in fragment["temporal_annotations"]]
    records, dropped = budget.select(rows, memory_id)
    partial = fragment["occurrences_partial"] or dropped
    status = fragment["context_status"]
    reasons = set(fragment["temporal_warnings"])
    if dropped:
        reasons.add("occurrence_budget_exceeded")
        status = worst_status(status, "partial")
    return {
        "temporal_schema_version": 1,
        "indexed_text": indexed_text,
        "chunk_text": "\n".join(span["quote"] for span in fragment["source_spans"]),
        "source_ref": dict(ref),
        "source_spans": [dict(span) for span in fragment["source_spans"]],
        "source_context": [dict(entry) for entry in fragment["source_context"]],
        "temporal_annotations": [row.model_dump(mode="json") for row in records],
        "annotation_origin": fragment["annotation_origin"],
        "context_status": status,
        "occurrences_partial": bool(partial),
    }, reasons, status != "complete"
