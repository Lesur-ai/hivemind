# -*- coding: utf-8 -*-
"""Selective, staged mutation admission for the 1.6 source (#1420).

Source-side bridge for the 1.7 upgrade workflow. It closes mutation admission
in stages while reads stay open, and lets already admitted work settle. The
vocabulary (modes, states, status fields) deliberately mirrors the contract
merged on ``main`` by #1419 so one consumer can drive both lines.

Scope and limits, on purpose:

* One controller per *process*. Core and Graph are separate containers on
  1.6, so each owns its durable state file; a consumer composes both.
* Controlled-producer assumption: every writer of the namespace must be an
  instrumented Hivemind process. Neither this barrier nor an S3 listing can
  discover an external writer or a stolen credential.
* Ownership follows tickets, not asyncio tasks, so queued jobs and nested
  workers accepted before closure keep running to settlement.
* Uncertainty is durable. A cancelled or crashed owner becomes ``unknown`` and
  never clears by timeout, PID check, or an empty queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Order matters: a higher mode closes everything a lower mode closes.
MODES = {"background": 1, "all": 2, "cutover": 3}
# The lowest mode that refuses each kind of work.
_CLOSES_AT = {"mutation": 1, "short": 2, "read": 3}
MAX_CONTROLLERS = 16
MAX_SCOPES = 16
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
STATE_FILE = "controller.json"


class MaintenanceRefused(RuntimeError):
    """A fixed, value-free refusal. ``code`` is the only detail exposed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(eq=False)
class Ticket:
    kind: str
    scope: str
    refs: int = 1
    unknown: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


_CURRENT: ContextVar[Ticket | None] = ContextVar("selective_maintenance_ticket", default=None)


def _scope(value: Any) -> str:
    if type(value) is not str or not (value == "*" or _SCOPE_RE.match(value)):
        raise MaintenanceRefused("maintenance_input")
    return value


def validate_scope(value: Any) -> str:
    """Public alias for callers that must classify untrusted ids."""
    return _scope(value)


def _overlap(left: list[str] | str, right: list[str] | str) -> bool:
    a = {left} if isinstance(left, str) else set(left)
    b = {right} if isinstance(right, str) else set(right)
    return "*" in a or "*" in b or bool(a & b)


def _operation_id(value: Any) -> str:
    try:
        if type(value) is str and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise MaintenanceRefused("maintenance_input")


def _uncertain(error: BaseException, classify) -> bool:
    """Only an explicit settlement certificate can clear error uncertainty."""
    if classify is None:
        return True
    try:
        return classify(error) is not False
    except BaseException:
        return True


