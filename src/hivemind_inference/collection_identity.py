# -*- coding: utf-8 -*-
"""Pure canonical embedding identity for P13-managed Qdrant collections.

This module owns no Qdrant client and no collection lifecycle.  It only maps an
already-validated Graph Memory ``memory_id`` to a collision-resistant name,
builds/parses the compact persisted identity, and compares that identity with a
process-frozen embedding profile plus optional per-call model evidence.

Graph/long remains derived and non-authoritative: none of these records carries
Hivemind commit, membership, lease, fencing, tombstone, watermark, backup, or
recovery authority (ADR-0010, ADR-0017, ADR-0028).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .profiles import (
    _FINGERPRINT_FIELDS,
    EMBEDDING_COLLECTION_SCHEMA_VERSION,
    EMBEDDING_CONTRACT_VERSION,
    EMBEDDING_DISTANCE,
    ResolvedEmbeddingProfile,
    embedding_profile_fingerprint,
)
from .records import MODEL_EVIDENCE_VALUES, EmbeddingResult, _is_bounded_int
from .registry import EMBEDDING_PROVIDER_IDS, PROVIDER_TO_ADAPTER

__all__ = [
    "EMBEDDING_COLLECTION_IDENTITY_FIELDS",
    "EmbeddingCollectionIdentity",
    "EmbeddingIdentityError",
    "build_configured_embedding_collection_identity",
    "build_embedding_collection_identity",
    "canonical_qdrant_collection_name",
    "embedding_metadata_fingerprint",
    "parse_embedding_collection_identity",
    "validate_embedding_collection_identity",
]


_COLLECTION_NAME_PREFIX = "memory_v1_"
_COLLECTION_NAME_DOMAIN = b"hivemind:qdrant:v1\x00"
_MEMORY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

EMBEDDING_COLLECTION_IDENTITY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "embedding_contract_version",
    "memory_namespace",
    "provider_id",
    "adapter_id",
    "configured_model",
    "resolved_model",
    "model_evidence",
    "dimensions",
    "distance",
    "endpoint_sha256",
    "profile_fingerprint",
)

_ERROR_REASONS: tuple[str, ...] = (
    "invalid_memory_id",
    "invalid_metadata",
    "fingerprint_mismatch",
    "memory_namespace_mismatch",
    "static_profile_mismatch",
    "dynamic_evidence_mismatch",
)


class EmbeddingIdentityError(ValueError):
    """Value-free failure at the compact embedding-identity boundary."""

    def __init__(self, reason: str) -> None:
        canonical = None
        if type(reason) is str:
            for candidate in _ERROR_REASONS:
                if candidate == reason:
                    canonical = candidate
                    break
        if canonical is None:
            raise ValueError("embedding identity error reason is not recognized")
        self.reason = canonical
        super().__init__(f"embedding identity validation failed: {canonical}")


def _fail(reason: str) -> None:
    raise EmbeddingIdentityError(reason)


def _valid_memory_id(value: object) -> bool:
    return type(value) is str and _MEMORY_ID_PATTERN.fullmatch(value) is not None


def _require_memory_id(value: object, *, reason: str) -> str:
    if not _valid_memory_id(value):
        _fail(reason)
    return value


def _is_non_blank_string(value: object) -> bool:
    return type(value) is str and bool(str.strip(value))


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_PATTERN.fullmatch(value) is not None


def _valid_model_evidence(
    model_evidence: object,
    resolved_model: object,
) -> bool:
    if type(model_evidence) is not str:
        return False
    if model_evidence not in MODEL_EVIDENCE_VALUES:
        return False
    if resolved_model is not None and not _is_non_blank_string(resolved_model):
        return False
    if model_evidence == "configured_only":
        return resolved_model is None
    if model_evidence == "provider_reported":
        return _is_non_blank_string(resolved_model)
    return True


def canonical_qdrant_collection_name(memory_id: str) -> str:
    """Return ``memory_v1_<readable>_<full-domain-separated-sha256>``.

    The readable prefix is diagnostic only.  Exact ownership is always checked
    against the independently persisted ``memory_namespace``.
    """

    memory_id = _require_memory_id(memory_id, reason="invalid_memory_id")
    digest = hashlib.sha256(
        _COLLECTION_NAME_DOMAIN + str.encode(memory_id, "utf-8")
    ).hexdigest()
    return f"{_COLLECTION_NAME_PREFIX}{memory_id[:32]}_{digest}"


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
    """Hash the ten raw compatibility fields after strict value-free validation.

    This primitive deliberately accepts raw persisted scalars rather than a
    profile object.  A stored record therefore proves its own self-consistency;
    it cannot borrow a live profile's digest while carrying contradictory
    fields.
    """

    if (
        type(schema_version) is not str
        or schema_version != EMBEDDING_COLLECTION_SCHEMA_VERSION
        or type(embedding_contract_version) is not int
        or embedding_contract_version != EMBEDDING_CONTRACT_VERSION
        or type(provider_id) is not str
        or provider_id not in EMBEDDING_PROVIDER_IDS
        or type(adapter_id) is not str
        or adapter_id != PROVIDER_TO_ADAPTER[provider_id]
        or not _is_non_blank_string(configured_model)
        or not _valid_model_evidence(model_evidence, resolved_model)
        or type(dimensions) is not int
        or not _is_bounded_int(dimensions, minimum=1)
        or type(distance) is not str
        or distance != EMBEDDING_DISTANCE
        or not _is_sha256(endpoint_sha256)
    ):
        _fail("invalid_metadata")

    payload = {
        "schema_version": schema_version,
        "embedding_contract_version": embedding_contract_version,
        "provider_id": provider_id,
        "adapter_id": adapter_id,
        "configured_model": configured_model,
        "resolved_model": resolved_model,
        "model_evidence": model_evidence,
        "dimensions": dimensions,
        "distance": distance,
        "endpoint_sha256": endpoint_sha256,
    }
    assert tuple(payload) == _FINGERPRINT_FIELDS
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingCollectionIdentity:
    """Exact twelve-field identity persisted as Qdrant collection metadata."""

    schema_version: str
    embedding_contract_version: int
    memory_namespace: str
    provider_id: str
    adapter_id: str
    configured_model: str
    resolved_model: str | None
    model_evidence: str
    dimensions: int
    distance: str
    endpoint_sha256: str
    profile_fingerprint: str

    def __post_init__(self) -> None:
        _require_memory_id(self.memory_namespace, reason="invalid_metadata")
        if not _is_sha256(self.profile_fingerprint):
            _fail("invalid_metadata")
        expected = embedding_metadata_fingerprint(
            schema_version=self.schema_version,
            embedding_contract_version=self.embedding_contract_version,
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            configured_model=self.configured_model,
            resolved_model=self.resolved_model,
            model_evidence=self.model_evidence,
            dimensions=self.dimensions,
            distance=self.distance,
            endpoint_sha256=self.endpoint_sha256,
        )
        if self.profile_fingerprint != expected:
            _fail("fingerprint_mismatch")

    def to_mapping(self) -> dict[str, object]:
        """Return the exact persisted mapping in canonical field order."""

        return {
            "schema_version": self.schema_version,
            "embedding_contract_version": self.embedding_contract_version,
            "memory_namespace": self.memory_namespace,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "configured_model": self.configured_model,
            "resolved_model": self.resolved_model,
            "model_evidence": self.model_evidence,
            "dimensions": self.dimensions,
            "distance": self.distance,
            "endpoint_sha256": self.endpoint_sha256,
            "profile_fingerprint": self.profile_fingerprint,
        }


def _profile_fields(profile: object) -> dict[str, object]:
    if type(profile) is not ResolvedEmbeddingProfile:
        _fail("invalid_metadata")
    try:
        fields = {
            "schema_version": EMBEDDING_COLLECTION_SCHEMA_VERSION,
            "embedding_contract_version": EMBEDDING_CONTRACT_VERSION,
            "provider_id": profile.provider_id,
            "adapter_id": profile.adapter_id,
            "configured_model": profile.configured_model,
            "dimensions": profile.expected_dimensions,
            "distance": EMBEDDING_DISTANCE,
            "endpoint_sha256": profile.endpoint_sha256,
        }
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise EmbeddingIdentityError("invalid_metadata") from None
    return fields


def _result_fields(
    result: object,
    *,
    profile_fields: dict[str, object],
) -> tuple[str | None, str]:
    if type(result) is not EmbeddingResult:
        _fail("invalid_metadata")
    if (
        type(result.configured_model) is not str
        or result.configured_model != profile_fields["configured_model"]
        or type(result.effective_dimensions) is not int
        or result.effective_dimensions != profile_fields["dimensions"]
    ):
        _fail("dynamic_evidence_mismatch")
    if not _valid_model_evidence(result.model_evidence, result.resolved_model):
        _fail("invalid_metadata")
    return result.resolved_model, result.model_evidence


def _build_identity(
    memory_id: str,
    *,
    profile_fields: dict[str, object],
    resolved_model: str | None,
    model_evidence: str,
) -> EmbeddingCollectionIdentity:
    memory_namespace = _require_memory_id(
        memory_id,
        reason="invalid_memory_id",
    )
    fingerprint = embedding_metadata_fingerprint(
        **profile_fields,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
    )
    return EmbeddingCollectionIdentity(
        **profile_fields,
        memory_namespace=memory_namespace,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
        profile_fingerprint=fingerprint,
    )


def build_embedding_collection_identity(
    memory_id: str,
    profile: ResolvedEmbeddingProfile,
    result: EmbeddingResult,
) -> EmbeddingCollectionIdentity:
    """Build identity from the frozen profile and this call's model evidence."""

    profile_fields = _profile_fields(profile)
    resolved_model, model_evidence = _result_fields(
        result,
        profile_fields=profile_fields,
    )
    return _build_identity(
        memory_id,
        profile_fields=profile_fields,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
    )


