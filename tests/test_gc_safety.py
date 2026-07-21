# -*- coding: utf-8 -*-
"""Issue #163 — route-first and exact-set safety for orphan-note GC.

The suite is deterministic and offline.  It exercises the real engine routing
seam over in-memory Hivemind state, and mutation-proves that an equal-count key
substitution cannot pass the delete precondition.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from live_mem.core import gc as gc_module
from live_mem.core.consolidator import ConsolidatorService
from live_mem.core.engines import EngineRegistry, RegistryRefused
from live_mem.core.hivemind import CorruptedStateError
from live_mem.core.hivemind.models import HiveNodeStatus, NodeHealth
from live_mem.core.hivemind.state import HivemindStateStore
from live_mem.core.hivemind import layout
from live_mem.core.locks import get_lock_manager
from live_mem.core.write_sink import DirectLocalWriteSink, StagedWriteNotImplemented
from tests.test_engine_registry import _seed_healthy_hive
from tests.test_write_sink import WriteSinkFakeStorage


class GCStorage(WriteSinkFakeStorage):
    """WriteSink fake plus the read helpers used by GC/consolidator."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_delete: set[str] = set()

    async def list_prefixes(self, prefix: str) -> list[str]:
        prefixes: set[str] = set()
        for key in self.objects:
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix) :]
            if "/" not in suffix:
                continue
            first = suffix.split("/", 1)[0]
            prefixes.add(f"{prefix}{first}/")
        return sorted(prefixes)

    async def list_and_get(self, prefix: str) -> list[dict[str, str]]:
        return [
            {"key": key, "content": self.objects[key]}
            for key in sorted(self.objects)
            if key.startswith(prefix)
        ]

    async def delete(self, key: str) -> None:
        if key in self.fail_delete:
            raise RuntimeError("injected delete failure")
        await super().delete(key)


def _registry(storage: GCStorage) -> EngineRegistry:
    return EngineRegistry(
        storage=storage,  # type: ignore[arg-type]
        live=object(),  # type: ignore[arg-type]
        consolidator=object(),  # type: ignore[arg-type]
        queue=object(),  # type: ignore[arg-type]
        bridge=object(),  # type: ignore[arg-type]
    )


def _bind_runtime(monkeypatch: pytest.MonkeyPatch, storage: GCStorage) -> None:
    from live_mem.core import engines

    registry = _registry(storage)
    monkeypatch.setattr(gc_module, "get_storage", lambda: storage)
    monkeypatch.setattr(engines, "get_engine_registry", lambda: registry)


def _old_key(space_id: str, suffix: str, agent: str = "alice") -> str:
    uuid8 = hashlib.sha256(suffix.encode()).hexdigest()[:8]
    return f"{space_id}/live/20000101T000000_{agent}_observation_{uuid8}.md"


def _fresh_key(space_id: str, suffix: str, agent: str = "alice") -> str:
    uuid8 = hashlib.sha256(suffix.encode()).hexdigest()[:8]
    return f"{space_id}/live/29990101T000000_{agent}_observation_{uuid8}.md"


def _seed_space(storage: GCStorage, space_id: str, *note_keys: str) -> None:
    storage.objects[f"{space_id}/_meta.json"] = "{}"
    storage.objects[f"{space_id}/_rules.md"] = "# Rules"
    for key in note_keys:
        storage.objects[key] = f"note:{key}"


async def _seed_route(storage: GCStorage, space_id: str, route: str) -> None:
    if route in {"healthy", "resync"}:
        await _seed_healthy_hive(storage, space_id)
    if route == "unsafe":
        store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.UNSAFE, reason="injected unsafe")
        )
    elif route == "resync":
        store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
        await store.set_node_status(
            NodeHealth(
                status=HiveNodeStatus.RESYNC_REQUIRED,
                reason="injected resync",
                observed_epoch=9,
            )
        )
    elif route == "corrupt":
        storage.objects[layout.node_key(space_id)] = "{not-json"


