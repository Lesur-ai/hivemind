# -*- coding: utf-8 -*-
"""The single Hivemind-owned bounded retry loop (ADR-0027).

Every provider SDK is constructed with its implicit retry mechanism disabled;
this loop is the only retry authority and never changes provider or model
between attempts:

- at most TWO total attempts;
- a second attempt only for an explicitly transient ``rate_limited`` response
  (machine-readable ``Retry-After`` of at most five seconds) or a transport
  error proven pre-send (connection establishment failure before HTTP
  transmission);
- the request's total deadline covers connection, transmission, response
  read, the permitted delay, and both attempts — an attempt is not started
  when the remaining budget cannot contain its delay;
- authentication, quota, invalid/unsupported/content errors, response-read
  timeouts, malformed responses, cancellation, HTTP ``5xx``, proxy failures,
  and every ambiguous-delivery outcome are never retried;
- discovery probes perform zero retries (they never enter this loop).

Requests may additionally select the closed ``none`` policy, which reduces the
limit to one total attempt. It cannot increase the normal two-attempt ceiling.

Cancellation is control flow: ``asyncio.CancelledError`` is a
``BaseException`` and propagates through untouched.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from .errors import InferenceError
from .records import REQUEST_RETRY_POLICIES, _is_finite_number

# Maximum machine-readable Retry-After honored for the single transient
# rate-limit retry. Larger, missing, malformed, or negative values do not
# authorize a retry.
MAX_RETRY_AFTER_SECONDS = 5.0

MAX_ATTEMPTS = 2

# ADR-0027 sole-retry allowlist. A second attempt is permitted ONLY for an
# explicitly transient ``rate_limited`` response (a validated Retry-After delay
# in ``[0, MAX_RETRY_AFTER_SECONDS]``) or a transport failure proven pre-send
# (``unavailable`` with a delay of exactly ``0.0``). Every other category —
# auth, quota_exhausted, timeout, unsupported, invalid_request,
# content_rejected, invalid_response, and post-send/ambiguous ``unavailable`` —
# is never retried. This allowlist is enforced at the contract layer (below) so
# that no adapter can replay a forbidden category merely by constructing a
# ``retryable`` error, independent of the SDK-level retry being disabled.
RETRYABLE_CATEGORIES: tuple[str, ...] = ("rate_limited", "unavailable")


def parse_retry_after_seconds(raw_value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into an authorized delay.

    Only non-negative integer delta-seconds of at most
    ``MAX_RETRY_AFTER_SECONDS`` authorize the retry; HTTP-date forms and every
    malformed value return ``None`` (no retry) by design.
    """
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    # ASCII base-10 digits only. ``str.isdigit()`` alone ALSO accepts Unicode
    # "digit" characters — superscripts (``²``/``³``), circled (``①``), and
    # non-Latin decimals — some of which ``int()`` then rejects with a
    # ValueError that echoes the raw header value. A provider-controlled
    # ``Retry-After`` must never crash or leak here: require ASCII so any
    # non-base-10 value cleanly authorizes no retry (returns ``None``).
    if not (stripped.isascii() and stripped.isdigit()):
        return None
    # A hard digit-count ceiling applied BEFORE numeric conversion. The semantic
    # cap (MAX_RETRY_AFTER_SECONDS) is a single-digit number of seconds, so any
    # longer value is over-cap. Bounding here prevents ``int()``/``float()`` from
    # raising OverflowError (a ~309-digit value overflows float) or the CPython
    # integer-string digit-limit ValueError (a ~4300+-digit value) on a huge
    # provider-supplied digit run; the value check below still rejects any
    # short-but-over-cap value. Three digits leaves headroom for leading zeros
    # while staying trivially convertible.
    if len(stripped) > 3:
        return None
    delay = float(int(stripped))
    if delay > MAX_RETRY_AFTER_SECONDS:
        return None
    return delay


class AttemptFailure(Exception):
    """Internal adapter signal: one attempt failed with a normalized error.

    ``retry_delay_seconds`` is ``None`` for a non-retryable failure, ``0.0``
    for a proven pre-send transport failure, and the validated ``Retry-After``
    delay for an explicitly transient rate limit.
    """

    def __init__(self, error: InferenceError, retry_delay_seconds: float | None) -> None:
        super().__init__(str(error))
        self.error = error
        self.retry_delay_seconds = retry_delay_seconds
        if retry_delay_seconds is not None and not error.retryable:
            raise ValueError("a retry delay requires a retryable error")
        if retry_delay_seconds is None and error.retryable:
            raise ValueError("a retryable error requires an explicit retry delay")
        if error.retryable:
            # Fail-closed enforcement of the ADR-0027 sole-retry allowlist. A
            # retryable failure must be one of exactly two proven-safe shapes;
            # anything else (a forbidden category, or a malformed/out-of-range
            # delay) is rejected here so it can never reach the retry loop.
            delay = retry_delay_seconds
            if (
                not isinstance(delay, (int, float))
                or isinstance(delay, bool)
                or not _is_finite_number(delay)
            ):
                raise ValueError("a retry delay must be a finite number")
            if error.category not in RETRYABLE_CATEGORIES:
                raise ValueError(
                    f"category '{error.category}' is never retryable under "
                    "ADR-0027 (only transient rate_limited or pre-send "
                    "unavailable may retry)"
                )
            if error.category == "rate_limited":
                if not (0.0 <= delay <= MAX_RETRY_AFTER_SECONDS):
                    raise ValueError(
                        "a rate_limited retry delay must be within "
                        "[0, MAX_RETRY_AFTER_SECONDS]"
                    )
            else:  # unavailable: only the proven pre-send failure, with no wait
                if delay != 0.0:
                    raise ValueError(
                        "a pre-send unavailable retry uses a delay of exactly 0.0"
                    )


