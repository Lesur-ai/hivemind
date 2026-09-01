# -*- coding: utf-8 -*-
"""Existing local space -> Project Mesh source transition (#413)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from live_mem.auth.context import current_token_info
from live_mem.core import locks as locks_module
from live_mem.core import live as live_module
from live_mem.core import space as space_module
from live_mem.core import tokens as tokens_module
from live_mem.core.backup import BackupService
from live_mem.core.consolidation_queue import ConsolidationQueueService
from live_mem.core.engines import EngineRegistry
from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    BootstrapLimitError,
    BootstrapService,
    HiveNodeStatus,
    HivemindStateStore,
    MembershipView,
    NodeHealth,
    PeerScope,
    TokenLeaseState,
    TokenState,
    WriteRoute,
    hive_status_label,
    layout,
    resolve_write_route,
    token_mutation_lock,
)
from live_mem.core.locks import LockManager
from live_mem.core.live import LiveService
from live_mem.core.reservation_guard import (
    clear_direct_local_checker,
    clear_reservation_checker,
    register_direct_local_checker,
    register_reservation_checker,
    source_preparation_key,
)
from live_mem.core.space import SpaceService
from live_mem.core.tokens import TokenService
from live_mem.core.write_sink import DirectLocalWriteFenced
from live_mem.mesh.identity import MESH_PRIVATE_KEY_PREFIX
from live_mem.mesh.pairing_service import (
    MeshPairingService,
    MeshPairingServiceError,
    _legacy_membership_key,
    _node_id_from_fingerprint,
)
from live_mem.mesh.bootstrap_snapshot import serialize_snapshot
from live_mem.mesh.pairing_state import (
    MeshPairingState,
    SourcePreparationIntent,
    SourcePreparationState,
)
from live_mem.mesh.pairing_store import MeshPairingStoreError
from live_mem.mesh import pairing_store as pairing_store_module
from tests.test_hivemind_state import FakeStorage
from tests.fakes.backup_storage import CopyFakeStorage
from tests.test_mesh_pairing_e2e import NOW_MS, SPACE, _config, _seed_source


_PRIVATE_A = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(
    bytes([201]) * 32
).decode().rstrip("=")
_PRIVATE_B = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(
    bytes([202]) * 32
).decode().rstrip("=")


class _FailAfterPutStorage(FakeStorage):
    """Persist one selected write, then raise an ambiguous timeout once."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_offset = 0
        self._armed_puts = 0

    def fail_after_put(self, offset: int) -> None:
        self._fail_offset = offset
        self._armed_puts = 0

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        await super().put(key, content, content_type)
        if self._fail_offset:
            self._armed_puts += 1
            if self._armed_puts == self._fail_offset:
                self._fail_offset = 0
                raise TimeoutError(f"ambiguous post-PUT timeout: {key}")


class _BlockingUnsafeStorage(FakeStorage):
    """Pause the first protocol write after the durable PREPARING intent."""

    def __init__(self) -> None:
        super().__init__()
        self.unsafe_started = asyncio.Event()
        self.release_unsafe = asyncio.Event()
        self._blocked = False

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        if key == layout.node_status_key(SPACE) and not self._blocked:
            self._blocked = True
            self.unsafe_started.set()
            await self.release_unsafe.wait()
        await super().put(key, content, content_type)


@pytest.fixture(autouse=True)
def _isolated_mesh_globals():
    clear_reservation_checker()
    clear_direct_local_checker()
    pairing_store_module._PAIRING_UNSAFE_PREFIXES.clear()
    yield
    clear_reservation_checker()
    clear_direct_local_checker()
    pairing_store_module._PAIRING_UNSAFE_PREFIXES.clear()


async def _create_product_space(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage
) -> dict[str, str]:
    """Create and populate a space through the real product service."""

    monkeypatch.setattr(space_module, "get_storage", lambda: storage)
    monkeypatch.setattr(tokens_module, "get_storage", lambda: storage)
    monkeypatch.setattr(tokens_module, "_token_service", TokenService())
    monkeypatch.setattr(locks_module, "_lock_manager", LockManager())
    created = await SpaceService().create(
        SPACE,
        "Existing product space",
        "# Rules\nKeep durable source content.",
        owner="operator",
        bootstrap_admin=True,
    )
    assert created["status"] == "created"
    await storage.put(f"{SPACE}/live/note.md", "# Live\nexisting note")
    await storage.put(f"{SPACE}/bank/activeContext.md", "# Bank\nexisting bank")
    return {
        key: value
        for key, value in storage.snapshot().items()
        if key.startswith(f"{SPACE}/") and "/_hivemind/" not in key
    }


def _service(
    storage: FakeStorage,
    *,
    private_key: str = _PRIVATE_A,
    queue: ConsolidationQueueService | None = None,
    public_url: str = "https://source.mesh.test",
    max_objects: int | None = None,
    max_bytes: int | None = None,
) -> MeshPairingService:
    config = _config(private_key, public_url)
    if max_objects is not None:
        config = replace(config, bootstrap_max_objects=max_objects)
    if max_bytes is not None:
        config = replace(config, bootstrap_max_bytes=max_bytes)
    return MeshPairingService(
        config,
        storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _endpoint: None,
        consolidation_queue=queue or ConsolidationQueueService(),
    )