@pytest.mark.parametrize("operation", ["consolidate", "delete"])
@pytest.mark.parametrize(
    ("route", "expected_exception"),
    [
        ("healthy", StagedWriteNotImplemented),
        ("unsafe", RegistryRefused),
        ("resync", RegistryRefused),
        ("corrupt", CorruptedStateError),
    ],
)
async def test_non_direct_routes_fail_typed_with_zero_durable_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    route: str,
    expected_exception: type[Exception],
) -> None:
    storage = GCStorage()
    sid = f"gc-{route}-{operation}"
    _seed_space(storage, sid, _old_key(sid, "a"))
    await _seed_route(storage, sid, route)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)
    before = storage.snapshot()

    with pytest.raises(expected_exception):
        if operation == "consolidate":
            await service.consolidate_old_notes(sid, 7)
        else:
            await service.delete_old_notes(
                sid,
                7,
                expected_eligible_set_token=dry["eligible_set_token"],
            )

    assert storage.snapshot() == before


@pytest.mark.parametrize("operation", ["consolidate", "delete"])
async def test_global_preflight_refuses_before_mutating_an_earlier_local_space(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    storage = GCStorage()
    local = "a-local"
    shared = "z-shared"
    _seed_space(storage, local, _old_key(local, "a"))
    _seed_space(storage, shared, _old_key(shared, "b"))
    await _seed_healthy_hive(storage, shared)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes("", 7)
    before = storage.snapshot()

    with pytest.raises(StagedWriteNotImplemented):
        if operation == "consolidate":
            await service.consolidate_old_notes("", 7)
        else:
            await service.delete_old_notes(
                "",
                7,
                expected_eligible_set_token=dry["eligible_set_token"],
            )

    assert storage.snapshot() == before
    assert _old_key(local, "a") in storage.objects


def test_eligible_set_token_is_stable_scoped_and_opaque() -> None:
    keys = ["space/live/old-b.md", "space/live/old-a.md"]
    token = gc_module._eligible_set_token(
        space_id="space", max_age_days=7, keys=keys
    )

    assert token == gc_module._eligible_set_token(
        space_id="space", max_age_days=7, keys=reversed(keys)
    )
    assert token != gc_module._eligible_set_token(
        space_id="space", max_age_days=8, keys=keys
    )
    assert token != gc_module._eligible_set_token(
        space_id="", max_age_days=7, keys=keys
    )
    assert token.startswith("gc-set-v1:")
    assert len(token) == len("gc-set-v1:") + 64
    assert all(key not in token for key in keys)


async def test_empty_consolidation_keeps_the_full_count_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-empty-consolidation"
    _seed_space(storage, sid)
    _bind_runtime(monkeypatch, storage)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "ok"
    assert result["action"] == "consolidate"
    assert result["consolidated"] == 0
    assert result["consolidation_requested"] == 0
    assert result["consolidation_failed"] == 0
    assert result["consolidation_details"] == {}
    assert "eligible_set_token" not in result


@pytest.mark.parametrize("token", ["", "wrong", "gc-set-v1:not-hex"])
async def test_delete_requires_a_well_formed_prior_token_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    storage = GCStorage()
    sid = "local-required"
    _seed_space(storage, sid, _old_key(sid, "a"))
    _bind_runtime(monkeypatch, storage)
    before = storage.snapshot()

    result = await gc_module.GCService().delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=token,
    )

    assert result["status"] == "error"
    assert result["reason"] == "eligible_set_token_required"
    assert result["deleted"] == 0
    assert storage.snapshot() == before


async def test_equal_count_key_substitution_is_rejected_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-substitution"
    key_a = _old_key(sid, "a")
    key_b = _old_key(sid, "b")
    _seed_space(storage, sid, key_a)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)

    del storage.objects[key_a]
    storage.objects[key_b] = "substituted with equal cardinality"
    before_delete = storage.snapshot()
    result = await service.delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )

    assert result == {
        "status": "conflict",
        "reason": "eligible_set_changed",
        "action": "delete",
        "deleted": 0,
        "message": result["message"],
    }
    assert "eligible_set_token" not in result
    assert storage.snapshot() == before_delete
    assert key_b in storage.objects


async def test_equal_count_substitution_between_discovery_and_locked_scan_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-midflight-substitution"
    key_a = _old_key(sid, "a")
    key_b = _old_key(sid, "b")
    _seed_space(storage, sid, key_a)
    _bind_runtime(monkeypatch, storage)
    dry = await gc_module.GCService().scan_old_notes(sid, 7)

    class SubstitutingGC(gc_module.GCService):
        calls = 0

        async def scan_old_notes(self, *args, **kwargs):
            result = await super().scan_old_notes(*args, **kwargs)
            self.calls += 1
            if self.calls == 1:
                del storage.objects[key_a]
                storage.objects[key_b] = "same count, different key"
            return result

    result = await SubstitutingGC().delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )

    assert result["status"] == "conflict"
    assert result["reason"] == "eligible_set_changed"
    assert result["deleted"] == 0
    assert key_b in storage.objects


