# -*- coding: utf-8 -*-
"""P13-1A (#274) — immutable records, safe error envelope, profile/endpoint
identity, and the frozen provider→adapter/role registry map (ADR-0027).

These are the dependency-neutral CONTRACTS the boundary freezes before any
provider adapter (#275) or consumer migration (#276) exists. Everything here is
deterministic and offline: no network, no SDK, no ``.env``.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from hivemind_inference import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    EmbeddingRequest,
    EmbeddingResult,
    ERROR_CATEGORIES,
    InferenceError,
    ProbeResult,
    ResolvedChatProfile,
    ResolvedEmbeddingProfile,
    adapter_for_provider,
    embedding_profile_fingerprint,
    endpoint_sha256,
)
from hivemind_inference.records import (
    EMBEDDING_INPUT_TYPES,
    FINISH_REASONS,
    MESSAGE_ROLES,
    MODEL_EVIDENCE_VALUES,
    REQUEST_RETRY_POLICIES,
)
from hivemind_inference.profiles import MAX_CHAT_GENERATION_TOKENS
from hivemind_inference.registry import (
    ADAPTER_TEMPERATURE_RANGES,
    CHAT_PROVIDER_IDS,
    EMBEDDING_PROVIDER_IDS,
    HOSTED_PROVIDER_HOSTS,
    HOSTED_PROVIDER_PATHS,
    PROVIDER_TO_ADAPTER,
)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# ChatMessage / ChatRequest                                                   #
# --------------------------------------------------------------------------- #


class TestChatMessage:
    def test_valid_roles(self):
        for role in MESSAGE_ROLES:
            assert ChatMessage(role=role, content="x").role == role

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError):
            ChatMessage(role="tool", content="x")

    def test_non_string_content_rejected(self):
        with pytest.raises(ValueError):
            ChatMessage(role="user", content=123)  # type: ignore[arg-type]

    def test_frozen(self):
        message = ChatMessage(role="user", content="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            message.content = "y"  # type: ignore[misc]


class TestChatRequest:
    def _msg(self):
        return (ChatMessage(role="user", content="hi"),)

    def test_empty_messages_rejected(self):
        with pytest.raises(ValueError):
            ChatRequest(messages=(), timeout_seconds=1.0)

    def test_non_chatmessage_rejected(self):
        with pytest.raises(ValueError):
            ChatRequest(messages=("hi",), timeout_seconds=1.0)  # type: ignore[arg-type]

    def test_list_messages_coerced_to_tuple(self):
        request = ChatRequest(messages=[ChatMessage(role="user", content="hi")], timeout_seconds=1.0)
        assert isinstance(request.messages, tuple)

    @pytest.mark.parametrize("timeout", [0, -1.0, float("inf"), float("nan"), True, "1"])
    def test_invalid_timeout_rejected(self, timeout):
        with pytest.raises(ValueError):
            ChatRequest(messages=self._msg(), timeout_seconds=timeout)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0, -1, True])
    def test_invalid_max_output_tokens_rejected(self, value):
        with pytest.raises(ValueError):
            ChatRequest(messages=self._msg(), timeout_seconds=1.0, max_output_tokens=value)

    def test_none_max_output_tokens_allowed(self):
        assert ChatRequest(messages=self._msg(), timeout_seconds=1.0).max_output_tokens is None

    def test_correlation_id_generated_hex32(self):
        request = ChatRequest(messages=self._msg(), timeout_seconds=1.0)
        assert len(request.correlation_id) == 32
        int(request.correlation_id, 16)  # valid hex

    @pytest.mark.parametrize("policy", REQUEST_RETRY_POLICIES)
    def test_explicit_retry_policies_are_closed_and_preserved(self, policy):
        request = ChatRequest(
            messages=self._msg(), timeout_seconds=1.0, retry_policy=policy
        )
        assert request.retry_policy == policy

    @pytest.mark.parametrize("bad", ["", "zero", "unbounded", None, 0])
    def test_unknown_retry_policy_is_rejected(self, bad):
        with pytest.raises(ValueError):
            ChatRequest(
                messages=self._msg(),
                timeout_seconds=1.0,
                retry_policy=bad,
            )

    @pytest.mark.parametrize("bad", ["with space", "a" * 129, "tab\t", ""])
    def test_invalid_correlation_id_rejected(self, bad):
        with pytest.raises(ValueError):
            ChatRequest(messages=self._msg(), timeout_seconds=1.0, correlation_id=bad)

    def test_frozen(self):
        request = ChatRequest(messages=self._msg(), timeout_seconds=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.timeout_seconds = 2.0  # type: ignore[misc]

    def test_no_authority_or_identity_override_fields(self):
        # Per-operation records cannot override provider/model/endpoint/etc.
        # nor carry protocol-authority fields.
        forbidden = {
            "provider_id", "adapter_id", "endpoint", "api_key", "model",
            "configured_model", "temperature", "expected_dimensions",
            "space_id", "commit_id", "membership", "term", "lease",
            "fencing", "staging", "manifest", "tombstone", "watermark",
        }
        names = {f.name for f in dataclasses.fields(ChatRequest)}
        assert not (names & forbidden)


class TestChatResult:
    def test_valid(self):
        result = ChatResult(text="hi", configured_model="m", model_evidence="configured_only", finish_reason="stop")
        assert result.resolved_model is None

    def test_correlation_id_is_validated_and_safe_to_render(self):
        result = ChatResult(
            text="secret completion",
            configured_model="m",
            model_evidence="configured_only",
            finish_reason="stop",
            correlation_id="request-42",
        )
        assert result.correlation_id == "request-42"
        assert "secret completion" not in repr(result)
        with pytest.raises(ValueError):
            dataclasses.replace(result, correlation_id="unsafe id")

    @pytest.mark.parametrize("finish", ["stopped", "", "STOP", "eos"])
    def test_unknown_finish_reason_rejected(self, finish):
        with pytest.raises(ValueError):
            ChatResult(text="x", configured_model="m", model_evidence="configured_only", finish_reason=finish)

    def test_all_frozen_finish_reasons_accepted(self):
        for finish in FINISH_REASONS:
            assert ChatResult(text="x", configured_model="m", model_evidence="configured_only", finish_reason=finish).finish_reason == finish

    def test_configured_only_never_populates_resolved_model(self):
        with pytest.raises(ValueError):
            ChatResult(text="x", configured_model="m", model_evidence="configured_only", finish_reason="stop", resolved_model="m-1")

    def test_provider_reported_may_carry_resolved_model(self):
        result = ChatResult(text="x", configured_model="m", model_evidence="provider_reported", finish_reason="stop", resolved_model="m-1")
        assert result.resolved_model == "m-1"

    @pytest.mark.parametrize("name", ["input_tokens", "output_tokens", "total_tokens"])
    def test_negative_or_bool_tokens_rejected(self, name):
        with pytest.raises(ValueError):
            ChatResult(text="x", configured_model="m", model_evidence="configured_only", finish_reason="stop", **{name: -1})
        with pytest.raises(ValueError):
            ChatResult(text="x", configured_model="m", model_evidence="configured_only", finish_reason="stop", **{name: True})

    def test_content_rejected_is_a_finish_reason_not_an_exception(self):
        # A refusal is a normalized outcome, never a silent empty success.
        result = ChatResult(text="", configured_model="m", model_evidence="configured_only", finish_reason="content_rejected")
        assert result.finish_reason == "content_rejected"

    @pytest.mark.parametrize("bad", [{"provider": "data"}, ["x"], 123, "", "   ", None])
    def test_configured_model_must_be_nonblank_string(self, bad):
        with pytest.raises(ValueError):
            ChatResult(text="hi", configured_model=bad, model_evidence="configured_only", finish_reason="stop")

    def test_resolved_model_payload_rejected_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            ChatResult(text="hi", configured_model="m", model_evidence="provider_reported", finish_reason="stop", resolved_model={"leak": "sk-secret"})
        assert "sk-secret" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# EmbeddingRequest / EmbeddingResult                                          #
# --------------------------------------------------------------------------- #


class TestEmbeddingRecords:
    def test_empty_inputs_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=(), timeout_seconds=1.0)

    def test_non_string_input_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=(1,), timeout_seconds=1.0)  # type: ignore[arg-type]

    @pytest.mark.parametrize("input_type", EMBEDDING_INPUT_TYPES)
    def test_valid_input_types(self, input_type):
        assert EmbeddingRequest(inputs=("x",), timeout_seconds=1.0, input_type=input_type).input_type == input_type

    def test_unknown_input_type_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=("x",), timeout_seconds=1.0, input_type="passage")

    def test_default_input_type_is_document(self):
        assert EmbeddingRequest(inputs=("x",), timeout_seconds=1.0).input_type == "document"

    @pytest.mark.parametrize("policy", REQUEST_RETRY_POLICIES)
    def test_embedding_retry_policy_is_closed(self, policy):
        assert (
            EmbeddingRequest(
                inputs=("x",), timeout_seconds=1.0, retry_policy=policy
            ).retry_policy
            == policy
        )
        with pytest.raises(ValueError):
            EmbeddingRequest(
                inputs=("x",), timeout_seconds=1.0, retry_policy="forever"
            )

    def test_result_requires_nonempty_vectors(self):
        with pytest.raises(ValueError):
            EmbeddingResult(vectors=(), configured_model="m", model_evidence="configured_only", effective_dimensions=3)

    def test_embedding_result_correlation_id_is_validated(self):
        result = EmbeddingResult(
            vectors=((0.0,),),
            configured_model="m",
            model_evidence="configured_only",
            effective_dimensions=1,
            correlation_id="request-43",
        )
        assert result.correlation_id == "request-43"
        with pytest.raises(ValueError):
            dataclasses.replace(result, correlation_id="unsafe id")

    def test_result_configured_only_invariant(self):
        with pytest.raises(ValueError):
            EmbeddingResult(vectors=((0.0,),), configured_model="m", model_evidence="configured_only", effective_dimensions=1, resolved_model="m1")

    @pytest.mark.parametrize("dims", [0, -1, True])
    def test_result_effective_dimensions_positive_int(self, dims):
        with pytest.raises(ValueError):
            EmbeddingResult(vectors=((0.0,),), configured_model="m", model_evidence="configured_only", effective_dimensions=dims)

    def test_request_frozen(self):
        request = EmbeddingRequest(inputs=("x",), timeout_seconds=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.input_type = "query"  # type: ignore[misc]

    def test_inner_list_is_canonicalized_and_immutable(self):
        # A retained caller list must not be able to mutate a frozen result.
        mutable = [1.0, 2.0]
        result = EmbeddingResult(
            vectors=(mutable,), configured_model="m",
            model_evidence="configured_only", effective_dimensions=2,
        )
        assert result.vectors == ((1.0, 2.0),)
        assert isinstance(result.vectors[0], tuple)
        mutable[0] = 999.0  # mutating the original must not touch the record
        assert result.vectors == ((1.0, 2.0),)

    def test_outer_list_is_coerced_to_tuple(self):
        result = EmbeddingResult(
            vectors=[[1.0], [2.0]], configured_model="m",
            model_evidence="configured_only", effective_dimensions=1,
        )
        assert result.vectors == ((1.0,), (2.0,))

    def test_wrong_dimension_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0, 2.0),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=3,
            )

    def test_ragged_vectors_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0, 2.0), (3.0,)), configured_model="m",
                model_evidence="configured_only", effective_dimensions=2,
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_component_rejected(self, bad):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((bad,),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=1,
            )

    def test_bool_component_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((True,),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=1,
            )

    def test_non_numeric_component_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=(("x",),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=1,
            )

    def test_valid_multi_vector_result_all_tuples(self):
        result = EmbeddingResult(
            vectors=((1.0, 2.0), (3.0, 4.0)), configured_model="m",
            model_evidence="configured_only", effective_dimensions=2,
        )
        assert all(isinstance(v, tuple) for v in result.vectors)
        assert result.effective_dimensions == 2

    def test_configured_model_payload_rejected(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0,),), configured_model={"x": "y"},
                model_evidence="configured_only", effective_dimensions=1,
            )
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0,),), configured_model="  ",
                model_evidence="configured_only", effective_dimensions=1,
            )


# --------------------------------------------------------------------------- #
# Record secret-hygiene (repr) + fail-closed numeric totality                 #
# --------------------------------------------------------------------------- #


class TestRecordHygieneAndTotality:
    def test_content_fields_excluded_from_repr(self):
        # ADR-0027 forbids prompts/completions/documents/vectors in logs and
        # diagnostics; the default dataclass repr must never carry them.
        msg = ChatMessage(role="user", content="PROMPT-SECRET")
        req = ChatRequest(messages=(msg,), timeout_seconds=5.0)
        res = ChatResult(
            text="COMPLETION-SECRET", configured_model="m",
            model_evidence="configured_only", finish_reason="stop",
        )
        ereq = EmbeddingRequest(inputs=("DOCUMENT-SECRET",), timeout_seconds=5.0)
        eres = EmbeddingResult(
            vectors=((0.123456789,),), configured_model="m",
            model_evidence="configured_only", effective_dimensions=1,
        )
        for record, secret in [
            (msg, "PROMPT-SECRET"), (req, "PROMPT-SECRET"),
            (res, "COMPLETION-SECRET"), (ereq, "DOCUMENT-SECRET"),
            (eres, "0.123456789"),
        ]:
            assert secret not in repr(record)
        # Nested repr (a record inside a container) is also safe.
        assert "PROMPT-SECRET" not in repr([req])
        assert "COMPLETION-SECRET" not in repr({"r": res})

    @pytest.mark.parametrize("bad", ["secret", b"secret"])
    def test_embedding_request_rejects_scalar_str_bytes(self, bad):
        # tuple("secret") would silently become a per-character embedding batch.
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=bad, timeout_seconds=5.0)

    @pytest.mark.parametrize("bad", ["ab", b"ab"])
    def test_chat_request_rejects_scalar_str_bytes_messages(self, bad):
        with pytest.raises(ValueError):
            ChatRequest(messages=bad, timeout_seconds=5.0)

    def test_sequence_inputs_canonicalize_to_tuple(self):
        assert (
            EmbeddingRequest(inputs=["a", "b"], timeout_seconds=5.0).inputs
            == ("a", "b")
        )

    @pytest.mark.parametrize(
        "huge",
        [
            pytest.param(10**10000, id="pos-huge"),
            pytest.param(-(10**10000), id="neg-huge"),
        ],
    )
    def test_huge_int_timeout_fails_closed_not_overflow(self, huge):
        # math.isfinite(10**10000) raises OverflowError; numeric validation must
        # stay total and normalize it, never escape as a raw OverflowError.
        with pytest.raises(ValueError):
            ChatRequest(
                messages=(ChatMessage(role="user", content="x"),),
                timeout_seconds=huge,
            )

    def test_huge_int_vector_component_fails_closed_not_overflow(self):
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((10**10000,),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=1,
            )

    def test_probe_latency_huge_int_fails_closed(self):
        # Numeric totality reaches ProbeResult.latency_ms too (no raw OverflowError).
        with pytest.raises(ValueError):
            ProbeResult(
                connectivity="reachable", discovery="unsupported",
                latency_ms=10**10000,
            )

    def test_huge_int_fields_rejected_at_construction(self):
        # A huge int is rejected at CONSTRUCTION, so repr()/safe_snapshot()/
        # fingerprint serialization can never reach the CPython int-string limit.
        huge = 10**10000
        with pytest.raises(ValueError):
            ChatRequest(
                messages=(ChatMessage(role="user", content="x"),),
                timeout_seconds=5.0, max_output_tokens=huge,
            )
        with pytest.raises(ValueError):
            ChatResult(
                text="x", configured_model="m", model_evidence="configured_only",
                finish_reason="stop", total_tokens=huge,
            )
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0,),), configured_model="m",
                model_evidence="configured_only", effective_dimensions=huge,
            )

    def test_provider_reported_requires_resolved_model(self):
        # provider_reported must carry a non-blank resolved_model in both result
        # records; a positive case with the model is accepted.
        with pytest.raises(ValueError):
            ChatResult(
                text="x", configured_model="m", model_evidence="provider_reported",
                finish_reason="stop", resolved_model=None,
            )
        with pytest.raises(ValueError):
            EmbeddingResult(
                vectors=((1.0,),), configured_model="m",
                model_evidence="provider_reported", effective_dimensions=1,
                resolved_model=None,
            )
        assert ChatResult(
            text="x", configured_model="m", model_evidence="provider_reported",
            finish_reason="stop", resolved_model="srv",
        ).resolved_model == "srv"


# --------------------------------------------------------------------------- #
# ProbeResult                                                                 #
# --------------------------------------------------------------------------- #


class TestProbeResult:
    def test_discovery_only_semantics(self):
        # A successful listing proves discovery/connectivity, not readiness.
        probe = ProbeResult(connectivity="reachable", discovery="available", model_available=True)
        assert probe.healthy is True

    def test_unsupported_discovery_is_healthy_not_a_failure(self):
        probe = ProbeResult(connectivity="reachable", discovery="unsupported", model_available=None)
        assert probe.healthy is True

    def test_error_discovery_is_not_healthy(self):
        assert ProbeResult(connectivity="reachable", discovery="error").healthy is False

    def test_unreachable_is_not_healthy(self):
        assert ProbeResult(connectivity="unreachable", discovery="not_run").healthy is False

    @pytest.mark.parametrize("connectivity", ["ok", "up", ""])
    def test_invalid_connectivity_rejected(self, connectivity):
        with pytest.raises(ValueError):
            ProbeResult(connectivity=connectivity, discovery="available")

    @pytest.mark.parametrize("discovery", ["listed", "missing", ""])
    def test_invalid_discovery_rejected(self, discovery):
        with pytest.raises(ValueError):
            ProbeResult(connectivity="reachable", discovery=discovery)

    def test_model_available_defaults_none_for_non_available_discovery(self):
        assert (
            ProbeResult(connectivity="reachable", discovery="unsupported").model_available
            is None
        )

    def test_available_discovery_requires_bool_model_available(self):
        # A successful listing must report whether the model was found.
        with pytest.raises(ValueError):
            ProbeResult(connectivity="reachable", discovery="available")
        assert (
            ProbeResult(
                connectivity="reachable", discovery="available", model_available=False
            ).model_available
            is False
        )

    def test_error_category_accepts_safe_set_and_none(self):
        assert ProbeResult(
            connectivity="reachable", discovery="error", error_category="auth"
        ).error_category == "auth"
        assert ProbeResult(
            connectivity="reachable", discovery="available", model_available=True
        ).error_category is None

    def test_error_category_rejects_arbitrary_payload_and_is_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            ProbeResult(
                connectivity="reachable",
                discovery="error",
                error_category="provider-body: bearer sk-live-secret",
            )
        assert "sk-live-secret" not in str(excinfo.value)

    def test_planted_payload_never_appears_in_repr(self):
        # error_category can only ever be a safe token, so the record repr —
        # an outward diagnostic surface — is always secret-free.
        probe = ProbeResult(
            connectivity="reachable", discovery="error", error_category="quota_exhausted"
        )
        assert "quota_exhausted" in repr(probe)

    def test_model_available_requires_available_discovery(self):
        with pytest.raises(ValueError):
            ProbeResult(
                connectivity="reachable", discovery="unsupported", model_available=True
            )

    def test_unreachable_cannot_report_available_discovery(self):
        with pytest.raises(ValueError):
            ProbeResult(connectivity="unreachable", discovery="available")

    def test_unreachable_with_error_discovery_and_category_is_coherent(self):
        probe = ProbeResult(
            connectivity="unreachable", discovery="error", error_category="unavailable"
        )
        assert probe.healthy is False

    @pytest.mark.parametrize(
        "bad", ["provider-payload-secret", float("nan"), float("inf"), -1.0, True]
    )
    def test_latency_ms_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            ProbeResult(connectivity="reachable", discovery="unsupported", latency_ms=bad)

    def test_latency_ms_string_payload_rejected_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            ProbeResult(
                connectivity="reachable",
                discovery="unsupported",
                latency_ms="provider-payload-secret",
            )
        assert "provider-payload-secret" not in str(excinfo.value)

    def test_latency_ms_accepts_none_and_nonnegative(self):
        assert (
            ProbeResult(
                connectivity="reachable", discovery="unsupported", latency_ms=12.5
            ).latency_ms
            == 12.5
        )
        assert (
            ProbeResult(
                connectivity="reachable", discovery="unsupported", latency_ms=0
            ).latency_ms
            == 0
        )

    def test_error_category_cannot_accompany_a_healthy_state(self):
        # A contradictory diagnostic (error category on a reachable, successful
        # or unsupported probe) must not be constructible.
        with pytest.raises(ValueError):
            ProbeResult(
                connectivity="reachable", discovery="available",
                model_available=True, error_category="auth",
            )
        with pytest.raises(ValueError):
            ProbeResult(
                connectivity="reachable", discovery="unsupported", error_category="auth"
            )

    def test_error_category_allowed_only_on_error_or_unreachable(self):
        assert (
            ProbeResult(
                connectivity="reachable", discovery="error", error_category="auth"
            ).healthy
            is False
        )
        assert (
            ProbeResult(
                connectivity="unreachable", discovery="not_run",
                error_category="unavailable",
            ).healthy
            is False
        )


# --------------------------------------------------------------------------- #
# InferenceError safe envelope                                                #
# --------------------------------------------------------------------------- #


class TestInferenceError:
    def _error(self, **overrides):
        base = dict(category="auth", role="chat", provider_id="cloud-temple", adapter_id="openai-compatible", retryable=False, correlation_id="abc123")
        base.update(overrides)
        return InferenceError(**base)

    def test_error_categories_frozen(self):
        assert ERROR_CATEGORIES == (
            "auth", "quota_exhausted", "rate_limited", "timeout", "unsupported",
            "invalid_request", "content_rejected", "invalid_response", "unavailable",
        )

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError):
            self._error(category="boom")

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError):
            self._error(role="probe")

    def test_safe_payload_has_exactly_the_six_fields(self):
        payload = self._error().safe_payload()
        assert set(payload) == {"category", "role", "provider_id", "adapter_id", "retryable", "correlation_id"}

    def test_str_is_built_only_from_safe_fields(self):
        error = self._error(correlation_id="corr-42")
        rendered = str(error)
        # The message is deterministic from the six safe fields.
        assert "category=auth" in rendered
        assert "provider=cloud-temple" in rendered
        assert "adapter=openai-compatible" in rendered
        assert "retryable=false" in rendered
        assert "corr-42" in rendered

    def test_no_channel_for_provider_text(self):
        # The constructor accepts ONLY the six safe keyword fields — there is no
        # parameter through which a raw provider message/response/URL/key could
        # enter the envelope.
        import inspect

        params = set(inspect.signature(InferenceError.__init__).parameters) - {"self"}
        assert params == {"category", "role", "provider_id", "adapter_id", "retryable", "correlation_id"}
        for leak_channel in ("message", "detail", "response", "body", "exception", "cause", "url", "headers"):
            assert leak_channel not in params
        # Persisted state is exactly those six fields — no hidden attribute.
        assert set(InferenceError.__slots__) == {"category", "role", "provider_id", "adapter_id", "retryable", "correlation_id"}

    def test_immutable(self):
        error = self._error()
        with pytest.raises(AttributeError):
            error.category = "timeout"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del error.category

    def test_retryable_flag_recorded(self):
        assert self._error(category="rate_limited", retryable=True).retryable is True

    def test_provider_id_must_be_registered_and_rejection_is_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            self._error(provider_id="https://sk-secret-key@llm.internal/v1")
        assert "sk-secret-key" not in str(excinfo.value)
        assert "llm.internal" not in str(excinfo.value)

    def test_adapter_id_must_be_registered(self):
        with pytest.raises(ValueError) as excinfo:
            self._error(adapter_id="http://leak.example/v1?token=abc")
        assert "leak.example" not in str(excinfo.value)
        assert "token=abc" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad", ["id with spaces", "https://x:pw@h/v1", "a" * 129, "line\nbreak"]
    )
    def test_correlation_id_grammar_enforced_and_value_free(self, bad):
        with pytest.raises(ValueError) as excinfo:
            self._error(correlation_id=bad)
        assert bad not in str(excinfo.value)

    def test_empty_correlation_id_rejected(self):
        with pytest.raises(ValueError):
            self._error(correlation_id="")

    @pytest.mark.parametrize("bad", ["yes", 1, None, 0])
    def test_retryable_must_be_bool(self, bad):
        with pytest.raises(ValueError):
            self._error(retryable=bad)

    def test_planted_secret_cannot_reach_str_or_safe_payload(self):
        # There is no path to construct an envelope whose rendered form carries
        # a URL/credential: every outward field is validated first.
        for field in ("provider_id", "adapter_id", "correlation_id"):
            with pytest.raises(ValueError):
                self._error(**{field: "https://user:hunter2@api.example.com/v1"})

    def test_unknown_category_rejection_is_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            self._error(category="provider-payload: synthetic-secret")
        exc = excinfo.value
        while exc is not None:
            assert "synthetic-secret" not in repr(exc)
            exc = exc.__cause__ or exc.__context__

    def test_unknown_role_rejection_is_value_free(self):
        with pytest.raises(ValueError) as excinfo:
            self._error(role="secret-role-payload")
        exc = excinfo.value
        while exc is not None:
            assert "secret-role-payload" not in repr(exc)
            exc = exc.__cause__ or exc.__context__

    def test_adapter_must_match_registry_mapping_for_provider(self):
        # anthropic's registered adapter is "anthropic"; pairing it with
        # openai-compatible is a contradictory identity and is rejected.
        with pytest.raises(ValueError):
            self._error(provider_id="anthropic", adapter_id="openai-compatible")

    def test_embedding_error_rejects_anthropic_provider(self):
        with pytest.raises(ValueError):
            InferenceError(
                category="invalid_response",
                role="embedding",
                provider_id="anthropic",
                adapter_id="anthropic",
                retryable=False,
                correlation_id="c",
            )


# --------------------------------------------------------------------------- #
# Profile identity edge cases                                                 #
# --------------------------------------------------------------------------- #


class TestProfileIdentity:
    def _embedding(self, **overrides):
        base = dict(provider_id="cloud-temple", adapter_id="openai-compatible", endpoint="https://api.ai.cloud-temple.com/v1", api_key="k", configured_model="bge-m3:567m", expected_dimensions=1024)
        base.update(overrides)
        return ResolvedEmbeddingProfile(**base)

    def test_invalid_source_rejected(self):
        with pytest.raises(ValueError):
            ResolvedChatProfile(provider_id="openai", adapter_id="openai-compatible", endpoint="https://api.openai.com/v1", api_key="k", configured_model="m", context_window=100, max_output_tokens=10, source="hand-made")

    def test_profiles_frozen_and_secret_free_repr(self):
        profile = self._embedding()
        with pytest.raises(dataclasses.FrozenInstanceError):
            profile.api_key = "other"  # type: ignore[misc]
        rendered = repr(profile)
        assert "k" not in [profile.provider_id]  # sanity
        assert "api.ai.cloud-temple.com" not in rendered
        assert "api_key" not in rendered or "'k'" not in rendered

    def test_safe_snapshot_excludes_endpoint_and_key(self):
        snapshot = self._embedding().safe_snapshot()
        assert "endpoint" not in snapshot
        assert "api_key" not in snapshot
        assert snapshot["provider_id"] == "cloud-temple"

    def test_endpoint_normalization_case_default_port_trailing_slash(self):
        assert endpoint_sha256("https://API.Example.com:443/v1/") == endpoint_sha256("https://api.example.com/v1")

    def test_endpoint_identity_scoped_by_path_and_port(self):
        base = endpoint_sha256("https://api.example.com/v1")
        assert base != endpoint_sha256("https://api.example.com/v2")
        assert base != endpoint_sha256("https://api.example.com:8443/v1")
        assert base != endpoint_sha256("http://api.example.com/v1")

    def test_repeated_slash_collides_at_hash_so_profiles_reject_it(self):
        # endpoint_sha256 collapses trailing slashes, so '/v1//' and '/v1' hash
        # IDENTICALLY even though a path-sensitive gateway routes them
        # differently. The collision is therefore closed UPSTREAM: no valid
        # profile can carry a repeated trailing slash, so two distinct routes
        # can never both mint a fingerprint. This proves the collision exists
        # (mutation evidence) and that construction fails closed value-free.
        assert endpoint_sha256("https://gw.example/v1//") == endpoint_sha256(
            "https://gw.example/v1"
        )
        with pytest.raises(ValueError) as excinfo:
            ResolvedChatProfile(
                provider_id="openai-compatible",
                adapter_id="openai-compatible",
                endpoint="https://gw.example/v1//",
                api_key="k",
                configured_model="m",
                context_window=100,
                max_output_tokens=10,
            )
        assert "repeated trailing slashes" in str(excinfo.value)

    def test_ipv6_endpoint_default_port_and_injectivity(self):
        # The structured canonical tuple disambiguates IPv6 host/port pairs that
        # a textual "host:port" concatenation could otherwise confuse.
        assert endpoint_sha256("https://[2001:db8::1]/v1") == endpoint_sha256("https://[2001:db8::1]:443/v1")
        assert endpoint_sha256("https://[2001:db8::1]:443/v1") != endpoint_sha256("https://[2001:db8::1]:8443/v1")
        assert endpoint_sha256("https://[2001:db8::1]/v1") != endpoint_sha256("https://[2001:db8::2]/v1")

    def test_fingerprint_configured_only_forbids_resolved_model(self):
        with pytest.raises(ValueError):
            embedding_profile_fingerprint(self._embedding(), resolved_model="m", model_evidence="configured_only")

    def test_fingerprint_changes_with_resolved_model_and_evidence(self):
        profile = self._embedding()
        base = embedding_profile_fingerprint(profile)
        reported = embedding_profile_fingerprint(profile, resolved_model="server-model", model_evidence="provider_reported")
        digest = embedding_profile_fingerprint(profile, model_evidence="immutable_digest")
        assert base != reported != digest and base != digest

    def test_fingerprint_provider_reported_requires_resolved_model(self):
        with pytest.raises(ValueError):
            embedding_profile_fingerprint(
                self._embedding(), model_evidence="provider_reported",
                resolved_model=None,
            )

    def test_huge_dimensions_rejected_so_repr_and_fingerprint_cannot_crash(self):
        # A huge expected_dimensions is rejected at construction (U2), so
        # repr()/safe_snapshot()/embedding_profile_fingerprint() can never reach
        # the CPython integer-string digit limit.
        with pytest.raises(ValueError):
            self._embedding(expected_dimensions=10**10000)
        # A valid (large-but-bounded) profile serializes without raising.
        profile = self._embedding(expected_dimensions=4096)
        assert isinstance(repr(profile), str)
        assert len(embedding_profile_fingerprint(profile)) == 64

    @pytest.mark.parametrize(
        "bad, secret",
        [
            ("https://proxy.local:C0NFIGSECRET/v1", "C0NFIGSECRET"),
            ("https://[bad::v6::S3CRETX]/v1", "S3CRETX"),
            ("https://user:HUNTER2PW@h.example/v1", "HUNTER2PW"),
            ("https://h.example/v1?token=T0KENLEAK", "T0KENLEAK"),
            ("https://h.example/v1#FRAGLEAK", "FRAGLEAK"),
        ],
    )
    def test_profile_rejects_unsafe_endpoint_value_free(self, bad, secret):
        # Resolved profiles are public API: a directly built profile with a
        # malformed or credential-bearing endpoint fails closed, and the value
        # never appears in the exception chain.
        with pytest.raises(ValueError) as excinfo:
            self._embedding(endpoint=bad)
        exc = excinfo.value
        while exc is not None:
            assert secret not in repr(exc)
            exc = exc.__cause__ or exc.__context__

    @pytest.mark.parametrize(
        "bad, secret",
        [
            ("https://h.example:C0NFIGSECRET/v1", "C0NFIGSECRET"),
            ("https://[bad::v6::S3CRETY]/v1", "S3CRETY"),
        ],
    )
    def test_endpoint_sha256_rejects_malformed_value_free(self, bad, secret):
        with pytest.raises(ValueError) as excinfo:
            endpoint_sha256(bad)
        exc = excinfo.value
        while exc is not None:
            assert secret not in repr(exc)
            exc = exc.__cause__ or exc.__context__

    def test_profile_rejects_provider_adapter_mismatch(self):
        with pytest.raises(ValueError):
            ResolvedEmbeddingProfile(
                provider_id="cloud-temple", adapter_id="anthropic",
                endpoint="https://api.ai.cloud-temple.com/v1", api_key="k",
                configured_model="m", expected_dimensions=1024,
            )

    def test_embedding_profile_rejects_anthropic_role(self):
        with pytest.raises(ValueError):
            ResolvedEmbeddingProfile(
                provider_id="anthropic", adapter_id="anthropic",
                endpoint="https://api.anthropic.com", api_key="k",
                configured_model="m", expected_dimensions=1024,
            )

    def test_profile_rejects_unregistered_provider(self):
        with pytest.raises(ValueError):
            self._embedding(provider_id="surprise-ai", adapter_id="openai-compatible")

    @pytest.mark.parametrize("dims", [0, -1, True])
    def test_embedding_profile_rejects_nonpositive_dimensions(self, dims):
        with pytest.raises(ValueError):
            self._embedding(expected_dimensions=dims)

    def test_profile_rejects_blank_model_or_key(self):
        with pytest.raises(ValueError):
            self._embedding(configured_model="   ")
        with pytest.raises(ValueError):
            self._embedding(api_key="")
        with pytest.raises(ValueError):  # whitespace-only key is still blank
            self._embedding(api_key="   ")

    def test_chat_profile_rejects_bad_ceilings_and_temperature(self):
        base = dict(
            provider_id="openai", adapter_id="openai-compatible",
            endpoint="https://api.openai.com/v1", api_key="k",
            configured_model="m", context_window=100, max_output_tokens=10,
        )
        with pytest.raises(ValueError):  # output >= context
            ResolvedChatProfile(**{**base, "max_output_tokens": 100})
        with pytest.raises(ValueError, match="supported generation budget"):
            ResolvedChatProfile(
                **{
                    **base,
                    "context_window": MAX_CHAT_GENERATION_TOKENS + 2,
                    "max_output_tokens": MAX_CHAT_GENERATION_TOKENS + 1,
                }
            )
        with pytest.raises(ValueError):  # temperature out of openai-compatible range
            ResolvedChatProfile(**base, temperature=2.5)
        with pytest.raises(ValueError):  # anthropic range is tighter (<= 1.0)
            ResolvedChatProfile(
                provider_id="anthropic", adapter_id="anthropic",
                endpoint="https://api.anthropic.com", api_key="k",
                configured_model="m", context_window=100, max_output_tokens=10,
                temperature=1.5,
            )

    def test_valid_direct_chat_profile_is_accepted(self):
        # Use a generic, non-codename model name: this file ships in the public
        # tree, so it must not embed internal model or reviewer identifiers.
        profile = ResolvedChatProfile(
            provider_id="openai", adapter_id="openai-compatible",
            endpoint="https://api.openai.com/v1", api_key="k",
            configured_model="example-chat-model", context_window=131072,
            max_output_tokens=16384, temperature=0.7,
        )
        assert profile.adapter_id == "openai-compatible"

    def test_direct_chat_profile_accepts_exact_generation_budget_boundary(self):
        profile = ResolvedChatProfile(
            provider_id="openai-compatible",
            adapter_id="openai-compatible",
            endpoint="https://gateway.example/v1",
            api_key="k",
            configured_model="example-chat-model",
            context_window=MAX_CHAT_GENERATION_TOKENS + 1,
            max_output_tokens=MAX_CHAT_GENERATION_TOKENS,
        )
        assert profile.max_output_tokens == MAX_CHAT_GENERATION_TOKENS

    def test_fingerprint_rejects_unknown_model_evidence(self):
        with pytest.raises(ValueError):
            embedding_profile_fingerprint(self._embedding(), model_evidence="totally-made-up")

    @pytest.mark.parametrize(
        "provider, endpoint",
        [
            ("openai", "http://evil.example/v1"),         # wrong scheme + host
            ("openai", "https://evil.example/v1"),        # wrong host
            ("openai", "https://api.openai.com/v2"),      # wrong path
            ("openai", "https://api.openai.com:8443/v1"),  # wrong port
            ("cloud-temple", "https://api.openai.com/v1"),  # host of another provider
            ("gemini", "https://generativelanguage.googleapis.com/v1/openai"),
            ("anthropic", "https://api.anthropic.com/v1"),  # anthropic base must be bare
        ],
    )
    def test_named_provider_profile_pins_host_path_scheme(self, provider, endpoint):
        # A directly built named-provider profile must satisfy the SAME endpoint
        # policy config enforces, so a key cannot be sent to an arbitrary host.
        adapter = "anthropic" if provider == "anthropic" else "openai-compatible"
        with pytest.raises(ValueError):
            ResolvedChatProfile(
                provider_id=provider, adapter_id=adapter, endpoint=endpoint,
                api_key="k", configured_model="m", context_window=4096,
                max_output_tokens=1024,
            )

    def test_generic_and_ollama_profiles_accept_operator_endpoint(self):
        assert ResolvedChatProfile(
            provider_id="openai-compatible", adapter_id="openai-compatible",
            endpoint="http://gateway.local:4000/anything/v9", api_key="k",
            configured_model="m", context_window=4096, max_output_tokens=1024,
        ).provider_id == "openai-compatible"
        assert ResolvedChatProfile(
            provider_id="ollama", adapter_id="openai-compatible",
            endpoint="http://host.docker.internal:11434/v1", api_key="ollama",
            configured_model="m", context_window=4096, max_output_tokens=1024,
        ).provider_id == "ollama"


# --------------------------------------------------------------------------- #
# Registry map + adapter_for_provider resolution                              #
# --------------------------------------------------------------------------- #


class TestRegistryResolution:
    @pytest.mark.parametrize("provider_id", sorted(PROVIDER_TO_ADAPTER))
    def test_chat_resolution_matches_table(self, provider_id):
        assert adapter_for_provider(provider_id, role="chat") == PROVIDER_TO_ADAPTER[provider_id]

    @pytest.mark.parametrize("provider_id", sorted(EMBEDDING_PROVIDER_IDS))
    def test_embedding_resolution_matches_table(self, provider_id):
        assert adapter_for_provider(provider_id, role="embedding") == PROVIDER_TO_ADAPTER[provider_id]

    def test_unknown_provider_fails_closed(self):
        with pytest.raises(ValueError):
            adapter_for_provider("surprise-ai", role="chat")

    def test_anthropic_embedding_fails_closed(self):
        with pytest.raises(ValueError):
            adapter_for_provider("anthropic", role="embedding")

    def test_unknown_role_fails_closed(self):
        with pytest.raises(ValueError):
            adapter_for_provider("openai", role="reasoning")

    def test_provider_to_adapter_table_frozen(self):
        assert PROVIDER_TO_ADAPTER == {
            "cloud-temple": "openai-compatible",
            "scaleway": "openai-compatible",
            "openai": "openai-compatible",
            "mistral": "openai-compatible",
            "gemini": "openai-compatible",
            "ovhcloud": "openai-compatible",
            "anthropic": "anthropic",
            "ollama": "openai-compatible",
            "openai-compatible": "openai-compatible",
        }

    def test_adr_provider_table_matches_the_runtime_registry(self):
        adr_path = ROOT / "docs/adr/0027-provider-neutral-inference-boundary.md"
        if not adr_path.exists():
            pytest.skip(
                "docs/adr/0027-provider-neutral-inference-boundary.md is "
                "private-only (absent from the public release tree)"
            )
        adr = adr_path.read_text(encoding="utf-8")
        section = adr.split(
            "### Provider identifiers and adapter resolution", 1
        )[1].split("### Split configuration and legacy selection", 1)[0]
        rows = re.findall(
            r"^\| `([^`]+)` \| `([^`]+)` \|",
            section,
            flags=re.MULTILINE,
        )
        assert dict(rows) == dict(PROVIDER_TO_ADAPTER)

    def test_role_maps_frozen(self):
        assert set(CHAT_PROVIDER_IDS) == set(PROVIDER_TO_ADAPTER)
        assert set(EMBEDDING_PROVIDER_IDS) == set(PROVIDER_TO_ADAPTER) - {"anthropic"}

    def test_hosted_host_and_path_maps_frozen(self):
        assert HOSTED_PROVIDER_HOSTS == {
            "cloud-temple": "api.ai.cloud-temple.com",
            "scaleway": "api.scaleway.ai",
            "openai": "api.openai.com",
            "mistral": "api.mistral.ai",
            "gemini": "generativelanguage.googleapis.com",
            "ovhcloud": "oai.endpoints.kepler.ai.cloud.ovh.net",
            "anthropic": "api.anthropic.com",
        }
        assert HOSTED_PROVIDER_PATHS == {
            "cloud-temple": "/v1",
            "scaleway": "/v1",
            "openai": "/v1",
            "mistral": "/v1",
            "gemini": "/v1beta/openai",
            "ovhcloud": "/v1",
            "anthropic": "",
        }
        # ollama and the generic profile are deliberately NOT pinned to a host.
        assert "ollama" not in HOSTED_PROVIDER_HOSTS
        assert "openai-compatible" not in HOSTED_PROVIDER_HOSTS

    def test_temperature_ranges_frozen(self):
        assert ADAPTER_TEMPERATURE_RANGES == {
            "openai-compatible": (0.0, 2.0),
            "anthropic": (0.0, 1.0),
        }

    @pytest.mark.parametrize(
        "mapping",
        [
            PROVIDER_TO_ADAPTER,
            HOSTED_PROVIDER_HOSTS,
            HOSTED_PROVIDER_PATHS,
            ADAPTER_TEMPERATURE_RANGES,
        ],
    )
    def test_registry_maps_reject_mutation(self, mapping):
        # The frozen provider identity cannot be remapped by any importer.
        with pytest.raises(TypeError):
            mapping["openai"] = "anthropic"  # type: ignore[index]
        with pytest.raises(TypeError):
            del mapping["openai"]  # type: ignore[misc]

    def test_resolution_stays_canonical_despite_public_exposure(self):
        assert adapter_for_provider("openai", role="chat") == "openai-compatible"
        assert adapter_for_provider("anthropic", role="chat") == "anthropic"

    def test_no_addressable_mutable_backing_dict(self):
        # The proxy is built from an unreferenced literal, so there is no
        # module-private dict an importer could mutate to subvert resolution.
        import hivemind_inference.registry as reg

        assert not hasattr(reg, "_PROVIDER_TO_ADAPTER")
