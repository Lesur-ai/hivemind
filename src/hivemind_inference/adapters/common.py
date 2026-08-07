# -*- coding: utf-8 -*-
"""Shared adapter plumbing: transport classification and safe logging.

The classification encodes the frozen ADR-0027 retry table:

- a connection-establishment failure (``httpx.ConnectError``,
  ``httpx.ConnectTimeout``) proves no request bytes were sent → category
  ``unavailable`` and eligible for the single bounded retry, but ONLY when no
  proxy is configured. When a proxy is configured, the identical exception is
  also raised for an unreachable proxy or a failed proxy TLS handshake, which
  ADR-0027 requires to fail closed with no retry and no direct-connection
  fallback; since httpx/httpcore raise the same exception type for both the
  proxy hop and a direct connection, the caller's proxy configuration is the
  only signal available to tell them apart;
- a proxy failure fails closed and is never retried (and never falls back to
  a direct connection);
- read/write/pool timeouts and protocol errors after transmission are
  ambiguous-delivery outcomes → never retried.

Server-side logs record only structured safe fields (exception class, HTTP
status, category, correlation id, and a closed local diagnostic token) — never
provider payloads, prompts, or endpoint URLs.
"""

from __future__ import annotations

import json
import logging

from ..errors import InferenceError
from ..profiles import MAX_CHAT_GENERATION_TOKENS
from ..records import _is_bounded_int

_logger = logging.getLogger("hivemind_inference.adapters")

_SAFE_PROVIDER_FAILURE_DIAGNOSTICS = frozenset(
    {
        "encoding_refused",
        "response_too_large",
        "invalid_json",
        "invalid_root",
        "invalid_choices",
        "invalid_message",
        "invalid_content",
    }
)


_JSON_PARSE_FAILURES = (
    json.JSONDecodeError,
    ValueError,
    TypeError,
    AttributeError,
    UnicodeDecodeError,
    # A provider-controlled body nested deeply enough exhausts the parser's
    # recursion budget (reachable at ~100k depth). That is a malformed
    # provider response, not an internal failure entitled to escape the
    # frozen error envelope as a raw exception.
    RecursionError,
)


def safe_json_body_with_status(response: object) -> tuple[bool, object | None]:
    """Parse a provider response body and retain whether parsing succeeded.

    A valid JSON ``null`` and an invalid JSON document both normalize to
    ``None`` through :func:`safe_json_body`. Chat response diagnostics need to
    distinguish those two local control-flow branches without introducing a
    second parser or inspecting/logging provider values, so this companion
    returns ``(parsed, body)`` from the same guarded entry point.
    """
    payload = response if isinstance(response, (bytes, bytearray)) else getattr(
        response, "content", None
    )
    try:
        return True, json.loads(payload)
    except _JSON_PARSE_FAILURES:
        return False, None


def safe_json_body(response: object) -> object | None:
    """Parse a provider response body, or return ``None`` if it is unusable.

    This and :func:`safe_json_body_with_status` share the same guarded JSON
    entry point for every adapter boundary — success, error, and probe alike —
    so no path can be individually forgotten (exactly the gap that let a raw
    ``RecursionError`` escape the Anthropic parser and probe). Callers treat
    ``None`` as ``invalid_response``; a caller that needs a specific container
    type must still check it, since a valid JSON scalar such as ``123`` parses
    successfully but is not traversable.

    Accepts either an object exposing ``.content`` or raw ``bytes``, so a
    bounded read (:func:`read_bounded_body`) can hand its already-capped
    bytes straight in.
    """
    parsed, body = safe_json_body_with_status(response)
    return body if parsed else None


# Last-resort ceiling for an operation whose legitimate response size cannot
# be derived from the request (currently only model discovery). Operations
# that CAN derive one use :func:`response_ceiling_bytes` instead — a fixed
# ceiling far above the real shape is a weak bound, because a hostile response
# just under it still buffers and amplifies through JSON/base64 decoding.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Generous per-unit byte allowances for deriving a request-aware ceiling.
# Deliberately loose: a ceiling that is too tight rejects a legitimate
# provider response, which is its own availability failure, so these sit well
# above ordinary encodings while staying orders of magnitude below the
# amplification a hostile body could otherwise achieve. For chat this models
# VISIBLE serialized output only; it must not be used to decide whether a
# reasoning-inclusive generation budget is serviceable, because hidden
# reasoning tokens need not appear in the response JSON.
_BYTES_PER_EMBEDDING_COMPONENT = 26  # a JSON float plus separator, worst case
_BYTES_PER_OUTPUT_TOKEN = 256        # loose visible JSON output allowance
_ENVELOPE_OVERHEAD_BYTES = 256 * 1024  # ids, model names, usage, whitespace


