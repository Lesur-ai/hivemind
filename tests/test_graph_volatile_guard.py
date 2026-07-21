# -*- coding: utf-8 -*-
"""
P4-8 — GraphBridgeService.push() hardening: orphan-cleanup scoping + the
volatile-file guardrail, proven SAFE and non-authoritative.

This suite drives the REAL :class:`GraphBridgeService.push` through the same
deterministic seam the P4-4 suite uses (``test_long_engine.py``): a real bridge
wired with a :class:`FakeGraphTransport` client factory, over an in-memory
:class:`FakeStorage`. NO network / S3 / Neo4j / Qdrant / LLM / clock — every
``document_list`` / ``document_delete`` / ``memory_ingest`` response is canned
and every call is recorded, so each assertion below is reproducible.

LAYERING (reconciled EPIC-P4, ADR-0010/0012):

- The **bridge** owns the orphan-cleanup scoping and the volatile FILTER (a
  default push skips the configured volatile files and reports them in
  ``skipped_volatile``; ``include_volatile=True`` simply forwards through). The
  bridge does NO auth and NO audit — it is a verbatim pass-through.
- The **tool layer** (``tools/graph.py::graph_push``) owns the ``manage``
  permission gate AND the structured ``graph_push_volatile_optin`` audit, gating
  the opt-in BEFORE delegating to the engine/bridge. Those cases live in
  ``tests/test_graph_push_volatile_tool.py``.

Two safety properties pinned HERE (bridge level):

1. ORPHAN-CLEANUP SCOPING (data-loss critical). ``push`` must NEVER issue a
   ``document_delete`` for a canonical / archive document that merely lacks a
   matching CURRENT bank filename. Cleanup may only delete docs that were in the
   PRIOR recorded bank-mirror set (``graph_memory["bank_mirror"]`` in local
   ``_meta``) AND are absent from the current bank. The PRIMARY whitelist is the
   ledger: a canonical doc is never recorded in ``bank_mirror`` (which holds only
   bank filenames), so it is never an orphan candidate. P7-8 adds a SECOND
   independent protection: the real GM ``document_list`` exposes ``source_path``
   (always present, ``None`` if absent), and only docs with a nul ``source_path``
   are id-resolvable mirror copies — a canonical doc is excluded from the delete
   map even when it shares a bank filename (tests/test_p7_8_security_backup.py).
   The load-bearing test here seeds a canonical doc absent from the ledger and
   asserts push does NOT delete it.

2. VOLATILE-FILE FILTER. Default push SKIPS ``activeContext.md`` + ``progress.md``,
   reports them in ``skipped_volatile``, and does NOT ingest (or delete) them.
   Volatile filtering runs AFTER ``bank_relpath`` normalization, so a
   ``1.MEMORY_BANK/activeContext.md``-prefixed object key is still filtered.

3. RESPONSE BACK-COMPAT. The response keeps ``pushed:int`` plus the existing
   ``cleaned_orphans`` + ``duration_seconds`` keys; ``pushed_files`` and
   ``skipped_volatile`` are ADDITIVE (no renames). The P4-5 watermark recording
   stays intact.

The bridge reads the volatile file set from
``config.get_settings().graph_push_volatile_files`` (default
``("activeContext.md", "progress.md")``).
"""

from __future__ import annotations

import base64
import json
import logging
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from live_mem.core.graph_bridge import GraphBridgeService


# =============================================================================
# In-memory storage fake — identical idiom to test_long_engine.py / test_
# hivemind_state.py: only the methods push() touches (get_json / put_json /
# list_and_get). Fully deterministic, no S3.
# =============================================================================


