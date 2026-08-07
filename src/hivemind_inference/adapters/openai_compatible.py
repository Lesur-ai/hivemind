# -*- coding: utf-8 -*-
"""Generic OpenAI-compatible adapter (chat, embeddings, discovery probe).

One adapter serves every explicitly registered OpenAI-compatible provider
profile (Cloud Temple, Gemini, Scaleway, OpenAI, Mistral, OVHcloud, Ollama,
and operator-configured gateways). Cloud Temple is the historical regression
shape: the wire calls keep the exact historical paths and bodies
(``POST /chat/completions`` with ``max_tokens``, ``POST /embeddings``,
``GET /models``).

The OpenAI SDK is deliberately NOT used. Every operation is issued over the
owned ``httpx`` transport, because the SDK owns request construction and
response reading in ways this boundary cannot accept:

- it silently injects ``encoding_format="base64"`` when the argument is
  omitted, and decodes it in a ``post_parser``, so validating raw wire types
  is impossible through it while omitting the field is impossible for a
  provider that rejects it;
- its typed models coerce values (a JSON ``true`` becomes ``1.0``) before any
  validation could observe the real wire type;
- it buffers the complete response before returning, so an oversized or
  compressed body could exhaust memory ahead of any check.

Reading responses ourselves under a request-aware byte ceiling resolves all
three, and removes the SDK's implicit retry entirely rather than merely
disabling it. The Hivemind-owned bounded retry loop
(:func:`hivemind_inference.retry.run_with_bounded_retry`) is the only retry
authority; this adapter never retries on its own.

The adapter always injects an owned transport (proxied when ``PROXY_URL`` is
configured, explicit direct with ``trust_env=False`` otherwise) so ambient
``HTTP(S)_PROXY`` variables are never honored on any path.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import struct
import time
from types import MappingProxyType
from typing import Mapping

from ..egress import build_owned_async_http_client, close_owned_client_from_sync
from ..errors import InferenceError
from ..certification_budget import (
    CertificationBudgetError,
    certification_retry_policy,
    complete_budget_reservation,
    protected_certification_context_active,
    reserve_request_from_environment,
)
from ..profiles import ResolvedChatProfile, ResolvedEmbeddingProfile
from ..records import (
    ChatRequest,
    ChatResult,
    EmbeddingRequest,
    EmbeddingResult,
    ProbeResult,
    _is_finite_number,
)
from ..retry import AttemptFailure, parse_retry_after_seconds, run_with_bounded_retry
from .common import (
    IDENTITY_ENCODING_HEADERS,
    ResponseEncodingRefused,
    ResponseTooLarge,
    classify_httpx_transport,
    log_provider_failure,
    chat_response_ceiling,
    chat_response_is_serviceable,
    embedding_response_ceiling,
    embedding_response_is_serviceable,
    make_error,
    read_bounded_body,
    safe_json_body as _safe_json,
    safe_json_body_with_status as _safe_json_with_status,
    safe_resolved_model,
    safe_token_count,
)

# Default owned-transport timeout; every real call overrides it with the
# request's remaining budget, so this only bounds traffic no call-site timed.
_CLIENT_DEFAULT_TIMEOUT_SECONDS = 60.0

_logger = logging.getLogger("hivemind_inference.adapters")
_REPORTED_MODEL_DIAGNOSTICS = frozenset(
    {"absent", "invalid", "nonexact", "exact"}
)

# Statuses that mean "the endpoint answered but does not offer this
# operation" for the discovery probe.
_DISCOVERY_UNSUPPORTED_STATUSES = (404, 405, 501)


def _reported_model_diagnostic(
    response: Mapping[str, object], configured_model: str
) -> str:
    if "model" not in response:
        return "absent"
    value = response["model"]
    if not isinstance(value, str):
        return "invalid"
    if value == configured_model:
        return "exact"
    return "nonexact"


def _log_protected_model_diagnostic(
    *, provider_id: str, correlation_id: str, diagnostic: object
) -> None:
    safe_diagnostic = (
        diagnostic
        if isinstance(diagnostic, str)
        and diagnostic in _REPORTED_MODEL_DIAGNOSTICS
        else "-"
    )
    _logger.warning(
        "protected certification chat model confirmation: provider=%s "
        "correlation_id=%s reported-model=%s",
        provider_id,
        correlation_id,
        safe_diagnostic,
    )


def _listed_model_matches(
    *, provider_id: str, configured_model: str, listed_model: object
) -> bool:
    """Match one discovery id without changing the configured request model.

    OpenAI-compatible listings normally return a bare model id. Google's
    public documentation separately exposes the OpenAI-compatible list route
    and a native ``models/{model}`` resource shape without publishing the raw
    compatibility-list payload. For the explicit Gemini identity only, accept
    those two exact representations. Aliases, suffix matches, other prefixes,
    and every non-string value remain unavailable rather than triggering model
    substitution or request mutation.
    """
    if not isinstance(listed_model, str):
        return False
    if listed_model == configured_model:
        return True
    return provider_id == "gemini" and listed_model == f"models/{configured_model}"


def _body_error_codes(body: object) -> list[str]:
    """Closed machine-readable top-level and nested code/type candidates.

    OpenAI-compatible providers commonly use top-level or nested ``code`` and
    ``type``. Structured Google reasons are kept separate because they may
    authorize a retry only for the explicit ``gemini`` provider identity.
    Provider messages and arbitrary metadata are never inspected or copied.
    """
    if not isinstance(body, dict):
        return []
    error = body.get("error")
    candidates = [body.get("code"), body.get("type")]
    if isinstance(error, dict):
        candidates.extend([error.get("code"), error.get("type")])
    return [c for c in candidates if isinstance(c, str)]


def _structured_error_reasons(body: object) -> list[str]:
    """Return only stable ``details[*].reason`` values.

    Google's REST ``ErrorInfo`` shape carries its stable reason here. The
    legacy ``errors`` container is deliberately excluded: accepting an
    undocumented value from it would widen the retry-authorizing surface for
    every generic OpenAI-compatible endpoint.
    """
    if not isinstance(body, dict):
        return []
    error = body.get("error")
    reasons: list[str] = []
    for container in (body, error):
        if not isinstance(container, dict):
            continue
        details = container.get("details")
        if isinstance(details, list):
            reasons.extend(
                item.get("reason") for item in details if isinstance(item, dict)
            )
    return [reason for reason in reasons if isinstance(reason, str)]


def _contains_google_quota_failure(body: object) -> bool:
    """Recognize Google's typed quota-failure envelope without reading ids.

    Any ``google.rpc.QuotaFailure`` is terminal. This deliberately chooses the
    safe availability tradeoff: over-matching a transient quota violation can
    suppress one bounded retry, while under-matching can duplicate a paid
    request. Quota ids, metrics, units, metadata, messages, and violations are
    never inspected, copied, logged, or returned.
    """
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    for container in (body, error):
        if not isinstance(container, dict):
            continue
        details = container.get("details")
        if not isinstance(details, list):
            continue
        for detail in details:
            if isinstance(detail, dict) and detail.get("@type") == (
                "type.googleapis.com/google.rpc.QuotaFailure"
            ):
                return True
    return False


def _quota_exhaustion_code(body: object) -> bool:
    """True for a machine-readable quota/credit exhaustion code.

    The conservative ``insufficient_quota`` substring guard is deliberately
    broader than the retry-authorizing allowlist: over-matching only makes a
    transient shape terminal, while under-matching can issue a duplicate paid
    request. Exact terminal codes and closed daily/spend/billing quota-scope
    markers extend that guard for Google. A bare Google
    ``RESOURCE_EXHAUSTED`` remains ambiguous and non-retryable.
    """
    codes = {
        code.strip().lower()
        for code in (*_body_error_codes(body), *_structured_error_reasons(body))
    }
    exact_quota_codes = {
        "billing_hard_limit_reached",
        "credits_exhausted",
        "insufficient_quota",
        "quota_exceeded",
        "quota_exhausted",
    }
    if codes & exact_quota_codes or any(
        "insufficient_quota" in code for code in codes
    ):
        return True
    return _contains_google_quota_failure(body)


# A 429 is retryable only on the CONJOINT proof of a machine-readable
# transient rate/concurrency code AND a valid bounded Retry-After (ADR-0027).
# A bodyless 429, an unknown code, or an ambiguous quota/policy 429 stays
# non-retryable — never a duplicate paid request on an ambiguous response.
_TRANSIENT_RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "rate_limit_error",
        "rate_limited",
        "concurrency_limit_exceeded",
    }
)


def _transient_rate_limit_code(body: object, *, provider_id: str) -> bool:
    """True only for a provider-scoped, allowlisted transient code.

    Existing code/type candidates retain the historical exact, case-sensitive
    match for every provider. Gemini alone additionally accepts Google's exact
    structured ``RATE_LIMIT_EXCEEDED`` reason. No generic endpoint can gain a
    retry from case-folding or a nested reason added for Google.
    """
    if any(
        code.strip() in _TRANSIENT_RATE_LIMIT_CODES
        for code in _body_error_codes(body)
    ):
        return True
    return provider_id == "gemini" and any(
        reason.strip() == "RATE_LIMIT_EXCEEDED"
        for reason in _structured_error_reasons(body)
    )


# ADR-0027 requires the adapter to map the normalized output limit to "the
# exact supported Chat Completions field" for the frozen `openai-reference`
# profile, whose configured model is a reasoning model. Current OpenAI Chat
# Completions defines ``max_completion_tokens`` as the reasoning-inclusive
# bound and deprecates ``max_tokens``; a reasoning model given the legacy field
# can reject the request or fail to enforce the intended total budget. The
# model slug itself stays out of this tree — it is private routing metadata,
# and the mapping keys on provider identity anyway. Other OpenAI-compatible
# servers (Cloud Temple, Gemini, Scaleway, Mistral, OVHcloud, Ollama, and
# operator gateways) still take ``max_tokens``, so the field is selected per
# provider identity rather than assumed uniform — exactly the per-profile
# mapping the ADR asks for, and never inferred from a URL or model name.
_OUTPUT_LIMIT_FIELDS: Mapping[str, str] = MappingProxyType(
    {"openai": "max_completion_tokens"}
)
_DEFAULT_OUTPUT_LIMIT_FIELD = "max_tokens"


def output_limit_field(provider_id: str) -> str:
    """The Chat Completions output-budget field for this provider profile."""
    return _OUTPUT_LIMIT_FIELDS.get(provider_id, _DEFAULT_OUTPUT_LIMIT_FIELD)


def _classify_openai_status(
    status: int, body: object, retry_after: str | None, *, provider_id: str
) -> tuple[str, bool, float | None]:
    """Map an OpenAI-compatible error status to ``(category, retryable, delay)``.

    The SINGLE status-mapping authority, shared by the chat, embedding, and
    discovery paths, so the frozen ADR-0027 429 semantics (quota beats
    transient; a retry needs a machine-readable transient code AND a bounded
    ``Retry-After``) cannot drift between operations. ``body`` may be either a
    provider error object or a full raw response body — ``_body_error_codes``
    reads codes from both shapes.
    """
    if status in (401, 403):
        return "auth", False, None
    if status == 429:
        if _quota_exhaustion_code(body):
            return "quota_exhausted", False, None
        delay = parse_retry_after_seconds(retry_after)
        if delay is None or not _transient_rate_limit_code(
            body, provider_id=provider_id
        ):
            return "rate_limited", False, None
        return "rate_limited", True, delay
    if 400 <= status < 500:
        return "invalid_request", False, None
    return "unavailable", False, None


def _decode_embedding_vector(raw: object) -> list | None:
    """Normalize a wire embedding to a list of numbers, or ``None`` if unusable.

    Accepts the two shapes a compliant OpenAI-compatible provider may return:
    a JSON array of numbers, or a base64-packed little-endian ``float32``
    buffer. The base64 form is DECODED rather than rejected so a provider that
    returns it — with or without being asked — still works; the decoded values
    then face exactly the same dimension and finiteness validation as an array.
    Returns ``None`` for anything else, including a malformed or truncated
    base64 payload, so the caller fails closed as ``invalid_response``.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            packed = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(packed) % 4:  # not a whole number of float32 values
            return None
        try:
            # struct with an EXPLICIT "<" is required, not cosmetic:
            # array.array("f", ...) interprets the buffer in HOST byte order,
            # so on a big-endian host every value would be silently
            # byte-swapped. Byte-swapped float32s are overwhelmingly still
            # finite and still the right count, so they would pass every
            # downstream dimension and finiteness check and be stored as
            # corrupted embeddings. The wire format is little-endian, so it is
            # decoded as little-endian on every architecture.
            return [value for (value,) in struct.iter_unpack("<f", packed)]
        except struct.error:
            return None
    return None


