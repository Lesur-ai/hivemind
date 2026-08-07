# -*- coding: utf-8 -*-
"""P13-1 (#261) — split configuration, strict LLMAAS_* legacy path, registry.

Table-driven conformance for the frozen ADR-0027 migration contract:

- positive: no inference variables (both roles unavailable), complete legacy
  pair with defaults, complete legacy with every explicit optional, complete
  new chat + embedding, intentionally absent whole role, two distinct
  explicit providers;
- negative: missing or empty required field, any legacy/new coexistence,
  unknown provider, ``anthropic`` embeddings, invalid URL component/scheme,
  invalid context/output/temperature/dimensions;
- mutation: delete AND empty each required variable, insert any legacy
  variable into a new profile and any new variable into legacy, the frozen
  provider-to-adapter/role map, numeric boundary crossings, and secret-free
  diagnostics for planted credentials.

Everything runs on explicit environment mappings — no process env, no
``.env`` file, no network.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from hivemind_inference import (
    CHAT_PROVIDER_IDS,
    EMBEDDING_PROVIDER_IDS,
    InferenceConfigError,
    PROVIDER_TO_ADAPTER,
    embedding_profile_fingerprint,
    endpoint_sha256,
    merged_environment,
    resolve_inference_config,
)
from hivemind_inference.config import (
    CHAT_REQUIRED_VARIABLES,
    EMBEDDING_REQUIRED_VARIABLES,
    LEGACY_KNOWN_VARIABLES,
    _reset_legacy_deprecation_warning_for_tests,
)
from hivemind_inference.profiles import MAX_CHAT_GENERATION_TOKENS
from hivemind_inference.records import ChatMessage, ChatRequest, EmbeddingRequest

NEW_CHAT = {
    "INFERENCE_CHAT_PROVIDER": "cloud-temple",
    "INFERENCE_CHAT_API_URL": "https://api.ai.cloud-temple.com/v1",
    "INFERENCE_CHAT_API_KEY": "chat-key",
    "INFERENCE_CHAT_MODEL": "qwen3.6:27b",
    "INFERENCE_CHAT_CONTEXT_WINDOW": "131072",
    "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "16384",
}
NEW_EMBEDDING = {
    "INFERENCE_EMBEDDING_PROVIDER": "cloud-temple",
    "INFERENCE_EMBEDDING_API_URL": "https://api.ai.cloud-temple.com/v1",
    "INFERENCE_EMBEDDING_API_KEY": "embed-key",
    "INFERENCE_EMBEDDING_MODEL": "bge-m3:567m",
    "INFERENCE_EMBEDDING_DIMENSIONS": "1024",
}
LEGACY_PAIR = {
    "LLMAAS_API_URL": "https://api.ai.cloud-temple.com/v1",
    "LLMAAS_API_KEY": "legacy-key",
}


def _resolve(env):
    return resolve_inference_config(env)


def _errors(env) -> list[str]:
    with pytest.raises(InferenceConfigError) as excinfo:
        resolve_inference_config(env)
    return excinfo.value.errors


# --------------------------------------------------------------------------- #
# Positive migration classes                                                  #
# --------------------------------------------------------------------------- #


class TestPositiveResolution:
    def test_no_inference_variables_means_both_roles_unavailable(self):
        config = _resolve({})
        assert config.chat is None
        assert config.embedding is None
        assert config.legacy_active is False
        assert config.configured_roles == ()

    def test_unrelated_variables_are_ignored(self):
        config = _resolve({"PATH": "/usr/bin", "S3_ENDPOINT_URL": "http://s3"})
        assert config.configured_roles == ()

    def test_historical_empty_pair_with_tunables_stays_unconfigured(self):
        # .env.example ships LLMAAS_API_URL= / LLMAAS_API_KEY= empty alongside
        # non-empty tunables: 1.x treated that as a VALID unconfigured start.
        config = _resolve(
            {
                "LLMAAS_API_URL": "",
                "LLMAAS_API_KEY": "",
                "LLMAAS_MODEL": "qwen3.5:27b",
                "LLMAAS_TEMPERATURE": "0.3",
            }
        )
        assert config.configured_roles == ()
        assert config.legacy_active is False

    def test_complete_legacy_pair_applies_frozen_defaults(self):
        config = _resolve(dict(LEGACY_PAIR))
        assert config.legacy_active is True
        chat = config.chat
        embedding = config.embedding
        assert chat is not None and embedding is not None
        assert chat.provider_id == "openai-compatible"
        assert chat.adapter_id == "openai-compatible"
        assert chat.configured_model == "qwen3.5:27b"
        assert chat.context_window == 131072
        assert chat.max_output_tokens == 16384
        assert chat.temperature == 0.3
        assert chat.source == "llmaas-legacy"
        assert embedding.provider_id == "openai-compatible"
        assert embedding.configured_model == "bge-m3:567m"
        assert embedding.expected_dimensions == 1024
        assert embedding.source == "llmaas-legacy"

    def test_complete_legacy_with_every_explicit_optional(self):
        config = _resolve(
            {
                **LEGACY_PAIR,
                "LLMAAS_MODEL": "custom-chat",
                "LLMAAS_CONTEXT_WINDOW": "65536",
                "LLMAAS_MAX_TOKENS": "2048",
                "LLMAAS_TEMPERATURE": "1.5",
                "LLMAAS_EMBEDDING_MODEL": "custom-embed",
                "LLMAAS_EMBEDDING_DIMENSIONS": "512",
            }
        )
        assert config.chat.configured_model == "custom-chat"
        assert config.chat.context_window == 65536
        assert config.chat.max_output_tokens == 2048
        assert config.chat.temperature == 1.5
        assert config.embedding.configured_model == "custom-embed"
        assert config.embedding.expected_dimensions == 512

    def test_legacy_reasoning_budget_above_old_byte_heuristic_resolves(self):
        config = _resolve(
            {
                **LEGACY_PAIR,
                "LLMAAS_CONTEXT_WINDOW": "1000000",
                "LLMAAS_MAX_TOKENS": "200000",
            }
        )
        assert config.chat.context_window == 1_000_000
        assert config.chat.max_output_tokens == 200_000

    @pytest.mark.parametrize("family", ["split", "legacy"])
    def test_exact_million_token_generation_budget_resolves(self, family):
        if family == "split":
            env = {
                **NEW_CHAT,
                "INFERENCE_CHAT_CONTEXT_WINDOW": "1000001",
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1000000",
            }
        else:
            env = {
                **LEGACY_PAIR,
                "LLMAAS_CONTEXT_WINDOW": "1000001",
                "LLMAAS_MAX_TOKENS": "1000000",
            }
        assert _resolve(env).chat.max_output_tokens == MAX_CHAT_GENERATION_TOKENS

    def test_legacy_names_match_case_insensitively(self):
        # pydantic-settings historically resolved llmaas_* case-insensitively.
        config = _resolve(
            {
                "llmaas_api_url": "https://api.ai.cloud-temple.com/v1",
                "llmaas_api_key": "legacy-key",
            }
        )
        assert config.legacy_active is True

    def test_complete_new_chat_and_embedding(self):
        config = _resolve({**NEW_CHAT, **NEW_EMBEDDING})
        assert config.legacy_active is False
        assert config.configured_roles == ("chat", "embedding")
        assert config.chat.provider_id == "cloud-temple"
        assert config.chat.temperature is None  # omitted → omitted on wire
        assert config.embedding.expected_dimensions == 1024

    def test_intentionally_absent_embedding_role(self):
        config = _resolve(dict(NEW_CHAT))
        assert config.configured_roles == ("chat",)
        assert config.embedding is None

    def test_intentionally_absent_chat_role(self):
        config = _resolve(dict(NEW_EMBEDDING))
        assert config.configured_roles == ("embedding",)
        assert config.chat is None

    def test_two_distinct_explicit_providers(self):
        env = {
            "INFERENCE_CHAT_PROVIDER": "anthropic",
            "INFERENCE_CHAT_API_URL": "https://api.anthropic.com",
            "INFERENCE_CHAT_API_KEY": "anthropic-key",
            "INFERENCE_CHAT_MODEL": "claude-sonnet-5",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "131072",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "16384",
            **NEW_EMBEDDING,
        }
        config = _resolve(env)
        assert config.chat.provider_id == "anthropic"
        assert config.chat.adapter_id == "anthropic"
        assert config.embedding.provider_id == "cloud-temple"
        assert config.embedding.adapter_id == "openai-compatible"

    def test_optional_temperature_is_validated_when_present(self):
        config = _resolve({**NEW_CHAT, "INFERENCE_CHAT_TEMPERATURE": "0.7"})
        assert config.chat.temperature == 0.7

    def test_ollama_local_http_endpoint_accepted(self):
        env = {
            "INFERENCE_CHAT_PROVIDER": "ollama",
            "INFERENCE_CHAT_API_URL": "http://host.docker.internal:11434/v1",
            "INFERENCE_CHAT_API_KEY": "ollama",
            "INFERENCE_CHAT_MODEL": "qwen3:4b",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "8192",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "2048",
        }
        assert _resolve(env).chat.provider_id == "ollama"

    def test_generic_profile_accepts_explicit_http_endpoint(self):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "openai-compatible",
            "INFERENCE_CHAT_API_URL": "http://127.0.0.1:8080/v1",
        }
        assert _resolve(env).chat.adapter_id == "openai-compatible"


# --------------------------------------------------------------------------- #
# Negative and mutation classes                                               #
# --------------------------------------------------------------------------- #


class TestNegativeResolution:
    @pytest.mark.parametrize("variable", sorted(CHAT_REQUIRED_VARIABLES))
    def test_each_chat_required_variable_deleted_fails(self, variable):
        env = {**NEW_CHAT, **NEW_EMBEDDING}
        del env[variable]
        errors = _errors(env)
        assert any(variable in error for error in errors)

    @pytest.mark.parametrize("variable", sorted(CHAT_REQUIRED_VARIABLES))
    def test_each_chat_required_variable_emptied_fails(self, variable):
        env = {**NEW_CHAT, **NEW_EMBEDDING, variable: ""}
        errors = _errors(env)
        assert any(variable in error for error in errors)

    @pytest.mark.parametrize("variable", sorted(EMBEDDING_REQUIRED_VARIABLES))
    def test_each_embedding_required_variable_deleted_fails(self, variable):
        env = {**NEW_CHAT, **NEW_EMBEDDING}
        del env[variable]
        errors = _errors(env)
        assert any(variable in error for error in errors)

    @pytest.mark.parametrize("variable", sorted(EMBEDDING_REQUIRED_VARIABLES))
    def test_each_embedding_required_variable_emptied_fails(self, variable):
        env = {**NEW_CHAT, **NEW_EMBEDDING, variable: ""}
        errors = _errors(env)
        assert any(variable in error for error in errors)

    @pytest.mark.parametrize("legacy_variable", sorted(LEGACY_KNOWN_VARIABLES))
    def test_any_legacy_variable_inserted_into_new_profile_fails(
        self, legacy_variable
    ):
        env = {**NEW_CHAT, **NEW_EMBEDDING, legacy_variable: "anything"}
        errors = _errors(env)
        assert any("must not coexist" in error for error in errors)
        assert any(legacy_variable in error for error in errors)

    @pytest.mark.parametrize(
        "new_variable", sorted({**NEW_CHAT, **NEW_EMBEDDING})
    )
    def test_any_new_variable_inserted_into_legacy_fails(self, new_variable):
        env = {**LEGACY_PAIR, new_variable: {**NEW_CHAT, **NEW_EMBEDDING}[new_variable]}
        errors = _errors(env)
        assert any("must not coexist" in error for error in errors)

    def test_present_empty_new_variable_still_selects_new_path(self):
        # Raw presence includes a present EMPTY value: the legacy family must
        # not silently take over.
        errors = _errors({**LEGACY_PAIR, "INFERENCE_CHAT_PROVIDER": ""})
        assert any("must not coexist" in error for error in errors)

    def test_partial_legacy_pair_url_only_fails(self):
        errors = _errors({"LLMAAS_API_URL": "https://api.example.com/v1"})
        assert any("LLMaaS partially configured" in error for error in errors)

    def test_partial_legacy_pair_key_only_fails(self):
        errors = _errors({"LLMAAS_API_KEY": "k"})
        assert any("LLMaaS partially configured" in error for error in errors)

    def test_unknown_provider_fails_closed(self):
        errors = _errors({**NEW_CHAT, "INFERENCE_CHAT_PROVIDER": "surprise-ai"})
        assert any("INFERENCE_CHAT_PROVIDER must be one of" in e for e in errors)

    def test_anthropic_embedding_role_fails_closed(self):
        env = {**NEW_EMBEDDING, "INFERENCE_EMBEDDING_PROVIDER": "anthropic",
               "INFERENCE_EMBEDDING_API_URL": "https://api.anthropic.com"}
        errors = _errors(env)
        assert any("does not support the embedding role" in e for e in errors)

    def test_unknown_family_variable_fails_closed(self):
        errors = _errors({**NEW_CHAT, "INFERENCE_CHAT_MODLE": "typo"})
        assert any("unknown inference variable INFERENCE_CHAT_MODLE" in e for e in errors)

    @pytest.mark.parametrize(
        "url, fragment",
        [
            ("ftp://api.ai.cloud-temple.com/v1", "absolute http(s) URL"),
            ("api.ai.cloud-temple.com/v1", "absolute http(s) URL"),
            ("https://user:pw@api.ai.cloud-temple.com/v1", "userinfo"),
            ("https://api.ai.cloud-temple.com/v1?token=x", "query"),
            ("https://api.ai.cloud-temple.com/v1#frag", "fragment"),
            ("http://api.ai.cloud-temple.com/v1", "https"),
            ("https://api.ai.cloud-temple.com:8443/v1", "default https port"),
            ("https://evil.example.com/v1", "documented 'cloud-temple' host"),
        ],
    )
    def test_invalid_url_components_fail(self, url, fragment):
        errors = _errors({**NEW_CHAT, "INFERENCE_CHAT_API_URL": url})
        assert any(fragment in error for error in errors)

    @pytest.mark.parametrize(
        "url, fragment",
        [
            (
                "ftp://generativelanguage.googleapis.com/v1beta/openai",
                "absolute http(s) URL",
            ),
            (
                "https://user:pw@generativelanguage.googleapis.com/v1beta/openai",
                "userinfo",
            ),
            (
                "https://generativelanguage.googleapis.com/v1beta/openai?key=x",
                "query",
            ),
            (
                "https://generativelanguage.googleapis.com/v1beta/openai#frag",
                "fragment",
            ),
            (
                "http://generativelanguage.googleapis.com/v1beta/openai",
                "https",
            ),
            (
                "https://generativelanguage.googleapis.com:8443/v1beta/openai",
                "default https port",
            ),
            (
                "https://evil.example.com/v1beta/openai",
                "documented 'gemini' host",
            ),
        ],
    )
    def test_gemini_endpoint_components_fail_closed(self, url, fragment):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "gemini",
            "INFERENCE_CHAT_API_URL": url,
        }
        errors = _errors(env)
        assert any(fragment in error for error in errors)
        assert all(url not in error for error in errors)

    @pytest.mark.parametrize(
        "provider, host, bad_path",
        [
            # Endpoint shape = documented host AND exact documented path: an
            # arbitrary path on the correct host fails at resolution time, never
            # as a runtime inference outage.
            ("cloud-temple", "api.ai.cloud-temple.com", ""),
            ("cloud-temple", "api.ai.cloud-temple.com", "/v2"),
            ("cloud-temple", "api.ai.cloud-temple.com", "/v1/v1"),
            ("openai", "api.openai.com", ""),
            ("openai", "api.openai.com", "/v1/chat"),
            ("scaleway", "api.scaleway.ai", "/project-id/v1"),
            ("mistral", "api.mistral.ai", "/api/v1"),
            ("ovhcloud", "oai.endpoints.kepler.ai.cloud.ovh.net", "/v2"),
            ("gemini", "generativelanguage.googleapis.com", "/v1/openai"),
            ("anthropic", "api.anthropic.com", "/v1"),
            ("anthropic", "api.anthropic.com", "/v1/messages"),
        ],
    )
    def test_wrong_documented_path_fails_closed(self, provider, host, bad_path):
        env = {
            "INFERENCE_CHAT_PROVIDER": provider,
            "INFERENCE_CHAT_API_URL": f"https://{host}{bad_path}",
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        errors = _errors(env)
        assert any("endpoint path exactly" in error for error in errors)
        # Diagnostics never echo the configured URL, only the safe documented
        # shape.
        joined = "\n".join(errors)
        assert bad_path == "" or f"{host}{bad_path}" not in joined

    @pytest.mark.parametrize(
        "provider, url",
        [
            ("cloud-temple", "https://api.ai.cloud-temple.com/v1/"),
            ("openai", "https://api.openai.com/v1/"),
            (
                "gemini",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            ),
            ("anthropic", "https://api.anthropic.com/"),
            ("anthropic", "https://api.anthropic.com"),
        ],
    )
    def test_documented_path_with_optional_trailing_slash_passes(
        self, provider, url
    ):
        env = {
            "INFERENCE_CHAT_PROVIDER": provider,
            "INFERENCE_CHAT_API_URL": url,
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        assert _resolve(env).chat.provider_id == provider

    def test_generic_profile_keeps_arbitrary_paths(self):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "openai-compatible",
            "INFERENCE_CHAT_API_URL": "http://gateway.local:4000/anything/v9",
        }
        assert _resolve(env).chat.adapter_id == "openai-compatible"

    @pytest.mark.parametrize(
        "provider, host, bad_path",
        [
            ("cloud-temple", "api.ai.cloud-temple.com", "/v1//"),
            ("openai", "api.openai.com", "/v1//"),
            ("scaleway", "api.scaleway.ai", "/v1///"),
            ("gemini", "generativelanguage.googleapis.com", "/v1beta/openai//"),
            ("anthropic", "api.anthropic.com", "//"),
        ],
    )
    def test_repeated_trailing_slash_hosted_path_fails(self, provider, host, bad_path):
        # '/v1//' routes to a different path than '/v1' on a path-sensitive
        # provider yet normalizes to the same fingerprint: reject it.
        env = {
            "INFERENCE_CHAT_PROVIDER": provider,
            "INFERENCE_CHAT_API_URL": f"https://{host}{bad_path}",
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        errors = _errors(env)
        assert any("endpoint path exactly" in e for e in errors)

    @pytest.mark.parametrize(
        "provider, url",
        [
            ("openai-compatible", "http://gateway.local:4000/v1//"),
            ("openai-compatible", "https://gw.example/anything//"),
            ("ollama", "http://localhost:11434/v1//"),
        ],
    )
    def test_repeated_trailing_slash_rejected_for_generic_and_ollama(
        self, provider, url
    ):
        # The repeated-trailing-slash guard is provider-INDEPENDENT: it runs
        # before any host/path pinning, so the generic openai-compatible profile
        # and the pinned ollama profile alike reject '/v1//'. Endpoint identity
        # stays injective over accepted paths for every provider.
        env = {
            "INFERENCE_CHAT_PROVIDER": provider,
            "INFERENCE_CHAT_API_URL": url,
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        errors = _errors(env)
        assert any("repeated trailing slashes" in e for e in errors)

    @pytest.mark.parametrize(
        "url, fragment",
        [
            # Ollama is the documented LOCAL endpoint (ADR-0027), not a generic
            # gateway: an external host, TLS, a nonstandard port, or a non-/v1
            # path must fail closed so the trusted local identity cannot resolve
            # an arbitrary (possibly external) URL under a local-dev label.
            ("http://evil.example:11434/v1", "documented local 'ollama' host"),
            ("http://10.0.0.9:11434/v1", "documented local 'ollama' host"),
            ("https://localhost:11434/v1", "http for the local 'ollama' profile"),
            ("http://localhost:8080/v1", "documented 'ollama' port"),
            ("http://localhost:11434/api", "documented 'ollama' endpoint path"),
            ("http://localhost:11434", "documented 'ollama' endpoint path"),
        ],
    )
    def test_ollama_pinned_to_documented_local_endpoint(self, url, fragment):
        env = {
            "INFERENCE_CHAT_PROVIDER": "ollama",
            "INFERENCE_CHAT_API_URL": url,
            "INFERENCE_CHAT_API_KEY": "ollama",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        errors = _errors(env)
        assert any(fragment in e for e in errors)
        # Diagnostics stay value-free: the configured URL is never echoed.
        assert all(url not in e for e in errors)

    def test_endpoint_diagnostics_never_echo_host_port_path(self):
        # Neither the DOCUMENTED endpoint components (host/port/path) nor the
        # configured value appear in diagnostics — only the provider identity
        # and guidance. ADR-0027 treats endpoint host/port as sensitive.
        hosted = _errors(
            {**NEW_CHAT, "INFERENCE_CHAT_PROVIDER": "openai",
             "INFERENCE_CHAT_API_URL": "https://evil.example/v2"}
        )
        ollama = _errors(
            {**NEW_CHAT, "INFERENCE_CHAT_PROVIDER": "ollama",
             "INFERENCE_CHAT_API_URL": "https://evil.example:9999/api"}
        )
        joined = "\n".join(hosted + ollama)
        assert any("documented 'openai' host" in e for e in hosted)  # still useful
        for literal in (
            "api.openai.com", "localhost", "host.docker.internal", "11434",
            "evil.example",
        ):
            assert literal not in joined

    @pytest.mark.parametrize(
        "variable, value",
        [
            ("INFERENCE_CHAT_CONTEXT_WINDOW", "0"),
            ("INFERENCE_CHAT_CONTEXT_WINDOW", "-1"),
            ("INFERENCE_CHAT_CONTEXT_WINDOW", "12.5"),
            ("INFERENCE_CHAT_CONTEXT_WINDOW", "abc"),
            ("INFERENCE_CHAT_MAX_OUTPUT_TOKENS", "0"),
            ("INFERENCE_CHAT_MAX_OUTPUT_TOKENS", "nope"),
            # Unicode digits are not ASCII base-10: Arabic-Indic ١٢٣ would be
            # accepted by int() and superscript ² crashes it — both must fail
            # the documented base-10 integer contract closed.
            ("INFERENCE_CHAT_CONTEXT_WINDOW", "١٢٣"),
            ("INFERENCE_CHAT_MAX_OUTPUT_TOKENS", "²"),
            # A 4300+-digit value must fail closed value-free, never escape as
            # the CPython integer-string digit-limit ValueError.
            pytest.param(
                "INFERENCE_CHAT_CONTEXT_WINDOW", "9" * 5000, id="context-huge-digits"
            ),
        ],
    )
    def test_invalid_chat_numbers_fail(self, variable, value):
        errors = _errors({**NEW_CHAT, variable: value})
        assert any(variable in error for error in errors)

    def test_output_must_be_strictly_below_context(self):
        errors = _errors(
            {
                **NEW_CHAT,
                "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "4096",
            }
        )
        assert any("strictly below" in error for error in errors)

    def test_boundary_output_one_below_context_is_valid(self):
        config = _resolve(
            {
                **NEW_CHAT,
                "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "4095",
            }
        )
        assert config.chat.max_output_tokens == 4095

    @pytest.mark.parametrize("family", ["split", "legacy"])
    def test_generation_budget_above_declared_contract_fails_startup(self, family):
        if family == "split":
            variable = "INFERENCE_CHAT_MAX_OUTPUT_TOKENS"
            env = {
                **NEW_CHAT,
                "INFERENCE_CHAT_CONTEXT_WINDOW": "1000002",
                variable: "1000001",
            }
        else:
            variable = "LLMAAS_MAX_TOKENS"
            env = {
                **LEGACY_PAIR,
                "LLMAAS_CONTEXT_WINDOW": "1000002",
                variable: "1000001",
            }
        errors = _errors(env)
        assert any(
            variable in error and "generation budget" in error for error in errors
        )

    @pytest.mark.parametrize("value", ["abc", "nan", "inf", "-0.1", "2.1"])
    def test_invalid_openai_compatible_temperature_fails(self, value):
        errors = _errors({**NEW_CHAT, "INFERENCE_CHAT_TEMPERATURE": value})
        assert any("INFERENCE_CHAT_TEMPERATURE" in error for error in errors)

    def test_anthropic_temperature_range_is_tighter(self):
        env = {
            "INFERENCE_CHAT_PROVIDER": "anthropic",
            "INFERENCE_CHAT_API_URL": "https://api.anthropic.com",
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "claude-sonnet-5",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "131072",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "16384",
            "INFERENCE_CHAT_TEMPERATURE": "1.5",
        }
        errors = _errors(env)
        assert any("out of range [0.0, 1.0]" in error for error in errors)
        env["INFERENCE_CHAT_TEMPERATURE"] = "1.0"
        assert _resolve(env).chat.temperature == 1.0

    @pytest.mark.parametrize(
        "variable, value",
        [
            ("INFERENCE_EMBEDDING_DIMENSIONS", "0"),
            ("INFERENCE_EMBEDDING_DIMENSIONS", "-8"),
            ("INFERENCE_EMBEDDING_DIMENSIONS", "1024.0"),
            ("INFERENCE_EMBEDDING_DIMENSIONS", "big"),
            ("INFERENCE_EMBEDDING_DIMENSIONS", "١٠٢٤"),  # Unicode digits, not base-10
            pytest.param(
                "INFERENCE_EMBEDDING_DIMENSIONS", "9" * 5000, id="dims-huge-digits"
            ),
        ],
    )
    def test_invalid_dimensions_fail(self, variable, value):
        errors = _errors({**NEW_EMBEDDING, variable: value})
        assert any(variable in error for error in errors)

    @pytest.mark.parametrize(
        "variable, value",
        [
            ("LLMAAS_CONTEXT_WINDOW", "zero"),
            ("LLMAAS_MAX_TOKENS", "-5"),
            ("LLMAAS_TEMPERATURE", "3.5"),
            ("LLMAAS_EMBEDDING_DIMENSIONS", "0"),
            ("LLMAAS_MODEL", ""),
            ("LLMAAS_EMBEDDING_MODEL", "   "),
        ],
    )
    def test_invalid_legacy_values_fail(self, variable, value):
        errors = _errors({**LEGACY_PAIR, variable: value})
        assert any(variable.split("_", 1)[0] in error or variable in error for error in errors)

    def test_legacy_output_context_coherence_enforced(self):
        errors = _errors(
            {**LEGACY_PAIR, "LLMAAS_CONTEXT_WINDOW": "1000", "LLMAAS_MAX_TOKENS": "1000"}
        )
        assert any("strictly less than" in error for error in errors)

    def test_legacy_url_with_credentials_fails_without_echoing_them(self):
        errors = _errors(
            {
                "LLMAAS_API_URL": "https://svc:hunter2@llm.example.com/v1?access_token=abc",
                "LLMAAS_API_KEY": "k",
            }
        )
        joined = "\n".join(errors)
        assert "hunter2" not in joined
        assert "access_token" not in joined
        assert "svc:" not in joined

    def test_diagnostics_never_echo_configured_values(self):
        errors = _errors(
            {
                **NEW_CHAT,
                "INFERENCE_CHAT_API_KEY": "sk-super-secret-value",
                "INFERENCE_CHAT_API_URL": "https://user:pw@api.ai.cloud-temple.com/v1",
                "INFERENCE_CHAT_CONTEXT_WINDOW": "boom-value",
            }
        )
        joined = "\n".join(errors)
        assert "sk-super-secret-value" not in joined
        assert "pw@" not in joined
        assert "boom-value" not in joined

    @pytest.mark.parametrize(
        "typo",
        [
            "LLMAAS_MODLE",
            "LLMAAS_API_KYE",
            "LLMAAS_EMBEDDING_DIMENSION",
            "LLMAAS_TEMP",
            "LLMAAS_MAXTOKENS",
        ],
    )
    def test_unknown_legacy_variable_with_valid_pair_fails(self, typo):
        # A typo'd optional legacy variable must fail closed, never silently
        # fall back to the default model/tunable.
        errors = _errors({**LEGACY_PAIR, typo: "whatever"})
        assert any("unknown legacy variable" in e for e in errors)
        assert any(typo in e for e in errors)

    def test_unknown_legacy_variable_alone_fails(self):
        errors = _errors({"LLMAAS_MODLE": "x"})
        assert any(
            "unknown legacy variable" in e and "LLMAAS_MODLE" in e for e in errors
        )

    def test_known_legacy_variables_do_not_trip_the_guard(self):
        config = _resolve(
            {**LEGACY_PAIR, "LLMAAS_MODEL": "m", "LLMAAS_EMBEDDING_DIMENSIONS": "512"}
        )
        assert config.legacy_active is True

    def test_unknown_legacy_diagnostic_echoes_name_not_value(self):
        errors = _errors({**LEGACY_PAIR, "LLMAAS_SECRET_TOKEN": "super-secret-value"})
        joined = "\n".join(errors)
        assert "LLMAAS_SECRET_TOKEN" in joined
        assert "super-secret-value" not in joined

    def test_case_variant_collision_conflicting_values_fails_closed(self):
        errors = _errors(
            {
                "LLMAAS_API_URL": "https://real-endpoint.example/v1",
                "llmaas_api_url": "https://attacker-endpoint.example/v1",
                "LLMAAS_API_KEY": "k",
            }
        )
        joined = "\n".join(errors)
        assert any("case-variant environment collision" in e for e in errors)
        assert "LLMAAS_API_URL" in joined
        assert "real-endpoint.example" not in joined  # values are never echoed
        assert "attacker-endpoint.example" not in joined

    def test_case_variant_same_value_still_fails_closed(self):
        # ANY canonical name under more than one case-spelling is ambiguous and
        # fails closed, even when the duplicated values happen to match: which
        # spelling is later edited would silently flip behavior. Value-free.
        errors = _errors(
            {
                "LLMAAS_API_URL": "https://api.ai.cloud-temple.com/v1",
                "llmaas_api_url": "https://api.ai.cloud-temple.com/v1",
                "LLMAAS_API_KEY": "k",
            }
        )
        assert any("case-variant environment collision" in e for e in errors)
        assert all("api.ai.cloud-temple.com" not in e for e in errors)

    def test_new_family_case_variant_collision_fails_closed(self):
        errors = _errors(
            {**NEW_CHAT, "inference_chat_provider": "anthropic"}
        )
        assert any("case-variant environment collision" in e for e in errors)

    @pytest.mark.parametrize(
        "typo",
        [
            "INFERENCE_CHATX_PROVIDER",
            "INFERENCE_EMBEDDINGS_MODEL",
            "INFERENCE_CHT_API_URL",
            "INFERENCE_PROVIDER",
        ],
    )
    def test_stray_inference_typo_with_legacy_pair_fails_closed(self, typo):
        # A typo'd INFERENCE_* name must NOT be ignored so that a valid legacy
        # pair silently activates legacy egress. It fails closed (raising),
        # naming the stray variable.
        errors = _errors({**LEGACY_PAIR, typo: "x"})
        assert any("unknown inference variable" in e for e in errors)
        assert any(typo in e for e in errors)

    def test_stray_inference_alone_fails_closed(self):
        errors = _errors({"INFERENCE_CHATX_PROVIDER": "openai"})
        assert any(
            "unknown inference variable" in e and "INFERENCE_CHATX_PROVIDER" in e
            for e in errors
        )

    def test_stray_inference_never_silently_selects_legacy(self):
        # Direct proof of the fail-open the guard closes: the same env must
        # raise rather than resolve to an active legacy profile.
        with pytest.raises(InferenceConfigError):
            resolve_inference_config({**LEGACY_PAIR, "INFERENCE_CHATX_PROVIDER": "x"})

    def test_whitespace_only_chat_key_fails(self):
        errors = _errors({**NEW_CHAT, "INFERENCE_CHAT_API_KEY": "   "})
        assert any("INFERENCE_CHAT_API_KEY must not be blank" in e for e in errors)

    def test_whitespace_only_embedding_key_fails(self):
        errors = _errors({**NEW_EMBEDDING, "INFERENCE_EMBEDDING_API_KEY": "\t "})
        assert any("INFERENCE_EMBEDDING_API_KEY must not be blank" in e for e in errors)

    def test_whitespace_only_legacy_key_fails(self):
        errors = _errors(
            {
                "LLMAAS_API_URL": "https://api.ai.cloud-temple.com/v1",
                "LLMAAS_API_KEY": "   ",
            }
        )
        assert any("LLMAAS_API_KEY must not be blank" in e for e in errors)


# --------------------------------------------------------------------------- #
# Frozen registry table                                                       #
# --------------------------------------------------------------------------- #


class TestFrozenRegistry:
    def test_provider_to_adapter_table_is_exactly_the_adr_table(self):
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

    def test_role_restrictions(self):
        assert set(CHAT_PROVIDER_IDS) == set(PROVIDER_TO_ADAPTER)
        assert set(EMBEDDING_PROVIDER_IDS) == set(PROVIDER_TO_ADAPTER) - {"anthropic"}

    @pytest.mark.parametrize("provider_id", sorted(PROVIDER_TO_ADAPTER))
    def test_every_chat_provider_resolves_through_the_table(self, provider_id):
        hosts = {
            "cloud-temple": "https://api.ai.cloud-temple.com/v1",
            "scaleway": "https://api.scaleway.ai/v1",
            "openai": "https://api.openai.com/v1",
            "mistral": "https://api.mistral.ai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "ovhcloud": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
            "anthropic": "https://api.anthropic.com",
            "ollama": "http://localhost:11434/v1",
            "openai-compatible": "http://gateway.local:4000/v1",
        }
        env = {
            "INFERENCE_CHAT_PROVIDER": provider_id,
            "INFERENCE_CHAT_API_URL": hosts[provider_id],
            "INFERENCE_CHAT_API_KEY": "k",
            "INFERENCE_CHAT_MODEL": "m",
            "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
        }
        config = _resolve(env)
        assert config.chat.adapter_id == PROVIDER_TO_ADAPTER[provider_id]

    def test_url_never_selects_an_adapter(self):
        # An OpenAI-shaped URL under the generic profile stays generic; the
        # anthropic host under the generic profile stays generic too.
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "openai-compatible",
            "INFERENCE_CHAT_API_URL": "https://api.anthropic.com",
        }
        assert _resolve(env).chat.adapter_id == "openai-compatible"


# --------------------------------------------------------------------------- #
# Deprecation warning, merge helper, records, fingerprints                    #
# --------------------------------------------------------------------------- #


class TestLegacyDeprecationWarning:
    def test_exactly_one_warning_per_process(self, caplog):
        _reset_legacy_deprecation_warning_for_tests()
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.config"):
            resolve_inference_config(dict(LEGACY_PAIR))
            resolve_inference_config(dict(LEGACY_PAIR))
        deprecations = [
            record
            for record in caplog.records
            if "deprecated" in record.getMessage()
        ]
        assert len(deprecations) == 1
        _reset_legacy_deprecation_warning_for_tests()

    def test_new_path_emits_no_deprecation(self, caplog):
        _reset_legacy_deprecation_warning_for_tests()
        with caplog.at_level(logging.WARNING, logger="hivemind_inference.config"):
            resolve_inference_config({**NEW_CHAT, **NEW_EMBEDDING})
        assert not [
            record
            for record in caplog.records
            if "deprecated" in record.getMessage()
        ]


class TestMergedEnvironment:
    def test_process_environment_wins_over_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("LLMAAS_API_URL=https://from-file.example/v1\nEXTRA=1\n")
        monkeypatch.setenv("LLMAAS_API_URL", "https://from-process.example/v1")
        merged = merged_environment(str(env_file))
        assert merged["LLMAAS_API_URL"] == "https://from-process.example/v1"
        assert merged["EXTRA"] == "1"

    def test_present_empty_file_entry_counts_as_present(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("INFERENCE_CHAT_PROVIDER=\n")
        monkeypatch.delenv("INFERENCE_CHAT_PROVIDER", raising=False)
        merged = merged_environment(str(env_file))
        assert merged["INFERENCE_CHAT_PROVIDER"] == ""

    def test_missing_file_is_not_an_error(self, tmp_path):
        merged = merged_environment(str(tmp_path / "absent.env"))
        assert isinstance(merged, dict)


class TestImmutableRecords:
    def test_profiles_are_frozen_and_repr_safe(self):
        config = _resolve({**NEW_CHAT, **NEW_EMBEDDING})
        chat = config.chat
        with pytest.raises(dataclasses.FrozenInstanceError):
            chat.configured_model = "other"  # type: ignore[misc]
        for rendered in (repr(chat), str(chat), repr(config.embedding)):
            assert "chat-key" not in rendered
            assert "embed-key" not in rendered
            assert "api.ai.cloud-temple.com" not in rendered

    def test_safe_snapshot_excludes_secrets(self):
        config = _resolve({**NEW_CHAT, **NEW_EMBEDDING})
        for snapshot in (config.chat.safe_snapshot(), config.embedding.safe_snapshot()):
            joined = str(snapshot)
            assert "key" not in snapshot.values().__class__.__name__  # sanity
            assert "chat-key" not in joined
            assert "embed-key" not in joined
            assert "endpoint" not in snapshot
            assert "api_key" not in snapshot

    def test_requests_are_immutable_and_carry_no_authority_fields(self):
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="hi"),),
            timeout_seconds=5.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.timeout_seconds = 1.0  # type: ignore[misc]
        forbidden = {
            "space_id",
            "commit_id",
            "bank_version",
            "term",
            "membership",
            "watermark",
            "tombstone",
            "manifest",
            "lease",
        }
        for record_type in (ChatRequest, EmbeddingRequest):
            field_names = {f.name for f in dataclasses.fields(record_type)}
            assert not (field_names & forbidden)

    def test_correlation_id_generated_and_validated(self):
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="hi"),), timeout_seconds=1.0
        )
        assert len(request.correlation_id) == 32
        with pytest.raises(ValueError):
            ChatRequest(
                messages=(ChatMessage(role="user", content="hi"),),
                timeout_seconds=1.0,
                correlation_id="bad id with spaces",
            )

    def test_embedding_request_validation(self):
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=(), timeout_seconds=1.0)
        with pytest.raises(ValueError):
            EmbeddingRequest(inputs=("x",), timeout_seconds=1.0, input_type="mixed")


class TestEmbeddingFingerprint:
    def _embedding(self, **overrides):
        env = {**NEW_EMBEDDING, **overrides}
        return _resolve(env).embedding

    def test_fingerprint_is_deterministic(self):
        profile = self._embedding()
        assert embedding_profile_fingerprint(profile) == embedding_profile_fingerprint(
            profile
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"INFERENCE_EMBEDDING_MODEL": "other-model"},
            {"INFERENCE_EMBEDDING_DIMENSIONS": "512"},
            {
                "INFERENCE_EMBEDDING_PROVIDER": "scaleway",
                "INFERENCE_EMBEDDING_API_URL": "https://api.scaleway.ai/v1",
            },
            {
                "INFERENCE_EMBEDDING_PROVIDER": "openai-compatible",
                "INFERENCE_EMBEDDING_API_URL": "http://gateway.local:4000/v1",
            },
        ],
    )
    def test_every_compatibility_field_changes_the_fingerprint(self, overrides):
        assert embedding_profile_fingerprint(
            self._embedding()
        ) != embedding_profile_fingerprint(self._embedding(**overrides))

    def test_endpoint_normalization_is_stable_but_identity_scoped(self):
        assert endpoint_sha256("https://API.Example.com:443/v1/") == endpoint_sha256(
            "https://api.example.com/v1"
        )
        assert endpoint_sha256("https://api.example.com/v1") != endpoint_sha256(
            "https://api.example.com/v2"
        )

    # NOTE (P13-1A / #274 scope): the Qdrant COLLECTION metadata builder that
    # embeds this profile fingerprint (``build_embedding_collection_metadata``)
    # and its stored-metadata self-consistency check are the canonical Qdrant
    # embedding identity, deferred with the drift guards to #277. This
    # foundation slice proves only the PROFILE/ENDPOINT identity primitives.


class TestDependencyNeutrality:
    def test_package_never_imports_the_consumers(self):
        import ast
        import pathlib

        package_root = (
            pathlib.Path(__file__).resolve().parents[1] / "src" / "hivemind_inference"
        )
        forbidden_roots = {"live_mem", "mcp_memory"}
        offending: list[str] = []
        source_files = sorted(package_root.rglob("*.py"))
        assert source_files, "hivemind_inference sources must exist"
        for source_file in source_files:
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in forbidden_roots:
                            offending.append(f"{source_file.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module_root = (node.module or "").split(".")[0]
                    if node.level == 0 and module_root in forbidden_roots:
                        offending.append(
                            f"{source_file.name}: from {node.module} import ..."
                        )
        assert offending == []


class TestPortValidationNeverEchoesValues:
    """A malformed endpoint port is a value-free configuration error for EVERY
    provider — never a raw ValueError embedding the configured (possibly
    secret-bearing) URL."""

    @pytest.mark.parametrize(
        "provider, url",
        [
            ("cloud-temple", "https://api.ai.cloud-temple.com:S3CRET-P0RT/v1"),
            ("openai-compatible", "https://llm.internal:S3CRET-P0RT/v1"),
            ("ollama", "http://ollama.local:S3CRET-P0RT"),
        ],
    )
    def test_malformed_port_is_a_named_diagnostic(self, provider, url):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": provider,
            "INFERENCE_CHAT_API_URL": url,
        }
        errors = _errors(env)
        assert any("endpoint port is invalid" in e for e in errors)
        assert not any("S3CRET-P0RT" in e for e in errors)

    def test_malformed_port_never_leaks_through_the_exception(self):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "openai-compatible",
            "INFERENCE_CHAT_API_URL": "https://llm.internal:S3CRET-P0RT/v1",
        }
        with pytest.raises(InferenceConfigError) as excinfo:
            resolve_inference_config(env)
        exc = excinfo.value
        seen = []
        while exc is not None and exc not in seen:
            seen.append(exc)
            assert "S3CRET-P0RT" not in repr(exc)
            assert "S3CRET-P0RT" not in str(exc.args)
            exc = exc.__cause__ or exc.__context__


    def test_valid_explicit_port_is_accepted_on_generic_profiles(self):
        env = {
            **NEW_CHAT,
            "INFERENCE_CHAT_PROVIDER": "openai-compatible",
            "INFERENCE_CHAT_API_URL": "https://llm.internal:8443/v1",
        }
        config = resolve_inference_config(env)
        assert config.chat is not None

    @pytest.mark.parametrize(
        "family",
        [
            {
                "INFERENCE_CHAT_PROVIDER": "openai-compatible",
                "INFERENCE_CHAT_API_URL": "https://[bad::v6::S3CRET6]/v1",
                "INFERENCE_CHAT_API_KEY": "k",
                "INFERENCE_CHAT_MODEL": "m",
                "INFERENCE_CHAT_CONTEXT_WINDOW": "4096",
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS": "1024",
            },
            {
                "INFERENCE_EMBEDDING_PROVIDER": "openai-compatible",
                "INFERENCE_EMBEDDING_API_URL": "https://[bad::v6::S3CRET6]/v1",
                "INFERENCE_EMBEDDING_API_KEY": "k",
                "INFERENCE_EMBEDDING_MODEL": "m",
                "INFERENCE_EMBEDDING_DIMENSIONS": "1024",
            },
            {
                "LLMAAS_API_URL": "https://[bad::v6::S3CRET6]/v1",
                "LLMAAS_API_KEY": "k",
            },
        ],
    )
    def test_malformed_ipv6_endpoint_is_a_value_free_config_error(self, family):
        # urlsplit itself raises ValueError (embedding the value) on a malformed
        # IPv6 literal; it must surface as an aggregated InferenceConfigError
        # with the value nowhere in the exception chain — for new and legacy.
        with pytest.raises(InferenceConfigError) as excinfo:
            resolve_inference_config(family)
        exc = excinfo.value
        seen = []
        while exc is not None and exc not in seen:
            seen.append(exc)
            assert "S3CRET6" not in repr(exc)
            assert "S3CRET6" not in str(exc.args)
            exc = exc.__cause__ or exc.__context__

# NOTE (P13-1A / #274 scope): the guard that the docker-compose service
# definitions never inject LLMAAS_*/INFERENCE_* names (the family choice must
# stay in .env, since a service-level env wins over env_file and would poison a
# split deployment with legacy names) belongs to the consumer/runtime migration
# that edits docker-compose.yml (#276). This foundation slice does not touch
# compose, so asserting on it here would test an unshipped change.
