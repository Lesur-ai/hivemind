# -*- coding: utf-8 -*-
"""Shared process-lifespan guard for Hivemind Core and Graph Memory.

The guard owns one ASGI lifespan negotiation and turns the inner application's
terminal messages plus Hivemind's resource hooks into one truthful terminal
verdict per phase.

The central invariant is sticky failure: once a phase observes a failure,
protocol violation, cleanup failure, or cancellation, later observations cannot
turn that phase back into success.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Hook = Callable[[], None | Awaitable[None]]
SyncHook = Callable[[], None]

_RETRY = object()


def _base_string(value: str) -> str:
    """Return an exact ``str``, bypassing hostile subclass overrides."""

    return str.__getitem__(value, slice(None))


def _safe_text(value: object, *, fallback: str) -> str:
    """Render untrusted diagnostic text without making verdicts load-bearing."""

    try:
        rendered = _base_string(str(value))
    except BaseException:  # noqa: BLE001 - hostile diagnostics fail closed
        return fallback
    return rendered if len(rendered) > 0 else fallback


def _safe_type_name(value: object, *, fallback: str) -> str:
    try:
        name = type(value).__name__
    except BaseException:  # noqa: BLE001 - hostile diagnostics fail closed
        return fallback
    return _safe_text(name, fallback=fallback)


def _safe_exception_text(failure: BaseException) -> str:
    return _safe_text(
        failure,
        fallback=_safe_type_name(
            failure,
            fallback="<unprintable exception>",
        ),
    )


def _safe_repr(value: object, *, fallback: str) -> str:
    try:
        return _base_string(repr(value))
    except BaseException:  # noqa: BLE001 - hostile diagnostics fail closed
        return fallback


def _dict_field(message: dict[Any, Any], name: str) -> object:
    """Read an exact dict field without invoking subclass overrides."""

    return dict.get(message, name)


def _message_type(message: dict[Any, Any]) -> str | None:
    """Return an exact string message type or a non-overridable sentinel."""

    value = _dict_field(message, "type")
    return _base_string(value) if isinstance(value, str) else None


def _contains_cancellation(failure: BaseException | None) -> bool:
    """Return whether an exception, including a group, carries cancellation."""

    if isinstance(failure, asyncio.CancelledError):
        return True
    if isinstance(failure, BaseExceptionGroup):
        return any(
            _contains_cancellation(child)
            for child in failure.exceptions
        )
    return False


def _is_pure_cancellation(failure: BaseException | None) -> bool:
    """Return whether every leaf in an outcome is cancellation."""

    if isinstance(failure, asyncio.CancelledError):
        return True
    if isinstance(failure, BaseExceptionGroup):
        return bool(failure.exceptions) and all(
            _is_pure_cancellation(child)
            for child in failure.exceptions
        )
    return False


async def run_finalizers(*steps: Hook) -> BaseException | None:
    """Run every cleanup step and return cancellation or the first failure.

    Cleanup is deliberately exhaustive.  A failure or cancellation in one
    finalizer must not orphan resources owned by the remaining finalizers.
    Cancellation is returned to the caller so it can be re-raised only after
    the ASGI failure verdict is on the wire.
    """

    cancellation: BaseException | None = None
    first_error: BaseException | None = None
    for step in steps:
        try:
            outcome = step()
            if inspect.isawaitable(outcome):
                await outcome
        except BaseException as exc:  # noqa: BLE001 - cleanup must be exhaustive
            if _contains_cancellation(exc) and cancellation is None:
                # Preserve the whole group when TaskGroup/AnyIO combines a
                # cancellation with sibling failures.
                cancellation = exc
            elif first_error is None:
                first_error = exc
    return cancellation if cancellation is not None else first_error


async def _run_fail_fast(steps: tuple[Hook, ...]) -> BaseException | None:
    """Run ordered startup work and stop at the first non-success."""

    for step in steps:
        try:
            outcome = step()
            if inspect.isawaitable(outcome):
                await outcome
        except BaseException as exc:  # noqa: BLE001 - cancellation is an outcome
            return exc
    return None


def _run_sync_hook(step: SyncHook, *, role: str) -> BaseException | None:
    """Run one non-suspending ownership transition.

    A process-window transition must be complete before the lifecycle
    coordinator crosses another cancellation or scheduling boundary.  An
    accidental async callback is therefore a contract failure, never work the
    guard awaits later.
    """

    try:
        outcome = step()
        if inspect.isawaitable(outcome):
            # A coroutine has not started yet and can be closed. A Task/Future
            # may already be scheduled for the next loop turn, so merely
            # rejecting its return value is insufficient: it could otherwise
            # execute an invalid delayed release after this function reports
            # failure. Prefer cancellation when available, falling back to
            # closing an unstarted coroutine.
            cancel = getattr(outcome, "cancel", None)
            close = getattr(outcome, "close", None)
            neutralize = cancel if callable(cancel) else close
            if callable(neutralize):
                neutralize()
            raise TypeError(f"{role} must be synchronous")
        if outcome is not None:
            raise TypeError(f"{role} must return None")
    except BaseException as exc:  # noqa: BLE001 - transition failure is state
        return exc
    return None


@dataclass(frozen=True, slots=True)
class LifespanOwnership:
    """Paired process-window reservation and positive release callbacks.

    ``reserve`` runs after pure validation and before any resource may be
    acquired. ``release_reusable`` runs only after the guard has confirmed a
    clean shutdown or a fully settled startup rollback. Both callbacks are
    synchronous so ownership cannot be half-transitioned across an await.
    """

    reserve: SyncHook
    release_reusable: SyncHook


@dataclass(frozen=True, slots=True)
class LifespanHooks:
    """Service-owned work attached to the process lifespan.

    ``on_validate`` must be pure: it may inspect configuration but must not
    acquire a resource. ``ownership.reserve`` is the separate, synchronous
    process-local reservation phase. ``on_startup`` may acquire resources.
    ``on_shutdown`` releases resources, including resources acquired lazily by
    request handling. ``ownership.release_reusable`` is not a finalizer: it is
    a positive notification emitted only after lifecycle cleanup is confirmed.
    The presence of ownership or either resource hook makes an ASGI lifespan
    mandatory; a ``--lifespan off`` request is refused.
    """

    on_validate: tuple[Hook, ...] = ()
    on_startup: tuple[Hook, ...] = ()
    on_shutdown: tuple[Hook, ...] = ()
    ownership: LifespanOwnership | None = None


class TerminalMessageViolation(RuntimeError):
    """The inner application violated the one-terminal-message contract."""


class StartupRefused(RuntimeError):
    """The process is not in a serving state."""


@dataclass(slots=True)
class _PhaseLatch:
    phase: str
    saw_complete: bool = False
    saw_failed: bool = False
    terminal_count: int = 0
    reasons: list[str] = field(default_factory=list)
    emitted: bool = False
    frozen: bool = False
    _settle_scheduled: bool = False
    _settled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def ok(self) -> bool:
        return (
            self.terminal_count == 1
            and self.saw_complete
            and not self.saw_failed
            and not self.reasons
        )

    def _schedule_settle(self) -> None:
        if self._settle_scheduled:
            return
        self._settle_scheduled = True
        # Inner ASGI applications commonly emit consecutive terminal messages
        # without an actual suspension.  Settling on the next loop turn sees
        # the entire synchronous burst while still making progress when the app
        # parks somewhere other than receive().
        asyncio.get_running_loop().call_soon(self._settled.set)

    def observe_terminal(self, kind: str, reason: object | None) -> None:
        if self.frozen or self.emitted:
            raise TerminalMessageViolation(
                f"lifespan.{self.phase} emitted a terminal message after "
                "its verdict was sealed"
            )

        self.terminal_count += 1
        if kind == "failed":
            self.saw_failed = True
            if reason is not None:
                self.reasons.append(
                    _safe_text(
                        reason,
                        fallback="<unprintable inner failure reason>",
                    )
                )
        else:
            self.saw_complete = True
        self._schedule_settle()

        if self.terminal_count > 1:
            self.reasons.append(
                f"{self.terminal_count} terminal messages were emitted for "
                f"lifespan.{self.phase}"
            )
            raise TerminalMessageViolation(
                f"duplicate terminal message for lifespan.{self.phase}"
            )

    def observe_violation(self, reason: object) -> None:
        rendered = _safe_text(
            reason,
            fallback="<unprintable protocol violation>",
        )
        if self.frozen or self.emitted:
            raise TerminalMessageViolation(rendered)
        self.saw_failed = True
        self.reasons.append(rendered)
        self._schedule_settle()

    async def wait_until_settled(self) -> None:
        await self._settled.wait()

    def freeze(self) -> None:
        self.frozen = True


@dataclass(slots=True)
class _LifespanExecution:
    """Mutable ownership record used only to make cancellation recoverable."""

    startup: _PhaseLatch = field(default_factory=lambda: _PhaseLatch("startup"))
    shutdown: _PhaseLatch = field(default_factory=lambda: _PhaseLatch("shutdown"))
    generation: int | None = None
    app_task: asyncio.Task[Any] | None = None
    resources_may_be_owned: bool = False
    cleanup_finished: bool = False
    cleanup_failure: BaseException | None = None
    reservation_acquired: bool = False
    reusable: bool = False
    was_quarantined: bool = False
    activated: bool = False
    accept_inner_messages: bool = True
    phase: str = "startup"
    protocol_commit: asyncio.Lock = field(default_factory=asyncio.Lock)
    protocol_failure: asyncio.Future[BaseException] = field(init=False)

    def __post_init__(self) -> None:
        self.protocol_failure = asyncio.get_running_loop().create_future()


def _owned_task_exit_failure(
    task: asyncio.Task[Any],
    *,
    clean_exit_message: str,
) -> BaseException | None:
    if not task.done():
        return None
    outcome = _task_outcome(task)
    return (
        outcome
        if outcome is not None
        else TerminalMessageViolation(clean_exit_message)
    )


class _StartupGate:
    """Process gate shared by lifespan and request scopes.

    State transitions are:

    ``idle -> starting -> active -> stopping -> stopped``

    A startup or shutdown failure settles in terminal ``failed``.  A new, real
    lifespan scope may start another serving window only after a clean
    ``stopped`` state.  A failed process must be recycled; neither requests nor
    another lifespan scope may silently adopt its uncertain resources.
    """

    __slots__ = (
        "_failure",
        "_generation",
        "_lock",
        "_owner_task",
        "_ready",
        "_state",
    )

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = "idle"
        self._generation = 0
        self._failure: BaseException | None = None
        self._owner_task: asyncio.Task[Any] | None = None
        self._ready: asyncio.Future[object] | None = None

    @property
    def state(self) -> str:
        return self._state

    async def begin_lifespan(self) -> int:
        async with self._lock:
            if self._state in {"starting", "active", "stopping", "failed"}:
                raise StartupRefused(
                    f"cannot start a new lifespan while the gate is {self._state}"
                )
            self._generation += 1
            self._state = "starting"
            self._failure = None
            self._owner_task = None
            self._ready = asyncio.get_running_loop().create_future()
            return self._generation

    async def activate(
        self,
        generation: int,
        *,
        owner_task: asyncio.Task[Any] | None = None,
    ) -> BaseException | None:
        async with self._lock:
            if generation != self._generation:
                raise StartupRefused("the lifespan ownership generation is stale")
            if self._state != "starting":
                raise RuntimeError(
                    f"cannot activate a lifespan from gate state {self._state}"
                )
            if owner_task is not None:
                failure = _owned_task_exit_failure(
                    owner_task,
                    clean_exit_message=(
                        "the inner lifespan application exited before "
                        "startup activation"
                    ),
                )
                if failure is not None:
                    return failure
            self._state = "active"
            self._owner_task = owner_task
            self._settle_waiters(None)
            return None

    async def fail(self, failure: BaseException, generation: int) -> bool:
        async with self._lock:
            if generation != self._generation:
                return False
            self._state = "failed"
            self._failure = failure
            self._owner_task = None
            self._settle_waiters(failure)
            return True

    async def poison(self, failure: BaseException, generation: int) -> bool:
        """Fail requests now without reopening the owner slot during cleanup."""

        async with self._lock:
            if generation != self._generation:
                return False
            self._failure = failure
            if self._state in {"starting", "active"}:
                self._state = "stopping"
            self._settle_waiters(failure)
            return True

    async def begin_shutdown(self, generation: int) -> bool:
        async with self._lock:
            if generation != self._generation:
                return False
            if self._state == "active":
                self._state = "stopping"
            return True

    async def finish_shutdown(
        self,
        failure: BaseException | None,
        generation: int,
    ) -> bool:
        async with self._lock:
            if generation != self._generation:
                return False
            if failure is None:
                failure = self._failure
            self._failure = failure
            self._state = "failed" if failure is not None else "stopped"
            self._owner_task = None
            return True

    def _settle_waiters(self, result: object) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_result(result)

    async def allow_request(
        self,
        validators: tuple[Hook, ...],
        *,
        lifecycle_required: bool,
    ) -> BaseException | None:
        """Return ``None`` only when request handling is permitted."""

        while True:
            owner = False
            waiter: asyncio.Future[object] | None = None
            async with self._lock:
                if self._state == "active":
                    owner_task = self._owner_task
                    if owner_task is None:
                        return None
                    owner_failure = _owned_task_exit_failure(
                        owner_task,
                        clean_exit_message=(
                            "the active inner lifespan application exited"
                        ),
                    )
                    if owner_failure is None:
                        return None
                    self._failure = owner_failure
                    self._state = "stopping"
                    self._settle_waiters(owner_failure)
                    return owner_failure
                if self._state == "starting":
                    waiter = self._ready
                elif self._state == "idle":
                    if lifecycle_required:
                        return StartupRefused(
                            "the ASGI lifespan protocol is required for "
                            "lifecycle-owned resources"
                        )
                    self._state = "starting"
                    self._ready = asyncio.get_running_loop().create_future()
                    owner = True
                else:
                    return (
                        self._failure
                        if self._failure is not None
                        else StartupRefused(
                            f"the process gate is {self._state}"
                        )
                    )

            if waiter is not None:
                result = await asyncio.shield(waiter)
                if result is _RETRY:
                    continue
                if isinstance(result, BaseException):
                    return result
                # Recheck active-owner liveness before a waiter crosses the
                # request boundary. The owner can die just after activation
                # settles this shared future.
                continue

            if not owner:  # defensive: every branch above returns, waits, or owns
                continue

            outcome = await _run_fail_fast(validators)
            owner_ready = self._ready
            owner_cancelled = asyncio.Event()

            async def commit_outcome() -> None:
                async with self._lock:
                    # The ready future identifies this exact ownership turn.
                    # No abandoned owner may settle a later retry.
                    if (
                        self._state != "starting"
                        or self._ready is not owner_ready
                    ):
                        return
                    if owner_cancelled.is_set() or _is_pure_cancellation(
                        outcome
                    ):
                        self._state = "idle"
                        self._failure = None
                        self._settle_waiters(_RETRY)
                    elif outcome is not None:
                        self._state = "failed"
                        self._failure = outcome
                        self._settle_waiters(outcome)
                    else:
                        self._state = "active"
                        self._settle_waiters(None)

            commit_task = asyncio.create_task(commit_outcome())
            cancellation: asyncio.CancelledError | None = None
            while not commit_task.done():
                try:
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError as exc:
                    # Once the slot is reserved, cancellation is not allowed to
                    # strand it. Mark the turn retryable, let the commit task
                    # reacquire the lock, and only then propagate cancellation.
                    owner_cancelled.set()
                    cancellation = exc
            commit_task.result()

            if cancellation is not None or _contains_cancellation(outcome):
                # Validation causes may carry credentials. The complete outcome
                # remains stored for redacted refusal, but cancellation crosses
                # the request boundary without its raw message or siblings.
                raise asyncio.CancelledError() from None
            return outcome


def _task_outcome(task: asyncio.Task[Any]) -> BaseException | None:
    if not task.done():
        return None
    try:
        task.result()
    except asyncio.CancelledError as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001 - inner app outcome is evidence
        return exc
    return None


async def _cancel_owned_task(
    task: asyncio.Task[Any] | None,
) -> BaseException | None:
    """Stop an owned app task without misclassifying our own cancellation.

    A buggy app may catch the first cancellation while handling a protocol
    violation. Re-issue cancellation across a few event-loop turns so that a
    single swallowed signal cannot block process cleanup forever. Python cannot
    forcibly kill a task that suppresses cancellation indefinitely; that
    non-cooperative case is returned as a terminal protocol failure. The guard
    retains the still-live task in an explicit terminal quarantine until it
    exits or the supervisor recycles the process.
    """

    if task is None:
        return None
    if task.done():
        return _task_outcome(task)

    was_already_cancelling = bool(getattr(task, "cancelling", lambda: 0)())
    caller_cancellation: asyncio.CancelledError | None = None
    for _ in range(4):
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as exc:
            if caller_cancellation is None:
                caller_cancellation = exc
        if not task.done():
            continue

        outcome = _task_outcome(task)
        if caller_cancellation is not None:
            return caller_cancellation
        if was_already_cancelling:
            return outcome if outcome is not None else asyncio.CancelledError()
        if isinstance(outcome, asyncio.CancelledError):
            return None
        return outcome

    return (
        caller_cancellation
        if caller_cancellation is not None
        else TerminalMessageViolation(
            "the owned lifespan application ignored repeated cancellation"
        )
    )


async def _wait_for_phase(
    latch: _PhaseLatch,
    app_task: asyncio.Task[Any],
    *,
    clean_exit_is_failure: bool = False,
) -> BaseException | None:
    """Wait for a terminal burst or inner-app termination, whichever comes first."""

    if not app_task.done():
        settlement = asyncio.create_task(latch.wait_until_settled())
        try:
            await asyncio.wait(
                {settlement, app_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # One additional turn lets an immediate post-send return, exception,
            # or cancellation become observable before the verdict is sealed.
            await asyncio.sleep(0)
        finally:
            if not settlement.done():
                settlement.cancel()
            try:
                await settlement
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current and getattr(current, "cancelling", lambda: 0)():
                    raise
    else:
        # Run already-scheduled call_soon settlement callbacks before freezing.
        await asyncio.sleep(0)

    latch.freeze()
    outcome = _task_outcome(app_task)
    if clean_exit_is_failure and app_task.done() and outcome is None:
        return TerminalMessageViolation(
            "the inner lifespan application exited during startup"
        )
    return outcome


async def _wait_for_shutdown_request(
    receive: Any,
    app_task: asyncio.Task[Any],
    protocol_failure: asyncio.Future[BaseException],
) -> tuple[Any | None, BaseException | None]:
    """Wait for server shutdown while continuing to supervise the inner app.

    Once startup has been accepted, the inner lifespan task is still owned by
    the guard. If it returns, raises, is cancelled, or emits a delayed protocol
    violation that the inner app catches, the process must fail closed
    immediately instead of leaving the request gate active until a shutdown
    message happens to arrive.
    """

    receive_task = asyncio.create_task(
        receive(),
        name="hivemind-lifespan-shutdown-receive",
    )
    try:
        done, _ = await asyncio.wait(
            {receive_task, app_task, protocol_failure},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if protocol_failure in done:
            return None, protocol_failure.result()
        if receive_task in done:
            return receive_task.result(), None

        failure = _task_outcome(app_task)
        if failure is None:
            failure = TerminalMessageViolation(
                "the inner lifespan application exited before server shutdown"
            )
        return None, failure
    finally:
        if not receive_task.done():
            receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current and getattr(current, "cancelling", lambda: 0)():
                raise


class LifespanGuard:
    """Outermost ASGI middleware owning one process lifespan."""

    __slots__ = (
        "app",
        "_gate",
        "_hooks",
        "_name",
        "_quarantined_tasks",
        "_redact",
        "_report",
    )

    def __init__(
        self,
        app: Any,
        *,
        name: str,
        hooks: LifespanHooks,
        redact: Callable[[str], str],
        report: Callable[[str], None],
    ) -> None:
        self.app = app
        self._name = name
        self._hooks = hooks
        self._redact = redact
        self._report = report
        self._gate = _StartupGate()
        self._quarantined_tasks: set[asyncio.Task[Any]] = set()

    def _emit_report(self, line: str) -> None:
        try:
            self._report(line)
        except BaseException:  # noqa: BLE001 - diagnostics are never load-bearing
            pass

    def _notify_reusable(self, execution: _LifespanExecution) -> None:
        """Release a reserved process window only from a positive checkpoint."""

        ownership = self._hooks.ownership
        if (
            ownership is None
            or not execution.reservation_acquired
            or not execution.reusable
        ):
            return
        # The checkpoint is one-shot even if future recovery refactors revisit
        # this execution. A failed notification deliberately retains the
        # process owner and requires recycle; it is never retried implicitly.
        execution.reusable = False
        failure = _run_sync_hook(
            ownership.release_reusable,
            role="release_reusable",
        )
        if failure is not None:
            self._emit_report(
                f"{self._name}: reusable-window notification failed "
                f"({_safe_type_name(failure, fallback='<unknown error>')}); "
                "process recycle required"
            )

    def _safe_redact(self, value: str) -> str:
        try:
            redacted = self._redact(value)
            if not isinstance(redacted, str):
                raise TypeError("redactor returned a non-string value")
            # Normalize subclasses so hostile __bool__/__str__/__format__
            # methods cannot escape after this fail-safe boundary.
            return _base_string(redacted)
        except BaseException:  # noqa: BLE001 - raw fallback could leak a secret
            self._emit_report(f"{self._name}: redaction failed; reason withheld")
            return "<redaction failed>"

    def _reason_for(self, failure: BaseException) -> str:
        return self._safe_redact(_safe_exception_text(failure))

    def _quarantine_owned_task(self, task: asyncio.Task[Any]) -> None:
        """Retain a non-cooperative task until process recycle or late exit."""

        if task.done() or task in self._quarantined_tasks:
            return
        self._quarantined_tasks.add(task)
        self._emit_report(
            f"{self._name}: owned lifespan task did not stop; "
            "process recycle required"
        )

        def release(done: asyncio.Task[Any]) -> None:
            self._quarantined_tasks.discard(done)
            _task_outcome(done)

        task.add_done_callback(release)

    async def _stop_owned_task(
        self,
        task: asyncio.Task[Any] | None,
        *,
        execution: _LifespanExecution,
    ) -> BaseException | None:
        if (
            task is not None
            and not task.done()
            and task in self._quarantined_tasks
        ):
            execution.was_quarantined = True
            # Quarantine is terminal for this process. Recovery may revisit the
            # same execution after owner cancellation, but must not re-enter a
            # non-cooperative task's cancellation handlers after cleanup.
            return TerminalMessageViolation(
                "the owned lifespan application ignored repeated cancellation"
            )
        failure = await _cancel_owned_task(task)
        if task is not None and not task.done():
            execution.was_quarantined = True
            self._quarantine_owned_task(task)
        return failure

    async def _cleanup_once(
        self,
        execution: _LifespanExecution,
    ) -> BaseException | None:
        """Run the execution's finalizers at most once across recovery paths."""

        if execution.cleanup_finished:
            return execution.cleanup_failure
        cleanup = await run_finalizers(*self._hooks.on_shutdown)
        # No await may occur between the completed cleanup and this checkpoint:
        # wrapper recovery must never replay a terminal/side-effectful closer.
        execution.cleanup_failure = cleanup
        execution.cleanup_finished = True
        return cleanup

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if _message_type(scope) == "lifespan":
            await self._run_lifespan(scope, receive, send)
            return
        await self._guard_request(scope, receive, send)

    async def _guard_request(self, scope: dict, receive: Any, send: Any) -> None:
        lifecycle_required = bool(
            self._hooks.ownership
            or self._hooks.on_startup
            or self._hooks.on_shutdown
        )
        failure = await self._gate.allow_request(
            self._hooks.on_validate,
            lifecycle_required=lifecycle_required,
        )
        if failure is not None:
            raise StartupRefused(
                f"{self._name}: startup refused ({self._reason_for(failure)})"
            ) from None
        await self.app(scope, receive, send)

    async def _emit_terminal(
        self,
        latch: _PhaseLatch,
        send: Any,
        extra_failures: list[BaseException | str],
    ) -> None:
        if latch.emitted:
            raise TerminalMessageViolation(
                f"a second terminal verdict for lifespan.{latch.phase} "
                "was suppressed"
            )

        if latch.ok and not extra_failures:
            payload = {"type": f"lifespan.{latch.phase}.complete"}
            failed = False
        else:
            reasons = [self._safe_redact(reason) for reason in latch.reasons]
            for failure in extra_failures:
                if isinstance(failure, BaseException):
                    reasons.append(self._reason_for(failure))
                else:
                    reasons.append(self._safe_redact(failure))
            if latch.terminal_count == 0 and not latch.reasons:
                reasons.append(
                    "the inner application emitted no terminal message"
                )
            message = "; ".join(reason for reason in reasons if reason)
            if not message:
                message = f"{self._name}: {latch.phase} failed"
            payload = {
                "type": f"lifespan.{latch.phase}.failed",
                "message": message,
            }
            failed = True

        # All diagnostics are rendered and redacted before the irreversible
        # seal. A hostile __str__/redactor can therefore never mark a terminal
        # as emitted without putting a terminal message on the wire.
        latch.freeze()
        latch.emitted = True
        if failed:
            self._emit_report(f"{self._name}: lifespan.{latch.phase}.failed")
        await send(payload)

    async def _rollback_startup(
        self,
        *,
        execution: _LifespanExecution,
        generation: int,
        latch: _PhaseLatch,
        send: Any,
        app_task: asyncio.Task[Any] | None,
        primary: BaseException | None,
        cleanup_required: bool,
    ) -> None:
        failures: list[BaseException | str] = []
        if primary is not None:
            failures.append(primary)

        app_failure = await self._stop_owned_task(
            app_task,
            execution=execution,
        )
        if app_failure is not None and app_failure is not primary:
            failures.append(app_failure)

        cleanup = (
            await self._cleanup_once(execution)
            if cleanup_required
            else None
        )
        if cleanup is not None:
            failures.append(cleanup)
            self._emit_report(
                f"{self._name}: startup rollback did not complete "
                f"({_safe_type_name(cleanup, fallback='<unknown error>')})"
            )

        # A child task may retain ``inner_send`` after the owned app task has
        # stopped. Keep that capability live through exhaustive cleanup, then
        # revoke it under the same commit lock used by every inner message.
        # Any violation observed before revocation is lifecycle uncertainty
        # and must retain the process-window owner.
        await asyncio.sleep(0)
        gate_settled = False
        protocol_failed = False
        async with execution.protocol_commit:
            execution.accept_inner_messages = False
            if execution.protocol_failure.done():
                protocol_failed = True
                protocol_error = execution.protocol_failure.result()
                if all(item is not protocol_error for item in failures):
                    failures.append(protocol_error)
            gate_failure = next(
                (item for item in failures if isinstance(item, BaseException)),
                StartupRefused(f"{self._name}: startup failed"),
            )
            try:
                await self._emit_terminal(latch, send, failures)
            finally:
                gate_settled = await self._gate.fail(gate_failure, generation)

        cancellation = next(
            (
                item
                for item in failures
                if isinstance(item, BaseException)
                and _contains_cancellation(item)
            ),
            None,
        )
        if (
            execution.reservation_acquired
            and gate_settled
            and not protocol_failed
            and cancellation is None
            and cleanup is None
            and not execution.was_quarantined
            and (app_task is None or app_task.done())
        ):
            # Startup did not commit, every potentially acquired resource was
            # released, and the owned task is gone. A fresh factory may try
            # again even though this guard instance remains terminal failed.
            execution.reusable = True
        if cancellation is not None:
            raise cancellation

    async def _abort_startup_commit(
        self,
        *,
        execution: _LifespanExecution,
        generation: int,
        app_task: asyncio.Task[Any],
        primary: BaseException,
    ) -> BaseException:
        """Undo acquired resources if publishing/activating startup aborts.

        The startup terminal may already have reached the server, so emitting a
        second one would itself violate ASGI.  The only truthful recovery left
        is to fail the request gate, stop the owned app task, exhaust cleanup,
        and propagate cancellation (or the commit error) to the server.
        """

        app_failure = await self._stop_owned_task(
            app_task,
            execution=execution,
        )
        cleanup = await self._cleanup_once(execution)
        failures = [
            item
            for item in (primary, app_failure, cleanup)
            if item is not None
        ]
        gate_failure = next(
            (
                item
                for item in failures
                if isinstance(item, BaseException)
                and _contains_cancellation(item)
            ),
            primary,
        )
        await self._gate.fail(gate_failure, generation)
        if cleanup is not None:
            self._emit_report(
                f"{self._name}: startup commit rollback did not complete "
                f"({_safe_type_name(cleanup, fallback='<unknown error>')})"
            )
        return gate_failure

    async def _abort_before_shutdown(
        self,
        *,
        execution: _LifespanExecution,
        generation: int,
        latch: _PhaseLatch,
        send: Any,
        app_task: asyncio.Task[Any],
        primary: BaseException,
        reason: str,
    ) -> None:
        """Fail an active process whose lifespan task dies before shutdown."""

        await self._gate.begin_shutdown(generation)
        latch.observe_violation(reason)
        app_failure = await self._stop_owned_task(
            app_task,
            execution=execution,
        )
        cleanup = await self._cleanup_once(execution)
        failures: list[BaseException | str] = [primary]
        if app_failure is not None and app_failure is not primary:
            failures.append(app_failure)
        if cleanup is not None:
            failures.append(cleanup)
            self._emit_report(
                f"{self._name}: cleanup did not complete "
                f"({_safe_type_name(cleanup, fallback='<unknown error>')})"
            )

        terminal_failure = next(
            (
                item
                for item in failures
                if isinstance(item, BaseException)
            ),
            primary,
        )
        try:
            await self._emit_terminal(latch, send, failures)
        except BaseException as exc:
            await self._gate.finish_shutdown(exc, generation)
            raise
        else:
            await self._gate.finish_shutdown(terminal_failure, generation)

        cancellation = next(
            (
                item
                for item in failures
                if isinstance(item, BaseException)
                and _contains_cancellation(item)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation

    async def _run_lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        execution = _LifespanExecution()
        try:
            await self._run_lifespan_owned(
                execution,
                scope,
                receive,
                send,
            )
        except BaseException as exc:  # noqa: BLE001 - owner must settle its slot
            lifecycle_settled = self._gate.state in {"failed", "stopped"} and (
                execution.startup.emitted or execution.shutdown.emitted
            )
            if not lifecycle_settled:
                recovery = asyncio.create_task(
                    self._recover_owned_lifespan(execution, send, exc)
                )
                while not recovery.done():
                    try:
                        await asyncio.shield(recovery)
                    except asyncio.CancelledError:
                        # Repeated caller cancellation cannot interrupt
                        # ownership recovery after slot reservation.
                        continue
                try:
                    recovery.result()
                except BaseException as recovery_error:  # noqa: BLE001
                    recovery_name = _safe_type_name(
                        recovery_error,
                        fallback="<unknown error>",
                    )
                    self._emit_report(
                        f"{self._name}: ownership recovery did not complete "
                        f"({recovery_name})"
                    )
            if _contains_cancellation(exc):
                # Never leak a mixed exception-group sibling through Uvicorn's
                # traceback path. Its full reason was retained for redacted
                # wire/report evidence before this sanitized cancellation.
                raise asyncio.CancelledError() from None
            raise
        finally:
            self._notify_reusable(execution)

    async def _recover_owned_lifespan(
        self,
        execution: _LifespanExecution,
        send: Any,
        primary: BaseException,
    ) -> None:
        """Settle every owner slot after an exception at any await boundary."""

        startup = execution.startup
        shutdown = execution.shutdown
        generation = execution.generation

        if generation is None:
            if not startup.emitted:
                if not startup.frozen:
                    startup.observe_violation(
                        "startup coordination ended before ownership began"
                    )
                await self._emit_terminal(startup, send, [primary])
            return

        # A completed terminal transition has already settled the local gate.
        # Process-window ownership is released separately, and only from the
        # positive checkpoint in ``_run_lifespan``.
        if self._gate.state in {"failed", "stopped"} and (
            startup.emitted or shutdown.emitted
        ):
            execution.accept_inner_messages = False
            return

        failures: list[BaseException | str] = [primary]
        if execution.activated:
            await self._gate.begin_shutdown(generation)

        app_failure = await self._stop_owned_task(
            execution.app_task,
            execution=execution,
        )
        if app_failure is not None and app_failure is not primary:
            failures.append(app_failure)

        if (
            execution.resources_may_be_owned
            and not execution.cleanup_finished
        ):
            await self._cleanup_once(execution)
        if execution.cleanup_failure is not None:
            failures.append(execution.cleanup_failure)
            cleanup_name = _safe_type_name(
                execution.cleanup_failure,
                fallback="<unknown error>",
            )
            self._emit_report(
                f"{self._name}: ownership recovery cleanup did not complete "
                f"({cleanup_name})"
            )

        async with execution.protocol_commit:
            execution.accept_inner_messages = False
            if execution.protocol_failure.done():
                protocol_error = execution.protocol_failure.result()
                if all(item is not protocol_error for item in failures):
                    failures.append(protocol_error)

            if execution.activated:
                if not shutdown.emitted:
                    if not shutdown.frozen:
                        shutdown.observe_violation(
                            "shutdown coordination was cancelled"
                            if _contains_cancellation(primary)
                            else "shutdown coordination failed"
                        )
                    try:
                        await self._emit_terminal(shutdown, send, failures)
                    except BaseException as send_failure:  # noqa: BLE001
                        failures.append(send_failure)
                terminal_failure = next(
                    (
                        item
                        for item in failures
                        if isinstance(item, BaseException)
                    ),
                    primary,
                )
                await self._gate.finish_shutdown(
                    terminal_failure,
                    generation,
                )
            else:
                if not startup.emitted:
                    if not startup.frozen:
                        startup.observe_violation(
                            "startup coordination was cancelled"
                            if _contains_cancellation(primary)
                            else "startup coordination failed"
                        )
                    try:
                        await self._emit_terminal(startup, send, failures)
                    except BaseException as send_failure:  # noqa: BLE001
                        failures.append(send_failure)
                gate_failure = next(
                    (
                        item
                        for item in failures
                        if isinstance(item, BaseException)
                    ),
                    primary,
                )
                await self._gate.fail(gate_failure, generation)

    async def _run_lifespan_owned(
        self,
        execution: _LifespanExecution,
        scope: dict,
        receive: Any,
        send: Any,
    ) -> None:
        startup = execution.startup
        shutdown = execution.shutdown
        app_task: asyncio.Task[Any] | None = None
        generation: int
        protocol_commit = execution.protocol_commit
        protocol_failure = execution.protocol_failure

        try:
            generation = await self._gate.begin_lifespan()
            execution.generation = generation
        except asyncio.CancelledError as exc:
            startup.observe_violation("startup was cancelled before it began")
            await self._emit_terminal(startup, send, [exc])
            raise
        except BaseException as exc:  # noqa: BLE001 - becomes startup.failed
            startup.observe_violation(_safe_exception_text(exc))
            await self._emit_terminal(startup, send, [])
            return

        try:
            first = await receive()
        except asyncio.CancelledError as exc:
            startup.observe_violation("initial lifespan receive was cancelled")
            try:
                await self._emit_terminal(startup, send, [exc])
            finally:
                await self._gate.fail(exc, generation)
            raise
        except BaseException as exc:  # noqa: BLE001 - becomes startup.failed
            startup.observe_violation("initial lifespan receive failed")
            try:
                await self._emit_terminal(startup, send, [exc])
            finally:
                await self._gate.fail(exc, generation)
            return

        raw_first_type = (
            _dict_field(first, "type") if isinstance(first, dict) else None
        )
        first_type = _message_type(first) if isinstance(first, dict) else None
        if not isinstance(first, dict) or first_type != "lifespan.startup":
            received = (
                _safe_repr(
                    raw_first_type,
                    fallback="<unprintable message type>",
                )
                if isinstance(first, dict)
                else (
                    "a non-mapping "
                    f"{_safe_type_name(first, fallback='<unknown type>')}"
                )
            )
            failure = TerminalMessageViolation(
                f"expected lifespan.startup, received {received}"
            )
            startup.observe_violation(failure)
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=None,
                primary=failure,
                cleanup_required=False,
            )
            return

        validation = await _run_fail_fast(self._hooks.on_validate)
        if validation is not None:
            startup.observe_violation(
                "startup validation failed "
                f"({_safe_type_name(validation, fallback='<unknown error>')})"
            )
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=None,
                primary=validation,
                cleanup_required=False,
            )
            return

        ownership = self._hooks.ownership
        if ownership is not None:
            reservation = _run_sync_hook(
                ownership.reserve,
                role="ownership reserve",
            )
            if reservation is not None:
                startup.observe_violation(
                    "process ownership reservation failed "
                    f"({_safe_type_name(reservation, fallback='<unknown error>')})"
                )
                await self._rollback_startup(
                    execution=execution,
                    generation=generation,
                    latch=startup,
                    send=send,
                    app_task=None,
                    primary=reservation,
                    cleanup_required=False,
                )
                return
            execution.reservation_acquired = True

        # From this point onward shutdown hooks own any partial acquisition and
        # any resources created lazily while the process is active.
        execution.resources_may_be_owned = True
        startup_work = await _run_fail_fast(self._hooks.on_startup)
        if startup_work is not None:
            startup.observe_violation(
                "resource startup failed "
                f"({_safe_type_name(startup_work, fallback='<unknown error>')})"
            )
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=None,
                primary=startup_work,
                cleanup_required=True,
            )
            return

        inbound: asyncio.Queue[dict] = asyncio.Queue()
        inbound.put_nowait(first)
        startup_delivered = False

        async def inner_receive() -> dict:
            nonlocal startup_delivered
            if (
                execution.phase == "startup"
                and startup_delivered
                and startup.terminal_count == 0
                and not startup.reasons
            ):
                startup.observe_violation(
                    "the inner application requested another lifespan message "
                    "without emitting a startup terminal"
                )
            message = await inbound.get()
            if _message_type(message) == "lifespan.startup":
                startup_delivered = True
            return message

        async def inner_send(message: Any) -> None:
            async with protocol_commit:
                if not execution.accept_inner_messages:
                    raise TerminalMessageViolation(
                        "the lifespan scope no longer accepts inner messages"
                    )
                latch = (
                    startup
                    if execution.phase == "startup"
                    else shutdown
                )
                try:
                    if not isinstance(message, dict):
                        latch.observe_violation(
                            "malformed non-mapping lifespan message"
                        )
                        return
                    raw_kind = _dict_field(message, "type")
                    kind = _message_type(message)
                    expected_prefix = f"lifespan.{execution.phase}."
                    if not isinstance(kind, str) or not kind.startswith(
                        expected_prefix
                    ):
                        latch.observe_violation(
                            "out-of-phase or malformed lifespan message: "
                            f"{_safe_repr(raw_kind, fallback='<unprintable type>')}"
                        )
                        return
                    suffix = kind[len(expected_prefix) :]
                    if suffix not in {"complete", "failed"}:
                        latch.observe_violation(
                            "non-terminal lifespan message: "
                            f"{_safe_repr(raw_kind, fallback='<unprintable type>')}"
                        )
                        return
                    latch.observe_terminal(
                        suffix,
                        _dict_field(message, "message"),
                    )
                except TerminalMessageViolation as exc:
                    # The inner app may catch this exception. Signal the owner
                    # independently so a post-seal violation still fails the
                    # gate and starts cleanup. The same lock is held by a
                    # terminal wire commit, defining whether this observation
                    # happened before or after that irreversible boundary.
                    if not protocol_failure.done():
                        protocol_failure.set_result(exc)
                    await self._gate.poison(exc, generation)
                    raise

        app_task = asyncio.create_task(
            self.app(scope, inner_receive, inner_send),
            name=f"{self._name}-lifespan-inner",
        )
        execution.app_task = app_task

        try:
            inner_startup_failure = await _wait_for_phase(
                startup,
                app_task,
                clean_exit_is_failure=True,
            )
        except asyncio.CancelledError as exc:
            startup.observe_violation("startup coordination was cancelled")
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=app_task,
                primary=exc,
                cleanup_required=True,
            )
            raise exc  # defensive if rollback ever stops re-raising

        if not startup.ok or inner_startup_failure is not None:
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=app_task,
                primary=inner_startup_failure,
                cleanup_required=True,
            )
            return

        startup_precommit_failure: BaseException | None = None
        startup_commit_failure: BaseException | None = None
        async with protocol_commit:
            if protocol_failure.done():
                startup_precommit_failure = protocol_failure.result()
            elif app_task.done():
                owner_outcome = _task_outcome(app_task)
                startup_precommit_failure = (
                    owner_outcome
                    if owner_outcome is not None
                    else TerminalMessageViolation(
                        "the inner lifespan application exited before "
                        "startup commit"
                    )
                )
            else:
                try:
                    await self._emit_terminal(startup, send, [])
                    activation_failure = await self._gate.activate(
                        generation,
                        owner_task=app_task,
                    )
                    if activation_failure is None:
                        execution.activated = True
                    elif _contains_cancellation(activation_failure):
                        startup_commit_failure = activation_failure
                    else:
                        startup_commit_failure = TerminalMessageViolation(
                            "the inner lifespan application terminated "
                            "during startup commit"
                        )
                except BaseException as exc:  # noqa: BLE001 - wire commit
                    startup_commit_failure = exc

        if startup_precommit_failure is not None:
            await self._rollback_startup(
                execution=execution,
                generation=generation,
                latch=startup,
                send=send,
                app_task=app_task,
                primary=startup_precommit_failure,
                cleanup_required=True,
            )
            return

        if startup_commit_failure is not None:
            failure = await self._abort_startup_commit(
                execution=execution,
                generation=generation,
                app_task=app_task,
                primary=startup_commit_failure,
            )
            raise failure

        try:
            shutdown_request, premature_failure = (
                await _wait_for_shutdown_request(
                    receive,
                    app_task,
                    protocol_failure,
                )
            )
        except asyncio.CancelledError as exc:
            await self._gate.begin_shutdown(generation)
            shutdown.observe_violation(
                "lifespan coordination was cancelled before shutdown"
            )
            app_failure = await self._stop_owned_task(
                app_task,
                execution=execution,
            )
            cleanup = await self._cleanup_once(execution)
            failures = [
                item for item in (exc, app_failure, cleanup) if item is not None
            ]
            await asyncio.sleep(0)
            async with protocol_commit:
                execution.accept_inner_messages = False
                if protocol_failure.done():
                    failures.append(protocol_failure.result())
                try:
                    await self._emit_terminal(shutdown, send, failures)
                finally:
                    await self._gate.finish_shutdown(exc, generation)
            raise
        except BaseException as exc:  # noqa: BLE001 - receive is process-owned
            await self._abort_before_shutdown(
                execution=execution,
                generation=generation,
                latch=shutdown,
                send=send,
                app_task=app_task,
                primary=exc,
                reason="lifespan shutdown coordination failed",
            )
            return

        if premature_failure is not None:
            await self._abort_before_shutdown(
                execution=execution,
                generation=generation,
                latch=shutdown,
                send=send,
                app_task=app_task,
                primary=premature_failure,
                reason=(
                    "the inner lifespan application terminated before "
                    "server shutdown"
                ),
            )
            return

        await self._gate.begin_shutdown(generation)
        execution.phase = "shutdown"
        raw_shutdown_type = (
            _dict_field(shutdown_request, "type")
            if isinstance(shutdown_request, dict)
            else None
        )
        shutdown_type = (
            _message_type(shutdown_request)
            if isinstance(shutdown_request, dict)
            else None
        )
        if (
            isinstance(shutdown_request, dict)
            and shutdown_type == "lifespan.shutdown"
        ):
            if not app_task.done():
                inbound.put_nowait(shutdown_request)
        else:
            shutdown_request_type_name = _safe_type_name(
                shutdown_request,
                fallback="<unknown type>",
            )
            received = (
                _safe_repr(
                    raw_shutdown_type,
                    fallback="<unprintable message type>",
                )
                if isinstance(shutdown_request, dict)
                else (
                    "None"
                    if shutdown_request is None
                    else f"a non-mapping {shutdown_request_type_name}"
                )
            )
            shutdown.observe_violation(
                f"expected lifespan.shutdown, received {received}"
            )

        try:
            inner_shutdown_failure = await _wait_for_phase(shutdown, app_task)
        except asyncio.CancelledError as exc:
            inner_shutdown_failure = exc
            if not shutdown.frozen:
                shutdown.observe_violation("shutdown coordination was cancelled")

        owned_task_failure = await self._stop_owned_task(
            app_task,
            execution=execution,
        )
        deferred = (
            inner_shutdown_failure
            if inner_shutdown_failure is not None
            else owned_task_failure
        )
        cleanup = await self._cleanup_once(execution)

        failures = [
            item for item in (deferred, cleanup) if item is not None
        ]
        # A child task can hold ``inner_send`` after the main inner task exits.
        # Keep the failure channel live through cleanup, then synchronously
        # revoke that scope capability before sealing the final verdict.
        await asyncio.sleep(0)

        if cleanup is not None:
            self._emit_report(
                f"{self._name}: cleanup did not complete "
                f"({_safe_type_name(cleanup, fallback='<unknown error>')})"
            )
        async with protocol_commit:
            execution.accept_inner_messages = False
            if protocol_failure.done():
                protocol_error = protocol_failure.result()
                if all(item is not protocol_error for item in failures):
                    failures.append(protocol_error)
            terminal_failure: BaseException | None = next(
                (
                    item
                    for item in failures
                    if isinstance(item, BaseException)
                ),
                None,
            )
            if not shutdown.ok and terminal_failure is None:
                terminal_failure = TerminalMessageViolation(
                    f"{self._name}: inner shutdown did not complete cleanly"
                )
            try:
                await self._emit_terminal(shutdown, send, failures)
            except BaseException as exc:
                await self._gate.finish_shutdown(exc, generation)
                raise
            else:
                gate_settled = await self._gate.finish_shutdown(
                    terminal_failure,
                    generation,
                )
                if (
                    execution.reservation_acquired
                    and gate_settled
                    and self._gate.state == "stopped"
                    and terminal_failure is None
                    and shutdown.ok
                    and cleanup is None
                    and not execution.was_quarantined
                    and app_task.done()
                ):
                    # The inner task is gone, exhaustive cleanup succeeded, the
                    # clean terminal reached the supervisor, and the local
                    # gate is stopped. Only now may another factory claim the
                    # process-global resources.
                    execution.reusable = True

        cancellation = next(
            (
                item
                for item in failures
                if isinstance(item, BaseException)
                and _contains_cancellation(item)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation
