# -*- coding: utf-8 -*-
"""
P3-8 (issue #57) — the consolidated byte-for-byte regression GATE for the P3
engine-boundaries stack (the P3 analogue of ``tests/test_p2_regression_gate.py``).

P3-8 is TESTS-ONLY and ADDITIVE: this file adds NO ``src/`` changes. It supplies
the three genuinely-missing P3-8 artifacts and composes the EPIC acceptance
matrix by REFERENCING (not re-implementing) the byte goldens that sibling P3
suites already pin.

THREE sections, each mapping to a P3-8 requirement:

1. CONSOLIDATION STORED-OBJECT GOLDEN (the net-new spine). No existing P3 test
   runs a real ``ConsolidatorService.consolidate`` end-to-end and asserts
   ``storage.objects`` byte-equality through the engine/registry stack —
   ``tests/test_writesink_routing.py`` only gates the consolidate ENQUEUE, and
   ``tests/test_engine_mid.py`` only proves identity-passthrough over a FAKE
   consolidator. Here we drive the REAL consolidator twice over two identically
   seeded fakes — once on the LEGACY direct path, once through
   ``EngineRegistry.mid_engine(space).consolidate(...)`` — and assert the full
   stored footprint is byte-identical. Wave-1 contract: the registry resolves a
   non-Hivemind space to ``DirectLocalWriteSink`` (HELD-not-consumed), so the
   consolidator's own ``get_storage`` writes ARE the byte-for-byte path the
   golden proves stays identical post-routing.

2. DEFAULT ROUTING for a no-``_hivemind/`` space (registry-level complement to
   ``test_engine_registry::test_resolve_sink_non_hivemind_returns_direct_local``):
   an EMPTY space with no ``_hivemind/`` prefix at all resolves to
   ``DirectLocalWriteSink`` by DEFAULT, and the resolved default sink writes
   byte-identically to a parallel direct ``storage.put`` / ``put_json``.

3. COVERAGE MANIFEST (modeled on
   ``test_p2_regression_gate.py::test_coverage_manifest_names_every_p2_contract``):
   a ``COVERAGE_MAP`` naming all 9 P3 sibling guard modules with real anchor
   functions, so renaming/deleting any P3 guard suite (or a named anchor) goes
   RED — the executable DoD anchor for P3.

ALREADY PROVEN — referenced, NOT re-implemented here (so this file covers the
whole EPIC {live note, consolidation, bank write, bank delete} matrix by
composition; only consolidation was genuinely unpinned as a stored-object
golden):
- live note byte-identity:
  ``tests/test_writesink_routing.py::test_live_note_non_hivemind_byte_identical``.
- bank write byte-identity:
  ``tests/test_writesink_routing.py::test_bank_write_non_hivemind_byte_identical``.
- bank delete via ``sink.delete_many``:
  ``tests/test_writesink_routing.py::test_bank_delete_non_hivemind_routes_delete_many``.
- ``DirectLocalWriteSink`` put/put_json/delete/delete_many byte parity:
  ``tests/test_write_sink.py`` (``test_direct_local_*``).

Deterministic and offline: the consolidator is built with ``get_settings``
patched and a resolved shared-boundary runtime installed (the
``tests/test_proxy.py::_make_consolidator`` idiom, so no transport is
constructed); the LLM is stubbed at
``ConsolidatorService._call_llm``; the clock is frozen at the
``live_mem.core.consolidator.datetime`` seam; and the storage seam
``live_mem.core.consolidator.get_storage`` is pointed at an in-memory fake. No
real S3 / boto3 / network / LLM.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from live_mem.config import Settings
from live_mem.core.consolidator import ConsolidatorService
from live_mem.core.engines import EngineRegistry
from live_mem.core.live import LiveService
from live_mem.core.write_sink import DirectLocalWriteSink
from tests.test_write_sink import WriteSinkFakeStorage


# =============================================================================
# Determinism seams — fakes / clock / settings / LLM stub
# =============================================================================


class ConsolidatorFakeStorage(WriteSinkFakeStorage):
    """``WriteSinkFakeStorage`` (put/put_json/get/get_json/delete/delete_many/
    list_objects/exists) + ``list_and_get`` — the read the consolidator needs.

    ``ConsolidatorService.consolidate`` reads notes + bank via
    ``storage.list_and_get`` (``_collect_inputs`` at consolidator.py:898/922,
    ``_compact_bank_if_needed`` re-read at 570). The shared
    ``WriteSinkFakeStorage`` adds only ``delete_many`` (required by the
    consolidator's notes-cleanup ``delete_many`` at 1491) and lacks
    ``list_and_get``; three sibling suites already extend the fake the same way
    (``test_engine_short.LiveFakeStorage``, ``test_p2_regression_gate``,
    ``test_space_hive_status``). This mirrors ``StorageService.list_and_get``:
    returns ``[{"key", "content", "size", "last_modified"}]`` (LOWER-cased
    ``key``/``content`` — the consolidator reads ``bf["key"]`` / ``bf["content"]``
    at 1315/1321) and EXCLUDES ``.keep`` sentinels (matching the real default).
    """

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects[key]
            out.append(
                {
                    "key": key,
                    "content": content,
                    "size": len(content),
                    "last_modified": "",
                }
            )
        return out


_FROZEN = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


class _Dt:
    """Frozen replacement for ``live_mem.core.consolidator.datetime``.

    Only ``now`` is used by the consolidator write path
    (``datetime.now(timezone.utc).isoformat()`` for ``_synthesis.md`` at 1466 and
    the ``_meta.json`` epilogue at 802). Freezing it makes both runs' timestamped
    bytes identical so the dict-equality holds.
    """

    @classmethod
    def now(cls, tz=None):
        return _FROZEN


# The FIXED LLM result both runs receive. A SINGLE ``create`` of a NEW bank file
# whose content has NO duplicate markdown headings, so ``_deduplicate_content`` ->
# ``_detect_duplicates`` returns empty and the run NEVER reaches the SECOND LLM
# seam ``_merge_sections_via_llm`` (which would build a real client call). The
# ``create`` branch (consolidator.py:1357-1364) does not even call
# ``_deduplicate_content`` — strictly the safest, single-LLM-call path.
_LLM_RESULT: dict = {
    "status": "ok",
    "data": {
        "file_edits": [
            {
                "filename": "activeContext.md",
                "action": "create",
                "content": (
                    "# Active Context\n"
                    "\n"
                    "## Current Focus\n"
                    "\n"
                    "- seeded fact one\n"
                    "- seeded fact two\n"
                    "\n"
                    "## Open Questions\n"
                    "\n"
                    "- none\n"
                ),
            }
        ],
        "synthesis": "Consolidated two seeded notes into activeContext.md.",
    },
    "usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
}


_SETTINGS_BASE = {
    "mcp_server_name": "Test",
    "mcp_server_host": "0.0.0.0",
    "mcp_server_port": 8002,
    "mcp_server_debug": False,
    "admin_bootstrap_key": "change_me_in_production",
    "s3_endpoint_url": "",
    "s3_access_key_id": "",
    "s3_secret_access_key": "",
    "s3_bucket_name": "live-mem",
    "s3_region_name": "fr1",
    "llmaas_api_url": "https://api.example.com/v1",
    "llmaas_api_key": "sk-test",
    "llmaas_model": "test-model",
    "llmaas_context_window": 131072,
    "llmaas_max_tokens": 16384,
    "llmaas_temperature": 0.3,
    "default_rules_file": "",
    "consolidation_timeout": 600,
    "consolidation_max_notes": 500,
    "consolidation_batch_size": 5,
    "consolidation_cooldown_seconds": 60,
    "consolidation_validation_enabled": False,
    "compact_threshold": 0.6,
    "bank_file_max_size": 15360,
    "response_max_bytes": 512 * 1024,
    "proxy_url": None,
}


def _make_settings(**overrides) -> Settings:
    data = dict(_SETTINGS_BASE)
    data.update(overrides)
    return Settings.model_validate(data)


def _make_stubbed_consolidator() -> ConsolidatorService:
    """Build the REAL ``ConsolidatorService`` with NO real LLM/httpx client.

    Idiom from ``tests/test_proxy.py::_make_consolidator``: patch
    ``consolidator.get_settings`` and install a resolved shared-boundary
    runtime (P13-1C) so ``__init__`` snapshots a chat profile without building
    any transport, then stub the instance method ``_call_llm`` (``AsyncMock``)
    to the FIXED ``_LLM_RESULT``. The SAME instance is shared by both runs so
    any residual nondeterminism is shared and cancels in the equality.
    """
    from tests.fakes.inference_fakes import core_inference_runtime

    settings = _make_settings()
    with (
        patch("live_mem.core.consolidator.get_settings", return_value=settings),
        core_inference_runtime(),
    ):
        svc = ConsolidatorService()
    svc._call_llm = AsyncMock(return_value=_LLM_RESULT)
    return svc


_SPACE = "space-a"

# Deterministic literal note keys (identical in both storages). Filenames follow
# the ``{ts}_{agent}_{cat}_{uuid}.md`` convention parsed by ``_build_prompt``;
# with ``agent=''`` ``_collect_inputs`` consolidates ALL notes (no agent filter),
# so all three are consumed and deleted last via ``delete_many``.
_NOTE_KEYS = (
    f"{_SPACE}/live/20260618T120000_agentx_observation_aaaaaaaa.md",
    f"{_SPACE}/live/20260618T120100_agentx_decision_bbbbbbbb.md",
    f"{_SPACE}/live/20260618T120200_agentx_observation_cccccccc.md",
)


async def _seed(storage: ConsolidatorFakeStorage) -> None:
    """Seed ONE space with ``_meta.json`` + ``_rules.md`` + 3 live notes.

    Kept tiny so the bank stays well under the auto-compact threshold
    (~39KB => ``_compact_bank_if_needed`` short-circuits, consolidator.py:1762)
    and the 3 notes are a SINGLE batch (batch_size=5 => one ``_call_llm`` => one
    ``_write_results(skip_meta=True)`` => one epilogue ``_meta.json`` put). At
    least one consolidatable note is required: with zero notes ``consolidate``
    returns early ("No new notes") and never writes the epilogue meta.
    """
    await storage.put_json(f"{_SPACE}/_meta.json", {"consolidation_count": 0})
    await storage.put(f"{_SPACE}/_rules.md", "# Rules\n\nBe concise.\n")
    await storage.put(_NOTE_KEYS[0], "Observed: the build is green.\n")
    await storage.put(_NOTE_KEYS[1], "Decided: ship on Friday.\n")
    await storage.put(_NOTE_KEYS[2], "Observed: latency is nominal.\n")


async def _run_legacy(consolidator: ConsolidatorService) -> ConsolidatorFakeStorage:
    """LEGACY reference run: patch ``consolidator.get_storage`` -> a fresh fake,
    call ``ConsolidatorService.consolidate`` directly (no engine/registry)."""
    storage = ConsolidatorFakeStorage()
    await _seed(storage)
    with (
        patch("live_mem.core.consolidator.get_storage", return_value=storage),
        patch("live_mem.core.consolidator.datetime", _Dt),
    ):
        res = await consolidator.consolidate(_SPACE, enforce_cooldown=False)
    assert res.get("status") == "ok", f"legacy consolidate failed: {res}"
    assert res.get("notes_processed") == len(_NOTE_KEYS), res
    return storage


async def _run_engine(consolidator: ConsolidatorService) -> ConsolidatorFakeStorage:
    """ENGINE run: build an ``EngineRegistry`` over a fresh identically-seeded
    fake, resolve ``mid_engine(space)`` (non-Hivemind => DirectLocalWriteSink,
    HELD-not-consumed), then ``engine.consolidate(...)``. The MidEngine delegates
    verbatim and the consolidator still writes via the patched
    ``consolidator.get_storage`` -> the SAME engine fake."""
    storage = ConsolidatorFakeStorage()
    await _seed(storage)
    reg = EngineRegistry(
        storage=storage,
        live=LiveService(),
        consolidator=consolidator,
        queue=object(),
        bridge=object(),
    )
    with (
        patch("live_mem.core.consolidator.get_storage", return_value=storage),
        patch("live_mem.core.consolidator.datetime", _Dt),
    ):
        engine = await reg.mid_engine(_SPACE)
        # The resolved sink is DirectLocalWriteSink (non-Hivemind); held, inert.
        assert isinstance(engine.write_sink, DirectLocalWriteSink)
        res = await engine.consolidate(_SPACE, enforce_cooldown=False)
    assert res.get("status") == "ok", f"engine consolidate failed: {res}"
    assert res.get("notes_processed") == len(_NOTE_KEYS), res
    return storage


# =============================================================================
# SECTION 1 — the CONSOLIDATION stored-object byte-for-byte golden (net-new spine)
# =============================================================================


async def test_consolidation_non_hivemind_byte_identical_stored_objects() -> None:
    """THE net-new golden: a real consolidation run produces a BYTE-IDENTICAL
    stored footprint whether driven directly (legacy) or through
    ``EngineRegistry.mid_engine(space).consolidate`` (the P3 routing stack).

    Same stubbed ``_call_llm`` + frozen ``consolidator.datetime`` + patched
    ``consolidator.get_storage`` per run + ``enforce_cooldown=False``. The single
    ``engine_storage.objects == legacy_storage.objects`` equality pins the WHOLE
    footprint at once: ``bank/*`` create PUT (1361), ``_synthesis.md`` (1477),
    ``_meta.json`` epilogue (809) AND the consumed ``live/*`` keys DELETED last
    (delete_many 1491). This is the WRITE_SINK_MUTATION_CALL_SITES consolidator
    branch made executable as a stored-object golden — what
    ``test_writesink_routing`` (ENQUEUE gate) and ``test_engine_mid`` (fake
    consolidator) do NOT prove.
    """
    consolidator = _make_stubbed_consolidator()
    legacy_storage = await _run_legacy(consolidator)
    engine_storage = await _run_engine(consolidator)

    # The decisive claim — entire stored footprint byte-identical.
    assert engine_storage.objects == legacy_storage.objects

    # Sanity that the run actually wrote a non-trivial footprint (not two empty
    # dicts trivially equal): bank file created + synthesis + meta present.
    assert f"{_SPACE}/bank/activeContext.md" in legacy_storage.objects
    assert f"{_SPACE}/_synthesis.md" in legacy_storage.objects
    assert f"{_SPACE}/_meta.json" in legacy_storage.objects


async def test_consolidation_writes_synthesis_and_meta_bytes_match() -> None:
    """Readable sub-assert: ``_synthesis.md`` and ``_meta.json`` bytes are present
    and identical across the two runs (frozen-clock seam at consolidator.py:1466
    / epilogue 802). Gives a targeted failure message if the whole-dict equality
    above ever breaks on a timestamped artifact."""
    consolidator = _make_stubbed_consolidator()
    legacy_storage = await _run_legacy(consolidator)
    engine_storage = await _run_engine(consolidator)

    syn = f"{_SPACE}/_synthesis.md"
    meta = f"{_SPACE}/_meta.json"
    assert legacy_storage.objects[syn] == engine_storage.objects[syn]
    assert legacy_storage.objects[meta] == engine_storage.objects[meta]
    # Frozen timestamp landed in the synthesis front-matter (deterministic bytes).
    assert _FROZEN.isoformat() in legacy_storage.objects[syn]
    # Epilogue meta bumped the consolidation_count from the seed (0 -> 1).
    assert '"consolidation_count": 1' in legacy_storage.objects[meta]


async def test_consolidation_creates_bank_file_from_llm_create_edit() -> None:
    """The LLM ``create`` op (consolidator.py:1361) writes the bank file with the
    EXACT stubbed content in BOTH runs — the create branch byte-identity."""
    consolidator = _make_stubbed_consolidator()
    legacy_storage = await _run_legacy(consolidator)
    engine_storage = await _run_engine(consolidator)

    bank_key = f"{_SPACE}/bank/activeContext.md"
    expected = _LLM_RESULT["data"]["file_edits"][0]["content"]
    assert legacy_storage.objects[bank_key] == expected
    assert engine_storage.objects[bank_key] == expected


async def test_consolidation_deletes_consumed_live_notes_last() -> None:
    """Consumed ``live/*`` keys are absent after the run in BOTH storages
    (delete_many at consolidator.py:1491; atomicity: bank written BEFORE notes
    removed). Proves the delete footprint is identical post-routing."""
    consolidator = _make_stubbed_consolidator()
    legacy_storage = await _run_legacy(consolidator)
    engine_storage = await _run_engine(consolidator)

    for key in _NOTE_KEYS:
        assert key not in legacy_storage.objects, f"legacy left note: {key}"
        assert key not in engine_storage.objects, f"engine left note: {key}"
    # No stray live/* keys survive in either run.
    assert not any(k.startswith(f"{_SPACE}/live/") for k in legacy_storage.objects)
    assert not any(k.startswith(f"{_SPACE}/live/") for k in engine_storage.objects)


# =============================================================================
# SECTION 2 — DEFAULT routing for a no-_hivemind/ space (registry-level)
# =============================================================================


def _empty_registry() -> tuple[EngineRegistry, ConsolidatorFakeStorage]:
    storage = ConsolidatorFakeStorage()  # EMPTY: no _hivemind/ prefix at all.
    reg = EngineRegistry(
        storage=storage,
        live=LiveService(),
        consolidator=object(),
        queue=object(),
        bridge=object(),
    )
    return reg, storage


async def test_no_hivemind_space_resolves_direct_local_by_default() -> None:
    """DEFAULT routing: an EMPTY space with NO ``_hivemind/`` prefix whatsoever
    resolves to ``DirectLocalWriteSink`` (the non-Hivemind default). Framed as
    "default == direct-local"; complements
    ``test_engine_registry::test_resolve_sink_non_hivemind_returns_direct_local``
    (which seeds a richer non-hive state) by pinning the bare-default case."""
    reg, _storage = _empty_registry()
    sink = await reg.resolve_sink("fresh-space")
    assert isinstance(sink, DirectLocalWriteSink)


async def test_default_resolved_sink_writes_byte_identically() -> None:
    """The resolved DEFAULT sink writes byte-identically to a parallel direct
    ``storage.put`` / ``put_json`` — a write-through byte check the existing
    registry test does not make. Proves default routing is a verbatim
    pass-through to local storage."""
    reg, sink_storage = _empty_registry()
    sink = await reg.resolve_sink("fresh-space")
    assert isinstance(sink, DirectLocalWriteSink)

    await sink.put("fresh-space/bank/x.md", "body-bytes")
    await sink.put_json("fresh-space/_meta.json", {"k": "v", "n": 1})

    # Parallel direct path on an independent identical fake.
    direct_storage = ConsolidatorFakeStorage()
    await direct_storage.put("fresh-space/bank/x.md", "body-bytes")
    await direct_storage.put_json("fresh-space/_meta.json", {"k": "v", "n": 1})

    assert sink_storage.objects == direct_storage.objects
    assert sink_storage.objects["fresh-space/bank/x.md"] == "body-bytes"


# =============================================================================
# SECTION 3 — COVERAGE MANIFEST (executable DoD anchor for P3)
# =============================================================================
#
# Names where each P3 contract is already tested, so the gate is discoverable.
# Documentation-as-test: pins that the sibling guard suites exist (importlib +
# getattr), making a renamed/removed P3 guard suite (or a named anchor function)
# RED here. Does NOT re-run or re-implement any sibling matrix. The new P3-8 file
# need not list itself.

COVERAGE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    # contract -> (module, representative test function names that prove it)
    "hive_engine_readonly_passthrough": (
        "tests.test_engine_hive",
        (
            "test_construct_from_store_via_di",
            "test_corruption_propagates_unchanged_status",
        ),
    ),
    "long_engine_downstream_passthrough": (
        "tests.test_engine_long",
        (
            "test_long_engine_connect_passes_through",
            "test_long_engine_takes_no_writesink",
        ),
    ),
    "short_engine_byte_identical_passthrough": (
        "tests.test_engine_short",
        (
            "test_short_engine_write_note_byte_identical_to_legacy",
            "test_short_engine_default_write_sink_is_direct_local",
        ),
    ),
    "mid_engine_consolidation_passthrough": (
        "tests.test_engine_mid",
        (
            "test_mid_engine_consolidate_delegates_verbatim",
            "test_mid_engine_documents_writesink_mutation_call_sites",
        ),
    ),
    "registry_resolve_sink_three_valued_verdict": (
        "tests.test_engine_registry",
        (
            "test_resolve_sink_non_hivemind_returns_direct_local",
            "test_resolve_sink_corrupted_propagates_corrupted_state_error",
        ),
    ),
    "write_sink_boundary_byte_parity_and_fail_closed": (
        "tests.test_write_sink",
        (
            "test_direct_local_put_matches_direct_storage",
            # P5-8 (#16): the staged sink now BUFFERS put + commits atomically;
            # the fail-closed boundary anchor is the deferred put-only-delete leg.
            "test_staged_delete_raises_and_never_writes",
        ),
    ),
    "hivemind_routing_verdict_mapping": (
        "tests.test_hivemind_routing",
        (
            "test_route_clean_non_hivemind_is_direct_local",
            "test_route_corrupted_critical_is_not_not_shared_and_refuses",
        ),
    ),
    "tool_path_routing_byte_identical_and_fail_closed": (
        "tests.test_writesink_routing",
        (
            "test_live_note_non_hivemind_byte_identical",
            "test_bank_consolidate_healthy_hive_fails_closed_no_enqueue",
        ),
    ),
    "static_no_adhoc_storage_for_mutations": (
        "tests.test_writesink_no_adhoc_storage",
        (
            "test_live_note_routes_through_registry_not_adhoc_storage",
            "test_bank_delete_routes_delete_many_through_sink",
        ),
    ),
}


def test_coverage_manifest_names_every_p3_contract():
    """Every contract in COVERAGE_MAP points to a real P3 sibling guard module
    whose named test functions actually exist. If a sibling guard suite is
    renamed or removed (or an anchor function renamed), this gate goes RED —
    keeping the P3 manifest honest instead of letting a free-form docstring
    silently rot. The executable DoD anchor for the P3 engine-boundaries epic.

    Non-vacuous: the loop body asserts on real importlib/getattr results, and we
    pin that all nine current EPIC-P3 behavioral guard suites are represented
    so the manifest cannot
    silently shrink.
    """
    expected_contracts = {
        "hive_engine_readonly_passthrough",
        "long_engine_downstream_passthrough",
        "short_engine_byte_identical_passthrough",
        "mid_engine_consolidation_passthrough",
        "registry_resolve_sink_three_valued_verdict",
        "write_sink_boundary_byte_parity_and_fail_closed",
        "hivemind_routing_verdict_mapping",
        "tool_path_routing_byte_identical_and_fail_closed",
        "static_no_adhoc_storage_for_mutations",
    }
    assert set(COVERAGE_MAP) == expected_contracts

    for contract, (module_name, func_names) in COVERAGE_MAP.items():
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Skip ONLY when the named guard suite itself is intentionally absent
            # from the staged public tree. A transitive missing dependency (a
            # different module name) or a deleted suite in the private tree must
            # still fail — this accommodation must not weaken private CI.
            if exc.name == module_name:
                pytest.skip(f"P3 guard suite {module_name} is private-only (absent from the public release tree)")
            raise
        assert func_names, f"{contract}: no test functions named"
        for fn in func_names:
            obj = getattr(module, fn, None)
            assert callable(obj), (
                f"{contract}: {module_name}::{fn} missing/not callable"
            )