@pytest.mark.parametrize("operation", ["consolidate", "delete"])
async def test_route_is_rechecked_under_lock_before_the_first_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from live_mem.core import engines

    storage = GCStorage()
    sid = f"route-drift-{operation}"
    old = _old_key(sid, "a")
    _seed_space(storage, sid, old)
    monkeypatch.setattr(gc_module, "get_storage", lambda: storage)

    class SequenceRegistry:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_sink(self, space_id: str):
            assert space_id == sid
            self.calls += 1
            if self.calls == 1:
                return DirectLocalWriteSink(storage=storage)
            return object()  # mapped by GC to StagedWriteNotImplemented

    registry = SequenceRegistry()
    monkeypatch.setattr(engines, "get_engine_registry", lambda: registry)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)
    before = storage.snapshot()

    if operation == "delete":
        with pytest.raises(StagedWriteNotImplemented):
            await service.delete_old_notes(
                sid,
                7,
                expected_eligible_set_token=dry["eligible_set_token"],
            )
    else:
        result = await service.consolidate_old_notes(sid, 7)
        assert result["status"] == "partial"
        assert result["reason"] == "partial_consolidation"

    assert registry.calls == 2
    assert storage.snapshot() == before


async def test_valid_token_deletes_only_the_freshly_scanned_exact_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-valid"
    old = _old_key(sid, "a")
    fresh = _fresh_key(sid, "fresh")
    _seed_space(storage, sid, old, fresh)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)

    result = await service.delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )

    assert result["status"] == "deleted"
    assert result["delete_requested"] == 1
    assert result["deleted"] == 1
    assert result["delete_failed"] == 0
    assert old not in storage.objects
    assert fresh in storage.objects


async def test_destructive_scan_excludes_nested_and_malformed_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-canonical-only"
    canonical = _old_key(sid, "canonical")
    nested = f"{sid}/live/archive/20000101T000000_alice_observation_deadbeef.md"
    malformed = f"{sid}/live/20000101T000000_alice_observation_not-a-uuid.md"
    _seed_space(storage, sid, canonical, nested, malformed)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()

    dry = await service.scan_old_notes(sid, 7)

    assert dry["total_old_notes"] == 1
    assert dry["spaces"][sid]["keys"] == [canonical]
    result = await service.delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )
    assert result["status"] == "deleted"
    assert result["deleted"] == 1
    assert canonical not in storage.objects
    assert nested in storage.objects
    assert malformed in storage.objects


async def test_global_delete_rechecks_each_space_route_after_earlier_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import engines

    storage = GCStorage()
    first_sid = "a-delete-local"
    second_sid = "z-delete-drifts-shared"
    first = _old_key(first_sid, "first")
    second = _old_key(second_sid, "second")
    _seed_space(storage, first_sid, first)
    _seed_space(storage, second_sid, second)
    monkeypatch.setattr(gc_module, "get_storage", lambda: storage)

    class SequenceRegistry:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_sink(self, _space_id: str):
            self.calls += 1
            # Discovery preflight (2), locked all-space proof (2), then A's
            # immediate proof are local. B drifts before its own delete.
            if self.calls <= 5:
                return DirectLocalWriteSink(storage=storage)
            return object()

    registry = SequenceRegistry()
    monkeypatch.setattr(engines, "get_engine_registry", lambda: registry)
    service = gc_module.GCService()
    dry = await service.scan_old_notes("", 7)

    result = await service.delete_old_notes(
        "",
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )

    assert result["status"] == "partial"
    assert result["reason"] == "partial_delete"
    assert result["failure_reason"] == "route_staged_not_implemented"
    assert result["delete_requested"] == 2
    assert result["deleted"] == 1
    assert result["delete_failed"] == 1
    assert first not in storage.objects
    assert second in storage.objects


async def test_partial_delete_reports_actual_count_and_never_claims_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-partial"
    first = _old_key(sid, "a")
    second = _old_key(sid, "b")
    _seed_space(storage, sid, first, second)
    storage.fail_delete.add(second)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)

    result = await service.delete_old_notes(
        sid,
        7,
        expected_eligible_set_token=dry["eligible_set_token"],
    )

    assert result["status"] == "partial"
    assert result["reason"] == "partial_delete"
    assert result["delete_requested"] == 2
    assert result["deleted"] == 1
    assert result["delete_failed"] == 1
    assert first not in storage.objects
    assert second in storage.objects


