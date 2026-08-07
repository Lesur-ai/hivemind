# -*- coding: utf-8 -*-
"""Process-local exclusion for bounded per-memory maintenance operations.

The P13 maintenance reindex is deliberately limited to one active Graph
Memory runtime.  This coordinator provides the corresponding in-process
boundary: ordinary namespace mutations may overlap, while one explicit
maintenance operation closes new admissions for its exact ``memory_id``.

Ownership is tied to :func:`asyncio.current_task`, not to a ``ContextVar``.
Consequently a helper called by the maintenance task may nest an ordinary
mutation after maintenance becomes active, but a child task never inherits
that privilege.  No lock is held while awaiting the optional idle check or
while executing either context body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


IdleCheck = Callable[[], Awaitable[bool]]
_Phase = Literal["requested", "active"]

# Shared #278 source bounds. A valid S3 namespace can contain at most one
# retained object per Graph document plus the single ontology object written
# by ``memory_create``.
MAX_REINDEX_SOURCE_DOCUMENTS = 10_000
MAX_REINDEX_SOURCE_OBJECTS = MAX_REINDEX_SOURCE_DOCUMENTS + 1
MAX_REINDEX_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024


class ReindexSourceLimitExceeded(RuntimeError):
    """A value-free adapter refusal for bounded #278 source inventory."""


class MaintenanceRejectionReason(StrEnum):
    """Stable reasons exposed by :class:`MaintenanceAdmissionError`."""

    ORDINARY_MUTATION_ACTIVE = "ordinary_mutation_active"
    MAINTENANCE_REQUESTED = "maintenance_requested"
    MAINTENANCE_ACTIVE = "maintenance_active"
    IDLE_CHECK_FAILED = "idle_check_failed"


class MaintenanceAdmissionError(RuntimeError):
    """A fail-fast maintenance or ordinary-mutation admission refusal."""

    def __init__(
        self,
        memory_id: str,
        reason: MaintenanceRejectionReason,
    ) -> None:
        self.memory_id = memory_id
        self.reason = reason
        super().__init__(f"{reason.value}: memory '{memory_id}'")


class MaintenanceCoordinatorCorrupted(RuntimeError):
    """Fixed, value-free refusal after cleanup state becomes unverifiable."""

    def __init__(self) -> None:
        super().__init__("maintenance coordinator state is unavailable")


@dataclass(slots=True)
class _MemoryState:
    ordinary_depths: dict[asyncio.Task[Any], int] = field(default_factory=dict)
    maintenance_owner: asyncio.Task[Any] | None = None
    maintenance_phase: _Phase | None = None