async def _prepare(service: MeshPairingService) -> dict:
    before = await service.inspect_source_eligibility(SPACE)
    return await service.prepare_source(
        SPACE,
        expected_state_token=before["state_token"],
        quiesced=True,
    )


async def test_real_space_create_prepare_invite_preserves_business_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    business_before = await _create_product_space(monkeypatch, storage)
    service = _service(storage)

    before = await service.inspect_source_eligibility(SPACE)
    assert before["state"] == "local_only_can_prepare"
    assert before["source_initializable"] is True
    assert before["can_create_invitation"] is False

    result = await _prepare(service)
    assert result["result"] == "prepared"
    assert result["source"]["state"] == "ready"
    assert result["source"]["can_create_invitation"] is True

    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    node = await store.get_node_identity()
    membership = await store.get_membership()
    health = await store.get_node_status()
    term = await store.get_term()
    token = await store.get_token()
    pointer = await store.get_bank_version_pointer()
    assert node is not None and membership is not None
    assert len(membership.members) == 1
    member = membership.members[0]
    assert member.node_id == node.node_id
    assert member.public_key == node.public_key
    assert member.status == "active"
    assert member.scopes == sorted(
        [
            PeerScope.READ.value,
            PeerScope.PROPOSE.value,
            PeerScope.COMMIT.value,
        ]
    )
    assert membership.epoch == 0
    assert term is not None and term.term == 0
    assert token is not None and token.state == TokenState.FREE.value
    assert token.membership_epoch == 0 and token.bank_version == -1
    assert pointer is not None and pointer.bank_version == -1 and pointer.commit_id == ""
    assert health is not None and health.status == HiveNodeStatus.HEALTHY.value
    assert health.reason == "source_ready"
    assert await hive_status_label(storage, SPACE) == "hivemind_healthy"
    assert await resolve_write_route(storage, SPACE) is WriteRoute.STAGED

    business_after = {
        key: value
        for key, value in storage.snapshot().items()
        if key.startswith(f"{SPACE}/") and "/_hivemind/" not in key
    }
    assert business_after == business_before

    writes_before_retry = storage.put_calls
    retried = await _prepare(service)
    assert retried["result"] == "already_ready"
    assert storage.put_calls == writes_before_retry

    invitation = await service.create_invitation(
        SPACE, requested_scopes=(PeerScope.READ.value, PeerScope.COMMIT.value)
    )
    assert invitation["pair_id"].startswith("pair_")
    assert invitation["invitation_bytes"]
    assert invitation["secret"]


@pytest.mark.parametrize("failed_write", range(1, 10))
async def test_fault_after_each_durable_write_resumes_exactly_and_healthy_last(
    monkeypatch: pytest.MonkeyPatch, failed_write: int
) -> None:
    storage = _FailAfterPutStorage()
    business_before = await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    token = (await service.inspect_source_eligibility(SPACE))["state_token"]
    storage.fail_after_put(failed_write)

    with pytest.raises(Exception):
        await service.prepare_source(
            SPACE, expected_state_token=token, quiesced=True
        )

    health = await HivemindStateStore(  # type: ignore[arg-type]
        storage=storage, space_id=SPACE
    ).get_node_status()
    if failed_write <= 7:
        assert health is None or health.status == HiveNodeStatus.UNSAFE.value
    else:
        assert health is not None and health.status == HiveNodeStatus.HEALTHY.value

    # A store I/O ambiguity intentionally poisons the current process. Clearing
    # this process-local sentinel models the required restart before exact resume.
    pairing_store_module._PAIRING_UNSAFE_PREFIXES.clear()
    restarted = _service(storage)
    resumed = await _prepare(restarted)
    assert resumed["result"] in {"prepared", "already_ready"}
    assert resumed["source"]["state"] == "ready"
    final_health = await HivemindStateStore(  # type: ignore[arg-type]
        storage=storage, space_id=SPACE
    ).get_node_status()
    assert final_health is not None
    assert final_health.status == HiveNodeStatus.HEALTHY.value
    business_after = {
        key: value
        for key, value in storage.snapshot().items()
        if key.startswith(f"{SPACE}/") and "/_hivemind/" not in key
    }
    assert business_after == business_before


