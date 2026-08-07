# -*- coding: utf-8 -*-
"""#306 — the shared ASGI lifespan guard, proven in isolation.

Every claimed supervisor verdict here runs through uvicorn's real
``uvicorn.lifespan.on.LifespanOn``. Targeted direct calls exist only where the
server cannot inject the transport fault under test; those tests assert the
guard's coordination, wire message, gate state, cleanup, and owned-task exit or
explicit terminal quarantine, not a Uvicorn process verdict. This distinction
prevents a direct middleware probe from being presented as evidence about what
the supervisor would do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import httpx
import pytest
import uvicorn
from uvicorn.lifespan.on import LifespanOn

import hivemind_inference.asgi_lifespan as lifespan_module
from hivemind_inference.asgi_lifespan import (
    LifespanGuard,
    LifespanHooks,
    LifespanOwnership,
    StartupRefused,
    TerminalMessageViolation,
)
from hivemind_inference.process_window import (
    ProcessWindowBusy,
    ProcessWindowGate,
)


def _guard(app, **kwargs):
    kwargs.setdefault("name", "T")
    kwargs.setdefault("hooks", LifespanHooks())
    kwargs.setdefault("redact", lambda s: s)
    kwargs.setdefault("report", lambda m: None)
    return LifespanGuard(app, **kwargs)


class _RecordingLifespanOn(LifespanOn):
    def __init__(self, config):
        super().__init__(config)
        self.sent = []

    async def send(self, message):
        self.sent.append(dict(message))
        await super().send(message)


def _lifespan(app) -> _RecordingLifespanOn:
    # log_config=None: uvicorn.Config applies a dictConfig to the PROCESS, and a
    # test about shutdown verdicts must not reconfigure logging suite-wide.
    return _RecordingLifespanOn(
        uvicorn.Config(app, lifespan="auto", log_config=None)
    )


def _app(startup=("complete",), shutdown=("complete",), *, hang=False):
    """Inner ASGI app emitting a scripted terminal sequence per phase."""

    async def _inner(scope, receive, send):
        while True:
            message = await receive()
            phase = message["type"].rsplit(".", 1)[-1]
            if phase == "startup":
                for kind in startup:
                    await send({"type": f"lifespan.startup.{kind}",
                                "message": f"inner startup {kind}"})
                if hang:
                    await asyncio.Event().wait()
            elif phase == "shutdown":
                for kind in shutdown:
                    await send({"type": f"lifespan.shutdown.{kind}",
                                "message": f"inner shutdown {kind}"})
                return

    return _inner


# --------------------------------------------------------------------------- #
# Rows 1-2, 12: the process startup gate through a real server                 #
# --------------------------------------------------------------------------- #

class TestStartupGate:
    async def test_startup_complete_is_emitted_without_another_receive(self):
        """A terminal send must settle startup even when the inner app parks
        somewhere other than ``receive()`` afterwards."""
        continue_to_shutdown = asyncio.Event()

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await continue_to_shutdown.wait()
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=0.25)
        assert not state.startup_failed
        continue_to_shutdown.set()
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed

    async def test_a_valid_startup_returns_without_waiting_for_shutdown(self):
        """Row 1. The startup verdict must reach uvicorn BEFORE it may send
        shutdown. Holding it until the inner app returns deadlocks: the inner
        app is itself waiting for the shutdown message."""
        ran = []

        async def validate():
            ran.append("validated")

        state = _lifespan(
            _guard(_app(), hooks=LifespanHooks(on_validate=(validate,)))
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert not state.startup_failed and not state.should_exit
        assert ran == ["validated"]
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed

    async def test_synchronous_hooks_run_in_phase_order(self):
        events = []

        def validate():
            events.append("validate")

        def acquire():
            events.append("startup")

        def release():
            events.append("shutdown")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_validate=(validate,),
                    on_startup=(acquire,),
                    on_shutdown=(release,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert not state.startup_failed and not state.should_exit
        assert events == ["validate", "startup"]
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed and not state.should_exit
        assert events == ["validate", "startup", "shutdown"]

    async def test_a_refused_startup_stops_the_server(self):
        """Row 2."""

        async def refuse():
            raise ValueError("mixed configuration")

        state = _lifespan(
            _guard(_app(), hooks=LifespanHooks(on_validate=(refuse,)))
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed and state.should_exit

    async def test_validation_failure_never_runs_resource_startup(self):
        events = []

        async def refuse():
            events.append("validate")
            raise ValueError("mixed configuration")

        async def acquire():
            events.append("acquired")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_validate=(refuse,),
                    on_startup=(acquire,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert events == ["validate"]

    async def test_inner_startup_failure_rolls_back_without_waiting_for_shutdown(self):
        events = []

        async def acquire():
            events.append("acquired")

        async def release():
            events.append("released")

        state = _lifespan(
            _guard(
                _app(startup=("failed",), hang=True),
                hooks=LifespanHooks(
                    on_startup=(acquire,),
                    on_shutdown=(release,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=0.25)
        assert state.startup_failed
        assert events == ["acquired", "released"]

    async def test_a_second_lifespan_in_one_process_re_runs_the_gate(self):
        """Row 12. A new serving window is entitled to a fresh answer, and its
        phase latches must start clean."""
        runs = []

        async def validate():
            runs.append(1)

        guard = _guard(_app(), hooks=LifespanHooks(on_validate=(validate,)))
        for _ in range(2):
            state = _lifespan(guard)
            await asyncio.wait_for(state.startup(), timeout=2.0)
            assert not state.startup_failed and not state.error_occurred
            assert guard._gate.state == "active"
            assert [message["type"] for message in state.sent] == [
                "lifespan.startup.complete"
            ]
            await asyncio.wait_for(state.shutdown(), timeout=2.0)
            assert not state.shutdown_failed and not state.error_occurred
            assert guard._gate.state == "stopped"
            assert [message["type"] for message in state.sent] == [
                "lifespan.startup.complete",
                "lifespan.shutdown.complete",
            ]
        assert len(runs) == 2

    async def test_failed_shutdown_requires_process_recycle(self):
        closes = []

        async def close():
            closes.append(1)
            if len(closes) == 1:
                raise RuntimeError("first serving window failed to close")

        guard = _guard(_app(), hooks=LifespanHooks(on_shutdown=(close,)))
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert first.shutdown_failed

        second = _lifespan(guard)
        await asyncio.wait_for(second.startup(), timeout=2.0)
        assert second.startup_failed and second.should_exit
        assert guard._gate.state == "failed"
        assert closes == [1]

    async def test_stale_generation_cannot_poison_current_owner(self):
        gate = lifespan_module._StartupGate()
        old_generation = await gate.begin_lifespan()
        await gate.activate(old_generation)
        await gate.begin_shutdown(old_generation)
        await gate.finish_shutdown(None, old_generation)

        current_generation = await gate.begin_lifespan()
        await gate.activate(current_generation)
        assert not await gate.poison(
            RuntimeError("stale protocol failure"),
            old_generation,
        )
        assert gate.state == "active"

    async def test_stale_generation_cannot_finish_current_owner(self):
        gate = lifespan_module._StartupGate()
        old_generation = await gate.begin_lifespan()
        await gate.activate(old_generation)
        await gate.begin_shutdown(old_generation)
        await gate.finish_shutdown(None, old_generation)

        current_generation = await gate.begin_lifespan()
        await gate.activate(current_generation)
        assert not await gate.finish_shutdown(
            RuntimeError("stale cleanup failure"),
            old_generation,
        )
        assert gate.state == "active"


class TestOverlappingLifespans:
    async def test_overlap_is_refused_while_first_lifespan_is_starting(self):
        validation_entered = asyncio.Event()
        release_validation = asyncio.Event()

        async def validate():
            validation_entered.set()
            await release_validation.wait()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_validate=(validate,)),
        )
        first = _lifespan(guard)
        first_startup = asyncio.create_task(first.startup())
        await asyncio.wait_for(validation_entered.wait(), timeout=2.0)
        assert guard._gate.state == "starting"

        overlap = _lifespan(guard)
        await asyncio.wait_for(overlap.startup(), timeout=2.0)
        assert overlap.startup_failed and overlap.should_exit
        assert [message["type"] for message in overlap.sent] == [
            "lifespan.startup.failed"
        ]
        assert guard._gate.state == "starting"

        release_validation.set()
        await asyncio.wait_for(first_startup, timeout=2.0)
        assert not first.startup_failed and not first.should_exit
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert not first.shutdown_failed

    async def test_overlap_is_refused_while_first_lifespan_is_active(self):
        guard = _guard(_app())
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)
        assert guard._gate.state == "active"

        overlap = _lifespan(guard)
        await asyncio.wait_for(overlap.startup(), timeout=2.0)
        assert overlap.startup_failed and overlap.should_exit
        assert [message["type"] for message in overlap.sent] == [
            "lifespan.startup.failed"
        ]
        assert guard._gate.state == "active"

        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert not first.shutdown_failed

    async def test_overlap_is_refused_before_its_receive_can_poison_active_gate(
        self,
    ):
        """Direct seam: an overlapping scope is rejected from gate state, so
        even a broken receive belonging to that scope cannot fail the active
        serving window."""

        receive_calls = []
        overlap_sent = []
        guard = _guard(_app())
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)

        async def broken_receive():
            receive_calls.append(1)
            raise RuntimeError("overlap transport failed")

        async def record(message):
            overlap_sent.append(dict(message))

        await guard({"type": "lifespan"}, broken_receive, record)
        assert receive_calls == []
        assert guard._gate.state == "active"
        assert [message["type"] for message in overlap_sent] == [
            "lifespan.startup.failed"
        ]

        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert not first.shutdown_failed

    async def test_overlap_is_refused_while_first_lifespan_is_stopping(self):
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_calls = []

        async def cleanup():
            cleanup_calls.append(1)
            cleanup_entered.set()
            await release_cleanup.wait()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)
        first_shutdown = asyncio.create_task(first.shutdown())
        await asyncio.wait_for(cleanup_entered.wait(), timeout=2.0)
        assert guard._gate.state == "stopping"

        overlap = _lifespan(guard)
        await asyncio.wait_for(overlap.startup(), timeout=2.0)
        assert overlap.startup_failed and overlap.should_exit
        assert [message["type"] for message in overlap.sent] == [
            "lifespan.startup.failed"
        ]
        assert guard._gate.state == "stopping"
        assert cleanup_calls == [1]

        release_cleanup.set()
        await asyncio.wait_for(first_shutdown, timeout=2.0)
        assert not first.shutdown_failed
        assert guard._gate.state == "stopped"


# --------------------------------------------------------------------------- #
# Rows 3-5: the gate seen from the request path (--lifespan off)               #
# --------------------------------------------------------------------------- #

class TestLifespanOffGate:
    @staticmethod
    async def _request(guard):
        served = []

        async def app(scope, receive, send):
            served.append(scope["type"])

        guard.app = app
        await guard({"type": "http"}, None, None)
        return served

    async def test_a_startup_failure_is_sticky_across_requests(self):
        """Row 3. The draft refused the first request and served the second —
        worse than never validating, because it reports the problem once and
        then behaves as though it were fixed."""
        attempts = []

        async def refuse():
            attempts.append(1)
            raise ValueError("mixed configuration")

        guard = _guard(_app(), hooks=LifespanHooks(on_validate=(refuse,)))
        for attempt in range(2):
            with pytest.raises(StartupRefused):
                await self._request(guard)
        assert len(attempts) == 1, "the failed gate re-ran its hooks"

    async def test_successful_validation_runs_once_across_many_requests(self):
        attempts = []

        async def validate():
            attempts.append(1)

        guard = _guard(_app(), hooks=LifespanHooks(on_validate=(validate,)))
        for _ in range(3):
            assert await self._request(guard) == ["http"]
        assert attempts == [1]

    async def test_concurrent_first_requests_all_wait_for_one_result(self):
        """Row 4. A boolean let the second request reach the application while
        validation was still in flight."""
        entered = asyncio.Event()
        release = asyncio.Event()
        runs = []
        served = []

        async def slow_validate():
            runs.append(1)
            entered.set()
            await release.wait()

        async def app(scope, receive, send):
            served.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_validate=(slow_validate,)))
        first = asyncio.create_task(guard({"type": "http"}, None, None))
        await entered.wait()
        second = asyncio.create_task(guard({"type": "http"}, None, None))
        await asyncio.sleep(0)
        assert served == [], "a request reached the app before startup resolved"
        release.set()
        await asyncio.gather(first, second)
        assert len(runs) == 1, "the gate ran its hooks concurrently"
        assert served == ["http", "http"]

    async def test_concurrent_validation_failure_is_shared(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = []
        served = []

        async def refuse():
            attempts.append(1)
            entered.set()
            await release.wait()
            raise ValueError("invalid")

        async def app(scope, receive, send):
            served.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_validate=(refuse,)))
        first = asyncio.create_task(guard({"type": "http"}, None, None))
        await entered.wait()
        second = asyncio.create_task(guard({"type": "http"}, None, None))
        for _ in range(100):
            await asyncio.sleep(0)
            ready = guard._gate._ready
            if ready is not None and ready._callbacks:
                break
        else:
            pytest.fail("the concurrent request never became a gate waiter")
        assert not second.done()
        release.set()
        outcomes = await asyncio.gather(
            first,
            second,
            return_exceptions=True,
        )
        assert all(isinstance(item, StartupRefused) for item in outcomes)
        assert attempts == [1]
        assert served == []

    async def test_a_cancelled_startup_hook_leaves_the_gate_retryable(self):
        """Row 5. Nothing was decided, so nothing may be recorded: a cancelled
        deploy must not make the process permanently unstartable, and must not
        be mistaken for success."""
        runs = []

        async def cancelled_once():
            runs.append(1)
            if len(runs) == 1:
                raise asyncio.CancelledError()

        guard = _guard(_app(), hooks=LifespanHooks(on_validate=(cancelled_once,)))
        with pytest.raises(asyncio.CancelledError):
            await self._request(guard)
        await self._request(guard)  # retried, and now succeeds
        assert len(runs) == 2

    async def test_mixed_cancellation_group_is_a_sticky_validation_failure(self):
        runs = []
        served = []

        async def cancel_once():
            runs.append(1)
            if len(runs) == 1:
                raise BaseExceptionGroup(
                    "validation cancelled",
                    [RuntimeError("sibling failed"), asyncio.CancelledError()],
                )

        async def app(scope, receive, send):
            served.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_validate=(cancel_once,)))
        with pytest.raises(asyncio.CancelledError):
            await guard({"type": "http"}, None, None)
        assert guard._gate.state == "failed"
        with pytest.raises(StartupRefused, match="validation cancelled"):
            await guard({"type": "http"}, None, None)
        assert runs == [1]
        assert served == []

    async def test_pure_cancellation_group_from_validation_is_retryable(self):
        runs = []
        served = []

        async def cancel_once():
            runs.append(1)
            if len(runs) == 1:
                raise BaseExceptionGroup(
                    "validation cancelled",
                    [asyncio.CancelledError(), asyncio.CancelledError()],
                )

        async def app(scope, receive, send):
            served.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_validate=(cancel_once,)))
        with pytest.raises(asyncio.CancelledError):
            await guard({"type": "http"}, None, None)
        assert guard._gate.state == "idle"
        await guard({"type": "http"}, None, None)
        assert runs == [1, 1]
        assert served == ["http"]

    async def test_mixed_cancellation_group_never_crosses_request_boundary_raw(
        self,
    ):
        secret = "https://user:password@example.invalid"
        runs = []

        async def refuse():
            runs.append(1)
            raise BaseExceptionGroup(
                secret,
                [RuntimeError(secret), asyncio.CancelledError(secret)],
            )

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_validate=(refuse,)),
            redact=lambda _value: "<redacted>",
        )
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await guard({"type": "http"}, None, None)
        assert str(cancelled.value) == ""
        assert cancelled.value.__suppress_context__

        with pytest.raises(StartupRefused) as refused:
            await guard({"type": "http"}, None, None)
        assert secret not in str(refused.value)
        assert "<redacted>" in str(refused.value)
        assert runs == [1]

    async def test_waiter_retries_after_validation_owner_is_cancelled(self):
        """The ``_RETRY`` wake-up is an in-flight handoff, not merely a later
        serial retry: a waiter already blocked on the cancelled owner must
        become the next owner and perform validation before it can serve."""

        first_validation_entered = asyncio.Event()
        validation_runs = []
        served = []

        async def validate():
            validation_runs.append(len(validation_runs) + 1)
            if len(validation_runs) == 1:
                first_validation_entered.set()
                await asyncio.Event().wait()

        async def app(scope, receive, send):
            served.append(asyncio.current_task())

        guard = _guard(app, hooks=LifespanHooks(on_validate=(validate,)))
        owner = asyncio.create_task(
            guard({"type": "http"}, None, None),
            name="validation-owner",
        )
        await asyncio.wait_for(first_validation_entered.wait(), timeout=2.0)
        waiter = asyncio.create_task(
            guard({"type": "http"}, None, None),
            name="validation-waiter",
        )
        await asyncio.sleep(0)
        assert served == []

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await asyncio.wait_for(waiter, timeout=2.0)

        assert validation_runs == [1, 2]
        assert served == [waiter]
        assert guard._gate.state == "active"
        assert owner.done() and waiter.done()

    async def test_validation_owner_cancelled_during_commit_releases_waiter(self):
        validation_entered = asyncio.Event()
        release_validation = asyncio.Event()
        validation_runs = []
        served = []

        async def validate():
            validation_runs.append(len(validation_runs) + 1)
            if len(validation_runs) == 1:
                validation_entered.set()
                await release_validation.wait()

        async def app(scope, receive, send):
            served.append(asyncio.current_task())

        guard = _guard(app, hooks=LifespanHooks(on_validate=(validate,)))
        owner = asyncio.create_task(
            guard({"type": "http"}, None, None),
            name="commit-owner",
        )
        await asyncio.wait_for(validation_entered.wait(), timeout=2.0)
        waiter = asyncio.create_task(
            guard({"type": "http"}, None, None),
            name="commit-waiter",
        )
        for _ in range(100):
            await asyncio.sleep(0)
            ready = guard._gate._ready
            if ready is not None and ready._callbacks:
                break
        else:
            pytest.fail("the second request never became a gate waiter")

        await guard._gate._lock.acquire()
        try:
            release_validation.set()
            for _ in range(100):
                await asyncio.sleep(0)
                if getattr(owner, "_fut_waiter", None) is not None:
                    break
            else:
                pytest.fail("the validation owner never reached its commit")

            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done(), "the owner abandoned its reserved gate slot"
        finally:
            guard._gate._lock.release()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)
        await asyncio.wait_for(waiter, timeout=2.0)

        assert validation_runs == [1, 2]
        assert served == [waiter]
        assert guard._gate.state == "active"

    async def test_lifecycle_owned_resources_refuse_a_lifespan_less_deployment(self):
        """The honest `--lifespan off` decision: never ACQUIRE something no
        shutdown can ever release."""
        acquired = []
        dispatched = []

        async def acquire():
            acquired.append(1)

        async def app(scope, receive, send):
            dispatched.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_startup=(acquire,)))
        with pytest.raises(
            StartupRefused,
            match=(
                r"^T: startup refused \(the ASGI lifespan protocol is "
                r"required for lifecycle-owned resources\)$"
            ),
        ):
            await self._request(guard)
        assert acquired == [], "a resource was acquired with no release path"
        assert dispatched == []

    async def test_shutdown_only_resource_refuses_a_lifespan_less_deployment(self):
        released = []
        dispatched = []

        async def release():
            released.append(1)

        async def app(scope, receive, send):
            dispatched.append(scope["type"])

        guard = _guard(app, hooks=LifespanHooks(on_shutdown=(release,)))
        with pytest.raises(
            StartupRefused,
            match=(
                r"^T: startup refused \(the ASGI lifespan protocol is "
                r"required for lifecycle-owned resources\)$"
            ),
        ):
            await self._request(guard)
        assert released == []
        assert dispatched == []

    async def test_request_after_shutdown_is_refused(self):
        released = []

        async def release():
            released.append(1)

        guard = _guard(_app(), hooks=LifespanHooks(on_shutdown=(release,)))
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert released == [1]
        with pytest.raises(StartupRefused):
            await self._request(guard)

    async def test_real_uvicorn_refuses_sticky_failure_without_logging_secret(
        self,
        caplog,
    ):
        """The no-lifespan contract includes Uvicorn's real traceback path."""

        secret = "https://user:password@example.invalid"
        validations = []
        served = []

        async def refuse():
            validations.append(1)
            raise ValueError(secret)

        async def app(scope, receive, send):
            served.append(scope["type"])
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        guard = _guard(
            app,
            hooks=LifespanHooks(on_validate=(refuse,)),
            redact=lambda _value: "<redacted>",
        )
        config = uvicorn.Config(
            guard,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            access_log=False,
            log_config=None,
        )
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started or serving.done():
                    break
                await asyncio.sleep(0.01)
            assert server.started
            port = server.servers[0].sockets[0].getsockname()[1]
            with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
                async with httpx.AsyncClient() as client:
                    responses = [
                        await client.get(f"http://127.0.0.1:{port}/")
                        for _ in range(2)
                    ]
            assert [response.status_code for response in responses] == [500, 500]
            assert validations == [1]
            assert served == []
            assert "<redacted>" in caplog.text
            assert secret not in caplog.text
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=2.0)


