# -*- coding: utf-8 -*-
"""Reservation write-guard tests (P10-3, issue #191).

The guard blocks ordinary writes to a space reserved for a Project Mesh pairing.
A blank/reserved target routes DIRECT_LOCAL, so the guard is threaded into the
ordinary-write entrypoints and is a zero-cost no-op when Mesh is disabled.
"""

from __future__ import annotations

import pytest

from live_mem.core import reservation_guard
from live_mem.core.reservation_guard import (
    PairingActivationError,
    SpaceReservedError,
    assert_no_pairing_activation,
    assert_space_not_reserved,
    clear_pairing_activation_checker,
    clear_reservation_checker,
    has_reservation_checker,
    register_pairing_activation_checker,
    register_reservation_checker,
)


@pytest.fixture(autouse=True)
def _clean_checker():
    clear_reservation_checker()
    clear_pairing_activation_checker()
    yield
    clear_reservation_checker()
    clear_pairing_activation_checker()


async def test_no_op_when_no_checker_registered() -> None:
    assert has_reservation_checker() is False
    # Zero-cost no-op: never raises, no I/O, for any space (Mesh disabled).
    await assert_space_not_reserved("any-space")


async def test_registered_checker_refuses_reserved_space() -> None:
    async def checker(space_id: str) -> None:
        if space_id == "reserved":
            raise SpaceReservedError(space_id)

    register_reservation_checker(checker)
    assert has_reservation_checker() is True
    await assert_space_not_reserved("other")  # unrelated space unaffected
    with pytest.raises(SpaceReservedError):
        await assert_space_not_reserved("reserved")


async def test_clear_restores_no_op() -> None:
    async def checker(space_id: str) -> None:
        raise SpaceReservedError(space_id)

    register_reservation_checker(checker)
    clear_reservation_checker()
    await assert_space_not_reserved("anything")  # back to no-op


async def test_guard_blocks_a_real_write_entrypoint(monkeypatch) -> None:
    # Prove the guard is actually wired into an ordinary-write entrypoint: with a
    # checker registered, resolving a write sink for the reserved space refuses.
    from live_mem.core.engines import EngineRegistry

    async def checker(space_id: str) -> None:
        if space_id == "reserved-blank":
            raise SpaceReservedError(space_id)

    register_reservation_checker(checker)

    registry = EngineRegistry()
    with pytest.raises(SpaceReservedError):
        await registry.resolve_sink("reserved-blank")


async def test_mesh_pairing_store_backs_a_checker() -> None:
    # The MeshPairingStore.assert_space_not_reserved is the intended checker
    # backend: a reserved space is refused, an unreserved one passes.
    import secrets

    from live_mem.mesh.pairing_store import MeshPairingStore, MeshPairingStoreError

    class InMem:
        def __init__(self):
            self.o = {}

        async def put(self, k, c, content_type=""):
            self.o[k] = c

        async def get(self, k):
            return self.o.get(k)

        async def delete(self, k):
            self.o.pop(k, None)

        async def list_objects(self, prefix, max_keys=0):
            return [{"Key": k} for k in self.o if k.startswith(prefix)]

    store = MeshPairingStore(InMem(), prefix=f"_system/mesh_pairing/hm1:{secrets.token_hex(32)}/")
    await store.reserve("alpha", "pair_" + "a" * 32, now_ms=1)

    register_reservation_checker(store.assert_space_not_reserved)
    await assert_space_not_reserved("beta")  # not reserved
    with pytest.raises(MeshPairingStoreError):
        await assert_space_not_reserved("alpha")  # reserved


# ---------------------------------------------------------------------------
# Pairing-activation fence: a SECOND, independent registered-checker slot that
# blocks operator epoch-advancing membership mutations while a SOURCE pairing is
# mid-activation. Same no-op-when-unregistered discipline as the write guard.
# ---------------------------------------------------------------------------


async def test_activation_fence_no_op_when_no_checker_registered() -> None:
    # Zero-cost no-op for any space when Mesh is disabled (no checker).
    await assert_no_pairing_activation("any-space")


async def test_activation_fence_registered_checker_refuses_mid_activation() -> None:
    async def checker(space_id: str, ignore_pair_id) -> None:
        if space_id == "activating":
            raise PairingActivationError(space_id)

    register_pairing_activation_checker(checker)
    await assert_no_pairing_activation("idle")  # unrelated space unaffected
    with pytest.raises(PairingActivationError):
        await assert_no_pairing_activation("activating")


async def test_activation_fence_forwards_pairing_scoped_bypass() -> None:
    # The pairing-scoped bypass id is threaded verbatim to the checker so a
    # pairing driving its OWN transition is exempt while others are refused.
    seen: list = []

    async def checker(space_id: str, ignore_pair_id) -> None:
        seen.append((space_id, ignore_pair_id))
        if ignore_pair_id != "mypair":
            raise PairingActivationError(space_id)

    register_pairing_activation_checker(checker)
    await assert_no_pairing_activation("s", ignore_pair_id="mypair")  # own -> ok
    with pytest.raises(PairingActivationError):
        await assert_no_pairing_activation("s", ignore_pair_id="otherpair")
    with pytest.raises(PairingActivationError):
        await assert_no_pairing_activation("s")  # operator (None) -> fenced
    assert seen == [("s", "mypair"), ("s", "otherpair"), ("s", None)]


async def test_activation_fence_is_independent_of_reservation_checker() -> None:
    # The two registered-checker slots are separate: clearing one leaves the other.
    async def reserve_checker(space_id: str) -> None:
        raise SpaceReservedError(space_id)

    async def activation_checker(space_id: str, ignore_pair_id) -> None:
        raise PairingActivationError(space_id)

    register_reservation_checker(reserve_checker)
    register_pairing_activation_checker(activation_checker)
    clear_reservation_checker()
    # Reservation cleared -> write guard is a no-op again, but the activation
    # fence is still armed.
    await assert_space_not_reserved("x")
    with pytest.raises(PairingActivationError):
        await assert_no_pairing_activation("x")
    clear_pairing_activation_checker()
    await assert_no_pairing_activation("x")  # now cleared too
