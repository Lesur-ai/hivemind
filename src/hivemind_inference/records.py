# -*- coding: utf-8 -*-
"""Immutable normalized request/result/probe records (ADR-0027).

Per-operation records deliberately CANNOT override provider, adapter,
endpoint, credential, model, temperature, or expected dimensions: none of
those exist as request fields. The resolved profile is snapshotted once by
the registry/runtime and governs every call.

None of these records carries ``space_id``, Hivemind commit, membership,
queue, lease, term, fencing, staging, manifest, tombstone, or watermark
authority — asserted structurally by ``tests/test_p13_inference_package.py``.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

MESSAGE_ROLES: tuple[str, ...] = ("system", "user", "assistant")
FINISH_REASONS: tuple[str, ...] = ("stop", "length", "content_rejected", "other")
MODEL_EVIDENCE_VALUES: tuple[str, ...] = (
    "provider_reported",
    "immutable_digest",
    "configured_only",
)
EMBEDDING_INPUT_TYPES: tuple[str, ...] = ("document", "query")
REQUEST_RETRY_POLICIES: tuple[str, ...] = ("bounded", "none")

# Frozen ADR-0027 safe error-category vocabulary. It lives with the other
# normalized record vocabularies so both the probe record (below) and the
# ``InferenceError`` envelope can reference it without an import cycle.
ERROR_CATEGORIES: tuple[str, ...] = (
    "auth",
    "quota_exhausted",
    "rate_limited",
    "timeout",
    "unsupported",
    "invalid_request",
    "content_rejected",
    "invalid_response",
    "unavailable",
)

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _generate_correlation_id() -> str:
    return uuid.uuid4().hex


def _validate_correlation_id(value: str) -> None:
    if not isinstance(value, str) or not _CORRELATION_ID_RE.fullmatch(value):
        raise ValueError(
            "correlation_id must match ^[A-Za-z0-9._-]{1,128}$ "
            "(omit it to have one generated)"
        )


def _is_finite_number(value: object) -> bool:
    """True only for a genuinely finite real ``int``/``float``.

    ``math.isfinite`` (and ``float()``) raise ``OverflowError`` on an
    out-of-range integer such as ``10**10000``; this helper catches that so
    every numeric validator stays TOTAL over accepted Python numeric types and
    fails closed value-free instead of escaping as a raw OverflowError. It is
    the single finite-number primitive shared by the records, resolved
    profiles, and the retry loop.
    """
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


# Digit-count ceiling for every integer record/profile field. Bounding the
# MAGNITUDE (a pure int comparison — never a float/str conversion) keeps repr(),
# safe_snapshot(), and JSON fingerprint serialization from ever reaching the
# CPython integer-string digit limit on a pathological directly-constructed
# value. No real context window, dimension, token count, or budget approaches
# 10**18.
_MAX_INT_FIELD = 10**18


def _is_bounded_int(value: object, *, minimum: int) -> bool:
    """A real ``int`` (not ``bool``) in ``[minimum, _MAX_INT_FIELD]``."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= _MAX_INT_FIELD
    )


def _validate_timeout(value: object) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("timeout_seconds must be a number")
    # ``value <= 0`` compares an int/float directly (no float() that could
    # itself overflow); _is_finite_number rejects a huge int, nan, and inf.
    if not _is_finite_number(value) or value <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")


def _validate_model_fields(configured_model: object, resolved_model: object) -> None:
    """Both result records carry model identities as OUTWARD metadata; keep them
    typed so no provider payload object can ride in a field treated as safe."""
    if not isinstance(configured_model, str) or not configured_model.strip():
        raise ValueError("configured_model must be a non-blank string")
    if resolved_model is not None and (
        not isinstance(resolved_model, str) or not resolved_model.strip()
    ):
        raise ValueError("resolved_model must be None or a non-blank string")


