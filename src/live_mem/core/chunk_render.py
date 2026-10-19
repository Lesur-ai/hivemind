"""Directly usable text of a LONG passage and the shared response bounds.

Pure and dependency-free on purpose: ``src/live_mem/core/chunk_render.py`` is a
byte-identical copy (the façade cannot import the Graph Memory runtime) and a
test pins the two files together. Contract: DESIGN/hivemind/LONG_TEMPORAL_CONTRACT_V1.md
section 5. Nothing here reads a model, a clock or the network; the text only
restates stored fields, labelling technical capture/ingestion apart from
source, event and validity time.
"""
from __future__ import annotations

import json

# Contract section 5 response-wide bounds for occurrence enrichment.
RECORDS_PER_RESPONSE = 128
BYTES_PER_RESPONSE = 65_536

# Precedence when several context states apply: unavailable, ambiguous, partial, complete.
_STATUS_RANK = {"complete": 0, "partial": 1, "ambiguous": 2, "unavailable": 3}


def worst_status(left, right):
    return left if _STATUS_RANK[left] >= _STATUS_RANK[right] else right


def canonical(record) -> str:
    """Stable serialized record; the size counted against the response budget."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _compact(value):
    return "null" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _point(point):
    return {key: point[key] for key in ("kind", "value", "precision", "timezone")}


def _normalized(value):
    """Fixed key order: a store may return JSON objects sorted differently."""
    if value is None:
        return "null"
    if value["kind"] == "point":
        return _compact(_point(value))
    return _compact({
        "kind": "interval",
        "start": None if value["start"] is None else _point(value["start"]),
        "end": None if value["end"] is None else _point(value["end"]),
        "bounds": value["bounds"],
    })


def _anchor_lines(heading, anchor):
    lines = [f"{heading}: {anchor['expression']}"]
    lines += [f"Anchor evidence [{s['start']}:{s['end']}]: {s['quote']}" for s in anchor["expression_evidence"]]
    return lines


def _time_lines(label, time):
    expression = "null" if time["expression"] is None else json.dumps(time["expression"], ensure_ascii=False)
    lines = [f"{label}: {time['status']}; expression={expression}; precision={time['precision']}; "
             f"normalized={_normalized(time['normalized'])}"]
    if time["anchor"] is not None:
        lines += _anchor_lines("Selected anchor", time["anchor"])
    for candidate in time["anchor_candidates"]:
        lines += _anchor_lines("Unselected candidate", candidate)
    return lines


def _unaligned_reason(row):
    if row.get("annotation_origin") is None:
        return "unsupported temporal annotation"
    if row.get("context_status") == "ambiguous":
        return "structural placement ambiguous"
    return "source context unavailable"


def _technical_lines(row):
    lines = []
    if row.get("captured_at"):
        lines.append(f"Technical capture time (not historical): {row['captured_at']}")
    if row.get("ingested_at"):
        lines.append(f"Technical ingestion time (not historical): {row['ingested_at']}")
    return lines


def render_text(row) -> str:
    """The passage with its identity, context quotations and derived annotations.

    An unresolved relative expression stays unresolved: only the selected
    anchor and its quoted proof are attached, never a computed date.
    """
    ref = row.get("source_ref")
    if ref is None or row.get("chunk_text") is None:
        lines = [f"[Unaligned indexed passage; {_unaligned_reason(row)}]", row["indexed_text"]]
        return "\n".join(lines + _technical_lines(row))
    lines = [f"[Source: {ref['memory_id']}/{ref['doc_id']}; sha256={ref['sha256']}; "
             f"text_sha256={ref['text_sha256']}]",
             f"Context status: {row['context_status']}"]
    for entry in row["source_context"]:
        for span in entry["evidence"]:
            lines.append(f"Source context [{entry['role']}; {span['start']}:{span['end']}]: {span['quote']}")
    lines += ["Source passage:", row["chunk_text"]]
    for record in row["temporal_annotations"]:
        fact = record["fact"]
        lines.append(f"Derived temporal annotation {record['occurrence_id']}: "
                     f"{fact['from_entity']} --{fact['type']}--> {fact['to_entity']}")
        lines += _time_lines("Source time", record["source_time"])
        lines += _time_lines("Event time", record["event_time"])
        validity = record.get("valid_time")
        if validity is None:
            lines.append("Validity: not expressed")
        else:
            lines += _time_lines("Validity", validity)
    return "\n".join(lines + _technical_lines(row))


def rebound(rows):
    """Re-apply the response-wide occurrence bounds to already-rendered rows.

    Needed after the façade interleaves two namespaces, each of which was
    bounded on its own. Whole records only, ranked order, repeated payloads
    counted. A row that loses a record states it (``occurrences_partial``,
    partial context) and its text is rendered again from the kept records, so
    text and structure always describe the same subset. Returns the rows and
    one warning per namespace that lost a record.
    """
    seen, used, trimmed, bounded = set(), 0, set(), []
    for row in rows:
        records = row.get("temporal_annotations") or []
        kept = []
        for record in records:
            size = len(canonical(record).encode("utf-8"))
            record_id = record.get("occurrence_id")
            if record_id not in seen and len(seen) >= RECORDS_PER_RESPONSE or used + size > BYTES_PER_RESPONSE:
                continue
            seen.add(record_id)
            used += size
            kept.append(record)
        if len(kept) != len(records):
            trimmed.add(str(row.get("memory_id", "")))
            row = {**row, "temporal_annotations": kept, "occurrences_partial": True,
                   "context_status": worst_status(row["context_status"], "partial")}
            row["text"] = render_text(row)
        bounded.append(row)
    warnings = [{"memory_id": memory_id, "reason": "occurrence_budget_exceeded"}
                for memory_id in sorted(trimmed)]
    return bounded, warnings
