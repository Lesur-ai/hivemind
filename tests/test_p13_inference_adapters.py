# -*- coding: utf-8 -*-
"""P13-1B (#275) — adapter conformance against the deterministic emulator.

Proves, with zero real network and zero credentials, that BOTH registered
adapters (generic ``openai-compatible`` and native ``anthropic``) honor the
same normalized request/result/probe/error/retry/proxy/redaction contracts:

- exact wire shapes (Cloud Temple regression shape for the generic adapter,
  native Messages API for Anthropic), including temperature omission;
- exactly ONE upstream request per attempt (no SDK is involved at all now, so
  no implicit retry can exist; the count is still asserted on the wire);
- the single bounded ADR-0027 retry: only an explicitly transient 429
  (``Retry-After`` <= 5s AND a machine-readable transient code) re-traverses
  the wire; quota exhaustion, ambiguous 429s, 5xx, post-send drops, and read
  timeouts never do;
- normalized error categories with secret-free envelopes and logs;
- embedding order/cardinality/dimension/finite validation;
- discovery probes: unsupported ``/models`` is reachable+unsupported, never
  a false provider failure, and probes never retry;
- proxy routing through an owned transport, the direct-connection trap, and
  ambient ``HTTP(S)_PROXY`` immunity (``trust_env=False``);
- cancellation propagates as control flow and never tears down the shared
  transport; ``aclose()`` is idempotent.

Profiles point at the loopback emulator and are constructed directly — the
documented test seam. Configuration-layer URL rules are locked separately in
``tests/test_p13_inference_config.py``. The bounded-retry-loop unit contract
itself (attempt counting, deadline wall, cancellation) is proven independently
of any adapter in ``tests/test_p13_inference_retry.py`` (P13-1A, #274) and is
not re-tested here.

Reuse note: adapted from the draft PR #273 slice (materially authored by
``claude-fable-5``, Anthropic) on branch ``claude/p13-1-implementation-425762``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time

import pytest

import hivemind_inference.adapters.common as common
import hivemind_inference.adapters.openai_compatible as openai_compatible
from hivemind_inference import InferenceError
from hivemind_inference.adapters.common import ResponseTooLarge, read_bounded_body
from hivemind_inference.profiles import ResolvedChatProfile, ResolvedEmbeddingProfile
from hivemind_inference.records import ChatMessage, ChatRequest, EmbeddingRequest
from hivemind_inference.registry import (
    build_chat_probe,
    build_chat_provider,
    build_embedding_probe,
    build_embedding_provider,
)
from tests.fakes.inference_emulator import (
    InferenceEmulator,
    anthropic_message_payload,
    openai_chat_payload,
    openai_embeddings_payload,
)

PLANTED_SECRET = "sk-planted-super-secret"
PLANTED_URL = "https://svc-user:hunter2@leak.example.com/v1?access_token=abc"


# Documented endpoints for the named hosted profiles. ResolvedChatProfile
# pins each brand identifier to its exact host/scheme/path at construction
# (ADR-0027), so a profile for a named provider must be built against the real
# endpoint and then retargeted at the loopback emulator — the same seam the
# anthropic helper uses. Only `endpoint` is overridden; every other invariant
# (role, adapter mapping, temperature range) is still validated.
_DOCUMENTED_ENDPOINTS = {
    "cloud-temple": "https://api.ai.cloud-temple.com/v1",
    "scaleway": "https://api.scaleway.ai/v1",
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ovhcloud": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
    "ollama": "http://localhost:11434/v1",
}


def openai_chat_profile(
    endpoint: str,
    *,
    temperature=None,
    provider="openai-compatible",
    context_window=8192,
    max_output_tokens=128,
):
    profile = ResolvedChatProfile(
        provider_id=provider,
        adapter_id="openai-compatible",
        endpoint=_DOCUMENTED_ENDPOINTS.get(provider, endpoint),
        api_key="emulated-key",
        configured_model="emulated-chat-model",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    if provider in _DOCUMENTED_ENDPOINTS:
        object.__setattr__(profile, "endpoint", endpoint)
    return profile


def anthropic_chat_profile(
    endpoint: str,
    *,
    temperature=None,
    context_window=8192,
    max_output_tokens=128,
):
    # ResolvedChatProfile pins provider_id="anthropic" to the documented
    # https://api.anthropic.com host/scheme/path at construction time
    # (ADR-0027 hosted-endpoint policy, enforced even for a directly built
    # profile — see profiles.py). Construct a genuinely valid profile against
    # that real host first, then retarget the frozen dataclass's `endpoint`
    # at the loopback emulator: the adapter has no other dependency on the
    # endpoint being the real host, and every other profile invariant (role,
    # adapter mapping, temperature range) is still validated above.
    profile = ResolvedChatProfile(
        provider_id="anthropic",
        adapter_id="anthropic",
        endpoint="https://api.anthropic.com",
        api_key="emulated-anthropic-key",
        configured_model="emulated-anthropic-model",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    object.__setattr__(profile, "endpoint", endpoint)
    return profile


def embedding_profile(endpoint: str, *, dimensions=4, provider="openai-compatible"):
    profile = ResolvedEmbeddingProfile(
        provider_id=provider,
        adapter_id="openai-compatible",
        endpoint=_DOCUMENTED_ENDPOINTS.get(provider, endpoint),
        api_key="emulated-key",
        configured_model="emulated-embedding-model",
        expected_dimensions=dimensions,
    )
    if provider in _DOCUMENTED_ENDPOINTS:
        object.__setattr__(profile, "endpoint", endpoint)
    return profile


def chat_request(*, timeout=5.0, max_output_tokens=None, retry_policy="bounded"):
    return ChatRequest(
        messages=(
            ChatMessage(role="system", content="You are terse."),
            ChatMessage(role="user", content="Say hi."),
        ),
        timeout_seconds=timeout,
        max_output_tokens=max_output_tokens,
        retry_policy=retry_policy,
    )


async def complete_with(profile, request, *, proxy_url=None):
    provider = build_chat_provider(profile, proxy_url=proxy_url)
    try:
        return await provider.complete(request)
    finally:
        await provider.aclose()


async def embed_with(profile, request, *, proxy_url=None):
    provider = build_embedding_provider(profile, proxy_url=proxy_url)
    try:
        return await provider.embed(request)
    finally:
        await provider.aclose()


def refused_port_url() -> str:
    """A loopback URL whose port is bound but NOT listening -> ECONNREFUSED."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- #
# Generic OpenAI-compatible chat                                              #
# --------------------------------------------------------------------------- #