# --------------------------------------------------------------------------- #
# Rows 6-11: the terminal-sequence matrix, per phase                           #
# --------------------------------------------------------------------------- #

_SEQUENCES = [
    (("complete",), False, "complete"),
    (("failed",), True, "failed"),
    (("failed", "complete"), True, "failed->complete"),
    (("complete", "failed"), True, "complete->failed"),
    (("complete", "complete"), True, "duplicate complete"),
    (("failed", "failed"), True, "duplicate failed"),
    ((), True, "silence"),
]


def _shutdown_inner_evidence(sequence: tuple[str, ...]) -> tuple[str, ...]:
    evidence = []
    if "failed" in sequence:
        evidence.append("inner shutdown failed")
    if len(sequence) > 1:
        evidence.extend(
            (
                "2 terminal messages were emitted for lifespan.shutdown",
                "duplicate terminal message for lifespan.shutdown",
            )
        )
    if not sequence:
        evidence.append("the inner application emitted no terminal message")
    return tuple(evidence)


class TestTerminalMatrix:
    @pytest.mark.parametrize(
        ("sequence", "expect_failed", "label"),
        _SEQUENCES,
        ids=[s[2] for s in _SEQUENCES],
    )
    @pytest.mark.parametrize("cleanup_kind", ("ok", "failed", "cancelled"))
    async def test_shutdown_verdict(
        self,
        sequence,
        expect_failed,
        label,
        cleanup_kind,
    ):
        """Rows 6-9, 11. A witnessed failure is never erased, a duplicate never
        improves the verdict, silence is not consent, and cleanup contributes
        to the same sticky verdict."""
        cleanup_calls = []

        async def cleanup():
            cleanup_calls.append(cleanup_kind)
            if cleanup_kind == "failed":
                raise RuntimeError("cleanup failed")
            if cleanup_kind == "cancelled":
                raise asyncio.CancelledError()

        state = _lifespan(
            _guard(
                _app(shutdown=sequence),
                hooks=LifespanHooks(on_shutdown=(cleanup,)),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert not state.startup_failed
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        expected = expect_failed or cleanup_kind != "ok"
        assert state.shutdown_failed is expected
        assert state.should_exit is expected
        assert cleanup_calls == [cleanup_kind]
        shutdown_terminals = [
            message
            for message in state.sent
            if message["type"].startswith("lifespan.shutdown.")
        ]
        assert len(shutdown_terminals) == 1
        assert shutdown_terminals[0]["type"] == (
            "lifespan.shutdown.failed"
            if expected
            else "lifespan.shutdown.complete"
        )
        terminal_message = shutdown_terminals[0].get("message", "")
        for fragment in _shutdown_inner_evidence(sequence):
            assert fragment in terminal_message, (
                f"inner evidence {fragment!r} was masked in "
                f"{label}/{cleanup_kind}: {terminal_message!r}"
            )
        cleanup_evidence = {
            "ok": (),
            "failed": ("cleanup failed",),
            "cancelled": ("CancelledError",),
        }[cleanup_kind]
        for fragment in cleanup_evidence:
            assert fragment in terminal_message, (
                f"cleanup evidence {fragment!r} was masked in "
                f"{label}/{cleanup_kind}: {terminal_message!r}"
            )
        if not expected:
            assert terminal_message == ""
        assert state.error_occurred is (cleanup_kind == "cancelled")

    @pytest.mark.parametrize(
        ("sequence", "expect_failed", "label"),
        _SEQUENCES,
        ids=[s[2] for s in _SEQUENCES],
    )
    async def test_startup_verdict(self, sequence, expect_failed, label):
        """Row 10 and the startup half of the matrix: the same sticky rule
        applies to the phase where getting it wrong makes uvicorn read
        'protocol appears unsupported' and serve on."""
        state = _lifespan(_guard(_app(startup=sequence)))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed is expect_failed
        assert state.should_exit is expect_failed
        startup_terminals = [
            message
            for message in state.sent
            if message["type"].startswith("lifespan.startup.")
        ]
        assert len(startup_terminals) == 1
        if not expect_failed:
            await asyncio.wait_for(state.shutdown(), timeout=2.0)

    async def test_a_malformed_terminal_message_is_not_a_success(self):
        """Row 9b. `lifespan.startup.weird` is not terminal. The draft treated
        anything not ending in `.failed` as success and swallowed it."""
        state = _lifespan(_guard(_app(startup=("weird",))))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed, "an unknown message counted as success"
        assert len(state.sent) == 1

    async def test_a_non_terminal_lifespan_message_fails_honestly(self):
        """A made-up message must not reach Uvicorn's assert-based classifier."""
        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.progress", "message": "half"})
            await send({"type": "lifespan.startup.complete"})

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert not state.error_occurred
        assert len(state.sent) == 1

    async def test_shutdown_terminal_before_shutdown_request_fails_startup(self):
        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            await send({"type": "lifespan.startup.complete"})

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    @pytest.mark.parametrize("phase", ("startup", "shutdown"))
    async def test_non_mapping_terminal_fails_the_observed_phase(self, phase):
        parked = asyncio.Event()

        async def inner(scope, receive, send):
            await receive()
            if phase == "startup":
                await send(None)
                await parked.wait()
            else:
                await send({"type": "lifespan.startup.complete"})
                await receive()
                await send(["lifespan.shutdown.complete"])

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        if phase == "startup":
            assert state.startup_failed and state.should_exit
            assert [message["type"] for message in state.sent] == [
                "lifespan.startup.failed"
            ]
            return

        assert not state.startup_failed and not state.should_exit
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert state.shutdown_failed and state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]


class TestReusableWindowNotification:
    """The process owner is released only on positive lifecycle evidence."""

    async def test_clean_shutdown_notifies_after_the_gate_is_stopped(self):
        observations = []
        guard = None

        def reusable():
            observations.append(guard._gate.state)

        guard = _guard(
            _app(),
            hooks=LifespanHooks(
                ownership=LifespanOwnership(lambda: None, reusable),
            ),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert observations == []

        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed
        assert observations == ["stopped"]

    async def test_fully_cleaned_startup_rollback_notifies_once(self):
        events = []

        def refuse():
            events.append("startup")
            raise RuntimeError("startup refused")

        async def cleanup():
            events.append("cleanup")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    ownership=LifespanOwnership(
                        lambda: None,
                        lambda: events.append("reusable"),
                    ),
                    on_startup=(refuse,),
                    on_shutdown=(cleanup,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed
        assert events == ["startup", "cleanup", "reusable"]

    @pytest.mark.parametrize("failure_kind", ("error", "cancelled"))
    async def test_failed_cleanup_never_notifies_reusability(
        self,
        failure_kind,
    ):
        notifications = []

        async def cleanup():
            if failure_kind == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("cleanup not confirmed")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    ownership=LifespanOwnership(
                        lambda: None,
                        lambda: notifications.append("reusable"),
                    ),
                    on_shutdown=(cleanup,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)

        assert state.shutdown_failed
        assert notifications == []

    async def test_inner_death_after_startup_never_notifies_reusability(self):
        from hivemind_inference.process_window import (
            ProcessWindowBusy,
            ProcessWindowGate,
        )

        die = asyncio.Event()
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await die.wait()
            raise RuntimeError("inner lifespan died")

        state = _lifespan(
            _guard(
                inner,
                hooks=LifespanHooks(
                    ownership=LifespanOwnership(
                        window.claim,
                        window.release,
                    ),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        die.set()
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=2.0)

        assert state.shutdown_failed
        assert gate.owner is window
        with pytest.raises(ProcessWindowBusy):
            gate.new_window().claim()

    async def test_late_child_violation_during_rollback_retains_the_owner(self):
        trigger_child = asyncio.Event()
        child_finished = asyncio.Event()
        child_tasks = []
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()

        async def inner(scope, receive, send):
            await receive()

            async def violate_during_cleanup():
                await trigger_child.wait()
                try:
                    await send({"type": "lifespan.startup.complete"})
                except TerminalMessageViolation:
                    pass
                finally:
                    child_finished.set()

            child_tasks.append(asyncio.create_task(violate_during_cleanup()))
            await send({"type": "lifespan.startup.failed"})
            await asyncio.Event().wait()

        async def cleanup():
            trigger_child.set()
            await child_finished.wait()

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                ownership=LifespanOwnership(window.claim, window.release),
                on_shutdown=(cleanup,),
            ),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed
        assert guard._gate.state == "failed"
        assert gate.owner is window
        with pytest.raises(ProcessWindowBusy):
            gate.new_window().claim()
        assert len(child_tasks) == 1 and child_tasks[0].done()

    @pytest.mark.parametrize(
        "failure_kind",
        ("raises", "awaitable", "task", "value"),
    )
    async def test_broken_reusable_callback_retains_the_owner_and_reports(
        self,
        failure_kind,
    ):
        from hivemind_inference.process_window import ProcessWindowGate

        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()
        reports = []

        if failure_kind == "raises":

            def broken_release():
                raise RuntimeError("release callback failed")

        else:

            async def release_later():
                window.release()

            def broken_release():
                return release_later()

        if failure_kind == "task":

            def broken_release():
                return asyncio.create_task(release_later())

        if failure_kind == "value":

            def broken_release():
                return "not None"

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    ownership=LifespanOwnership(
                        window.claim,
                        broken_release,
                    ),
                ),
                report=reports.append,
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        await asyncio.sleep(0)

        assert not state.shutdown_failed
        assert gate.owner is window
        assert any(
            "reusable-window notification failed" in report
            and "process recycle required" in report
            for report in reports
        )

    @pytest.mark.parametrize(
        "failure_kind",
        ("raises", "awaitable", "task", "value"),
    )
    async def test_broken_reserve_refuses_before_resources_are_ownable(
        self,
        failure_kind,
    ):
        events = []

        async def reserve_later():
            events.append("async reserve ran")

        if failure_kind == "raises":

            def broken_reserve():
                raise RuntimeError("reservation failed")

        elif failure_kind == "awaitable":

            def broken_reserve():
                return reserve_later()

        elif failure_kind == "task":

            def broken_reserve():
                return asyncio.create_task(reserve_later())

        else:

            def broken_reserve():
                return "not None"

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    ownership=LifespanOwnership(
                        broken_reserve,
                        lambda: events.append("released"),
                    ),
                    on_startup=(lambda: events.append("startup"),),
                    on_shutdown=(lambda: events.append("shutdown"),),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.sleep(0)

        assert state.startup_failed and state.should_exit
        assert events == []

    async def test_ownership_alone_requires_a_lifespan(self):
        from hivemind_inference.process_window import ProcessWindowGate

        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()
        served = []

        async def inner(scope, receive, send):
            served.append(scope["type"])

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                ownership=LifespanOwnership(window.claim, window.release),
            ),
        )
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)

        assert served == []
        assert gate.owner is None


