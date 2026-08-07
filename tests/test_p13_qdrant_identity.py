# -*- coding: utf-8 -*-
"""P13-1D (#277) — pure canonical Qdrant embedding identity.

This suite exercises only the dependency-neutral identity primitive.  Qdrant
observation, state resolution, and vector-store enforcement are covered by the
consumer lot; no Qdrant client or hostile in-process Python object belongs here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

import hivemind_inference.profiles as profiles_module
from hivemind_inference import (
    EMBEDDING_COLLECTION_SCHEMA_VERSION,
    EMBEDDING_CONTRACT_VERSION,
    EmbeddingResult,
    ResolvedEmbeddingProfile,
    embedding_metadata_fingerprint,
    embedding_profile_fingerprint,
)
from hivemind_inference.collection_identity import (
    EMBEDDING_COLLECTION_IDENTITY_FIELDS,
    EmbeddingCollectionIdentity,
    EmbeddingIdentityError,
    build_configured_embedding_collection_identity,
    build_embedding_collection_identity,
    canonical_qdrant_collection_name,
    parse_embedding_collection_identity,
    validate_embedding_collection_identity,
)


def _profile(
    *,
    configured_model: str = "embedding-model-v1",
    expected_dimensions: int = 3,
    endpoint: str = "https://embedding.example.test/v1",
) -> ResolvedEmbeddingProfile:
    return ResolvedEmbeddingProfile(
        provider_id="openai-compatible",
        adapter_id="openai-compatible",
        endpoint=endpoint,
        api_key="test-only-secret",
        configured_model=configured_model,
        expected_dimensions=expected_dimensions,
    )


def _result(
    *,
    configured_model: str = "embedding-model-v1",
    effective_dimensions: int = 3,
    resolved_model: str | None = "embedding-model-v1",
    model_evidence: str = "provider_reported",
) -> EmbeddingResult:
    vector = tuple(float(index + 1) for index in range(effective_dimensions))
    return EmbeddingResult(
        vectors=(vector,),
        configured_model=configured_model,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
        effective_dimensions=effective_dimensions,
    )


def _raw_fingerprint_fields(**overrides: object) -> dict[str, object]:
    profile = _profile()
    fields: dict[str, object] = {
        "schema_version": EMBEDDING_COLLECTION_SCHEMA_VERSION,
        "embedding_contract_version": EMBEDDING_CONTRACT_VERSION,
        "provider_id": profile.provider_id,
        "adapter_id": profile.adapter_id,
        "configured_model": profile.configured_model,
        "resolved_model": "embedding-model-v1",
        "model_evidence": "provider_reported",
        "dimensions": profile.expected_dimensions,
        "distance": "cosine",
        "endpoint_sha256": profile.endpoint_sha256,
    }
    fields.update(overrides)
    return fields


def _identity() -> EmbeddingCollectionIdentity:
    return build_embedding_collection_identity(
        "memory-A_1",
        _profile(),
        _result(),
    )


def _fixed_error(call, reason: str) -> EmbeddingIdentityError:
    with pytest.raises(EmbeddingIdentityError) as excinfo:
        call()
    error = excinfo.value
    assert error.reason == reason
    assert str(error) == f"embedding identity validation failed: {reason}"
    return error


class TestCanonicalCollectionName:
    def test_name_uses_readable_prefix_and_full_domain_separated_digest(self):
        memory_id = "Memory-01_with-readable-tail"
        expected_digest = hashlib.sha256(
            b"hivemind:qdrant:v1\x00" + memory_id.encode("utf-8")
        ).hexdigest()

        assert canonical_qdrant_collection_name(memory_id) == (
            f"memory_v1_{memory_id[:32]}_{expected_digest}"
        )

    def test_deployed_lossy_collision_and_same_readable_prefix_do_not_collide(self):
        assert canonical_qdrant_collection_name(
            "a-b"
        ) != canonical_qdrant_collection_name("a_b")

        common = "A" * 32
        assert canonical_qdrant_collection_name(
            common + "x"
        ) != canonical_qdrant_collection_name(common + "y")

    def test_maximum_name_is_exactly_107_ascii_characters(self):
        memory_id = "A" * 64
        name = canonical_qdrant_collection_name(memory_id)

        assert len(name) == 107
        assert name.isascii()
        assert name.startswith(f"memory_v1_{'A' * 32}_")
        assert len(name.rsplit("_", 1)[1]) == 64

    @pytest.mark.parametrize(
        "memory_id",
        [
            "",
            "_leading",
            "-leading",
            "contains.dot",
            "contains/slash",
            "contains space",
            "é",
            "A" * 65,
            b"bytes",
            7,
            True,
            None,
        ],
    )
    def test_invalid_memory_ids_fail_value_free(self, memory_id):
        error = _fixed_error(
            lambda: canonical_qdrant_collection_name(memory_id),
            "invalid_memory_id",
        )
        assert repr(memory_id) not in str(error)


class TestRawEmbeddingFingerprint:
    def test_raw_helper_hashes_the_exact_ten_field_canonical_payload(self):
        fields = _raw_fingerprint_fields()
        expected = hashlib.sha256(
            json.dumps(
                fields,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        assert embedding_metadata_fingerprint(**fields) == expected

    def test_live_profile_helper_delegates_to_the_raw_helper(self, monkeypatch):
        profile = _profile()
        captured = {}

        def fake_raw_helper(**fields):
            captured.update(fields)
            return "a" * 64

        monkeypatch.setattr(
            profiles_module,
            "embedding_metadata_fingerprint",
            fake_raw_helper,
        )

        assert (
            embedding_profile_fingerprint(
                profile,
                resolved_model="embedding-model-v1",
                model_evidence="provider_reported",
            )
            == "a" * 64
        )
        assert captured == _raw_fingerprint_fields()

    def test_live_and_raw_helpers_have_one_compatibility_identity(self):
        profile = _profile()
        assert embedding_profile_fingerprint(
            profile,
            resolved_model="embedding-model-v1",
            model_evidence="provider_reported",
        ) == embedding_metadata_fingerprint(**_raw_fingerprint_fields())

    @pytest.mark.parametrize(
        "overrides",
        [
            {"schema_version": "other-schema"},
            {"schema_version": b"hivemind.embedding-collection.v1"},
            {"embedding_contract_version": 2},
            {"embedding_contract_version": True},
            {"provider_id": "unknown"},
            {"provider_id": b"openai-compatible"},
            {"adapter_id": "anthropic"},
            {"configured_model": ""},
            {"configured_model": 4},
            {"resolved_model": 4},
            {"model_evidence": "unknown"},
            {"model_evidence": b"provider_reported"},
            {"model_evidence": "configured_only"},
            {"dimensions": 0},
            {"dimensions": True},
            {"distance": "dot"},
            {"distance": b"cosine"},
            {"endpoint_sha256": "A" * 64},
            {"endpoint_sha256": "not-a-digest"},
        ],
    )
    def test_raw_fields_use_strict_builtin_types_and_value_free_validation(
        self, overrides
    ):
        fields = _raw_fingerprint_fields(**overrides)
        error = _fixed_error(
            lambda: embedding_metadata_fingerprint(**fields),
            "invalid_metadata",
        )
        assert "unknown" not in str(error)
        assert "not-a-digest" not in str(error)


class TestEmbeddingCollectionIdentity:
    def test_configured_builder_never_fabricates_provider_evidence(self):
        profile = _profile()
        identity = build_configured_embedding_collection_identity(
            "memory-A_1",
            profile,
        )

        assert identity.resolved_model is None
        assert identity.model_evidence == "configured_only"
        assert validate_embedding_collection_identity(
            identity,
            memory_id="memory-A_1",
            profile=profile,
        ) is identity

    def test_builder_produces_the_exact_twelve_field_identity(self):
        profile = _profile()
        result = _result()
        identity = build_embedding_collection_identity(
            "memory-A_1",
            profile,
            result,
        )

        assert EMBEDDING_COLLECTION_IDENTITY_FIELDS == (
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
        assert tuple(identity.to_mapping()) == EMBEDDING_COLLECTION_IDENTITY_FIELDS
        assert identity.memory_namespace == "memory-A_1"
        assert identity.provider_id == profile.provider_id
        assert identity.adapter_id == profile.adapter_id
        assert identity.configured_model == profile.configured_model
        assert identity.resolved_model == result.resolved_model
        assert identity.model_evidence == result.model_evidence
        assert identity.dimensions == result.effective_dimensions
        assert identity.distance == "cosine"
        assert identity.endpoint_sha256 == profile.endpoint_sha256
        assert identity.profile_fingerprint == embedding_metadata_fingerprint(
            **{
                key: value
                for key, value in identity.to_mapping().items()
                if key
                not in {
                    "memory_namespace",
                    "profile_fingerprint",
                }
            }
        )

    def test_mapping_round_trip_is_exact_and_returns_a_plain_dict(self):
        identity = _identity()
        mapping = identity.to_mapping()

        assert type(mapping) is dict
        assert parse_embedding_collection_identity(mapping) == identity

    @pytest.mark.parametrize(
        "field",
        EMBEDDING_COLLECTION_IDENTITY_FIELDS,
    )
    def test_parser_rejects_every_missing_field(self, field):
        mapping = _identity().to_mapping()
        del mapping[field]

        _fixed_error(
            lambda: parse_embedding_collection_identity(mapping),
            "invalid_metadata",
        )

    def test_parser_rejects_extra_non_string_and_non_dict_shapes(self):
        extra = _identity().to_mapping()
        extra["unexpected"] = "value"
        _fixed_error(
            lambda: parse_embedding_collection_identity(extra),
            "invalid_metadata",
        )

        non_string_key = _identity().to_mapping()
        non_string_key[1] = non_string_key.pop("schema_version")
        _fixed_error(
            lambda: parse_embedding_collection_identity(non_string_key),
            "invalid_metadata",
        )

        _fixed_error(
            lambda: parse_embedding_collection_identity(
                list(_identity().to_mapping().items())
            ),
            "invalid_metadata",
        )

    def test_parser_recomputes_fingerprint_from_its_own_fields(self):
        copied_fingerprint = _identity().to_mapping()
        copied_fingerprint["configured_model"] = "another-valid-model"
        _fixed_error(
            lambda: parse_embedding_collection_identity(copied_fingerprint),
            "fingerprint_mismatch",
        )

        forged_digest = _identity().to_mapping()
        forged_digest["profile_fingerprint"] = "b" * 64
        _fixed_error(
            lambda: parse_embedding_collection_identity(forged_digest),
            "fingerprint_mismatch",
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("schema_version", "other-schema"),
            ("embedding_contract_version", True),
            ("memory_namespace", "contains.dot"),
            ("provider_id", "unknown"),
            ("adapter_id", "anthropic"),
            ("configured_model", ""),
            ("resolved_model", 1),
            ("model_evidence", "configured_only"),
            ("dimensions", 0),
            ("distance", "dot"),
            ("endpoint_sha256", "not-a-digest"),
            ("profile_fingerprint", "not-a-digest"),
        ],
    )
    def test_parser_rejects_each_structurally_invalid_field(self, field, value):
        mapping = _identity().to_mapping()
        mapping[field] = value

        _fixed_error(
            lambda: parse_embedding_collection_identity(mapping),
            "invalid_metadata",
        )

    def test_builder_rejects_result_static_disagreement_before_identity_exists(self):
        _fixed_error(
            lambda: build_embedding_collection_identity(
                "memory-A_1",
                _profile(),
                _result(configured_model="other-model"),
            ),
            "dynamic_evidence_mismatch",
        )
        _fixed_error(
            lambda: build_embedding_collection_identity(
                "memory-A_1",
                _profile(),
                _result(effective_dimensions=4),
            ),
            "dynamic_evidence_mismatch",
        )

    def test_builder_rejects_non_records_value_free(self):
        _fixed_error(
            lambda: build_embedding_collection_identity(
                "memory-A_1",
                {"provider_id": "openai-compatible"},
                _result(),
            ),
            "invalid_metadata",
        )
        _fixed_error(
            lambda: build_embedding_collection_identity(
                "memory-A_1",
                _profile(),
                {"model_evidence": "provider_reported"},
            ),
            "invalid_metadata",
        )


class TestExpectedIdentityValidation:
    def test_exact_identity_matches_static_profile_and_dynamic_result(self):
        profile = _profile()
        result = _result()
        identity = build_embedding_collection_identity(
            "memory-A_1",
            profile,
            result,
        )

        assert validate_embedding_collection_identity(
            identity,
            memory_id="memory-A_1",
            profile=profile,
            result=result,
        ) is identity

    def test_non_provider_operations_validate_static_fields_without_rewriting_evidence(
        self,
    ):
        profile = _profile()
        identity = _identity()

        assert validate_embedding_collection_identity(
            identity,
            memory_id="memory-A_1",
            profile=profile,
        ) is identity
        assert identity.model_evidence == "provider_reported"
        assert identity.resolved_model == "embedding-model-v1"

    def test_expected_namespace_is_validated_and_compared_exactly(self):
        identity = _identity()

        _fixed_error(
            lambda: validate_embedding_collection_identity(
                identity,
                memory_id="memory-B_2",
                profile=_profile(),
            ),
            "memory_namespace_mismatch",
        )
        _fixed_error(
            lambda: validate_embedding_collection_identity(
                identity,
                memory_id="contains.dot",
                profile=_profile(),
            ),
            "invalid_memory_id",
        )

    @pytest.mark.parametrize(
        "profile",
        [
            _profile(configured_model="another-model"),
            _profile(expected_dimensions=4),
            _profile(endpoint="https://other-embedding.example.test/v1"),
        ],
        ids=["configured-model", "dimensions", "endpoint"],
    )
    def test_static_profile_drift_is_distinct_from_malformed_metadata(self, profile):
        _fixed_error(
            lambda: validate_embedding_collection_identity(
                _identity(),
                memory_id="memory-A_1",
                profile=profile,
            ),
            "static_profile_mismatch",
        )

    def test_dynamic_result_evidence_must_match_the_stored_identity(self):
        configured_only = _result(
            resolved_model=None,
            model_evidence="configured_only",
        )

        _fixed_error(
            lambda: validate_embedding_collection_identity(
                _identity(),
                memory_id="memory-A_1",
                profile=_profile(),
                result=configured_only,
            ),
            "dynamic_evidence_mismatch",
        )

    def test_validator_accepts_only_exact_normalized_records(self):
        _fixed_error(
            lambda: validate_embedding_collection_identity(
                _identity().to_mapping(),
                memory_id="memory-A_1",
                profile=_profile(),
            ),
            "invalid_metadata",
        )
        _fixed_error(
            lambda: validate_embedding_collection_identity(
                _identity(),
                memory_id="memory-A_1",
                profile={"provider_id": "openai-compatible"},
            ),
            "invalid_metadata",
        )
        _fixed_error(
            lambda: validate_embedding_collection_identity(
                _identity(),
                memory_id="memory-A_1",
                profile=_profile(),
                result={"model_evidence": "provider_reported"},
            ),
            "invalid_metadata",
        )

    def test_typed_errors_never_echo_secret_bearing_values(self):
        secret = "https://user:LEAK-ME@example.test/v1"
        fields = _raw_fingerprint_fields(endpoint_sha256=secret)

        error = _fixed_error(
            lambda: embedding_metadata_fingerprint(**fields),
            "invalid_metadata",
        )
        assert secret not in str(error)
        assert secret not in repr(error)
