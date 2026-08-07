# -*- coding: utf-8 -*-
"""Native Anthropic chat adapter (Messages API over the owned transport).

Chat-only by the frozen registry contract — Anthropic has no native embedding
model and the ``anthropic`` provider fails embedding-role configuration
validation. The adapter speaks the native Messages API directly over the
owned ``httpx`` transport (no OpenAI compatibility layer and no extra SDK
dependency); the deterministic Anthropic-shaped emulator proves the exact
wire behavior in ordinary CI. P13-3 binds this adapter to the frozen
`anthropic-cloud-temple-reference` composite; real-provider certification
still requires a separately authorized protected final-SHA run.

Wire contract implemented here:

- ``POST {endpoint}/v1/messages`` with ``x-api-key`` and
  ``anthropic-version: 2023-06-01`` headers;
- leading ``system`` messages map to the top-level ``system`` parameter;
  a ``system`` message after a non-system message is ``invalid_request``
  (the native API has no mid-conversation system role);
- ``max_tokens`` is always sent (the API requires it); the profile
  temperature is sent only when configured;
- ``stop_reason`` mapping: ``end_turn``/``stop_sequence`` -> ``stop``,
  ``max_tokens`` -> ``length``, ``refusal`` -> ``content_rejected`` (raised,
  never a successful empty completion), anything else -> ``other``;
- discovery probe: ``GET {endpoint}/v1/models`` (zero retries).
"""

from __future__ import annotations

import asyncio
import time

from ..egress import build_owned_async_http_client, close_owned_client_from_sync
from ..errors import InferenceError
from ..profiles import ResolvedChatProfile
from ..records import ChatRequest, ChatResult, ProbeResult
from ..retry import AttemptFailure, parse_retry_after_seconds, run_with_bounded_retry
from .common import (
    IDENTITY_ENCODING_HEADERS,
    ResponseEncodingRefused,
    ResponseTooLarge,
    classify_httpx_transport,
    log_provider_failure,
    chat_response_ceiling,
    chat_response_is_serviceable,
    make_error,
    read_bounded_body,
    safe_json_body,
    safe_resolved_model,
    safe_token_count,
)

ANTHROPIC_VERSION = "2023-06-01"

_CLIENT_DEFAULT_TIMEOUT_SECONDS = 60.0

_DISCOVERY_UNSUPPORTED_STATUSES = (404, 405, 501)

# Native error ``type`` -> category.
_ERROR_TYPE_CATEGORIES: dict[str, str] = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "rate_limit_error": "rate_limited",
    "invalid_request_error": "invalid_request",
    "not_found_error": "invalid_request",
    "request_too_large": "invalid_request",
    "overloaded_error": "unavailable",
    "api_error": "unavailable",
}