class TestStartupRollback:
    async def test_partial_resource_startup_failure_runs_full_rollback(self):
        events = []

        async def acquire_one():
            events.append("acquire-one")

        async def acquire_two():
            events.append("acquire-two")
            raise RuntimeError("cannot acquire two")

        async def must_not_run():
            events.append("acquire-three")

        async def release_one():
            events.append("release-one")
            raise RuntimeError("release one failed")

        async def release_two():
            events.append("release-two")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_startup=(acquire_one, acquire_two, must_not_run),
                    on_shutdown=(release_one, release_two),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert events == [
            "acquire-one",
            "acquire-two",
            "release-one",
            "release-two",
        ]
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    @pytest.mark.parametrize("emit_complete", (False, True))
    async def test_clean_inner_return_during_startup_is_not_success(
        self,
        emit_complete,
    ):
        """A clean task result is still premature during startup, including
        after an inner ``startup.complete`` but before the serving window is
        committed."""

        acquired = []
        released = []
        inner_tasks = []
        request_dispatches = []
        inner_entered = asyncio.Event()
        allow_clean_return = asyncio.Event()

        async def inner(scope, receive, send):
            if scope["type"] != "lifespan":
                request_dispatches.append(scope["type"])
                return
            inner_tasks.append(asyncio.current_task())
            await receive()
            inner_entered.set()
            await allow_clean_return.wait()
            if emit_complete:
                await send({"type": "lifespan.startup.complete"})

        async def acquire():
            acquired.append(1)

        async def release():
            released.append(1)

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                on_startup=(acquire,),
                on_shutdown=(release,),
            ),
        )
        state = _lifespan(guard)
        startup = asyncio.create_task(state.startup())
        await asyncio.wait_for(inner_entered.wait(), timeout=2.0)
        assert guard._gate.state == "starting"

        request = asyncio.create_task(
            guard({"type": "http"}, None, None)
        )
        await asyncio.sleep(0)
        assert not request.done()
        assert request_dispatches == []

        allow_clean_return.set()
        await asyncio.wait_for(startup, timeout=2.0)
        with pytest.raises(StartupRefused):
            await asyncio.wait_for(request, timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert acquired == [1] and released == [1]
        assert request_dispatches == []
        assert len(inner_tasks) == 1 and inner_tasks[0].done()
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cancelled_validation_never_acquires_or_cleans_up(self):
        events = []

        async def cancel_validation():
            events.append("validate")
            raise asyncio.CancelledError()

        async def acquire():
            events.append("acquire")

        async def release():
            events.append("release")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_validate=(cancel_validation,),
                    on_startup=(acquire,),
                    on_shutdown=(release,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert state.error_occurred
        assert events == ["validate"]
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cancellation_group_from_validation_fails_wire_then_propagates(
        self,
    ):
        events = []

        async def cancel_validation():
            events.append("validate")
            raise BaseExceptionGroup(
                "grouped startup cancellation",
                [ValueError("sibling failed"), asyncio.CancelledError()],
            )

        async def acquire():
            events.append("acquire")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_validate=(cancel_validation,),
                    on_startup=(acquire,),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert state.error_occurred
        assert events == ["validate"]
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cancelled_resource_startup_rolls_back_then_propagates(self):
        events = []

        async def acquire_one():
            events.append("acquire-one")

        async def cancel_acquire_two():
            events.append("acquire-two")
            raise asyncio.CancelledError()

        async def release_one():
            events.append("release-one")
            raise asyncio.CancelledError()

        async def release_two():
            events.append("release-two")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_startup=(acquire_one, cancel_acquire_two),
                    on_shutdown=(release_one, release_two),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert state.error_occurred
        assert events == [
            "acquire-one",
            "acquire-two",
            "release-one",
            "release-two",
        ]

    async def test_inner_cancel_after_complete_changes_startup_to_failed(self):
        released = []

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            raise asyncio.CancelledError()

        async def release():
            released.append(1)

        state = _lifespan(
            _guard(
                inner,
                hooks=LifespanHooks(on_shutdown=(release,)),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert state.startup_failed
        assert state.error_occurred
        assert released == [1]
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cooperative_failed_inner_startup_exits_owned_task(self):
        owned = []

        async def inner(scope, receive, send):
            owned.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.failed"})
            await asyncio.Event().wait()

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.sleep(0)
        assert len(owned) == 1
        assert owned[0] is not None and owned[0].done()

    async def test_non_cooperative_inner_task_is_explicitly_quarantined(
        self,
    ):
        from hivemind_inference.process_window import (
            ProcessWindowBusy,
            ProcessWindowGate,
        )

        owned = []
        cancellations = []
        reports = []
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()
        release = asyncio.Event()

        async def inner(scope, receive, send):
            owned.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.failed"})
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations.append(1)
                    continue

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                ownership=LifespanOwnership(
                    window.claim,
                    window.release,
                ),
            ),
            report=reports.append,
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        try:
            assert state.startup_failed and state.should_exit
            assert guard._gate.state == "failed"
            assert len(owned) == 1 and not owned[0].done()
            assert cancellations == [1, 1, 1, 1]
            assert owned[0] in guard._quarantined_tasks
            assert gate.owner is window
            with pytest.raises(ProcessWindowBusy):
                gate.new_window().claim()
            assert any(
                "owned lifespan task did not stop" in report
                and "process recycle required" in report
                for report in reports
            )
            with pytest.raises(StartupRefused):
                await guard({"type": "http"}, None, None)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(owned[0]), timeout=2.0)
            await asyncio.sleep(0)
        assert owned[0] not in guard._quarantined_tasks
        assert gate.owner is window

    async def test_external_startup_cancellation_rolls_back_then_re_raises(self):
        entered = asyncio.Event()
        released = []
        sent = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})

        async def inner(scope, receive, send):
            await receive()
            entered.set()
            await asyncio.Event().wait()

        async def release():
            released.append(1)

        async def record(message):
            sent.append(message)

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(release,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, record)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == [1]
        assert [message["type"] for message in sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cancellation_while_publishing_startup_rolls_back(self):
        """Cancellation after the server sees complete must still close every
        acquired resource and permanently refuse requests."""

        publish_entered = asyncio.Event()
        released = []
        sent = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await asyncio.Event().wait()

        async def release():
            released.append(1)

        async def blocking_send(message):
            sent.append(dict(message))
            publish_entered.set()
            await asyncio.Event().wait()

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(release,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, blocking_send)
        )
        await asyncio.wait_for(publish_entered.wait(), timeout=2.0)
        owned = [
            candidate
            for candidate in asyncio.all_tasks()
            if candidate.get_name() == "T-lifespan-inner"
            and not candidate.done()
        ]
        assert len(owned) == 1
        owned_task = owned[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert released == [1]
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete"
        ]
        assert guard._gate.state == "failed"
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)
        assert owned_task.done()

    async def test_startup_wire_commit_serializes_a_concurrent_violation(self):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        publish_entered = asyncio.Event()
        release_publish = asyncio.Event()
        trigger_duplicate = asyncio.Event()
        duplicate_was_swallowed = asyncio.Event()
        cleanup_calls = []
        sent = []

        async def inner(scope, receive, send):
            await receive()

            async def violate_after_seal():
                await trigger_duplicate.wait()
                try:
                    await send({"type": "lifespan.startup.complete"})
                except TerminalMessageViolation:
                    duplicate_was_swallowed.set()

            asyncio.create_task(violate_after_seal())
            await send({"type": "lifespan.startup.complete"})
            await asyncio.Event().wait()

        async def cleanup():
            cleanup_calls.append(1)

        async def blocking_send(message):
            sent.append(dict(message))
            publish_entered.set()
            await release_publish.wait()

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, blocking_send)
        )
        await asyncio.wait_for(publish_entered.wait(), timeout=2.0)

        trigger_duplicate.set()
        await asyncio.sleep(0)
        assert not duplicate_was_swallowed.is_set()
        assert guard._gate.state == "starting"

        overlap = _lifespan(guard)
        await asyncio.wait_for(overlap.startup(), timeout=2.0)
        assert overlap.startup_failed and overlap.should_exit

        release_publish.set()
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_post_snapshot_startup_violation_fails_before_wire_commit(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_phase
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        trigger_duplicate = asyncio.Event()
        duplicate_was_swallowed = asyncio.Event()

        async def pause_after_startup_snapshot(latch, app_task, **kwargs):
            result = await original_wait(latch, app_task, **kwargs)
            if latch.phase == "startup":
                snapshot_taken.set()
                await release_snapshot.wait()
            return result

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_phase",
            pause_after_startup_snapshot,
        )

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await trigger_duplicate.wait()
            try:
                await send({"type": "lifespan.startup.complete"})
            except TerminalMessageViolation:
                duplicate_was_swallowed.set()
            await asyncio.Event().wait()

        guard = _guard(inner)
        state = _lifespan(guard)
        starting = asyncio.create_task(state.startup())
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)

        trigger_duplicate.set()
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)
        assert guard._gate.state == "stopping"
        assert state.sent == []

        release_snapshot.set()
        await asyncio.wait_for(starting, timeout=2.0)
        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    @pytest.mark.parametrize(
        "death_kind",
        ("return", "exception", "cancelled"),
    )
    async def test_post_snapshot_inner_death_never_activates_requests(
        self,
        monkeypatch,
        death_kind,
    ):
        original_wait = lifespan_module._wait_for_phase
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        terminate_inner = asyncio.Event()
        inner_tasks = []
        served = []
        cleanup_calls = []

        async def pause_after_startup_snapshot(latch, app_task, **kwargs):
            result = await original_wait(latch, app_task, **kwargs)
            if latch.phase == "startup":
                snapshot_taken.set()
                await release_snapshot.wait()
            return result

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_phase",
            pause_after_startup_snapshot,
        )

        async def app(scope, receive, send):
            if scope["type"] == "http":
                served.append("http")
                return
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await terminate_inner.wait()
            if death_kind == "exception":
                raise RuntimeError("inner died before startup commit")
            if death_kind == "cancelled":
                raise asyncio.CancelledError()

        async def cleanup():
            cleanup_calls.append(1)

        guard = _guard(
            app,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        state = _lifespan(guard)
        starting = asyncio.create_task(state.startup())
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)

        terminate_inner.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if inner_tasks[0].done():
                break
        else:
            pytest.fail("the inner lifespan task never terminated")

        async def request():
            try:
                await guard({"type": "http"}, None, None)
            except StartupRefused:
                return "refused"
            return "served"

        request_task = asyncio.create_task(request())
        await asyncio.sleep(0)
        assert not request_task.done()

        release_snapshot.set()
        await asyncio.wait_for(starting, timeout=2.0)
        assert await asyncio.wait_for(request_task, timeout=2.0) == "refused"
        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert cleanup_calls == [1]
        assert served == []
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]

    @pytest.mark.parametrize(
        "death_kind",
        ("return", "exception", "cancelled"),
    )
    async def test_inner_death_during_startup_send_never_activates_requests(
        self,
        death_kind,
    ):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        publish_entered = asyncio.Event()
        release_publish = asyncio.Event()
        terminate_inner = asyncio.Event()
        inner_tasks = []
        cleanup_calls = []
        served = []
        sent = []

        async def app(scope, receive, send):
            if scope["type"] == "http":
                served.append("http")
                return
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await terminate_inner.wait()
            if death_kind == "exception":
                raise RuntimeError("inner died during startup send")
            if death_kind == "cancelled":
                raise asyncio.CancelledError()

        async def cleanup():
            cleanup_calls.append(1)

        async def blocking_send(message):
            sent.append(dict(message))
            if message["type"] == "lifespan.startup.complete":
                publish_entered.set()
                await release_publish.wait()

        guard = _guard(
            app,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, blocking_send)
        )
        await asyncio.wait_for(publish_entered.wait(), timeout=2.0)

        terminate_inner.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if inner_tasks[0].done():
                break
        else:
            pytest.fail("the inner lifespan task never terminated")

        async def request():
            try:
                await guard({"type": "http"}, None, None)
            except StartupRefused:
                return "refused"
            return "served"

        request_task = asyncio.create_task(request())
        await asyncio.sleep(0)
        assert not request_task.done()

        release_publish.set()
        expected = (
            asyncio.CancelledError
            if death_kind == "cancelled"
            else TerminalMessageViolation
        )
        with pytest.raises(expected):
            await asyncio.wait_for(owner, timeout=2.0)
        assert await asyncio.wait_for(request_task, timeout=2.0) == "refused"
        assert guard._gate.state == "failed"
        assert cleanup_calls == [1]
        assert served == []
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete"
        ]

    async def test_post_activation_owner_death_refuses_new_request(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_shutdown_request
        supervision_paused = asyncio.Event()
        release_supervision = asyncio.Event()
        terminate_inner = asyncio.Event()
        inner_tasks = []
        cleanup_calls = []
        served = []

        async def pause_supervision(receive, app_task, protocol_failure):
            supervision_paused.set()
            await release_supervision.wait()
            return await original_wait(
                receive,
                app_task,
                protocol_failure,
            )

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_shutdown_request",
            pause_supervision,
        )

        async def app(scope, receive, send):
            if scope["type"] == "http":
                served.append("http")
                return
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await terminate_inner.wait()

        async def cleanup():
            cleanup_calls.append(1)

        guard = _guard(
            app,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(supervision_paused.wait(), timeout=2.0)
        assert guard._gate.state == "active"

        terminate_inner.set()
        await asyncio.wait_for(
            asyncio.shield(inner_tasks[0]),
            timeout=2.0,
        )
        with pytest.raises(
            StartupRefused,
            match="active inner lifespan application exited",
        ):
            await guard({"type": "http"}, None, None)
        assert guard._gate.state == "stopping"
        assert served == []

        release_supervision.set()
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=2.0)
        assert state.shutdown_failed
        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"

    async def test_startup_waiter_rechecks_owner_after_activation(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_phase
        original_settle = lifespan_module._StartupGate._settle_waiters
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        terminate_inner = asyncio.Event()
        inner_tasks = []
        cleanup_calls = []
        served = []

        async def pause_after_startup_snapshot(latch, app_task, **kwargs):
            result = await original_wait(latch, app_task, **kwargs)
            if latch.phase == "startup":
                snapshot_taken.set()
                await release_snapshot.wait()
            return result

        def terminate_before_success_waiters_run(gate, result):
            if result is None and gate.state == "active":
                terminate_inner.set()
            original_settle(gate, result)

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_phase",
            pause_after_startup_snapshot,
        )
        monkeypatch.setattr(
            lifespan_module._StartupGate,
            "_settle_waiters",
            terminate_before_success_waiters_run,
        )

        async def app(scope, receive, send):
            if scope["type"] == "http":
                served.append("http")
                return
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await terminate_inner.wait()

        async def cleanup():
            cleanup_calls.append(1)

        guard = _guard(
            app,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        state = _lifespan(guard)
        starting = asyncio.create_task(state.startup())
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)

        async def request():
            try:
                await guard({"type": "http"}, None, None)
            except StartupRefused:
                return "refused"
            return "served"

        request_task = asyncio.create_task(request())
        await asyncio.sleep(0)
        assert not request_task.done()

        release_snapshot.set()
        await asyncio.wait_for(starting, timeout=2.0)
        assert await asyncio.wait_for(request_task, timeout=2.0) == "refused"
        assert served == []
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=2.0)
        assert state.shutdown_failed
        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert inner_tasks[0].done()

    @pytest.mark.parametrize("cleanup_kind", ("ok", "failed", "cancelled"))
    async def test_startup_publish_error_exhausts_cleanup_without_second_terminal(
        self,
        cleanup_kind,
    ):
        """The outer send is the startup commit point. If it raises, cleanup
        remains exhaustive, cancellation keeps priority, and a speculative
        startup.failed must not follow the terminal that may already be on the
        wire."""

        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        sent = []
        cleanup_events = []
        inner_exited = asyncio.Event()
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()

        async def inner(scope, receive, send):
            try:
                await receive()
                await send({"type": "lifespan.startup.complete"})
                await asyncio.Event().wait()
            finally:
                inner_exited.set()

        async def first_cleanup():
            cleanup_events.append(f"first-{cleanup_kind}")
            if cleanup_kind == "failed":
                raise RuntimeError("cleanup failed")
            if cleanup_kind == "cancelled":
                raise asyncio.CancelledError()

        async def second_cleanup():
            cleanup_events.append("second-ran")

        async def outer_send(message):
            sent.append(dict(message))
            if message["type"] == "lifespan.startup.complete":
                raise RuntimeError("outer startup send failed")

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                ownership=LifespanOwnership(window.claim, window.release),
                on_shutdown=(first_cleanup, second_cleanup),
            ),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, outer_send)
        )
        if cleanup_kind == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
        else:
            with pytest.raises(RuntimeError, match="outer startup send failed"):
                await asyncio.wait_for(task, timeout=2.0)

        await asyncio.wait_for(inner_exited.wait(), timeout=2.0)
        assert cleanup_events == [f"first-{cleanup_kind}", "second-ran"]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete"
        ]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)
        assert gate.owner is window
        with pytest.raises(ProcessWindowBusy):
            gate.new_window().claim()

    async def test_startup_rollback_wire_failure_retains_process_window(self):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        sent = []
        cleanup_calls = []
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()

        def refuse_startup():
            raise RuntimeError("resource startup refused")

        async def cleanup():
            cleanup_calls.append(1)

        async def fail_terminal(message):
            sent.append(dict(message))
            if message["type"] == "lifespan.startup.failed":
                raise RuntimeError("startup rollback wire failed")

        guard = _guard(
            _app(),
            hooks=LifespanHooks(
                ownership=LifespanOwnership(window.claim, window.release),
                on_startup=(refuse_startup,),
                on_shutdown=(cleanup,),
            ),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, fail_terminal)
        )
        with pytest.raises(RuntimeError, match="startup rollback wire failed"):
            await asyncio.wait_for(task, timeout=2.0)

        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.failed"
        ]
        assert gate.owner is window
        with pytest.raises(ProcessWindowBusy):
            gate.new_window().claim()

    async def test_startup_commit_abort_helper_releases_its_owner_directly(self):
        cleanup_calls = []
        inner_started = asyncio.Event()

        async def cleanup():
            cleanup_calls.append(1)

        async def parked_inner():
            inner_started.set()
            await asyncio.Event().wait()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        generation = await guard._gate.begin_lifespan()
        app_task = asyncio.create_task(parked_inner())
        execution = lifespan_module._LifespanExecution()
        execution.generation = generation
        execution.app_task = app_task
        execution.resources_may_be_owned = True
        await asyncio.wait_for(inner_started.wait(), timeout=2.0)
        primary = RuntimeError("startup wire commit failed")

        returned = await guard._abort_startup_commit(
            execution=execution,
            generation=generation,
            app_task=app_task,
            primary=primary,
        )

        assert returned is primary
        assert cleanup_calls == [1]
        assert app_task.done()
        assert guard._gate.state == "failed"

    async def test_cancellation_after_cleanup_never_retries_finalizers(
        self,
    ):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        cleanup_checkpoint = asyncio.Event()
        cleanup_calls = []
        inner_tasks = []

        async def inner(scope, receive, send):
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await asyncio.Event().wait()

        guard = None

        async def cleanup():
            cleanup_calls.append(len(cleanup_calls) + 1)
            if len(cleanup_calls) == 1:
                assert guard is not None
                await guard._gate._lock.acquire()
                cleanup_checkpoint.set()

        async def fail_startup_send(message):
            if message["type"] == "lifespan.startup.complete":
                raise RuntimeError("startup wire failed")

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, fail_startup_send)
        )
        await asyncio.wait_for(cleanup_checkpoint.wait(), timeout=2.0)
        try:
            for _ in range(100):
                await asyncio.sleep(0)
                if getattr(owner, "_fut_waiter", None) is not None:
                    break
            else:
                pytest.fail("the owner never reached the gate commit")
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done()
        finally:
            guard._gate._lock.release()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)
        assert cleanup_calls == [1]
        assert len(inner_tasks) == 1 and inner_tasks[0].done()
        assert guard._gate.state == "failed"

    async def test_recovery_never_recancels_a_quarantined_owned_task(self):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        cleanup_checkpoint = asyncio.Event()
        release_inner = asyncio.Event()
        cleanup_calls = []
        cancellations = []
        inner_tasks = []
        guard = None

        async def inner(scope, receive, send):
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            while not release_inner.is_set():
                try:
                    await release_inner.wait()
                except asyncio.CancelledError:
                    cancellations.append(1)

        async def cleanup():
            cleanup_calls.append(1)
            assert guard is not None
            await guard._gate._lock.acquire()
            cleanup_checkpoint.set()

        async def fail_startup_send(message):
            if message["type"] == "lifespan.startup.complete":
                raise RuntimeError("startup wire failed")

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, fail_startup_send)
        )
        await asyncio.wait_for(cleanup_checkpoint.wait(), timeout=2.0)

        inner_task = inner_tasks[0]
        before_recovery = list(cancellations)
        quarantined_before_recovery = (
            inner_task in guard._quarantined_tasks
        )
        owner_outcome = None
        try:
            for _ in range(100):
                await asyncio.sleep(0)
                if getattr(owner, "_fut_waiter", None) is not None:
                    break
            else:
                pytest.fail("the owner never reached the failed gate commit")

            owner.cancel()
            for _ in range(20):
                await asyncio.sleep(0)

            assert cancellations == [1, 1, 1, 1]
            assert inner_task in guard._quarantined_tasks
            assert cleanup_calls == [1]
        finally:
            if guard._gate._lock.locked():
                guard._gate._lock.release()
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=2.0)
            except BaseException as exc:  # noqa: BLE001 - assertion evidence
                owner_outcome = exc
            release_inner.set()
            await asyncio.wait_for(asyncio.shield(inner_task), timeout=2.0)
            await asyncio.sleep(0)

        assert cleanup_calls == [1]
        assert len(inner_tasks) == 1
        assert before_recovery == [1, 1, 1, 1]
        assert quarantined_before_recovery
        assert isinstance(owner_outcome, asyncio.CancelledError)
        assert guard._gate.state == "failed"
        assert inner_task not in guard._quarantined_tasks


