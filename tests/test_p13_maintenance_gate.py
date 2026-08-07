# -*- coding: utf-8 -*-
"""Focused contract tests for the P13 per-memory maintenance coordinator."""

from __future__ import annotations

import asyncio

import pytest

from mcp_memory.core.maintenance import (
    MaintenanceAdmissionError,
    MaintenanceCoordinator,
    MaintenanceCoordinatorCorrupted,
    MaintenanceRejectionReason,
    get_maintenance_coordinator,
    reset_maintenance_coordinator_for_tests,
)


async def _assert_ordinary_admitted(
    coordinator: MaintenanceCoordinator,
    memory_id: str,
) -> None:
    async with coordinator.ordinary(memory_id):
        pass


async def _assert_maintenance_admitted(
    coordinator: MaintenanceCoordinator,
    memory_id: str,
) -> None:
    async with coordinator.maintenance(memory_id):
        pass


async def test_same_task_ordinary_nesting_tracks_the_outermost_scope() -> None:
    coordinator = MaintenanceCoordinator()

    async with coordinator.ordinary("memory-a"):
        async with coordinator.ordinary("memory-a"):
            with pytest.raises(MaintenanceAdmissionError) as exc_info:
                async with coordinator.maintenance("memory-a"):
                    pytest.fail("maintenance must not enter an ordinary scope")

            assert (
                exc_info.value.reason
                == MaintenanceRejectionReason.ORDINARY_MUTATION_ACTIVE
            )

        with pytest.raises(MaintenanceAdmissionError) as exc_info:
            async with coordinator.maintenance("memory-a"):
                pytest.fail("the outer ordinary scope is still active")
        assert (
            exc_info.value.reason
            == MaintenanceRejectionReason.ORDINARY_MUTATION_ACTIVE
        )

    await _assert_maintenance_admitted(coordinator, "memory-a")


async def test_maintenance_fails_fast_before_idle_check_when_ordinary_is_active() -> None:
    coordinator = MaintenanceCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    idle_called = False

    async def hold_ordinary() -> None:
        async with coordinator.ordinary("memory-a"):
            entered.set()
            await release.wait()

    async def idle_check() -> bool:
        nonlocal idle_called
        idle_called = True
        return True

    holder = asyncio.create_task(hold_ordinary())
    await entered.wait()
    try:
        with pytest.raises(MaintenanceAdmissionError) as exc_info:
            async with coordinator.maintenance(
                "memory-a", idle_check=idle_check
            ):
                pytest.fail("maintenance must fail fast")
        assert (
            exc_info.value.reason
            == MaintenanceRejectionReason.ORDINARY_MUTATION_ACTIVE
        )
        assert idle_called is False
    finally:
        release.set()
        await holder

    await _assert_ordinary_admitted(coordinator, "memory-a")


async def test_requested_phase_closes_admissions_and_false_idle_reopens_them() -> None:
    coordinator = MaintenanceCoordinator()
    idle_entered = asyncio.Event()
    finish_idle = asyncio.Event()

    async def idle_check() -> bool:
        idle_entered.set()
        await finish_idle.wait()
        return False

    async def request_maintenance() -> None:
        async with coordinator.maintenance("memory-a", idle_check=idle_check):
            pytest.fail("a false idle check must not activate maintenance")

    request = asyncio.create_task(request_maintenance())
    await idle_entered.wait()

    with pytest.raises(MaintenanceAdmissionError) as ordinary_exc:
        async with coordinator.ordinary("memory-a"):
            pytest.fail("ordinary admission must be closed while requested")
    assert (
        ordinary_exc.value.reason
        == MaintenanceRejectionReason.MAINTENANCE_REQUESTED
    )

    with pytest.raises(MaintenanceAdmissionError) as maintenance_exc:
        async with coordinator.maintenance("memory-a"):
            pytest.fail("a second maintenance request must be rejected")
    assert (
        maintenance_exc.value.reason
        == MaintenanceRejectionReason.MAINTENANCE_REQUESTED
    )

    # The global bookkeeping lock is not held across the idle-check await.
    await _assert_ordinary_admitted(coordinator, "memory-b")
    await _assert_maintenance_admitted(coordinator, "memory-b")

    finish_idle.set()
    with pytest.raises(MaintenanceAdmissionError) as idle_exc:
        await request
    assert idle_exc.value.reason == MaintenanceRejectionReason.IDLE_CHECK_FAILED

    await _assert_ordinary_admitted(coordinator, "memory-a")
    await _assert_maintenance_admitted(coordinator, "memory-a")


