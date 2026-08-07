# -*- coding: utf-8 -*-
"""P13-1C (#276) — the shared per-process inference runtime holder (ADR-0027).

``InferenceRuntime`` is the single seam through which both consuming services
reach a provider. Everything locked here is a property a consumer relies on
without being able to check it itself:

- the resolved configuration is snapshotted ONCE — a later environment change
  cannot mutate a running provider;
- each role adapter is built at most once and reused, so one transport per role
  exists per process;
- ``aclose()`` releases EVERY owned transport, is idempotent, and still closes
  the siblings when one of them fails (a leaked proxied transport is the exact
  failure this method exists to prevent);
- an unconfigured role fails closed with a value-free
  ``InferenceRoleUnavailable`` instead of silently degrading;
- resolution is fail-closed: a mixed configuration family raises before any
  adapter — therefore before any network access — exists.

No network: adapters are built against unresolvable ``.invalid`` endpoints and
never invoked, or replaced by recording doubles where the test is about
lifecycle rather than the wire.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from hivemind_inference import (
    InferenceConfig,
    InferenceConfigError,
    InferenceRoleUnavailable,
    InferenceRuntime,
)
import hivemind_inference.runtime as runtime_module
from hivemind_inference.runtime import (
    InferenceRuntimeClosed,
    InferenceShutdownIncomplete,
)
from tests.fakes.inference_fakes import (
    LifecycleAdapter,
    make_chat_profile,
    make_embedding_profile,
    make_inference_config,
)

_LEGACY_ENV = {
    "LLMAAS_API_URL": "http://provider.p13-1c.invalid/v1",
    "LLMAAS_API_KEY": "test-key",
}


class _RecordingAdapter:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.closed = 0
        self._fail = fail

    async def aclose(self) -> None:
        self.closed += 1
        if self._fail is not None:
            raise self._fail


class TestResolution:
    def test_from_environment_accepts_an_explicit_mapping(self):
        runtime = InferenceRuntime.from_environment(dict(_LEGACY_ENV))
        assert runtime.config.legacy_active is True
        assert runtime.config.configured_roles == ("chat", "embedding")

    def test_absent_configuration_is_a_valid_runtime(self):
        runtime = InferenceRuntime.from_environment({})
        assert runtime.config.chat is None
        assert runtime.config.embedding is None
        assert runtime.config.configured_roles == ()

    def test_mixed_families_fail_closed_before_any_adapter(self):
        environ = dict(_LEGACY_ENV)
        environ["INFERENCE_CHAT_PROVIDER"] = "openai-compatible"
        with pytest.raises(InferenceConfigError):
            InferenceRuntime.from_environment(environ)

    def test_profiles_are_snapshotted_once(self):
        environ = dict(_LEGACY_ENV)
        environ["LLMAAS_MODEL"] = "first-model"
        runtime = InferenceRuntime.from_environment(environ)
        assert runtime.config.chat.configured_model == "first-model"
        # Mutating the SOURCE mapping after resolution must not change the
        # running profile: a new process (or explicit reset) is required.
        environ["LLMAAS_MODEL"] = "second-model"
        assert runtime.config.chat.configured_model == "first-model"


class TestAdapterLifecycle:
    async def test_each_role_adapter_is_built_once_and_reused(self):
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        try:
            assert runtime.chat_provider() is runtime.chat_provider()
            assert runtime.embedding_provider() is runtime.embedding_provider()
            assert runtime.chat_probe() is runtime.chat_probe()
            assert runtime.embedding_probe() is runtime.embedding_probe()
            assert runtime.chat_provider() is not runtime.chat_probe()
        finally:
            await runtime.aclose()

    async def test_aclose_closes_every_owned_transport(self):
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        transports = [
            runtime.chat_provider()._owned_http_client,
            runtime.embedding_provider()._owned_http_client,
            runtime.chat_probe()._owned_http_client,
            runtime.embedding_probe()._owned_http_client,
        ]
        assert all(not transport.is_closed for transport in transports)
        await runtime.aclose()
        assert all(transport.is_closed for transport in transports)

    async def test_aclose_is_idempotent(self):
        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = _RecordingAdapter()
        runtime._chat_provider = adapter
        await runtime.aclose()
        await runtime.aclose()
        assert adapter.closed == 1

    async def test_aclose_closes_siblings_when_one_fails_then_reraises(self):
        """The failure mode this guards: a transport left open (and its proxy
        connection alive) because an EARLIER adapter raised on close."""
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        boom = RuntimeError("close exploded")
        first = _RecordingAdapter(fail=boom)
        others = [_RecordingAdapter() for _ in range(3)]
        runtime._chat_provider = first
        runtime._embedding_provider, runtime._chat_probe, runtime._embedding_probe = others

        with pytest.raises(RuntimeError) as excinfo:
            await runtime.aclose()

        assert excinfo.value is boom
        assert first.closed == 1
        assert all(adapter.closed == 1 for adapter in others)

    async def test_a_failed_close_is_terminal_and_never_re_attempted(self):
        """PR #303 sweep (S2-F1): the retry this method used to promise was a
        fiction, and the fiction leaked.

        ``httpx.AsyncClient.aclose()`` sets its state to CLOSED *before*
        awaiting the transport, so once a close has failed part-way, a second
        call returns instantly having touched nothing. The old contract then
        read that quiet success as "closed", cleared the slot, and the holder
        dropped the last reference to a still-open connection pool — R1-F2
        again, one layer down.

        So a failed close is now TERMINAL-UNCONFIRMED: never re-attempted,
        never clearable, and reported on every subsequent call.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True))
        first = _RecordingAdapter(fail=RuntimeError("close exploded"))
        runtime._chat_provider = first

        with pytest.raises(RuntimeError):
            await runtime.aclose()
        assert first.closed == 1
        assert runtime._chat_provider is first
        assert not runtime.is_fully_closed

        # The second call neither re-invokes the adapter nor reports success.
        with pytest.raises(InferenceShutdownIncomplete) as caught:
            await runtime.aclose()
        assert caught.value.slots == ("_chat_provider",)
        assert first.closed == 1, "a no-op retry was issued anyway"
        assert not runtime.is_fully_closed