class _RecordingConsolidator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
        self.calls.append({"space_id": space_id, **kwargs})
        return {
            "status": "ok",
            "notes_processed": len(kwargs.get("note_keys", [])),
            "notes_deleted": len(kwargs.get("note_keys", [])),
            "notes_delete_failed": 0,
            "notes_remaining": 0,
            "bank_files_created": 0,
            "bank_files_updated": 0,
        }


async def test_gc_consolidation_passes_an_exact_allowlist_excluding_fresh_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-consolidate"
    old = _old_key(sid, "old")
    fresh = _fresh_key(sid, "fresh")
    _seed_space(storage, sid, old, fresh)
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "ok"
    assert result["consolidated"] == 1
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["enforce_cooldown"] is False
    assert old in call["note_keys"]
    assert fresh not in call["note_keys"]
    notice_keys = [key for key in call["note_keys"] if key not in {old, fresh}]
    assert len(notice_keys) == 1
    assert notice_keys[0].startswith(f"{sid}/live/")
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["notice_written"] is True
    assert detail["notice_processed"] is True
    assert "notes_deleted" not in detail
    assert "notes_delete_failed" not in detail
    assert "notes_remaining" not in detail


async def test_gc_disables_per_space_cooldown_for_every_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-two-agents"
    _seed_space(
        storage,
        sid,
        _old_key(sid, "a", agent="alice"),
        _old_key(sid, "b", agent="bob"),
    )
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "ok"
    assert [call["agent"] for call in recorder.calls] == ["alice", "bob"]
    assert all(call["enforce_cooldown"] is False for call in recorder.calls)


async def test_gc_keeps_underscore_agent_identity_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-underscore-agent"
    old = _old_key(sid, "old", agent="foo_bar")
    _seed_space(storage, sid, old)
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)

    dry = await gc_module.GCService().scan_old_notes(sid, 7)
    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert dry["spaces"][sid]["by_agent"] == {"foo_bar": 1}
    assert result["status"] == "ok"
    assert [call["agent"] for call in recorder.calls] == ["foo_bar"]
    notice_key = next(key for key in recorder.calls[0]["note_keys"] if key != old)
    assert 'agent: "foo_bar"' in storage.objects[notice_key]


async def test_gc_rechecks_route_before_each_agent_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import engines

    storage = GCStorage()
    sid = "route-drift-between-agents"
    alice = _old_key(sid, "a", agent="alice")
    bob = _old_key(sid, "b", agent="bob")
    _seed_space(storage, sid, alice, bob)
    monkeypatch.setattr(gc_module, "get_storage", lambda: storage)

    class SequenceRegistry:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_sink(self, space_id: str):
            assert space_id == sid
            self.calls += 1
            # Global preflight, Alice notice, and Alice consolidate are local.
            # The route becomes shared before Bob's notice.
            if self.calls <= 3:
                return DirectLocalWriteSink(storage=storage)
            return object()

    registry = SequenceRegistry()
    monkeypatch.setattr(engines, "get_engine_registry", lambda: registry)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["consolidated"] == 1
    assert [call["agent"] for call in recorder.calls] == ["alice"]
    assert result["consolidation_details"][sid]["bob"]["reason"] == (
        "route_staged_not_implemented"
    )
    created_notices = [
        key
        for key in storage.objects
        if key.startswith(f"{sid}/live/") and key not in {alice, bob}
    ]
    assert len(created_notices) == 1
    assert 'agent: "alice"' in storage.objects[created_notices[0]]


async def test_gc_preserves_prior_counts_when_later_route_recheck_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import engines

    storage = GCStorage()
    sid = "route-read-failure-between-agents"
    alice = _old_key(sid, "a", agent="alice")
    bob = _old_key(sid, "b", agent="bob")
    _seed_space(storage, sid, alice, bob)
    monkeypatch.setattr(gc_module, "get_storage", lambda: storage)

    class SequenceRegistry:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_sink(self, space_id: str):
            assert space_id == sid
            self.calls += 1
            # Global preflight + Alice notice/final proof succeed. Bob's
            # notice-time route read fails after Alice's result is durable.
            if self.calls <= 3:
                return DirectLocalWriteSink(storage=storage)
            raise RuntimeError("injected later route read failure")

    # Keep one registry instance across every route resolution.
    registry = SequenceRegistry()
    monkeypatch.setattr(engines, "get_engine_registry", lambda: registry)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "partial"
    assert result["consolidated"] == 1
    assert result["consolidation_requested"] == 2
    assert result["consolidation_failed"] == 1
    assert [call["agent"] for call in recorder.calls] == ["alice"]
    assert result["consolidation_details"][sid]["bob"]["reason"] == (
        "route_recheck_failed"
    )