async def test_interrupted_preparation_keeps_exact_resume_and_refuses_loss_or_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness distinguishes an exact resumable prefix from loss or corruption."""

    storage = _FailAfterPutStorage()
    await _create_product_space(monkeypatch, storage)
    first = _service(storage)
    token = (await first.inspect_source_eligibility(SPACE))["state_token"]
    # The durable intent and its first UNSAFE marker land before an ambiguous
    # timeout. A restarted service must advertise resume, never overwrite it.
    storage.fail_after_put(2)
    with pytest.raises(MeshPairingServiceError) as exc:
        await first.prepare_source(SPACE, expected_state_token=token, quiesced=True)
    assert exc.value.code == "source_prepare_interrupted"

    pairing_store_module._PAIRING_UNSAFE_PREFIXES.clear()
    restarted = _service(storage)
    before_resume = storage.snapshot()
    resumable = await restarted.inspect_source_eligibility(SPACE)
    assert resumable["state"] == "preparing"
    assert resumable["source_initializable"] is True
    assert resumable["resumable"] is True
    assert resumable["can_create_invitation"] is False
    assert storage.snapshot() == before_resume

    # The preparation-specific inventory is a second, larger bounded read. An
    # outage there is retryable availability, not durable-state corruption.
    original_list = storage.list_objects

    async def unavailable_resume_inventory(prefix: str, max_keys: int = 0) -> list[dict]:
        if prefix == layout.HIVEMIND_PREFIX(SPACE) and max_keys > 1:
            raise OSError("preparation inventory temporarily unavailable")
        return await original_list(prefix, max_keys=max_keys)

    storage.list_objects = unavailable_resume_inventory
    before_unavailable = storage.snapshot()
    unavailable = await restarted.inspect_source_eligibility(SPACE)
    assert unavailable["state"] == unavailable["hive_status"] == "unavailable"
    assert unavailable["source_initializable"] is False
    assert unavailable["resumable"] is False
    assert unavailable["can_create_invitation"] is False
    assert storage.snapshot() == before_unavailable
    storage.list_objects = original_list

    # A malformed marker after the durable intent is not resumable. Readiness
    # preserves the evidence and refuses automatic repair or invitation issue.
    storage.objects[layout.node_status_key(SPACE)] = "{"
    before_recovery = storage.snapshot()
    recovery = await restarted.inspect_source_eligibility(SPACE)
    assert recovery["state"] == "prepare_recovery_required"
    assert recovery["source_initializable"] is False
    assert recovery["resumable"] is False
    assert recovery["can_create_invitation"] is False
    assert storage.snapshot() == before_recovery


async def test_identity_change_after_intent_is_recovery_required_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FailAfterPutStorage()
    await _create_product_space(monkeypatch, storage)
    first = _service(storage)
    state_token = (await first.inspect_source_eligibility(SPACE))["state_token"]
    storage.fail_after_put(1)  # PREPARING lands; read-back reports ambiguity.
    with pytest.raises(MeshPairingStoreError):
        await first.prepare_source(
            SPACE, expected_state_token=state_token, quiesced=True
        )

    pairing_store_module._PAIRING_UNSAFE_PREFIXES.clear()
    rotated = _service(storage, private_key=_PRIVATE_B)
    readiness = await rotated.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "prepare_recovery_required"
    before = storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await rotated.prepare_source(
            SPACE,
            expected_state_token=readiness["state_token"],
            quiesced=True,
        )
    assert exc.value.code == "prepare_recovery_required"
    assert storage.snapshot() == before


async def test_prefix_residue_without_intent_refuses_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    await storage.put_json(layout.term_key(SPACE), {"protocol_version": 1, "term": 0})
    service = _service(storage)
    before = storage.snapshot()
    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "unsafe"
    with pytest.raises(MeshPairingServiceError):
        await service.prepare_source(
            SPACE,
            expected_state_token=readiness["state_token"],
            quiesced=True,
        )
    assert storage.snapshot() == before


async def test_stale_direct_sink_is_fenced_after_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    registry = EngineRegistry(storage=storage)
    sink = await registry.resolve_sink(SPACE)

    await _prepare(service)
    with pytest.raises(DirectLocalWriteFenced):
        await sink.put(f"{SPACE}/bank/late.md", "must not bypass staging")
    assert f"{SPACE}/bank/late.md" not in storage.objects


async def test_stale_short_engine_cannot_bypass_route_after_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real SHORT mutation lives in LiveService, not its held sink."""

    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    monkeypatch.setattr(live_module, "get_storage", lambda: storage)
    service = _service(storage)
    register_reservation_checker(service.store.assert_space_not_reserved)

    # Retain the production-shaped engine while the space is still local. Its
    # DirectLocal sink is intentionally inert inside ShortEngine.write_note().
    stale_engine = await EngineRegistry(
        storage=storage,
        live=LiveService(),
    ).short_engine(SPACE)
    await _prepare(service)
    before = storage.snapshot()

    auth = current_token_info.set(
        {
            "client_name": "stale-short-writer",
            "permissions": ["read", "write"],
            "allowed_resources": [],
        }
    )
    try:
        with pytest.raises(DirectLocalWriteFenced):
            await stale_engine.write_note(
                SPACE,
                "observation",
                "must not bypass Project Mesh staging",
            )
    finally:
        current_token_info.reset(auth)

    assert storage.snapshot() == before