async def test_idle_check_requires_the_exact_boolean_true() -> None:
    coordinator = MaintenanceCoordinator()

    async def truthy_non_boolean() -> bool:
        return 1  # type: ignore[return-value]

    with pytest.raises(MaintenanceAdmissionError) as exc_info:
        async with coordinator.maintenance(
            "memory-a",
            idle_check=truthy_non_boolean,
        ):
            pytest.fail("a merely truthy idle result must fail closed")

    assert exc_info.value.reason == MaintenanceRejectionReason.IDLE_CHECK_FAILED
    await _assert_ordinary_admitted(coordinator, "memory-a")
    await _assert_maintenance_admitted(coordinator, "memory-a")


async def test_active_owner_can_nest_but_child_task_and_second_maintenance_cannot() -> None:
    coordinator = MaintenanceCoordinator()
    active = asyncio.Event()
    release = asyncio.Event()
    nested_owner_admitted = False

    async def hold_maintenance() -> None:
        nonlocal nested_owner_admitted
        async with coordinator.maintenance("memory-a"):
            async with coordinator.ordinary("memory-a"):
                nested_owner_admitted = True
            active.set()
            await release.wait()

    holder = asyncio.create_task(hold_maintenance())
    await active.wait()
    assert nested_owner_admitted is True

    with pytest.raises(MaintenanceAdmissionError) as child_exc:
        await asyncio.create_task(_assert_ordinary_admitted(coordinator, "memory-a"))
    assert child_exc.value.reason == MaintenanceRejectionReason.MAINTENANCE_ACTIVE

    with pytest.raises(MaintenanceAdmissionError) as second_exc:
        async with coordinator.maintenance("memory-a"):
            pytest.fail("a second maintenance context must be rejected")
    assert second_exc.value.reason == MaintenanceRejectionReason.MAINTENANCE_ACTIVE

    release.set()
    await holder
    await _assert_ordinary_admitted(coordinator, "memory-a")


async def test_child_ordinary_task_has_independent_ownership() -> None:
    coordinator = MaintenanceCoordinator()
    child_entered = asyncio.Event()
    release_child = asyncio.Event()

    async def child_ordinary() -> None:
        async with coordinator.ordinary("memory-a"):
            child_entered.set()
            await release_child.wait()

    async with coordinator.ordinary("memory-a"):
        child = asyncio.create_task(child_ordinary())
        await child_entered.wait()

    try:
        with pytest.raises(MaintenanceAdmissionError) as exc_info:
            async with coordinator.maintenance("memory-a"):
                pytest.fail("the child remains an independently active owner")
        assert (
            exc_info.value.reason
            == MaintenanceRejectionReason.ORDINARY_MUTATION_ACTIVE
        )
    finally:
        release_child.set()
        await child

    await _assert_maintenance_admitted(coordinator, "memory-a")


@pytest.mark.parametrize("failure_phase", ["idle", "body"])
async def test_errors_release_maintenance_admission(failure_phase: str) -> None:
    coordinator = MaintenanceCoordinator()

    class ExpectedError(Exception):
        pass

    async def idle_check() -> bool:
        if failure_phase == "idle":
            raise ExpectedError("idle failed")
        return True

    with pytest.raises(ExpectedError):
        async with coordinator.maintenance("memory-a", idle_check=idle_check):
            if failure_phase == "body":
                raise ExpectedError("body failed")

    await _assert_ordinary_admitted(coordinator, "memory-a")
    await _assert_maintenance_admitted(coordinator, "memory-a")


