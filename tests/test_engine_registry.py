# -*- coding: utf-8 -*-
"""
Tests for P3-7 (issue #56) — EngineRegistry construction + the per-space
WriteSink routing seam (resolve_sink).

Deterministic and offline: backed by ``WriteSinkFakeStorage`` (the shared
``FakeStorage`` + ``delete_many``, from ``tests.test_write_sink``) plus tiny DI
fakes for live/consolidator/queue/bridge. No real S3 / boto3 / AsyncOpenAI /
network / LLM is ever constructed.

What is verified (the P3-7 contract):
- EngineRegistry builds from injected fakes with NO real client.
- resolve_sink maps the 3-valued WriteRoute verdict:
    non-Hivemind        -> DirectLocalWriteSink (the ONLY direct-local path);
    Hivemind-healthy     -> StagedHivemindWriteSink (resolve succeeds, refuses
                            at write time);
    UNSAFE / RESYNC      -> RegistryRefused (never a sink, never direct-local);
    corrupt critical file-> CorruptedStateError PROPAGATES (never caught, never
                            downgraded to DIRECT_LOCAL).
- short_engine / mid_engine resolve the per-space sink and are NOT cached across
  spaces; long_engine is cached and takes no sink; hive_engine is read-only.
- get_engine_registry() returns a process-wide singleton.
- ``import live_mem.core.engines`` is import-cycle clean (smoke).
"""

from __future__ import annotations

import importlib

import pytest

from live_mem.core.engines import (
    EngineRegistry,
    RegistryRefused,
    get_engine_registry,
)
from live_mem.core.engines.hive import HiveEngine
from live_mem.core.engines.long_engine import LongEngine
from live_mem.core.engines.mid import MidEngine
from live_mem.core.engines.short import ShortEngine
from datetime import datetime, timedelta, timezone

from live_mem.core.hivemind import (
    BankVersionPointer,
    CorruptedStateError,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    TokenLeaseState,
    TokenState,
    generate_peer_keypair,
    layout,
)
from live_mem.core.write_sink import (
    DirectLocalWriteSink,
    StagedHivemindWriteSink,
)
from tests.test_write_sink import WriteSinkFakeStorage


# =============================================================================
# DI fakes — held verbatim by the engines; never exercised here (we only test
# construction + routing, not delegation, which the engine suites already cover).
# =============================================================================


class _FakeLive:
    pass


class _FakeConsolidator:
    pass


class _FakeQueue:
    pass


class _FakeBridge:
    pass


def _registry(storage: WriteSinkFakeStorage) -> EngineRegistry:
    """A fully DI-constructed registry (no get_* singleton, no real client)."""
    return EngineRegistry(
        storage=storage,
        live=_FakeLive(),
        consolidator=_FakeConsolidator(),
        queue=_FakeQueue(),
        bridge=_FakeBridge(),
    )


