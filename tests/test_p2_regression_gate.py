# -*- coding: utf-8 -*-
"""
P2-6 (issue #38) — Wave-3 VALIDATION GATE for EPIC P2 (Unified Space Model).

This module is the consolidated regression gate. It does NOT re-author the P2
detection / allowlist / projection / corruption / backup-guard / anti-drift
matrices — those already ship and are exercised by the sibling modules named in
``COVERAGE_MAP`` below (see ``test_coverage_manifest_names_every_p2_contract``).

The ONE genuinely-missing, load-bearing artifact P2-6 adds is the non-Hivemind
byte-for-byte STORED-OBJECT passthrough proof:

    P2-4 (tests/test_space_hive_status.py) proved the RESPONSE dict complement
    of get_summary/export_space/get_info is golden-equal for a non-Hivemind
    space. P2-5 (tests/test_backup_restore_hivemind_guard.py) proved
    ``storage.objects == before`` only after ``restore()`` on orphan / corrupted
    / local_only targets. NEITHER ran the three SpaceService read surfaces +
    ``hive_status_label`` together against ONE space and asserted the STORED S3
    bytes — in particular ``{space}/_meta.json`` — are byte-identical afterward.

This materializes the EPIC P2-1 acceptance criterion: "No diff to _meta.json
bytes for an existing non-Hivemind space (proven by a round-trip test, see
P2-6)" — finally as a STORED-object proof, not a response-shape golden.

Deterministic, OFFLINE, fake-backed ONLY. Reuses ``tests/test_hivemind_state``'s
``FakeStorage``; a local ``_PassthroughStorage`` subclass adds ONLY the
``StorageService`` methods these paths call but the shared fake lacks
(``list_and_get`` / ``copy_object``). The shared class is never mutated (other
suites pin its surface). No real S3 / network / LLM.
"""

from __future__ import annotations

import importlib
import json
from copy import deepcopy

import pytest

from live_mem.core import space as space_module
from live_mem.core.backup import BackupService
from live_mem.core.hivemind import hive_status_label
from tests.test_hivemind_state import FakeStorage as _BaseFakeStorage

SPACE = "p2-6-nonhive-space"
GM_TOKEN = "SECRET-graph-token-p26-do-not-leak-0123456789"
TS = "2026-06-18T09-00-00"
BACKUP_ID = f"{SPACE}/{TS}"
BACKUP_PREFIX = f"_backups/{SPACE}/{TS}/"


# ─────────────────────────────────────────────────────────────────────────────
# FakeStorage extension — faithful to StorageService semantics.
#
# The shared FakeStorage (tests/test_hivemind_state.py) covers the
# HivemindStateStore contract but NOT list_and_get / copy_object, which
# get_summary / export_space (list_and_get) and BackupService.restore's
# inherited copy path (copy_object) call. We subclass HERE — never mutate the
# shared class — mirroring the already-green extensions in
# tests/test_space_hive_status.py and tests/test_backup_restore_hivemind_guard.py
# verbatim:
#   - list_and_get returns lower-case-keyed dicts {'key','content','size',
#     'last_modified'} (storage.py contract), exclude_keep default True;
#   - copy_object increments put_calls so the no-mutation assertion is
#     meaningful IF it were ever reached. The headline proof deliberately takes
#     the REFUSED restore branch (local_only -> inherited "_meta.json existe
#     déjà"), so copy_object is NOT reached; it exists only to make the
#     byte-identity claim load-bearing rather than vacuous.
# ─────────────────────────────────────────────────────────────────────────────


class _PassthroughStorage(_BaseFakeStorage):
    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        objects = await self.list_objects(prefix)
        results: list[dict] = []
        for obj in objects:
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = await self.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": str(obj.get("LastModified", "")),
                    }
                )
        return results

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        # Only reached on the proceed branch (not_a_space / unsafe_recovery),
        # which the headline test never takes. Increments put_calls so a stray
        # copy would break the read-only counter assertion.
        self.put_calls += 1
        self.objects[dest_key] = self.objects[source_key]


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ─────────────────────────────────────────────────────────────────────────────