async def test_gc_preserves_prior_agent_counts_when_next_recheck_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "recheck-failure-between-agents"
    alice = _old_key(sid, "a", agent="alice")
    bob = _old_key(sid, "b", agent="bob")
    _seed_space(storage, sid, alice, bob)
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)
    original_exists = storage.exists

    async def fail_bob_recheck(key: str) -> bool:
        if key == bob:
            raise RuntimeError("injected second-agent recheck failure")
        return await original_exists(key)

    monkeypatch.setattr(storage, "exists", fail_bob_recheck)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["consolidated"] == 1
    assert result["consolidation_requested"] == 2
    assert result["consolidation_failed"] == 1
    assert [call["agent"] for call in recorder.calls] == ["alice"]
    assert result["consolidation_details"][sid]["bob"]["reason"] == (
        "selected_note_recheck_failed"
    )


async def test_gc_notice_failure_stops_the_agent_and_reports_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-notice-failure"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)
    monkeypatch.setattr(
        gc_module,
        "_write_gc_notice",
        AsyncMock(side_effect=RuntimeError("injected notice write failure")),
    )
    before = storage.snapshot()

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["consolidated"] == 0
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["reason"] == "gc_notice_failed"
    assert detail["notice_written"] is False
    assert recorder.calls == []
    assert storage.snapshot() == before


async def test_gc_cleans_written_notice_when_consolidator_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-consolidator-failure-cleans-notice"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    _bind_runtime(monkeypatch, storage)
    failing = AsyncMock()
    failing.consolidate.side_effect = RuntimeError("injected consolidator failure")
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: failing)

    result = await gc_module.GCService().consolidate_old_notes(sid, 7)

    assert result["status"] == "partial"
    assert result["consolidated"] == 0
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["reason"] == "consolidation_failed"
    assert detail["notice_written"] is True
    assert detail["notice_processed"] is False
    assert detail["notice_cleaned"] is True
    remaining_live = [key for key in storage.objects if key.startswith(f"{sid}/live/")]
    assert remaining_live == [old]


async def test_busy_consolidation_lock_writes_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "local-busy"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    _bind_runtime(monkeypatch, storage)
    recorder = _RecordingConsolidator()
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: recorder)
    lock = get_lock_manager().consolidation(sid)
    await lock.acquire()
    before = storage.snapshot()
    try:
        result = await gc_module.GCService().consolidate_old_notes(sid, 7)
    finally:
        lock.release()

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert recorder.calls == []
    assert storage.snapshot() == before


async def test_delete_refuses_a_busy_consolidation_lock_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = GCStorage()
    sid = "local-delete-busy"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    _bind_runtime(monkeypatch, storage)
    service = gc_module.GCService()
    dry = await service.scan_old_notes(sid, 7)
    lock = get_lock_manager().consolidation(sid)
    await lock.acquire()
    before = storage.snapshot()
    try:
        result = await service.delete_old_notes(
            sid,
            7,
            expected_eligible_set_token=dry["eligible_set_token"],
        )
    finally:
        lock.release()

    assert result["status"] == "conflict"
    assert result["reason"] == "consolidation_in_progress"
    assert result["deleted"] == 0
    assert "eligible_set_token" not in result
    assert storage.snapshot() == before


async def test_consolidator_collect_inputs_honours_empty_and_exact_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "collect-allowlist"
    selected = _old_key(sid, "selected")
    excluded = _old_key(sid, "excluded")
    _seed_space(storage, sid, selected, excluded)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)
    service._max_notes = 100

    exact = await service._collect_inputs(
        sid,
        agent="alice",
        note_keys=[selected],
    )
    empty = await service._collect_inputs(
        sid,
        agent="alice",
        note_keys=[],
    )

    assert exact["notes_keys"] == [selected]
    assert empty["notes_keys"] == []