class FakeStorage:
    """Minimal in-memory StorageService stand-in. No S3, fully deterministic."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append(
                    {"Key": key, "Size": len(self.objects[key]), "LastModified": ""}
                )
        return out

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        results: list[dict] = []
        for obj in await self.list_objects(prefix):
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": "",
                    }
                )
        return results

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)


# Import the shared transport fake AFTER FakeStorage so the file reads top-down
# like its sibling suite.
from tests.fakes import FakeGraphTransport  # noqa: E402


# =============================================================================
# Fixtures / helpers
# =============================================================================

_CONNECTED_URL = "https://gm.example.com"
_SPACE = "space-a"
_MEM = "mem-1"


def _meta_connected(
    *,
    url: str = _CONNECTED_URL,
    memory_id: str = _MEM,
    bank_mirror: list[str] | None = None,
) -> dict:
    """A connected-space ``_meta.json``.

    ``bank_mirror`` seeds the prior recorded bank-mirror filename set in the
    local-only ``graph_memory`` block — the orphan-scoping ledger push uses to
    decide which existing docs it is allowed to clean. Omitted => no prior set
    recorded (first push / legacy meta).
    """
    gm: dict[str, Any] = {
        "url": url,
        "token": "tok-secret",
        "memory_id": memory_id,
        "ontology": "general",
        "last_push": None,
        "push_count": 0,
        "files_pushed": 0,
    }
    if bank_mirror is not None:
        gm["bank_mirror"] = list(bank_mirror)
    return {"space_id": _SPACE, "version": 1, "graph_memory": gm}


def _build(storage: "FakeStorage", **factory_kwargs):
    """Real bridge wired with a fake transport factory. Returns (bridge, factory)."""
    factory = FakeGraphTransport.factory(**factory_kwargs)
    bridge = GraphBridgeService(client_factory=factory)
    return bridge, factory


def _patch_storage(storage: "FakeStorage"):
    """Redirect the bridge's get_storage() to the in-memory fake (module-level
    patch, same as test_long_engine.py)."""
    return patch("live_mem.core.graph_bridge.get_storage", return_value=storage)


def _doc_list_response(documents: list[dict]) -> dict:
    """Canned ``document_list`` payload shaped like the real GM list response:
    each doc carries ``filename`` AND ``id`` (P7-8: deletes are keyed by
    ``document_id``, resolved from this list — a deterministic ``id-<filename>``
    is injected when the test does not seed one explicitly). The real GM list
    also exposes ``source_path`` (``None`` when absent); docs seeded here
    without one are mirror-shaped, which keeps this suite's ledger-scoping
    assertions load-bearing on their own (the source_path protection is
    covered by tests/test_p7_8_security_backup.py)."""
    docs = []
    for doc in documents:
        entry = dict(doc)
        entry.setdefault("id", f"id-{entry.get('filename', '')}")
        docs.append(entry)
    return {"status": "ok", "memory_id": _MEM, "documents": docs}


# Tool names used as a vocabulary in assertions.
DELETE = "document_delete"
INGEST = "memory_ingest"


def _deleted_filenames(inst: "FakeGraphTransport") -> list[str]:
    """All filenames push issued a document_delete for (re-ingest deletes +
    orphan cleans, in call order). P7-8: the bridge deletes by ``document_id``
    (never ``filename``); this helper asserts that contract on every recorded
    delete, then maps the deterministic ``id-<filename>`` ids back to
    filenames so the suite's intent-level assertions stay readable."""
    filenames = []
    for a in inst.args_for(DELETE):
        assert "filename" not in a, (
            f"push issued a filename-keyed document_delete {a!r} — the real GM "
            f"tool is document_id-keyed (P7-8)"
        )
        doc_id = a["document_id"]
        assert doc_id.startswith("id-"), f"unexpected canned document_id {doc_id!r}"
        filenames.append(doc_id[len("id-"):])
    return filenames


def _ingested_filenames(inst: "FakeGraphTransport") -> list[str]:
    return [a["filename"] for a in inst.args_for(INGEST)]


# =============================================================================
# CASE 1 — ORPHAN-CLEANUP SCOPING (the load-bearing data-loss test).
#
# A canonical / source_path-keyed doc that lacks a matching CURRENT bank
# filename must NOT be deleted. Today's `existing_docs - bank_files.keys()`
# would delete it; the hardened scoping must spare it.
# =============================================================================


