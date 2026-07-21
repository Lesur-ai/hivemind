# -*- coding: utf-8 -*-
"""
Tests for PROXY_URL feature.

Covers:
- StorageService: proxy injected (or not) into boto3 Config objects
- ConsolidatorService: _http_client lifecycle (create, close)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live_mem.config import Settings


# ─────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────

_BASE = {
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
    "proxy_url": None,
    # P7-9 hermeticity: these tests assert the DUAL wiring (distinct SigV2 data
    # + SigV4 metadata clients). Without this pin, pydantic-settings fills
    # s3_signature_mode from the ambient env/.env — an operator running the
    # suite with S3_SIGNATURE_MODE=sigv4 (MinIO/AWS) got 2 false failures.
    "s3_signature_mode": "dual",
}


def _make_settings(**overrides) -> Settings:
    defaults = dict(_BASE)
    defaults.update(overrides)
    return Settings.model_validate(defaults)


# ─────────────────────────────────────────────────────────────
# StorageService — proxy → boto3 Config
# ─────────────────────────────────────────────────────────────


class TestStorageServiceProxy:
    """Vérifie que PROXY_URL est bien injecté dans les deux Config boto3."""

    def _run_storage_with(self, proxy_url):
        """Instancie StorageService avec les settings donnés, capture les Config."""
        settings = _make_settings(proxy_url=proxy_url)
        captured_configs = []

        def _capture_client(*args, **kwargs):
            if "config" in kwargs:
                captured_configs.append(kwargs["config"])
            return MagicMock()

        with (
            patch("live_mem.core.storage.get_settings", return_value=settings),
            patch("live_mem.core.storage.boto3.client", side_effect=_capture_client),
        ):
            from live_mem.core.storage import StorageService

            StorageService()

        return captured_configs

    def test_proxy_injected_in_both_boto3_configs(self):
        """Avec PROXY_URL, les deux Config (SigV2 + SigV4) doivent avoir proxies."""
        configs = self._run_storage_with("http://proxy.example.com:3128")

        assert len(configs) == 2, "StorageService doit créer 2 clients boto3"
        for cfg in configs:
            assert cfg.proxies == {
                "http": "http://proxy.example.com:3128",
                "https": "http://proxy.example.com:3128",
            }, f"proxies manquant dans Config: {cfg.__dict__}"

    def test_no_proxy_no_proxies_key(self):
        """Sans PROXY_URL, les Config boto3 ne doivent PAS avoir de clé proxies."""
        configs = self._run_storage_with(None)

        assert len(configs) == 2
        for cfg in configs:
            assert cfg.proxies is None, f"proxies inattendu: {cfg.proxies}"

    def test_empty_proxy_url_treated_as_none(self):
        """PROXY_URL='' (vide) doit être traité comme absent (None)."""
        # Le field_validator normalise '' → None
        settings = _make_settings(proxy_url="")
        assert settings.proxy_url is None

        configs = self._run_storage_with("")
        for cfg in configs:
            assert cfg.proxies is None


# ─────────────────────────────────────────────────────────────
# ConsolidatorService — _http_client lifecycle
# ─────────────────────────────────────────────────────────────


class TestConsolidatorServiceProxy:
    """Vérifie le cycle de vie du httpx.AsyncClient dans ConsolidatorService."""

    def _make_consolidator(self, proxy_url):
        """Instancie ConsolidatorService en mockant AsyncOpenAI."""
        settings = _make_settings(
            proxy_url=proxy_url,
            llmaas_api_url="https://api.example.com/v1",
            llmaas_api_key="sk-test",
        )
        with (
            patch("live_mem.core.consolidator.get_settings", return_value=settings),
            patch("live_mem.core.consolidator.AsyncOpenAI"),
        ):
            from live_mem.core.consolidator import ConsolidatorService

            return ConsolidatorService()

    def test_http_client_created_when_proxy_set(self):
        """Avec PROXY_URL, _http_client doit être un httpx.AsyncClient."""
        import httpx

        svc = self._make_consolidator("http://proxy.example.com:3128")
        assert svc._http_client is not None
        assert isinstance(svc._http_client, httpx.AsyncClient)

    def test_no_http_client_without_proxy(self):
        """Sans PROXY_URL, _http_client doit être None."""
        svc = self._make_consolidator(None)
        assert svc._http_client is None

    def test_close_calls_aclose_and_resets_to_none(self):
        """close() doit appeler aclose() sur _http_client et le remettre à None."""
        svc = self._make_consolidator("http://proxy.example.com:3128")

        mock_client = AsyncMock()
        svc._http_client = mock_client

        asyncio.run(svc.close())

        mock_client.aclose.assert_awaited_once()
        assert svc._http_client is None

    def test_close_is_safe_without_proxy(self):
        """close() ne doit pas lever d'exception si _http_client est None."""
        svc = self._make_consolidator(None)
        assert svc._http_client is None
        # Ne doit pas lever
        asyncio.run(svc.close())

    def test_close_idempotent(self):
        """close() deux fois ne doit pas lever d'exception."""
        svc = self._make_consolidator("http://proxy.example.com:3128")

        mock_client = AsyncMock()
        svc._http_client = mock_client

        asyncio.run(svc.close())
        asyncio.run(svc.close())  # 2ème appel : _http_client est None

        mock_client.aclose.assert_awaited_once()  # Appelé une seule fois

    def test_close_consolidator_if_initialized_clears_singleton(self):
        """close_consolidator_if_initialized() doit fermer et remettre le singleton à None."""
        import live_mem.core.consolidator as _mod

        svc = self._make_consolidator("http://proxy.example.com:3128")

        mock_client = AsyncMock()
        svc._http_client = mock_client

        # Injecter dans le singleton
        _mod._consolidator = svc
        try:
            asyncio.run(_mod.close_consolidator_if_initialized())
            assert _mod._consolidator is None
            mock_client.aclose.assert_awaited_once()
        finally:
            _mod._consolidator = None  # Nettoyage

    def test_close_if_not_initialized_is_noop(self):
        """close_consolidator_if_initialized() sans singleton ne doit pas lever."""
        import live_mem.core.consolidator as _mod

        original = _mod._consolidator
        _mod._consolidator = None
        try:
            asyncio.run(_mod.close_consolidator_if_initialized())  # Ne doit pas lever
        finally:
            _mod._consolidator = original