class _OpenAICompatibleBase:
    """Owned client construction/lifecycle shared by the three surfaces."""

    def __init__(
        self,
        profile: ResolvedChatProfile | ResolvedEmbeddingProfile,
        *,
        proxy_url: str | None = None,
    ) -> None:
        self._profile = profile
        self._proxy_configured = bool(proxy_url)
        self._owned_http_client = build_owned_async_http_client(
            proxy_url, timeout=_CLIENT_DEFAULT_TIMEOUT_SECONDS
        )

    async def aclose(self) -> None:
        """Close the owned transport (idempotent)."""
        await self._owned_http_client.aclose()

    # -- shared failure mapping ---------------------------------------------

    def _classify_exception(self, exc: Exception) -> tuple[str, bool, float | None]:
        """Map one transport failure to (category, retryable, delay).

        Only transport exceptions reach here now: HTTP status handling lives
        in :func:`_classify_openai_status`, called directly from each
        operation once the status is known.
        """
        import httpx

        if isinstance(exc, httpx.HTTPError):
            category, retryable = classify_httpx_transport(
                exc, proxy_configured=self._proxy_configured
            )
            return category, retryable, 0.0 if retryable else None
        return "unavailable", False, None

    def _attempt_failure(
        self, exc: Exception, *, role: str, correlation_id: str
    ) -> AttemptFailure:
        category, retryable, delay = self._classify_exception(exc)
        status_code = None
        log_provider_failure(
            role=role,
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            category=category,
            correlation_id=correlation_id,
            exc=exc,
            status_code=status_code,
        )
        error = make_error(
            category=category,
            role=role,
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            retryable=retryable,
            correlation_id=correlation_id,
        )
        return AttemptFailure(error, delay if retryable else None)

    # -- shared raw request -------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._profile.api_key}",
            "content-type": "application/json",
            "accept": "application/json",
            **IDENTITY_ENCODING_HEADERS,
        }

    def _api_url(self, path: str) -> str:
        return self._profile.endpoint.rstrip("/") + path

    async def _request(
        self,
        method,
        path,
        *,
        timeout,
        ceiling,
        json_body=None,
        output_token_reservation: int = 0,
    ):
        """Issue one bounded request; return (status, headers, raw body)."""
        reservation = reserve_request_from_environment(
            role=self._profile.role,
            provider_id=self._profile.provider_id,
            configured_model=self._profile.configured_model,
            method=method,
            path=path,
            json_body=json_body,
            output_token_reservation=output_token_reservation,
        )
        reported_input_tokens: int | None = None
        reported_output_tokens: int | None = None
        try:
            async with self._owned_http_client.stream(
                method,
                self._api_url(path),
                headers=self._auth_headers(),
                json=json_body,
                timeout=timeout,
            ) as response:
                raw_body = await read_bounded_body(response, ceiling)
                if response.status_code == 200:
                    payload = _safe_json(raw_body)
                    usage = payload.get("usage") if isinstance(payload, dict) else None
                    if isinstance(usage, dict):
                        reported_input_tokens = safe_token_count(
                            usage.get("prompt_tokens")
                        )
                        if self._profile.role == "chat":
                            reported_output_tokens = safe_token_count(
                                usage.get("completion_tokens")
                            )
                return response.status_code, response.headers, raw_body
        finally:
            complete_budget_reservation(
                reservation,
                reported_input_tokens=reported_input_tokens,
                reported_output_tokens=reported_output_tokens,
            )

    def _attempt_failure_from_category(
        self,
        category: str,
        *,
        role: str,
        correlation_id: str,
        diagnostic: str | None = None,
    ) -> AttemptFailure:
        """A terminal, non-retryable attempt failure for a locally-detected
        condition (no provider exception to classify) — e.g. an oversized
        response aborted mid-read."""
        return AttemptFailure(
            self._direct_error(
                category,
                role=role,
                correlation_id=correlation_id,
                diagnostic=diagnostic,
            ),
            None,
        )

    def _direct_error(
        self,
        category: str,
        *,
        role: str,
        correlation_id: str,
        diagnostic: str | None = None,
        status_code: int | None = None,
    ) -> InferenceError:
        log_provider_failure(
            role=role,
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            category=category,
            correlation_id=correlation_id,
            diagnostic=diagnostic,
            status_code=status_code,
        )
        return make_error(
            category=category,
            role=role,
            provider_id=self._profile.provider_id,
            adapter_id=self._profile.adapter_id,
            retryable=False,
            correlation_id=correlation_id,
        )