@pytest.mark.parametrize("cancellation_phase", ["idle", "body"])
async def test_cancellation_releases_maintenance_admission(
    cancellation_phase: str,
) -> None:
    coordinator = MaintenanceCoordinator()
    phase_entered = asyncio.Event()
    never = asyncio.Event()

    async def idle_check() -> bool:
        if cancellation_phase == "idle":
            phase_entered.set()
            await never.wait()
        return True

    async def hold_maintenance() -> None:
        async with coordinator.maintenance("memory-a", idle_check=idle_check):
            if cancellation_phase == "body":
                phase_entered.set()
                await never.wait()

    holder = asyncio.create_task(hold_maintenance())
    await phase_entered.wait()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    await _assert_ordinary_admitted(coordinator, "memory-a")
    await _assert_maintenance_admitted(coordinator, "memory-a")


@pytest.mark.parametrize("scope", ["ordinary", "maintenance"])
async def test_repeated_cancellation_cannot_interrupt_state_release(
    scope: str,
) -> None:
    coordinator = MaintenanceCoordinator()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def hold_scope() -> None:
        context = (
            coordinator.ordinary("memory-a")
            if scope == "ordinary"
            else coordinator.maintenance("memory-a")
        )
        async with context:
            entered.set()
            await never.wait()

    holder = asyncio.create_task(hold_scope())
    await entered.wait()
    await coordinator._guard.acquire()
    try:
        holder.cancel()
        await asyncio.sleep(0)
        assert holder.done() is False

        holder.cancel()
        await asyncio.sleep(0)
        assert holder.done() is False
    finally:
        coordinator._guard.release()

    with pytest.raises(asyncio.CancelledError):
        await holder

    await _assert_ordinary_admitted(coordinator, "memory-a")
    await _assert_maintenance_admitted(coordinator, "memory-a")


async def test_cancelled_owner_keeps_cancellation_when_cleanup_is_corrupt() -> None:
    coordinator = MaintenanceCoordinator()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def hold_ordinary() -> None:
        async with coordinator.ordinary("memory-a"):
            entered.set()
            await never.wait()

    holder = asyncio.create_task(hold_ordinary())
    await entered.wait()
    # Plant the exact bookkeeping corruption the release path must detect.
    state = coordinator._states["memory-a"]
    state.ordinary_depths.clear()

    holder.cancel("authoritative caller cancellation")
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await holder

    assert cancelled.value.args == ("authoritative caller cancellation",)
    assert type(cancelled.value.__cause__) is MaintenanceCoordinatorCorrupted
    assert str(cancelled.value.__cause__) == (
        "maintenance coordinator state is unavailable"
    )
    assert cancelled.value.__cause__.__cause__ is None

    for admission in (
        _assert_ordinary_admitted,
        _assert_maintenance_admitted,
    ):
        with pytest.raises(MaintenanceCoordinatorCorrupted) as poisoned:
            await admission(coordinator, "memory-after-corruption")
        assert str(poisoned.value) == (
            "maintenance coordinator state is unavailable"
        )
        serialized = repr(poisoned.value) + str(poisoned.value)
        assert "memory-a" not in serialized
        assert "admission was lost" not in serialized


async def test_cancelled_maintenance_owner_keeps_cancellation_and_poisons_all() -> None:
    coordinator = MaintenanceCoordinator()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def hold_maintenance() -> None:
        async with coordinator.maintenance("memory-a"):
            entered.set()
            await never.wait()

    holder = asyncio.create_task(hold_maintenance())
    await entered.wait()
    coordinator._states["memory-a"].maintenance_phase = None

    holder.cancel("authoritative maintenance cancellation")
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await holder

    assert cancelled.value.args == ("authoritative maintenance cancellation",)
    assert type(cancelled.value.__cause__) is MaintenanceCoordinatorCorrupted
    assert cancelled.value.__cause__.__cause__ is None
    for admission in (_assert_ordinary_admitted, _assert_maintenance_admitted):
        with pytest.raises(MaintenanceCoordinatorCorrupted):
            await admission(coordinator, "memory-after-maintenance-corruption")