async def test_push_never_deletes_canonical_source_path_doc() -> None:
    """LOAD-BEARING: a canonical (P4-7, out-of-band) doc among the existing GM
    docs must survive a push even though no current bank file maps to its
    filename. It is spared by the PRIMARY protection — the bank-mirror LEDGER
    whitelist: a canonical doc is never recorded in ``bank_mirror`` (that ledger
    holds only bank filenames), so it is never an orphan candidate — even
    without the independent P7-8 source_path protection (the doc is seeded
    mirror-shaped here precisely so the ledger alone carries this test).
    This is the data-loss regression the whole task exists to prevent."""
    storage = FakeStorage()
    # Prior bank-mirror set: only systemPatterns.md was previously mirrored.
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        _meta_connected(bank_mirror=["systemPatterns.md"]),
    )
    # Current bank has one non-volatile file.
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    # GM already contains: the bank-mirror doc (will be re-ingested) AND a
    # canonical doc ingested out-of-band by P4-7 — NOT in the bank-mirror ledger.
    # Its filename matches no current bank file, so the FORBIDDEN naive orphan
    # diff (existing_docs - bank_files) WOULD delete it; the ledger scoping must
    # not. (Seeded mirror-shaped — source_path None — so the LEDGER alone is
    # what protects it here; the source_path protection is tested separately.)
    responses = {
        "document_list": _doc_list_response(
            [
                {"filename": "systemPatterns.md"},
                {"filename": "rfc-0007-routing.md"},
            ]
        )
    }
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    deleted = _deleted_filenames(inst)

    # ── The assertion the task is built around ──────────────────────────────
    assert "rfc-0007-routing.md" not in deleted, (
        "DATA LOSS: push deleted a canonical source_path doc that merely "
        "lacked a matching current bank filename"
    )
    # The only delete allowed here is the re-ingest delete of the bank-mirror
    # doc (delete-then-ingest), NOT an orphan clean of the canonical doc.
    assert deleted == ["systemPatterns.md"]
    # And it WAS re-ingested.
    assert "systemPatterns.md" in _ingested_filenames(inst)
    # cleaned_orphans counts zero true orphans (the canonical doc is out of
    # scope, not an orphan).
    assert result["cleaned_orphans"] == 0


async def test_push_cleans_only_orphans_in_recorded_bank_mirror_set() -> None:
    """A doc IS eligible for orphan cleanup only if it was in the PRIOR recorded
    bank-mirror set and is absent from the current bank. A bank-mirror doc that
    fell out of the bank gets cleaned; a foreign/canonical doc never does."""
    storage = FakeStorage()
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        # Previously mirrored two bank files; one is now gone from the bank.
        _meta_connected(bank_mirror=["systemPatterns.md", "techContext.md"]),
    )
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    # techContext.md no longer in bank -> a true bank-mirror orphan.

    responses = {
        "document_list": _doc_list_response(
            [
                {"filename": "systemPatterns.md"},
                {"filename": "techContext.md"},  # bank-mirror orphan -> clean
                # canonical doc, never in the bank-mirror ledger -> spared by the
                # ledger whitelist (no source_path needed: GM list omits it).
                {"filename": "incident-2031.md"},
            ]
        )
    }
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    deleted = _deleted_filenames(inst)
    # techContext.md cleaned (re-ingest delete impossible — it's not in bank),
    # systemPatterns.md delete is the re-ingest delete. incident never touched.
    assert "techContext.md" in deleted
    assert "incident-2031.md" not in deleted
    assert result["cleaned_orphans"] == 1
    # The recorded bank-mirror ledger is rewritten to the CURRENT mirror set.
    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    assert meta["graph_memory"]["bank_mirror"] == ["systemPatterns.md"]


# =============================================================================
# CASE 2 — DEFAULT PUSH SKIPS VOLATILE FILES, reports them, does NOT ingest.
# =============================================================================