class TestOpenAICompatibleChat:
    async def test_success_normalization_and_wire_shape(self):
        normalized_request = chat_request()
        async with InferenceEmulator() as emulator:
            profile = openai_chat_profile(emulator.v1_url, temperature=0.3)
            result = await complete_with(profile, normalized_request)
        assert result.text == "canned completion"
        assert result.finish_reason == "stop"
        assert result.configured_model == "emulated-chat-model"
        assert result.resolved_model == "emulated-chat-model"
        assert result.model_evidence == "provider_reported"
        assert (result.input_tokens, result.output_tokens, result.total_tokens) == (
            7,
            5,
            12,
        )
        assert result.correlation_id == normalized_request.correlation_id
        (request,) = emulator.requests
        assert request["method"] == "POST"
        assert request["url"].endswith("/v1/chat/completions")
        assert request["headers"]["authorization"] == "Bearer emulated-key"
        body = request["json"]
        assert body["model"] == "emulated-chat-model"
        assert body["max_tokens"] == 128
        assert body["temperature"] == 0.3
        assert body["messages"][0] == {"role": "system", "content": "You are terse."}

    async def test_omitted_temperature_never_reaches_the_wire(self):
        async with InferenceEmulator() as emulator:
            profile = openai_chat_profile(emulator.v1_url, temperature=None)
            await complete_with(profile, chat_request())
        assert "temperature" not in emulator.requests[0]["json"]

    async def test_request_may_only_lower_the_output_ceiling(self):
        async with InferenceEmulator() as emulator:
            profile = openai_chat_profile(emulator.v1_url)
            await complete_with(profile, chat_request(max_output_tokens=16))
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(profile, chat_request(max_output_tokens=129))
        assert emulator.requests[0]["json"]["max_tokens"] == 16
        assert len(emulator.requests) == 1  # the rejected request never sent
        assert excinfo.value.category == "invalid_request"

    async def test_empty_content_with_stop_is_a_successful_empty_result(self):
        script = [{"body": openai_chat_payload("", usage={})}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        assert result.text == ""
        assert result.finish_reason == "stop"
        assert result.input_tokens is None and result.total_tokens is None

    async def test_length_finish_reason_is_normalized(self):
        script = [{"body": openai_chat_payload("cut", finish_reason="length")}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        assert result.finish_reason == "length"

    async def test_unknown_finish_reason_maps_to_other(self):
        script = [{"body": openai_chat_payload("x", finish_reason="tool_calls")}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        assert result.finish_reason == "other"

    async def test_refusal_is_content_rejected_never_empty_success(self):
        script = [{"body": openai_chat_payload("", refusal="cannot comply")}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "content_rejected"
        assert excinfo.value.retryable is False

    async def test_content_filter_finish_is_content_rejected(self):
        script = [
            {"body": openai_chat_payload("partial", finish_reason="content_filter")}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "content_rejected"

    @pytest.mark.parametrize(
        "body",
        [
            {"id": "x", "object": "chat.completion", "choices": []},
            {"unexpected": True},
        ],
    )
    async def test_structurally_invalid_success_bodies_are_invalid_response(
        self, body
    ):
        async with InferenceEmulator([{"body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "invalid_response"

    async def test_non_json_success_body_is_invalid_response(self):
        async with InferenceEmulator([{"body_raw": b"<html>nope</html>"}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "invalid_response"

    async def test_truncated_json_body_is_invalid_response(self):
        async with InferenceEmulator(
            [{"body_raw": b'{"choices": [{"message": {"conten'}]
        ) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "invalid_response"

    @pytest.mark.parametrize(
        "script, expected_diagnostic",
        [
            pytest.param(
                [{"body_raw": b"<html>sk-planted-super-secret</html>"}],
                "invalid_json",
                id="invalid-json",
            ),
            pytest.param(
                [{"body": [PLANTED_SECRET]}],
                "invalid_root",
                id="invalid-root",
            ),
            pytest.param(
                [{"body_raw": b"null"}],
                "invalid_root",
                id="json-null-root",
            ),
            pytest.param(
                [{"body": {"choices": PLANTED_SECRET}}],
                "invalid_choices",
                id="invalid-choices",
            ),
            pytest.param(
                [{"body": {"choices": [{"message": PLANTED_SECRET}]}}],
                "invalid_message",
                id="invalid-message",
            ),
            pytest.param(
                [
                    {
                        "body": {
                            "choices": [
                                {
                                    "message": {
                                        "content": None,
                                        "reasoning": PLANTED_SECRET,
                                        "reasoning_content": PLANTED_SECRET,
                                        "tool_calls": [
                                            {"arguments": PLANTED_SECRET}
                                        ],
                                    }
                                }
                            ]
                        }
                    }
                ],
                "invalid_content",
                id="invalid-content",
            ),
        ],
    )
    async def test_invalid_success_logs_only_closed_structural_diagnostic(
        self, script, expected_diagnostic, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            async with InferenceEmulator(script) as emulator:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        openai_chat_profile(emulator.v1_url), chat_request()
                    )
        assert excinfo.value.category == "invalid_response"
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert f"diagnostic={expected_diagnostic}" in logs
        assert "status=200" in logs
        assert PLANTED_SECRET not in logs

    def test_unknown_provider_diagnostic_is_rendered_as_dash(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            common.log_provider_failure(
                role="chat",
                provider_id="cloud-temple",
                adapter_id="openai-compatible",
                category="invalid_response",
                correlation_id="corr-safe",
                diagnostic=PLANTED_SECRET,
            )
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "diagnostic=-" in logs
        assert PLANTED_SECRET not in logs

    def test_closed_diagnostic_is_chat_openai_compatible_only(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            common.log_provider_failure(
                role="embedding",
                provider_id="cloud-temple",
                adapter_id="openai-compatible",
                category="invalid_response",
                correlation_id="corr-role",
                diagnostic="invalid_content",
            )
            common.log_provider_failure(
                role="chat",
                provider_id="anthropic",
                adapter_id="anthropic",
                category="invalid_response",
                correlation_id="corr-adapter",
                diagnostic="invalid_content",
            )
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "diagnostic=" not in logs


class TestOpenAICompatibleErrorFamilies:
    @pytest.mark.parametrize("status, category", [(401, "auth"), (403, "auth")])
    async def test_authentication_failures(self, status, category):
        async with InferenceEmulator([{"status": status}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == category
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_server_error_is_unavailable_and_sends_exactly_one_request(self):
        # No SDK is involved, so no implicit retry can exist; the wire count is
        # still asserted so a future transport change cannot reintroduce one.
        async with InferenceEmulator([{"status": 500}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_ambiguous_429_is_rate_limited_without_retry(self):
        async with InferenceEmulator([{"status": 429}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_transient_429_retries_exactly_once_through_the_wire(self):
        # A retry requires a machine-readable transient code IN ADDITION to
        # the bounded Retry-After.
        body = {"error": {"message": "slow down", "type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        # Construct explicitly so the same request identity is proven across
        # the adapter's permitted second attempt and successful result.
        normalized_request = ChatRequest(
            messages=chat_request().messages,
            timeout_seconds=5.0,
            correlation_id="adapter-retry",
        )
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), normalized_request
            )
        assert result.text == "canned completion"
        assert result.correlation_id == "adapter-retry"
        assert len(emulator.requests) == 2

    async def test_explicit_zero_retry_policy_stops_transient_429_after_one_post(self):
        body = {
            "error": {
                "message": "slow down",
                "type": "rate_limit_exceeded",
                "code": "rate_limit_exceeded",
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url),
                    chat_request(retry_policy="none"),
                )
        assert excinfo.value.category == "rate_limited"
        assert len(emulator.requests) == 1

    async def test_transient_429_second_failure_surfaces_and_stops(self):
        body = {"error": {"message": "slow down", "type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert len(emulator.requests) == 2  # never a third attempt

    async def test_oversized_retry_after_does_not_authorize_a_retry(self):
        script = [{"status": 429, "headers": {"retry-after": "300"}}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_bodyless_429_with_retry_after_is_not_retried(self):
        # Without a machine-readable transient code, a valid Retry-After
        # authorizes NOTHING.
        script = [{"status": 429, "headers": {"retry-after": "0"}}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_unknown_429_code_with_retry_after_is_not_retried(self):
        body = {"error": {"message": "policy", "type": "policy_violation",
                          "code": "org_blocked"}}
        script = [{"status": 429, "headers": {"retry-after": "0"}, "body": body}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_quota_exhaustion_code_is_never_retried(self):
        body = {
            "error": {
                "message": "You exceeded your current quota",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        }
        script = [{"status": 429, "headers": {"retry-after": "0"}, "body": body}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "quota_exhausted"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_quota_exhaustion_prefix_beats_transient_type(self):
        # Quota classification is deliberately broader than the exact
        # transient allowlist: under-matching can authorize a duplicate paid
        # request, while over-matching only makes a retryable shape terminal.
        body = {
            "error": {
                "type": "rate_limit_exceeded",
                "code": "insufficient_quota_reached",
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "quota_exhausted"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_status_only_transient_429_never_retries(self):
        # `status` is not a documented retry-authorizing source. Only closed
        # code/type fields or Google's structured details[*].reason may carry
        # an allowlisted transient reason.
        body = {"error": {"status": "rate_limited"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"error": {"code": "Rate_Limit_Exceeded"}},
            {"error": {"errors": [{"reason": "rate_limited"}]}},
            {
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "RATE_LIMIT_EXCEEDED",
                        }
                    ]
                }
            },
        ],
    )
    async def test_generic_endpoint_never_inherits_gemini_retry_evidence(self, body):
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_gemini_exact_quota_reason_is_never_retried(self):
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "QUOTA_EXCEEDED",
                    }
                ],
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url, provider="gemini"),
                    chat_request(),
                )
        assert excinfo.value.category == "quota_exhausted"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_gemini_transient_reason_retries_exactly_once(self):
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "RATE_LIMIT_EXCEEDED",
                    }
                ],
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(
                    emulator.v1_url,
                    provider="gemini",
                ),
                chat_request(),
            )
        assert result.text == "canned completion"
        assert len(emulator.requests) == 2

    @pytest.mark.parametrize(
        "retry_after",
        ["6", "Wed, 21 Oct 2015 07:28:00 GMT"],
    )
    async def test_gemini_transient_reason_rejects_unbounded_retry_after(
        self, retry_after
    ):
        body = {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "RATE_LIMIT_EXCEEDED",
                    }
                ]
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": retry_after}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url, provider="gemini"),
                    chat_request(),
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_gemini_daily_quota_reason_wins_and_is_never_retried(
        self, caplog
    ):
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "RATE_LIMIT_EXCEEDED",
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            }
                        ],
                    }
                ],
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        with caplog.at_level(logging.DEBUG):
            async with InferenceEmulator(script) as emulator:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        openai_chat_profile(emulator.v1_url, provider="gemini"),
                        chat_request(),
                    )
        assert excinfo.value.category == "quota_exhausted"
        assert excinfo.value.retryable is False
        planted_quota_id = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        assert planted_quota_id not in str(excinfo.value)
        assert planted_quota_id not in "\n".join(
            record.getMessage() for record in caplog.records
        )
        assert len(emulator.requests) == 1

    async def test_gemini_ambiguous_resource_exhausted_never_retries(self):
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "sk-planted-provider-message",
            }
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url, provider="gemini"),
                    chat_request(),
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert "sk-planted-provider-message" not in str(excinfo.value)
        assert len(emulator.requests) == 1

    async def test_post_send_connection_drop_is_never_retried(self):
        async with InferenceEmulator([{"action": "drop"}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_read_timeout_is_never_retried(self):
        async with InferenceEmulator([{"action": "stall"}]) as emulator:
            started = time.monotonic()
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(emulator.v1_url),
                    chat_request(timeout=0.4),
                )
        assert excinfo.value.category == "timeout"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1
        assert time.monotonic() - started < 5

    async def test_connection_refused_is_pre_send_retryable_unavailable(self):
        with pytest.raises(InferenceError) as excinfo:
            await complete_with(
                openai_chat_profile(refused_port_url() + "/v1"),
                chat_request(timeout=2.0),
            )
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is True  # pre-send: the one safe retry ran

    async def test_cancellation_propagates_and_keeps_the_transport_alive(self):
        async with InferenceEmulator([{"action": "stall"}, None]) as emulator:
            profile = openai_chat_profile(emulator.v1_url)
            provider = build_chat_provider(profile)
            try:
                task = asyncio.create_task(provider.complete(chat_request()))
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if emulator.requests:
                        break
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                # The shared transport must survive an in-flight cancellation.
                result = await provider.complete(chat_request())
                assert result.text == "canned completion"
            finally:
                await provider.aclose()
                await provider.aclose()  # idempotent

    async def test_error_envelope_and_logs_never_leak_planted_secrets(self, caplog):
        body = {
            "error": {
                "message": f"denied for {PLANTED_URL} with key {PLANTED_SECRET}",
                "type": "invalid_request_error",
            }
        }
        with caplog.at_level(logging.DEBUG):
            async with InferenceEmulator([{"status": 400, "body": body}]) as emulator:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        openai_chat_profile(emulator.v1_url), chat_request()
                    )
        rendered = str(excinfo.value) + repr(excinfo.value.safe_payload())
        logs = "\n".join(record.getMessage() for record in caplog.records)
        for leak in (PLANTED_SECRET, "hunter2", "access_token", "leak.example.com"):
            assert leak not in rendered
            assert leak not in logs
        assert excinfo.value.category == "invalid_request"
        assert excinfo.value.correlation_id


# --------------------------------------------------------------------------- #
# Generic OpenAI-compatible embeddings                                        #
# --------------------------------------------------------------------------- #


class TestOpenAICompatibleEmbeddings:
    async def test_success_preserves_order_and_validates_dimensions(self):
        normalized_request = EmbeddingRequest(
            inputs=("alpha", "beta", "gamma"), timeout_seconds=5.0
        )
        async with InferenceEmulator() as emulator:
            profile = embedding_profile(emulator.v1_url)
            result = await embed_with(profile, normalized_request)
        assert len(result.vectors) == 3
        assert [vector[0] for vector in result.vectors] == [0.25, 1.25, 2.25]
        assert result.effective_dimensions == 4
        assert result.resolved_model == "emulated-embedding-model"
        assert result.model_evidence == "provider_reported"
        assert result.correlation_id == normalized_request.correlation_id
        body = emulator.requests[0]["json"]
        assert body["model"] == "emulated-embedding-model"
        assert body["input"] == ["alpha", "beta", "gamma"]
        assert "dimensions" not in body  # validation-only, never a wire field

    async def test_wire_body_is_exactly_model_and_input(self):
        # The request is issued over the owned transport precisely so that NO
        # encoding_format is sent: the SDK would inject "base64", and a
        # provider documenting the field as unsupported (Scaleway) must not
        # receive it at all. expected_dimensions is validation metadata, so no
        # `dimensions` field is sent either (ADR-0027).
        async with InferenceEmulator() as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
        body = emulator.requests[0]["json"]
        assert set(body) == {"model", "input"}
        assert "encoding_format" not in body
        assert "dimensions" not in body
        assert emulator.requests[0]["headers"]["authorization"] == "Bearer emulated-key"
        assert emulator.requests[0]["url"].endswith("/v1/embeddings")
        assert result.vectors == ((0.25, 0.25, 0.25, 0.25),)

    async def test_base64_vectors_are_decoded_and_validated(self):
        # A provider may answer base64 with or without being asked. Decode it
        # rather than fail closed — the decoded values then face the same
        # dimension and finiteness checks as an array would.
        payload = openai_embeddings_payload(2, encoding_format="base64")
        async with InferenceEmulator([{"body": payload}]) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                EmbeddingRequest(inputs=("a", "b"), timeout_seconds=5.0),
            )
        assert [round(v[0], 6) for v in result.vectors] == [0.25, 1.25]
        assert all(len(v) == 4 for v in result.vectors)

    async def test_base64_is_decoded_little_endian_on_every_host(self):
        # FIXED bytes, deliberately NOT produced by the emulator helper: if the
        # test generated them with the same primitive the decoder uses, both
        # would byte-swap in lockstep on a big-endian host and the test could
        # never fail on the portability bug it exists to catch.
        # 1.0, 2.0, 3.0, 4.0 as little-endian float32:
        le_bytes = (
            b"\x00\x00\x80\x3f"
            b"\x00\x00\x00\x40"
            b"\x00\x00\x40\x40"
            b"\x00\x00\x80\x40"
        )
        payload = {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": base64.b64encode(le_bytes).decode("ascii"),
                }
            ],
            "model": "m",
        }
        async with InferenceEmulator([{"body": payload}]) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
        assert result.vectors == ((1.0, 2.0, 3.0, 4.0),)

    async def test_base64_wrong_dimension_still_rejected(self):
        # Decoding must not become a bypass: a base64 buffer of the wrong
        # length fails the same dimension check an array would.
        payload = openai_embeddings_payload(1, dimensions=8, encoding_format="base64")
        async with InferenceEmulator([{"body": payload}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    @pytest.mark.parametrize(
        "bad_b64",
        [
            "not-valid-base64!!",   # not decodable at all
            "AAAA",                  # decodes to 3 bytes: not a whole float32
        ],
    )
    async def test_malformed_base64_is_invalid_response(self, bad_b64):
        payload = openai_embeddings_payload(1)
        payload["data"][0]["embedding"] = bad_b64
        async with InferenceEmulator([{"body": payload}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_out_of_order_indexes_are_restored_to_input_order(self):
        payload = openai_embeddings_payload(3)
        payload["data"] = [payload["data"][2], payload["data"][0], payload["data"][1]]
        async with InferenceEmulator([{"body": payload}]) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                EmbeddingRequest(inputs=("a", "b", "c"), timeout_seconds=5.0),
            )
        assert [vector[0] for vector in result.vectors] == [0.25, 1.25, 2.25]

    async def test_cardinality_mismatch_is_invalid_response(self):
        async with InferenceEmulator(
            [{"body": openai_embeddings_payload(2)}]
        ) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a", "b", "c"), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_dimension_mismatch_is_invalid_response(self):
        async with InferenceEmulator(
            [{"body": openai_embeddings_payload(2, dimensions=8)}]
        ) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a", "b"), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    @pytest.mark.parametrize(
        "bad", [float("nan"), float("inf"), None, "0.1", True, False]
    )
    async def test_non_finite_or_non_numeric_components_are_invalid_response(
        self, bad
    ):
        # True/False must be validated from the RAW response bytes: the
        # openai SDK's CreateEmbeddingResponse types `embedding` as
        # List[float], so Pydantic silently coerces a JSON boolean into
        # 1.0/0.0 before a component-by-component isinstance(x, bool) check
        # over the PARSED model would ever see it.
        payload = openai_embeddings_payload(1)
        payload["data"][0]["embedding"][2] = bad
        raw = json.dumps(payload).encode()  # emits NaN/Infinity literals
        async with InferenceEmulator([{"body_raw": raw}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_oversized_integer_component_is_invalid_response(self):
        # float(10**400) raises OverflowError; a provider-controlled numeric
        # must never escape the frozen envelope as a raw internal exception.
        # The package's TOTAL _is_finite_number primitive absorbs it.
        raw = (
            b'{"object":"list","data":[{"object":"embedding","index":0,'
            b'"embedding":[0.1,0.2,0.3,' + b"1" + b"0" * 400 + b"]}],"
            b'"model":"m"}'
        )
        async with InferenceEmulator([{"body_raw": raw}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_deeply_nested_body_is_invalid_response_not_recursion_error(self):
        # Deep enough nesting exhausts the JSON parser's recursion budget;
        # that is a malformed provider response, not an internal failure.
        depth = 100_000
        raw = (
            b'{"object":"list","data":'
            + b"[" * depth
            + b"]" * depth
            + b',"model":"m"}'
        )
        async with InferenceEmulator([{"body_raw": raw}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(emulator.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_scaleway_strict_provider_accepts_our_request(self):
        # Regression guard for R3-F1. A provider that REJECTS an unsupported
        # `encoding_format` (Scaleway documents it as unsupported) must still
        # be able to embed. Re-introducing any universal pin turns this RED.
        async with InferenceEmulator(
            rejected_fields=("encoding_format", "dimensions")
        ) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
        assert len(result.vectors) == 1
        assert "encoding_format" not in emulator.requests[0]["json"]

    async def test_embedding_transient_429_retries_once(self):
        body = {"error": {"message": "slow down", "type": "rate_limit_exceeded",
                          "code": "rate_limit_exceeded"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        normalized_request = EmbeddingRequest(
            inputs=("a",),
            timeout_seconds=5.0,
            correlation_id="embedding-retry",
        )
        async with InferenceEmulator(script) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url),
                normalized_request,
            )
        assert len(result.vectors) == 1
        assert result.correlation_id == "embedding-retry"
        assert len(emulator.requests) == 2

    async def test_query_and_document_requests_share_the_symmetric_wire(self):
        async with InferenceEmulator() as emulator:
            profile = embedding_profile(emulator.v1_url)
            await embed_with(
                profile,
                EmbeddingRequest(
                    inputs=("q",), timeout_seconds=5.0, input_type="query"
                ),
            )
            await embed_with(
                profile,
                EmbeddingRequest(
                    inputs=("d",), timeout_seconds=5.0, input_type="document"
                ),
            )
        first, second = emulator.requests
        assert set(first["json"]) == set(second["json"])  # no extra wire field


class TestEmbeddingIndexPermutation:
    """Embedding responses must carry the exact integer permutation 0..N-1 —
    provider order is never trusted."""

    def _payload(self, entries):
        return {
            "object": "list",
            "data": entries,
            "model": "bge-m3:567m",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

    async def _embed(self, emulator):
        return await embed_with(
            embedding_profile(emulator.v1_url),
            EmbeddingRequest(inputs=("a", "b"), timeout_seconds=5.0),
        )

    async def test_missing_indexes_are_invalid(self):
        # Vectors match the profile's expected_dimensions=4 exactly, so a
        # dimension-mismatch failure cannot mask whether the missing-index
        # check itself is what rejects this payload.
        body = self._payload([
            {"object": "embedding", "embedding": [0.1] * 4},
            {"object": "embedding", "embedding": [0.2] * 4},
        ])
        async with InferenceEmulator([{"status": 200, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await self._embed(emulator)
        assert excinfo.value.category == "invalid_response"

    async def test_partial_indexes_are_invalid(self):
        body = self._payload([
            {"object": "embedding", "index": 0, "embedding": [0.1] * 4},
            {"object": "embedding", "embedding": [0.2] * 4},
        ])
        async with InferenceEmulator([{"status": 200, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await self._embed(emulator)
        assert excinfo.value.category == "invalid_response"

    async def test_duplicate_indexes_are_invalid(self):
        body = self._payload([
            {"object": "embedding", "index": 0, "embedding": [0.1] * 4},
            {"object": "embedding", "index": 0, "embedding": [0.2] * 4},
        ])
        async with InferenceEmulator([{"status": 200, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await self._embed(emulator)
        assert excinfo.value.category == "invalid_response"

    async def test_boolean_indexes_are_invalid(self):
        body = self._payload([
            {"object": "embedding", "index": False, "embedding": [0.1] * 4},
            {"object": "embedding", "index": True, "embedding": [0.2] * 4},
        ])
        async with InferenceEmulator([{"status": 200, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await self._embed(emulator)
        assert excinfo.value.category == "invalid_response"


# --------------------------------------------------------------------------- #
# Native Anthropic chat                                                       #
# --------------------------------------------------------------------------- #


class TestAnthropicChat:
    async def test_success_wire_shape_and_normalization(self):
        normalized_request = chat_request()
        async with InferenceEmulator() as emulator:
            profile = anthropic_chat_profile(emulator.url, temperature=0.5)
            result = await complete_with(profile, normalized_request)
        assert result.text == "canned anthropic completion"
        assert result.finish_reason == "stop"
        assert result.resolved_model == "emulated-anthropic-model"
        assert result.model_evidence == "provider_reported"
        assert (result.input_tokens, result.output_tokens, result.total_tokens) == (
            9,
            4,
            13,
        )
        assert result.correlation_id == normalized_request.correlation_id
        (request,) = emulator.requests
        assert request["method"] == "POST"
        assert request["url"].endswith("/v1/messages")
        assert request["headers"]["x-api-key"] == "emulated-anthropic-key"
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request["headers"]
        body = request["json"]
        assert body["model"] == "emulated-anthropic-model"
        assert body["max_tokens"] == 128
        assert body["system"] == "You are terse."
        assert body["temperature"] == 0.5
        assert body["messages"] == [{"role": "user", "content": "Say hi."}]

    async def test_omitted_temperature_never_reaches_the_wire(self):
        async with InferenceEmulator() as emulator:
            await complete_with(
                anthropic_chat_profile(emulator.url), chat_request()
            )
        assert "temperature" not in emulator.requests[0]["json"]

    async def test_mid_conversation_system_message_is_invalid_request(self):
        request = ChatRequest(
            messages=(
                ChatMessage(role="user", content="hi"),
                ChatMessage(role="system", content="late system"),
            ),
            timeout_seconds=5.0,
        )
        async with InferenceEmulator() as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(anthropic_chat_profile(emulator.url), request)
        assert excinfo.value.category == "invalid_request"
        assert emulator.requests == []  # rejected before any wire traffic

    async def test_system_only_conversation_is_invalid_request(self):
        request = ChatRequest(
            messages=(ChatMessage(role="system", content="only system"),),
            timeout_seconds=5.0,
        )
        async with InferenceEmulator() as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(anthropic_chat_profile(emulator.url), request)
        assert excinfo.value.category == "invalid_request"
        assert emulator.requests == []

    async def test_max_tokens_finish_maps_to_length(self):
        script = [{"body": anthropic_message_payload("cut", stop_reason="max_tokens")}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                anthropic_chat_profile(emulator.url), chat_request()
            )
        assert result.finish_reason == "length"

    async def test_refusal_stop_reason_is_content_rejected(self):
        script = [{"body": anthropic_message_payload("", stop_reason="refusal")}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "content_rejected"

    @pytest.mark.parametrize(
        "status, error_type, category",
        [
            (401, "authentication_error", "auth"),
            (403, "permission_error", "auth"),
            (400, "invalid_request_error", "invalid_request"),
            (404, "not_found_error", "invalid_request"),
            (500, "api_error", "unavailable"),
            (529, "overloaded_error", "unavailable"),
        ],
    )
    async def test_native_error_types_map_to_frozen_categories(
        self, status, error_type, category
    ):
        body = {"type": "error", "error": {"type": error_type, "message": "no"}}
        async with InferenceEmulator([{"status": status, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == category
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_transient_429_retries_exactly_once(self):
        body = {"type": "error", "error": {"type": "rate_limit_error", "message": "no"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                anthropic_chat_profile(emulator.url), chat_request()
            )
        assert result.text == "canned anthropic completion"
        assert len(emulator.requests) == 2

    async def test_explicit_zero_retry_policy_stops_transient_429_after_one_post(self):
        body = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "no"},
        }
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body},
            None,
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url),
                    chat_request(retry_policy="none"),
                )
        assert excinfo.value.category == "rate_limited"
        assert len(emulator.requests) == 1

    async def test_ambiguous_429_is_not_retried(self):
        body = {"type": "error", "error": {"type": "rate_limit_error", "message": "no"}}
        async with InferenceEmulator([{"status": 429, "body": body}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert len(emulator.requests) == 1

    async def test_non_json_and_malformed_bodies_are_invalid_response(self):
        async with InferenceEmulator([{"body_raw": b"not json"}]) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "invalid_response"

    async def test_secretless_envelope_for_planted_error_body(self, caplog):
        body = {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": f"denied for {PLANTED_URL} with key {PLANTED_SECRET}",
            },
        }
        with caplog.at_level(logging.DEBUG):
            async with InferenceEmulator([{"status": 400, "body": body}]) as emulator:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        anthropic_chat_profile(emulator.url), chat_request()
                    )
        rendered = str(excinfo.value)
        logs = "\n".join(record.getMessage() for record in caplog.records)
        for leak in (PLANTED_SECRET, "hunter2", "access_token", "leak.example.com"):
            assert leak not in rendered
            assert leak not in logs


class TestAnthropicAmbiguous429:
    """The Anthropic adapter retries a 429 only with the machine-readable
    transient code AND a bounded Retry-After."""

    async def test_bodyless_429_with_retry_after_is_not_retried(self):
        script = [{"status": 429, "headers": {"retry-after": "0"}}]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "rate_limited"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1

    async def test_unknown_429_type_with_retry_after_is_not_retried(self):
        body = {"type": "error",
                "error": {"type": "policy_violation", "message": "no"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1


class TestBoundedResponseReads:
    """R4-F1: a provider response must be capped while it is being READ.
    httpx's ordinary post()/get() eagerly buffer the complete body before any
    validation runs, so an oversized — or small-but-highly-compressible —
    response could exhaust process memory before a single cardinality or
    dimension check executed, defeating the safe envelope through resource
    exhaustion rather than through any payload the validators inspect."""

    async def test_reader_aborts_an_unbounded_stream(self):
        # The decisive property: an INFINITE stream terminates. A cap applied
        # after buffering could not do this.
        class Endless:
            headers: dict = {}

            def __init__(self):
                self.chunks = 0

            async def aiter_raw(self):
                while True:
                    self.chunks += 1
                    yield b"x" * (1024 * 1024)

        endless = Endless()
        with pytest.raises(ResponseTooLarge):
            await read_bounded_body(endless, max_bytes=4 * 1024 * 1024)
        assert endless.chunks <= 6  # stopped promptly, did not run away

    @pytest.mark.parametrize("size, cap, fits", [(100, 100, True), (101, 100, False)])
    async def test_reader_boundary_is_exact(self, size, cap, fits):
        class Fixed:
            headers: dict = {}

            async def aiter_raw(self):
                yield b"y" * size

        if fits:
            assert len(await read_bounded_body(Fixed(), max_bytes=cap)) == size
        else:
            with pytest.raises(ResponseTooLarge):
                await read_bounded_body(Fixed(), max_bytes=cap)

    async def test_oversized_embedding_response_is_invalid_response(self):
        # Uses the REAL request-aware ceiling — no monkeypatched constant. For
        # 1 input x 4 dimensions the permitted size is a few hundred KB, so a
        # ~1 MB body is refused. This is the R5-F2 property: a fixed 32 MiB
        # ceiling would have let the originally-reported 12 MB amplification
        # through untouched, because 12 MB sits below it.
        #
        # The body must be OTHERWISE VALID so this isolates size: a payload
        # malformed for another reason would fail with or without the ceiling
        # and prove nothing. JSON whitespace keeps the document identical.
        valid = json.dumps(openai_embeddings_payload(1)).encode()
        padded = valid[:-1] + b" " * 1_000_000 + b"}"
        assert json.loads(padded) == json.loads(valid)  # still the same document
        assert len(padded) < 12 * 1024 * 1024  # and well under the old fixed cap
        async with InferenceEmulator([{"body_raw": padded}]) as em:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(em.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=10.0),
                )
        assert excinfo.value.category == "invalid_response"
        assert excinfo.value.retryable is False

    async def test_request_aware_ceiling_scales_with_the_request(self):
        # The ceiling tracks what was requested rather than being one blunt
        # constant, across the whole serviceable range (both ends stay under
        # the absolute clamp, so this measures scaling and not the clamp).
        small = common.embedding_response_ceiling(1, 4)
        large = common.embedding_response_ceiling(common.MAX_EMBEDDING_INPUTS, 4096)
        assert small < large <= common.ABSOLUTE_MAX_RESPONSE_BYTES
        # A legitimate large batch still succeeds end to end.
        async with InferenceEmulator(embedding_dimensions=1024) as em:
            result = await embed_with(
                embedding_profile(em.v1_url, dimensions=1024),
                EmbeddingRequest(
                    inputs=tuple(f"input-{i}" for i in range(8)), timeout_seconds=10.0
                ),
            )
        assert len(result.vectors) == 8
        assert all(len(v) == 1024 for v in result.vectors)

    async def test_oversized_anthropic_chat_response_is_invalid_response(self):
        # Real request-aware ceiling (max_output_tokens=128 on the test
        # profile), no monkeypatched constant.
        assert common.chat_response_ceiling(128) < 1_000_000
        big = (
            b'{"type":"message","content":[{"type":"text","text":"'
            + b"A" * 1_000_000
            + b'"}]}'
        )
        async with InferenceEmulator([{"body_raw": big}]) as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(em.url), chat_request(timeout=10.0)
                )
        assert excinfo.value.category == "invalid_response"
        assert excinfo.value.retryable is False

    async def test_oversized_anthropic_probe_response_is_a_safe_probe_result(
        self, monkeypatch
    ):
        monkeypatch.setattr(common, "MAX_RESPONSE_BYTES", 4096)
        big = b'{"data":[{"id":"' + b"A" * 200_000 + b'"}]}'
        async with InferenceEmulator([{"body_raw": big}]) as em:
            probe = build_chat_probe(anthropic_chat_profile(em.url))
            try:
                result = await probe.probe(timeout_seconds=10.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "error"
        assert result.error_category == "invalid_response"


class TestAbsoluteResponseBound:
    """R6-F1: deriving the ceiling from the request removed the ABSOLUTE bound
    a fixed constant used to provide. Public ceiling helpers still accept
    caller-provided values independently of profile validation, and embedding
    shapes can derive enormous values. A request-derived limit is only safe
    with a hard process ceiling above it."""

    # Deliberately bypass profile validation to prove the enforcement primitive
    # stays safe for an extreme direct caller.
    EXTREME = 10**18

    @pytest.mark.parametrize(
        "derived_ceiling",
        [
            lambda: common.chat_response_ceiling(TestAbsoluteResponseBound.EXTREME),
            lambda: common.embedding_response_ceiling(1, TestAbsoluteResponseBound.EXTREME),
            lambda: common.embedding_response_ceiling(10**6, 4096),
        ],
    )
    def test_no_derived_ceiling_exceeds_the_absolute_bound(self, derived_ceiling):
        assert derived_ceiling() <= common.ABSOLUTE_MAX_RESPONSE_BYTES

    async def test_reader_clamps_even_an_absurd_explicit_limit(self):
        # Defence in depth at the ENFORCEMENT point: a caller passing a
        # ridiculous ceiling must still not be able to retain unbounded bytes.
        class Endless:
            headers: dict = {}

            def __init__(self):
                self.chunks = 0

            async def aiter_raw(self):
                while True:
                    self.chunks += 1
                    yield b"x" * (1024 * 1024)

        endless = Endless()
        with pytest.raises(ResponseTooLarge):
            await read_bounded_body(endless, max_bytes=10**20)
        # Bounded by the absolute ceiling, not by the caller's absurd number.
        assert endless.chunks <= common.ABSOLUTE_MAX_RESPONSE_BYTES // (1024 * 1024) + 2

    async def test_unserviceable_embedding_request_is_refused_pre_send(self):
        # No provider request may be issued at all: the caller gets an
        # actionable invalid_request rather than a mid-read invalid_response.
        async with InferenceEmulator() as em:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(em.v1_url, dimensions=self.EXTREME),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
            assert em.requests == []  # nothing was sent
        assert excinfo.value.category == "invalid_request"

    async def test_over_contract_batch_is_refused_pre_send(self):
        # One input beyond the DECLARED contract is refused, with no request
        # sent. Splitting is the consumer's job (#276), which knows the corpus.
        huge_batch = tuple(f"input-{i}" for i in range(common.MAX_EMBEDDING_INPUTS + 1))
        async with InferenceEmulator() as em:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(em.v1_url, dimensions=1536),
                    EmbeddingRequest(inputs=huge_batch, timeout_seconds=5.0),
                )
            assert em.requests == []
        assert excinfo.value.category == "invalid_request"

    async def test_declared_batch_contract_covers_every_reference_profile(self):
        # The contract must serve every frozen reference profile at its full
        # declared batch, and the hosted chat ceiling — a bound that refuses
        # supported work is its own availability failure.
        for dimensions in (1024, 1536, 4096):  # cloud-temple, openai, scaleway
            assert common.embedding_response_is_serviceable(
                common.MAX_EMBEDDING_INPUTS, dimensions
            ), dimensions
        assert common.chat_response_is_serviceable(16384)  # hosted ceiling
        async with InferenceEmulator(embedding_dimensions=1024) as em:
            result = await embed_with(
                embedding_profile(em.v1_url, dimensions=1024),
                EmbeddingRequest(
                    inputs=tuple(f"i{n}" for n in range(16)), timeout_seconds=10.0
                ),
            )
        assert len(result.vectors) == 16

    async def test_batch_exactly_at_the_contract_limit_is_served(self):
        # The boundary itself must work, not just values comfortably inside.
        n = common.MAX_EMBEDDING_INPUTS
        async with InferenceEmulator(embedding_dimensions=4) as em:
            result = await embed_with(
                embedding_profile(em.v1_url, dimensions=4),
                EmbeddingRequest(
                    inputs=tuple(f"i{i}" for i in range(n)), timeout_seconds=10.0
                ),
            )
        assert len(result.vectors) == n


class TestChatOutputBudgetContract:
    """A generation budget and a serialized response size are distinct.

    Reasoning tokens consume ``max_output_tokens`` without necessarily being
    returned in the provider JSON, so the byte estimate must not recreate an
    accidental output-token ceiling. The streamed body remains independently
    bounded by ``ABSOLUTE_MAX_RESPONSE_BYTES``.
    """

    def test_exact_million_token_contract_is_serviceable(self):
        assert common.MAX_CHAT_GENERATION_TOKENS == 1_000_000
        assert common.chat_response_is_serviceable(
            common.MAX_CHAT_GENERATION_TOKENS
        )
        assert not common.chat_response_is_serviceable(
            common.MAX_CHAT_GENERATION_TOKENS + 1
        )
        assert (
            common.chat_response_ceiling(common.MAX_CHAT_GENERATION_TOKENS)
            == common.ABSOLUTE_MAX_RESPONSE_BYTES
        )

    async def test_openai_compatible_sends_exact_million_token_budget(self):
        async with InferenceEmulator() as em:
            await complete_with(
                openai_chat_profile(
                    em.v1_url,
                    context_window=1_000_001,
                    max_output_tokens=1_000_000,
                ),
                chat_request(),
            )
        assert len(em.requests) == 1
        assert em.requests[0]["json"]["max_tokens"] == 1_000_000

    async def test_anthropic_sends_exact_million_token_budget(self):
        async with InferenceEmulator() as em:
            await complete_with(
                anthropic_chat_profile(
                    em.url,
                    context_window=1_000_001,
                    max_output_tokens=1_000_000,
                ),
                chat_request(),
            )
        assert len(em.requests) == 1
        assert em.requests[0]["json"]["max_tokens"] == 1_000_000

    async def test_over_contract_budget_is_refused_before_egress(self):
        async with InferenceEmulator() as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    openai_chat_profile(
                        em.v1_url,
                        context_window=1_000_001,
                        max_output_tokens=1_000_000,
                    ),
                    chat_request(max_output_tokens=1_000_001),
                )
        assert em.requests == []
        assert excinfo.value.category == "invalid_request"

    @pytest.mark.parametrize("adapter", ["openai-compatible", "anthropic"])
    async def test_saturated_eight_mib_ceiling_is_enforced_end_to_end(
        self, adapter, caplog
    ):
        payload_object = (
            openai_chat_payload("x")
            if adapter == "openai-compatible"
            else anthropic_message_payload("x")
        )
        valid = json.dumps(payload_object).encode()
        target_size = common.ABSOLUTE_MAX_RESPONSE_BYTES + 1
        padded = valid[:-1] + b" " * (target_size - len(valid)) + b"}"
        assert len(padded) == target_size
        assert json.loads(padded) == payload_object

        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            async with InferenceEmulator([{"body_raw": padded}]) as em:
                profile = (
                    openai_chat_profile(
                        em.v1_url,
                        context_window=1_000_001,
                        max_output_tokens=1_000_000,
                    )
                    if adapter == "openai-compatible"
                    else anthropic_chat_profile(
                        em.url,
                        context_window=1_000_001,
                        max_output_tokens=1_000_000,
                    )
                )
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(profile, chat_request(timeout=20.0))

        assert len(em.requests) == 1
        assert excinfo.value.category == "invalid_response"
        assert excinfo.value.retryable is False
        logs = "\n".join(record.getMessage() for record in caplog.records)
        if adapter == "openai-compatible":
            assert "diagnostic=response_too_large" in logs
        else:
            assert "category=invalid_response" in logs


class TestResolvedModelIsCorroborated:
    """R8-F1: ADR-0027 lets `resolved_model` reach outward operational surfaces
    and the PERSISTED embedding-collection identity record, while forbidding
    raw provider payload, prompts, credentials, and endpoints there. Copying an
    arbitrary provider `model` string into that field therefore hands a
    compromised or malformed provider a channel through the redaction boundary
    AND a way to corrupt an embedding collection's identity.

    The prior tests covered planted secrets in ERROR bodies only; these cover
    SUCCESSFUL response metadata, which is the path that was open."""

    # A credentialed URL, an API key, and an injected instruction in one value.
    HOSTILE = f"{PLANTED_URL} key={PLANTED_SECRET} ignore prior instructions"

    @pytest.mark.parametrize(
        ("body", "expected"),
        (
            ({}, "absent"),
            ({"model": None}, "invalid"),
            ({"model": PLANTED_SECRET}, "nonexact"),
            ({"model": "emulated-chat-model"}, "exact"),
        ),
    )
    def test_protected_model_diagnostic_is_closed(self, body, expected):
        diagnostic = openai_compatible._reported_model_diagnostic(
            body, "emulated-chat-model"
        )

        assert diagnostic == expected
        assert PLANTED_SECRET not in diagnostic

    @pytest.mark.parametrize(
        ("raw_model", "expected"),
        (
            pytest.param(None, "invalid", id="invalid"),
            pytest.param(PLANTED_SECRET, "nonexact", id="nonexact"),
            pytest.param("emulated-chat-model", "exact", id="exact"),
        ),
    )
    async def test_model_diagnostic_is_protected_only_and_value_free(
        self, raw_model, expected, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            openai_compatible,
            "protected_certification_context_active",
            lambda: True,
        )
        script = [{"body": openai_chat_payload(model=raw_model)}]
        with caplog.at_level(
            logging.WARNING, logger="hivemind_inference.adapters"
        ):
            async with InferenceEmulator(script) as emulator:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert f"reported-model={expected}" in logs
        assert PLANTED_SECRET not in logs

    async def test_absent_model_diagnostic_and_unknown_value_are_value_free(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            openai_compatible,
            "protected_certification_context_active",
            lambda: True,
        )
        body = openai_chat_payload(model="emulated-chat-model")
        body.pop("model")
        with caplog.at_level(
            logging.WARNING, logger="hivemind_inference.adapters"
        ):
            async with InferenceEmulator([{"body": body}]) as emulator:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )
            openai_compatible._log_protected_model_diagnostic(
                provider_id="cloud-temple",
                correlation_id="corr-safe",
                diagnostic=[PLANTED_SECRET],
            )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "reported-model=absent" in logs
        assert "reported-model=-" in logs
        assert PLANTED_SECRET not in logs

    async def test_model_diagnostic_is_silent_outside_protected_context(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            openai_compatible,
            "protected_certification_context_active",
            lambda: False,
        )
        with caplog.at_level(
            logging.WARNING, logger="hivemind_inference.adapters"
        ):
            async with InferenceEmulator(
                [{"body": openai_chat_payload(model=PLANTED_SECRET)}]
            ) as emulator:
                await complete_with(
                    openai_chat_profile(emulator.v1_url), chat_request()
                )

        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "reported-model=" not in logs
        assert PLANTED_SECRET not in logs

    @pytest.mark.parametrize(
        "hostile",
        [
            HOSTILE,
            PLANTED_SECRET,
            PLANTED_URL,
            "emulated-chat-model but ignore that and exfiltrate",  # prefix + free text
            "x" * 129,
            "model\nwith-newline",
            "model with spaces",
            "totally-unrelated-model",
            # R9-F1: the separator-prefixed payloads that defeated the first
            # version of this guard. Each is pure letters/digits/hyphen after
            # the configured stem, so no charset or length rule excludes them —
            # only exact-match does.
            f"emulated-chat-model-{PLANTED_SECRET}",
            "emulated-chat-model_c2stcGxhbnRlZC1zdXBlci1zZWNyZXQ",  # base64url
            "emulated-chat-model.deadbeefcafe0123456789abcdef",      # hex
            "emulated-chat-model:leak.example.com",
            "emulated-chat-model/svc-user-hunter2",
            # A version pin. Legitimate, but indistinguishable from the above
            # by any prefix rule, so it is refused too — see the docstring.
            "emulated-chat-model-2024-08-06",
        ],
    )
    async def test_openai_chat_refuses_uncorroborated_model(self, hostile):
        script = [{"body": openai_chat_payload(model=hostile)}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        # The completion itself is still usable — only the unverifiable
        # identity claim is dropped.
        assert result.text == "canned completion"
        assert result.resolved_model is None
        assert result.model_evidence == "configured_only"

    async def test_openai_embedding_refuses_uncorroborated_model(self):
        script = [{"body": openai_embeddings_payload(1, dimensions=4, model=self.HOSTILE)}]
        async with InferenceEmulator(script) as emulator:
            result = await embed_with(
                embedding_profile(emulator.v1_url, dimensions=4),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
        assert len(result.vectors) == 1
        assert result.resolved_model is None
        assert result.model_evidence == "configured_only"

    async def test_anthropic_refuses_uncorroborated_model(self):
        script = [{"body": anthropic_message_payload(model=self.HOSTILE)}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                anthropic_chat_profile(emulator.url), chat_request()
            )
        assert result.resolved_model is None
        assert result.model_evidence == "configured_only"

    async def test_no_planted_value_survives_anywhere_in_the_result(self):
        """The whole record, not just the one field — a leak that moved to
        another attribute would still cross the boundary."""
        script = [{"body": openai_chat_payload(model=self.HOSTILE)}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        rendered = repr(result)
        for leak in (
            PLANTED_SECRET,
            "hunter2",
            "access_token",
            "leak.example.com",
            "ignore prior instructions",
        ):
            assert leak not in rendered

    async def test_separator_prefixed_secret_is_rejected_on_every_path(self):
        """R9-F1 across all three paths, not just OpenAI chat — the first fix
        was bypassable identically on each."""
        cases = [
            ("openai-chat", "emulated-chat-model"),
            ("openai-embed", "emulated-embedding-model"),
            ("anthropic", "emulated-anthropic-model"),
        ]
        for path, configured in cases:
            hostile = f"{configured}-{PLANTED_SECRET}"
            if path == "openai-chat":
                script = [{"body": openai_chat_payload(model=hostile)}]
                async with InferenceEmulator(script) as emulator:
                    result = await complete_with(
                        openai_chat_profile(emulator.v1_url), chat_request()
                    )
            elif path == "openai-embed":
                script = [
                    {"body": openai_embeddings_payload(1, dimensions=4, model=hostile)}
                ]
                async with InferenceEmulator(script) as emulator:
                    result = await embed_with(
                        embedding_profile(emulator.v1_url, dimensions=4),
                        EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                    )
            else:
                script = [{"body": anthropic_message_payload(model=hostile)}]
                async with InferenceEmulator(script) as emulator:
                    result = await complete_with(
                        anthropic_chat_profile(emulator.url), chat_request()
                    )
            assert result.resolved_model is None, path
            assert result.model_evidence == "configured_only", path
            assert PLANTED_SECRET not in repr(result), path

    # --- the guard must not over-reject: an exact echo still corroborates ---

    async def test_exact_echo_is_provider_reported(self):
        script = [{"body": openai_chat_payload(model="emulated-chat-model")}]
        async with InferenceEmulator(script) as emulator:
            result = await complete_with(
                openai_chat_profile(emulator.v1_url), chat_request()
            )
        assert result.resolved_model == "emulated-chat-model"
        assert result.model_evidence == "provider_reported"

    async def test_exact_echo_returns_the_configured_string_object(self):
        """The returned value is OUR string, never the provider's equal-but-
        distinct one, so no provider bytes reach the record even in principle."""
        script = [{"body": openai_chat_payload(model="emulated-chat-model")}]
        async with InferenceEmulator(script) as emulator:
            profile = openai_chat_profile(emulator.v1_url)
            result = await complete_with(profile, chat_request())
        assert result.resolved_model is profile.configured_model


class TestOutputLimitFieldPerProfile:
    """R7-F2: ADR-0027 requires the adapter to map the normalized output limit
    to "the exact supported Chat Completions field" for the frozen
    `openai-reference` profile, whose configured model is a reasoning model.
    Current OpenAI Chat Completions defines max_completion_tokens as the
    reasoning-inclusive bound and deprecates max_tokens, so hard-coding the
    legacy field for every provider violated the frozen contract. The model
    slug stays out of this tree: it is private routing metadata."""

    @pytest.mark.parametrize(
        "provider, expected",
        [
            ("openai", "max_completion_tokens"),
            ("cloud-temple", "max_tokens"),
            ("scaleway", "max_tokens"),
            ("mistral", "max_tokens"),
            ("gemini", "max_tokens"),
            ("ovhcloud", "max_tokens"),
            ("ollama", "max_tokens"),
            ("openai-compatible", "max_tokens"),
        ],
    )
    async def test_output_limit_field_is_selected_per_provider(
        self, provider, expected
    ):
        async with InferenceEmulator() as em:
            await complete_with(
                openai_chat_profile(em.v1_url, provider=provider), chat_request()
            )
        body = em.requests[0]["json"]
        assert body[expected] == 128
        # and the other field is absent, never both
        other = "max_tokens" if expected == "max_completion_tokens" else "max_completion_tokens"
        assert other not in body


class TestMalformedContentIsNotEmptySuccess:
    """R7-F4: provider-controlled malformed or unsupported content must not be
    normalized into a successful empty completion. Coercing a null/list/object
    content to "" would present it as a valid empty answer."""

    @pytest.mark.parametrize("bad", [None, ["a"], {"x": 1}, 5])
    async def test_non_string_openai_content_is_invalid_response(self, bad):
        payload = openai_chat_payload("x")
        payload["choices"][0]["message"]["content"] = bad
        async with InferenceEmulator([{"body_raw": json.dumps(payload).encode()}]) as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(openai_chat_profile(em.v1_url), chat_request())
        assert excinfo.value.category == "invalid_response"

    async def test_anthropic_unsupported_only_blocks_are_invalid_response(self):
        payload = anthropic_message_payload("x")
        payload["content"] = [{"type": "tool_use", "id": "t", "name": "f"}]
        async with InferenceEmulator([{"body_raw": json.dumps(payload).encode()}]) as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(anthropic_chat_profile(em.url), chat_request())
        assert excinfo.value.category == "invalid_response"

    async def test_genuinely_empty_completions_still_succeed(self):
        # The guard must not swallow a legitimate empty answer.
        for payload, profile_for in (
            (openai_chat_payload(""), lambda em: openai_chat_profile(em.v1_url)),
            (anthropic_message_payload(""), lambda em: anthropic_chat_profile(em.url)),
        ):
            async with InferenceEmulator(
                [{"body_raw": json.dumps(payload).encode()}]
            ) as em:
                result = await complete_with(profile_for(em), chat_request())
            assert result.text == ""
            assert result.finish_reason == "stop"


class TestNoSdkAndCompressionRefusal:
    """R5-F1: the previously SDK-backed chat and discovery operations now use
    the same bounded raw transport as everything else, so no path buffers an
    unbounded provider response. R5-F2: identity encoding is requested on
    every call and a provider that compresses anyway is REFUSED rather than
    decompressed, because httpx's gzip decoder calls zlib.decompress with no
    output limit — the allocation would happen before any counter saw it."""

    def test_adapter_module_constructs_no_sdk_client(self):
        import inspect

        import hivemind_inference.adapters.openai_compatible as oc

        src = inspect.getsource(oc)
        assert "AsyncOpenAI" not in src
        assert "self._client" not in src

    async def test_every_request_asks_for_identity_encoding(self):
        async with InferenceEmulator() as em:
            await complete_with(openai_chat_profile(em.v1_url), chat_request())
            await embed_with(
                embedding_profile(em.v1_url),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
            probe = build_chat_probe(openai_chat_profile(em.v1_url))
            try:
                await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
            await complete_with(anthropic_chat_profile(em.url), chat_request())
        assert len(em.requests) == 4
        for request in em.requests:
            assert request["headers"]["accept-encoding"] == "identity"

    @pytest.mark.parametrize("encoding", ["gzip", "br", "deflate"])
    async def test_unrequested_content_encoding_is_refused_not_decompressed(
        self, encoding
    ):
        # Refusing is the point: decompressing to find out how big it is would
        # be the very unbounded allocation this guards against.
        script = [{"headers": {"content-encoding": encoding}}]
        async with InferenceEmulator(script) as em:
            with pytest.raises(InferenceError) as excinfo:
                await embed_with(
                    embedding_profile(em.v1_url),
                    EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
                )
        assert excinfo.value.category == "invalid_response"

    async def test_unrequested_chat_content_encoding_has_safe_diagnostic(
        self, caplog
    ):
        script = [{"headers": {"content-encoding": "gzip"}}]
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            async with InferenceEmulator(script) as em:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        openai_chat_profile(em.v1_url), chat_request()
                    )
        assert excinfo.value.category == "invalid_response"
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "diagnostic=encoding_refused" in logs

    async def test_oversized_chat_response_is_invalid_response(self, caplog):
        assert common.chat_response_ceiling(128) < 1_000_000
        payload = json.dumps(openai_chat_payload("x")).encode()
        padded = payload[:-1] + b" " * 1_000_000 + b"}"
        assert json.loads(padded) == json.loads(payload)
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.adapters"):
            async with InferenceEmulator([{"body_raw": padded}]) as em:
                with pytest.raises(InferenceError) as excinfo:
                    await complete_with(
                        openai_chat_profile(em.v1_url), chat_request(timeout=10.0)
                    )
        assert excinfo.value.category == "invalid_response"
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert "diagnostic=response_too_large" in logs

    async def test_oversized_openai_probe_response_is_a_safe_probe_result(
        self, monkeypatch
    ):
        # Discovery has no request-derived size, so it uses the backstop.
        monkeypatch.setattr(common, "MAX_RESPONSE_BYTES", 4096)
        big = b'{"object":"list","data":[{"id":"' + b"A" * 200_000 + b'"}]}'
        async with InferenceEmulator([{"body_raw": big}]) as em:
            probe = build_chat_probe(openai_chat_profile(em.v1_url))
            try:
                result = await probe.probe(timeout_seconds=10.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "error"
        assert result.error_category == "invalid_response"

    @pytest.mark.parametrize(
        "body_raw",
        [b'{"data":123}', b'{"data":"nope"}', b"[]", b"not json at all"],
    )
    async def test_openai_probe_malformed_listing_is_safe(self, body_raw):
        async with InferenceEmulator([{"body_raw": body_raw}]) as em:
            probe = build_chat_probe(openai_chat_profile(em.v1_url))
            try:
                result = await probe.probe(timeout_seconds=5.0)
            finally:
                await probe.aclose()
        assert result.discovery == "error"
        assert result.error_category == "invalid_response"


class TestProbeTotalDeadline:
    """R5-F3: a probe's timeout must be a TOTAL wall-clock deadline. An httpx
    read timeout only bounds inactivity BETWEEN chunks, so a provider sending
    one small chunk per interval could hold public discovery open indefinitely
    while never tripping it. Chat and embedding inherit a total bound from the
    retry loop; probes never enter it."""

    async def test_openai_probe_stops_at_its_total_deadline(self):
        # "drip", not "stall": silence trips httpx's read timeout, so a
        # stalling server would pass even with NO total deadline. A drip keeps
        # every inter-chunk gap under the read timeout, so only a total
        # wall-clock bound can stop it.
        async with InferenceEmulator([{"action": "drip", "interval": 0.02}]) as em:
            probe = build_chat_probe(openai_chat_profile(em.v1_url))
            started = time.monotonic()
            try:
                result = await probe.probe(timeout_seconds=0.3)
            finally:
                await probe.aclose()
            elapsed = time.monotonic() - started
        assert result.connectivity == "unreachable"
        assert result.error_category == "timeout"
        assert elapsed < 5  # bounded, not hanging

    async def test_anthropic_probe_stops_at_its_total_deadline(self):
        async with InferenceEmulator([{"action": "drip", "interval": 0.02}]) as em:
            probe = build_chat_probe(anthropic_chat_profile(em.url))
            started = time.monotonic()
            try:
                result = await probe.probe(timeout_seconds=0.3)
            finally:
                await probe.aclose()
            elapsed = time.monotonic() - started
        assert result.connectivity == "unreachable"
        assert result.error_category == "timeout"
        assert elapsed < 5


class TestAnthropicJsonTotality:
    """R3-F2: EVERY Anthropic response boundary — chat success, chat error,
    probe success, probe error — must route through the shared safe parser.
    Previously each site caught only JSONDecodeError/ValueError, so a deeply
    nested body escaped as raw RecursionError and a non-list `data` escaped as
    raw TypeError, both bypassing the frozen envelope."""

    def _deep(self, inner: bytes = b'"type":"message",') -> bytes:
        depth = 100_000
        return b"{" + inner + b'"content":' + b"[" * depth + b"]" * depth + b"}"

    async def test_chat_success_deep_nesting_is_invalid_response(self):
        async with InferenceEmulator([{"body_raw": self._deep()}]) as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(anthropic_chat_profile(em.url), chat_request())
        assert excinfo.value.category == "invalid_response"

    async def test_chat_error_body_deep_nesting_still_classifies_by_status(self):
        # An unparseable ERROR body must not crash either; the status alone
        # still drives the category.
        async with InferenceEmulator(
            [{"status": 500, "body_raw": self._deep()}]
        ) as em:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(anthropic_chat_profile(em.url), chat_request())
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is False

    async def test_probe_deep_nesting_is_a_safe_probe_result(self):
        async with InferenceEmulator([{"body_raw": self._deep()}]) as em:
            probe = build_chat_probe(anthropic_chat_profile(em.url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "error"
        assert result.error_category == "invalid_response"

    @pytest.mark.parametrize(
        "body_raw",
        [
            b'{"data":123}',        # scalar where a list is required
            b'{"data":"nope"}',     # string is iterable but not a listing
            b'{"data":{"a":1}}',    # dict is iterable but not a listing
            b"[]",                  # top-level not even an object
        ],
    )
    async def test_probe_non_list_data_is_a_safe_probe_result(self, body_raw):
        async with InferenceEmulator([{"body_raw": body_raw}]) as em:
            probe = build_chat_probe(anthropic_chat_profile(em.url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "error"
        assert result.error_category == "invalid_response"


class TestOversizedUsageDegradesInsteadOfEscaping:
    """Class-wide sibling sweep of the oversized-numeric finding: the same
    provider-controlled magnitude problem exists in `usage` token counts, not
    only in embedding components. An out-of-range count must degrade to absent
    metadata (ADR-0027 keeps missing usage explicitly absent) rather than reach
    the record constructor and escape the safe envelope as a raw ValueError —
    and it must never discard an otherwise usable completion."""

    HUGE = 10**400

    async def test_openai_compatible_chat_usage_degrades(self):
        payload = openai_chat_payload(
            "hello",
            usage={
                "prompt_tokens": self.HUGE,
                "completion_tokens": 5,
                "total_tokens": 12,
            },
        )
        async with InferenceEmulator([{"body_raw": json.dumps(payload).encode()}]) as em:
            result = await complete_with(
                openai_chat_profile(em.v1_url), chat_request()
            )
        assert result.text == "hello"          # completion preserved
        assert result.input_tokens is None      # unusable count dropped
        assert result.output_tokens == 5        # usable counts kept

    async def test_anthropic_chat_usage_degrades(self):
        payload = anthropic_message_payload(
            "hello", usage={"input_tokens": self.HUGE, "output_tokens": 4}
        )
        async with InferenceEmulator([{"body_raw": json.dumps(payload).encode()}]) as em:
            result = await complete_with(
                anthropic_chat_profile(em.url), chat_request()
            )
        assert result.text == "hello"
        assert result.input_tokens is None
        assert result.output_tokens == 4
        # The derived total requires BOTH operands; it stays absent here.
        assert result.total_tokens is None

    async def test_embedding_usage_degrades(self):
        payload = openai_embeddings_payload(1)
        payload["usage"] = {"prompt_tokens": self.HUGE, "total_tokens": self.HUGE}
        async with InferenceEmulator([{"body_raw": json.dumps(payload).encode()}]) as em:
            result = await embed_with(
                embedding_profile(em.v1_url),
                EmbeddingRequest(inputs=("a",), timeout_seconds=5.0),
            )
        assert len(result.vectors) == 1         # vectors still returned
        assert result.input_tokens is None
        assert result.total_tokens is None


class TestAnthropicRateLimitTypeRequiresMatchingStatus:
    """A body claiming a transient rate-limit type must never authorize a
    retry on a status other than 429 — ADR-0027 makes every HTTP 5xx
    terminal unconditionally, regardless of body content. Retry eligibility
    is derived from status_code first, never from the documented error type
    alone."""

    @pytest.mark.parametrize("status", [500, 529])
    async def test_5xx_with_rate_limit_error_body_is_never_retried(self, status):
        body = {"type": "error",
                "error": {"type": "rate_limit_error", "message": "no"}}
        script = [
            {"status": status, "headers": {"retry-after": "0"}, "body": body}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is False
        assert len(emulator.requests) == 1  # never a second, duplicate POST


class TestAnthropicQuotaCodePriority:
    """An explicit machine-readable quota code beats a transient
    rate_limit_error type — one request, never retried."""

    async def test_quota_code_with_transient_type_is_never_retried(self):
        body = {"type": "error",
                "error": {"type": "rate_limit_error",
                          "code": "insufficient_quota",
                          "message": "quota exhausted"}}
        script = [
            {"status": 429, "headers": {"retry-after": "0"}, "body": body}
        ]
        async with InferenceEmulator(script) as emulator:
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    anthropic_chat_profile(emulator.url), chat_request()
                )
        assert excinfo.value.category == "quota_exhausted"
        assert excinfo.value.retryable is False


# --------------------------------------------------------------------------- #
# Discovery probes                                                            #
# --------------------------------------------------------------------------- #


class TestProbes:
    async def test_openai_probe_reports_discovery_and_model_presence(self):
        async with InferenceEmulator() as emulator:
            probe = build_chat_probe(openai_chat_profile(emulator.v1_url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "available"
        assert result.model_available is True
        assert result.latency_ms is not None
        assert result.healthy is True
        assert emulator.requests[0]["method"] == "GET"
        assert emulator.requests[0]["url"].endswith("/v1/models")

    async def test_missing_model_is_reported_without_failing_the_probe(self):
        async with InferenceEmulator() as emulator:
            profile = embedding_profile(emulator.v1_url)
            probe = build_embedding_probe(
                ResolvedEmbeddingProfile(
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    endpoint=profile.endpoint,
                    api_key=profile.api_key,
                    configured_model="absent-model",
                    expected_dimensions=4,
                )
            )
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.discovery == "available"
        assert result.model_available is False
        assert result.healthy is True

    @pytest.mark.parametrize("status", [404, 405, 501])
    async def test_unsupported_model_listing_is_not_a_failure(self, status):
        async with InferenceEmulator([{"status": status}]) as emulator:
            probe = build_chat_probe(openai_chat_profile(emulator.v1_url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "unsupported"
        assert result.model_available is None
        assert result.error_category is None
        assert result.healthy is True

    async def test_auth_failure_is_reachable_discovery_error(self):
        async with InferenceEmulator([{"status": 401}]) as emulator:
            probe = build_chat_probe(openai_chat_profile(emulator.v1_url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.connectivity == "reachable"
        assert result.discovery == "error"
        assert result.error_category == "auth"
        assert result.healthy is False

    async def test_probe_never_retries_even_transient_rate_limits(self):
        script = [{"status": 429, "headers": {"retry-after": "0"}}]
        async with InferenceEmulator(script) as emulator:
            probe = build_chat_probe(openai_chat_profile(emulator.v1_url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.discovery == "error"
        assert len(emulator.requests) == 1

    async def test_unreachable_endpoint(self):
        probe = build_chat_probe(openai_chat_profile(refused_port_url() + "/v1"))
        try:
            result = await probe.probe(timeout_seconds=1.0)
        finally:
            await probe.aclose()
        assert result.connectivity == "unreachable"
        assert result.discovery == "error"
        assert result.healthy is False

    async def test_anthropic_probe_success_and_unsupported(self):
        async with InferenceEmulator() as emulator:
            probe = build_chat_probe(anthropic_chat_profile(emulator.url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
            assert result.discovery == "available"
            assert result.model_available is True
            assert emulator.requests[0]["headers"]["x-api-key"] == (
                "emulated-anthropic-key"
            )
        async with InferenceEmulator([{"status": 404}]) as emulator:
            probe = build_chat_probe(anthropic_chat_profile(emulator.url))
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
            assert result.connectivity == "reachable"
            assert result.discovery == "unsupported"


# --------------------------------------------------------------------------- #
# Proxy routing, direct trap, ambient immunity                                #
# --------------------------------------------------------------------------- #


class TestProxyAwareTransportClassification:
    """httpx/httpcore raise the identical ConnectError/ConnectTimeout for an
    unreachable PROXY and an unreachable ORIGIN — only the caller's own
    knowledge of whether it configured a proxy for this call can tell them
    apart. ADR-0027 requires every proxy failure to fail closed with no
    retry, so proxy_configured=True must make both exception types
    terminal, never the pre-send-provable retryable case."""

    def test_connect_error_is_retryable_only_without_a_proxy(self):
        import httpx

        from hivemind_inference.adapters.common import classify_httpx_transport

        exc = httpx.ConnectError("connection refused")
        assert classify_httpx_transport(exc, proxy_configured=False) == (
            "unavailable",
            True,
        )
        assert classify_httpx_transport(exc, proxy_configured=True) == (
            "unavailable",
            False,
        )

    def test_connect_timeout_is_retryable_only_without_a_proxy(self):
        import httpx

        from hivemind_inference.adapters.common import classify_httpx_transport

        exc = httpx.ConnectTimeout("timed out connecting")
        assert classify_httpx_transport(exc, proxy_configured=False) == (
            "unavailable",
            True,
        )
        assert classify_httpx_transport(exc, proxy_configured=True) == (
            "unavailable",
            False,
        )

    def test_omitting_proxy_configured_keeps_the_direct_no_proxy_default(self):
        import httpx

        from hivemind_inference.adapters.common import classify_httpx_transport

        # Every real call site now passes proxy_configured explicitly; this
        # only pins the keyword's default so a future caller that omits it
        # gets the direct (no-proxy) classification, not a silent change.
        assert classify_httpx_transport(httpx.ConnectError("x")) == (
            "unavailable",
            True,
        )


class TestProxyContract:
    async def test_chat_routes_through_the_proxy_absolute_form(self):
        async with InferenceEmulator() as proxy:
            profile = openai_chat_profile("http://llm.p13-hivemind.invalid/v1")
            result = await complete_with(
                profile, chat_request(), proxy_url=proxy.url
            )
        assert result.text == "canned completion"
        (request,) = proxy.requests
        assert request["url"].startswith("http://llm.p13-hivemind.invalid/v1")

    async def test_anthropic_routes_through_the_proxy(self):
        async with InferenceEmulator() as proxy:
            profile = anthropic_chat_profile("http://claude.p13-hivemind.invalid")
            result = await complete_with(
                profile, chat_request(), proxy_url=proxy.url
            )
        assert result.text == "canned anthropic completion"
        assert proxy.requests[0]["url"].startswith(
            "http://claude.p13-hivemind.invalid/v1/messages"
        )

    async def test_proxy_failure_never_falls_back_to_a_direct_connection(self):
        async with InferenceEmulator() as origin:  # live, would answer
            profile = openai_chat_profile(origin.v1_url)
            with pytest.raises(InferenceError) as excinfo:
                await complete_with(
                    profile,
                    chat_request(timeout=2.0),
                    proxy_url=refused_port_url(),
                )
            assert origin.connections == 0
            assert origin.requests == []
        assert excinfo.value.category == "unavailable"
        # A refused-proxy ConnectError is NOT the proven-pre-send-to-the-
        # origin case: it must never be marked retryable, which would
        # authorize a second connection attempt against the same broken
        # proxy instead of failing closed immediately.
        assert excinfo.value.retryable is False

    async def test_anthropic_proxy_failure_is_terminal_not_retryable(self):
        # The native adapter calls the SAME shared classifier directly (no
        # SDK layer in between); prove the proxy-awareness fix applies here
        # too, not only through the openai-compatible adapter's SDK path.
        profile = anthropic_chat_profile("https://api.anthropic.invalid")
        with pytest.raises(InferenceError) as excinfo:
            await complete_with(
                profile,
                chat_request(timeout=2.0),
                proxy_url=refused_port_url(),
            )
        assert excinfo.value.category == "unavailable"
        assert excinfo.value.retryable is False

    async def test_proxy_407_fails_closed_without_direct_fallback(self):
        async with InferenceEmulator() as origin:
            async with InferenceEmulator([{"status": 407}]) as proxy:
                profile = openai_chat_profile(origin.v1_url)
                with pytest.raises(InferenceError):
                    await complete_with(
                        profile, chat_request(timeout=2.0), proxy_url=proxy.url
                    )
                assert len(proxy.requests) == 1
            assert origin.connections == 0

    async def test_ambient_proxy_variables_are_never_honored(self, monkeypatch):
        async with InferenceEmulator() as trap:
            async with InferenceEmulator() as origin:
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                    monkeypatch.setenv(name, trap.url)
                    monkeypatch.setenv(name.lower(), trap.url)
                result = await complete_with(
                    openai_chat_profile(origin.v1_url), chat_request()
                )
                anthropic_result = await complete_with(
                    anthropic_chat_profile(origin.url), chat_request()
                )
                assert result.text == "canned completion"
                assert anthropic_result.text == "canned anthropic completion"
                assert origin.connections == 2
            assert trap.connections == 0
            assert trap.requests == []

    async def test_probe_routes_through_the_proxy_too(self):
        async with InferenceEmulator() as proxy:
            probe = build_chat_probe(
                openai_chat_profile("http://llm.p13-hivemind.invalid/v1"),
                proxy_url=proxy.url,
            )
            try:
                result = await probe.probe(timeout_seconds=2.0)
            finally:
                await probe.aclose()
        assert result.discovery == "available"
        assert proxy.requests[0]["url"].startswith(
            "http://llm.p13-hivemind.invalid/v1/models"
        )