def _validate_model_evidence_coherence(
    model_evidence: str, resolved_model: object
) -> None:
    """Shared model-evidence coherence for the result records and the embedding
    fingerprint.

    ``configured_only`` carries NO resolved model; a ``provider_reported``
    identity MUST carry a validated non-blank ``resolved_model`` — otherwise a
    record or fingerprint would claim a provider-reported identity while
    carrying none. ``immutable_digest`` may omit it (the digest is the identity).
    """
    if model_evidence == "configured_only":
        if resolved_model is not None:
            raise ValueError(
                "configured_only evidence never populates resolved_model"
            )
    elif model_evidence == "provider_reported":
        if not (isinstance(resolved_model, str) and resolved_model.strip()):
            raise ValueError(
                "provider_reported evidence requires a non-blank resolved_model"
            )


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One ordered chat message (``system``/``user``/``assistant`` + text).

    ``content`` is prompt text: it is excluded from ``repr()`` so a diagnostic,
    assertion failure, or ordinary ``%r`` log can never leak the prompt that
    ADR-0027 forbids in logs and diagnostics.
    """

    role: str
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.role not in MESSAGE_ROLES:
            raise ValueError(f"message role must be one of {MESSAGE_ROLES}")
        if not isinstance(self.content, str):
            raise ValueError("message content must be a string")


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Normalized chat/generation request.

    ``max_output_tokens`` may only LOWER the profile ceiling — the adapter
    rejects a value above it as ``invalid_request``. ``timeout_seconds`` is
    the TOTAL deadline: connection, transmission, response read, any permitted
    retry delay, and both attempts. ``retry_policy="none"`` disables the one
    normally permitted replay for explicit bounded operations such as deep
    readiness; ``"bounded"`` preserves the global two-attempt ceiling.
    """

    messages: tuple[ChatMessage, ...] = field(repr=False)
    timeout_seconds: float
    max_output_tokens: int | None = None
    correlation_id: str = field(default_factory=_generate_correlation_id)
    retry_policy: str = "bounded"

    def __post_init__(self) -> None:
        # Reject a bare str/bytes BEFORE tuple() coercion: tuple("hi") would
        # otherwise silently become a per-character message batch.
        if isinstance(self.messages, (str, bytes)):
            raise ValueError(
                "messages must be an ordered sequence of ChatMessage, not a "
                "single str/bytes"
            )
        if not isinstance(self.messages, tuple):
            object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages:
            raise ValueError("messages must be a non-empty ordered sequence")
        for message in self.messages:
            if not isinstance(message, ChatMessage):
                raise ValueError("every message must be a ChatMessage")
        _validate_timeout(self.timeout_seconds)
        if self.max_output_tokens is not None:
            if not isinstance(self.max_output_tokens, int) or isinstance(
                self.max_output_tokens, bool
            ):
                raise ValueError("max_output_tokens must be an integer")
            if not _is_bounded_int(self.max_output_tokens, minimum=1):
                raise ValueError("max_output_tokens must be >= 1")
        _validate_correlation_id(self.correlation_id)
        if self.retry_policy not in REQUEST_RETRY_POLICIES:
            raise ValueError(
                f"retry_policy must be one of {REQUEST_RETRY_POLICIES}"
            )


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Normalized chat/generation result.

    ``model_evidence`` distinguishes provider-reported identity from the
    configured value: ``configured_only`` evidence never populates
    ``resolved_model``, and missing usage stays explicitly absent (``None``)
    instead of being mislabeled. Concrete adapters copy the originating
    request's ``correlation_id``; its default exists for trusted deterministic
    fakes that construct normalized results directly.
    """

    text: str = field(repr=False)  # completion content: never in repr/logs
    configured_model: str
    model_evidence: str
    finish_reason: str
    resolved_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    correlation_id: str = field(default_factory=_generate_correlation_id)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if self.finish_reason not in FINISH_REASONS:
            raise ValueError(f"finish_reason must be one of {FINISH_REASONS}")
        if self.model_evidence not in MODEL_EVIDENCE_VALUES:
            raise ValueError(
                f"model_evidence must be one of {MODEL_EVIDENCE_VALUES}"
            )
        _validate_model_fields(self.configured_model, self.resolved_model)
        _validate_model_evidence_coherence(self.model_evidence, self.resolved_model)
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and not _is_bounded_int(value, minimum=0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        _validate_correlation_id(self.correlation_id)


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """Normalized ordered embedding request.

    ``input_type`` (``document``/``query``) is semantically preserved even
    when the wire format of the selected profile has no distinct parameter
    for it (the standard OpenAI-compatible shape is symmetric). The closed
    retry policy has the same ``bounded``/``none`` semantics as chat.
    """

    inputs: tuple[str, ...] = field(repr=False)  # document content: never in repr
    timeout_seconds: float
    input_type: str = "document"
    correlation_id: str = field(default_factory=_generate_correlation_id)
    retry_policy: str = "bounded"

    def __post_init__(self) -> None:
        # Reject a bare str/bytes BEFORE tuple() coercion: EmbeddingRequest(
        # inputs="secret") would otherwise silently become a per-character
        # embedding batch ('s','e','c',...), inflating cost and producing
        # semantically invalid vectors instead of failing closed.
        if isinstance(self.inputs, (str, bytes)):
            raise ValueError(
                "inputs must be an ordered sequence of strings, not a single "
                "str/bytes"
            )
        if not isinstance(self.inputs, tuple):
            object.__setattr__(self, "inputs", tuple(self.inputs))
        if not self.inputs:
            raise ValueError("inputs must be a non-empty ordered sequence")
        for text in self.inputs:
            if not isinstance(text, str):
                raise ValueError("every embedding input must be a string")
        if self.input_type not in EMBEDDING_INPUT_TYPES:
            raise ValueError(
                f"input_type must be one of {EMBEDDING_INPUT_TYPES}"
            )
        _validate_timeout(self.timeout_seconds)
        _validate_correlation_id(self.correlation_id)
        if self.retry_policy not in REQUEST_RETRY_POLICIES:
            raise ValueError(
                f"retry_policy must be one of {REQUEST_RETRY_POLICIES}"
            )


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Normalized ordered embedding result.

    ``vectors`` preserve input order. The record ENFORCES its own normalized
    guarantee rather than trusting the adapter: every inner vector is
    canonicalized to an immutable tuple (so a retained caller list cannot
    mutate a supposedly frozen result), must have exactly
    ``effective_dimensions`` components, and every component must be a finite
    real number. A violation raises ``ValueError`` before the record exists —
    the same fail-closed boundary vector consumers rely on. Concrete adapters
    copy the request ``correlation_id``; direct deterministic fakes may rely on
    the generated default.
    """

    vectors: tuple[tuple[float, ...], ...] = field(repr=False)  # never in repr
    configured_model: str
    model_evidence: str
    effective_dimensions: int
    resolved_model: str | None = None
    input_tokens: int | None = None
    total_tokens: int | None = None
    correlation_id: str = field(default_factory=_generate_correlation_id)

    def __post_init__(self) -> None:
        if isinstance(self.vectors, (str, bytes)) or not isinstance(
            self.vectors, (tuple, list)
        ):
            raise ValueError("vectors must be a non-empty ordered sequence of vectors")
        if not self.vectors:
            raise ValueError("vectors must be a non-empty tuple of vectors")
        if self.model_evidence not in MODEL_EVIDENCE_VALUES:
            raise ValueError(
                f"model_evidence must be one of {MODEL_EVIDENCE_VALUES}"
            )
        _validate_model_fields(self.configured_model, self.resolved_model)
        _validate_model_evidence_coherence(self.model_evidence, self.resolved_model)
        if not _is_bounded_int(self.effective_dimensions, minimum=1):
            raise ValueError("effective_dimensions must be a positive integer")
        # Deep immutability + exact validation: canonicalize each inner vector
        # to a tuple and check cardinality and finite numeric components. This
        # makes the frozen result genuinely immutable (a retained caller list
        # can no longer mutate it) and rejects wrong-length or non-finite
        # vectors before any consumer trusts them.
        canonical: list[tuple[float, ...]] = []
        for vector in self.vectors:
            if isinstance(vector, (str, bytes)) or not isinstance(
                vector, (tuple, list)
            ):
                raise ValueError("each vector must be an ordered sequence of numbers")
            if len(vector) != self.effective_dimensions:
                raise ValueError(
                    "each vector must have exactly effective_dimensions components"
                )
            for component in vector:
                if (
                    not isinstance(component, (int, float))
                    or isinstance(component, bool)
                    or not _is_finite_number(component)
                ):
                    raise ValueError(
                        "every vector component must be a finite real number"
                    )
            canonical.append(tuple(vector))
        object.__setattr__(self, "vectors", tuple(canonical))
        for name in ("input_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and not _is_bounded_int(value, minimum=0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        _validate_correlation_id(self.correlation_id)


PROBE_CONNECTIVITY_VALUES: tuple[str, ...] = ("reachable", "unreachable")
PROBE_DISCOVERY_VALUES: tuple[str, ...] = ("available", "unsupported", "error", "not_run")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Role-scoped connectivity/discovery probe outcome.

    A successful model listing proves ONLY discovery/connectivity — never
    authentication, quota, or paid-inference readiness. ``discovery ==
    "unsupported"`` (listing not offered by the endpoint) keeps
    ``connectivity == "reachable"`` and is NOT a provider failure.
    ``model_available`` is ``None`` whenever the listing could not be read.
    """

    connectivity: str
    discovery: str
    model_available: bool | None = None
    latency_ms: float | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        if self.connectivity not in PROBE_CONNECTIVITY_VALUES:
            raise ValueError(
                f"connectivity must be one of {PROBE_CONNECTIVITY_VALUES}"
            )
        if self.discovery not in PROBE_DISCOVERY_VALUES:
            raise ValueError(f"discovery must be one of {PROBE_DISCOVERY_VALUES}")
        # latency_ms is an OUTWARD diagnostic: restrict it to None or a finite
        # non-negative number so no string/provider payload can reach the record
        # repr or a serialized health surface. Value-free rejection.
        if self.latency_ms is not None:
            if (
                not isinstance(self.latency_ms, (int, float))
                or isinstance(self.latency_ms, bool)
                or not _is_finite_number(self.latency_ms)
                or self.latency_ms < 0
            ):
                raise ValueError("latency_ms must be None or a finite non-negative number")
        # error_category is an OUTWARD diagnostic: restrict it to None or the
        # frozen safe set so no adapter can surface raw provider text or a
        # credential through this normalized record. The rejection is value-free.
        if self.error_category is not None and self.error_category not in ERROR_CATEGORIES:
            raise ValueError(
                "error_category must be None or a recognized safe inference "
                "error category"
            )
        # State coherence: an error category may only accompany an actual error
        # state, so a successful/unsupported reachable probe cannot carry one
        # (which would let a contradictory diagnostic report ``healthy`` while
        # signalling a failure).
        if self.error_category is not None and not (
            self.discovery == "error" or self.connectivity == "unreachable"
        ):
            raise ValueError(
                "error_category is only valid on an error probe (discovery == "
                "'error' or connectivity == 'unreachable')"
            )
        # Coherence, both directions: a successful listing (discovery ==
        # 'available') knows whether the configured model was found, so
        # model_available must be a bool exactly then; every other discovery
        # state listed nothing, so it must be None. An unreachable endpoint
        # cannot have listed anything.
        if self.discovery == "available":
            if not isinstance(self.model_available, bool):
                raise ValueError(
                    "model_available must be a bool when discovery == 'available'"
                )
        elif self.model_available is not None:
            raise ValueError(
                "model_available must be None unless discovery == 'available'"
            )
        if self.connectivity == "unreachable" and self.discovery not in (
            "not_run",
            "error",
        ):
            raise ValueError(
                "an unreachable endpoint cannot report discovery "
                "'available'/'unsupported'"
            )

    @property
    def healthy(self) -> bool:
        """Historical top-level health mapping: reachable and either a
        successful listing or an endpoint that simply does not offer one."""
        return self.connectivity == "reachable" and self.discovery in (
            "available",
            "unsupported",
        )
