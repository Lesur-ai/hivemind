# -*- coding: utf-8 -*-
"""
Tests for PROXY_URL feature.

Covers:
- StorageService: proxy injected (or not) into boto3 Config objects
- ConsolidatorService: _http_client lifecycle (create, close)
- LLM health probes (public /health + authenticated system_health):
  owned proxied client lifecycle on every success and exception path (P12-1)
"""

import asyncio
import json
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


# ─────────────────────────────────────────────────────────────
# P12-1 — LLM health probes honor PROXY_URL with an owned client
# ─────────────────────────────────────────────────────────────

_PROXY = "http://proxy.example.com:3128"


class _FakeModels:
    """models.list() double returning a fixed model inventory."""

    def __init__(self, model_ids, error=None):
        self._model_ids = model_ids
        self._error = error

    async def list(self):
        if self._error is not None:
            raise self._error
        data = [MagicMock(id=model_id) for model_id in self._model_ids]
        return MagicMock(data=data)


def _probe_settings(**overrides):
    defaults = {
        "llmaas_api_url": "https://api.example.com/v1",
        "llmaas_api_key": "sk-test",
        "llmaas_model": "test-model",
    }
    defaults.update(overrides)
    return _make_settings(**defaults)


def _probe_rig(monkeypatch_none=None, *, model_ids=("test-model",), list_error=None,
               constructor_error=None):
    """Patch the probe seams and return (patches, captured) for assertions.

    ``captured`` records the constructed owned httpx client, its aclose mock,
    the httpx.AsyncClient kwargs, and the AsyncOpenAI kwargs.
    """
    captured = {
        "async_client_calls": [],
        "owned_client": None,
        "openai_kwargs": None,
    }

    def _fake_async_client(**kwargs):
        captured["async_client_calls"].append(kwargs)
        owned = MagicMock()
        owned.aclose = AsyncMock()
        captured["owned_client"] = owned
        return owned

    def _fake_openai(**kwargs):
        captured["openai_kwargs"] = kwargs
        if constructor_error is not None:
            raise constructor_error
        client = MagicMock()
        client.models = _FakeModels(list(model_ids), error=list_error)
        return client

    patches = [
        patch("live_mem.core.llm_probe.httpx.AsyncClient",
              side_effect=_fake_async_client),
        patch("live_mem.core.llm_probe.AsyncOpenAI", side_effect=_fake_openai),
    ]
    return patches, captured


class TestLlmProbeOwnedClientLifecycle:
    """Unit contract of the shared probe helper (list_llm_models)."""

    def _run(self, settings, **rig_kwargs):
        from live_mem.core.llm_probe import list_llm_models

        patches, captured = _probe_rig(**rig_kwargs)
        for p in patches:
            p.start()
        try:
            outcome = {"error": None, "model_ids": None}

            async def _invoke():
                try:
                    outcome["model_ids"] = await list_llm_models(settings)
                except Exception as exc:  # noqa: BLE001 — test captures it
                    outcome["error"] = exc

            asyncio.run(_invoke())
        finally:
            for p in reversed(patches):
                p.stop()
        return outcome, captured

    def test_proxy_builds_owned_client_and_passes_it_to_openai(self):
        import httpx

        settings = _probe_settings(proxy_url=_PROXY)
        outcome, captured = self._run(settings)

        assert outcome["error"] is None
        assert outcome["model_ids"] == ["test-model"]
        assert len(captured["async_client_calls"]) == 1
        kwargs = captured["async_client_calls"][0]
        assert isinstance(kwargs["proxy"], httpx.Proxy)
        assert kwargs["proxy"].url == httpx.URL(_PROXY)
        assert kwargs["timeout"] == 5
        assert captured["openai_kwargs"]["http_client"] is captured["owned_client"]
        assert captured["openai_kwargs"]["timeout"] == 5

    def test_no_proxy_keeps_direct_behavior_without_owned_client(self):
        settings = _probe_settings(proxy_url=None)
        outcome, captured = self._run(settings)

        assert outcome["error"] is None
        assert captured["async_client_calls"] == []
        assert captured["openai_kwargs"]["http_client"] is None

    def test_owned_client_closed_on_success(self):
        settings = _probe_settings(proxy_url=_PROXY)
        _, captured = self._run(settings)

        captured["owned_client"].aclose.assert_awaited_once()

    def test_owned_client_closed_when_provider_call_fails(self):
        settings = _probe_settings(proxy_url=_PROXY)
        outcome, captured = self._run(
            settings, list_error=RuntimeError("injected provider failure")
        )

        assert isinstance(outcome["error"], RuntimeError)
        captured["owned_client"].aclose.assert_awaited_once()

    def test_owned_client_closed_when_provider_call_times_out(self):
        settings = _probe_settings(proxy_url=_PROXY)
        outcome, captured = self._run(
            settings, list_error=asyncio.TimeoutError()
        )

        assert isinstance(outcome["error"], asyncio.TimeoutError)
        captured["owned_client"].aclose.assert_awaited_once()

    def test_owned_client_closed_when_openai_constructor_fails(self):
        settings = _probe_settings(proxy_url=_PROXY)
        outcome, captured = self._run(
            settings, constructor_error=RuntimeError("injected constructor failure")
        )

        assert isinstance(outcome["error"], RuntimeError)
        captured["owned_client"].aclose.assert_awaited_once()