async def test_default_push_skips_and_reports_volatile_without_ingesting() -> None:
    """Default push (include_volatile=False) must NOT ingest activeContext.md or
    progress.md, must list them in skipped_volatile, and must still push the
    durable file."""
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/progress.md", "prog")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)  # default => include_volatile False

    inst = factory.instances[-1]
    ingested = _ingested_filenames(inst)

    # Volatile files never reach memory_ingest.
    assert "activeContext.md" not in ingested
    assert "progress.md" not in ingested
    # The durable file IS pushed.
    assert ingested == ["systemPatterns.md"]
    assert result["pushed"] == 1

    # skipped_volatile reports BOTH volatile files (order-insensitive).
    assert set(result["skipped_volatile"]) == {"activeContext.md", "progress.md"}

    # And they are not silently deleted either (no document_delete for them).
    assert "activeContext.md" not in _deleted_filenames(inst)
    assert "progress.md" not in _deleted_filenames(inst)

    # No manage check is consulted on the default path (guardrail is opt-IN).
    assert result["status"] == "ok"


# =============================================================================
# CASE 5 — volatile filter runs AFTER bank_relpath normalization.
#
# A bank object stored under a "1.MEMORY_BANK/" subfolder normalizes (via
# bank_relpath) to "1.MEMORY_BANK/activeContext.md". The volatile filter must
# match on the BASENAME of the normalized relpath, so this prefixed file is
# still skipped by default — proving the filter is post-normalization, not a
# raw-key match.
# =============================================================================


async def test_volatile_filter_matches_basename_after_bank_relpath() -> None:
    """A '1.MEMORY_BANK/activeContext.md'-style relpath is still treated as
    volatile (filter is on the normalized basename, AFTER bank_relpath)."""
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    # Subfolder-prefixed volatile file; bank_relpath -> "1.MEMORY_BANK/activeContext.md".
    await storage.put(f"{_SPACE}/bank/1.MEMORY_BANK/activeContext.md", "ctx")
    # A durable file in the same subfolder to prove only the volatile one is skipped.
    await storage.put(f"{_SPACE}/bank/1.MEMORY_BANK/decisions.md", "decisions")
    bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    ingested = _ingested_filenames(inst)

    # The prefixed volatile file is NOT ingested.
    assert "1.MEMORY_BANK/activeContext.md" not in ingested
    # The prefixed durable file IS ingested (with its full relpath as filename).
    assert ingested == ["1.MEMORY_BANK/decisions.md"]
    # Reported under its NORMALIZED relpath (what GM would key on).
    assert result["skipped_volatile"] == ["1.MEMORY_BANK/activeContext.md"]


# =============================================================================
# CASE 6 — RESPONSE BACK-COMPAT: pushed:int + cleaned_orphans +
# duration_seconds all survive; pushed_files / skipped_volatile are additive;
# the P4-5 watermark recording is untouched.
# =============================================================================


async def test_response_is_additive_and_backcompat() -> None:
    """The push response keeps the legacy keys (pushed:int, cleaned_orphans,
    duration_seconds) and ADDS pushed_files + skipped_volatile — no renames, no
    removals."""
    storage = FakeStorage()
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    # One pre-existing bank-mirror doc that's now gone -> a real orphan to count.
    responses = {
        "document_list": _doc_list_response(
            [{"filename": "systemPatterns.md"}, {"filename": "techContext.md"}]
        )
    }
    # Make techContext a recorded mirror orphan.
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        _meta_connected(bank_mirror=["systemPatterns.md", "techContext.md"]),
    )
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    # ── Legacy keys preserved with their original types/semantics ───────────
    assert result["status"] == "ok"
    assert isinstance(result["pushed"], int)
    assert result["pushed"] == 1  # only the durable file (volatile skipped)
    assert "cleaned_orphans" in result and isinstance(result["cleaned_orphans"], int)
    assert result["cleaned_orphans"] == 1  # techContext.md mirror orphan
    assert "duration_seconds" in result
    # Legacy companions still present (not removed by the refactor).
    assert "deleted_before_reingest" in result
    assert "errors" in result

    # ── Additive keys ───────────────────────────────────────────────────────
    assert result["skipped_volatile"] == ["activeContext.md"]
    # pushed_files enumerates exactly the durable files actually ingested, and
    # its length agrees with the pushed counter (no drift between the two).
    assert result["pushed_files"] == ["systemPatterns.md"]
    assert len(result["pushed_files"]) == result["pushed"]


