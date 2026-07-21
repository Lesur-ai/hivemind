# -*- coding: utf-8 -*-
"""
Unit tests for startup configuration validation.
"""

import pytest

from live_mem.config import Settings


def _make_settings(**overrides):
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
        "consolidation_batch_size": 5,
        "compact_threshold": 0.6,
        "bank_file_max_size": 15360,
        "response_max_bytes": 512 * 1024,
    }
    defaults.update(overrides)
    # Use model_construct to bypass env file loading, then validate
    s = Settings.model_validate(defaults)
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
    def test_timeout_too_low(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_TIMEOUT"):
            _make_settings(consolidation_timeout=5)

    def test_max_notes_zero(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_MAX_NOTES"):
            _make_settings(consolidation_max_notes=0)

    def test_batch_size_zero(self):
        with pytest.raises(ValueError, match="CONSOLIDATION_BATCH_SIZE"):
            _make_settings(consolidation_batch_size=0)


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
        s = _make_settings(proxy_url="http://10.185.132.250:3128")
        assert s.proxy_url == "http://10.185.132.250:3128"

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
        with pytest.raises(ValueError, match="PROXY_URL must start"):
            _make_settings(proxy_url="tcp://proxy:3128")

    def test_invalid_scheme_bare_host_rejected(self):
        with pytest.raises(ValueError, match="PROXY_URL must start"):
            _make_settings(proxy_url="proxy.example.com:3128")