async def _seed_healthy_hive(storage: WriteSinkFakeStorage, space_id: str) -> None:
    """Hivemind structurellement complet et sain — node.json + 1 membre ACTIVE
    portant une vraie clé Ed25519 (mirror of test_hivemind_routing).

    P5-8 (#16): also seeds a HELD token + term + bank pointer so the STAGED
    branch is fully serviceable (the capstone resolve_sink fail-closes on a hive
    with no HELD token — see test_resolve_sink_healthy_hive_no_held_token_*)."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(epoch=1, members=[Member(node_id="n1", public_key=keys.public_key)])
    )
    await store.bump_term(1, updated_by_node_id="n1")
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="n1",
            term=1,
            fencing_token=1,
            granted_at=now.isoformat(),
            lease_until=(now + timedelta(seconds=300)).isoformat(),
            membership_epoch=1,
            event_id="evt-seed",
        )
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=-1))


@pytest.fixture
def storage() -> WriteSinkFakeStorage:
    return WriteSinkFakeStorage()


# =============================================================================
# Construction — no real client at import / construction
# =============================================================================


def test_registry_constructs_with_di_fakes_no_real_client(
    storage: WriteSinkFakeStorage,
) -> None:
    reg = _registry(storage)
    assert isinstance(reg, EngineRegistry)
    # Injected storage is the SAME object the routing seam will read.
    assert reg._storage_dep() is storage


def test_import_engines_is_cycle_clean() -> None:
    """Smoke: importing the engines package must not pull tools/server or build
    a client (import-cycle / lazy-construction guard)."""
    mod = importlib.import_module("live_mem.core.engines")
    assert hasattr(mod, "EngineRegistry")
    assert hasattr(mod, "get_engine_registry")


# =============================================================================
# resolve_sink — the single WriteRoute -> WriteSink seam
# =============================================================================


async def test_resolve_sink_non_hivemind_returns_direct_local(
    storage: WriteSinkFakeStorage,
) -> None:
    """A blank (non-Hivemind) space -> DirectLocalWriteSink wrapping the SAME
    injected storage (deterministic, byte-for-byte legacy path)."""
    reg = _registry(storage)
    sink = await reg.resolve_sink("space-a")
    assert isinstance(sink, DirectLocalWriteSink)
    assert sink.storage is storage


async def test_resolve_sink_healthy_hive_returns_staged_stub(
    storage: WriteSinkFakeStorage,
) -> None:
    """A HEALTHY hive WITH a HELD token -> real StagedHivemindWriteSink. Resolve
    SUCCEEDS; the sink buffers writes and drives an atomic commit (P5-8 #16)."""
    await _seed_healthy_hive(storage, "hive-a")
    reg = _registry(storage)
    sink = await reg.resolve_sink("hive-a")
    assert isinstance(sink, StagedHivemindWriteSink)
    assert sink.space_id == "hive-a"


async def test_resolve_sink_healthy_hive_no_held_token_fails_closed(
    storage: WriteSinkFakeStorage,
) -> None:
    """P5-8 (#16) FAIL-CLOSED deferral of the lease ACQUISITION ceremony: a
    HEALTHY hive with NO HELD token -> RegistryRefused (never a sink, never a
    direct write). The capstone sink assumes a pre-established holder;
    assert_commit_allowed is the sole verifier. Without the token there is no
    holder to commit, so resolve fails closed rather than returning a sink that
    could not commit."""
    # node + ACTIVE member only (HEALTHY route) but NO token seeded.
    store = HivemindStateStore(storage=storage, space_id="hive-nt")  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1, members=[Member(node_id="n1", public_key=keys.public_key)]
        )
    )
    reg = _registry(storage)
    before = storage.snapshot()
    with pytest.raises(RegistryRefused) as exc:
        await reg.resolve_sink("hive-nt")
    assert exc.value.space_id == "hive-nt"
    # No write occurred while fail-closing.
    assert storage.objects == before


async def test_resolve_sink_unsafe_hive_raises_registry_refused(
    storage: WriteSinkFakeStorage,
) -> None:
    """An explicit UNSAFE marker -> RegistryRefused (never returns a sink)."""
    store = HivemindStateStore(storage=storage, space_id="hive-u")  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="crash mid-import")
    )
    reg = _registry(storage)
    with pytest.raises(RegistryRefused) as exc:
        await reg.resolve_sink("hive-u")
    assert exc.value.space_id == "hive-u"


async def test_resolve_sink_resync_required_raises_registry_refused(
    storage: WriteSinkFakeStorage,
) -> None:
    """RESYNC_REQUIRED on an otherwise-complete hive -> RegistryRefused."""
    await _seed_healthy_hive(storage, "hive-r")
    store = HivemindStateStore(storage=storage, space_id="hive-r")  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(
            status=HiveNodeStatus.RESYNC_REQUIRED,
            reason="epoch futur",
            observed_epoch=5,
        )
    )
    reg = _registry(storage)
    with pytest.raises(RegistryRefused):
        await reg.resolve_sink("hive-r")


