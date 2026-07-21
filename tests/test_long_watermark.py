# -*- coding: utf-8 -*-
"""
P4-5 (EPIC-P4 + ADR-0017, refines ADR-0010) — derived, READ-ONLY long-push
watermark.

WHAT THIS LOCKS
---------------
When a long ``push`` consumes an already-committed mid bank version, the bridge
records the consumed coords — ``(bank_version, commit_id, term, provenance)`` —
as FLAT keys of the LOCAL-ONLY ``graph_memory`` block of ``_meta.json`` (the
derived watermark). The watermark is strictly downstream bookkeeping:

- it inherits locality from the ``graph_memory`` block (no ``SHARED_META_FIELDS``
  change) — it appears in ``meta_local_complement`` and NEVER in
  ``meta_shared_projection`` (case 3);
- when committed coords are ABSENT (the common case — protocol issue #8 commit
  production is not landed), coords degrade to ``null`` / ``"not available"``,
  never fabricated, while provenance is still present (case 2);
- it is monotone-or-absent: a later push recording an OLDER ``bank_version`` than
  the last recorded one is FLAGGED and does NOT regress recorded forward progress
  (case 5);
- no commit-path module READS the watermark — verified structurally by an
  AST / negative-import gate (case 4);
- the push response stays backward-compatible: ``pushed:int`` +
  ``cleaned_orphans`` + ``duration_seconds`` preserved (case 6).

The coord SOURCE is the committed bank state under ``{space_id}/_hivemind/``:
``layout.bank_version_key`` (the CURRENT pointer) and
``layout.commit_key(space_id, bank_version)`` (the commit journal entry, a
``BankCommit`` JSON). Reading those is ALLOWED (ADR-0017 downstream consumption)
and is done in ``graph_bridge.push`` via storage + ``layout`` path helpers,
WITHOUT importing any commit-DECISION module.

OFFLINE / DETERMINISTIC
-----------------------
No network / S3 / Neo4j / Qdrant / LLM / real clock. Drives the REAL
``GraphBridgeService`` through the P4-4 client-injection seam
(``client_factory=FakeGraphTransport.factory(...)``) over an in-memory
``FakeStorage``, with the bridge module's wall clock pinned to a fixed instant
(injected time) so any provenance/recorded-at timestamp is reproducible.

CONTRACT (red → green)
----------------------
This suite pins the P4-5 contract that ``graph_bridge.push`` records:

- the watermark lives as FLAT keys of ``meta["graph_memory"]`` — matching the
  already-merged ``test_meta_shared_local.py`` contract and the ratified
  ``models.GraphMemoryConfig`` docstring; ``GraphMemoryConfig`` ignores these as
  extra fields (the raw dict round-trips), and they inherit ``graph_memory``-block
  locality verbatim;
- keys: ``bank_version`` / ``commit_id`` / ``term`` (the consumed coords),
  ``provenance`` (always present, ``"mid-consolidation"`` when available), and a
  ``flagged`` marker used by the monotone-or-absent rule;
- absent coords serialize as JSON ``null`` AND the human-facing provenance
  string contains ``"not available"``.
"""

from __future__ import annotations

import ast
import inspect
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.hivemind import layout
from live_mem.core.models import (
    meta_local_complement,
    meta_shared_projection,
)
from tests.fakes import FakeGraphTransport

# A frozen instant for the injected clock — every push in this module records
# provenance/recorded-at against THIS, so timestamps are reproducible and tests
# never read the real wall clock.
_FROZEN = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
_FROZEN_ISO = _FROZEN.isoformat()

_SPACE = "space-a"
_META_KEY = f"{_SPACE}/_meta.json"
_GM_URL = "https://gm.example.com"


# =============================================================================
# In-memory storage fake (idiom from tests/test_hivemind_state.py +
# tests/test_long_engine.py) — only the methods the bridge + watermark read
# touch: get / put / get_json / put_json / list_objects / list_and_get.
# =============================================================================