class TestShutdownCoordination:
    @pytest.mark.parametrize(
        "failure_kind",
        ("delayed-duplicate", "exception", "clean-return"),
    )
    async def test_inner_app_death_after_startup_fails_gate_immediately(
        self,
        failure_kind,
    ):
        trigger = asyncio.Event()
        released = asyncio.Event()

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await trigger.wait()
            if failure_kind == "delayed-duplicate":
                await send({"type": "lifespan.startup.failed"})
            elif failure_kind == "exception":
                raise RuntimeError("delayed lifespan failure")
            else:
                return

        async def release():
            released.set()

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(release,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert guard._gate.state == "active"

        trigger.set()
        await asyncio.wait_for(released.wait(), timeout=2.0)
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=2.0)

        assert guard._gate.state == "failed"
        assert state.shutdown_failed
        # Uvicorn records an unsolicited shutdown.failed but does not set
        # should_exit until its own shutdown() path runs. The guard therefore
        # fail-closes requests; the supervisor must recycle the listening
        # process (the documented residual).
        assert not state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)

    async def test_post_seal_duplicate_cannot_be_swallowed_by_inner_app(self):
        trigger_duplicate = asyncio.Event()
        duplicate_was_swallowed = asyncio.Event()
        cleanup_finished = asyncio.Event()
        inner_exited = asyncio.Event()
        cleanup_calls = []

        async def inner(scope, receive, send):
            try:
                await receive()
                await send({"type": "lifespan.startup.complete"})
                await trigger_duplicate.wait()
                try:
                    await send({"type": "lifespan.startup.complete"})
                except BaseException:
                    duplicate_was_swallowed.set()
                await asyncio.Event().wait()
            finally:
                inner_exited.set()

        async def cleanup():
            cleanup_calls.append(1)
            cleanup_finished.set()

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert guard._gate.state == "active"

        trigger_duplicate.set()
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)
        await asyncio.wait_for(cleanup_finished.wait(), timeout=2.0)
        await asyncio.wait_for(inner_exited.wait(), timeout=2.0)
        await asyncio.wait_for(state.shutdown_event.wait(), timeout=2.0)

        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert state.shutdown_failed and not state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)

    async def test_post_seal_failure_keeps_owner_slot_through_cleanup(self):
        trigger_duplicate = asyncio.Event()
        duplicate_was_swallowed = asyncio.Event()
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        validations = []

        async def validate():
            validations.append(1)

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await trigger_duplicate.wait()
            try:
                await send({"type": "lifespan.startup.complete"})
            except TerminalMessageViolation:
                duplicate_was_swallowed.set()
            await asyncio.Event().wait()

        async def cleanup():
            cleanup_entered.set()
            await release_cleanup.wait()

        guard = _guard(
            inner,
            hooks=LifespanHooks(
                on_validate=(validate,),
                on_shutdown=(cleanup,),
            ),
        )
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)

        trigger_duplicate.set()
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)
        await asyncio.wait_for(cleanup_entered.wait(), timeout=2.0)
        assert guard._gate.state == "stopping"

        overlap = _lifespan(guard)
        await asyncio.wait_for(overlap.startup(), timeout=2.0)
        assert overlap.startup_failed and overlap.should_exit
        assert guard._gate.state == "stopping"
        assert validations == [1]

        release_cleanup.set()
        await asyncio.wait_for(first.shutdown_event.wait(), timeout=2.0)
        assert first.shutdown_failed
        assert guard._gate.state == "failed"

        after_failure = _lifespan(guard)
        await asyncio.wait_for(after_failure.startup(), timeout=2.0)
        assert after_failure.startup_failed and after_failure.should_exit
        assert validations == [1]

    async def test_post_snapshot_duplicate_still_fails_shutdown(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_shutdown_request
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        trigger_duplicate = asyncio.Event()
        duplicate_was_swallowed = asyncio.Event()

        async def pause_after_snapshot(receive, app_task, protocol_failure):
            result = await original_wait(
                receive,
                app_task,
                protocol_failure,
            )
            snapshot_taken.set()
            await release_snapshot.wait()
            return result

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_shutdown_request",
            pause_after_snapshot,
        )

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await trigger_duplicate.wait()
            try:
                await send({"type": "lifespan.startup.complete"})
            except TerminalMessageViolation:
                duplicate_was_swallowed.set()
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        guard = _guard(inner)
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        stopping = asyncio.create_task(state.shutdown())
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)

        trigger_duplicate.set()
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)
        release_snapshot.set()
        await asyncio.wait_for(stopping, timeout=2.0)

        assert state.shutdown_failed
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_duplicate_during_post_terminal_cancellation_fails_shutdown(
        self,
    ):
        duplicate_was_swallowed = asyncio.Event()

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                try:
                    await send({"type": "lifespan.shutdown.complete"})
                except TerminalMessageViolation:
                    duplicate_was_swallowed.set()

        guard = _guard(inner)
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        await asyncio.wait_for(duplicate_was_swallowed.wait(), timeout=2.0)

        assert state.shutdown_failed
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_stale_send_capability_cannot_poison_new_generation(self):
        sends = []

        async def inner(scope, receive, send):
            sends.append(send)
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        guard = _guard(inner)
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert guard._gate.state == "stopped"

        second = _lifespan(guard)
        await asyncio.wait_for(second.startup(), timeout=2.0)
        assert guard._gate.state == "active"

        with pytest.raises(
            TerminalMessageViolation,
            match="no longer accepts inner messages",
        ):
            await sends[0]({"type": "lifespan.shutdown.complete"})
        assert guard._gate.state == "active"

        await asyncio.wait_for(second.shutdown(), timeout=2.0)
        assert not second.shutdown_failed
        assert guard._gate.state == "stopped"

    @pytest.mark.parametrize("failure_kind", ("error", "cancelled"))
    async def test_shutdown_wire_failure_is_a_terminal_gate_failure(
        self,
        failure_kind,
    ):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        inbound.put_nowait({"type": "lifespan.shutdown"})
        sent = []
        cleanup_calls = []
        gate = ProcessWindowGate(service="Test")
        window = gate.new_window()

        async def cleanup():
            cleanup_calls.append(1)

        async def outer_send(message):
            sent.append(dict(message))
            if message["type"] == "lifespan.shutdown.complete":
                if failure_kind == "cancelled":
                    raise asyncio.CancelledError()
                raise RuntimeError("shutdown wire failed")

        guard = _guard(
            _app(),
            hooks=LifespanHooks(
                ownership=LifespanOwnership(window.claim, window.release),
                on_shutdown=(cleanup,),
            ),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, outer_send)
        )
        expected = (
            asyncio.CancelledError
            if failure_kind == "cancelled"
            else RuntimeError
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=2.0)

        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
        ]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)
        assert gate.owner is window
        with pytest.raises(ProcessWindowBusy):
            gate.new_window().claim()

    async def test_owned_shutdown_path_settles_gate_before_wrapper_recovery(self):
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        inbound.put_nowait({"type": "lifespan.shutdown"})
        sent = []

        async def outer_send(message):
            sent.append(dict(message))
            if message["type"] == "lifespan.shutdown.complete":
                raise RuntimeError("shutdown wire failed")

        guard = _guard(_app())
        execution = lifespan_module._LifespanExecution()
        with pytest.raises(RuntimeError, match="shutdown wire failed"):
            await guard._run_lifespan_owned(
                execution,
                {"type": "lifespan"},
                inbound.get,
                outer_send,
            )

        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
        ]

    async def test_cancellation_while_beginning_shutdown_recovers_owner(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_shutdown_request
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        sent = []
        cleanup_calls = []
        inner_tasks = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        inbound.put_nowait({"type": "lifespan.shutdown"})

        async def pause_after_shutdown_snapshot(
            receive,
            app_task,
            protocol_failure,
        ):
            result = await original_wait(
                receive,
                app_task,
                protocol_failure,
            )
            snapshot_taken.set()
            await release_snapshot.wait()
            return result

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_shutdown_request",
            pause_after_shutdown_snapshot,
        )

        async def inner(scope, receive, send):
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()

        async def cleanup():
            cleanup_calls.append(1)

        async def record(message):
            sent.append(dict(message))

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, record)
        )
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)
        assert guard._gate.state == "active"

        await guard._gate._lock.acquire()
        try:
            release_snapshot.set()
            for _ in range(100):
                await asyncio.sleep(0)
                if getattr(owner, "_fut_waiter", None) is not None:
                    break
            else:
                pytest.fail("the lifespan owner never reached begin_shutdown")
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done()
        finally:
            guard._gate._lock.release()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)
        assert cleanup_calls == [1]
        assert len(inner_tasks) == 1 and inner_tasks[0].done()
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_cancellation_after_cleanup_still_commits_failed_shutdown(
        self,
        monkeypatch,
    ):
        original_finalizers = lifespan_module.run_finalizers
        cleanup_calls = []
        sent = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})
        inbound.put_nowait({"type": "lifespan.shutdown"})
        inject_once = True

        async def inject_cancellation_after_cleanup(*steps):
            nonlocal inject_once
            outcome = await original_finalizers(*steps)
            if inject_once:
                inject_once = False
                current = asyncio.current_task()
                assert current is not None
                asyncio.get_running_loop().call_soon(current.cancel)
            return outcome

        monkeypatch.setattr(
            lifespan_module,
            "run_finalizers",
            inject_cancellation_after_cleanup,
        )

        async def cleanup():
            cleanup_calls.append(1)

        async def record(message):
            sent.append(dict(message))

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, record)
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)

        assert cleanup_calls == [1]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_cancellation_waiting_for_startup_commit_recovers_owner(
        self,
        monkeypatch,
    ):
        original_wait = lifespan_module._wait_for_phase
        snapshot_taken = asyncio.Event()
        release_snapshot = asyncio.Event()
        trigger_duplicate = asyncio.Event()
        duplicate_started = asyncio.Event()
        sent = []
        cleanup_calls = []
        inner_tasks = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})

        async def pause_after_startup_snapshot(latch, app_task, **kwargs):
            result = await original_wait(latch, app_task, **kwargs)
            if latch.phase == "startup":
                snapshot_taken.set()
                await release_snapshot.wait()
            return result

        monkeypatch.setattr(
            lifespan_module,
            "_wait_for_phase",
            pause_after_startup_snapshot,
        )

        async def inner(scope, receive, send):
            inner_tasks.append(asyncio.current_task())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await trigger_duplicate.wait()
            duplicate_started.set()
            try:
                await send({"type": "lifespan.startup.complete"})
            except BaseException:
                pass
            await asyncio.Event().wait()

        async def cleanup():
            cleanup_calls.append(1)

        async def record(message):
            sent.append(dict(message))

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        owner = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, record)
        )
        await asyncio.wait_for(snapshot_taken.wait(), timeout=2.0)

        await guard._gate._lock.acquire()
        try:
            trigger_duplicate.set()
            await asyncio.wait_for(duplicate_started.wait(), timeout=2.0)
            await asyncio.sleep(0)
            release_snapshot.set()
            await asyncio.sleep(0)
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done()
        finally:
            guard._gate._lock.release()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2.0)
        assert cleanup_calls == [1]
        assert len(inner_tasks) == 1 and inner_tasks[0].done()
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.failed"
        ]

    async def test_cancellation_during_receive_task_cleanup_is_re_raised(self):
        """Cancelling the coordinator while its owned receive task handles its
        own cancellation must not consume the caller's cancellation."""

        sent = []
        released = []
        startup_sent = asyncio.Event()
        trigger_inner_failure = asyncio.Event()
        receive_cleanup_started = asyncio.Event()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "lifespan.startup"}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                receive_cleanup_started.set()
                await asyncio.Event().wait()
                raise

        async def inner(scope, inner_receive, inner_send):
            await inner_receive()
            await inner_send({"type": "lifespan.startup.complete"})
            await trigger_inner_failure.wait()
            raise RuntimeError("inner died")

        async def release():
            released.append(1)

        async def send(message):
            sent.append(dict(message))
            startup_sent.set()

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(release,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, receive, send)
        )
        await asyncio.wait_for(startup_sent.wait(), timeout=2.0)
        trigger_inner_failure.set()
        await asyncio.wait_for(receive_cleanup_started.wait(), timeout=2.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert released == [1]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_shutdown_verdict_waits_for_cleanup_to_settle(self):
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def cleanup():
            cleanup_entered.set()
            await release_cleanup.wait()

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(on_shutdown=(cleanup,)),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        stopping = asyncio.create_task(state.shutdown())
        await asyncio.wait_for(cleanup_entered.wait(), timeout=2.0)
        assert not [
            message
            for message in state.sent
            if message["type"].startswith("lifespan.shutdown.")
        ]
        release_cleanup.set()
        await asyncio.wait_for(stopping, timeout=2.0)
        assert not state.shutdown_failed

    async def test_deferred_inner_cancellation_is_failed_then_propagated(self):
        cleanup_calls = []

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            raise asyncio.CancelledError()

        async def cleanup():
            cleanup_calls.append(1)

        state = _lifespan(
            _guard(
                inner,
                hooks=LifespanHooks(on_shutdown=(cleanup,)),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert state.shutdown_failed
        assert state.error_occurred
        assert cleanup_calls == [1]
        assert [
            message["type"]
            for message in state.sent
            if message["type"].startswith("lifespan.shutdown.")
        ] == ["lifespan.shutdown.failed"]

    async def test_every_finalizer_runs_after_error_and_cancellation(self):
        events = []

        async def cancelled():
            events.append("cancelled")
            raise asyncio.CancelledError()

        async def failed():
            events.append("failed")
            raise RuntimeError("failed")

        async def completed():
            events.append("completed")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(
                    on_shutdown=(cancelled, failed, completed),
                ),
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert events == ["cancelled", "failed", "completed"]
        assert state.shutdown_failed
        assert state.error_occurred

    async def test_falsey_cleanup_cancellation_is_failed_and_re_raised(self):
        class FalseyCancelledError(asyncio.CancelledError):
            def __bool__(self):
                return False

        async def cancelled():
            raise FalseyCancelledError()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cancelled,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)

        assert state.shutdown_failed
        assert state.error_occurred
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]

    async def test_falsey_cleanup_failure_keeps_gate_recycle_only(self):
        class FalseyRuntimeError(RuntimeError):
            def __bool__(self):
                return False

        async def failed():
            raise FalseyRuntimeError("close was not confirmed")

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(failed,)),
        )
        first = _lifespan(guard)
        await asyncio.wait_for(first.startup(), timeout=2.0)
        await asyncio.wait_for(first.shutdown(), timeout=2.0)

        assert first.shutdown_failed
        assert guard._gate.state == "failed"

        second = _lifespan(guard)
        await asyncio.wait_for(second.startup(), timeout=2.0)
        assert second.startup_failed and second.should_exit
        assert guard._gate.state == "failed"
        with pytest.raises(
            StartupRefused,
            match="close was not confirmed",
        ):
            await guard({"type": "http"}, None, None)

    async def test_external_shutdown_cancellation_emits_failure_then_re_raises(self):
        startup_sent = asyncio.Event()
        released = []
        sent = []
        inbound = asyncio.Queue()
        inbound.put_nowait({"type": "lifespan.startup"})

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            startup_sent.set()
            await receive()

        async def release():
            released.append(1)

        async def record(message):
            sent.append(message)

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(release,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, inbound.get, record)
        )
        await startup_sent.wait()
        while not sent:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == [1]
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]


