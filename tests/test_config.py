# -*- coding: utf-8 -*-
"""
Unit tests for startup configuration validation.
"""

from pathlib import Path

import pytest

from live_mem.config import Settings


def _make_settings(*, _env_file=None, **overrides):
    """Create Settings with sensible defaults, overridden by kwargs."""
    defaults = {
        "mcp_server_name": "Test",
        "mcp_server_host": "0.0.0.0",
        "mcp_server_port": 8002,
        "mcp_server_debug": False,
        "admin_bootstrap_key": "change_me_in_production",
        "s3_endpoint_url": "",
        "s3_access_key_id": "",
        "s3_secret_access_key": "",
        "s3_bucket_name": "live-mem",
        "s3_region_name": "fr1",
        "s3_signature_mode": "dual",
        "llmaas_api_url": "",
        "llmaas_api_key": "",
        "llmaas_model": "test-model",
        "llmaas_context_window": 131072,
        "llmaas_max_tokens": 16384,
        "llmaas_temperature": 0.3,
        "default_rules_file": "",
        "consolidation_timeout": 600,
        "consolidation_max_notes": 500,
        "bank_file_max_size": 15360,
        "response_max_bytes": 512 * 1024,
    }
    defaults.update(overrides)
    # Default to no dotenv file; template tests opt into the actual file.
    s = Settings(_env_file=_env_file, **defaults)
    return s


class TestPortValidation:
    def test_valid_port(self):
        s = _make_settings(mcp_server_port=8080)
        assert s.mcp_server_port == 8080

    def test_port_zero_rejected(self):
        with pytest.raises(ValueError, match="MCP_SERVER_PORT"):
            _make_settings(mcp_server_port=0)

    def test_port_too_high_rejected(self):
        with pytest.raises(ValueError, match="MCP_SERVER_PORT"):
            _make_settings(mcp_server_port=70000)


class TestS3Validation:
    def test_all_s3_fields_set(self):
        s = _make_settings(
            s3_endpoint_url="http://minio:9000",
            s3_access_key_id="key",
            s3_secret_access_key="secret",
        )
        assert s.s3_endpoint_url == "http://minio:9000"

    def test_partial_s3_rejected(self):
        with pytest.raises(ValueError, match="S3 partially"):
            _make_settings(
                s3_endpoint_url="http://minio:9000",
                s3_access_key_id="",
                s3_secret_access_key="secret",
            )

    def test_s3_url_must_start_with_http(self):
        with pytest.raises(ValueError, match="S3_ENDPOINT_URL must start"):
            _make_settings(
                s3_endpoint_url="ftp://minio:9000",
                s3_access_key_id="key",
                s3_secret_access_key="secret",
            )

    def test_no_s3_is_ok(self):
        """All S3 fields empty is valid (unconfigured)."""
        s = _make_settings(
            s3_endpoint_url="",
            s3_access_key_id="",
            s3_secret_access_key="",
        )
        assert s.s3_endpoint_url == ""


class TestS3SignatureMode:
    def test_dual_is_default(self):
        s = _make_settings()
        assert s.s3_signature_mode == "dual"

    def test_sigv4_accepted(self):
        s = _make_settings(s3_signature_mode="sigv4")
        assert s.s3_signature_mode == "sigv4"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="S3_SIGNATURE_MODE"):
            _make_settings(s3_signature_mode="sigv2")

    def test_empty_mode_rejected(self):
        with pytest.raises(ValueError, match="S3_SIGNATURE_MODE"):
            _make_settings(s3_signature_mode="")


class TestLLMValidation:
    def test_both_llm_fields_set(self):
        s = _make_settings(
            llmaas_api_url="https://api.example.com/v1",
            llmaas_api_key="sk-test",
        )
        assert s.llmaas_api_url == "https://api.example.com/v1"

    def test_partial_llm_rejected(self):
        with pytest.raises(ValueError, match="LLMaaS partially"):
            _make_settings(
                llmaas_api_url="https://api.example.com/v1",
                llmaas_api_key="",
            )

    def test_no_llm_is_ok(self):
        s = _make_settings(llmaas_api_url="", llmaas_api_key="")
        assert s.llmaas_api_url == ""