class FakeStorage:
    """Minimal in-memory StorageService stand-in. No S3, fully deterministic."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> Optional[str]:
        return self.objects.get(key)

    async def get_json(self, key: str) -> Optional[dict]:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append(
                    {"Key": key, "Size": len(self.objects[key]), "LastModified": ""}
                )
                if max_keys and len(out) >= max_keys:
                    break
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


# =============================================================================
# Builders / fixtures
# =============================================================================


def _meta_connected() -> dict:
    """A space already connected to Graph Memory (local-only graph_memory block).

    Mirrors the shape ``graph_connect`` writes: the 7 ``GraphMemoryConfig``
    fields, no watermark yet (the push under test is what records it).
    """
    return {
        "space_id": _SPACE,
        "description": "d",
        "owner": "o",
        "created_at": "2026-01-01T00:00:00Z",
        "version": 1,
        "graph_memory": {
            "url": _GM_URL,
            "token": "tok-secret",
            "memory_id": "mem-1",
            "ontology": "general",
            "last_push": None,
            "push_count": 0,
            "files_pushed": 0,
        },
    }


def _commit_doc(bank_version: int, commit_id: str, term: int) -> dict:
    """A committed ``BankCommit`` JSON as written under
    ``{space_id}/_hivemind/commits/{bank_version:020d}.json``.

    Shaped like ``core/hivemind/models.BankCommit`` (the watermark read only
    needs ``bank_version`` / ``commit_id`` / ``term``)."""
    return {
        "protocol_version": 1,
        "bank_version": bank_version,
        "parent_bank_version": bank_version - 1,
        "term": term,
        "commit_id": commit_id,
        "committed_by_node_id": "node-1",
        "committed_at": "2026-01-02T00:00:00Z",
        "manifest": [],
        "notes_consumed": [],
    }


async def _seed_committed(
    storage: FakeStorage, bank_version: int, commit_id: str, term: int
) -> None:
    """Seed a *committed* mid coord: the current pointer + the commit journal
    entry, exactly where the watermark read sources them from."""
    await storage.put_json(
        layout.bank_version_key(_SPACE),
        {
            "protocol_version": 1,
            "bank_version": bank_version,
            "commit_id": commit_id,
            "updated_at": "2026-01-02T00:00:00Z",
        },
    )
    await storage.put_json(
        layout.commit_key(_SPACE, bank_version),
        _commit_doc(bank_version, commit_id, term),
    )


async def _seed_bank(storage: FakeStorage) -> None:
    """Two DURABLE bank files so push has something to push (pushed == 2).

    P4-8: ``activeContext.md`` / ``progress.md`` are now volatile (skipped by a
    default push), so this helper seeds non-volatile files instead — the
    watermark contract under test is orthogonal to the volatile guardrail and
    must still observe a real ingest (pushed >= 1)."""
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    await storage.put(f"{_SPACE}/bank/techContext.md", "tech")


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


def _build(**factory_kwargs):
    """Wire a REAL bridge over a deterministic fake transport.

    Returns ``(bridge, factory)``. The default canned ``document_list`` (empty)
    means every bank file is a fresh ingest, no orphan to clean — callers that
    care about orphans override ``responses``.
    """
    factory = FakeGraphTransport.factory(**factory_kwargs)
    bridge = GraphBridgeService(client_factory=factory)
    return bridge, factory


def _patch_env(storage: FakeStorage):
    """Patch BOTH seams the bridge push path touches in-module:

    - ``get_storage`` → our in-memory fake (no S3);
    - ``datetime`` → a frozen clock so any recorded-at / provenance timestamp is
      reproducible (injected time; never the real wall clock).

    ``patch.multiple`` returns a context manager that applies both at once.
    """
    return patch.multiple(
        "live_mem.core.graph_bridge",
        get_storage=lambda: storage,
        datetime=_FrozenDatetime,
    )


class _FrozenDatetime:
    """Minimal drop-in for ``datetime`` exposing only ``now`` (the single
    method the push path calls: ``datetime.now(timezone.utc)``)."""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN if tz is None else _FROZEN.astimezone(tz)


def _watermark_of(meta: dict) -> dict:
    """The recorded watermark — FLAT keys of the local ``graph_memory`` block.

    The merged P4-3 contract (``test_meta_shared_local.py``) + the ratified
    ``models.GraphMemoryConfig`` docstring pin the coords as flat keys of
    ``graph_memory``, not a nested sub-block. ``provenance`` is always recorded
    once a push has run, so its presence is the marker that the watermark was
    written.
    """
    gm = meta["graph_memory"]
    assert "provenance" in gm, (
        "push must record the watermark coords (bank_version / commit_id / term "
        "/ provenance) as flat keys of the local graph_memory block (P4-5 / "
        "ADR-0017)"
    )
    return gm


# =============================================================================
# CASE 1 — committed coord present → watermark records the MATCHING coords
# =============================================================================


async def test_push_with_committed_coord_records_matching_watermark(
    storage: FakeStorage,
) -> None:
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    await _seed_committed(storage, bank_version=7, commit_id="c-7-deadbeef", term=3)

    bridge, _factory = _build()
    with _patch_env(storage):
        result = await bridge.push(_SPACE)

    assert result["status"] == "ok"

    meta = await storage.get_json(_META_KEY)
    wm = _watermark_of(meta)
    # Recorded coords == the consumed commit's coords, exactly.
    assert wm["bank_version"] == 7
    assert wm["commit_id"] == "c-7-deadbeef"
    assert wm["term"] == 3
    # Provenance present and pins the available-coords marker (ADR-0017 / the
    # merged test_meta_shared_local.py fixture), NOT the degraded marker.
    assert wm["provenance"] == "mid-consolidation"
    assert "not available" not in str(wm["provenance"]).lower()
    # Recorded against the injected clock, never the real wall clock.
    assert wm["recorded_at"] == _FROZEN_ISO


async def test_push_watermark_does_not_disturb_existing_gm_metrics(
    storage: FakeStorage,
) -> None:
    """Recording the watermark must coexist with the legacy push-metrics write
    (push_count / files_pushed / last_push) — same _meta write, no clobber."""
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    await _seed_committed(storage, bank_version=2, commit_id="c-2", term=1)

    bridge, _factory = _build()
    with _patch_env(storage):
        await bridge.push(_SPACE)

    gm = (await storage.get_json(_META_KEY))["graph_memory"]
    assert gm["push_count"] == 1
    assert gm["files_pushed"] == 2
    assert gm["last_push"] == _FROZEN_ISO
    # The legacy connection fields survive untouched.
    assert gm["url"] == _GM_URL
    assert gm["memory_id"] == "mem-1"
    assert gm["bank_version"] == 2


# =============================================================================
# CASE 2 — NO commit present → coords null / "not available", provenance present
# =============================================================================


async def test_push_with_no_commit_records_null_coords_with_provenance(
    storage: FakeStorage,
) -> None:
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    # Deliberately seed NO bank_version.json and NO commit journal — the live
    # path today (#8 not landed): committed coords are absent.

    bridge, _factory = _build()
    with _patch_env(storage):
        result = await bridge.push(_SPACE)

    assert result["status"] == "ok"
    wm = _watermark_of(await storage.get_json(_META_KEY))

    # Coords are explicitly null — NEVER fabricated to 0 / -1 / "".
    assert wm["bank_version"] is None
    assert wm["commit_id"] is None
    assert wm["term"] is None
    # Provenance is still present and self-describes the degraded read.
    assert "not available" in str(wm["provenance"]).lower()


async def test_push_with_pointer_but_missing_commit_journal_degrades(
    storage: FakeStorage,
) -> None:
    """A dangling pointer (bank_version.json present, commit journal entry
    missing) must ALSO degrade to 'not available' — never half-fabricate."""
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    await storage.put_json(
        layout.bank_version_key(_SPACE),
        {"protocol_version": 1, "bank_version": 9, "commit_id": "c-9"},
    )
    # No commits/00...09.json written.

    bridge, _factory = _build()
    with _patch_env(storage):
        await bridge.push(_SPACE)

    wm = _watermark_of(await storage.get_json(_META_KEY))
    assert wm["bank_version"] is None
    assert wm["commit_id"] is None
    assert wm["term"] is None
    assert "not available" in str(wm["provenance"]).lower()


# =============================================================================
# CASE 3 — watermark ONLY in meta_local_complement, NEVER meta_shared_projection
# =============================================================================


async def test_recorded_watermark_is_local_only_never_shared(
    storage: FakeStorage,
) -> None:
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    await _seed_committed(
        storage, bank_version=11, commit_id="c-11-secret", term=5
    )

    bridge, _factory = _build()
    with _patch_env(storage):
        await bridge.push(_SPACE)

    meta = await storage.get_json(_META_KEY)

    # In the LOCAL complement (the whole graph_memory block is local — the flat
    # watermark coords live directly on it).
    local = meta_local_complement(meta)
    assert local["graph_memory"]["bank_version"] == 11

    # NEVER in the shared projection — neither the block nor any coord leaks.
    shared = meta_shared_projection(meta)
    assert "graph_memory" not in shared
    flat = json.dumps(shared)
    for needle in ("c-11-secret", "bank_version", "11"):
        # 'bank_version'/'11' must not appear via any shared field either.
        assert needle not in flat, needle


# =============================================================================
# CASE 4 — AST / negative-import: no COMMIT-PATH module READS the watermark
# =============================================================================

_SRC = Path(__file__).resolve().parents[1] / "src" / "live_mem"

# The commit-decision modules. None of them may read the derived watermark
# (ADR-0010/0017: watermark is input-only, never read back by the commit path).
_COMMIT_PATH_MODULES = (
    _SRC / "core" / "consolidator.py",
    _SRC / "core" / "write_sink.py",
    _SRC / "core" / "hivemind" / "state.py",
    _SRC / "core" / "hivemind" / "lifecycle.py",
)

# Tokens that, appearing in a commit-path module, would indicate it reads the
# long-derived watermark coords. The watermark lives as FLAT keys of the
# ``graph_memory`` block (``graph_memory["bank_version"]`` / ``["commit_id"]`` /
# ``["provenance"]``), so we match that access shape — distinct from the
# Hivemind per-peer ``Watermark`` model / ``watermark_key`` (replication cursors,
# a DIFFERENT concept the commit path legitimately touches).
_WATERMARK_ACCESS_MARKERS = (
    'graph_memory"]["bank_version"',
    "graph_memory'][\'bank_version'",
    'graph_memory"]["commit_id"',
    "graph_memory'][\'commit_id'",
    'graph_memory"]["provenance"',
    "graph_memory'][\'provenance'",
)


def test_commit_path_modules_do_not_read_long_watermark_text() -> None:
    for mod in _COMMIT_PATH_MODULES:
        if not mod.exists():
            continue
        src = mod.read_text(encoding="utf-8")
        for marker in _WATERMARK_ACCESS_MARKERS:
            assert marker not in src, f"{mod.name} reads the long watermark: {marker!r}"


def test_commit_path_modules_do_not_import_graph_bridge_or_long() -> None:
    """The commit path must not import the long/graph modules at all — so it
    structurally CANNOT reach ``graph_memory.watermark`` through them."""
    forbidden = ("graph_bridge", "long_engine", "engines.long")
    for mod in _COMMIT_PATH_MODULES:
        if not mod.exists():
            continue
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                low = name.lower()
                for bad in forbidden:
                    assert bad not in low, f"{mod.name} imports {name!r} (commit→long edge)"


def test_push_reads_committed_coords_without_importing_commit_decision() -> None:
    """The READ of committed coords must live in graph_bridge and must NOT pull
    in any commit-DECISION module (consolidator / write_sink / hivemind.state /
    hivemind.lifecycle / assert_commit). Reading via storage + layout helpers is
    the allowed downstream-consumption path (ADR-0017)."""
    import live_mem.core.graph_bridge as gb_mod

    src = Path(inspect.getsourcefile(gb_mod)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(src)

    # AIRTIGHT: the downstream long bridge must import NOTHING from the hivemind
    # commit subpackage — not even ``layout`` — because ``hivemind/__init__``
    # eagerly loads the commit-STATE module, which would drag it into the
    # bridge's import graph. Coord paths are hard-coded instead (drift-guarded
    # below). A blanket ``hivemind`` ban over the import set enforces this.
    forbidden = ("consolidator", "write_sink", "hivemind")
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.append("import " + ", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            imports.append(
                f"from {prefix}{node.module or ''} import "
                + ", ".join(a.name for a in node.names)
            )
    blob = "\n".join(imports).lower()
    for bad in forbidden:
        assert bad not in blob, f"graph_bridge imports commit-path module {bad!r}"

    # No assert_commit* authority symbol referenced anywhere (AST, so prose in
    # docstrings naming it as forbidden does not trip this).
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert not node.id.startswith("assert_commit")
        if isinstance(node, ast.Attribute):
            assert not node.attr.startswith("assert_commit")

    # The read sources committed coords from the hard-coded _hivemind journal
    # paths (kept in lockstep with the canonical layout helpers by the
    # drift-guard test below).
    assert "_hivemind/commits/" in src and "_hivemind/bank_version.json" in src, (
        "push should source committed coords from the _hivemind commit-journal paths"
    )


def test_hardcoded_commit_paths_match_layout_helpers() -> None:
    """graph_bridge hard-codes the ``_hivemind`` coord paths (to avoid importing
    the hivemind subpackage); guard them against drift from the canonical
    ``layout`` helpers — which a TEST may freely import."""
    from live_mem.core.hivemind import layout

    assert layout.bank_version_key(_SPACE) == f"{_SPACE}/_hivemind/bank_version.json"
    assert layout.commit_key(_SPACE, 7) == f"{_SPACE}/_hivemind/commits/{7:020d}.json"


# =============================================================================
# CASE 5 — monotone-or-absent: an OLDER bank_version is FLAGGED, no regression
# =============================================================================


async def test_second_push_with_older_bank_version_is_flagged_no_regression(
    storage: FakeStorage,
) -> None:
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)

    bridge, _factory = _build()

    # First push consumes committed bank_version 10.
    await _seed_committed(storage, bank_version=10, commit_id="c-10", term=4)
    with _patch_env(storage):
        await bridge.push(_SPACE)
    wm1 = _watermark_of(await storage.get_json(_META_KEY))
    assert wm1["bank_version"] == 10
    assert wm1.get("flagged") in (False, None)  # forward progress is not flagged

    # Second push sees an OLDER committed pointer (bank_version 6) — e.g. a
    # rollback / split-brain artifact. Monotone-or-absent: the recorded forward
    # progress (10) must NOT regress, and the regression must be FLAGGED.
    await _seed_committed(storage, bank_version=6, commit_id="c-6", term=2)
    with _patch_env(storage):
        await bridge.push(_SPACE)

    wm2 = _watermark_of(await storage.get_json(_META_KEY))
    # Recorded bank_version did NOT regress below the high-water mark.
    assert wm2["bank_version"] == 10, "older push must not regress recorded progress"
    assert wm2["commit_id"] == "c-10"
    # The attempted regression is surfaced, not silently dropped.
    assert wm2["flagged"] is True


async def test_monotone_forward_push_advances_and_clears_flag(
    storage: FakeStorage,
) -> None:
    """A later push with a NEWER bank_version advances the watermark and is not
    flagged — the rule is monotone-or-absent, not frozen-after-first."""
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    bridge, _factory = _build()

    await _seed_committed(storage, bank_version=4, commit_id="c-4", term=1)
    with _patch_env(storage):
        await bridge.push(_SPACE)

    await _seed_committed(storage, bank_version=8, commit_id="c-8", term=2)
    with _patch_env(storage):
        await bridge.push(_SPACE)

    wm = _watermark_of(await storage.get_json(_META_KEY))
    assert wm["bank_version"] == 8
    assert wm["commit_id"] == "c-8"
    assert wm["term"] == 2
    assert wm.get("flagged") in (False, None)


# =============================================================================
# CASE 6 — push response stays backward-compatible
# =============================================================================


async def test_push_response_preserves_pushed_int_and_metrics(
    storage: FakeStorage,
) -> None:
    # P4-8: seed the prior bank-mirror ledger so the recorded-mirror orphan
    # (stale.md) is eligible for cleanup; the durable bank files re-ingest.
    meta = _meta_connected()
    meta["graph_memory"]["bank_mirror"] = [
        "systemPatterns.md",
        "techContext.md",
        "stale.md",
    ]
    await storage.put_json(_META_KEY, meta)
    await _seed_bank(storage)
    await _seed_committed(storage, bank_version=3, commit_id="c-3", term=1)

    # One pre-existing doc (re-ingested) + one recorded-mirror orphan to clean.
    # P7-8: listed docs carry their GM ``id`` — deletes are document_id-keyed.
    responses = {
        "document_list": {
            "status": "ok",
            "documents": [
                {"id": "uuid-patterns", "filename": "systemPatterns.md"},
                {"id": "uuid-stale", "filename": "stale.md"},
            ],
        }
    }
    bridge, _factory = _build(responses=responses)
    with _patch_env(storage):
        result = await bridge.push(_SPACE)

    # Backward-compatible response shape (watermark recording must not reshape
    # the dict the tool layer/clients already depend on).
    assert result["status"] == "ok"
    assert isinstance(result["pushed"], int) and result["pushed"] == 2
    assert "cleaned_orphans" in result and result["cleaned_orphans"] == 1
    assert "duration_seconds" in result
    assert isinstance(result["duration_seconds"], (int, float))
    assert result["errors"] == 0
    # Only systemPatterns.md pre-existed -> exactly one re-ingest delete.
    assert result["deleted_before_reingest"] == 1


async def test_watermark_recording_does_not_leak_into_push_response(
    storage: FakeStorage,
) -> None:
    """The watermark is a _meta side-effect, not part of the push RESPONSE
    contract — clients parsing the response must see no new required key."""
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_bank(storage)
    await _seed_committed(storage, bank_version=1, commit_id="c-1", term=0)

    bridge, _factory = _build()
    with _patch_env(storage):
        result = await bridge.push(_SPACE)

    # The legacy keys are exactly the public contract; watermark lives in _meta.
    assert "watermark" not in result


async def test_empty_bank_skips_push_and_records_no_watermark(
    storage: FakeStorage,
) -> None:
    """The empty-bank early-return (pushed:0) is a legacy branch that never
    contacts GM — it must not synthesize a watermark either."""
    await storage.put_json(_META_KEY, _meta_connected())
    await _seed_committed(storage, bank_version=5, commit_id="c-5", term=2)
    # No bank files seeded.

    bridge, factory = _build()
    with _patch_env(storage):
        result = await bridge.push(_SPACE)

    assert result["pushed"] == 0
    assert factory.instances == []  # no client built — no GM contact
    gm = (await storage.get_json(_META_KEY))["graph_memory"]
    # The early-return records nothing — no watermark coords were written.
    assert "bank_version" not in gm
    assert "provenance" not in gm