class OpenAICompatibleChatProvider(_OpenAICompatibleBase):
    """``ChatProvider`` implementation over ``chat.completions.create``."""

    def __init__(self, profile: ResolvedChatProfile, *, proxy_url: str | None = None):
        if profile.role != "chat":
            raise ValueError("OpenAICompatibleChatProvider requires a chat profile")
        super().__init__(profile, proxy_url=proxy_url)

    @property
    def profile(self) -> ResolvedChatProfile:
        return self._profile  # type: ignore[return-value]

    async def complete(self, request: ChatRequest) -> ChatResult:
        profile: ResolvedChatProfile = self._profile  # type: ignore[assignment]
        effective_max = profile.max_output_tokens
        if request.max_output_tokens is not None:
            if request.max_output_tokens > profile.max_output_tokens:
                raise self._direct_error(
                    "invalid_request",
                    role="chat",
                    correlation_id=request.correlation_id,
                )
            effective_max = request.max_output_tokens

        body: dict = {
            "model": profile.configured_model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            output_limit_field(profile.provider_id): effective_max,
        }
        if profile.temperature is not None:
            body["temperature"] = profile.temperature
        if not chat_response_is_serviceable(effective_max):
            raise self._direct_error(
                "invalid_request",
                role="chat",
                correlation_id=request.correlation_id,
            )
        # Keep small visible-output responses request-aware. Reasoning-inclusive
        # budgets do not predict serialized bytes, so larger requests saturate
        # at the independent 8 MiB raw-body safety boundary.
        ceiling = chat_response_ceiling(effective_max)

        async def attempt(remaining_seconds: float):
            try:
                status_code, headers, raw_body = await self._request(
                    "POST",
                    "/chat/completions",
                    timeout=remaining_seconds,
                    ceiling=ceiling,
                    json_body=body,
                    output_token_reservation=effective_max,
                )
            except ResponseEncodingRefused:
                raise self._attempt_failure_from_category(
                    "invalid_response",
                    role="chat",
                    correlation_id=request.correlation_id,
                    diagnostic="encoding_refused",
                ) from None
            except ResponseTooLarge:
                raise self._attempt_failure_from_category(
                    "invalid_response",
                    role="chat",
                    correlation_id=request.correlation_id,
                    diagnostic="response_too_large",
                ) from None
            except CertificationBudgetError:
                raise
            except Exception as exc:
                raise self._attempt_failure(
                    exc, role="chat", correlation_id=request.correlation_id
                ) from None
            if status_code != 200:
                payload = _safe_json(raw_body)
                category, retryable, delay = _classify_openai_status(
                    status_code,
                    payload,
                    headers.get("retry-after"),
                    provider_id=profile.provider_id,
                )
                log_provider_failure(
                    role="chat",
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    category=category,
                    correlation_id=request.correlation_id,
                    status_code=status_code,
                )
                raise AttemptFailure(
                    make_error(
                        category=category,
                        role="chat",
                        provider_id=profile.provider_id,
                        adapter_id=profile.adapter_id,
                        retryable=retryable,
                        correlation_id=request.correlation_id,
                    ),
                    delay,
                )
            return self._normalize_chat_response(raw_body, request)

        return await run_with_bounded_retry(
            attempt,
            timeout_seconds=request.timeout_seconds,
            role="chat",
            provider_id=profile.provider_id,
            adapter_id=profile.adapter_id,
            correlation_id=request.correlation_id,
            retry_policy=certification_retry_policy(request.retry_policy),
        )

    def _normalize_chat_response(self, raw_body: bytes, request: ChatRequest) -> ChatResult:
        profile: ResolvedChatProfile = self._profile  # type: ignore[assignment]

        def _invalid(diagnostic: str) -> InferenceError:
            return self._direct_error(
                "invalid_response",
                role="chat",
                correlation_id=request.correlation_id,
                diagnostic=diagnostic,
                status_code=200,
            )

        parsed, data = _safe_json_with_status(raw_body)
        if not parsed:
            raise _invalid("invalid_json")
        if not isinstance(data, dict):
            raise _invalid("invalid_root")
        choices = data.get("choices")
        # Container types are checked BEFORE traversal: a valid JSON scalar
        # parses fine but is not indexable, and would otherwise escape as a
        # raw TypeError past the envelope.
        if not isinstance(choices, list) or not choices:
            raise _invalid("invalid_choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise _invalid("invalid_choices")
        message = first.get("message")
        if not isinstance(message, dict):
            raise _invalid("invalid_message")
        refusal = message.get("refusal")
        finish_reason = first.get("finish_reason")
        text = message.get("content")
        if (isinstance(refusal, str) and refusal) or finish_reason == "content_filter":
            # Refusal content is a content_rejected outcome — never a
            # successful empty completion.
            raise self._direct_error(
                "content_rejected", role="chat", correlation_id=request.correlation_id
            )
        if finish_reason == "stop":
            normalized_finish = "stop"
        elif finish_reason == "length":
            normalized_finish = "length"
        else:
            normalized_finish = "other"
        reported_model_diagnostic = _reported_model_diagnostic(
            data, profile.configured_model
        )
        resolved_model = safe_resolved_model(
            data.get("model"), profile.configured_model
        )
        if protected_certification_context_active():
            _log_protected_model_diagnostic(
                provider_id=profile.provider_id,
                correlation_id=request.correlation_id,
                diagnostic=reported_model_diagnostic,
            )
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # A non-string ``content`` (null, list, object) is NOT an empty
        # completion. Coercing it to "" would present provider-controlled
        # malformed — or unsupported, e.g. tool-call — output as a successful
        # empty answer. Tool calling is explicitly out of scope for this lot,
        # so any shape other than a string fails closed. A genuinely empty
        # string stays a valid successful completion.
        if not isinstance(text, str):
            raise _invalid("invalid_content")
        return ChatResult(
            text=text,
            configured_model=profile.configured_model,
            model_evidence=(
                "provider_reported" if resolved_model is not None else "configured_only"
            ),
            finish_reason=normalized_finish,
            resolved_model=resolved_model,
            input_tokens=safe_token_count(usage.get("prompt_tokens")),
            output_tokens=safe_token_count(usage.get("completion_tokens")),
            total_tokens=safe_token_count(usage.get("total_tokens")),
            correlation_id=request.correlation_id,
        )


class OpenAICompatibleEmbeddingProvider(_OpenAICompatibleBase):
    """``EmbeddingProvider`` over a RAW ``POST {endpoint}/embeddings``.

    This role deliberately does NOT use the OpenAI SDK's ``embeddings.create``
    (P13-1B, review round 3). Three requirements are jointly unsatisfiable
    while the SDK owns request construction:

    1. components must be validated from the RAW wire JSON, because the SDK's
       typed model coerces a JSON ``true`` into ``1.0`` before any
       ``isinstance(x, bool)`` check could observe it;
    2. the SDK INJECTS ``encoding_format="base64"`` whenever the argument is
       omitted, and decodes it in a ``post_parser`` that ``with_raw_response``
       bypasses — so raw validation alone sees an undecodable string;
    3. a provider whose API documents ``encoding_format`` as unsupported
       (Scaleway) must not receive the field at all, so it cannot simply be
       pinned for everyone either.

    Issuing the request over the owned transport resolves all three: we send
    exactly ``{"model", "input"}`` with no encoding field, read the response
    bytes ourselves, and accept EITHER a float array or a base64 buffer
    (decoded, then validated identically). Chat and discovery were moved off
    the SDK too, so the whole module now speaks one transport.

    The standard OpenAI-compatible wire format has no distinct document/query
    parameter: the request's ``input_type`` is preserved semantically and the
    symmetric wire call is identical for both (declared profile behavior).
    ``expected_dimensions`` is validation-only — no wire ``dimensions``
    parameter is ever sent.
    """

    def __init__(
        self, profile: ResolvedEmbeddingProfile, *, proxy_url: str | None = None
    ):
        if profile.role != "embedding":
            raise ValueError(
                "OpenAICompatibleEmbeddingProvider requires an embedding profile"
            )
        super().__init__(profile, proxy_url=proxy_url)

    @property
    def profile(self) -> ResolvedEmbeddingProfile:
        return self._profile  # type: ignore[return-value]

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        profile: ResolvedEmbeddingProfile = self._profile  # type: ignore[assignment]
        # Exactly the documented wire body MINUS encoding_format (see class
        # docstring). No dimensions field: expected_dimensions is validation
        # metadata, never an implicit request parameter (ADR-0027).
        body = {"model": profile.configured_model, "input": list(request.inputs)}

        # Refuse BEFORE any paid call when the request's own shape implies a
        # response larger than this boundary will ever read: the caller gets an
        # actionable invalid_request instead of a mid-read invalid_response,
        # and no provider request is issued at all.
        if not embedding_response_is_serviceable(
            len(request.inputs), profile.expected_dimensions
        ):
            raise self._direct_error(
                "invalid_request",
                role="embedding",
                correlation_id=request.correlation_id,
            )
        # The response can be no larger than the vectors we asked for; a fixed
        # ceiling would let a hostile body sit just under it and still amplify
        # through JSON/base64 decoding.
        ceiling = embedding_response_ceiling(
            len(request.inputs), profile.expected_dimensions
        )

        async def attempt(remaining_seconds: float):
            try:
                status_code, headers, raw_body = await self._request(
                    "POST",
                    "/embeddings",
                    timeout=remaining_seconds,
                    ceiling=ceiling,
                    json_body=body,
                )
            except (ResponseTooLarge, ResponseEncodingRefused):
                raise self._attempt_failure_from_category(
                    "invalid_response",
                    role="embedding",
                    correlation_id=request.correlation_id,
                ) from None
            except CertificationBudgetError:
                raise
            except Exception as exc:
                raise self._attempt_failure(
                    exc, role="embedding", correlation_id=request.correlation_id
                ) from None
            if status_code != 200:
                payload = _safe_json(raw_body)
                category, retryable, delay = _classify_openai_status(
                    status_code,
                    payload,
                    headers.get("retry-after"),
                    provider_id=profile.provider_id,
                )
                log_provider_failure(
                    role="embedding",
                    provider_id=profile.provider_id,
                    adapter_id=profile.adapter_id,
                    category=category,
                    correlation_id=request.correlation_id,
                    status_code=status_code,
                )
                raise AttemptFailure(
                    make_error(
                        category=category,
                        role="embedding",
                        provider_id=profile.provider_id,
                        adapter_id=profile.adapter_id,
                        retryable=retryable,
                        correlation_id=request.correlation_id,
                    ),
                    delay,
                )
            return self._normalize_embedding_response(raw_body, request)

        return await run_with_bounded_retry(
            attempt,
            timeout_seconds=request.timeout_seconds,
            role="embedding",
            provider_id=profile.provider_id,
            adapter_id=profile.adapter_id,
            correlation_id=request.correlation_id,
            retry_policy=certification_retry_policy(request.retry_policy),
        )

    def _normalize_embedding_response(
        self, raw_body: bytes, request: EmbeddingRequest
    ) -> EmbeddingResult:
        profile: ResolvedEmbeddingProfile = self._profile  # type: ignore[assignment]

        def _invalid() -> InferenceError:
            return self._direct_error(
                "invalid_response",
                role="embedding",
                correlation_id=request.correlation_id,
            )

        data = _safe_json(raw_body)
        if not isinstance(data, dict):
            raise _invalid()
        items = data.get("data")
        if not isinstance(items, list):
            raise _invalid()
        if len(items) != len(request.inputs):
            raise _invalid()
        if not all(isinstance(item, dict) for item in items):
            raise _invalid()
        # EVERY index must be a non-bool int and the set must form EXACTLY the
        # permutation 0..N-1 — a missing, partial, duplicate, or boolean index
        # is invalid (never "the provider's order" by default, which would
        # allow silent chunk/vector misalignment).
        indexes = [item.get("index") for item in items]
        if not all(
            isinstance(index, int) and not isinstance(index, bool)
            for index in indexes
        ):
            raise _invalid()
        if sorted(indexes) != list(range(len(items))):
            raise _invalid()
        items = sorted(items, key=lambda item: item["index"])
        vectors: list[tuple[float, ...]] = []
        for item in items:
            # Accept a float array OR a base64 float32 buffer; the decoded
            # values then face exactly the same dimension and finiteness
            # checks, so tolerating the encoding never weakens validation.
            embedding = _decode_embedding_vector(item.get("embedding"))
            if embedding is None or len(embedding) != profile.expected_dimensions:
                raise _invalid()
            values: list[float] = []
            for component in embedding:
                if isinstance(component, bool) or not isinstance(
                    component, (int, float)
                ):
                    raise _invalid()
                # _is_finite_number is the package's single TOTAL finite-number
                # primitive (P13-1A): it absorbs the OverflowError that plain
                # ``float()``/``math.isfinite()`` raise on an out-of-range JSON
                # integer such as 10**400, so a hostile numeric component fails
                # closed as invalid_response instead of escaping the envelope.
                # Check BEFORE converting — float(10**400) itself overflows.
                if not _is_finite_number(component):
                    raise _invalid()
                values.append(float(component))
            vectors.append(tuple(values))
        resolved_model = safe_resolved_model(
            data.get("model"), profile.configured_model
        )
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return EmbeddingResult(
            vectors=tuple(vectors),
            configured_model=profile.configured_model,
            model_evidence=(
                "provider_reported" if resolved_model is not None else "configured_only"
            ),
            effective_dimensions=profile.expected_dimensions,
            resolved_model=resolved_model,
            input_tokens=safe_token_count(usage.get("prompt_tokens")),
            total_tokens=safe_token_count(usage.get("total_tokens")),
            correlation_id=request.correlation_id,
        )


class OpenAICompatibleProbe(_OpenAICompatibleBase):
    """``ProviderProbe`` over ``models.list`` — discovery only, zero retries.

    An unavailable model-list operation (404/405/501) yields
    ``discovery="unsupported"`` with ``connectivity="reachable"`` — never
    provider unavailability. A successful listing proves only
    discovery/connectivity, never paid-inference readiness.
    """

    async def probe(self, *, timeout_seconds: float = 5.0) -> ProbeResult:
        # asyncio.timeout gives the probe a TOTAL wall-clock deadline. The
        # httpx timeout alone bounds only inactivity BETWEEN chunks, so a
        # provider dripping one byte per interval could hold public discovery
        # open indefinitely. Chat and embedding inherit this bound from the
        # retry loop; probes never enter it, so they need their own. Caller
        # cancellation still propagates — only expiry of THIS deadline becomes
        # a normalized result.
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_seconds):
                status_code, _headers, raw_body = await self._request(
                    "GET",
                    "/models",
                    timeout=timeout_seconds,
                    ceiling=None,  # no request-derived size; use the backstop
                )
        except TimeoutError:
            log_provider_failure(
                role=self._profile.role,
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
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                error_category="invalid_response",
            )
        except CertificationBudgetError:
            raise
        except Exception as exc:
            category, _, _ = self._classify_exception(exc)
            log_provider_failure(
                role=self._profile.role,
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
            category, _, _ = _classify_openai_status(
                status_code,
                _safe_json(raw_body),
                None,
                provider_id=self._profile.provider_id,
            )
            log_provider_failure(
                role=self._profile.role,
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
        data = _safe_json(raw_body)
        listing = data.get("data") if isinstance(data, dict) else None
        # Container type checked before traversal: {"data": 123} parses fine
        # but is not iterable and would escape as a raw TypeError.
        if not isinstance(listing, list):
            return ProbeResult(
                connectivity="reachable",
                discovery="error",
                model_available=None,
                latency_ms=latency,
                error_category="invalid_response",
            )
        model_ids = [item.get("id") for item in listing if isinstance(item, dict)]
        return ProbeResult(
            connectivity="reachable",
            discovery="available",
            model_available=any(
                _listed_model_matches(
                    provider_id=self._profile.provider_id,
                    configured_model=self._profile.configured_model,
                    listed_model=model_id,
                )
                for model_id in model_ids
            ),
            latency_ms=latency,
        )