async def test_consolidator_agent_filter_uses_exact_front_matter_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "collect-agent-identity"
    underscored = _old_key(sid, "underscored", agent="foo_bar")
    alice_decision = (
        f"{sid}/live/20000101T000000_alice_decision_deadbeef.md"
    )
    _seed_space(storage, sid, underscored, alice_decision)
    storage.objects[underscored] = '---\nagent: "foo_bar"\n---\n\nunderscored'
    storage.objects[alice_decision] = '---\nagent: "alice"\n---\n\nalice'
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)
    service._max_notes = 100

    matching = await service._collect_inputs(sid, agent="foo_bar")
    category_collision = await service._collect_inputs(sid, agent="decision")

    assert matching["notes_keys"] == [underscored]
    assert category_collision["notes_keys"] == []


async def test_consolidator_agent_filter_resists_filename_normalization_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "collect-agent-collision"
    dotted = f"{sid}/live/20000101T000000_ab---c_observation_11111111.md"
    plain = f"{sid}/live/20000101T000001_ab---c_observation_22222222.md"
    malformed = f"{sid}/live/20000101T000002_ab---c_observation_33333333.md"
    _seed_space(storage, sid, dotted, plain, malformed)
    storage.objects[dotted] = '---\nagent: "a.b---c"\n---\n\ndotted'
    storage.objects[plain] = '---\nagent: "ab---c"\n---\n\nplain'
    storage.objects[malformed] = "no front matter"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)
    service._max_notes = 100

    dotted_scope = await service._collect_inputs(sid, agent="a.b---c")
    plain_scope = await service._collect_inputs(sid, agent="ab---c")
    global_scope = await service._collect_inputs(sid, agent="")

    assert dotted_scope["notes_keys"] == [dotted]
    assert plain_scope["notes_keys"] == [plain]
    assert global_scope["notes_keys"] == [dotted, plain, malformed]


def test_consolidator_prompt_preserves_body_with_inline_delimiter_in_identity() -> None:
    service = object.__new__(ConsolidatorService)
    body = "PAYLOAD EXACT --- body marker stays"
    content = (
        '---\nagent: "a.b---c"\ncategory: "decision"\n'
        'tags: ["identity"]\n---\n\n' + body
    )

    messages = service._build_prompt(
        space_id="prompt-agent-identity",
        rules="# Rules",
        synthesis=None,
        notes=[
            {
                "key": (
                    "prompt-agent-identity/live/"
                    "20000101T000000_ab---c_decision_11111111.md"
                ),
                "content": content,
            }
        ],
        bank_files=[],
    )
    user_prompt = messages[1]["content"]

    assert "[agent=a.b---c, catégorie=decision, tags=[\"identity\"]]" in user_prompt
    assert body in user_prompt
    assert 'agent: "a.b---c"' not in user_prompt


async def test_consolidator_allowlist_rejects_missing_or_foreign_keys_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "collect-conflict"
    selected = _old_key(sid, "selected")
    _seed_space(storage, sid, selected)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)
    service._max_notes = 100

    missing = await service._collect_inputs(
        sid,
        note_keys=[_old_key(sid, "missing")],
    )
    foreign = await service._collect_inputs(
        sid,
        note_keys=["other/live/20000101T000000_alice_observation_x.md"],
    )

    assert missing["status"] == "conflict"
    assert missing["reason"] == "selected_note_set_changed"
    assert foreign["status"] == "error"
    assert foreign["reason"] == "invalid_selected_note_key"


async def test_exact_selection_preserves_notice_first_under_max_notes_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "collect-notice-first"
    old = _old_key(sid, "old")
    notice = _fresh_key(sid, "notice")
    _seed_space(storage, sid, old, notice)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)
    service._max_notes = 1

    selected = await service._collect_inputs(
        sid,
        agent="alice",
        note_keys=[notice, old],
    )

    assert selected["notes_keys"] == [notice]
    assert selected["notes_remaining"] == 1


async def test_consolidator_write_results_reports_partial_live_note_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "consolidator-partial-delete"
    first = _old_key(sid, "first")
    second = _old_key(sid, "second")
    _seed_space(storage, sid, first, second)
    storage.fail_delete.add(second)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)

    result = await service._write_results(
        space_id=sid,
        llm_output={"file_edits": [], "synthesis": "Integrated safely."},
        bank_files=[],
        notes_keys=[first, second],
        notes_count=2,
        usage={},
        skip_meta=True,
    )

    assert result["status"] == "partial"
    assert result["reason"] == "partial_delete"
    assert result["notes_processed"] == 2
    assert result["notes_deleted"] == 1
    assert result["notes_delete_failed"] == 1
    assert first not in storage.objects
    assert second in storage.objects