class TestTransportFaultSeams:
    @pytest.mark.parametrize(
        "first_kind",
        ("wrong-message", "non-mapping", "exception", "cancelled"),
    )
    async def test_invalid_initial_receive_fails_startup_and_gate(
        self,
        first_kind,
    ):
        """Uvicorn cannot inject a bad first receive, so this direct seam
        asserts only guard coordination and the wire verdict."""

        sent = []
        inner_calls = []
        baseline_tasks = set(asyncio.all_tasks())

        async def receive():
            if first_kind == "wrong-message":
                return {"type": "lifespan.shutdown"}
            if first_kind == "non-mapping":
                return ["lifespan.startup"]
            if first_kind == "exception":
                raise RuntimeError("initial receive failed")
            raise asyncio.CancelledError()

        async def inner(scope, inner_receive, inner_send):
            inner_calls.append(1)

        async def record(message):
            sent.append(dict(message))

        guard = _guard(inner)
        task = asyncio.create_task(
            guard({"type": "lifespan"}, receive, record)
        )
        if first_kind == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
        else:
            await asyncio.wait_for(task, timeout=2.0)

        await asyncio.sleep(0)
        assert inner_calls == []
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.failed"
        ]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)
        assert not [
            pending
            for pending in asyncio.all_tasks()
            if pending not in baseline_tasks
        ]

    @pytest.mark.parametrize(
        "receive_kind",
        ("exception", "wrong-message", "non-mapping", "cancelled"),
    )
    @pytest.mark.parametrize("cleanup_kind", ("ok", "failed", "cancelled"))
    async def test_shutdown_receive_fault_crosses_cleanup_outcome(
        self,
        receive_kind,
        cleanup_kind,
    ):
        """Direct transport-fault matrix: every fault reaches cleanup, emits
        exactly one shutdown.failed, fails the gate, and owns no leftover
        coordination or inner task."""

        sent = []
        cleanup_calls = []
        receive_calls = 0
        inner_exited = asyncio.Event()
        baseline_tasks = set(asyncio.all_tasks())

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "lifespan.startup"}
            if receive_kind == "exception":
                raise RuntimeError("shutdown receive failed")
            if receive_kind == "wrong-message":
                return {"type": "lifespan.disconnect"}
            if receive_kind == "non-mapping":
                return ["lifespan.shutdown"]
            raise asyncio.CancelledError()

        async def inner(scope, inner_receive, inner_send):
            try:
                await inner_receive()
                await inner_send({"type": "lifespan.startup.complete"})
                await inner_receive()
                await inner_send({"type": "lifespan.shutdown.complete"})
            finally:
                inner_exited.set()

        async def cleanup():
            cleanup_calls.append(cleanup_kind)
            if cleanup_kind == "failed":
                raise RuntimeError("cleanup failed")
            if cleanup_kind == "cancelled":
                raise asyncio.CancelledError()

        async def record(message):
            sent.append(dict(message))

        guard = _guard(
            inner,
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        task = asyncio.create_task(
            guard({"type": "lifespan"}, receive, record)
        )
        propagates_cancellation = (
            receive_kind == "cancelled" or cleanup_kind == "cancelled"
        )
        if propagates_cancellation:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
        else:
            await asyncio.wait_for(task, timeout=2.0)

        await asyncio.wait_for(inner_exited.wait(), timeout=2.0)
        await asyncio.sleep(0)
        assert receive_calls == 2
        assert cleanup_calls == [cleanup_kind]
        assert guard._gate.state == "failed"
        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]
        expected_reason = {
            "exception": "shutdown receive failed",
            "wrong-message": (
                "expected lifespan.shutdown, received 'lifespan.disconnect'"
            ),
            "non-mapping": (
                "expected lifespan.shutdown, received a non-mapping list"
            ),
            "cancelled": (
                "lifespan coordination was cancelled before shutdown"
            ),
        }[receive_kind]
        assert expected_reason in sent[-1]["message"]
        with pytest.raises(StartupRefused):
            await guard({"type": "http"}, None, None)
        assert not [
            pending
            for pending in asyncio.all_tasks()
            if pending not in baseline_tasks
        ]