# DECLARED BATCH CONTRACT.
#
# ADR-0027 specifies no maximum embedding batch, and providers differ (OpenAI
# documents up to 2048 inputs). Leaving it implicit forced an impossible
# trade-off: a ceiling loose enough for a provider's documented maximum could
# not also bound the memory a response consumes, while a ceiling tight enough
# to bound memory silently refused provider-valid requests. Hivemind therefore
# states the limit rather than deriving one by accident:
#
#   this boundary embeds at most MAX_EMBEDDING_INPUTS inputs per request.
#
# Splitting a larger workload belongs to the consumer, which knows the corpus;
# an over-limit request is refused before any paid call with a specific,
# actionable error rather than truncated or silently accepted.
MAX_EMBEDDING_INPUTS = 64

# DECLARED CHAT GENERATION CONTRACT.
#
# ``max_output_tokens`` is a provider generation budget, not a declaration of
# serialized response bytes. In particular, reasoning models charge hidden
# reasoning against this budget without necessarily returning those tokens in
# the response JSON. Deriving serviceability from
# ``max_output_tokens * _BYTES_PER_OUTPUT_TOKEN`` therefore created an
# accidental 31,744-token limit at the 8 MiB response clamp.
#
# Hivemind accepts generation budgets through the profile-owned contract. The
# separate streamed raw-body ceiling below remains authoritative for memory
# safety; a physical response above it fails closed while being read.

# Hard process-level ceiling that NO derived limit may exceed.
#
# A request-derived limit alone is not a bound: this helper is a public
# enforcement primitive and can receive extreme caller-provided values, while
# embedding requests also carry independently variable cardinality and
# dimensions. A derived value can therefore become enormous; this constant
# restores the process-level bound even if an upstream validator is bypassed.
#
# 8 MiB is sized from the embedding response contract: at 64 inputs the
# largest frozen reference profile (4096-dimension Scaleway) derives ~6.75 MiB.
# It also accommodates ordinary chat response bodies, but it is deliberately
# independent of the reasoning-inclusive generation contract above. Keeping it
# this small bounds what json.loads can allocate from a permitted body: parsing
# amplifies a dense numeric array by roughly 10-20x, so the peak parsed
# representation stays in the low hundreds of MiB rather than the multiple GiB
# a 64 MiB raw cap would have allowed. A tighter guarantee on PEAK PARSED memory
# needs incremental, schema-aware parsing; that remains residual risk rather
# than claimed as solved.
ABSOLUTE_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _derived_embedding_bytes(input_count: int, dimensions: int) -> int:
    return input_count * dimensions * _BYTES_PER_EMBEDDING_COMPONENT + _ENVELOPE_OVERHEAD_BYTES


def _derived_chat_bytes(max_output_tokens: int) -> int:
    return max_output_tokens * _BYTES_PER_OUTPUT_TOKEN + _ENVELOPE_OVERHEAD_BYTES


def embedding_response_ceiling(input_count: int, dimensions: int) -> int:
    """Largest plausible embeddings response for THIS request, hard-clamped."""
    return min(
        _derived_embedding_bytes(input_count, dimensions),
        ABSOLUTE_MAX_RESPONSE_BYTES,
    )


def chat_response_ceiling(max_output_tokens: int) -> int:
    """Loose visible-output ceiling for this budget, physically hard-clamped."""
    return min(_derived_chat_bytes(max_output_tokens), ABSOLUTE_MAX_RESPONSE_BYTES)


def embedding_batch_within_contract(input_count: int) -> bool:
    """False when the request exceeds the DECLARED batch contract."""
    return input_count <= MAX_EMBEDDING_INPUTS


def embedding_response_is_serviceable(input_count: int, dimensions: int) -> bool:
    """False when this request's own shape implies a response this boundary
    will never read, so it can be refused BEFORE any paid network call.

    Both the declared batch contract and the derived size are checked: the
    contract is the operator-facing rule, the size bound is the mechanical
    guarantee, and neither alone covers an unusual dimension count.
    """
    return embedding_batch_within_contract(input_count) and (
        _derived_embedding_bytes(input_count, dimensions)
        <= ABSOLUTE_MAX_RESPONSE_BYTES
    )


