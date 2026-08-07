# -*- coding: utf-8 -*-
"""P13-6 — bounded deep readiness and health-cache integration.

All provider work uses deterministic in-process doubles.  The suite proves
manage-before-runtime authorization, fixed zero-retry inputs, per-loop
single-flight/cooldown, cancellation ownership, shutdown ordering, exact
profile isolation, typed redaction, and public/authenticated health separation.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from hivemind_inference import (
    ChatResult,
    EmbeddingResult,
    InferenceConfig,
    InferenceError,
    InferenceRuntime,
    ProbeResult,
)
from live_mem.auth.context import current_token_info
from live_mem.core import inference_readiness as readiness
from live_mem.core import inference_runtime as core_runtime
from live_mem.tools import system as system_tools
from live_mem.tools.exposure import (
    TOOL_EXPOSURES,
    ToolAudience,
    ToolOperation,
    ToolPermission,
    discovery_names_for_permission,
)
from tests.fakes.inference_fakes import (
    make_chat_profile,
    make_embedding_profile,
)

PLANTED_SECRET = "sk-self-test-must-never-leak"


class _ChatProvider:
    def __init__(self, profile, *, block: bool = False) -> None:
        self.profile = profile
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.cancel_cleanup_release = asyncio.Event()
        self.close_calls = 0
        if not block:
            self.release.set()
            self.cancel_cleanup_release.set()

    async def complete(self, request):
        self.requests.append(request)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.cancel_cleanup_release.wait()
            raise
        return ChatResult(
            text=PLANTED_SECRET,
            configured_model=self.profile.configured_model,
            model_evidence="provider_reported",
            resolved_model=self.profile.configured_model,
            finish_reason="stop",
            input_tokens=3,
            output_tokens=1,
            total_tokens=4,
            correlation_id=request.correlation_id,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


class _EmbeddingProvider:
    def __init__(self, profile, *, block: bool = False) -> None:
        self.profile = profile
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.cancel_cleanup_release = asyncio.Event()
        self.close_calls = 0
        if not block:
            self.release.set()
            self.cancel_cleanup_release.set()

    async def embed(self, request):
        self.requests.append(request)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.cancel_cleanup_release.wait()
            raise
        return EmbeddingResult(
            vectors=(tuple(0.25 for _ in range(self.profile.expected_dimensions)),),
            configured_model=self.profile.configured_model,
            model_evidence="provider_reported",
            resolved_model=self.profile.configured_model,
            effective_dimensions=self.profile.expected_dimensions,
            input_tokens=2,
            total_tokens=2,
            correlation_id=request.correlation_id,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


def _runtime(
    *,
    chat: bool = True,
    embedding: bool = True,
    block: bool = False,
    chat_model: str = "self-test-chat",
) -> tuple[InferenceRuntime, _ChatProvider | None, _EmbeddingProvider | None]:
    chat_profile = make_chat_profile(
        configured_model=chat_model,
        max_output_tokens=64,
    ) if chat else None
    embedding_profile = make_embedding_profile(
        configured_model="self-test-embedding",
        expected_dimensions=3,
    ) if embedding else None
    runtime = InferenceRuntime(
        InferenceConfig(
            chat=chat_profile,
            embedding=embedding_profile,
            legacy_active=False,
        )
    )
    chat_provider = _ChatProvider(chat_profile, block=block) if chat_profile else None
    embedding_provider = (
        _EmbeddingProvider(embedding_profile, block=block)
        if embedding_profile
        else None
    )
    runtime._chat_provider = chat_provider
    runtime._embedding_provider = embedding_provider
    return runtime, chat_provider, embedding_provider


def _system_handler(mcp: FastMCP, name: str):
    return mcp._tool_manager._tools[name].fn


async def _healthy_probe(_runtime, _role):
    return ProbeResult(
        connectivity="reachable",
        discovery="available",
        model_available=True,
        latency_ms=1.0,
    )


@pytest.mark.asyncio
async def test_self_test_uses_fixed_bounded_zero_retry_inputs_and_safe_output():
    runtime, chat, embedding = _runtime()

    result = await readiness.run_inference_self_test(runtime)

    assert result["status"] == "ok"
    assert result["readiness"] == "ready"
    assert result["cached"] is False
    assert len(chat.requests) == len(embedding.requests) == 1
    chat_request = chat.requests[0]
    embedding_request = embedding.requests[0]
    assert chat_request.messages == readiness._CHAT_MESSAGES
    assert chat_request.max_output_tokens <= 8
    assert chat_request.retry_policy == "none"
    assert embedding_request.inputs == readiness._EMBEDDING_INPUTS
    assert embedding_request.input_type == "query"
    assert embedding_request.retry_policy == "none"
    assert result["roles"]["chat"]["correlation_id"] == (
        chat_request.correlation_id
    )
    assert result["roles"]["embedding"]["correlation_id"] == (
        embedding_request.correlation_id
    )
    rendered = json.dumps(result, sort_keys=True)
    assert PLANTED_SECRET not in rendered
    assert "vectors" not in rendered
    assert "text" not in rendered
    assert "api_key" not in rendered
    assert "endpoint" not in rendered


@pytest.mark.asyncio
async def test_incoherent_model_evidence_cannot_turn_provider_text_into_output():
    runtime, chat, _embedding = _runtime()

    async def unsafe_complete(request):
        chat.requests.append(request)
        return ChatResult(
            text=PLANTED_SECRET,
            configured_model=chat.profile.configured_model,
            model_evidence="provider_reported",
            resolved_model=PLANTED_SECRET,
            finish_reason="stop",
            correlation_id=request.correlation_id,
        )

    chat.complete = unsafe_complete
    result = await readiness.run_inference_self_test(runtime)

    assert result["roles"]["chat"]["readiness"] == "not_ready"
    assert result["roles"]["chat"]["error_category"] == "invalid_response"
    assert PLANTED_SECRET not in json.dumps(result, sort_keys=True)


@pytest.mark.asyncio
async def test_parallel_callers_join_one_paid_operation_then_hit_cooldown():
    runtime, chat, embedding = _runtime(block=True)

    first = asyncio.create_task(readiness.run_inference_self_test(runtime))
    second = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await asyncio.gather(chat.entered.wait(), embedding.entered.wait())
    assert len(chat.requests) == len(embedding.requests) == 1

    chat.release.set()
    embedding.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["profile_fingerprint"] == second_result["profile_fingerprint"]

    cached = await readiness.run_inference_self_test(runtime)
    assert cached["cached"] is True
    assert len(chat.requests) == len(embedding.requests) == 1


@pytest.mark.asyncio
async def test_failed_readiness_is_cached_for_the_same_cost_cooldown():
    runtime, chat, embedding = _runtime()

    async def rate_limited(request):
        chat.requests.append(request)
        raise InferenceError(
            category="rate_limited",
            role="chat",
            provider_id=chat.profile.provider_id,
            adapter_id=chat.profile.adapter_id,
            retryable=True,
            correlation_id=request.correlation_id,
        )

    chat.complete = rate_limited
    first = await readiness.run_inference_self_test(runtime)
    second = await readiness.run_inference_self_test(runtime)

    assert first["readiness"] == "not_ready"
    assert first["roles"]["chat"]["error_category"] == "rate_limited"
    assert first["roles"]["chat"]["retryable"] is True
    assert second["cached"] is True
    assert second["readiness"] == "not_ready"
    assert len(chat.requests) == len(embedding.requests) == 1


@pytest.mark.asyncio
async def test_fresh_failed_self_test_degrades_only_authenticated_role_child(
    monkeypatch,
):
    runtime, chat, embedding = _runtime()

    async def rejected(request):
        chat.requests.append(request)
        raise InferenceError(
            category="auth",
            role="chat",
            provider_id=chat.profile.provider_id,
            adapter_id=chat.profile.adapter_id,
            retryable=False,
            correlation_id=request.correlation_id,
        )

    chat.complete = rejected
    await readiness.run_inference_self_test(runtime)
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((runtime, False))
    monkeypatch.setattr(core_runtime, "_probe_role_safely", _healthy_probe)
    try:
        authenticated = await core_runtime.build_llmaas_health_block(
            authenticated=True
        )
        public = await core_runtime.build_llmaas_health_block(authenticated=False)
    finally:
        core_runtime._holder.restore_for_tests(holder_snapshot)

    assert authenticated["status"] == "ok"
    assert authenticated["chat"]["status"] == "error"
    assert authenticated["chat"]["readiness"] == "not_ready"
    assert authenticated["chat"]["error_category"] == "auth"
    assert authenticated["embedding"]["status"] == "ok"
    assert authenticated["embedding"]["readiness"] == "ready"
    assert public["status"] == "ok"
    assert public["chat"]["status"] == "ok"
    assert public["chat"]["readiness"] == "unknown"
    assert len(chat.requests) == len(embedding.requests) == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_or_duplicate_owned_operation():
    runtime, chat, embedding = _runtime(block=True)
    first = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await asyncio.gather(chat.entered.wait(), embedding.entered.wait())

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    joined = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await asyncio.sleep(0)
    assert len(chat.requests) == len(embedding.requests) == 1
    chat.release.set()
    embedding.release.set()
    assert (await joined)["readiness"] == "ready"


@pytest.mark.asyncio
async def test_one_cancelled_role_cannot_escape_ownership_or_duplicate_sibling():
    runtime, chat, embedding = _runtime(block=True)

    async def independently_cancelled(request):
        chat.requests.append(request)
        raise asyncio.CancelledError("provider role cancelled independently")

    chat.complete = independently_cancelled
    first = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await embedding.entered.wait()
    await asyncio.sleep(0)
    assert first.done() is False

    joined = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await asyncio.sleep(0)
    assert len(chat.requests) == len(embedding.requests) == 1

    embedding.release.set()
    for waiter in (first, joined):
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert len(chat.requests) == len(embedding.requests) == 1


@pytest.mark.asyncio
async def test_cache_expires_and_exact_profile_change_cannot_reuse_it(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(readiness, "_monotonic", lambda: clock[0])
    runtime, chat, embedding = _runtime()

    await readiness.run_inference_self_test(runtime)
    assert (await readiness.run_inference_self_test(runtime))["cached"] is True
    clock[0] += readiness.SELF_TEST_CACHE_SECONDS + 0.001
    assert (await readiness.run_inference_self_test(runtime))["cached"] is False
    assert len(chat.requests) == len(embedding.requests) == 2

    changed, changed_chat, changed_embedding = _runtime(
        chat_model="different-chat-model"
    )
    changed_result = await readiness.run_inference_self_test(changed)
    assert changed_result["cached"] is False
    assert len(changed_chat.requests) == len(changed_embedding.requests) == 1


def test_cache_and_single_flight_state_are_event_loop_scoped():
    runtime, chat, embedding = _runtime()

    first = asyncio.run(readiness.run_inference_self_test(runtime))
    second = asyncio.run(readiness.run_inference_self_test(runtime))

    assert first["profile_fingerprint"] == second["profile_fingerprint"]
    assert first["cached"] is second["cached"] is False
    assert len(chat.requests) == len(embedding.requests) == 2


@pytest.mark.asyncio
async def test_missing_roles_fail_closed_without_constructing_a_provider():
    runtime, chat, embedding = _runtime(chat=False, embedding=False)

    result = await readiness.run_inference_self_test(runtime)

    assert chat is embedding is None
    assert result["readiness"] == "not_ready"
    for role in ("chat", "embedding"):
        assert result["roles"][role] == {
            "configured": False,
            "readiness": "not_ready",
            "evidence": "none",
            "error_category": "unsupported",
            "retryable": False,
        }


@pytest.mark.asyncio
async def test_chat_only_self_test_never_claims_embedding_inference_evidence(
    monkeypatch,
):
    runtime, chat, embedding = _runtime(chat=True, embedding=False)
    result = await readiness.run_inference_self_test(runtime)
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((runtime, False))

    async def probe(_runtime, role):
        return await _healthy_probe(_runtime, role) if role == "chat" else None

    monkeypatch.setattr(core_runtime, "_probe_role_safely", probe)
    try:
        health = await core_runtime.build_llmaas_health_block(authenticated=True)
    finally:
        core_runtime._holder.restore_for_tests(holder_snapshot)

    assert chat is not None and len(chat.requests) == 1
    assert embedding is None
    assert result["roles"]["chat"]["evidence"] == "inference"
    assert result["roles"]["embedding"]["evidence"] == "none"
    assert health["embedding"]["configured"] is False
    assert health["embedding"]["connectivity"] == "not_configured"
    assert health["embedding"]["readiness"] == "not_ready"
    assert health["embedding"]["evidence"] == "none"
    assert health["embedding"]["status"] == "warning"


@pytest.mark.asyncio
async def test_shutdown_drains_cancelled_self_test_before_transport_close():
    runtime, chat, embedding = _runtime(block=True)
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((runtime, False))
    waiter = asyncio.create_task(readiness.run_inference_self_test(runtime))
    try:
        await asyncio.gather(chat.entered.wait(), embedding.entered.wait())
        closer = asyncio.create_task(
            core_runtime.close_inference_runtime_if_initialized()
        )
        await asyncio.gather(
            chat.cancel_seen.wait(),
            embedding.cancel_seen.wait(),
        )
        assert chat.close_calls == embedding.close_calls == 0

        chat.cancel_cleanup_release.set()
        embedding.cancel_cleanup_release.set()
        await closer
        assert chat.close_calls == embedding.close_calls == 1
        assert runtime.is_fully_closed
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        core_runtime._holder.restore_for_tests(holder_snapshot)


@pytest.mark.asyncio
async def test_shutdown_budget_reports_incomplete_and_retains_blocked_work(
    monkeypatch,
):
    runtime, chat, embedding = _runtime(block=True)
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((runtime, False))
    waiter = asyncio.create_task(readiness.run_inference_self_test(runtime))
    await asyncio.gather(chat.entered.wait(), embedding.entered.wait())
    monkeypatch.setattr(readiness, "SELF_TEST_SHUTDOWN_BUDGET_SECONDS", 0.0)

    try:
        with pytest.raises(readiness.InferenceSelfTestShutdownIncomplete):
            await core_runtime.close_inference_runtime_if_initialized()

        state = readiness._state_for_current_loop()
        assert state.closing is True
        assert state.inflight
        assert any(not task.done() for task in state.inflight.values())
        assert chat.close_calls == embedding.close_calls == 1
        assert runtime.is_fully_closed
    finally:
        chat.cancel_cleanup_release.set()
        embedding.cancel_cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await readiness.close_inference_self_test_window()
        core_runtime._holder.restore_for_tests(holder_snapshot)


@pytest.mark.asyncio
async def test_authenticated_health_reads_fresh_matching_cache_public_ignores_it(
    monkeypatch,
):
    clock = [10.0]
    monkeypatch.setattr(readiness, "_monotonic", lambda: clock[0])
    runtime, chat, embedding = _runtime()
    await readiness.run_inference_self_test(runtime)
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((runtime, False))
    monkeypatch.setattr(core_runtime, "_probe_role_safely", _healthy_probe)
    try:
        authenticated = await core_runtime.build_llmaas_health_block(
            authenticated=True
        )
        public = await core_runtime.build_llmaas_health_block(authenticated=False)
        clock[0] += readiness.SELF_TEST_CACHE_SECONDS + 1.0
        expired = await core_runtime.build_llmaas_health_block(authenticated=True)
    finally:
        core_runtime._holder.restore_for_tests(holder_snapshot)

    for role in ("chat", "embedding"):
        assert authenticated[role]["readiness"] == "ready"
        assert authenticated[role]["evidence"] == "inference"
        assert "checked_at" in authenticated[role]
        assert "expires_at" in authenticated[role]
        assert public[role]["readiness"] == "unknown"
        assert public[role]["evidence"] == "discovery"
        assert "checked_at" not in public[role]
        assert "resolved_model" not in public[role]
        assert expired[role]["readiness"] == "unknown"
        assert expired[role]["evidence"] == "discovery"

    assert chat is not None
    assert embedding is not None
    assert len(chat.requests) == 1
    assert len(embedding.requests) == 1


@pytest.mark.asyncio
async def test_authenticated_health_rejects_cache_from_a_different_profile(
    monkeypatch,
):
    original, _chat, _embedding = _runtime()
    await readiness.run_inference_self_test(original)
    changed, _changed_chat, _changed_embedding = _runtime(
        chat_model="different-chat-model"
    )
    holder_snapshot = core_runtime._holder.snapshot_for_tests()
    core_runtime._holder.restore_for_tests((changed, False))
    monkeypatch.setattr(core_runtime, "_probe_role_safely", _healthy_probe)
    try:
        health = await core_runtime.build_llmaas_health_block(authenticated=True)
    finally:
        core_runtime._holder.restore_for_tests(holder_snapshot)

    assert health["chat"]["readiness"] == "unknown"
    assert health["embedding"]["readiness"] == "unknown"


@pytest.mark.asyncio
async def test_manage_gate_precedes_runtime_and_hidden_contract(monkeypatch):
    mcp = FastMCP("self-test-tool")
    assert system_tools.register(mcp) == 4
    handler = _system_handler(mcp, "inference_self_test")
    run = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(core_runtime, "run_inference_self_test", run)

    denied_token = current_token_info.set(
        {"client_name": "reader", "permissions": ["read"]}
    )
    try:
        denied = await handler()
    finally:
        current_token_info.reset(denied_token)
    assert denied["status"] == "error"
    assert "manage" in denied["message"]
    run.assert_not_awaited()

    manage_token = current_token_info.set(
        {"client_name": "manager", "permissions": ["manage"]}
    )
    try:
        assert await handler() == {"status": "ok"}
    finally:
        current_token_info.reset(manage_token)
    run.assert_awaited_once_with()

    tool = mcp._tool_manager._tools["inference_self_test"]
    assert tool.parameters.get("properties", {}) == {}
    entry = next(
        entry
        for entry in TOOL_EXPOSURES
        if entry.canonical_name == "inference_self_test"
    )
    assert entry.audience is ToolAudience.OPERATOR
    assert entry.minimum_permission is ToolPermission.MANAGE
    assert entry.operation is ToolOperation.MUTATION
    for permission in ("read", "write", "manage", "admin"):
        assert "inference_self_test" not in discovery_names_for_permission(
            permission
        )