class TestTerminalShutdown:
    """PR #303 round 1 (Codex Sol, high x2): shutdown is TERMINAL and
    cancellation-safe.

    Both findings are the same defect seen from two sides — the runtime had no
    model of "we are shutting down", so a late caller could open a transport
    nobody would close, and a cancelled cleanup could strand one that nobody
    COULD close.
    """

    async def test_shutdown_refuses_to_build_a_new_transport(self):
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        transport = runtime.chat_provider()._owned_http_client
        await runtime.aclose()
        assert transport.is_closed
        assert runtime.is_closing
        for accessor in (
            "chat_provider",
            "embedding_provider",
            "chat_probe",
            "embedding_probe",
        ):
            with pytest.raises(InferenceRuntimeClosed):
                getattr(runtime, accessor)()

    async def test_the_guard_precedes_the_role_check(self):
        """A closed runtime reports SHUTDOWN, not "role not configured" — the
        two are different operator situations and must not be conflated."""
        runtime = InferenceRuntime(make_inference_config(chat=False, embedding=False))
        await runtime.aclose()
        with pytest.raises(InferenceRuntimeClosed):
            runtime.chat_provider()

    async def test_cancelling_aclose_completes_every_close_before_returning(self):
        """PR #303 round 2 (Codex Sol, high): the round-1 repair was incomplete.

        Shielding kept a started close alive only while the loop lived. Because
        ``aclose()`` re-raised the cancellation WITHOUT waiting for the
        interrupted task, it could return with that close still pending — and
        the caller's event-loop teardown then killed it. The interrupted close
        is deliberately SLOWER than every sibling here, which is exactly the
        ordering the round-1 test missed (it made the slow close last, so the
        siblings' own waiting hid the defect). No post-cancellation sleep: the
        assertions run the instant ``aclose()`` returns.
        """
        finished: list[str] = []

        class _Timed:
            def __init__(self, name: str, delay: float) -> None:
                self.name = name
                self.delay = delay
                self.closes = 0

            async def aclose(self) -> None:
                self.closes += 1
                await asyncio.sleep(self.delay)
                finished.append(self.name)

        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        slow = _Timed("chat-slow", 0.30)
        runtime._chat_provider = slow
        runtime._embedding_provider = _Timed("emb", 0.01)
        runtime._chat_probe = _Timed("chat-probe", 0.01)
        runtime._embedding_probe = _Timed("emb-probe", 0.01)

        task = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0.05)  # let the first close actually start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert runtime.is_closing
        # Everything that was STARTED has settled by the time aclose returned.
        assert sorted(finished) == ["chat-probe", "chat-slow", "emb", "emb-probe"]
        assert slow.closes == 1
        # Confirmed closes cleared their slots; nothing is left half-owned.
        assert runtime._chat_provider is None
        assert runtime._embedding_provider is None

    def test_cancelled_shutdown_survives_event_loop_teardown(self):
        """The same property observed from OUTSIDE the loop.

        This is the shape the round-1 test could not see: ``asyncio.run``
        returns, tearing the loop down, and the question is whether the
        transport close actually happened. Deliberately NOT an async test.
        """
        finished: list[str] = []

        class _Timed:
            def __init__(self, name: str, delay: float) -> None:
                self.name = name
                self.delay = delay

            async def aclose(self) -> None:
                await asyncio.sleep(self.delay)
                finished.append(self.name)

        async def _scenario() -> None:
            runtime = InferenceRuntime(
                make_inference_config(chat=True, embedding=True)
            )
            runtime._chat_provider = _Timed("chat-slow", 0.30)
            runtime._embedding_provider = _Timed("emb", 0.01)
            task = asyncio.create_task(runtime.aclose())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_scenario())
        assert sorted(finished) == ["chat-slow", "emb"]

    async def test_a_concurrent_retry_joins_the_in_flight_close(self):
        """Two overlapping ``aclose()`` calls must share ONE adapter close.

        Rewritten from a version that tested nothing: it retried *after* the
        first call had already drained to completion, so ``_chat_provider`` was
        already ``None`` and both assertions were trivially true. The retry is
        now genuinely concurrent, proven by asserting the first call has not
        returned before the close is released.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = LifecycleAdapter()
        runtime._chat_provider = adapter

        first = asyncio.create_task(runtime.aclose())
        await adapter.entered.wait()
        second = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)  # yield, not a timing guess: let it reach phase 2

        assert not first.done(), "the close settled before the retry was issued"
        assert adapter.calls == 1

        adapter.release.set()
        await asyncio.gather(first, second)
        assert adapter.calls == 1, "the retry started a second concurrent close"
        assert runtime._chat_provider is None

    async def test_an_unsettled_close_is_resumed_not_restarted(self, monkeypatch):
        """The one path where a close can still be in flight when ``aclose()``
        returns: the drain gave up because the caller kept cancelling.

        Then BOTH invariants must hold — the adapter stays reachable (clearing
        it would strand a transport that may still be open), and the next call
        RESUMES that same close instead of invoking ``adapter.aclose()`` again
        concurrently. The drain deadline is lowered so it expires while the
        close is still running, which is how this branch is reached through the
        public API.
        """
        monkeypatch.setattr(runtime_module, "_CLOSE_BUDGET_SECONDS", 0.05)
        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = LifecycleAdapter()
        runtime._chat_provider = adapter

        task = asyncio.create_task(runtime.aclose())
        await adapter.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert adapter.calls == 1
        # Unsettled: the adapter and its close task are both retained.
        assert runtime._chat_provider is adapter
        assert "_chat_provider" in runtime._close_tasks
        assert not runtime.is_fully_closed

        adapter.release.set()
        await runtime.aclose()
        assert adapter.calls == 1, "the retry restarted the close instead of resuming it"
        assert runtime._chat_provider is None
        assert runtime._close_tasks == {}

    async def test_a_cancelled_close_task_is_not_a_closed_transport(self):
        """PR #303 systemic audit (D2): the retry that reported success.

        A close task retained as unsettled and then killed by ordinary event-
        loop teardown came back CANCELLED. The drain treated that as nothing to
        report — it recorded neither an error nor a cancellation — so
        ``aclose()`` RETURNED NORMALLY with the slot still populated and its
        transport still open, and the holder then dropped its last reference.
        Unclosed and unreachable: the R1-F2 leak, one retry later.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = LifecycleAdapter()
        runtime._chat_provider = adapter

        killed = asyncio.ensure_future(adapter.aclose())
        await adapter.entered.wait()
        killed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await killed
        runtime._close_tasks["_chat_provider"] = killed
        runtime._closing = True

        with pytest.raises(InferenceShutdownIncomplete) as caught:
            await runtime.aclose()
        assert caught.value.slots == ("_chat_provider",)
        assert runtime._chat_provider is adapter
        assert not runtime.is_fully_closed

        # And it stays that way. A cancelled close leaves the same
        # no-evidence-possible state as a failed one (S2-F1): the client marked
        # itself closed before the cancellation landed, so nothing a later call
        # does can confirm the transport.
        adapter.release.set()
        with pytest.raises(InferenceShutdownIncomplete):
            await runtime.aclose()
        assert adapter.calls == 1, "a no-op retry was issued anyway"
        assert not runtime.is_fully_closed

    async def test_every_wedged_slot_is_named_not_just_the_first(self, monkeypatch):
        """The budget is PER SLOT, so a fully wedged runtime must terminate and
        report all four — an operator chasing a leak needs the whole list, and
        a single-slot test cannot tell "reports the first" from "reports all"."""
        monkeypatch.setattr(runtime_module, "_CLOSE_BUDGET_SECONDS", 0.05)
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        adapters = {}
        for slot in runtime_module._ADAPTER_SLOTS:
            adapters[slot] = LifecycleAdapter(mode="hang")
            setattr(runtime, slot, adapters[slot])

        closing = asyncio.create_task(runtime.aclose())
        try:
            done, _pending = await asyncio.wait({closing}, timeout=3.0)
            assert done, "a fully wedged shutdown did not terminate"
            with pytest.raises(InferenceShutdownIncomplete) as caught:
                await closing
            assert caught.value.slots == runtime_module._ADAPTER_SLOTS
            assert not runtime.is_fully_closed
        finally:
            closing.cancel()
            for task in list(runtime._close_tasks.values()):
                task.cancel()

    async def test_mixed_outcomes_across_slots_never_report_ownership(self):
        """One call, different outcomes per slot: whatever either concurrent
        caller RETURNS, the predicate must withhold ownership and every
        unconfirmed adapter must stay reachable.

        This is the property that survives however ``aclose()``'s return is
        described — the holder reads the predicate, never the return.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=True))
        slow_ok = LifecycleAdapter(mode="ok")
        fails = LifecycleAdapter(mode="raise")
        runtime._chat_provider = slow_ok
        runtime._embedding_provider = fails

        first = asyncio.create_task(runtime.aclose())
        await asyncio.gather(slow_ok.entered.wait(), fails.entered.wait())
        second = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)

        fails.release.set()
        slow_ok.release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

        assert any(isinstance(o, RuntimeError) for o in outcomes)
        assert not runtime.is_fully_closed, "ownership claimed over a failed close"
        assert runtime._embedding_provider is fails
        # The confirmed sibling is released exactly once, whichever call drained it.
        assert slow_ok.calls == 1 and fails.calls == 1
        assert runtime._chat_provider is None

    async def test_a_close_settling_after_its_budget_stays_conservative(
        self, monkeypatch
    ):
        """A close that overruns its budget and THEN succeeds leaves a settled
        task behind, so the predicate keeps saying "not closed" over a released
        transport.

        Deliberately pinned as accepted behaviour: the staleness is one-way —
        the predicate can never claim closed over a LIVE transport — and the
        next call reconciles it without re-invoking the adapter. Without this
        test the direction could silently invert.
        """
        monkeypatch.setattr(runtime_module, "_CLOSE_BUDGET_SECONDS", 0.05)
        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = LifecycleAdapter(mode="ok")
        runtime._chat_provider = adapter

        with pytest.raises(InferenceShutdownIncomplete):
            await runtime.aclose()
        assert not runtime.is_fully_closed

        adapter.release.set()
        await asyncio.sleep(0)  # let the retained close settle
        assert not runtime.is_fully_closed, "stale in the CONSERVATIVE direction"

        await runtime.aclose()
        assert adapter.calls == 1, "the reconciling call re-invoked the adapter"
        assert runtime.is_fully_closed

    async def test_an_unconfirmed_slot_alone_withholds_ownership(self):
        """Pinned white-box, deliberately.

        Today an unconfirmed slot always still holds its adapter, so the
        adapter check alone would answer this correctly and no black-box test
        can tell the two clauses apart. But "the slot is retained" is exactly
        the invariant four review rounds kept breaking, so the predicate must
        not depend on it: unconfirmed is enough, by itself, to withhold
        ownership.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True))
        assert runtime.is_fully_closed
        runtime._unconfirmed.add("_chat_provider")
        assert not runtime.is_fully_closed

    async def test_is_fully_closed_is_the_single_authority_on_ownership(self):
        """The predicate the owner reads to decide "may I drop this?".

        Derived from the slots rather than recorded in a flag, because a flag
        has to be set on every path and the branch that forgets is exactly how
        one ownership hole became four review findings.
        """
        runtime = InferenceRuntime(make_inference_config(chat=True))
        assert runtime.is_fully_closed, "a runtime that built nothing owns nothing"

        adapter = LifecycleAdapter()
        runtime._chat_provider = adapter
        assert not runtime.is_fully_closed

        adapter.release.set()
        await runtime.aclose()
        assert runtime.is_fully_closed

        # An outstanding close task alone is enough to withhold ownership,
        # even with every slot already cleared.
        pending = asyncio.ensure_future(asyncio.sleep(0))
        runtime._close_tasks["_chat_probe"] = pending
        assert not runtime.is_fully_closed
        await runtime._close_tasks.pop("_chat_probe")

    async def test_a_failed_close_keeps_the_adapter_but_never_claims_it_closed(self):
        """A close that RAISED leaves the transport in an unknown state, so the
        adapter must stay reachable — clearing it would strand a possibly-open
        transport with no reference left to it — and the runtime must never
        later claim it closed.

        The "flaky adapter that succeeds on the second attempt" this test used
        to model does not exist for the adapters actually registered here: an
        httpx client that failed a close is already marked closed and will
        report a cheerful success next time (S2-F1). Modelling it taught the
        suite to expect a recovery the real stack cannot deliver.
        """
        attempts = 0

        class _FlakyAdapter:
            async def aclose(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("close exploded")

        runtime = InferenceRuntime(make_inference_config(chat=True))
        adapter = _FlakyAdapter()
        runtime._chat_provider = adapter

        with pytest.raises(RuntimeError):
            await runtime.aclose()
        assert runtime._chat_provider is adapter

        with pytest.raises(InferenceShutdownIncomplete):
            await runtime.aclose()
        assert attempts == 1
        assert runtime._chat_provider is adapter
        assert not runtime.is_fully_closed


class TestUnconfiguredRoles:
    @pytest.mark.parametrize(
        "accessor,role",
        [
            ("chat_provider", "chat"),
            ("chat_probe", "chat"),
            ("embedding_provider", "embedding"),
            ("embedding_probe", "embedding"),
        ],
    )
    def test_absent_role_fails_closed(self, accessor, role):  # noqa: D401
        # No adapter is ever built here, so there is nothing to close.
        runtime = InferenceRuntime(make_inference_config(chat=False, embedding=False))
        with pytest.raises(InferenceRoleUnavailable) as excinfo:
            getattr(runtime, accessor)()
        assert excinfo.value.role == role
        # Value-free: the message names the role and the variables to set, and
        # nothing else.
        assert role in str(excinfo.value)

    async def test_a_configured_role_is_unaffected_by_an_absent_sibling(self):
        runtime = InferenceRuntime(make_inference_config(chat=True, embedding=False))
        try:
            assert runtime.chat_provider() is not None
            with pytest.raises(InferenceRoleUnavailable):
                runtime.embedding_provider()
        finally:
            await runtime.aclose()


class TestProxySignal:
    def test_configured_proxy_logs_a_redacted_origin(self, caplog):
        with caplog.at_level(logging.INFO, logger="hivemind_inference.runtime"):
            InferenceRuntime(
                make_inference_config(chat=True),
                proxy_url="http://user:pw@proxy.internal:3128",
            )
        joined = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name == "hivemind_inference.runtime"
        )
        assert "via proxy" in joined
        assert "http://<redacted>" in joined
        for secret in ("user", "pw", "proxy.internal", "3128"):
            assert secret not in joined

    def test_no_configured_role_emits_no_proxy_line(self, caplog):
        """A service with no provider must not advertise an egress path it
        will never use."""
        with caplog.at_level(logging.INFO, logger="hivemind_inference.runtime"):
            InferenceRuntime(
                InferenceConfig(chat=None, embedding=None, legacy_active=False),
                proxy_url="http://proxy.internal:3128",
            )
        assert not [
            record
            for record in caplog.records
            if record.name == "hivemind_inference.runtime"
        ]


class TestAdapterSelection:
    async def test_registry_maps_each_role_to_its_registered_adapter(self):
        from hivemind_inference.adapters.anthropic_native import AnthropicChatProvider
        from hivemind_inference.adapters.openai_compatible import (
            OpenAICompatibleChatProvider,
            OpenAICompatibleEmbeddingProvider,
        )

        runtime = InferenceRuntime(
            InferenceConfig(
                chat=make_chat_profile(),
                embedding=make_embedding_profile(),
                legacy_active=False,
            )
        )
        try:
            assert isinstance(runtime.chat_provider(), OpenAICompatibleChatProvider)
            assert isinstance(
                runtime.embedding_provider(), OpenAICompatibleEmbeddingProvider
            )
        finally:
            await runtime.aclose()

        anthropic = InferenceRuntime(
            InferenceConfig(
                chat=make_chat_profile(
                    provider_id="anthropic",
                    adapter_id="anthropic",
                    endpoint="https://api.anthropic.com",
                    temperature=0.3,
                ),
                embedding=None,
                legacy_active=False,
            )
        )
        try:
            assert isinstance(anthropic.chat_provider(), AnthropicChatProvider)
        finally:
            await anthropic.aclose()