def _full_meta_doc() -> dict:
    """A full _meta.json document INCLUDING the local-only graph_memory block
    (with a token) — so the proof also implicitly covers that no read path
    rewrites / projects the stored _meta.json (the secret stays on the stored
    object exactly as seeded; service responses masking it is P2-4's concern)."""
    return {
        "space_id": SPACE,
        "description": "desc-p2-6",
        "owner": "owner-p2-6",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_consolidation": None,
        "consolidation_count": 0,
        "total_notes_processed": 0,
        "version": 1,
        "graph_memory": {
            "url": "http://gm.example/mcp",
            "token": GM_TOKEN,
            "memory_id": "mem-p26",
            "ontology": "general",
        },
    }


async def _seed_non_hivemind_space(storage: _PassthroughStorage) -> None:
    """Seed a NON-Hivemind space: _meta.json + _rules.md + live/.keep +
    bank/.keep + bank/activeContext.md. Crucially NO {space}/_hivemind/ — so
    hive_status_label() classifies it as 'local_only'."""
    # Write _meta.json with the SAME serialization the service/store uses
    # (json.dumps indent=2 ensure_ascii=False) so nothing normalizes it later.
    await storage.put(
        f"{SPACE}/_meta.json",
        json.dumps(_full_meta_doc(), indent=2, ensure_ascii=False),
    )
    await storage.put(f"{SPACE}/_rules.md", "# Rules P2-6\n")
    await storage.put(f"{SPACE}/live/.keep", "")
    await storage.put(f"{SPACE}/bank/.keep", "")
    await storage.put(f"{SPACE}/bank/activeContext.md", "ctx body p2-6")