async def test_complete_provenance_survives_hivemind_state_loss_and_fences_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared source can never downgrade to a new local write authority."""

    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    monkeypatch.setattr(live_module, "get_storage", lambda: storage)
    service = _service(storage)
    register_reservation_checker(service.store.assert_space_not_reserved)
    register_direct_local_checker(service.store.assert_direct_local_allowed)

    registry = EngineRegistry(storage=storage, live=LiveService())
    stale_sink = await registry.resolve_sink(SPACE)
    stale_short = await registry.short_engine(SPACE)
    await _prepare(service)

    for key in list(storage.objects):
        if key.startswith(f"{SPACE}/_hivemind/"):
            await storage.delete(key)

    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "unsafe"
    assert readiness["can_create_invitation"] is False
    before = storage.snapshot()
    puts_before = storage.put_calls

    auth = current_token_info.set(
        {
            "client_name": "downgrade-writer",
            "permissions": ["read", "write"],
            "allowed_resources": [],
        }
    )
    try:
        with pytest.raises(MeshPairingStoreError) as retained_short:
            await stale_short.write_note(SPACE, "observation", "must stay fenced")
        assert retained_short.value.code == "direct_local_forbidden"

        with pytest.raises(MeshPairingStoreError) as new_short:
            await EngineRegistry(
                storage=storage, live=LiveService()
            ).short_engine(SPACE)
        assert new_short.value.code == "direct_local_forbidden"

        with pytest.raises(MeshPairingStoreError) as retained_sink:
            await stale_sink.put(f"{SPACE}/bank/late.md", "must stay fenced")
        assert retained_sink.value.code == "direct_local_forbidden"
    finally:
        current_token_info.reset(auth)

    assert storage.snapshot() == before
    assert storage.put_calls == puts_before


async def test_complete_provenance_with_lost_hivemind_refuses_normal_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CopyFakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    register_direct_local_checker(service.store.assert_direct_local_allowed)
    await _prepare(service)

    for key in list(storage.objects):
        if key.startswith(f"{SPACE}/_hivemind/"):
            await storage.delete(key)
    backup_id = f"{SPACE}/2026-08-19T12-00-00"
    backup_prefix = f"_backups/{backup_id}/"
    storage.objects[f"{backup_prefix}_meta.json"] = '{"space_id":"meshspace"}'
    storage.objects[f"{backup_prefix}_rules.md"] = "# restored rules"
    monkeypatch.setattr("live_mem.core.backup.get_storage", lambda: storage)

    before = storage.snapshot()
    puts_before = storage.put_calls
    deletes_before = storage.delete_calls
    result = await BackupService().restore(backup_id)

    assert result["status"] == "error"
    assert "irreversible Project Mesh source provenance" in result["message"]
    assert storage.snapshot() == before
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


async def test_complete_provenance_with_lost_hivemind_refuses_normal_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    register_direct_local_checker(service.store.assert_direct_local_allowed)
    await _prepare(service)

    for key in list(storage.objects):
        if key.startswith(f"{SPACE}/_hivemind/"):
            await storage.delete(key)
    before = storage.snapshot()
    puts_before = storage.put_calls
    deletes_before = storage.delete_calls

    result = await SpaceService().delete(SPACE, bootstrap_admin=True)

    assert result["status"] == "error"
    assert "irreversible Project Mesh source provenance" in result["message"]
    assert storage.snapshot() == before
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


async def test_live_note_checks_preparation_only_at_final_mutation_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PreparationReadTrackingStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.get_keys: list[str] = []

        async def get(self, key: str) -> str | None:
            self.get_keys.append(key)
            return await super().get(key)

    storage = PreparationReadTrackingStorage()
    await _create_product_space(monkeypatch, storage)
    monkeypatch.setattr(live_module, "get_storage", lambda: storage)
    service = _service(storage)
    register_reservation_checker(service.store.assert_space_not_reserved)
    register_direct_local_checker(service.store.assert_direct_local_allowed)
    storage.get_keys.clear()

    auth = current_token_info.set(
        {
            "client_name": "hot-path-writer",
            "permissions": ["read", "write"],
            "allowed_resources": [],
        }
    )
    try:
        result = await LiveService().write_note(
            SPACE, "observation", "one bounded preparation check pair"
        )
    finally:
        current_token_info.reset(auth)

    assert result["status"] == "created"
    preparation_reads = [
        key for key in storage.get_keys if key == source_preparation_key(SPACE)
    ]
    # One reservation/provenance read pair remains immediately before PUT;
    # the former early reservation read was redundant and is gone.
    assert len(preparation_reads) == 2


async def test_complete_provenance_allows_idempotent_create_but_not_recreation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    register_direct_local_checker(service.store.assert_direct_local_allowed)
    await _prepare(service)

    existing = await SpaceService().create(
        SPACE,
        "Existing product space",
        "# Rules\nKeep durable source content.",
        owner="operator",
        bootstrap_admin=True,
    )
    assert existing["status"] == "already_exists"

    for key in list(storage.objects):
        if key.startswith(f"{SPACE}/"):
            await storage.delete(key)
    before = storage.snapshot()
    puts_before = storage.put_calls
    with pytest.raises(MeshPairingStoreError) as exc:
        await SpaceService().create(
            SPACE,
            "Recreated product space",
            "# Rules\nMust not be recreated locally.",
            owner="operator",
            bootstrap_admin=True,
        )
    assert exc.value.code == "direct_local_forbidden"
    assert storage.snapshot() == before
    assert storage.put_calls == puts_before


async def test_queue_enqueue_racing_intent_observes_durable_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _BlockingUnsafeStorage()
    await _create_product_space(monkeypatch, storage)
    queue = ConsolidationQueueService()
    service = _service(storage, queue=queue)
    register_reservation_checker(service.store.assert_space_not_reserved)
    readiness = await service.inspect_source_eligibility(SPACE)

    preparing = asyncio.create_task(
        service.prepare_source(
            SPACE,
            expected_state_token=readiness["state_token"],
            quiesced=True,
        )
    )
    await asyncio.wait_for(storage.unsafe_started.wait(), timeout=1)
    intent = await service.store.get_source_preparation(SPACE)
    assert intent is not None
    assert intent.state_enum is SourcePreparationState.PREPARING

    with pytest.raises(MeshPairingStoreError) as exc:
        await asyncio.wait_for(
            queue.enqueue(SPACE, agent="", requested_by="operator"), timeout=1
        )
    assert exc.value.code == "space_reserved"

    storage.release_unsafe.set()
    result = await asyncio.wait_for(preparing, timeout=1)
    assert result["source"]["state"] == "ready"


async def test_held_token_is_not_invitation_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    node = await store.get_node_identity()
    assert node is not None
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=node.node_id,
            term=0,
            fencing_token=0,
            membership_epoch=0,
            bank_version=-1,
        )
    )
    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "mutation_in_progress"
    assert readiness["can_create_invitation"] is False
    with pytest.raises(MeshPairingServiceError) as exc:
        await service.create_invitation(SPACE, requested_scopes=("read",))
    assert exc.value.code == "mutation_in_progress"


@pytest.mark.parametrize("fault", ["unhealthy", "future_free_token"])
async def test_legacy_source_authority_anomalies_are_never_invitable(
    fault: str,
) -> None:
    """Schema-valid legacy state still fails closed when its authority drifts."""

    storage = FakeStorage()
    service = _service(storage)
    await _seed_source(storage, service._config)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    if fault == "unhealthy":
        await state_store.set_node_status(
            NodeHealth(status=HiveNodeStatus.UNSAFE, reason="operator repair required")
        )
    else:
        # FREE release baselines may lag the head, but they cannot claim a
        # future term. Persisting one must not make a legacy source invitable.
        await state_store.set_token(
            TokenLeaseState(
                state=TokenState.FREE,
                holder_node_id=None,
                term=3,
                fencing_token=0,
                membership_epoch=1,
                bank_version=1,
            )
        )

    before = storage.snapshot()
    readiness = await service.inspect_source_eligibility(SPACE)

    assert readiness["state"] == readiness["hive_status"] == "unsafe"
    assert readiness["source_ready"] is False
    assert readiness["source_initializable"] is False
    assert readiness["can_create_invitation"] is False
    assert readiness["resumable"] is False
    assert storage.snapshot() == before


async def _projected_preparation_payload(service: MeshPairingService) -> bytes:
    readiness = await service.inspect_source_eligibility(SPACE)
    now_ms = NOW_MS
    started_at_iso = datetime.fromtimestamp(
        now_ms / 1000, tz=timezone.utc
    ).isoformat(timespec="milliseconds")
    intent = SourcePreparationIntent(
        preparation_id="prep_" + "a" * 32,
        protocol_version=1,
        state=SourcePreparationState.PREPARING.value,
        space_id=SPACE,
        source_fingerprint=service._config.fingerprint,
        membership_public_key=_legacy_membership_key(service._config.public_key),
        node_id=_node_id_from_fingerprint(service._config.fingerprint),
        display_name=service._config.display_name,
        public_url=service._config.public_url,
        started_at_ms=now_ms,
        started_at_iso=started_at_iso,
        completed_at_ms=0,
        expected_state_token=readiness["state_token"],
    )
    expected = service._source_genesis_models(intent)
    snapshot = await service._bootstrap().project_source_preparation_snapshot(
        SPACE,
        node=expected["node"],
        membership=expected["membership"],
        term=expected["term"],
        token=expected["token"],
        pointer=expected["pointer"],
    )
    return serialize_snapshot(snapshot)


async def test_prepare_preflight_object_limit_is_zero_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage, max_objects=7)
    readiness = await service.inspect_source_eligibility(SPACE)
    before = storage.snapshot()
    puts_before = storage.put_calls

    with pytest.raises(MeshPairingServiceError) as exc:
        await service.prepare_source(
            SPACE,
            expected_state_token=readiness["state_token"],
            quiesced=True,
        )
    assert exc.value.code == "bootstrap_limit_exceeded"
    assert storage.snapshot() == before
    assert storage.put_calls == puts_before
    assert await service.store.get_source_preparation(SPACE) is None


async def test_prepare_preflight_byte_limit_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    baseline = _service(storage)
    projected = await _projected_preparation_payload(baseline)
    projected_body = json.loads(projected)
    assert projected_body["manifest"]["created_at"].endswith(
        ".000000+00:00"
    )
    exact_second_body = dict(projected_body)
    exact_second_body["manifest"] = dict(projected_body["manifest"])
    exact_second_body["manifest"]["created_at"] = (
        "2000-01-01T00:00:00+00:00"
    )
    exact_second_size = len(
        json.dumps(
            exact_second_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert exact_second_size == len(projected) - 7

    # A projection taken on an exact second used to omit seven bytes and could
    # persist PREPARING before the later fractional manifest overflowed. The
    # fixed-width projection now rejects that old short boundary with zero write.
    too_small = _service(storage, max_bytes=exact_second_size)
    readiness = await too_small.inspect_source_eligibility(SPACE)
    before = storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await too_small.prepare_source(
            SPACE,
            expected_state_token=readiness["state_token"],
            quiesced=True,
        )
    assert exc.value.code == "bootstrap_limit_exceeded"
    assert storage.snapshot() == before

    exact = _service(storage, max_bytes=len(projected))
    result = await _prepare(exact)
    assert result["result"] == "prepared"
    assert result["source"]["state"] == "ready"


@pytest.mark.parametrize("bad_size", [None, -1, "12"])
async def test_bounded_export_rejects_invalid_size_before_get(bad_size) -> None:
    class BadSizeStorage(FakeStorage):
        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            objects = await super().list_objects(prefix, max_keys=max_keys)
            for obj in objects:
                if obj["Key"] == f"{SPACE}/_meta.json":
                    if bad_size is None:
                        obj.pop("Size", None)
                    else:
                        obj["Size"] = bad_size
            return objects

    storage = BadSizeStorage()
    storage.objects[f"{SPACE}/_meta.json"] = '{"space_id":"meshspace"}'
    with pytest.raises(BootstrapLimitError):
        await BootstrapService(storage)._collect_shared_export_files(
            SPACE, max_objects=8, max_bytes=1024
        )
    assert storage.get_calls == 0


async def test_readiness_rejects_oversize_members_before_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadTrackingStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.get_keys: list[str] = []

        async def get(self, key: str) -> str | None:
            self.get_keys.append(key)
            return await super().get(key)

    storage = ReadTrackingStorage()
    await _create_product_space(monkeypatch, storage)
    members_key = layout.members_key(SPACE)
    storage.objects[members_key] = "x" * 262_145
    storage.get_keys.clear()

    readiness = await _service(storage).inspect_source_eligibility(SPACE)
    assert readiness["state"] == "unsafe"
    assert members_key not in storage.get_keys


@pytest.mark.parametrize(
    ("key", "payload"),
    [
        (f"{SPACE}/_meta.json", "x" * 262_145),
        (f"{SPACE}/_rules.md", "x" * 262_145),
        (f"{SPACE}/live/.keep", "not-empty"),
        (source_preparation_key(SPACE), "x" * 65_537),
    ],
)
async def test_readiness_bounds_product_and_preparation_before_get(
    monkeypatch: pytest.MonkeyPatch, key: str, payload: str
) -> None:
    class ReadTrackingStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.get_keys: list[str] = []

        async def get(self, item_key: str) -> str | None:
            self.get_keys.append(item_key)
            return await super().get(item_key)

    storage = ReadTrackingStorage()
    await _create_product_space(monkeypatch, storage)
    storage.objects[key] = payload
    storage.get_keys.clear()

    readiness = await _service(storage).inspect_source_eligibility(SPACE)

    assert readiness["state"] == "unsafe"
    assert key not in storage.get_keys


@pytest.mark.parametrize("bad_size", [None, 65_537])
async def test_readiness_bounds_selected_commit_before_get(
    monkeypatch: pytest.MonkeyPatch, bad_size
) -> None:
    class CommitSizeStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.commit_key = ""
            self.bad_size = False
            self.get_keys: list[str] = []

        async def get(self, key: str) -> str | None:
            self.get_keys.append(key)
            return await super().get(key)

        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            objects = await super().list_objects(prefix, max_keys=max_keys)
            if self.bad_size and prefix == self.commit_key:
                for obj in objects:
                    if bad_size is None:
                        obj.pop("Size", None)
                    else:
                        obj["Size"] = bad_size
            return objects

    storage = CommitSizeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    assert membership is not None
    commit = BankCommit(
        bank_version=0,
        parent_bank_version=-1,
        term=0,
        commit_id="commit-0",
        committed_by_node_id=membership.members[0].node_id,
    )
    await state_store.append_commit(commit)
    await state_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id=commit.commit_id)
    )
    storage.commit_key = layout.commit_key(SPACE, 0)
    storage.bad_size = True
    storage.get_keys.clear()

    readiness = await service.inspect_source_eligibility(SPACE)

    assert readiness["state"] == "unsafe"
    assert storage.commit_key not in storage.get_keys


async def test_corrupt_selected_commit_returns_stable_bounded_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    assert membership is not None
    commit = BankCommit(
        bank_version=0,
        parent_bank_version=-1,
        term=0,
        commit_id="commit-corrupt",
        committed_by_node_id=membership.members[0].node_id,
    )
    await state_store.append_commit(commit)
    await state_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id=commit.commit_id)
    )
    marker = "CORRUPT_SELECTED_COMMIT_MUST_NOT_LEAK"
    storage.objects[layout.commit_key(SPACE, 0)] = marker + "{"  # invalid JSON
    before = storage.snapshot()

    first = await service.inspect_source_eligibility(SPACE)
    second = await service.inspect_source_eligibility(SPACE)

    for readiness in (first, second):
        assert readiness["state"] == "unsafe"
        assert readiness["source_ready"] is False
        assert readiness["source_initializable"] is False
        assert readiness["can_create_invitation"] is False
        assert readiness["resumable"] is False
        assert len(readiness["state_token"]) == 64
        assert marker not in str(readiness)
    assert first["state_token"] == second["state_token"]
    assert storage.snapshot() == before


async def test_preparation_progress_rejects_non_exact_durable_prefixes() -> None:
    """Only a contiguous, exact preparation prefix is resumable."""

    storage = FakeStorage()
    service = _service(storage)
    started_at_iso = datetime.fromtimestamp(
        NOW_MS / 1000, tz=timezone.utc
    ).isoformat(timespec="milliseconds")
    intent = SourcePreparationIntent(
        preparation_id="prep_" + "b" * 32,
        protocol_version=1,
        state=SourcePreparationState.PREPARING.value,
        space_id=SPACE,
        source_fingerprint=service._config.fingerprint,
        membership_public_key=_legacy_membership_key(service._config.public_key),
        node_id=_node_id_from_fingerprint(service._config.fingerprint),
        display_name=service._config.display_name,
        public_url=service._config.public_url,
        started_at_ms=NOW_MS,
        started_at_iso=started_at_iso,
        completed_at_ms=0,
        expected_state_token="0" * 64,
    )
    status_key = layout.node_status_key(SPACE)

    for objects in (
        [{"Key": f"{SPACE}/_hivemind/unexpected-{index}"} for index in range(7)],
        [{"Key": f"{SPACE}/_hivemind/unexpected"}],
        [{"Key": layout.node_key(SPACE)}],
        [{"Key": status_key}],
    ):
        exact, phase, _ = await service._preparation_progress(
            SPACE, intent, objects=objects
        )
        assert (exact, phase) == (False, "conflict")

    storage.objects[status_key] = "{invalid"
    exact, phase, _ = await service._preparation_progress(
        SPACE, intent, objects=[{"Key": status_key}]
    )
    assert (exact, phase) == (False, "conflict")

    # Even a structurally valid HEALTHY marker is unsafe until every preceding
    # genesis record is present and exact.
    expected = service._source_genesis_models(intent)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await state_store.set_node_status(
        expected["unsafe"].model_copy(update={"reason": "unexpected"})
    )
    exact, phase, _ = await service._preparation_progress(
        SPACE, intent, objects=[{"Key": status_key}]
    )
    assert (exact, phase) == (False, "conflict")

    await state_store.set_node_status(expected["unsafe"])
    await state_store.set_node_identity(
        expected["node"].model_copy(update={"display_name": "unexpected"})
    )
    exact, phase, _ = await service._preparation_progress(
        SPACE,
        intent,
        objects=[{"Key": status_key}, {"Key": layout.node_key(SPACE)}],
    )
    assert (exact, phase) == (False, "conflict")

    await state_store.set_node_status(expected["healthy"])
    exact, phase, _ = await service._preparation_progress(
        SPACE, intent, objects=[{"Key": status_key}]
    )
    assert (exact, phase) == (False, "conflict")


def test_source_projection_normalizes_malformed_queue_state() -> None:
    service = _service(FakeStorage())

    projection = service._source_projection(
        space_id=SPACE,
        observation="test",
        lane={
            "running_job": {"job_id": "running"},
            "queued_job_ids": "not-a-list",
            "queued_count": 1,
        },
    )

    assert len(projection["lane_digest"]) == 64
    direct = service._source_projection(
        space_id=SPACE,
        observation="test",
        lane={"running_job_id": "direct", "queued_job_ids": ["queued"]},
    )
    assert projection["lane_digest"] != direct["lane_digest"]


async def test_absent_space_is_never_offered_as_a_mesh_source() -> None:
    readiness = await _service(FakeStorage()).inspect_source_eligibility("absent-space")

    assert readiness["state"] == "not_a_space"
    assert readiness["source_initializable"] is False
    assert readiness["can_create_invitation"] is False


async def test_source_with_resync_marker_is_not_invitable(
) -> None:
    storage = FakeStorage()
    service = _service(storage)
    await _seed_source(storage, service._config)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await state_store.set_node_status(
        NodeHealth(
            status=HiveNodeStatus.RESYNC_REQUIRED,
            reason="source resync required",
        )
    )

    readiness = await service.inspect_source_eligibility(SPACE)

    assert readiness["state"] == "resync_required"
    assert readiness["can_create_invitation"] is False


async def test_ready_source_rejects_mismatched_commit_and_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    assert membership is not None
    commit = BankCommit(
        bank_version=0,
        parent_bank_version=-1,
        term=0,
        commit_id="actual-commit",
        committed_by_node_id=membership.members[0].node_id,
    )
    await state_store.append_commit(commit)
    await state_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id="different-commit")
    )

    mismatch = await service.inspect_source_eligibility(SPACE)
    assert mismatch["state"] == "unsafe"

    await state_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id=commit.commit_id)
    )

    async def conflicting_reservation(_space_id: str) -> str:
        return "pair_" + "a" * 32

    service.store.get_reservation = conflicting_reservation
    reserved = await service.inspect_source_eligibility(SPACE)
    assert reserved["state"] == "pairing_in_flight"
    assert reserved["source_ready"] is True


async def test_status_readiness_inventory_uses_bounded_parallelism() -> None:
    class ConcurrencyTrackingStorage(FakeStorage):
        def __init__(self, spaces: list[str]) -> None:
            super().__init__()
            self.spaces = spaces
            self.active: dict[str, int] = {}
            self.max_distinct_spaces = 0

        async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
            space_id = next(
                (item for item in self.spaces if prefix.startswith(f"{item}/")),
                None,
            )
            if space_id is None:
                return await super().list_objects(prefix, max_keys=max_keys)
            self.active[space_id] = self.active.get(space_id, 0) + 1
            self.max_distinct_spaces = max(
                self.max_distinct_spaces, len(self.active)
            )
            try:
                await asyncio.sleep(0.002)
                return await super().list_objects(prefix, max_keys=max_keys)
            finally:
                self.active[space_id] -= 1
                if self.active[space_id] == 0:
                    self.active.pop(space_id)

    spaces = [f"parallel-{index:02d}" for index in range(12)]
    storage = ConcurrencyTrackingStorage(spaces)
    for space_id in spaces:
        storage.objects[f"{space_id}/_meta.json"] = (
            '{"space_id":"' + space_id + '"}'
        )
        storage.objects[f"{space_id}/_rules.md"] = "# rules"
        storage.objects[f"{space_id}/live/.keep"] = ""
        storage.objects[f"{space_id}/bank/.keep"] = ""

    readiness = await _service(storage).list_source_eligibility()

    assert [item["space_id"] for item in readiness] == spaces
    assert all(item["state"] == "local_only_can_prepare" for item in readiness)
    assert 1 < storage.max_distinct_spaces <= 8


async def test_invitation_preflight_is_serialized_with_token_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    node = await store.get_node_identity()
    assert node is not None

    lock = token_mutation_lock(SPACE)
    await lock.acquire()
    creating = asyncio.create_task(
        service.create_invitation(SPACE, requested_scopes=("read",))
    )
    try:
        await asyncio.sleep(0)
        assert not creating.done()
        await store.set_token(
            TokenLeaseState(
                state=TokenState.HELD,
                holder_node_id=node.node_id,
                term=0,
                fencing_token=0,
                membership_epoch=0,
                bank_version=-1,
            )
        )
        after_token = storage.snapshot()
    finally:
        lock.release()

    with pytest.raises(MeshPairingServiceError) as exc:
        await creating
    assert exc.value.code == "mutation_in_progress"
    assert await service.store.list_sessions() == []
    assert storage.snapshot() == after_token


async def test_complete_binding_endpoint_drift_changes_token_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    original = _service(storage, public_url="https://source-a.mesh.test")
    await _prepare(original)
    before = await original.inspect_source_eligibility(SPACE)
    snapshot = storage.snapshot()

    drifted = _service(storage, public_url="https://source-b.mesh.test")
    after = await drifted.inspect_source_eligibility(SPACE)
    assert after["state"] == "identity_mismatch"
    assert after["can_create_invitation"] is False
    assert after["state_token"] != before["state_token"]
    with pytest.raises(MeshPairingServiceError) as exc:
        await drifted.create_invitation(SPACE, requested_scopes=("read",))
    assert exc.value.code == "identity_mismatch"
    assert storage.snapshot() == snapshot


@pytest.mark.parametrize(
    "critical_key",
    [
        layout.node_status_key(SPACE),
        layout.token_key(SPACE),
        layout.bank_version_key(SPACE),
    ],
)
async def test_complete_provenance_requires_full_critical_baseline(
    monkeypatch: pytest.MonkeyPatch, critical_key: str
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    await storage.delete(critical_key)
    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "unsafe"
    assert readiness["can_create_invitation"] is False


async def test_provenance_never_becomes_not_a_space_after_prefix_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    for key in list(storage.objects):
        if key.startswith(f"{SPACE}/"):
            await storage.delete(key)
    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "unsafe"
    assert readiness["source_initializable"] is False


async def test_light_readiness_skips_corrupt_history_but_action_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    for index in range(129):
        await storage.put(
            f"{SPACE}/_hivemind/events/{index:04d}.json", "not-json"
        )

    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "ready"
    assert len(readiness["state_token"]) == 64
    listed = await service.list_source_eligibility()
    assert next(item for item in listed if item["space_id"] == SPACE)["state"] == "ready"
    before = storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await service.create_invitation(SPACE, requested_scopes=("read",))
    assert exc.value.code == "source_unhealthy"
    assert storage.snapshot() == before


async def test_free_token_may_lag_committed_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    await _create_product_space(monkeypatch, storage)
    service = _service(storage)
    await _prepare(service)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await store.get_membership()
    assert membership is not None
    await store.set_membership(
        MembershipView(epoch=2, members=membership.members)
    )
    await store.bump_term(2, updated_by_node_id=membership.members[0].node_id)
    await store.append_commit(
        BankCommit(
            bank_version=0,
            parent_bank_version=-1,
            term=2,
            commit_id="commit-0",
            committed_by_node_id=membership.members[0].node_id,
        )
    )
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id="commit-0")
    )
    readiness = await service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "ready"


async def test_source_listing_allows_128_spaces_plus_system_prefixes_and_refuses_129() -> None:
    storage = FakeStorage()
    await storage.put("_system/state.json", "{}")
    await storage.put("_backups/archive.json", "{}")
    for index in range(128):
        await storage.put(f"space{index:03d}/marker", "x")
    service = _service(storage)

    async def inspected(space_id: str):
        return {"space_id": space_id, "state": "ready"}

    service._inspect_source_eligibility = inspected
    listed = await service.list_source_eligibility()
    assert len(listed) == 128

    await storage.put("overflow/marker", "x")
    with pytest.raises(MeshPairingServiceError) as exc:
        await service.list_source_eligibility()
    assert exc.value.code == "mesh_status_inventory_too_large"


async def test_target_teardown_rejects_internal_id_and_oversize_before_delete() -> None:
    storage = FakeStorage()
    service = _service(storage, max_objects=2)
    await storage.put("_system/critical.json", "keep")
    before = storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await service._teardown_target_space("_system")
    assert exc.value.code == "invalid_space_id"
    assert storage.snapshot() == before
    assert storage.delete_calls == 0

    for index in range(8):
        await storage.put(f"polluted/item-{index}.json", "x")
    before = storage.snapshot()
    with pytest.raises(MeshPairingServiceError) as exc:
        await service._teardown_target_space("polluted")
    assert exc.value.code == "resync_inventory_too_large"
    assert storage.snapshot() == before
    assert storage.delete_calls == 0