async def test_response_watermark_recording_intact() -> None:
    """The P4-5 watermark block (bank_version / commit_id / term / provenance /
    recorded_at) must still be written by push — the volatile/orphan hardening
    is orthogonal to it."""
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    # Seed committed coords so push records a real watermark (P4-5 path).
    await storage.put_json(
        f"{_SPACE}/_hivemind/bank_version.json",
        {"bank_version": 7, "commit_id": "c-7"},
    )
    await storage.put_json(
        f"{_SPACE}/_hivemind/commits/{7:020d}.json",
        {"term": 2, "commit_id": "c-7"},
    )
    bridge, factory = _build(storage)

    with _patch_storage(storage):
        await bridge.push(_SPACE)

    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    gm = meta["graph_memory"]
    # Watermark coords recorded downstream-only, exactly as P4-5 specifies.
    assert gm["bank_version"] == 7
    assert gm["commit_id"] == "c-7"
    assert gm["term"] == 2
    assert gm["provenance"] == "mid-consolidation"
    assert gm["flagged"] is False
    assert gm["recorded_at"] is not None
    # Push bookkeeping still updated alongside.
    assert gm["push_count"] == 1


# =============================================================================
# CASE 7 (added P4-8) — an all-volatile bank still reconciles recorded
# bank-mirror orphans. It must NOT take the empty-return path: a client IS
# built and a recorded orphan IS cleaned, even though nothing is ingested.
# =============================================================================


async def test_all_volatile_bank_still_reconciles_mirror_orphans() -> None:
    """Bank contains ONLY volatile files -> nothing ingested, both reported in
    skipped_volatile, BUT a recorded bank-mirror orphan (stale.md) is still
    cleaned. Proves an all-volatile bank does not strand recorded orphans and
    does not take the empty-bank early-return."""
    storage = FakeStorage()
    await storage.put_json(
        f"{_SPACE}/_meta.json", _meta_connected(bank_mirror=["stale.md"])
    )
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/progress.md", "prog")

    responses = {"document_list": _doc_list_response([{"filename": "stale.md"}])}
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    # Not the empty-return path: a GM client WAS built (orphan reconciliation).
    assert factory.instances != []
    inst = factory.instances[-1]

    assert result["pushed"] == 0
    assert result["pushed_files"] == []
    assert set(result["skipped_volatile"]) == {"activeContext.md", "progress.md"}
    # The recorded orphan was cleaned.
    assert result["cleaned_orphans"] == 1
    assert "stale.md" in _deleted_filenames(inst)
    # The mirror ledger now reflects the (empty) post-filter current bank.
    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    assert meta["graph_memory"]["bank_mirror"] == []


# =============================================================================
# CASE 8 (added P4-8) — cold start: no prior bank_mirror ledger -> deletes
# NOTHING (intended safe under-delete), then records the current bank-mirror.
# =============================================================================


async def test_first_push_cold_start_deletes_nothing() -> None:
    """First post-upgrade push: no ``bank_mirror`` key in _meta -> prior set is
    empty -> ZERO orphan deletes for a foreign doc, cleaned_orphans == 0, and
    the bank-mirror ledger is recorded to the current bank afterward."""
    storage = FakeStorage()
    # No bank_mirror key in the graph_memory block (legacy/cold-start meta).
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    # GM already holds a foreign doc that is NOT in any prior mirror set.
    responses = {
        "document_list": _doc_list_response(
            [{"filename": "systemPatterns.md"}, {"filename": "foreign.md"}]
        )
    }
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    deleted = _deleted_filenames(inst)
    # The foreign doc is NOT deleted on cold start (prior_mirror is empty).
    assert "foreign.md" not in deleted
    assert result["cleaned_orphans"] == 0
    # The only delete is the re-ingest delete of systemPatterns.md.
    assert deleted == ["systemPatterns.md"]
    # The bank-mirror ledger is now recorded to the current bank.
    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    assert meta["graph_memory"]["bank_mirror"] == ["systemPatterns.md"]