class _AnthropicBase:
    def __init__(
        self, profile: ResolvedChatProfile, *, proxy_url: str | None = None
    ) -> None:
        if profile.adapter_id != "anthropic":
            raise ValueError("AnthropicChatProvider requires the anthropic adapter")
        self._profile = profile
        self._proxy_configured = bool(proxy_url)
        self._owned_http_client = build_owned_async_http_client(
            proxy_url, timeout=_CLIENT_DEFAULT_TIMEOUT_SECONDS
        )

    async def aclose(self) -> None:
        await self._owned_http_client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._profile.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
            # Identity encoding makes the raw-byte cap a true total bound; see
            # read_bounded_body for why counting decoded bytes is not enough.
            **IDENTITY_ENCODING_HEADERS,
        }

    def _url(self, path: str) -> str:
        return self._profile.endpoint.rstrip("/") + path

    def _classify_status(
        self, status_code: int, body: object, headers
    ) -> tuple[str, bool, float | None]:
        error_type = None
        quota_code = False
        if isinstance(body, dict):
            error = body.get("error")
            candidates = [body.get("type"), body.get("code")]
            if isinstance(error, dict):
                if isinstance(error.get("type"), str):
                    error_type = error["type"]
                candidates.extend([error.get("type"), error.get("code")])
            # A machine-readable quota/credit exhaustion code takes PRECEDENCE
            # over a transient rate-limit type — never a duplicate paid
            # request on an exhausted quota. The substring match is
            # deliberately broad (mirrored in the openai-compatible adapter's
            # _quota_exhaustion_code): over-matching only costs availability
            # (a retryable-shaped 429 becomes terminal), while under-matching
            # could let real quota exhaustion slip through and be retried —
            # the one outcome ADR-0027 forbids outright. The transient-code
            # check below stays a narrow exact match by contrast, since that
            # one AUTHORIZES a retry and must not be loosened.
            quota_code = any(
                isinstance(c, str) and "insufficient_quota" in c
                for c in candidates
            )
        # ADR-0027 makes every HTTP 5xx terminal UNCONDITIONALLY: retry
        # eligibility may be derived ONLY from an actual 429 status, never
        # from a documented error `type` alone. Gating on status_code FIRST
        # means a malformed/misleading 5xx body that happens to carry
        # `"type": "rate_limit_error"` cannot masquerade as a retryable rate
        # limit — without this gate, `_ERROR_TYPE_CATEGORIES` would resolve
        # "rate_limited" from the body regardless of the real status.
        if status_code == 429:
            if quota_code:
                return "quota_exhausted", False, None
            # Retry requires the CONJOINT proof of a machine-readable
            # transient type (``rate_limit_error``) AND a valid bounded
            # Retry-After (ADR-0027) — a bodyless, unknown-type, or
            # ambiguous policy/quota 429 stays terminal.
            delay = parse_retry_after_seconds(headers.get("retry-after"))
            if delay is not None and error_type == "rate_limit_error":
                return "rate_limited", True, delay
            return "rate_limited", False, None
        category = _ERROR_TYPE_CATEGORIES.get(error_type or "")
        if category in (None, "rate_limited"):
            # A "rate_limited"-mapped type on a non-429 status is exactly the
            # contradictory shape above: discard it and fall back to the
            # plain status-code bucket, never retryable.
            if status_code in (401, 403):
                category = "auth"
            elif 400 <= status_code < 500:
                category = "invalid_request"
            else:
                category = "unavailable"
        return category, False, None

    def _make_error(
        self, category: str, *, retryable: bool, correlation_id: str
    ) -> InferenceError:
        return make_error(
            category=category,
            role="chat",
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            retryable=retryable,
            correlation_id=correlation_id,
        )

    def _direct_error(
        self,
        category: str,
        *,
        correlation_id: str,
        exc: BaseException | None = None,
        status_code: int | None = None,
    ) -> InferenceError:
        log_provider_failure(
            role="chat",
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            category=category,
            correlation_id=correlation_id,
            exc=exc,
            status_code=status_code,
        )
        return self._make_error(category, retryable=False, correlation_id=correlation_id)