class _ProbeStorage:
    """Offline storage double for the two health probes."""

    def __init__(self, status="ok"):
        self._status = status

    async def test_connection(self):
        return {"status": self._status}

    async def list_prefixes(self, prefix):
        return ["project/", "_system/"]


class TestSystemHealthProbeProxy:
    """system_health (authenticated MCP tool) probes through PROXY_URL."""

    def _run_tool(self, settings, storage=None, **rig_kwargs):
        from mcp.server.fastmcp import FastMCP

        from live_mem.tools.system import register as register_system_tools

        mcp = FastMCP(name="probe-test")
        register_system_tools(mcp)
        tool = mcp._tool_manager._tools["system_health"]
        fn = None
        for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
            candidate = getattr(tool, attr, None)
            if callable(candidate):
                fn = candidate
                break
        assert fn is not None, "system_health tool has no callable"

        patches, captured = _probe_rig(**rig_kwargs)
        patches.extend(
            [
                patch("live_mem.config.get_settings", return_value=settings),
                patch(
                    "live_mem.core.storage.get_storage",
                    return_value=storage or _ProbeStorage(),
                ),
            ]
        )
        for p in patches:
            p.start()
        try:
            result = asyncio.run(fn())
        finally:
            for p in reversed(patches):
                p.stop()
        return result, captured

    def test_probe_uses_proxy_and_reports_model_availability(self):
        settings = _probe_settings(proxy_url=_PROXY)
        result, captured = self._run_tool(settings)

        assert result["services"]["llmaas"] == {
            "status": "ok",
            "model": "test-model",
            "model_available": True,
            "latency_ms": result["services"]["llmaas"]["latency_ms"],
        }
        assert captured["openai_kwargs"]["http_client"] is captured["owned_client"]
        captured["owned_client"].aclose.assert_awaited_once()

    def test_probe_without_proxy_stays_direct(self):
        settings = _probe_settings(proxy_url=None)
        result, captured = self._run_tool(settings)

        assert result["services"]["llmaas"]["status"] == "ok"
        assert captured["async_client_calls"] == []
        assert captured["openai_kwargs"]["http_client"] is None

    def test_probe_failure_closes_owned_client_and_stays_generic(self):
        settings = _probe_settings(proxy_url=_PROXY)
        result, captured = self._run_tool(
            settings, list_error=RuntimeError("provider detail: sk-secret leaked")
        )

        assert result["services"]["llmaas"] == {
            "status": "error",
            "message": "LLMaaS unreachable",
        }
        captured["owned_client"].aclose.assert_awaited_once()

    def test_model_unavailable_is_still_reported(self):
        settings = _probe_settings(proxy_url=_PROXY)
        result, _ = self._run_tool(settings, model_ids=("another-model",))

        assert result["services"]["llmaas"]["model_available"] is False


class _SendCollector:
    """ASGI send collector for the /health handler."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def body(self):
        return json.loads(self.messages[1]["body"].decode("utf-8"))


class TestPublicHealthProbeProxy:
    """Public /health endpoint probes through PROXY_URL with redaction."""

    def _run_health(self, settings, storage=None, **rig_kwargs):
        from live_mem.auth.middleware import StaticFilesMiddleware

        middleware = object.__new__(StaticFilesMiddleware)
        collector = _SendCollector()

        patches, captured = _probe_rig(**rig_kwargs)
        patches.extend(
            [
                patch("live_mem.config.get_settings", return_value=settings),
                patch(
                    "live_mem.core.storage.get_storage",
                    return_value=storage or _ProbeStorage(),
                ),
            ]
        )
        for p in patches:
            p.start()
        try:
            asyncio.run(middleware._handle_health(collector))
        finally:
            for p in reversed(patches):
                p.stop()
        return collector, captured

    def test_probe_uses_proxy_and_keeps_public_redaction(self):
        settings = _probe_settings(proxy_url=_PROXY)
        collector, captured = self._run_health(settings)

        assert collector.status == 200
        body = collector.body
        assert body["status"] == "healthy"
        # Public redaction: no model name, no model inventory — only
        # status and latency may appear on the anonymous endpoint.
        assert set(body["services"]["llmaas"].keys()) == {"status", "latency_ms"}
        assert body["services"]["llmaas"]["status"] == "ok"
        assert captured["openai_kwargs"]["http_client"] is captured["owned_client"]
        captured["owned_client"].aclose.assert_awaited_once()

    def test_probe_without_proxy_stays_direct(self):
        settings = _probe_settings(proxy_url=None)
        collector, captured = self._run_health(settings)

        assert collector.status == 200
        assert captured["async_client_calls"] == []
        assert captured["openai_kwargs"]["http_client"] is None

    def test_probe_failure_closes_owned_client_and_keeps_status_semantics(self):
        settings = _probe_settings(proxy_url=_PROXY)
        collector, captured = self._run_health(
            settings, list_error=RuntimeError("provider detail must not leak")
        )

        # S3 ok + LLM down = degraded 200 (unchanged HTTP semantics),
        # generic client message only.
        assert collector.status == 200
        body = collector.body
        assert body["status"] == "degraded"
        assert body["services"]["llmaas"] == {
            "status": "error",
            "message": "LLMaaS unreachable",
        }
        captured["owned_client"].aclose.assert_awaited_once()