class TestConsolidationValidation:
    def test_consolidation_batch_size_default_is_three(self, monkeypatch):
        # Assert the class default without an operator override from env/.env.
        monkeypatch.delenv("CONSOLIDATION_BATCH_SIZE", raising=False)
        assert Settings.model_fields["consolidation_batch_size"].default == 3
        assert _make_settings(_env_file=None).consolidation_batch_size == 3

    def test_consolidation_batch_size_override(self):
        assert _make_settings(consolidation_batch_size=7).consolidation_batch_size == 7

    def test_deployment_template_selects_two_note_recipe_batches(self, monkeypatch):
        # The deployment template explicitly
        # overrides the preserved three-note class default with two-note batches.
        template = Path(__file__).resolve().parents[1] / ".env.example"
        values = [
            line.split("=", 1)[1].strip()
            for line in template.read_text(encoding="utf-8").splitlines()
            if line.startswith("CONSOLIDATION_BATCH_SIZE=")
        ]
        assert values == ["2"]
        monkeypatch.delenv("CONSOLIDATION_BATCH_SIZE", raising=False)
        assert _make_settings(_env_file=template).consolidation_batch_size == 2

    def test_legacy_french_prompt_setting_is_gone_and_ignored(self):
        """The French compatibility bridge is removed. A stale variable in
        an operator .env must be ignored (extra="ignore"), never crash startup."""
        assert "consolidation_legacy_french_prompts" not in Settings.model_fields
        assert not hasattr(
            _make_settings(consolidation_legacy_french_prompts="true"),
            "consolidation_legacy_french_prompts",
        )

    def test_timeout_too_low(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_TIMEOUT"):
            _make_settings(consolidation_timeout=5)

    def test_timeout_default_lets_a_slow_model_work(self):
        """Large banks and slower models need a sufficient per-call budget.

        The default is 1800 s; a silent return to the former 600 s bound must
        fail here. Operators may tune it; the >= 10 s floor is unchanged.
        """
        assert Settings.model_fields["consolidation_timeout"].default == 1800
        assert _make_settings(consolidation_timeout=1800).consolidation_timeout == 1800

    def test_compact_threshold_no_longer_exists_and_its_env_var_is_ignored(self, monkeypatch):
        """Compaction is a human decision.

        Consolidation never triggers it, so the admission ratio has no consumer
        left; the removed field must not come back and a stale env var must be
        ignored rather than fail startup (``extra="ignore"``).
        """
        assert "compact_threshold" not in Settings.model_fields
        monkeypatch.setenv("COMPACT_THRESHOLD", "0.6")
        settings = _make_settings()
        assert not hasattr(settings, "compact_threshold")
        assert Settings.model_fields["bank_file_max_size"].default == 35000

    def test_max_notes_zero(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_MAX_NOTES"):
            _make_settings(consolidation_max_notes=0)

    def test_batch_size_zero(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_BATCH_SIZE"):
            _make_settings(consolidation_batch_size=0)


class TestCompactionValidation:


    @pytest.mark.parametrize("size", (0, -1))
    def test_bank_file_limit_must_be_positive(self, size):
        with pytest.raises(ValueError, match="BANK_FILE_MAX_SIZE"):
            _make_settings(bank_file_max_size=size)

    def test_bank_file_limit_accepts_one_utf8_byte(self):
        assert _make_settings(bank_file_max_size=1).bank_file_max_size == 1


class TestTemperatureValidation:
    def test_temperature_out_of_range(self):
        with pytest.raises(ValueError, match="LLMAAS_TEMPERATURE"):
            _make_settings(llmaas_temperature=3.0)

    def test_temperature_negative(self):
        with pytest.raises(ValueError, match="LLMAAS_TEMPERATURE"):
            _make_settings(llmaas_temperature=-0.5)

    def test_temperature_boundaries(self):
        _make_settings(llmaas_temperature=0.0)
        _make_settings(llmaas_temperature=2.0)


class TestResponseLimitValidation:
    def test_response_limit_too_low(self):
        with pytest.raises(ValueError, match="RESPONSE_MAX_BYTES"):
            _make_settings(response_max_bytes=100)


class TestProxyValidation:
    def test_valid_http_proxy(self):
        s = _make_settings(proxy_url="http://proxy.example.com:3128")
        assert s.proxy_url == "http://proxy.example.com:3128"

    def test_valid_https_proxy(self):
        s = _make_settings(proxy_url="https://proxy.example.com:8080")
        assert s.proxy_url == "https://proxy.example.com:8080"

    def test_no_proxy_is_none(self):
        s = _make_settings(proxy_url=None)
        assert s.proxy_url is None

    def test_empty_string_normalized_to_none(self):
        """field_validator normalise '' → None."""
        s = _make_settings(proxy_url="")
        assert s.proxy_url is None

    def test_whitespace_only_normalized_to_none(self):
        """field_validator normalise '   ' → None."""
        s = _make_settings(proxy_url="   ")
        assert s.proxy_url is None

    def test_proxy_with_leading_whitespace_stripped(self):
        """field_validator strip() avant validation."""
        s = _make_settings(proxy_url="  http://proxy:3128  ")
        assert s.proxy_url == "http://proxy:3128"

    def test_invalid_scheme_tcp_rejected(self):
        # P12-3 R4 : RuntimeError (pas ValueError) — pydantic convertirait un
        # ValueError en ValidationError qui écho la valeur brute (credentials).
        with pytest.raises(RuntimeError, match="PROXY_URL must start"):
            _make_settings(proxy_url="tcp://proxy:3128")

    def test_invalid_scheme_bare_host_rejected(self):
        with pytest.raises(RuntimeError, match="PROXY_URL must start"):
            _make_settings(proxy_url="proxy.example.com:3128")


class TestLlmBudgetCoherence:
    """P12-1 — LLMAAS_MAX_TOKENS must stay strictly below LLMAAS_CONTEXT_WINDOW."""

    def test_default_budget_passes(self):
        s = _make_settings()
        assert s.llmaas_max_tokens < s.llmaas_context_window

    def test_strictly_smaller_budget_passes(self):
        s = _make_settings(
            llmaas_max_tokens=131071, llmaas_context_window=131072
        )
        assert s.llmaas_max_tokens == 131071

    def test_equal_budget_rejected(self):
        with pytest.raises(ValueError, match="LLMAAS_MAX_TOKENS"):
            _make_settings(
                llmaas_max_tokens=131072, llmaas_context_window=131072
            )

    def test_greater_budget_rejected(self):
        with pytest.raises(ValueError, match="LLMAAS_MAX_TOKENS"):
            _make_settings(
                llmaas_max_tokens=200000, llmaas_context_window=131072
            )

    def test_error_names_both_variables_and_effective_values(self):
        with pytest.raises(ValueError) as exc_info:
            _make_settings(
                llmaas_max_tokens=200000, llmaas_context_window=131072
            )
        message = str(exc_info.value)
        assert "LLMAAS_MAX_TOKENS=200000" in message
        assert "LLMAAS_CONTEXT_WINDOW=131072" in message

    def test_budget_error_is_aggregated_with_other_startup_errors(self):
        with pytest.raises(ValueError) as exc_info:
            _make_settings(
                mcp_server_port=0,
                llmaas_max_tokens=131072,
                llmaas_context_window=131072,
            )
        message = str(exc_info.value)
        assert "MCP_SERVER_PORT" in message
        assert "LLMAAS_MAX_TOKENS=131072" in message
        assert "LLMAAS_CONTEXT_WINDOW=131072" in message