def chat_response_is_serviceable(max_output_tokens: int) -> bool:
    """Whether the generation budget is inside Hivemind's declared contract.

    Response bytes are enforced independently while streaming. A reasoning
    token budget cannot predict the physical response size because some or all
    of those tokens may be provider-internal rather than serialized output.
    """
    return 1 <= max_output_tokens <= MAX_CHAT_GENERATION_TOKENS


class ResponseTooLarge(Exception):
    """A provider response exceeded its permitted ceiling."""


class ResponseEncodingRefused(Exception):
    """A provider applied a content-encoding after being asked not to."""


# Sent on every adapter-owned request. Asking for an identity encoding means
# the bytes on the wire ARE the bytes we validate, so a cap over raw bytes is
# a true total bound. Counting after decoding is NOT sufficient: httpx's gzip
# decoder calls ``zlib.decompress`` with no output limit, so one small raw
# chunk can allocate an unbounded buffer BEFORE any counter observes it.
IDENTITY_ENCODING_HEADERS = {"accept-encoding": "identity"}


async def read_bounded_body(response, max_bytes: int | None = None) -> bytes:
    """Read a streamed response over RAW bytes, aborting past ``max_bytes``.

    ``httpx``'s ordinary ``post``/``get`` eagerly buffer the COMPLETE body
    before any validation runs, so an arbitrarily large response could exhaust
    memory before a single cardinality or dimension check executed, defeating
    the error envelope through resource exhaustion rather than through any
    provider payload.

    ``aiter_raw`` is used rather than ``aiter_bytes`` deliberately: the latter
    yields CONTENT-DECODED bytes, and the decode happens before the counter
    can see it, so a compressed bomb allocates first and is bounded second.
    Reading raw bytes — paired with the identity encoding requested on every
    adapter request — makes the cap a genuine total bound. A provider that
    compresses anyway is refused rather than decompressed, since honouring it
    would reintroduce exactly the unbounded allocation this avoids.
    """
    encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("identity", ""):
        raise ResponseEncodingRefused()
    # Resolved at CALL time, not captured as a default at definition time, so
    # the module-level ceiling stays a single live authority (and so a test can
    # lower it without patching every call site).
    #
    # The absolute clamp lives HERE, at the enforcement point, not only in the
    # ceiling calculations: a caller that computes a limit incorrectly — or a
    # future caller that forgets to check serviceability — still cannot make
    # this reader retain more than the process bound. Defence in depth for the
    # exact defect this replaced, where a derived limit could reach ~10**20.
    limit = min(
        MAX_RESPONSE_BYTES if max_bytes is None else max_bytes,
        ABSOLUTE_MAX_RESPONSE_BYTES,
    )
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def safe_token_count(value: object) -> int | None:
    """A provider-reported token count, or ``None`` when it is unusable.

    Shared by BOTH adapters so their notion of a safe usage value cannot
    drift from the record's. It delegates the magnitude bound to
    :func:`records._is_bounded_int`, the same predicate ``ChatResult`` and
    ``EmbeddingResult`` enforce — so an out-of-range count (e.g. ``10**400``
    from a malformed or hostile provider) degrades to absent metadata here
    instead of reaching the record constructor and escaping the safe envelope
    as a raw ``ValueError``. ADR-0027 keeps missing usage explicitly absent
    rather than fatal, so a usable completion is never discarded over
    unusable optional telemetry.
    """
    return value if _is_bounded_int(value, minimum=0) else None