class SelectiveController:
    """Process-wide controller. All state changes happen under one RLock."""

    def __init__(self, state_dir: str | os.PathLike[str] | None) -> None:
        self._lock = threading.RLock()
        self._dir = Path(state_dir) if state_dir else None
        self._controllers: dict[str, dict[str, Any]] = {}
        # Uncertainty carried over a crash. Kept with its scopes, never cleared
        # automatically (see docs/SELECTIVE_MAINTENANCE.md for adjudication).
        self._retired_unknown: list[list[str]] = []
        self._active: dict[str, Ticket] = {}
        # False after a failed write until a later write succeeds: the on-disk
        # state is then ambiguous and must never be certified (status shows
        # recovery_required, capture_ready false), even though admission itself
        # is already held closed in memory.
        self._durable = True
        self._load()

    # ─── persistence ──────────────────────────────────────────────

    def _path(self) -> Path | None:
        return self._dir / STATE_FILE if self._dir else None

    def _persist(self, process: str, controllers: dict[str, Any] | None = None) -> None:
        path = self._path()
        if path is None:
            return
        doc = {
            "version": 1,
            "process": process,
            "controllers": self._controllers if controllers is None else controllers,
            "retired_unknown": self._retired_unknown,
        }
        tmp = path.with_name(STATE_FILE + ".tmp")
        try:
            # Inside the guard on purpose: a failed mkdir leaves the disk state
            # just as uncertain as a failed write, so it must mark us undurable.
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                # os.write may write fewer bytes than asked (quota, ENOSPC): a
                # truncated receipt must never be certified, so loop to the end
                # and treat zero progress as a failed write.
                view = memoryview(json.dumps(doc, sort_keys=True).encode())
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write while persisting maintenance state")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            dfd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            self._durable = False
            raise
        self._durable = True

    def _load(self) -> None:
        path = self._path()
        if path is None or not path.exists():
            if path is not None:
                self._persist("active")
            return
        try:
            doc = json.loads(path.read_text())
            controllers = doc["controllers"]
            retired = doc["retired_unknown"]
            process = doc["process"]
            if doc["version"] != 1 or process not in ("active", "clean"):
                raise ValueError
            for op, row in controllers.items():
                _operation_id(op)
                if row["mode"] not in MODES:
                    raise ValueError
                row["scopes"] = [_scope(s) for s in row["scopes"]]
            retired = [[_scope(s) for s in scopes] for scopes in retired]
        except (OSError, ValueError, KeyError, TypeError, MaintenanceRefused):
            # Fail closed: a corrupt controller is never silently recreated.
            raise MaintenanceRefused("maintenance_recovery_required") from None
        self._controllers = controllers
        self._retired_unknown = retired
        if process == "active":
            # The previous process did not release cleanly: its in-flight work
            # is unknown. Keep it durable, scoped to the whole deployment.
            self._retired_unknown.append(["*"])
        self._persist("active")

    def release(self) -> None:
        """Mark a graceful shutdown. Refuses to certify pending work."""
        with self._lock:
            if self._active:
                return  # stays "active": the next start inherits unknown work
            try:
                self._persist("clean")
            except OSError:
                # The receipt stays "active" on disk, so the next start
                # conservatively inherits an unknown. A shutdown hook must not
                # fail the process over a receipt it could not certify.
                pass

    # ─── admission ────────────────────────────────────────────────

    def _closed(self, kind: str, scope: str, parent: Ticket | None = None) -> bool:
        need = _CLOSES_AT[kind]
        return any(
            MODES[row["mode"]] >= need and _overlap(row["scopes"], scope)
            and not (
                parent is not None and parent.kind != "read"
                and _overlap(row["scopes"], parent.scope)
                and _CLOSES_AT[parent.kind] <= MODES[row["mode"]]
            )
            for row in self._controllers.values()
        )

    def _admit_locked(self, kind: str, scope: str, parent: Ticket | None = None) -> Ticket:
        if parent is not None:
            self._validate_ticket(parent)
        if self._closed(kind, scope, parent):
            raise MaintenanceRefused("maintenance_admission_closed")
        # Wider work always gets its actual scope, including before a pause.
        # A later controller must see it even if it does not overlap the parent.
        ticket = Ticket(kind, scope)
        self._active[ticket.id] = ticket
        return ticket

    def admit(self, kind: str, scope: str) -> Ticket:
        scope = _scope(scope)
        with self._lock:
            return self._admit_locked(kind, scope)

    def retain(self, ticket: Ticket) -> Ticket:
        with self._lock:
            self._validate_ticket(ticket)
            ticket.refs += 1
            return ticket

    def mark_unknown(self, ticket: Ticket) -> None:
        """Remember an SDK failure before a caller can catch or translate it."""
        with self._lock:
            self._validate_ticket(ticket)
            ticket.unknown = True

    def finish(self, ticket: Ticket, *, unknown: bool = False) -> None:
        with self._lock:
            self._validate_ticket(ticket)
            ticket.unknown = ticket.unknown or unknown
            ticket.refs -= 1
            if ticket.refs > 0:
                return
            self._active.pop(ticket.id, None)
            if ticket.unknown and ticket.kind != "read":
                # In memory first (the closed direction), then durable. A
                # failed write must not mask the handler's own outcome: the
                # on-disk "active" process receipt already makes the next
                # start inherit an unknown, so nothing is forgotten.
                self._retired_unknown.append([ticket.scope])
                try:
                    self._persist("active")
                except OSError:
                    pass

    @contextlib.contextmanager
    def operation(self, kind: str, scope: str) -> Iterator[Ticket]:
        """Admit work, bind it to the context, and settle it on exit."""
        ticket = self.hold(kind, scope)
        token = _CURRENT.set(ticket)
        unknown = False
        try:
            yield ticket
        except BaseException as error:  # cancellation leaves an unknown effect
            unknown = not isinstance(error, Exception)
            raise
        finally:
            _CURRENT.reset(token)
            self.finish(ticket, unknown=unknown)

    def hold(self, kind: str, scope: str) -> Ticket:
        """Keep ownership for work that outlives its request (a queued job).

        Retains the caller's ticket when there is one; otherwise admits new
        work, which the barrier may refuse. Pair with :meth:`finish`.
        """
        scope = _scope(scope)
        with self._lock:
            inherited = self._inherited(scope)
            if inherited is not None and _CLOSES_AT[inherited.kind] <= _CLOSES_AT[kind]:
                return self.retain(inherited)
            return self._admit_locked(kind, scope, _CURRENT.get())

    def _validate_ticket(self, ticket: Ticket) -> None:
        if self._active.get(ticket.id) is not ticket or ticket.refs <= 0:
            raise MaintenanceRefused("maintenance_binding")

    def _inherited(self, scope: str) -> Ticket | None:
        """Called under the lock; overlap is not scope containment."""
        ticket = _CURRENT.get()
        if ticket is None:
            return None
        self._validate_ticket(ticket)
        if ticket.kind != "read" and (ticket.scope == "*" or ticket.scope == scope):
            return ticket
        return None

    @staticmethod
    def bind(ticket: Ticket) -> None:
        """Bind a held ticket to the current task's context without a reset.

        For a long-lived worker whose context is its own; each job rebinds.
        """
        _CURRENT.set(ticket)

    @contextlib.contextmanager
    def rebind(self, ticket: Ticket) -> Iterator[Ticket]:
        """Run a retained ticket's queued work in a worker's own context."""
        token = _CURRENT.set(ticket)
        try:
            yield ticket
        finally:
            _CURRENT.reset(token)

    @staticmethod
    def current() -> Ticket | None:
        return _CURRENT.get()

    def check_physical(self, scope: str) -> None:
        """Preflight only. Dispatch must also retain a physical reference."""
        scope = _scope(scope)
        with self._lock:
            if self._inherited(scope) is None and self._closed("mutation", scope, _CURRENT.get()):
                raise MaintenanceRefused("maintenance_admission_closed")

    def acquire_physical(self, scope: str) -> Ticket:
        """Atomically retain valid mutation ownership or admit the real scope."""
        scope = _scope(scope)
        with self._lock:
            inherited = self._inherited(scope)
            return (self.retain(inherited) if inherited is not None
                    else self._admit_locked("mutation", scope, _CURRENT.get()))

    @contextlib.contextmanager
    def physical(self, scope: str, *, outcome_unknown=None) -> Iterator[Ticket]:
        """Own a synchronous/async effect through its real completion boundary.

        Exceptions after dispatch may hide an applied effect. Retire uncertainty
        before an adapter's existing error handling can swallow/translate it.
        """
        ticket = self.acquire_physical(scope)
        token = _CURRENT.set(ticket)
        unknown = False
        try:
            yield ticket
        except BaseException as error:
            unknown = _uncertain(error, outcome_unknown)
            raise
        finally:
            _CURRENT.reset(token)
            self.finish(ticket, unknown=unknown)

    async def run_physical(self, scope: str, func, /, *args, outcome_unknown=None, **kwargs):
        """Executor ownership belongs to the SDK worker, never its awaiter.

        Acquire before submission; shielding keeps queued work alive on caller
        cancellation. The worker releases its reference even when nobody awaits
        its result. It records ambiguous errors before releasing that reference.
        """
        ticket = self.acquire_physical(scope)

        def worker():
            unknown = False
            try:
                with self.rebind(ticket):
                    return func(*args, **kwargs)
            except BaseException as error:
                unknown = _uncertain(error, outcome_unknown)
                raise
            finally:
                self.finish(ticket, unknown=unknown)

        try:
            future = asyncio.get_running_loop().run_in_executor(None, worker)
        except BaseException:
            self.finish(ticket)  # no SDK request was submitted
            raise
        # Retrieve abandoned errors as well as returning them to active callers.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(future)

    # ─── operator surface ─────────────────────────────────────────

    def _unknown(self, scopes: list[str]) -> int:
        live = sum(
            1 for t in self._active.values() if t.unknown and _overlap(scopes, t.scope)
        )
        return live + sum(1 for s in self._retired_unknown if _overlap(scopes, s))

    def _status(self, operation: str) -> dict[str, Any]:
        row = self._controllers.get(operation)
        if row is None:
            raise MaintenanceRefused("maintenance_conflict")
        mode = row["mode"]
        need = {"background": 1, "all": 2, "cutover": 3}[mode]
        pending = [
            t
            for t in self._active.values()
            if _overlap(row["scopes"], t.scope)
            and (mode == "cutover" or _CLOSES_AT[t.kind] <= need)
        ]
        unknown = self._unknown(row["scopes"])
        if unknown or not self._durable:
            state = "recovery_required"
        elif pending:
            state = {"background": "background_draining", "all": "all_write_draining",
                     "cutover": "cutover_draining"}[mode]
        else:
            state = {"background": "background_paused", "all": "read_only",
                     "cutover": "exclusive_cutover"}[mode]
        return {
            "state": state,
            "mode": mode,
            "scopes": list(row["scopes"]),
            "pending": len(pending),
            "unknown": unknown,
            "producer_coverage": "verified_live",
            "capture_ready": state == "read_only",
            "reads_available": mode != "cutover",
        }

    def status(self, operation: str | None = None) -> dict[str, Any]:
        with self._lock:
            if operation is not None:
                return self._status(_operation_id(operation))
            ops = [
                {"operation_id": op, **self._status(op)} for op in self._controllers
            ]
            unknown = self._unknown(["*"])
            return {
                "state": "recovery_required" if unknown or not self._durable
                else "paused" if ops else "normal",
                "unknown": unknown,
                "producer_coverage": "verified_live",
                "operations": ops,
            }

    def pause(self, operation: str, scopes: list[str], mode: str) -> dict[str, Any]:
        operation = _operation_id(operation)
        if mode not in MODES or type(scopes) is not list or not 1 <= len(scopes) <= MAX_SCOPES:
            raise MaintenanceRefused("maintenance_input")
        scopes = sorted({_scope(s) for s in scopes})
        with self._lock:
            if self._unknown(scopes):
                # Refuse BEFORE closing anything: old uncertainty is not a
                # reason to trap a serving scope in a pause.
                raise MaintenanceRefused("maintenance_unclean")
            prior = self._controllers.get(operation)
            if prior is not None:
                if prior["scopes"] != scopes or MODES[prior["mode"]] > MODES[mode]:
                    raise MaintenanceRefused("maintenance_conflict")
            elif len(self._controllers) >= MAX_CONTROLLERS or any(
                _overlap(scopes, r["scopes"]) for r in self._controllers.values()
            ):
                raise MaintenanceRefused("maintenance_conflict")
            # Close in memory FIRST. If the write then fails the on-disk state
            # is ambiguous, so admission stays closed and the caller is told
            # recovery is required instead of getting a false "paused".
            self._controllers[operation] = {"scopes": scopes, "mode": mode}
            try:
                self._persist("active")
            except OSError:
                raise MaintenanceRefused("maintenance_recovery_required") from None
            return self._status(operation)

    def resume(self, operation: str) -> dict[str, Any]:
        operation = _operation_id(operation)
        with self._lock:
            status = self._status(operation)
            if status["pending"] or status["unknown"]:
                raise MaintenanceRefused("maintenance_unclean")
            # Persist the candidate BEFORE publishing it: a failed write leaves
            # the pause held in memory (closed), matching a retry from disk.
            candidate = {k: v for k, v in self._controllers.items() if k != operation}
            try:
                self._persist("active", candidate)
            except OSError:
                raise MaintenanceRefused("maintenance_recovery_required") from None
            self._controllers = candidate
            return {"state": "normal", "operation_id": operation}


_controller: SelectiveController | None = None
_controller_lock = threading.Lock()


def get_controller() -> SelectiveController:
    """Lazy process singleton rooted at the private maintenance directory."""
    global _controller
    with _controller_lock:
        if _controller is None:
            from ..config import get_settings

            _controller = SelectiveController(get_settings().maintenance_state_dir or None)
        return _controller


def release_if_initialized() -> None:
    """Graceful-shutdown receipt; never constructs (or re-fails on) a controller."""
    with _controller_lock:
        controller = _controller
    if controller is not None:
        controller.release()


def reset_controller_for_tests(controller: SelectiveController | None = None) -> None:
    global _controller
    with _controller_lock:
        _controller = controller