async def run_with_bounded_retry(
    attempt: Callable[[float], Awaitable[object]],
    *,
    timeout_seconds: float,
    role: str,
    provider_id: str,
    adapter_id: str,
    correlation_id: str,
    retry_policy: str = "bounded",
):
    """Run ``attempt(remaining_budget_seconds)`` under the frozen policy.

    ``attempt`` either returns the successful normalized result or raises
    :class:`AttemptFailure`. The normalized ``timeout`` error surfaced when the
    total deadline is exhausted — before an attempt (or the authorized delay)
    can run, or when a running attempt overruns it — is constructed inside this
    boundary from the caller's already-validated safe identity. There is no
    caller-supplied error callback: the retry API cannot return or propagate an
    untrusted, possibly secret-bearing exception on the timeout path.

    The total deadline is enforced by COOPERATIVE cancellation: every attempt
    is awaited under ``asyncio.timeout(remaining)``, so an attempt that
    propagates ``CancelledError`` — which every Hivemind adapter does — is
    stopped at the deadline and surfaces the normalized timeout error. A
    late/partial success that returns just after its own cancellation, or a
    swallowed caller cancellation, is rejected by the post-attempt re-check
    below. The per-request HTTP client timeouts remain defense in depth.

    An attempt that SUPPRESSES cancellation and keeps running (or never
    returns) cannot be force-stopped from here — Python cancellation is
    cooperative — so it can overrun the wall clock until it returns. The
    genuine hard bound against that case (an owned child task the loop
    abandons at the deadline while force-closing its owned transport) belongs
    with the provider adapters that own the transport (#275); this
    dependency-neutral foundation has no transport to close. Caller
    cancellation is preserved (only the expiry of THIS deadline converts to
    the normalized timeout). ``retry_policy`` is a closed reduction of the
    ceiling: ``bounded`` permits at most :data:`MAX_ATTEMPTS`, while ``none``
    permits exactly one attempt and no replay.
    """
    if retry_policy not in REQUEST_RETRY_POLICIES:
        raise ValueError(
            f"retry_policy must be one of {REQUEST_RETRY_POLICIES}"
        )

    # The total deadline is the loop's promised bound (cooperative — see the
    # docstring); validate it at the adapter-facing entry rather than trusting
    # every present/future caller. _is_finite_number rejects a huge int before
    # the ``<= 0`` comparison, so validation stays total and value-free.
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not _is_finite_number(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number")
    # The sole timeout error, built once from validated identity. InferenceError
    # validates every field, so it cannot carry raw provider text; a bad
    # identity fails closed here before any attempt runs.
    timeout_error = InferenceError(
        category="timeout",
        role=role,
        provider_id=provider_id,
        adapter_id=adapter_id,
        retryable=False,
        correlation_id=correlation_id,
    )
    deadline = time.monotonic() + timeout_seconds
    attempt_limit = 1 if retry_policy == "none" else MAX_ATTEMPTS
    for attempt_number in range(1, attempt_limit + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error
        try:
            async with asyncio.timeout(remaining):
                result = await attempt(remaining)
        except TimeoutError:
            raise timeout_error from None
        except AttemptFailure as failure:
            delay = failure.retry_delay_seconds
            if attempt_number >= attempt_limit or delay is None:
                raise failure.error from None
            if time.monotonic() + delay >= deadline:
                # The remaining budget cannot contain the authorized delay:
                # surface the normalized failure instead of starting an
                # attempt that could not complete.
                raise failure.error from None
            if delay > 0:
                # Bound the authorized delay by the SAME hard deadline: a sleep
                # that oversleeps under scheduler load must not push completion
                # past the total deadline. Its expiry is the normalized timeout.
                try:
                    async with asyncio.timeout(deadline - time.monotonic()):
                        await asyncio.sleep(delay)
                except TimeoutError:
                    raise timeout_error from None
            continue
        # The attempt returned a value. asyncio.timeout() cancels THIS task, so
        # a cooperative attempt that caught the deadline cancellation (or a
        # caller cancellation) and returned just after it would otherwise yield
        # a result PAST the deadline. Cancellation is control flow and the
        # deadline must not be defeated by such a late/partial success: re-raise
        # a swallowed caller cancellation, then convert deadline expiry to the
        # normalized timeout. (An attempt that NEVER returns after suppressing
        # cancellation cannot be force-stopped here; that genuine hard bound
        # lands with the transport-owning adapters in #275 — see the docstring.)
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise asyncio.CancelledError()
        if time.monotonic() >= deadline:
            raise timeout_error
        return result
    raise AssertionError("unreachable: bounded retry loop always returns or raises")