# =============================================================================
# CASE 9 (added P4-8) — a skipped volatile doc already present in GM AND in the
# prior bank-mirror ledger is neither ingested NOR deleted. Pins the Design-B
# data-loss interaction (volatile filter must not turn a skipped file into its
# own orphan).
# =============================================================================


async def test_volatile_skip_never_becomes_orphan() -> None:
    """activeContext.md present in the bank AND in GM AND in the prior mirror
    ledger: a default push skips it (not ingested) AND does not delete it (it is
    excluded from orphan candidates by the configured volatile basenames)."""
    storage = FakeStorage()
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        _meta_connected(bank_mirror=["activeContext.md", "systemPatterns.md"]),
    )
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    responses = {
        "document_list": _doc_list_response(
            [{"filename": "activeContext.md"}, {"filename": "systemPatterns.md"}]
        )
    }
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    # Skipped: not ingested.
    assert "activeContext.md" not in _ingested_filenames(inst)
    # And NOT deleted — neither as a re-ingest delete nor an orphan clean.
    assert "activeContext.md" not in _deleted_filenames(inst)
    assert result["cleaned_orphans"] == 0
    assert result["skipped_volatile"] == ["activeContext.md"]


# =============================================================================
# CASE 10 (P4-8) — a doc in the prior mirror ledger but absent from the current
# bank IS a stale bank-mirror orphan and IS cleaned. A mirror-shaped doc
# (source_path None, id resolved) that sits in the ledger is a legitimate
# cleanup target; a canonical doc is protected TWICE (never in the ledger, and
# excluded from the P7-8 mirror-id map by its non-null source_path).
# This is the honest complement of the data-loss test above — cleanup works.
# =============================================================================


async def test_stale_bank_mirror_doc_is_cleaned() -> None:
    """A doc that WAS in the prior bank-mirror ledger but is absent from the
    current bank is a genuine stale mirror orphan and is cleaned. Proves orphan
    cleanup of the bank-mirror namespace actually works (no inert veto masking
    it)."""
    storage = FakeStorage()
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        _meta_connected(bank_mirror=["systemPatterns.md", "promoted.md"]),
    )
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    # promoted.md was a bank file last push, now gone from the bank -> stale.

    responses = {
        "document_list": _doc_list_response(
            [
                {"filename": "systemPatterns.md"},
                {"filename": "promoted.md"},
            ]
        )
    }
    bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    inst = factory.instances[-1]
    assert "promoted.md" in _deleted_filenames(inst)
    assert result["cleaned_orphans"] == 1


# =============================================================================
# CASE 11 (added P4-8) — empty bank: the early-return dict carries the additive
# fields and writes NO bank_mirror / watermark to _meta (no GM contact). Pins
# the watermark-test invariant.
# =============================================================================


async def test_empty_bank_returns_additive_fields() -> None:
    """An empty-bank push early-returns with pushed:0, the additive fields
    present (pushed_files == [] / skipped_volatile == []), builds NO client, and
    writes no bank_mirror / watermark to _meta."""
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    # No bank files seeded.
    bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await bridge.push(_SPACE)

    assert result["pushed"] == 0
    assert result["pushed_files"] == []
    assert result["skipped_volatile"] == []
    # No GM contact on the empty-bank early-return.
    assert factory.instances == []
    # _meta carries no orphan ledger or watermark (no meta mutation at all).
    gm = (await storage.get_json(f"{_SPACE}/_meta.json"))["graph_memory"]
    assert "bank_mirror" not in gm
    assert "provenance" not in gm
    assert gm["push_count"] == 0
