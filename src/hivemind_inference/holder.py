# -*- coding: utf-8 -*-
"""The per-service holder of one :class:`~hivemind_inference.InferenceRuntime`.

Both consuming services (the Hivemind core, the embedded Graph Memory runtime)
need the same thing: a module-level runtime resolved once, handed to every
caller, closed exactly once on ASGI shutdown, and refused to a late background
worker so no unowned provider transport is ever built.

**Why this lives here instead of once per service.** The two service modules
held four lifecycle functions that were byte-identical after ``ast.unparse``;
only an error string differed. Four consecutive adversarial-review rounds on
PR #303 each found one ownership defect, and each repair naturally landed in
whichever module the finding named — so the second copy kept the hole open. One
state machine, exercised by one test matrix parametrised over both services, is
the structural answer to a defect class that reappeared four times.

**The invariant, in one sentence.**

    The slot is cleared exactly when the runtime reports every adapter settled
    (:attr:`~hivemind_inference.InferenceRuntime.is_fully_closed`), computed in
    a ``finally`` and only if the slot still holds the runtime this call
    closed — never from how ``aclose()`` returned — and no operation may hand
    out, adopt, or report success against a runtime that is not fully closed.

Two consequences worth stating explicitly, because both were previously wrong:

- A close that RAISES, is CANCELLED, or never settles leaves the slot
  populated. The old code cleared it on the statement *after* ``await``, so any
  non-normal return skipped the clear and pinned the holder to a terminal
  runtime — while a later startup happily adopted it and reported success.
- A new serving window REFUSES to start while the previous one's transports are
  unaccounted for. It does not adopt them (every operation would fail while
  startup claimed health) and it does not discard them either: dropping the
  last reference to a possibly-open transport is precisely the never-orphan
  violation the earlier rounds were about. Refusing is the only option that
  neither lies nor leaks.

No lock is taken. Every mutation happens in synchronous code except the single
``await`` in :meth:`close_if_initialized`, and the compare-and-clear against
the runtime this call closed is what makes that one interleaving safe.
"""

from __future__ import annotations

from typing import Callable

from .runtime import InferenceRuntime, InferenceRuntimeClosed


class InferenceRuntimeHolder:
    """Owns one runtime per serving window, for one service."""

    __slots__ = ("_service", "_shutdown_message", "_proxy_url", "_env_file",
                 "_runtime", "_shutdown")

    def __init__(
        self,
        *,
        service: str,
        shutdown_message: str,
        proxy_url: Callable[[], str | None],
        env_file: str | None = ".env",
    ) -> None:
        self._service = service
        self._shutdown_message = shutdown_message
        self._proxy_url = proxy_url
        self._env_file = env_file
        self._runtime: InferenceRuntime | None = None
        # Shutdown is TERMINAL for the serving window. Both services run
        # background asyncio workers (consolidation, ingestion) that reach
        # inference and that no lifespan awaits, so without this flag a worker
        # still running when the close hook fires would find an empty slot and
        # build a REPLACEMENT runtime — whose transport nothing would ever
        # close, because the only close hook has already run.
        self._shutdown = False

    # -- introspection (tests and diagnostics) -------------------------------

    @property
    def current(self) -> InferenceRuntime | None:
        """The cached runtime, without resolving one."""
        return self._runtime

    @property
    def is_shut_down(self) -> bool:
        return self._shutdown

    # -- the four lifecycle operations ---------------------------------------

    def get(self) -> InferenceRuntime:
        """The runtime for this serving window (fail-closed on first use).

        Raises :class:`InferenceRuntimeClosed` once the window has begun
        shutting down, so a late background task fails its operation honestly
        instead of silently opening an unowned provider transport.
        """
        if self._shutdown:
            raise InferenceRuntimeClosed(self._shutdown_message)
        if self._runtime is None:
            self._runtime = InferenceRuntime.from_environment(
                env_file=self._env_file, proxy_url=self._proxy_url()
            )
        return self._runtime

    def validate_startup(self) -> None:
        """Resolve the configuration fail-closed at service startup.

        A partial role, a mixed ``LLMAAS_*``/``INFERENCE_*`` family, an unknown
        provider, or an invalid value raises ``InferenceConfigError`` here and
        blocks serving — before any provider network access. A wholly ABSENT
        profile stays a valid startup: the roles are simply unavailable, health
        reports them as not configured, and operations fail closed at call
        time.

        This is also the ONLY thing that reopens the seam after a shutdown. The
        terminal flag is scoped to one SERVING WINDOW, not to the interpreter:
        an explicit new startup (a second ASGI lifespan in the same process)
        opens a window whose runtime that window's own shutdown will close.

        It publishes the lowered flag only once resolution has SUCCEEDED — a
        failed startup that left the seam open would let a surviving worker
        build a runtime in a process whose close hook is never going to run.

        **What this method does NOT enforce, and where that lives instead.**
        It refuses to start over a previous window's *unaccounted-for
        transports*, which is a narrower property than "only one window at a
        time": ``is_fully_closed`` is True for a freshly resolved runtime whose
        lazy adapters do not exist yet, so this check cannot see an overlapping
        window that has not built an adapter. It used to be documented as if it
        could (R7-F1). Window uniqueness is enforced one layer up, by
        :class:`hivemind_inference.process_window.ProcessWindowGate`, which
        refuses a second application during the guard's dedicated synchronous
        ``LifespanOwnership.reserve`` phase — after pure validation but before
        any resource is ownable, and therefore before any rollback could tear
        down the first window's transports. The sentence above about a second
        lifespan is true *because* of that gate, not because of this check.
        """
        stale = self._runtime
        if stale is not None and not stale.is_fully_closed:
            raise InferenceRuntimeClosed(
                f"the previous {self._service} serving window has not finished "
                "releasing its provider transports; refusing to start a new "
                "one over them"
            )
        previous = self._shutdown
        self._shutdown = False
        try:
            self.get()
        except BaseException:
            self._shutdown = previous
            raise

    async def close_if_initialized(self) -> None:
        """Shutdown hook: close the owned adapter transports (idempotent).

        The terminal flag is raised BEFORE anything is awaited, so a background
        worker that reaches inference during the close cannot slip in and build
        a replacement runtime.

        The slot is cleared from the runtime's own settled state, in a
        ``finally``, and only if it still holds the runtime this call closed.
        Deciding from the ``await`` returning normally is what pinned the
        holder on every failure path; the compare guards against clearing a
        runtime a concurrent reset or startup already replaced.
        """
        self._shutdown = True
        runtime = self._runtime
        if runtime is None:
            return
        try:
            await runtime.aclose()
        finally:
            if runtime.is_fully_closed and self._runtime is runtime:
                self._runtime = None

    def reset_for_tests(self) -> None:
        """Drop the cached runtime AND lift the terminal flag so tests can
        re-resolve a patched environment.

        Transports are owned per runtime instance; a test that actually built
        an adapter should use :meth:`close_if_initialized` instead, so the
        transport is released rather than orphaned.
        """
        self._runtime = None
        self._shutdown = False

    def snapshot_for_tests(self) -> tuple:
        """Opaque state capture for a fixture that must restore this holder.

        Returned as one opaque value so a fixture cannot silently miss a field
        when the holder grows one — the recurring way lifecycle state leaked
        between tests.
        """
        return (self._runtime, self._shutdown)

    def restore_for_tests(self, snapshot: tuple) -> None:
        self._runtime, self._shutdown = snapshot
