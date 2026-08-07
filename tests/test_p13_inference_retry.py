# -*- coding: utf-8 -*-
"""P13-1A (#274) — the single Hivemind-owned bounded retry/deadline loop
(ADR-0027).

Provider SDKs are constructed with their implicit retry disabled; this loop is
the ONLY retry authority. Proven here: at most two attempts, retry only for an
explicitly transient rate limit or a proven pre-send transport failure, a
cooperative wall-clock total deadline (a hard bound against a non-cooperative
attempt is deferred to the transport-owning adapters, #275), budget-aware delay
refusal, and untouched cancellation propagation. Fully offline; no provider.
"""

from __future__ import annotations

import asyncio

import pytest

from hivemind_inference.errors import InferenceError
from hivemind_inference.retry import (
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    AttemptFailure,
    parse_retry_after_seconds,
    run_with_bounded_retry,
)


def _error(*, category="unavailable", retryable=False, correlation_id="c"):
    return InferenceError(
        category=category,
        role="chat",
        provider_id="openai",
        adapter_id="openai-compatible",
        retryable=retryable,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- #
# parse_retry_after_seconds                                                   #
# --------------------------------------------------------------------------- #


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, None),
            ("0", 0.0),
            ("3", 3.0),
            ("5", 5.0),
            ("  4  ", 4.0),
            ("6", None),          # above the 5s cap
            ("100", None),
            ("-1", None),         # negative is not an ASCII digit run
            ("3.5", None),        # fractional form is not honored
            ("abc", None),
            ("Wed, 21 Oct 2015 07:28:00 GMT", None),  # HTTP-date form → no retry
            ("", None),
            # Unicode "digit" characters must not be honored as delta-seconds:
            # superscripts (²/³) and circled (①) pass str.isdigit() but crash
            # int(); non-Latin decimals (Arabic-Indic ١٢٣) int() would accept.
            # ASCII base-10 only — anything else cleanly authorizes no retry.
            ("²", None),
            ("³", None),
            ("①", None),
            ("١٢٣", None),
            # Very long ASCII digit runs must return None, never raise: bounding
            # the length before conversion avoids OverflowError (~309 digits) and
            # the CPython integer-string digit-limit ValueError (~4300+ digits).
            ("9" * 309, None),
            ("9" * 5000, None),
            ("007", None),   # 3 ASCII digits but 7 > the 5s cap
            ("005", 5.0),    # leading zeros within the cap are honored
        ],
    )
    def test_parse(self, raw, expected):
        # A non-ASCII "digit" must return None, never raise: a hostile
        # Retry-After header cannot crash the retry path or leak its raw value.
        assert parse_retry_after_seconds(raw) == expected

    def test_cap_constant(self):
        assert MAX_RETRY_AFTER_SECONDS == 5.0
        assert parse_retry_after_seconds(str(int(MAX_RETRY_AFTER_SECONDS))) == 5.0


# --------------------------------------------------------------------------- #
# AttemptFailure invariants                                                   #
# --------------------------------------------------------------------------- #


class TestAttemptFailureInvariants:
    def test_retry_delay_requires_retryable_error(self):
        with pytest.raises(ValueError):
            AttemptFailure(_error(retryable=False), retry_delay_seconds=1.0)

    def test_retryable_error_requires_explicit_delay(self):
        with pytest.raises(ValueError):
            AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=None)

    def test_non_retryable_with_no_delay_ok(self):
        failure = AttemptFailure(_error(retryable=False), retry_delay_seconds=None)
        assert failure.retry_delay_seconds is None

    def test_pre_send_transport_uses_zero_delay(self):
        failure = AttemptFailure(_error(category="unavailable", retryable=True), retry_delay_seconds=0.0)
        assert failure.retry_delay_seconds == 0.0

    # --- ADR-0027 sole-retry allowlist enforced at the contract layer -------- #

    FORBIDDEN_RETRY_CATEGORIES = (
        "auth",
        "quota_exhausted",
        "timeout",
        "unsupported",
        "invalid_request",
        "content_rejected",
        "invalid_response",
    )

    @pytest.mark.parametrize("category", FORBIDDEN_RETRY_CATEGORIES)
    def test_forbidden_category_marked_retryable_is_rejected(self, category):
        # A buggy/future adapter that marks a forbidden category retryable and
        # supplies a delay must NOT be able to construct a retry signal.
        with pytest.raises(ValueError):
            AttemptFailure(_error(category=category, retryable=True), retry_delay_seconds=0.0)

    @pytest.mark.parametrize("delay", [5.1, 6.0, 100.0, -0.5, -1.0])
    def test_rate_limited_delay_out_of_range_rejected(self, delay):
        with pytest.raises(ValueError):
            AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=delay)

    @pytest.mark.parametrize("delay", [float("nan"), float("inf"), float("-inf")])
    def test_rate_limited_non_finite_delay_rejected(self, delay):
        with pytest.raises(ValueError):
            AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=delay)

    def test_bool_delay_rejected(self):
        with pytest.raises(ValueError):
            AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=True)

    def test_unavailable_nonzero_delay_rejected(self):
        # Only the proven pre-send unavailable retries, and only with zero wait.
        with pytest.raises(ValueError):
            AttemptFailure(_error(category="unavailable", retryable=True), retry_delay_seconds=1.0)

    @pytest.mark.parametrize("delay", [0.0, 2.0, 5.0])
    def test_rate_limited_in_range_delay_accepted(self, delay):
        failure = AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=delay)
        assert failure.retry_delay_seconds == delay

    async def test_loop_cannot_replay_a_forbidden_category(self):
        # End-to-end: an attempt that tries to signal a retryable auth failure
        # cannot even build the AttemptFailure, so the loop never replays it;
        # the ValueError surfaces instead of a silent second paid request.
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(_error(category="auth", retryable=True), retry_delay_seconds=0.0)

        with pytest.raises(ValueError):
            await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert len(calls) == 1