def safe_resolved_model(value: object, configured_model: str) -> str | None:
    """A corroborated provider-reported model identity, or ``None``.

    ADR-0027 lets ``resolved_model`` reach outward operational surfaces
    (``system_health`` role children) and the PERSISTED embedding-collection
    identity record, while forbidding raw provider payload, prompts,
    credentials, and endpoints on those same surfaces. Copying whatever string
    the provider puts in ``model`` therefore hands a compromised or malformed
    provider a channel straight through the redaction boundary — and, because
    the value lands in persisted identity/evidence, a way to corrupt the
    identity of an embedding collection.

    This is the SINGLE authority for that decision, shared by every response
    path in both adapters, so the three call sites cannot drift.

    ONLY an exact match with the configured identity is accepted, and the
    value returned is the CONFIGURED string, never the provider's. No provider
    bytes can reach the record even in principle: the function either echoes a
    value this process already had, or nothing.

    The first attempt at this guard (review round 8) also accepted the
    configured name extended at a separator boundary, to preserve real
    resolutions like ``gpt-4o`` -> ``gpt-4o-2024-08-06``. Review round 9 showed
    that rule is bypassable: the suffix was unconstrained apart from charset
    and length, and ``<configured_model>-<api-key>`` is pure
    letters/digits/hyphen, so a raw or base64url-encoded secret rode straight
    through as ``provider_reported``. A prefix check cannot separate a version
    pin from an appended payload, so the capability is withdrawn rather than
    narrowed — there is no safe way to accept attacker-chosen trailing bytes
    into a field bound for persisted identity.

    The consequence is deliberate and worth stating: ``resolved_model`` now
    carries no information beyond ``configured_model``. What it still records
    is whether the provider CONFIRMED the configured identity
    (``provider_reported``) or never corroborated it (``configured_only``),
    which is the distinction ADR-0027's ``model_evidence`` exists to make. A
    genuine version-pin resolution now reports ``configured_only``.

    Dropping rather than failing closed is deliberate too: discarding a usable
    completion over unverifiable optional telemetry would be a worse outcome
    than honestly recording that identity was not confirmed. This mirrors
    :func:`safe_token_count`, where unusable provider metadata degrades to
    absent instead of fatal.
    """
    if not isinstance(value, str):
        return None
    if value != configured_model:
        return None
    # Return OUR string, not the provider's equal-but-distinct object.
    return configured_model


def classify_httpx_transport(
    exc: BaseException, *, proxy_configured: bool = False
) -> tuple[str, bool]:
    """Map an ``httpx`` transport failure to ``(category, retryable)``.

    ``proxy_configured`` must reflect whether THIS call used an owned proxy
    transport: the same ``ConnectError``/``ConnectTimeout`` is raised whether
    the unreachable peer was the origin or the configured proxy, and only the
    direct (no-proxy) case is provably pre-send-safe to retry.
    """
    import httpx

    if isinstance(exc, httpx.ProxyError):
        return "unavailable", False
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        # Connection establishment failure: proven pre-send ONLY when there
        # is no proxy hop whose own failure could be indistinguishable from
        # this exception.
        return "unavailable", not proxy_configured
    if isinstance(exc, httpx.TimeoutException):
        # Read/write/pool deadline after the request may have been sent.
        return "timeout", False
    return "unavailable", False


def log_provider_failure(
    *,
    role: str,
    provider_id: str,
    adapter_id: str,
    category: str,
    correlation_id: str,
    exc: BaseException | None = None,
    status_code: int | None = None,
    diagnostic: str | None = None,
) -> None:
    """Structured server-side failure log with zero payload risk.

    Only the exception CLASS names and the HTTP status are recorded —
    exception text can embed endpoint URLs, credentials, or echoed request
    content and never reaches logs.
    """
    # Keep every sibling adapter/role log byte-for-byte in its prior shape;
    # only the boundary that owns these structural branches gains the field.
    diagnostic_scope = role == "chat" and adapter_id == "openai-compatible"
    safe_diagnostic = (
        diagnostic if diagnostic in _SAFE_PROVIDER_FAILURE_DIAGNOSTICS else "-"
    )
    exc_chain = []
    node = exc
    while node is not None and len(exc_chain) < 4:
        exc_chain.append(type(node).__name__)
        node = node.__cause__
    if diagnostic_scope:
        _logger.warning(
            "inference %s failure: category=%s provider=%s adapter=%s "
            "correlation_id=%s status=%s exception_chain=%s diagnostic=%s",
            role,
            category,
            provider_id,
            adapter_id,
            correlation_id,
            status_code if status_code is not None else "-",
            ">".join(exc_chain) if exc_chain else "-",
            safe_diagnostic,
        )
        return
    _logger.warning(
        "inference %s failure: category=%s provider=%s adapter=%s "
        "correlation_id=%s status=%s exception_chain=%s",
        role,
        category,
        provider_id,
        adapter_id,
        correlation_id,
        status_code if status_code is not None else "-",
        ">".join(exc_chain) if exc_chain else "-",
    )


def make_error(
    *,
    category: str,
    role: str,
    provider_id: str,
    adapter_id: str,
    retryable: bool,
    correlation_id: str,
) -> InferenceError:
    return InferenceError(
        category=category,
        role=role,
        provider_id=provider_id,
        adapter_id=adapter_id,
        retryable=retryable,
        correlation_id=correlation_id,
    )