def build_configured_embedding_collection_identity(
    memory_id: str,
    profile: ResolvedEmbeddingProfile,
) -> EmbeddingCollectionIdentity:
    """Build configured-only identity without fabricating an embedding result.

    This form is for an absent/zero-vector collection or backup identity where
    no paid embedding call exists.  It never claims provider-reported evidence.
    """

    return _build_identity(
        memory_id,
        profile_fields=_profile_fields(profile),
        resolved_model=None,
        model_evidence="configured_only",
    )


def parse_embedding_collection_identity(
    mapping: object,
) -> EmbeddingCollectionIdentity:
    """Parse an exact persisted mapping and recompute its own fingerprint."""

    if type(mapping) is not dict:
        _fail("invalid_metadata")
    keys = list(dict.keys(mapping))
    if (
        any(type(key) is not str for key in keys)
        or len(keys) != len(EMBEDDING_COLLECTION_IDENTITY_FIELDS)
        or set(keys) != set(EMBEDDING_COLLECTION_IDENTITY_FIELDS)
    ):
        _fail("invalid_metadata")
    return EmbeddingCollectionIdentity(
        **{
            field: dict.__getitem__(mapping, field)
            for field in EMBEDDING_COLLECTION_IDENTITY_FIELDS
        }
    )


def validate_embedding_collection_identity(
    identity: EmbeddingCollectionIdentity,
    *,
    memory_id: str,
    profile: ResolvedEmbeddingProfile,
    result: EmbeddingResult | None = None,
) -> EmbeddingCollectionIdentity:
    """Validate ownership, frozen profile, and optional per-call evidence."""

    if type(identity) is not EmbeddingCollectionIdentity:
        _fail("invalid_metadata")
    expected_namespace = _require_memory_id(
        memory_id,
        reason="invalid_memory_id",
    )
    profile_fields = _profile_fields(profile)

    if identity.memory_namespace != expected_namespace:
        _fail("memory_namespace_mismatch")

    if (
        identity.schema_version != profile_fields["schema_version"]
        or identity.embedding_contract_version
        != profile_fields["embedding_contract_version"]
        or identity.provider_id != profile_fields["provider_id"]
        or identity.adapter_id != profile_fields["adapter_id"]
        or identity.configured_model != profile_fields["configured_model"]
        or identity.dimensions != profile_fields["dimensions"]
        or identity.distance != profile_fields["distance"]
        or identity.endpoint_sha256 != profile_fields["endpoint_sha256"]
    ):
        _fail("static_profile_mismatch")

    try:
        live_fingerprint = embedding_profile_fingerprint(
            profile,
            resolved_model=identity.resolved_model,
            model_evidence=identity.model_evidence,
        )
    except (EmbeddingIdentityError, TypeError, ValueError, OverflowError):
        raise EmbeddingIdentityError("invalid_metadata") from None
    if identity.profile_fingerprint != live_fingerprint:
        _fail("static_profile_mismatch")

    if result is not None:
        resolved_model, model_evidence = _result_fields(
            result,
            profile_fields=profile_fields,
        )
        if (
            identity.resolved_model != resolved_model
            or identity.model_evidence != model_evidence
        ):
            _fail("dynamic_evidence_mismatch")

    return identity