async def test_resolve_sink_corrupted_propagates_corrupted_state_error(
    storage: WriteSinkFakeStorage,
) -> None:
    """LOAD-BEARING fail-closed: corrupt node.json -> CorruptedStateError
    PROPAGATES through resolve_sink. It is NEVER caught and NEVER downgraded to
    DIRECT_LOCAL (split-brain guard)."""
    storage.objects[layout.node_key("hive-c")] = "{not valid json"
    reg = _registry(storage)
    with pytest.raises(CorruptedStateError):
        await reg.resolve_sink("hive-c")

    # Defense: no path silently returns a DirectLocalWriteSink for a corrupt space.
    try:
        sink = await reg.resolve_sink("hive-c")
    except CorruptedStateError:
        sink = None
    assert not isinstance(sink, DirectLocalWriteSink)


# =============================================================================
# Per-space engines — sink resolved per call, not cached across spaces
# =============================================================================


async def test_short_engine_resolves_sink_per_space(
    storage: WriteSinkFakeStorage,
) -> None:
    """short_engine returns a ShortEngine carrying the route-correct sink; two
    different spaces yield distinct sinks (not cached)."""
    await _seed_healthy_hive(storage, "hive-a")  # STAGED route
    reg = _registry(storage)

    direct = await reg.short_engine("space-a")  # non-Hivemind -> DirectLocal
    staged = await reg.short_engine("hive-a")  # healthy hive -> Staged

    assert isinstance(direct, ShortEngine)
    assert isinstance(direct.write_sink, DirectLocalWriteSink)
    assert isinstance(staged.write_sink, StagedHivemindWriteSink)
    # The injected live is held verbatim.
    assert direct._live is reg._live
    # Distinct engine instances per call (not cached).
    again = await reg.short_engine("space-a")
    assert again is not direct


async def test_mid_engine_resolves_sink_per_space(
    storage: WriteSinkFakeStorage,
) -> None:
    await _seed_healthy_hive(storage, "hive-a")
    reg = _registry(storage)

    direct = await reg.mid_engine("space-a")
    staged = await reg.mid_engine("hive-a")

    assert isinstance(direct, MidEngine)
    assert isinstance(direct.write_sink, DirectLocalWriteSink)
    assert isinstance(staged.write_sink, StagedHivemindWriteSink)
    # Only the registry-issued DirectLocal engine receives a space-bound
    # compaction authority; a sink's runtime type alone is never proof.
    assert direct._direct_local_compaction_authority is not None
    assert staged._direct_local_compaction_authority is None
    # consolidator + queue held verbatim from DI.
    assert direct._consolidator is reg._consolidator
    assert direct._queue is reg._queue


async def test_mid_engine_unsafe_space_raises_before_construction(
    storage: WriteSinkFakeStorage,
) -> None:
    """mid_engine on an UNSAFE space fails closed (RegistryRefused) at the
    resolve gate — before any MidEngine / consolidator write path runs."""
    store = HivemindStateStore(storage=storage, space_id="hive-u")  # type: ignore[arg-type]
    await store.set_node_status(NodeHealth(status=HiveNodeStatus.UNSAFE))
    reg = _registry(storage)
    with pytest.raises(RegistryRefused):
        await reg.mid_engine("hive-u")


def test_long_engine_is_cached_and_takes_no_sink(
    storage: WriteSinkFakeStorage,
) -> None:
    reg = _registry(storage)
    a = reg.long_engine()
    b = reg.long_engine()
    assert isinstance(a, LongEngine)
    assert a is b  # cached
    assert not hasattr(a, "write_sink")  # LongEngine takes no WriteSink
    assert a.bridge is reg._bridge


def test_hive_engine_builds_readonly_store_per_space(
    storage: WriteSinkFakeStorage,
) -> None:
    reg = _registry(storage)
    eng = reg.hive_engine("hive-a")
    assert isinstance(eng, HiveEngine)
    assert eng.space_id == "hive-a"
    # No WriteSink on the read-only surface.
    assert not hasattr(eng, "write_sink")


# =============================================================================
# Singleton
# =============================================================================


def test_get_engine_registry_returns_singleton() -> None:
    assert get_engine_registry() is get_engine_registry()
    assert isinstance(get_engine_registry(), EngineRegistry)