# --------------------------------------------------------------------------- #
# run_with_bounded_retry                                                      #
# --------------------------------------------------------------------------- #


class TestBoundedRetry:
    async def test_success_on_first_attempt(self):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            return "ok"

        result = await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert result == "ok"
        assert len(calls) == 1
        assert calls[0] > 0  # positive remaining budget passed in

    async def test_non_retryable_failure_stops_after_one_attempt(self):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(_error(category="auth", retryable=False), retry_delay_seconds=None)

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert excinfo.value.category == "auth"
        assert len(calls) == 1

    async def test_transient_rate_limit_retries_once_then_succeeds(self):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            if len(calls) == 1:
                raise AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=0.0)
            return "recovered"

        result = await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert result == "recovered"
        assert len(calls) == MAX_ATTEMPTS == 2

    @pytest.mark.parametrize("category", ["rate_limited", "unavailable"])
    async def test_zero_retry_policy_never_replays_an_authorized_failure(
        self, category
    ):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(
                _error(category=category, retryable=True),
                retry_delay_seconds=0.0,
            )

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(
                attempt,
                timeout_seconds=5.0,
                role="chat",
                provider_id="openai",
                adapter_id="openai-compatible",
                correlation_id="c",
                retry_policy="none",
            )
        assert excinfo.value.category == category
        assert len(calls) == 1

    async def test_unknown_retry_policy_is_refused_before_the_attempt(self):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)

        with pytest.raises(ValueError):
            await run_with_bounded_retry(
                attempt,
                timeout_seconds=5.0,
                role="chat",
                provider_id="openai",
                adapter_id="openai-compatible",
                correlation_id="c",
                retry_policy="unbounded",
            )
        assert calls == []

    async def test_never_exceeds_two_attempts(self):
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=0.0)

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert excinfo.value.category == "rate_limited"
        assert len(calls) == 2  # exactly MAX_ATTEMPTS, never a third

    async def test_delay_that_cannot_fit_budget_is_refused(self):
        # A 5s authorized delay cannot fit a 50ms total budget: surface the
        # normalized failure instead of starting a doomed second attempt.
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(_error(category="rate_limited", retryable=True), retry_delay_seconds=5.0)

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(attempt, timeout_seconds=0.05, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert excinfo.value.category == "rate_limited"
        assert len(calls) == 1  # no second attempt started

    async def test_authorized_retry_delay_cannot_overrun_deadline(self, monkeypatch):
        # The authorized retry delay is awaited UNDER the same total deadline.
        # A delay that fits the budget at check time must still not overrun the
        # total deadline if the sleep is descheduled: asyncio.sleep is forced to
        # oversleep far past the deadline, and the loop surfaces the normalized
        # timeout instead of proceeding to a late second attempt.
        real_sleep = asyncio.sleep

        async def oversleeping_sleep(seconds):
            await real_sleep(seconds + 0.5)

        monkeypatch.setattr(asyncio, "sleep", oversleeping_sleep)
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise AttemptFailure(
                _error(category="rate_limited", retryable=True),
                retry_delay_seconds=0.01,
            )

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(
                attempt, timeout_seconds=0.05, role="chat",
                provider_id="openai", adapter_id="openai-compatible",
                correlation_id="c",
            )
        assert excinfo.value.category == "timeout"
        assert len(calls) == 1  # the oversleeping delay never reached a 2nd attempt

    async def test_exhausted_budget_before_first_attempt_is_timeout(self):
        async def attempt(remaining):  # pragma: no cover - must never run
            raise AssertionError("attempt started with no budget")

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(attempt, timeout_seconds=1e-9, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert excinfo.value.category == "timeout"

    async def test_deadline_cancels_overrunning_cooperative_attempt(self):
        # A slow-drip attempt that propagates cancellation is cancelled at the
        # total deadline and surfaces the normalized timeout — the HTTP client
        # timeout is defense in depth, not the sole bound.
        async def attempt(remaining):
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(attempt, timeout_seconds=0.05, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert excinfo.value.category == "timeout"

    async def test_cancellation_propagates_untouched(self):
        # Cancellation is control flow (BaseException): never normalized,
        # retried, or replaced by a partial result.
        calls = []

        async def attempt(remaining):
            calls.append(remaining)
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_with_bounded_retry(attempt, timeout_seconds=5.0, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c")
        assert len(calls) == 1  # never retried

    @pytest.mark.parametrize(
        "bad",
        [0, -1.0, float("nan"), float("inf"), True, "5",
         pytest.param(10**10000, id="huge-int")],
    )
    async def test_invalid_timeout_rejected_before_any_attempt(self, bad):
        # The loop's total deadline must not depend on callers preserving the
        # invariant: an infinite/non-positive/non-numeric/huge timeout is refused
        # before an attempt can run.
        async def attempt(remaining):  # pragma: no cover - must never run
            raise AssertionError("attempt started with an invalid timeout")

        with pytest.raises(ValueError):
            await run_with_bounded_retry(
                attempt, timeout_seconds=bad, role="chat", provider_id="openai", adapter_id="openai-compatible", correlation_id="c"
            )

    def test_no_error_callback_parameter_exists(self):
        # The timeout path builds its error from validated identity; there is no
        # caller-supplied callback that could return/raise a raw exception.
        import inspect

        params = set(inspect.signature(run_with_bounded_retry).parameters)
        assert "timeout_error" not in params
        assert {"role", "provider_id", "adapter_id", "correlation_id"} <= params

    async def test_bad_identity_fails_closed_before_any_attempt(self):
        async def attempt(remaining):  # pragma: no cover - must never run
            raise AssertionError("attempt started with an invalid identity")

        with pytest.raises(ValueError):
            await run_with_bounded_retry(
                attempt, timeout_seconds=5.0, role="chat",
                provider_id="surprise-ai", adapter_id="openai-compatible",
                correlation_id="c",
            )

    async def test_timeout_error_is_normalized_from_identity(self):
        async def attempt(remaining):
            await asyncio.sleep(10)

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(
                attempt, timeout_seconds=0.05, role="embedding",
                provider_id="cloud-temple", adapter_id="openai-compatible",
                correlation_id="corr-9",
            )
        err = excinfo.value
        assert err.category == "timeout"
        assert err.role == "embedding"
        assert err.provider_id == "cloud-temple"
        assert err.correlation_id == "corr-9"
        assert err.retryable is False

    async def test_attempt_suppressing_deadline_cannot_return_late_success(self):
        # A cooperative attempt that catches the deadline cancellation and
        # RETURNS a value just after it must not yield a late/partial success:
        # the post-attempt re-check converts the overrun to a normalized timeout.
        # (An attempt that NEVER returns after suppressing cancellation cannot be
        # force-stopped from here; that genuine hard bound lands with the
        # transport-owning adapters in #275 — see the run_with_bounded_retry
        # docstring.)
        async def attempt(remaining):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)  # keep working briefly, then return
            return "late-and-partial"

        with pytest.raises(InferenceError) as excinfo:
            await run_with_bounded_retry(
                attempt, timeout_seconds=0.03, role="chat",
                provider_id="openai", adapter_id="openai-compatible",
                correlation_id="c",
            )
        assert excinfo.value.category == "timeout"

    async def test_swallowed_caller_cancellation_is_reraised(self):
        # If the outer task is cancelled and the attempt swallows it, the loop
        # re-raises CancelledError rather than returning a value.
        async def attempt(remaining):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "swallowed"
            return "unreached"

        async def outer():
            return await run_with_bounded_retry(
                attempt, timeout_seconds=5.0, role="chat",
                provider_id="openai", adapter_id="openai-compatible",
                correlation_id="c",
            )

        task = asyncio.create_task(outer())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