class AnthropicChatProvider(_AnthropicBase):
    """``ChatProvider`` implementation over the native Messages API."""

    @property
    def profile(self) -> ResolvedChatProfile:
        return self._profile

    def _build_body(self, request: ChatRequest, effective_max: int) -> dict:
        system_parts: list[str] = []
        wire_messages: list[dict] = []
        for message in request.messages:
            if message.role == "system":
                if wire_messages:
                    raise self._direct_error(
                        "invalid_request", correlation_id=request.correlation_id
                    )
                system_parts.append(message.content)
            else:
                wire_messages.append(
                    {"role": message.role, "content": message.content}
                )
        if not wire_messages:
            raise self._direct_error(
                "invalid_request", correlation_id=request.correlation_id
            )
        body: dict = {
            "model": self._profile.configured_model,
            "max_tokens": effective_max,
            "messages": wire_messages,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if self._profile.temperature is not None:
            body["temperature"] = self._profile.temperature
        return body

    async def complete(self, request: ChatRequest) -> ChatResult:
        profile = self._profile
        effective_max = profile.max_output_tokens
        if request.max_output_tokens is not None:
            if request.max_output_tokens > profile.max_output_tokens:
                raise self._direct_error(
                    "invalid_request", correlation_id=request.correlation_id
                )
            effective_max = request.max_output_tokens
        if not chat_response_is_serviceable(effective_max):
            raise self._direct_error(
                "invalid_request", correlation_id=request.correlation_id
            )
        body = self._build_body(request, effective_max)
        # Keep small visible-output responses request-aware. Reasoning-inclusive
        # budgets do not predict serialized bytes, so larger requests saturate
        # at the independent 8 MiB raw-body safety boundary.
        ceiling = chat_response_ceiling(effective_max)

        async def attempt(remaining_seconds: float):
            try:
                # stream(), not post(): post() eagerly buffers the whole
                # body before validation. read_bounded_body caps RAW bytes and
                # refuses content encoding, so an oversized or compressed
                # response cannot exhaust memory ahead of the error envelope.
                async with self._owned_http_client.stream(
                    "POST",
                    self._url("/v1/messages"),
                    headers=self._headers(),
                    json=body,
                    timeout=remaining_seconds,
                ) as response:
                    status_code = response.status_code
                    response_headers = response.headers
                    raw_body = await read_bounded_body(response, ceiling)
            except (ResponseTooLarge, ResponseEncodingRefused):
                error = self._make_error(
                    "invalid_response",
                    retryable=False,
                    correlation_id=request.correlation_id,
                )
                log_provider_failure(
                    role="chat",
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    category="invalid_response",
                    correlation_id=request.correlation_id,
                )
                raise AttemptFailure(error, None) from None
            except Exception as exc:
                category, retryable = classify_httpx_transport(
                    exc, proxy_configured=self._proxy_configured
                )
                log_provider_failure(
                    role="chat",
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    category=category,
                    correlation_id=request.correlation_id,
                    exc=exc,
                )
                error = self._make_error(
                    category, retryable=retryable, correlation_id=request.correlation_id
                )
                raise AttemptFailure(error, 0.0 if retryable else None) from None
            if status_code != 200:
                payload = safe_json_body(raw_body)
                category, retryable, delay = self._classify_status(
                    status_code, payload, response_headers
                )
                log_provider_failure(
                    role="chat",
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    category=category,
                    correlation_id=request.correlation_id,
                    status_code=status_code,
                )
                error = self._make_error(
                    category, retryable=retryable, correlation_id=request.correlation_id
                )
                raise AttemptFailure(error, delay)
            return self._normalize_response(raw_body, request)

        return await run_with_bounded_retry(
            attempt,
            timeout_seconds=request.timeout_seconds,
            role="chat",
            provider_id=profile.provider_id,
            adapter_id=profile.adapter_id,
            correlation_id=request.correlation_id,
            retry_policy=request.retry_policy,
        )

    def _normalize_response(self, raw_body: bytes, request: ChatRequest) -> ChatResult:
        profile = self._profile

        def _invalid() -> InferenceError:
            return self._direct_error(
                "invalid_response",
                correlation_id=request.correlation_id,
                status_code=200,
            )

        data = safe_json_body(raw_body)
        if not isinstance(data, dict) or data.get("type") != "message":
            raise _invalid()
        content = data.get("content")
        if not isinstance(content, list):
            raise _invalid()
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                raise _invalid()
            if block.get("type") == "text":
                block_text = block.get("text")
                if not isinstance(block_text, str):
                    raise _invalid()
                text_parts.append(block_text)
        # At least one supported text block is required. A content list made
        # only of unsupported blocks (tool_use, etc.) is not a successful empty
        # completion — it is a shape this lot does not support, and silently
        # returning "" would let it be consumed as a valid answer.
        if not text_parts:
            raise _invalid()
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            raise self._direct_error(
                "content_rejected", correlation_id=request.correlation_id
            )
        if stop_reason in ("end_turn", "stop_sequence"):
            normalized_finish = "stop"
        elif stop_reason == "max_tokens":
            normalized_finish = "length"
        else:
            normalized_finish = "other"
        resolved_model = safe_resolved_model(
            data.get("model"), profile.configured_model
        )
        usage = data.get("usage")
        input_tokens = output_tokens = None
        if isinstance(usage, dict):
            # safe_token_count is shared with the openai-compatible adapter and
            # delegates its magnitude bound to the same predicate the records
            # enforce, so an oversized provider-reported count degrades to
            # absent metadata instead of escaping as a raw ValueError from the
            # ChatResult constructor.
            input_tokens = safe_token_count(usage.get("input_tokens"))
            output_tokens = safe_token_count(usage.get("output_tokens"))
        # The derived total is bounded too: two in-range counts can still sum
        # past the record's ceiling, and a rejected total must not discard a
        # usable completion.
        total_tokens = (
            safe_token_count(input_tokens + output_tokens)
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return ChatResult(
            text="".join(text_parts),
            configured_model=profile.configured_model,
            model_evidence=(
                "provider_reported" if resolved_model is not None else "configured_only"
            ),
            finish_reason=normalized_finish,
            resolved_model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            correlation_id=request.correlation_id,
        )


class AnthropicProbe(_AnthropicBase):
    """Discovery probe over ``GET /v1/models`` — zero retries."""

    async def probe(self, *, timeout_seconds: float = 5.0) -> ProbeResult:
        started = time.monotonic()
        try:
            # asyncio.timeout gives the probe a TOTAL wall-clock deadline. The
            # httpx timeout alone bounds only inactivity BETWEEN chunks, so a
            # provider dripping one byte per interval could hold public
            # discovery open indefinitely. Chat inherits this bound from the
            # retry loop; probes never enter it, so they need their own.
            async with asyncio.timeout(timeout_seconds):
                async with self._owned_http_client.stream(
                    "GET",
                    self._url("/v1/models"),
                    headers=self._headers(),
                    timeout=timeout_seconds,
                ) as response:
                    status_code = response.status_code
                    response_headers = response.headers
                    raw_body = await read_bounded_body(response)
        except TimeoutError:
            log_provider_failure(
                role="chat",
                provider_id=self._profile.provider_id,
                adapter_id=self._profile.adapter_id,
                category="timeout",
                correlation_id="probe",
            )
            return ProbeResult(
                connectivity="unreachable",
                discovery="error",
                model_available=None,
                error_category="timeout",
            )
        except (ResponseTooLarge, ResponseEncodingRefused):
            return ProbeResult(
                connectivity="reachable",
                discovery="error",
                model_available=None,
                error_category="invalid_response",
            )
        except Exception as exc:
            # retryable is discarded: discovery probes never retry regardless
            # of category; proxy_configured is still threaded through for
            # consistency with the chat path and in case a future caller
            # inspects it.
            category, _ = classify_httpx_transport(
                exc, proxy_configured=self._proxy_configured
            )
            log_provider_failure(
                role="chat",
                provider_id=self._profile.provider_id,
                adapter_id=self._profile.adapter_id,
                category=category,
                correlation_id="probe",
                exc=exc,
            )
            return ProbeResult(
                connectivity="unreachable",
                discovery="error",
                model_available=None,
                error_category=category,
            )
        latency = round((time.monotonic() - started) * 1000, 1)
        if status_code in _DISCOVERY_UNSUPPORTED_STATUSES:
            return ProbeResult(
                connectivity="reachable",
                discovery="unsupported",
                model_available=None,
                latency_ms=latency,
            )
        if status_code != 200:
            payload = safe_json_body(raw_body)
            category, _, _ = self._classify_status(
                status_code, payload, response_headers
            )
            log_provider_failure(
                role="chat",
                provider_id=self._profile.provider_id,
                adapter_id=self._profile.adapter_id,
                category=category,
                correlation_id="probe",
                status_code=status_code,
            )
            return ProbeResult(
                connectivity="reachable",
                discovery="error",
                model_available=None,
                latency_ms=latency,
                error_category=category,
            )
        data = safe_json_body(raw_body)
        listing = data.get("data") if isinstance(data, dict) else None
        # The container type MUST be checked before traversal: a valid JSON
        # scalar such as {"data": 123} parses fine and would otherwise raise a
        # raw TypeError straight past the ProbeResult envelope.
        if not isinstance(listing, list):
            return ProbeResult(
                connectivity="reachable",
                discovery="error",
                model_available=None,
                latency_ms=latency,
                error_category="invalid_response",
            )
        model_ids = [
            item.get("id") for item in listing if isinstance(item, dict)
        ]
        return ProbeResult(
            connectivity="reachable",
            discovery="available",
            model_available=self._profile.configured_model in model_ids,
            latency_ms=latency,
        )