class MaintenanceCoordinator:
    """Coordinate ordinary mutations and exclusive maintenance per memory.

    The bookkeeping lock is process-global only to linearize short admission
    state transitions.  It is never held across an external await, an idle
    check, or a caller-owned context body, so unrelated memories remain
    independently usable while one memory is in maintenance.
    """

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._states: dict[str, _MemoryState] = {}
        # Cleanup failure means an admission may still be recorded even though
        # its owner has returned.  There is no safe local reconstruction of
        # that ownership, so the process-wide coordinator is terminally
        # poisoned until process recycle (or the explicit test reset).
        self._corrupted = False

    @staticmethod
    def _memory_key(memory_id: str) -> str:
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        # Deliberately no strip, case-fold, hashing, or namespace conversion:
        # exclusion is keyed by the exact Graph Memory identifier.
        return memory_id

    @staticmethod
    def _current_task() -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - async contexts always own a task
            raise RuntimeError("maintenance admission requires an asyncio task")
        return task

    def _state(self, memory_id: str) -> _MemoryState:
        state = self._states.get(memory_id)
        if state is None:
            state = _MemoryState()
            self._states[memory_id] = state
        return state

    def _prune(self, memory_id: str, state: _MemoryState) -> None:
        if not state.ordinary_depths and state.maintenance_owner is None:
            if self._states.get(memory_id) is state:
                self._states.pop(memory_id, None)

    def _raise_if_corrupted(self) -> None:
        if self._corrupted:
            raise MaintenanceCoordinatorCorrupted()

    def health_status(self) -> dict[str, object]:
        """Return one lock-free, value-free process admission snapshot."""

        available = not self._corrupted
        return {
            "status": "ok" if available else "error",
            "admissions_available": available,
        }

    async def _drain_cleanup(
        self,
        cleanup: Awaitable[None],
        *,
        prior_cancellation: asyncio.CancelledError | None = None,
    ) -> None:
        """Finish state release before propagating any caller cancellation.

        A task can be cancelled again while an async-context ``finally`` is
        waiting for the bookkeeping lock. Run that small release in its own
        task and shield/drain it under repeated cancellation so no namespace
        remains permanently closed merely because its owner went away.

        If release itself fails, ownership is no longer reconstructible.  The
        coordinator is poisoned before another waiter can be admitted and all
        later admissions receive one fixed, value-free refusal.  A cancellation
        already delivered to the caller remains the authoritative exception;
        the fixed corruption refusal is attached as its cause rather than
        replacing it.
        """

        async def guarded_cleanup() -> None:
            try:
                await cleanup
            except BaseException:
                # This assignment contains no await.  In particular, when a
                # release raises while leaving ``_guard``, poison is published
                # before a woken admission waiter can resume and inspect it.
                self._corrupted = True
                raise

        worker = asyncio.create_task(guarded_cleanup())
        delivered_cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                delivered_cancellation = cancelled
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
            # Otherwise the cleanup worker itself was cancelled.  That is a
            # cleanup failure, not caller control flow, and is normalized below.
        except BaseException:
            # Retrieve and normalize the worker's terminal failure below.  Do
            # not retain or reflect its potentially value-bearing exception.
            pass

        corruption: MaintenanceCoordinatorCorrupted | None = None
        try:
            worker.result()
        except BaseException:
            self._corrupted = True
            corruption = MaintenanceCoordinatorCorrupted()

        authoritative_cancellation = prior_cancellation or delivered_cancellation
        if corruption is not None and authoritative_cancellation is not None:
            raise authoritative_cancellation from corruption
        if delivered_cancellation is not None and prior_cancellation is None:
            raise delivered_cancellation
        if corruption is not None:
            raise corruption from None

    @asynccontextmanager
    async def ordinary(self, memory_id: str) -> AsyncIterator[None]:
        """Admit one ordinary namespace mutation.

        Multiple ordinary tasks may overlap.  Re-entry by the same task tracks
        depth explicitly.  While maintenance is requested or active, all other
        tasks are rejected immediately.  Once maintenance is active, its owner
        may enter guarded helper mutations in the same task; a spawned child
        task is a different owner and remains rejected.
        """

        key = self._memory_key(memory_id)
        task = self._current_task()
        state: _MemoryState

        async with self._guard:
            self._raise_if_corrupted()
            state = self._state(key)
            if state.maintenance_owner is not None:
                same_active_owner = (
                    state.maintenance_owner is task
                    and state.maintenance_phase == "active"
                )
                if not same_active_owner:
                    reason = (
                        MaintenanceRejectionReason.MAINTENANCE_REQUESTED
                        if state.maintenance_phase == "requested"
                        else MaintenanceRejectionReason.MAINTENANCE_ACTIVE
                    )
                    raise MaintenanceAdmissionError(key, reason)
            state.ordinary_depths[task] = state.ordinary_depths.get(task, 0) + 1

        prior_cancellation: asyncio.CancelledError | None = None
        try:
            yield
        except asyncio.CancelledError as cancelled:
            prior_cancellation = cancelled
            raise
        finally:
            async def release() -> None:
                async with self._guard:
                    if self._states.get(key) is not state:
                        self._corrupted = True
                        raise RuntimeError(
                            "ordinary maintenance admission state was replaced"
                        )
                    depth = state.ordinary_depths.get(task)
                    if depth is None:
                        self._corrupted = True
                        raise RuntimeError(
                            "ordinary maintenance admission was lost"
                        )
                    if depth == 1:
                        state.ordinary_depths.pop(task, None)
                    else:
                        state.ordinary_depths[task] = depth - 1
                    self._prune(key, state)

            await self._drain_cleanup(
                release(),
                prior_cancellation=prior_cancellation,
            )

    @asynccontextmanager
    async def maintenance(
        self,
        memory_id: str,
        *,
        idle_check: IdleCheck | None = None,
    ) -> AsyncIterator[None]:
        """Acquire exclusive maintenance admission for an exact memory.

        Acquisition is intentionally fail-fast: an active ordinary task or an
        existing requested/active maintenance operation causes an immediate
        :class:`MaintenanceAdmissionError`.  Otherwise admissions close in the
        ``requested`` phase before ``idle_check`` is awaited.  The callback must
        return the exact boolean ``True``; false or unverifiable results fail
        closed and reopen admissions.

        A second maintenance context is always rejected, including from the
        current owner task.  Cancellation and errors in the idle check or body
        release ownership before propagating.
        """

        key = self._memory_key(memory_id)
        task = self._current_task()
        state: _MemoryState
        owns_request = False

        async with self._guard:
            self._raise_if_corrupted()
            state = self._state(key)
            if state.maintenance_owner is not None:
                reason = (
                    MaintenanceRejectionReason.MAINTENANCE_REQUESTED
                    if state.maintenance_phase == "requested"
                    else MaintenanceRejectionReason.MAINTENANCE_ACTIVE
                )
                raise MaintenanceAdmissionError(key, reason)
            if state.ordinary_depths:
                raise MaintenanceAdmissionError(
                    key,
                    MaintenanceRejectionReason.ORDINARY_MUTATION_ACTIVE,
                )
            state.maintenance_owner = task
            state.maintenance_phase = "requested"
            owns_request = True

        prior_cancellation: asyncio.CancelledError | None = None
        try:
            if idle_check is not None:
                idle = await idle_check()
                if idle is not True:
                    raise MaintenanceAdmissionError(
                        key,
                        MaintenanceRejectionReason.IDLE_CHECK_FAILED,
                    )

            async with self._guard:
                self._raise_if_corrupted()
                if (
                    state.maintenance_owner is not task
                    or state.maintenance_phase != "requested"
                ):
                    self._corrupted = True
                    raise RuntimeError("maintenance ownership was lost")
                state.maintenance_phase = "active"

            yield
        except asyncio.CancelledError as cancelled:
            prior_cancellation = cancelled
            raise
        finally:
            if owns_request:
                async def release() -> None:
                    async with self._guard:
                        if (
                            self._states.get(key) is not state
                            or state.maintenance_owner is not task
                            or state.maintenance_phase not in {"requested", "active"}
                        ):
                            self._corrupted = True
                            raise RuntimeError(
                                "maintenance admission state was lost"
                            )
                        state.maintenance_owner = None
                        state.maintenance_phase = None
                        self._prune(key, state)

                await self._drain_cleanup(
                    release(),
                    prior_cancellation=prior_cancellation,
                )


_coordinator = MaintenanceCoordinator()


def get_maintenance_coordinator() -> MaintenanceCoordinator:
    """Return the process-global Graph Memory maintenance coordinator."""

    return _coordinator


def reset_maintenance_coordinator_for_tests() -> None:
    """Replace the process-global coordinator with an empty test instance.

    Tests must call this only after all contexts using the previous instance
    have exited; replacing a live coordinator would intentionally not migrate
    task ownership.
    """

    global _coordinator
    _coordinator = MaintenanceCoordinator()
