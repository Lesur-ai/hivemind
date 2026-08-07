# -*- coding: utf-8 -*-
"""Immutable resolved role profiles and the embedding compatibility identity.

The registry resolves configuration ONCE into an immutable role profile.
Environment changes after resolution do not mutate a running provider; a new
process/profile resolution is required (ADR-0027).

Secret hygiene: ``endpoint`` and ``api_key`` are secret-bearing internal
fields. They are excluded from ``repr()`` and from :meth:`safe_snapshot`;
outward surfaces only ever see ``endpoint_sha256`` fingerprints.

Scope (P13-1A / #274, P13-1D / #277): this module owns the canonical PROFILE
and ENDPOINT identities — ``endpoint_sha256`` and
``embedding_profile_fingerprint``.  The live-profile helper delegates to
#277's raw ten-field ``embedding_metadata_fingerprint`` primitive, so a stored
Qdrant identity can recompute its own compatibility digest without borrowing a
live profile.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .records import (
    _is_bounded_int,
    _is_finite_number,
)
from .registry import (
    ADAPTER_TEMPERATURE_RANGES,
    EMBEDDING_PROVIDER_IDS,
    PROVIDER_TO_ADAPTER,
    validate_endpoint,
)

EMBEDDING_COLLECTION_SCHEMA_VERSION = "hivemind.embedding-collection.v1"
EMBEDDING_CONTRACT_VERSION = 1
EMBEDDING_DISTANCE = "cosine"

# Marks which configuration family produced the profile. ``llmaas-legacy``
# keeps the historical unified profile identifiable for the once-per-process
# deprecation warning and for operator-facing docs; it never changes adapter
# selection.
PROFILE_SOURCES: tuple[str, ...] = ("inference", "llmaas-legacy")

# Provider generation budget accepted by the shared chat profile contract.
# Hidden reasoning tokens may consume this budget without appearing in the
# serialized response, so response-byte safety is enforced independently by
# the adapters' streamed raw-body ceiling.
MAX_CHAT_GENERATION_TOKENS = 1_000_000


def endpoint_sha256(endpoint: str) -> str:
    """SHA-256 of the normalized endpoint identity.

    The normalized identity is ``scheme://host[:port]path`` with a lowercase
    scheme/host, the default port elided, no trailing slash, and — by
    configuration-validation guarantee — no userinfo, query, or fragment.
    Stored internally (Qdrant collection metadata) and never exposed raw.
    """
    scheme = host = ""
    port = None
    path = ""
    parsed = True
    try:
        parts = urlsplit(endpoint)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
        path = parts.path.rstrip("/")
    except ValueError:
        # A malformed authority (bad port / IPv6 literal) makes urlsplit or its
        # accessors raise a ValueError embedding the configured value. Fail
        # value-free, and raise OUTSIDE the except so the secret-bearing
        # original is not attached to the exception chain.
        parsed = False
    if not parsed:
        raise ValueError("endpoint is not a valid absolute URL")
    default_port = {"http": 80, "https": 443}.get(scheme)
    effective_port = default_port if port is None else port
    # Structured canonical tuple (JSON), NOT textual host:port concatenation:
    # the textual form was not injective for IPv6 (host "::1:443" with no port
    # vs host "::1" with port 443 would collide). Separate fields with an
    # explicit effective port remove that ambiguity.
    canonical = json.dumps(
        {
            "scheme": scheme,
            "host": host,
            "port": effective_port,
            "path": path,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_profile_endpoint(endpoint: str, provider_id: str) -> None:
    """Fail closed, value-free, unless ``endpoint`` satisfies the SAME
    provider-aware policy that configuration resolution enforces.

    Resolved profiles are public, directly constructible API. Delegating to the
    shared :func:`registry.validate_endpoint` means a directly built named-
    provider profile is pinned to its documented host/path/scheme (not just
    generic URL hygiene), so it cannot send a credential to an arbitrary
    endpoint, and config/profile validation cannot drift. The shared validator
    is value-free and never leaks the endpoint on any exception chain.
    """
    endpoint_errors = validate_endpoint(
        "profile endpoint", endpoint, provider_id=provider_id
    )
    if endpoint_errors:
        raise ValueError(endpoint_errors[0])


def _validate_common_profile_fields(
    provider_id: str,
    adapter_id: str,
    configured_model: str,
    api_key: str,
    *,
    allowed_providers,
) -> None:
    """Registry/role/identity invariants shared by both resolved profiles.

    Resolved profiles are public API, so a directly built profile must satisfy
    the same frozen contract config resolution enforces: a registered,
    role-compatible provider; the exact registry-derived adapter; and a
    non-blank model and non-empty key. Diagnostics are value-free.
    """
    if provider_id not in PROVIDER_TO_ADAPTER:
        raise ValueError("profile provider_id is not a registered provider")
    if provider_id not in allowed_providers:
        raise ValueError("profile provider does not support this role")
    if adapter_id != PROVIDER_TO_ADAPTER[provider_id]:
        raise ValueError(
            "profile adapter_id is not the registered adapter for the provider"
        )
    if not isinstance(configured_model, str) or not configured_model.strip():
        raise ValueError("profile configured_model must be a non-blank string")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("profile api_key must be a non-blank string")


@dataclass(frozen=True, slots=True)
class ResolvedChatProfile:
    """Immutable resolved chat-role profile."""

    provider_id: str
    adapter_id: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)
    configured_model: str
    context_window: int
    max_output_tokens: int
    temperature: float | None = None
    source: str = "inference"

    role: str = field(default="chat", init=False)

    def __post_init__(self) -> None:
        if self.source not in PROFILE_SOURCES:
            raise ValueError(f"profile source must be one of {PROFILE_SOURCES}")
        # Chat providers are unrestricted by role; the registry membership check
        # in the shared validator still rejects unknown identifiers. Validate
        # the provider first, then the provider-aware endpoint policy.
        _validate_common_profile_fields(
            self.provider_id,
            self.adapter_id,
            self.configured_model,
            self.api_key,
            allowed_providers=PROVIDER_TO_ADAPTER,
        )
        _validate_profile_endpoint(self.endpoint, self.provider_id)
        for name, value in (
            ("context_window", self.context_window),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if not _is_bounded_int(value, minimum=1):
                raise ValueError(f"profile {name} must be a positive integer")
        if self.max_output_tokens > MAX_CHAT_GENERATION_TOKENS:
            raise ValueError(
                "profile max_output_tokens exceeds the supported generation budget"
            )
        if self.max_output_tokens >= self.context_window:
            raise ValueError(
                "profile max_output_tokens must be strictly below context_window"
            )
        if self.temperature is not None:
            if (
                not isinstance(self.temperature, (int, float))
                or isinstance(self.temperature, bool)
                or not _is_finite_number(self.temperature)
            ):
                raise ValueError("profile temperature must be a finite number")
            low, high = ADAPTER_TEMPERATURE_RANGES[self.adapter_id]
            if not (low <= self.temperature <= high):
                raise ValueError(
                    "profile temperature is outside the adapter's supported range"
                )

    @property
    def endpoint_sha256(self) -> str:
        return endpoint_sha256(self.endpoint)

    def safe_snapshot(self) -> dict:
        """Secret-free view for authenticated operational surfaces."""
        return {
            "role": self.role,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "configured_model": self.configured_model,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ResolvedEmbeddingProfile:
    """Immutable resolved embedding-role profile.

    ``expected_dimensions`` is the exact expected output dimension and the
    Qdrant vector size. It is VALIDATION metadata, not an implicit provider
    request parameter: adapters never send a wire ``dimensions`` field merely
    because this setting exists (ADR-0027).
    """

    provider_id: str
    adapter_id: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)
    configured_model: str
    expected_dimensions: int
    source: str = "inference"

    role: str = field(default="embedding", init=False)

    def __post_init__(self) -> None:
        if self.source not in PROFILE_SOURCES:
            raise ValueError(f"profile source must be one of {PROFILE_SOURCES}")
        # Embeddings are role-restricted: anthropic (registry adapter
        # "anthropic") has no embedding model and is excluded here. Validate the
        # provider first, then the provider-aware endpoint policy.
        _validate_common_profile_fields(
            self.provider_id,
            self.adapter_id,
            self.configured_model,
            self.api_key,
            allowed_providers=EMBEDDING_PROVIDER_IDS,
        )
        _validate_profile_endpoint(self.endpoint, self.provider_id)
        if not _is_bounded_int(self.expected_dimensions, minimum=1):
            raise ValueError("profile expected_dimensions must be a positive integer")

    @property
    def endpoint_sha256(self) -> str:
        return endpoint_sha256(self.endpoint)

    def safe_snapshot(self) -> dict:
        """Secret-free view for authenticated operational surfaces."""
        return {
            "role": self.role,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "configured_model": self.configured_model,
            "expected_dimensions": self.expected_dimensions,
            "source": self.source,
        }


# Compatibility fields hashed into ``profile_fingerprint``, in canonical
# order. ``memory_namespace`` (ownership) and the fingerprint itself are
# deliberately excluded (ADR-0028 compact identity).
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "embedding_contract_version",
    "provider_id",
    "adapter_id",
    "configured_model",
    "resolved_model",
    "model_evidence",
    "dimensions",
    "distance",
    "endpoint_sha256",
)


def embedding_metadata_fingerprint(
    *,
    schema_version: str,
    embedding_contract_version: int,
    provider_id: str,
    adapter_id: str,
    configured_model: str,
    resolved_model: str | None,
    model_evidence: str,
    dimensions: int,
    distance: str,
    endpoint_sha256: str,
) -> str:
    """Delegate raw stored-field hashing to the #277 identity primitive.

    The import is intentionally lazy: ``collection_identity`` consumes the
    profile types and constants from this module, while package import must stay
    dependency-light and cycle-free.
    """

    from .collection_identity import (
        embedding_metadata_fingerprint as _raw_metadata_fingerprint,
    )

    return _raw_metadata_fingerprint(
        schema_version=schema_version,
        embedding_contract_version=embedding_contract_version,
        provider_id=provider_id,
        adapter_id=adapter_id,
        configured_model=configured_model,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
        dimensions=dimensions,
        distance=distance,
        endpoint_sha256=endpoint_sha256,
    )


def embedding_profile_fingerprint(
    profile: ResolvedEmbeddingProfile,
    *,
    resolved_model: str | None = None,
    model_evidence: str = "configured_only",
) -> str:
    """SHA-256 fingerprint of the embedding compatibility identity.

    At resolution time configuration cannot carry provider-reported model
    identity, so the runtime snapshot uses ``configured_only`` evidence with
    an absent ``resolved_model``; richer evidence values are reserved for
    future digest-pinned/certified profiles.

    This is the canonical PROFILE identity primitive (P13-1A). The Qdrant
    COLLECTION metadata that embeds it, and the stored-metadata
    self-consistency recomputation, land with the #277 drift guards.
    """
    return embedding_metadata_fingerprint(
        schema_version=EMBEDDING_COLLECTION_SCHEMA_VERSION,
        embedding_contract_version=EMBEDDING_CONTRACT_VERSION,
        provider_id=profile.provider_id,
        adapter_id=profile.adapter_id,
        configured_model=profile.configured_model,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
        dimensions=profile.expected_dimensions,
        distance=EMBEDDING_DISTANCE,
        endpoint_sha256=profile.endpoint_sha256,
    )
