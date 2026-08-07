# -*- coding: utf-8 -*-
"""Bounded, opt-in deep inference readiness for the Hivemind core.

The public health route never imports or calls this module.  The manage-only
``inference_self_test`` tool owns the only paid readiness path; authenticated
``system_health`` may read a fresh matching cache entry but never refresh it.

Each event loop gets independent locks, tasks, and cache state.  This matters
both for application ownership and for test runners that create several loops
inside one interpreter: an ``asyncio.Lock`` or ``Task`` must never migrate to a
different loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hivemind_inference import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    EmbeddingRequest,
    EmbeddingResult,
    InferenceError,
    InferenceRuntime,
)

SELF_TEST_CACHE_SECONDS = 300.0
SELF_TEST_ROLE_TIMEOUT_SECONDS = 15.0
SELF_TEST_CHAT_MAX_OUTPUT_TOKENS = 8
# Match the provider-runtime close budget: lifecycle shutdown must terminate
# even when a provider delays or suppresses cooperative cancellation.
SELF_TEST_SHUTDOWN_BUDGET_SECONDS = 10.0

# Fixed, synthetic, source-controlled inputs.  The MCP handler accepts no
# provider, model, endpoint, credential, prompt, or embedding input parameter.
_CHAT_MESSAGES = (
    ChatMessage(
        role="system",
        content="This is a synthetic service readiness check.",
    ),
    ChatMessage(role="user", content="Reply with OK."),
)
_EMBEDDING_INPUTS = ("synthetic hivemind readiness check",)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SelfTestRoleEvidence:
    """Closed, secret-free role result stored by the readiness cache."""

    configured: bool
    readiness: str
    provider_id: str | None = None
    adapter_id: str | None = None
    configured_model: str | None = None
    resolved_model: str | None = None
    model_evidence: str | None = None
    finish_reason: str | None = None
    expected_dimensions: int | None = None
    effective_dimensions: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error_category: str | None = None
    retryable: bool | None = None
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "configured": self.configured,
            "readiness": self.readiness,
            "evidence": "inference" if self.configured else "none",
        }
        for name in (
            "provider_id",
            "adapter_id",
            "configured_model",
            "resolved_model",
            "model_evidence",
            "finish_reason",
            "expected_dimensions",
            "effective_dimensions",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "error_category",
            "retryable",
            "correlation_id",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = item
        return value


@dataclass(frozen=True, slots=True)
class SelfTestCacheEntry:
    """One completed deep check, keyed by its exact safe profile digest."""

    profile_fingerprint: str
    checked_at: str
    expires_at: str
    expires_monotonic: float
    chat: SelfTestRoleEvidence
    embedding: SelfTestRoleEvidence

    @property
    def readiness(self) -> str:
        if self.chat.readiness == self.embedding.readiness == "ready":
            return "ready"
        return "not_ready"

    def to_dict(self, *, cached: bool) -> dict[str, Any]:
        return {
            "status": "ok" if self.readiness == "ready" else "error",
            "readiness": self.readiness,
            "evidence": "inference",
            "cached": cached,
            "profile_fingerprint": self.profile_fingerprint,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "roles": {
                "chat": self.chat.to_dict(),
                "embedding": self.embedding.to_dict(),
            },
        }


class _LoopState:
    __slots__ = ("lock", "cache", "inflight", "closing")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cache: dict[str, SelfTestCacheEntry] = {}
        self.inflight: dict[str, asyncio.Task[SelfTestCacheEntry]] = {}
        self.closing = False


class InferenceSelfTestShutdownIncomplete(RuntimeError):
    """Owned readiness work did not settle inside the shutdown budget.

    The message is deliberately fixed and value-free.  Unsettled tasks remain
    strongly referenced by their loop state so lifecycle code can report a
    failed shutdown without pretending that paid provider work disappeared.
    """

    def __init__(self) -> None:
        super().__init__("inference self-test shutdown incomplete")


_LOOP_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState]" = (
    weakref.WeakKeyDictionary()
)


def _state_for_current_loop() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _LOOP_STATES.get(loop)
    if state is None:
        state = _LoopState()
        _LOOP_STATES[loop] = state
    return state


def inference_profile_fingerprint(runtime: InferenceRuntime) -> str:
    """Digest every field that can change the combined role behavior.

    Endpoint URLs remain secret-bearing.  Only their canonical SHA-256 values
    enter the digest, alongside the explicit provider/adapter/model/limit/
    dimension/source fields.  The resulting digest is safe operational
    metadata; the payload used to derive it is never returned.
    """

    def role_payload(profile) -> dict[str, Any] | None:
        if profile is None:
            return None
        payload = profile.safe_snapshot()
        payload["endpoint_sha256"] = profile.endpoint_sha256
        return payload

    payload = {
        "chat": role_payload(runtime.config.chat),
        "embedding": role_payload(runtime.config.embedding),
        "legacy_active": runtime.config.legacy_active,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _not_configured() -> SelfTestRoleEvidence:
    # Missing roles are an explicit failed readiness check, not an absent or
    # optimistic state, and no request/provider is constructed for them.
    return SelfTestRoleEvidence(
        configured=False,
        readiness="not_ready",
        error_category="unsupported",
        retryable=False,
    )


def _failed_role(
    profile,
    *,
    correlation_id: str,
    error_category: str,
    retryable: bool,
) -> SelfTestRoleEvidence:
    fields: dict[str, Any] = {
        "configured": True,
        "readiness": "not_ready",
        "provider_id": profile.provider_id,
        "adapter_id": profile.adapter_id,
        "configured_model": profile.configured_model,
        "error_category": error_category,
        "retryable": retryable,
        "correlation_id": correlation_id,
    }
    if profile.role == "embedding":
        fields["expected_dimensions"] = profile.expected_dimensions
    return SelfTestRoleEvidence(**fields)


def _normalized_failure(profile, request, failure: Exception) -> SelfTestRoleEvidence:
    if isinstance(failure, InferenceError):
        if (
            failure.role == profile.role
            and failure.provider_id == profile.provider_id
            and failure.adapter_id == profile.adapter_id
            and failure.correlation_id == request.correlation_id
        ):
            return _failed_role(
                profile,
                correlation_id=request.correlation_id,
                error_category=failure.category,
                retryable=failure.retryable,
            )
        return _failed_role(
            profile,
            correlation_id=request.correlation_id,
            error_category="invalid_response",
            retryable=False,
        )
    # Raw provider/transport exceptions and incoherent normalized envelopes
    # collapse to one fixed value-free category.
    return _failed_role(
        profile,
        correlation_id=request.correlation_id,
        error_category="unavailable",
        retryable=False,
    )


async def _test_chat(runtime: InferenceRuntime) -> SelfTestRoleEvidence:
    profile = runtime.config.chat
    if profile is None:
        return _not_configured()
    request = ChatRequest(
        messages=_CHAT_MESSAGES,
        timeout_seconds=SELF_TEST_ROLE_TIMEOUT_SECONDS,
        max_output_tokens=min(
            SELF_TEST_CHAT_MAX_OUTPUT_TOKENS,
            profile.max_output_tokens,
        ),
        retry_policy="none",
    )
    try:
        result = await runtime.chat_provider().complete(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - outward evidence is normalized
        return _normalized_failure(profile, request, exc)
    if (
        type(result) is not ChatResult
        or result.correlation_id != request.correlation_id
        or result.configured_model != profile.configured_model
        or (
            result.resolved_model is not None
            and result.resolved_model != profile.configured_model
        )
    ):
        return _failed_role(
            profile,
            correlation_id=request.correlation_id,
            error_category="invalid_response",
            retryable=False,
        )
    return SelfTestRoleEvidence(
        configured=True,
        readiness="ready",
        provider_id=profile.provider_id,
        adapter_id=profile.adapter_id,
        configured_model=result.configured_model,
        resolved_model=result.resolved_model,
        model_evidence=result.model_evidence,
        finish_reason=result.finish_reason,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        correlation_id=result.correlation_id,
    )


async def _test_embedding(runtime: InferenceRuntime) -> SelfTestRoleEvidence:
    profile = runtime.config.embedding
    if profile is None:
        return _not_configured()
    request = EmbeddingRequest(
        inputs=_EMBEDDING_INPUTS,
        input_type="query",
        timeout_seconds=SELF_TEST_ROLE_TIMEOUT_SECONDS,
        retry_policy="none",
    )
    try:
        result = await runtime.embedding_provider().embed(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - outward evidence is normalized
        return _normalized_failure(profile, request, exc)
    if (
        type(result) is not EmbeddingResult
        or result.correlation_id != request.correlation_id
        or result.configured_model != profile.configured_model
        or (
            result.resolved_model is not None
            and result.resolved_model != profile.configured_model
        )
        or result.effective_dimensions != profile.expected_dimensions
        or len(result.vectors) != 1
    ):
        return _failed_role(
            profile,
            correlation_id=request.correlation_id,
            error_category="invalid_response",
            retryable=False,
        )
    return SelfTestRoleEvidence(
        configured=True,
        readiness="ready",
        provider_id=profile.provider_id,
        adapter_id=profile.adapter_id,
        configured_model=result.configured_model,
        resolved_model=result.resolved_model,
        model_evidence=result.model_evidence,
        expected_dimensions=profile.expected_dimensions,
        effective_dimensions=result.effective_dimensions,
        input_tokens=result.input_tokens,
        total_tokens=result.total_tokens,
        correlation_id=result.correlation_id,
    )


async def _execute_and_cache(
    state: _LoopState,
    runtime: InferenceRuntime,
    profile_fingerprint: str,
) -> SelfTestCacheEntry:
    current = asyncio.current_task()
    try:
        # Own each role task until BOTH have settled. ``asyncio.gather``
        # returns immediately when one child is independently cancelled and
        # deliberately leaves its sibling running; the parent would then drop
        # out of ``state.inflight`` and a new caller could duplicate the still
        # active paid role request. TaskGroup waits for a lone cancelled
        # child's sibling, and cancels/drains both children when the parent is
        # cancelled during serving-window shutdown.
        async with asyncio.TaskGroup() as roles:
            chat_task = roles.create_task(_test_chat(runtime))
            embedding_task = roles.create_task(_test_embedding(runtime))
        chat = chat_task.result()
        embedding = embedding_task.result()
        checked = _utc_now()
        expires = checked + timedelta(seconds=SELF_TEST_CACHE_SECONDS)
        entry = SelfTestCacheEntry(
            profile_fingerprint=profile_fingerprint,
            checked_at=_format_timestamp(checked),
            expires_at=_format_timestamp(expires),
            expires_monotonic=_monotonic() + SELF_TEST_CACHE_SECONDS,
            chat=chat,
            embedding=embedding,
        )
        # Shutdown is terminal for the serving window.  An operation that
        # happened to finish during the transition must not republish cache
        # evidence after shutdown cleared it.
        if not state.closing:
            state.cache[profile_fingerprint] = entry
        return entry
    finally:
        if state.inflight.get(profile_fingerprint) is current:
            state.inflight.pop(profile_fingerprint, None)


async def run_inference_self_test(runtime: InferenceRuntime) -> dict[str, Any]:
    """Run or join the bounded check for ``runtime``'s exact frozen profile."""

    profile_fingerprint = inference_profile_fingerprint(runtime)
    state = _state_for_current_loop()
    cache_hit = False
    async with state.lock:
        if state.closing:
            raise RuntimeError("inference self-test is shutting down")
        cached = state.cache.get(profile_fingerprint)
        if cached is not None and _monotonic() < cached.expires_monotonic:
            task = None
            entry = cached
            cache_hit = True
        else:
            state.cache.pop(profile_fingerprint, None)
            task = state.inflight.get(profile_fingerprint)
            if task is None:
                task = asyncio.create_task(
                    _execute_and_cache(state, runtime, profile_fingerprint),
                    name="hivemind-inference-self-test",
                )
                state.inflight[profile_fingerprint] = task

    if not cache_hit:
        # A caller cancellation stops only that waiter.  The one owned paid
        # operation remains strongly referenced in ``state.inflight`` and runs
        # to its cacheable outcome; another caller joins it instead of paying
        # for a duplicate operation.
        entry = await asyncio.shield(task)
    return entry.to_dict(cached=cache_hit)