async def test_poison_during_an_unrelated_idle_check_prevents_activation() -> None:
    coordinator = MaintenanceCoordinator()
    idle_entered = asyncio.Event()
    finish_idle = asyncio.Event()
    body_entered = False

    async def idle_check() -> bool:
        idle_entered.set()
        await finish_idle.wait()
        return True

    async def request_maintenance() -> None:
        nonlocal body_entered
        async with coordinator.maintenance("memory-b", idle_check=idle_check):
            body_entered = True

    request = asyncio.create_task(request_maintenance())
    await idle_entered.wait()
    assert coordinator._states["memory-b"].maintenance_phase == "requested"

    with pytest.raises(MaintenanceCoordinatorCorrupted):
        async with coordinator.ordinary("memory-a"):
            coordinator._states["memory-a"].ordinary_depths.clear()

    finish_idle.set()
    with pytest.raises(MaintenanceCoordinatorCorrupted):
        await request

    assert body_entered is False
    assert "memory-b" not in coordinator._states
    with pytest.raises(MaintenanceCoordinatorCorrupted):
        await _assert_maintenance_admitted(coordinator, "memory-c")


async def test_cleanup_corruption_without_cancellation_poison_all_admissions() -> None:
    coordinator = MaintenanceCoordinator()

    with pytest.raises(MaintenanceCoordinatorCorrupted) as corruption:
        async with coordinator.ordinary("memory-a"):
            coordinator._states["memory-a"].ordinary_depths.clear()

    assert str(corruption.value) == "maintenance coordinator state is unavailable"
    assert corruption.value.__cause__ is None

    with pytest.raises(MaintenanceCoordinatorCorrupted):
        await _assert_ordinary_admitted(coordinator, "memory-b")
    with pytest.raises(MaintenanceCoordinatorCorrupted):
        await _assert_maintenance_admitted(coordinator, "memory-b")


async def test_cleanup_publishes_poison_before_queued_waiter_can_enter() -> None:
    class YieldAfterReleaseLock:
        """FIFO lock that exposes contenders and yields after each release.

        The deliberate scheduling point mutation-proves that poison must be
        written inside the corrupted release branch, while the lock is held;
        publishing it only in an outer exception handler lets waiter two enter.
        """

        def __init__(self) -> None:
            self._inner = asyncio.Lock()
            self._contenders = 0
            self.first_waiter = asyncio.Event()
            self.second_waiter = asyncio.Event()

        async def acquire(self) -> bool:
            if self._inner.locked():
                self._contenders += 1
                if self._contenders == 1:
                    self.first_waiter.set()
                elif self._contenders == 2:
                    self.second_waiter.set()
            return await self._inner.acquire()

        def release(self) -> None:
            self._inner.release()

        async def __aenter__(self):
            await self.acquire()
            return self

        async def __aexit__(self, *_args):
            self.release()
            await asyncio.sleep(0)
            return False

    coordinator = MaintenanceCoordinator()
    observed_guard = YieldAfterReleaseLock()
    coordinator._guard = observed_guard
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def owner() -> None:
        async with coordinator.ordinary("memory-a"):
            entered.set()
            await leave.wait()

    owner_task = asyncio.create_task(owner())
    await entered.wait()

    # Hold the guard so both the corrupt cleanup and a later unrelated
    # admission queue in deterministic FIFO order.
    await observed_guard.acquire()
    leave.set()
    await observed_guard.first_waiter.wait()
    waiter_task = asyncio.create_task(
        _assert_ordinary_admitted(coordinator, "memory-b")
    )
    await observed_guard.second_waiter.wait()
    coordinator._states["memory-a"].ordinary_depths.clear()
    observed_guard.release()

    with pytest.raises(MaintenanceCoordinatorCorrupted):
        await owner_task
    with pytest.raises(MaintenanceCoordinatorCorrupted) as waiter:
        await waiter_task
    assert str(waiter.value) == "maintenance coordinator state is unavailable"


async def test_memory_keys_are_exact_and_cross_memory_operations_are_independent() -> None:
    coordinator = MaintenanceCoordinator()

    async with coordinator.maintenance("Memory-A"):
        await _assert_ordinary_admitted(coordinator, "memory-a")
        await _assert_maintenance_admitted(coordinator, "memory-a")

        with pytest.raises(MaintenanceAdmissionError):
            await asyncio.create_task(
                _assert_ordinary_admitted(coordinator, "Memory-A")
            )


def test_process_global_singleton_can_be_reset_between_tests() -> None:
    reset_maintenance_coordinator_for_tests()
    first = get_maintenance_coordinator()
    assert first is get_maintenance_coordinator()

    reset_maintenance_coordinator_for_tests()
    second = get_maintenance_coordinator()
    assert second is get_maintenance_coordinator()
    assert second is not first
