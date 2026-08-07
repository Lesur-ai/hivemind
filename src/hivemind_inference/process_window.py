# -*- coding: utf-8 -*-
"""One fail-closed serving window per process (#276 / R7-F1, R8-F1/F2).

**The defect this closes.** ``LifespanGuard`` owns a startup gate PER INSTANCE,
and every ``create_app()`` builds a new guard — but the resources those guards
manage are *process-global*: the Core consolidator singleton, the Graph Memory
extractor/embedder registries, and the shared inference runtime holder. Two
independently created apps in one process therefore each held a valid-looking
gate over one shared set of resources, and whichever stopped first tore them
down under the other.

Three things are needed, and only all three together are safe.

**1. Reserve before anything is acquired.** ``LifespanGuard`` runs the
synchronous ``LifespanOwnership.reserve`` phase after pure validation and
before it marks resources ownable. A refused contender therefore runs no
shutdown hook and cannot tear down the active window.

**2. Bind every process-global hook to an owner.** A window that legitimately
claimed and then failed startup does run the full shutdown bundle. Owner
identity makes that rollback release exactly what this window took and nothing
else, and makes a closer that somehow runs without ownership a no-op rather
than a cross-window teardown.

**3. Release only from positive lifecycle evidence.** A release is not an
``on_shutdown`` finalizer. The guard calls it only after either a fully cleaned
startup rollback or a clean shutdown whose inner task, cleanup, terminal send,
and local gate transition all settled. Any close failure, cancellation, inner
death, terminal failure, or quarantined task retains the owner and requires a
process recycle.

All owner reads and transitions use a ``threading.Lock``. Synchronous Python
code can still interleave across operating-system threads; the absence of an
``await`` is not an atomicity guarantee.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

Hook = Callable[[], Any]


class ProcessWindowBusy(RuntimeError):
    """Another serving window already owns this process's shared resources."""


class ProcessWindowGate:
    """The single owner slot for one service's process-global resources."""

    __slots__ = ("_service", "_lock", "_owner")

    def __init__(self, *, service: str) -> None:
        self._service = service
        self._lock = threading.Lock()
        self._owner: ProcessWindow | None = None

    @property
    def owner(self) -> "ProcessWindow | None":
        with self._lock:
            return self._owner

    def new_window(self) -> "ProcessWindow":
        """A fresh, unclaimed window token for one application instance."""
        return ProcessWindow(self)

    # -- test helpers --------------------------------------------------------

    def snapshot_for_tests(self) -> tuple:
        """Opaque capture, matching the holder's contract: one value, so a
        fixture cannot silently miss a field when this class grows one."""
        with self._lock:
            return (self._owner,)

    def restore_for_tests(self, snapshot: tuple) -> None:
        (owner,) = snapshot
        with self._lock:
            self._owner = owner


class ProcessWindow:
    """One application instance's claim on the process's serving window."""

    __slots__ = ("_gate",)

    def __init__(self, gate: ProcessWindowGate) -> None:
        self._gate = gate

    def claim(self) -> None:
        """Atomically take the process window, or refuse any occupied slot."""

        with self._gate._lock:
            if self._gate._owner is not None:
                raise ProcessWindowBusy(
                    f"another {self._gate._service} serving window already owns "
                    "this process's shared resources; a second application in "
                    "the same process is refused rather than allowed to share "
                    "or adopt an uncertain terminal window"
                )
            self._gate._owner = self

    def owns(self) -> bool:
        with self._gate._lock:
            return self._gate._owner is self

    def release(self) -> None:
        """Atomically clear this owner after a positive reusable checkpoint."""

        with self._gate._lock:
            if self._gate._owner is self:
                self._gate._owner = None

    def guard(self, hook: Hook) -> Hook:
        """Wrap a process-global hook so it runs only for this window's owner.

        Returns a SYNCHRONOUS callable that forwards whatever ``hook`` returns.
        Both ``run_finalizers`` and ``_run_fail_fast`` await the result only
        when it is awaitable, so this preserves an async hook's coroutine and a
        sync hook's ``None`` without the wrapper having to know which it got.
        """

        def owned() -> Any:
            # The lifecycle coordinator cannot issue the positive release
            # until all guarded hooks have settled, so the check and hook call
            # need not hold a thread lock across a potentially async result.
            if not self.owns():
                return None
            return hook()

        owned.__name__ = f"owned_{getattr(hook, '__name__', 'hook')}"
        owned.__qualname__ = owned.__name__
        owned.__doc__ = (
            f"Owner-guarded {getattr(hook, '__name__', 'hook')} "
            "(no-op unless this window owns the process)."
        )
        return owned