def read_fresh_inference_self_test(
    runtime: InferenceRuntime,
) -> SelfTestCacheEntry | None:
    """Read matching fresh evidence in the current loop, without refreshing."""

    try:
        profile_fingerprint = inference_profile_fingerprint(runtime)
        state = _LOOP_STATES.get(asyncio.get_running_loop())
    except (RuntimeError, ValueError):
        return None
    if state is None or state.closing:
        return None
    entry = state.cache.get(profile_fingerprint)
    if entry is None:
        return None
    if _monotonic() >= entry.expires_monotonic:
        state.cache.pop(profile_fingerprint, None)
        return None
    return entry


def open_inference_self_test_window() -> None:
    """Reopen cache admission after a successful serving-window startup."""

    try:
        state = _LOOP_STATES.get(asyncio.get_running_loop())
    except RuntimeError:
        return
    if state is None:
        return
    if any(not task.done() for task in state.inflight.values()):
        raise RuntimeError(
            "a previous inference self-test operation has not finished"
        )
    state.inflight.clear()
    state.cache.clear()
    state.closing = False


async def close_inference_self_test_window() -> None:
    """Cancel and fully drain this loop's owned readiness work.

    Caller cancellation is recorded while owned tasks drain, then re-raised.
    A wall-clock budget prevents a provider that delays or suppresses
    cancellation from wedging process shutdown: unfinished tasks remain
    referenced and the fixed
    :class:`InferenceSelfTestShutdownIncomplete` verdict lets the outer
    lifecycle continue closing provider transports while still failing the
    serving-window shutdown honestly.
    """

    loop = asyncio.get_running_loop()
    state = _LOOP_STATES.get(loop)
    if state is None:
        return
    # Terminal before the first await: no concurrent admission can start a new
    # paid task after we take the snapshot below.
    state.closing = True
    state.cache.clear()
    tasks = tuple(state.inflight.values())
    for task in tasks:
        if not task.done():
            task.cancel()

    cancellation: asyncio.CancelledError | None = None
    pending = {task for task in tasks if not task.done()}
    deadline = loop.time() + SELF_TEST_SHUTDOWN_BUDGET_SECONDS
    while pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            # ``asyncio.wait`` expires without cancelling the tasks it waits
            # on.  That permits a true wall-clock bound while a cancellation
            # of this finalizer is recorded instead of abandoning cleanup at
            # the first scheduling boundary.
            _done, pending = await asyncio.wait(pending, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            pending = {task for task in pending if not task.done()}

    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()  # retrieve any unexpected owned-task failure
    incomplete = any(not task.done() for task in tasks)
    if not incomplete:
        state.inflight.clear()
    if cancellation is not None:
        raise cancellation
    if incomplete:
        # Keep ``state.inflight`` intact and ``closing`` true.  The next
        # lifecycle finalizer may close transports, but this serving window
        # must report failure rather than wedge or claim clean ownership.
        raise InferenceSelfTestShutdownIncomplete()


def reset_inference_self_test_for_tests() -> None:
    """Drop current-loop state only when it owns no running task."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    state = _LOOP_STATES.get(loop)
    if state is None:
        return
    if any(not task.done() for task in state.inflight.values()):
        raise RuntimeError("cannot reset an active inference self-test")
    _LOOP_STATES.pop(loop, None)