class TestFailSafeCallbacks:
    async def test_hostile_terminal_type_subclass_cannot_forge_startup(self):
        class HostileMessageType(str):
            def startswith(self, _prefix, *args):
                return True

            def __getitem__(self, _key):
                return "complete"

        async def inner(scope, receive, send):
            await receive()
            await send(
                {
                    "type": HostileMessageType("not-a-lifespan-message"),
                }
            )
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        guard = _guard(inner)
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        assert "not-a-lifespan-message" in state.sent[0]["message"]

    async def test_hostile_message_dict_get_cannot_forge_startup(self):
        class HostileMessage(dict):
            def get(self, key, default=None):
                if key == "type":
                    return "lifespan.startup.complete"
                return super().get(key, default)

        async def inner(scope, receive, send):
            await receive()
            await send(
                HostileMessage(
                    {
                        "type": "not-a-lifespan-message",
                    }
                )
            )
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        guard = _guard(inner)
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        assert "not-a-lifespan-message" in state.sent[0]["message"]

    async def test_hostile_scope_subclasses_cannot_bypass_lifespan_routing(
        self,
    ):
        class HostileScopeType(str):
            def __eq__(self, _other):
                return False

        class HostileScope(dict):
            def __getitem__(self, key):
                if key == "type":
                    return "http"
                return super().__getitem__(key)

        cleanup_calls = []

        async def cleanup():
            cleanup_calls.append("cleanup")

        inbound = iter(
            (
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            )
        )
        sent = []

        async def receive():
            return next(inbound)

        async def send(message):
            sent.append(dict(message))

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        await guard(
            HostileScope(
                {
                    "type": HostileScopeType("lifespan"),
                }
            ),
            receive,
            send,
        )

        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
        ]
        assert cleanup_calls == ["cleanup"]
        assert guard._gate.state == "stopped"

    async def test_non_string_type_equality_cannot_forge_initial_startup(
        self,
    ):
        class ForgedMessageType:
            def __eq__(self, _other):
                return True

            def __ne__(self, _other):
                return False

            def __repr__(self):
                return "forged-non-string-startup"

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        inbound = iter(
            (
                {"type": ForgedMessageType()},
                {"type": "lifespan.shutdown"},
            )
        )
        sent = []

        async def receive():
            return next(inbound)

        async def send(message):
            sent.append(dict(message))

        guard = _guard(inner)
        await guard({"type": "lifespan"}, receive, send)

        assert [message["type"] for message in sent] == [
            "lifespan.startup.failed"
        ]
        assert "forged-non-string-startup" in sent[0]["message"]
        assert guard._gate.state == "failed"

    async def test_non_string_type_equality_cannot_forge_shutdown(self):
        class ForgedMessageType:
            def __eq__(self, _other):
                return True

            def __ne__(self, _other):
                return False

            def __repr__(self):
                return "forged-non-string-shutdown"

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})

        inbound = iter(
            (
                {"type": "lifespan.startup"},
                {"type": ForgedMessageType()},
            )
        )
        sent = []

        async def receive():
            return next(inbound)

        async def send(message):
            sent.append(dict(message))

        guard = _guard(inner)
        await guard({"type": "lifespan"}, receive, send)

        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]
        assert "forged-non-string-shutdown" in sent[-1]["message"]
        assert guard._gate.state == "failed"

    async def test_hostile_string_subclasses_are_normalized_before_emit(self):
        secret = "string-subclass-secret"

        class HostileString(str):
            def __bool__(self):
                raise RuntimeError(secret)

            def __format__(self, spec):
                raise RuntimeError(secret)

            def __str__(self):
                return self

        class HostileStringError(RuntimeError):
            def __str__(self):
                return HostileString("raw diagnostic")

        async def refuse():
            raise HostileStringError()

        def redact(_value):
            return HostileString("<safe diagnostic>")

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_validate=(refuse,)),
            redact=redact,
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        rendered = json.dumps(state.sent)
        assert secret not in rendered
        assert "<safe diagnostic>" in rendered

    async def test_hostile_repr_string_subclass_is_normalized_before_format(
        self,
    ):
        secret = "repr-subclass-secret"

        class HostileString(str):
            def __format__(self, spec):
                raise RuntimeError(secret)

            def __str__(self):
                return self

        class MessageType:
            def __repr__(self):
                return HostileString("malformed-kind")

        async def inner(scope, receive, send):
            await receive()
            await send({"type": MessageType()})

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        rendered = json.dumps(state.sent)
        assert secret not in rendered
        assert "malformed-kind" in rendered

    @pytest.mark.parametrize("phase", ("validate", "startup", "cleanup"))
    async def test_hostile_exception_type_name_never_controls_verdict(
        self,
        phase,
        caplog,
    ):
        secret = "metaclass-secret-that-must-not-reach-the-wire"

        class HostileExceptionMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise RuntimeError(secret)
                return super().__getattribute__(name)

        class HostileMetadataError(
            RuntimeError,
            metaclass=HostileExceptionMeta,
        ):
            def __str__(self):
                raise RuntimeError(secret)

        async def fail():
            raise HostileMetadataError()

        hooks = LifespanHooks(
            on_validate=(fail,) if phase == "validate" else (),
            on_startup=(fail,) if phase == "startup" else (),
            on_shutdown=(fail,) if phase == "cleanup" else (),
        )
        guard = _guard(_app(), hooks=hooks)
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        if phase == "cleanup":
            assert not state.startup_failed
            await asyncio.wait_for(state.shutdown(), timeout=2.0)
            assert state.shutdown_failed
            expected = [
                "lifespan.startup.complete",
                "lifespan.shutdown.failed",
            ]
        else:
            assert state.startup_failed and state.should_exit
            expected = ["lifespan.startup.failed"]

        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == expected
        rendered = json.dumps(state.sent) + caplog.text
        assert secret not in rendered
        assert "<unprintable exception>" in rendered

    async def test_broken_exception_string_still_emits_startup_failed(self):
        secret = "secret-that-must-not-reach-the-wire"

        class BrokenStringError(RuntimeError):
            def __str__(self):
                raise RuntimeError(secret)

        async def refuse():
            raise BrokenStringError()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_validate=(refuse,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        assert secret not in json.dumps(state.sent)
        assert "BrokenStringError" in json.dumps(state.sent)

    async def test_broken_inner_reason_still_emits_startup_failed(self):
        secret = "inner-secret-that-must-not-reach-the-wire"

        class BrokenReason:
            def __bool__(self):
                raise RuntimeError(secret)

            def __str__(self):
                raise RuntimeError(secret)

        async def inner(scope, receive, send):
            await receive()
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": BrokenReason(),
                }
            )

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        assert secret not in json.dumps(state.sent)
        assert "<unprintable inner failure reason>" in json.dumps(state.sent)

    async def test_broken_inner_message_repr_still_emits_startup_failed(self):
        secret = "repr-secret-that-must-not-reach-the-wire"

        class BrokenMessageType:
            def __repr__(self):
                raise RuntimeError(secret)

        async def inner(scope, receive, send):
            await receive()
            await send({"type": BrokenMessageType()})

        state = _lifespan(_guard(inner))
        await asyncio.wait_for(state.startup(), timeout=2.0)

        assert state.startup_failed and state.should_exit
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.failed"
        ]
        rendered = json.dumps(state.sent)
        assert secret not in rendered
        assert "<unprintable type>" in rendered

    async def test_broken_cleanup_string_still_emits_shutdown_failed(self):
        secret = "cleanup-secret-that-must-not-reach-the-wire"

        class BrokenStringError(RuntimeError):
            def __str__(self):
                raise RuntimeError(secret)

        async def cleanup():
            raise BrokenStringError()

        guard = _guard(
            _app(),
            hooks=LifespanHooks(on_shutdown=(cleanup,)),
        )
        state = _lifespan(guard)
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)

        assert state.shutdown_failed
        assert guard._gate.state == "failed"
        assert [message["type"] for message in state.sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.failed",
        ]
        assert secret not in json.dumps(state.sent)
        assert "BrokenStringError" in json.dumps(state.sent)

    async def test_redactor_failure_never_falls_back_to_the_secret(self):
        secret = "https://user:password@example.invalid"
        reports = []

        async def refuse():
            raise ValueError(secret)

        def broken_redactor(value):
            raise RuntimeError("redactor bug")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(on_validate=(refuse,)),
                redact=broken_redactor,
                report=reports.append,
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        rendered = json.dumps({"sent": state.sent, "reports": reports})
        assert state.startup_failed
        assert secret not in rendered
        assert "<redaction failed>" in rendered

    async def test_non_string_redaction_result_is_withheld(self):
        async def refuse():
            raise ValueError("secret-value")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(on_validate=(refuse,)),
                redact=lambda value: None,
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert "secret-value" not in json.dumps(state.sent)
        assert "<redaction failed>" in json.dumps(state.sent)

    async def test_reporter_failure_does_not_mask_shutdown_verdict(self):
        async def cleanup():
            raise RuntimeError("close failed")

        def broken_reporter(line):
            raise RuntimeError("reporter failed")

        state = _lifespan(
            _guard(
                _app(),
                hooks=LifespanHooks(on_shutdown=(cleanup,)),
                report=broken_reporter,
            )
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert state.shutdown_failed
        assert not state.error_occurred


class TestOptimizedMode:
    def test_duplicate_guard_survives_python_optimized_mode(self):
        program = textwrap.dedent(
            """
            import asyncio
            import json
            import uvicorn
            from uvicorn.lifespan.on import LifespanOn
            from hivemind_inference.asgi_lifespan import (
                LifespanGuard,
                LifespanHooks,
                TerminalMessageViolation,
                _PhaseLatch,
            )

            class Recording(LifespanOn):
                def __init__(self, config):
                    super().__init__(config)
                    self.sent = []

                async def send(self, message):
                    self.sent.append(dict(message))
                    await super().send(message)

            async def inner(scope, receive, send):
                await receive()
                await send({"type": "lifespan.startup.complete"})
                await send({"type": "lifespan.startup.complete"})

            async def main():
                duplicate_guard_raised = False
                latch = _PhaseLatch("startup")
                latch.observe_terminal("complete", None)
                try:
                    latch.observe_terminal("complete", None)
                except TerminalMessageViolation:
                    duplicate_guard_raised = True

                guard = LifespanGuard(
                    inner,
                    name="optimized",
                    hooks=LifespanHooks(),
                    redact=lambda value: value,
                    report=lambda line: None,
                )
                state = Recording(
                    uvicorn.Config(
                        guard,
                        lifespan="auto",
                        log_config=None,
                    )
                )
                await asyncio.wait_for(state.startup(), timeout=2.0)
                payload = {
                    "duplicate_guard_raised": duplicate_guard_raised,
                    "startup_failed": state.startup_failed,
                    "types": [message["type"] for message in state.sent],
                }
                print(json.dumps(payload))
                if payload != {
                    "duplicate_guard_raised": True,
                    "startup_failed": True,
                    "types": ["lifespan.startup.failed"],
                }:
                    raise SystemExit(7)

            asyncio.run(main())
            """
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(
            Path(lifespan_module.__file__).resolve().parents[1]
        )
        completed = subprocess.run(
            [sys.executable, "-O", "-c", program],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout.splitlines()[-1]) == {
            "duplicate_guard_raised": True,
            "startup_failed": True,
            "types": ["lifespan.startup.failed"],
        }