async def test_consolidator_keeps_all_sources_when_a_bank_edit_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "consolidator-invalid-edit-never-drop"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)

    result = await service._write_results(
        space_id=sid,
        llm_output={
            "file_edits": [
                {
                    "filename": "facts.md",
                    "action": "unsupported-action",
                }
            ],
            "synthesis": "Incomplete integration.",
        },
        bank_files=[],
        notes_keys=[old],
        notes_count=1,
        usage={},
        skip_meta=True,
    )

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["operations_failed"] == 1
    assert result["notes_processed"] == 0
    assert result["notes_deleted"] == 0
    assert result["notes_delete_failed"] == 1
    assert old in storage.objects


async def test_consolidator_does_not_read_storage_after_live_note_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    class NoReadsAfterDeleteStorage(GCStorage):
        delete_completed = False

        async def delete_many(self, keys: list[str]) -> int:
            deleted = await super().delete_many(keys)
            self.delete_completed = True
            return deleted

        async def list_objects(self, prefix: str) -> list[dict]:
            if self.delete_completed:
                raise RuntimeError("injected post-delete read failure")
            return await super().list_objects(prefix)

    storage = NoReadsAfterDeleteStorage()
    sid = "consolidator-delete-last-await"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)

    result = await service._write_results(
        space_id=sid,
        llm_output={"file_edits": [], "synthesis": "Integrated safely."},
        bank_files=[],
        notes_keys=[old],
        notes_count=1,
        usage={},
        skip_meta=True,
    )

    assert result["status"] == "ok"
    assert result["notes_processed"] == 1
    assert result["notes_deleted"] == 1
    assert old not in storage.objects


async def test_consolidator_bank_count_failure_happens_before_live_note_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    class FailingBankCountStorage(GCStorage):
        async def list_objects(self, prefix: str) -> list[dict]:
            if prefix.endswith("/bank/"):
                raise RuntimeError("injected bank count failure")
            return await super().list_objects(prefix)

    storage = FailingBankCountStorage()
    sid = "consolidator-bank-count-before-delete"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = object.__new__(ConsolidatorService)

    with pytest.raises(RuntimeError, match="bank count failure"):
        await service._write_results(
            space_id=sid,
            llm_output={"file_edits": [], "synthesis": "Integrated safely."},
            bank_files=[],
            notes_keys=[old],
            notes_count=1,
            usage={},
            skip_meta=True,
        )

    assert old in storage.objects