def _seed_backup(storage: _PassthroughStorage) -> None:
    """Pose un backup valide sous _backups/{SPACE}/{TS}/ (written directly into
    objects, like test_backup_restore_hivemind_guard.py, so it does NOT bump
    put_calls but IS part of the before-snapshot). Needed so restore() reaches
    the hive guard + the inherited refusal instead of short-circuiting on a
    missing backup ('not_found')."""
    storage.objects[f"{BACKUP_PREFIX}_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    storage.objects[f"{BACKUP_PREFIX}_rules.md"] = "# backup rules"
    storage.objects[f"{BACKUP_PREFIX}live/note-1.md"] = "backup note"


def _patch_single_storage(monkeypatch, storage: _PassthroughStorage) -> None:
    """Both space.py and backup.py bind get_storage module-locally
    (``from .storage import get_storage``). Patch BOTH so the two services share
    the ONE in-memory storage instance the byte-identity proof depends on."""
    monkeypatch.setattr(space_module, "get_storage", lambda: storage)
    monkeypatch.setattr("live_mem.core.backup.get_storage", lambda: storage)


@pytest.fixture
def stub_consolidation_queue(monkeypatch):
    """get_info lazily does ``from .consolidation_queue import
    get_consolidation_queue`` then awaits ``.get_space_summary(space_id)``. Patch
    the attribute on the consolidation_queue MODULE (resolved at call time) to an
    in-memory stub that performs NO storage write (same pattern as
    tests/test_space_hive_status.py::stub_consolidation_queue)."""
    from live_mem.core import consolidation_queue as cq_module

    class _StubQueue:
        async def get_space_summary(self, space_id: str) -> dict:
            return {"pending": 0, "spaces": []}

    monkeypatch.setattr(cq_module, "get_consolidation_queue", lambda: _StubQueue())
    return _StubQueue


# ═════════════════════════════════════════════════════════════════════════════
# HEADLINE PROOF — non-Hivemind byte-for-byte STORED-OBJECT passthrough
# ═════════════════════════════════════════════════════════════════════════════


async def test_non_hivemind_stored_bytes_unchanged_across_all_p2_read_paths(
    monkeypatch, stub_consolidation_queue
):
    """Run EVERY P2 read/detection path against ONE non-Hivemind space and
    prove the STORED S3 bytes — and specifically {space}/_meta.json — are
    byte-identical afterward. Reads + label computation + a refused (inapplicable)
    restore never mutate stored state.

    Non-vacuity is enforced by load-bearing side-evidence that each path really
    ran on a real non-Hivemind space:
      - hive_status_label() == 'local_only' (detection ran, _meta.json present,
        no _hivemind/);
      - each read surface returned status 'ok';
      - the restore took the inherited REFUSED branch (status 'error',
        message 'existe déjà'), NOT a copy.
    """
    storage = _PassthroughStorage()
    await _seed_non_hivemind_space(storage)
    _seed_backup(storage)
    _patch_single_storage(monkeypatch, storage)

    # Byte snapshots BEFORE any read. deepcopy (matching FakeStorage.snapshot())
    # to avoid aliasing the live dict; capture the _meta.json value separately
    # for the explicit per-object pin.
    before = deepcopy(storage.objects)
    meta_key = f"{SPACE}/_meta.json"
    meta_before = storage.objects[meta_key]
    puts_before, deletes_before = storage.put_calls, storage.delete_calls

    # 1) Detection layer.
    label = await hive_status_label(storage, SPACE)
    assert label == "local_only"  # non-vacuous: the path ran on a real space

    # 2) SpaceService read surfaces (all three).
    summary = await space_module.SpaceService().get_summary(SPACE)
    assert summary["status"] == "ok"
    assert summary["hive_status_label"] == "local_only"

    info = await space_module.SpaceService().get_info(SPACE)
    assert info["status"] == "ok"
    assert info["hive_status_label"] == "local_only"

    export = await space_module.SpaceService().export_space(SPACE)
    assert export["status"] == "ok"
    assert export["hive_status_label"] == "local_only"

    # 3) BackupService.restore classification path. The read-only hive guard
    # classifies the target 'local_only' (guard inapplicable -> falls through),
    # then the inherited "{space}/_meta.json already exists" check refuses. This
    # reuses the already-tested restore classification path
    # (test_backup_restore_hivemind_guard.py::
    #  test_restore_over_local_only_space_still_refused_existing_behavior)
    # INSIDE a new whole-store byte-identity sweep — proving an inapplicable
    # refused restore leaves a non-Hivemind target byte-identical, which no
    # existing test asserts together with the read trio.
    restore = await BackupService().restore(BACKUP_ID)  # unsafe_recovery default False
    assert restore["status"] == "error"
    assert "already exists" in restore["message"]  # inherited refusal, not a copy
    assert "files_restored" not in restore

    # ── THE PROOF ────────────────────────────────────────────────────────────
    # Whole-store byte identity: no read / label / refused-restore mutated any
    # stored object.
    assert storage.objects == before
    # Explicit per-object pin of the headline AC: the stored _meta.json BYTES
    # are unchanged (graph_memory.token still present on the stored object — the
    # read paths neither projected nor rewrote it; only service RESPONSES mask
    # it, which is P2-4's separately-tested concern).
    assert storage.objects[meta_key] == meta_before
    assert GM_TOKEN in storage.objects[meta_key]
    # No PUT / DELETE happened across the entire sequence: an integrated,
    # stronger form of the per-method read-only checks P2-4/P2-5 made in
    # isolation. (copy_object would bump put_calls — it was never reached.)
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


# ═════════════════════════════════════════════════════════════════════════════
# EMPTY / NOT_A_SPACE — reads never lazily materialize stored state
# ═════════════════════════════════════════════════════════════════════════════


async def test_empty_storage_not_a_space_reads_do_not_materialize_state(
    monkeypatch,
):
    """On a completely empty store, label == 'not_a_space' and the three
    SpaceService read surfaces early-return ('not_found') WITHOUT creating any
    object. Proves reads never lazily write _meta.json / _hivemind/.

    Net-new vs test_hive_status_label.py::test_label_not_a_space_empty_storage,
    which asserts the not_a_space VALUE but never that storage stays empty after
    the SpaceService read surfaces run.

    NB: get_summary/get_info early-return on get_json(_meta.json) is None and
    export_space on exists() is False — all BEFORE the lazy consolidation_queue
    import — so get_info needs no queue stub on the empty path.
    """
    storage = _PassthroughStorage()
    _patch_single_storage(monkeypatch, storage)

    assert storage.objects == {}

    label = await hive_status_label(storage, SPACE)
    assert label == "not_a_space"
    assert storage.objects == {}  # label computation wrote nothing

    summary = await space_module.SpaceService().get_summary(SPACE)
    assert summary["status"] == "not_found"
    assert "hive_status_label" not in summary  # field only on success path

    info = await space_module.SpaceService().get_info(SPACE)
    assert info["status"] == "not_found"

    export = await space_module.SpaceService().export_space(SPACE)
    assert export["status"] == "not_found"

    # The decisive claim: NO read surface lazily created an object.
    assert storage.objects == {}
    assert storage.put_calls == 0
    assert storage.delete_calls == 0


# ═════════════════════════════════════════════════════════════════════════════
# COVERAGE MANIFEST — names where each P2 contract is already tested, so the
# gate is discoverable. Documentation-as-test: it pins that the siblings exist
# (importlib + getattr), making a renamed/removed sibling test RED here. It does
# NOT re-run or re-implement any sibling matrix.
# ═════════════════════════════════════════════════════════════════════════════

COVERAGE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    # contract -> (module, representative test function names that prove it)
    "detection_matrix_and_fail_closed": (
        "tests.test_hive_status_label",
        (
            "test_label_not_a_space_empty_storage",
            "test_label_local_only_meta_no_hivemind",
            "test_label_hivemind_healthy",
            "test_label_unsafe_structurally_incomplete",
            "test_label_resync_required_marker",
            "test_label_orphaned_marker_no_meta_is_unsafe",
            "test_corruption_node_members_nodestatus_raises_not_local",
            "test_resolver_label_is_read_only_no_writes",
        ),
    ),
    "meta_allowlist_and_projection_roundtrip": (
        "tests.test_meta_allowlist",
        (
            "test_projection_deny_by_default_unknown_field_excluded",
            "test_meta_shared_projection_local_complement_round_trip",
            "test_token_never_in_shared_projection",
        ),
    ),
    "space_read_surfaces_label_and_response_golden": (
        "tests.test_space_hive_status",
        (
            "test_non_hivemind_summary_local_only_and_golden",
            "test_non_hivemind_export_local_only_and_golden",
            "test_non_hivemind_info_local_only_and_golden",
            "test_corrupted_summary_is_unsafe_not_local_no_raise",
            "test_update_persists_full_meta_document_not_projected",
        ),
    ),
    "backup_restore_hivemind_guard": (
        "tests.test_backup_restore_hivemind_guard",
        (
            "test_restore_over_orphan_hive_without_flag_is_refused_and_unmutated",
            "test_restore_over_local_only_space_still_refused_existing_behavior",
            "test_restore_into_not_a_space_target_proceeds_unchanged",
            "test_restore_over_corrupted_target_is_refused_fail_closed_regardless_of_flag",
        ),
    ),
    "concern_location_anti_drift": (
        "tests.test_unified_space",
        (
            "test_concern_location_mapping_matches_live_keys_no_drift",
            "test_node_local_hive_paths_not_marked_shared",
        ),
    ),
}


def test_coverage_manifest_names_every_p2_contract():
    """Every contract in COVERAGE_MAP points to a real sibling module whose
    named test functions actually exist. If a sibling test is renamed or
    removed, this gate goes RED — keeping the manifest honest instead of letting
    a free-form docstring silently rot.

    Non-vacuous: the loop body asserts on real importlib/getattr results, and we
    pin that all five EPIC-P2 contracts are represented so the manifest can't
    silently shrink.
    """
    expected_contracts = {
        "detection_matrix_and_fail_closed",
        "meta_allowlist_and_projection_roundtrip",
        "space_read_surfaces_label_and_response_golden",
        "backup_restore_hivemind_guard",
        "concern_location_anti_drift",
    }
    assert set(COVERAGE_MAP) == expected_contracts

    for contract, (module_name, func_names) in COVERAGE_MAP.items():
        module = importlib.import_module(module_name)
        assert func_names, f"{contract}: no test functions named"
        for fn in func_names:
            obj = getattr(module, fn, None)
            assert callable(obj), f"{contract}: {module_name}::{fn} missing/not callable"