async def test_consolidator_pipeline_never_reports_ok_after_an_incomplete_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "consolidator-incomplete-batch"
    first = _old_key(sid, "first")
    second = _old_key(sid, "second")
    _seed_space(storage, sid, first, second)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = object.__new__(ConsolidatorService)
    service._batch_size = 1
    service._validation_enabled = False
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [
                {"key": first, "content": "first"},
                {"key": second, "content": "second"},
            ],
            "notes_keys": [first, second],
            "notes_remaining": 0,
            "bank_files": [],
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        side_effect=[
            {"status": "ok", "data": {}, "usage": {}},
            {"status": "error", "message": "injected second-batch failure"},
        ]
    )
    service._write_results = AsyncMock(
        return_value={
            "status": "ok",
            "notes_processed": 1,
            "notes_deleted": 1,
            "notes_delete_failed": 0,
        }
    )

    result = await service.consolidate(sid, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["batches_total"] == 2
    assert result["batches_completed"] == 1
    assert result["notes_processed"] == 1
    assert result["notes_remaining"] == 1


async def test_consolidator_preserves_first_batch_counts_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    class RefreshFailsAfterDeleteStorage(GCStorage):
        delete_completed = False

        async def delete_many(self, keys: list[str]) -> int:
            deleted = await super().delete_many(keys)
            self.delete_completed = True
            return deleted

        async def list_and_get(self, prefix: str) -> list[dict[str, str]]:
            if self.delete_completed and prefix.endswith("/bank/"):
                raise RuntimeError("injected next-batch refresh failure")
            return await super().list_and_get(prefix)

    storage = RefreshFailsAfterDeleteStorage()
    sid = "consolidator-refresh-after-first-delete"
    first = _old_key(sid, "first")
    second = _old_key(sid, "second")
    _seed_space(storage, sid, first, second)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = object.__new__(ConsolidatorService)
    service._batch_size = 1
    service._validation_enabled = False
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [
                {"key": first, "content": "first"},
                {"key": second, "content": "second"},
            ],
            "notes_keys": [first, second],
            "notes_remaining": 0,
            "bank_files": [],
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={"status": "ok", "data": {}, "usage": {}}
    )

    result = await service.consolidate(sid, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["failure_reason"] == "batch_refresh_failed"
    assert result["notes_processed"] == 1
    assert result["notes_deleted"] == 1
    assert result["notes_remaining"] == 1
    assert first not in storage.objects
    assert second in storage.objects


async def test_consolidator_preserves_delete_counts_when_metadata_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    class MetaFailsAfterDeleteStorage(GCStorage):
        delete_completed = False

        async def delete_many(self, keys: list[str]) -> int:
            deleted = await super().delete_many(keys)
            self.delete_completed = True
            return deleted

        async def put_json(self, key: str, data: dict) -> None:
            if self.delete_completed and key.endswith("/_meta.json"):
                raise RuntimeError("injected post-delete metadata failure")
            await super().put_json(key, data)

    storage = MetaFailsAfterDeleteStorage()
    sid = "consolidator-metadata-after-delete"
    old = _old_key(sid, "old")
    _seed_space(storage, sid, old)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = object.__new__(ConsolidatorService)
    service._batch_size = 10
    service._validation_enabled = False
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": old, "content": "old"}],
            "notes_keys": [old],
            "notes_remaining": 0,
            "bank_files": [],
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={"status": "ok", "data": {}, "usage": {}}
    )

    result = await service.consolidate(sid, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["metadata_update_failed"] is True
    assert result["notes_processed"] == 1
    assert result["notes_deleted"] == 1
    assert result["notes_remaining"] == 0
    assert "Aucune note source ne reste" in result["message"]
    assert old not in storage.objects


async def test_consolidator_pipeline_counts_live_notes_left_after_partial_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "consolidator-live-note-remains"
    first = _old_key(sid, "first")
    second = _old_key(sid, "second")
    _seed_space(storage, sid, first, second)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = object.__new__(ConsolidatorService)
    service._batch_size = 10
    service._validation_enabled = False
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [
                {"key": first, "content": "first"},
                {"key": second, "content": "second"},
            ],
            "notes_keys": [first, second],
            "notes_remaining": 0,
            "bank_files": [],
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={"status": "ok", "data": {}, "usage": {}}
    )
    service._write_results = AsyncMock(
        return_value={
            "status": "partial",
            "reason": "partial_delete",
            "notes_processed": 2,
            "notes_deleted": 1,
            "notes_delete_failed": 1,
        }
    )

    result = await service.consolidate(sid, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_consolidation"
    assert result["batches_completed"] == 1
    assert result["notes_processed"] == 2
    assert result["notes_deleted"] == 1
    assert result["notes_delete_failed"] == 1
    assert result["notes_remaining"] == 1


@pytest.mark.parametrize(
    ("selected_keys", "expected_status"),
    [(None, "ok"), (["exact-selection"], "partial")],
)
async def test_only_exact_gc_selection_treats_max_notes_truncation_as_partial(
    monkeypatch: pytest.MonkeyPatch,
    selected_keys: list[str] | None,
    expected_status: str,
) -> None:
    from live_mem.core import consolidator as consolidator_module

    storage = GCStorage()
    sid = "consolidator-max-notes-contract"
    first = _old_key(sid, "first")
    _seed_space(storage, sid, first)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = object.__new__(ConsolidatorService)
    service._batch_size = 10
    service._validation_enabled = False
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": first, "content": "first"}],
            "notes_keys": [first],
            "notes_remaining": 1,
            "bank_files": [],
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={"status": "ok", "data": {}, "usage": {}}
    )
    service._write_results = AsyncMock(
        return_value={
            "status": "ok",
            "notes_processed": 1,
            "notes_deleted": 1,
            "notes_delete_failed": 0,
        }
    )

    result = await service.consolidate(
        sid,
        enforce_cooldown=False,
        note_keys=selected_keys,
    )

    assert result["status"] == expected_status
    assert result["notes_remaining"] == 1
    if expected_status == "partial":
        assert result["reason"] == "partial_consolidation"
