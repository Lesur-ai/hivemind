# -*- coding: utf-8 -*-
"""
Tests for PROXY_URL feature.

Covers:
- StorageService: proxy injected (or not) into boto3 Config objects
- ConsolidatorService: transport ownership after the P13-1C migration
- LLM health probes (public /health + authenticated system_health): proxied
  routing, discovery-only cost, and the public/authenticated field split

P13-1C (#276) note: the LLM transport moved out of the consolidator and the
retired ``core/llm_probe.py`` into the ONE shared inference runtime, so the
probe tests now drive the real registered adapter against the deterministic
``InferenceEmulator`` (used as an HTTP proxy or as a direct origin) instead of
mocking an SDK constructor. Proxy routing is proven positively: the external
origin uses the reserved ``.invalid`` TLD, so a recorded absolute-form request
is the only way the call could have succeeded.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from live_mem.config import Settings
from tests.fakes.inference_emulator import InferenceEmulator
from tests.fakes.inference_fakes import (
    core_inference_runtime,
    make_chat_profile,
    make_embedding_profile,
)


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
    """P13-1C : le consolidateur ne possède plus AUCUN transport.

    Le transport sortant (proxy compris) appartient au runtime d'inférence
    partagé, qui le ferme sur le shutdown ASGI. Ce qui reste vérifiable ici est
    la conséquence directe : le service ne peut plus créer ni libérer un
    transport, donc ni un appel parasite à ``close()`` ni la réinitialisation
    du singleton ne peut couper l'egress d'une autre opération en vol.
    """

    def _make_consolidator(self, proxy_url):
        """Instancie ConsolidatorService avec un profil chat résolu."""
        settings = _make_settings(proxy_url=proxy_url)
        with (
            patch("live_mem.core.consolidator.get_settings", return_value=settings),
            core_inference_runtime(proxy_url=proxy_url),
        ):
            from live_mem.core.consolidator import ConsolidatorService

            return ConsolidatorService()

    def test_service_owns_no_transport(self):
        """Ni avec ni sans PROXY_URL : plus d'attribut de transport possédé."""
        for proxy_url in ("http://proxy.example.com:3128", None):
            svc = self._make_consolidator(proxy_url)
            assert not hasattr(svc, "_http_client")
            assert not hasattr(svc, "_client")

    def test_close_is_a_noop_and_idempotent(self):
        """``close()`` reste appelable (ordre d'arrêt historique) mais ne peut
        plus fermer quoi que ce soit — y compris appelé deux fois."""
        svc = self._make_consolidator("http://proxy.example.com:3128")
        assert asyncio.run(svc.close()) is None
        assert asyncio.run(svc.close()) is None

    def test_close_consolidator_if_initialized_clears_singleton(self):
        """``close_consolidator_if_initialized()`` réinitialise le singleton
        SANS toucher au transport partagé du runtime."""
        import live_mem.core.consolidator as _mod

        svc = self._make_consolidator("http://proxy.example.com:3128")

        _mod._consolidator = svc
        try:
            asyncio.run(_mod.close_consolidator_if_initialized())
            assert _mod._consolidator is None
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

    def test_shared_runtime_transport_survives_a_stray_service_close(self):
        """Preuve non-vacante : le transport du runtime reste ouvert après un
        ``close()`` du service, et seul ``aclose()`` du runtime le libère."""
        from live_mem.core import inference_runtime as core_runtime

        async def _exercise():
            with core_inference_runtime(proxy_url="http://proxy.example.com:3128"):
                svc = self._make_consolidator("http://proxy.example.com:3128")
                runtime = core_runtime.get_inference_runtime()
                transport = runtime.chat_provider()._owned_http_client
                await svc.close()
                assert not transport.is_closed
                await runtime.aclose()
                assert transport.is_closed

        asyncio.run(_exercise())


class TestStorageProxyLogRedaction:
    """P12-3 R2 (#268, Codex round 2) — le log de démarrage du StorageService
    cœur ne doit exposer que l'origine scheme://host:port du proxy, jamais le
    userinfo (le chemin d'initialisation S3 est aussi atteignable que celui du
    consolidateur)."""

    def test_startup_log_is_display_safe(self, caplog):
        import logging

        settings = _make_settings(
            proxy_url="http://svc-user:s3cr3t-pw@proxy.example.com:3128",
        )
        with (
            patch("live_mem.core.storage.get_settings", return_value=settings),
            patch("live_mem.core.storage.boto3.client", return_value=MagicMock()),
            caplog.at_level(logging.INFO, logger="live_mem.storage"),
        ):
            from live_mem.core.storage import StorageService

            StorageService()

        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "proxy.example.com:3128" in joined
        assert "s3cr3t-pw" not in joined
        assert "svc-user" not in joined


class TestCoreStorageProxyErrorRedaction:
    """P12-3 R8 (#268) — le StorageService du CŒUR reçoit la même frontière
    de redaction que le service graph-memory embarqué : une erreur proxy
    botocore ne doit atteindre ni les consommateurs de ``str(e)`` (outils
    MCP, consolidateur, sondes) ni le payload récupéré de test_connection
    (forwardé verbatim par /health public et system_health)."""

    _SECRET = (
        "http://svc-user:s3cr3t@pw@proxy.internal:3128"
        "?access_token=qs3cr3t#fr4g"
    )

    def _make_core_storage(self, boom_method=None, boom=None):
        settings = _make_settings(proxy_url="http://proxy.example.com:3128")
        client = MagicMock()
        if boom_method:
            getattr(client, boom_method).side_effect = boom
        with (
            patch("live_mem.core.storage.get_settings", return_value=settings),
            patch("live_mem.core.storage.boto3.client", return_value=client),
        ):
            from live_mem.core.storage import StorageService

            return StorageService()

    def _assert_clean(self, surface):
        assert "s3cr3t" not in surface
        assert "pw@" not in surface
        assert "svc-user" not in surface
        assert "qs3cr3t" not in surface
        assert "fr4g" not in surface

    def test_raised_proxy_error_is_redacted(self):
        from botocore.exceptions import ProxyConnectionError

        svc = self._make_core_storage(
            "put_object", ProxyConnectionError(proxy_url=self._SECRET)
        )
        with pytest.raises(ProxyConnectionError) as excinfo:
            asyncio.run(svc.put("spaces/x/_meta.json", "{}"))
        self._assert_clean(str(excinfo.value))
        assert "proxy.internal:3128" in str(excinfo.value)

    def test_recovered_test_connection_payload_is_redacted(self):
        from botocore.exceptions import ProxyConnectionError

        svc = self._make_core_storage(
            "head_bucket", ProxyConnectionError(proxy_url=self._SECRET)
        )
        result = asyncio.run(svc.test_connection())
        assert result["status"] == "error"
        self._assert_clean(result["message"])

    def test_recovered_client_error_payload_is_redacted(self):
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": f"via {self._SECRET}"}},
            "HeadBucket",
        )
        svc = self._make_core_storage("head_bucket", err)
        result = asyncio.run(svc.test_connection())
        assert result["status"] == "error"
        assert "AccessDenied" in result["message"]
        self._assert_clean(result["message"])


class TestProxyUrlErrorEchoRedaction:
    """P12-3 R2/R3/R4 — une valeur PROXY_URL invalide ET porteuse de
    credentials ne doit fuiter NI dans le message d'erreur de démarrage NI
    dans une charge pydantic structurée (cœur)."""

    def test_invalid_scheme_error_never_echoes_credentials(self):
        """R3/R4 : userinfo ET query/fragment retirés ; l'erreur n'est PAS
        une ValidationError pydantic (dont input_value / errors()[0]['input']
        répéteraient la valeur brute) — RuntimeError propagée sans wrapping,
        démarrage toujours fail-closed."""
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(RuntimeError) as excinfo:
            _make_settings(
                proxy_url=(
                    "socks5://svc-user:s3cr3t@pw@proxy.internal:1080"
                    "?access_token=qs3cr3t#fr4g"
                )
            )
        assert not isinstance(excinfo.value, ValidationError)
        message = str(excinfo.value)
        assert "PROXY_URL must start" in message
        assert "s3cr3t" not in message
        assert "pw@" not in message
        assert "svc-user" not in message
        assert "qs3cr3t" not in message
        assert "access_token" not in message
        assert "fr4g" not in message
        assert "proxy.internal:1080" in message


class TestInferenceProxyLogRedaction:
    """P12-3 (#268) → P13-1C (#276) : le signal opérateur « l'egress
    d'inférence passe par un proxy » survit à la migration, mais il est émis
    par le SEUL propriétaire du transport — le runtime partagé. ADR-0027
    classe l'endpoint proxy (hôte ET port) comme configuration sensible, donc
    le rendu est plus strict que la baseline P12-3 : seul le schéma survit.
    """

    def test_shared_runtime_startup_log_is_display_safe(self, caplog):
        import logging

        from hivemind_inference import InferenceRuntime

        from tests.fakes.inference_fakes import make_inference_config

        with caplog.at_level(logging.INFO, logger="hivemind_inference.runtime"):
            InferenceRuntime(
                make_inference_config(chat=True, embedding=True),
                proxy_url="http://svc-user:s3cr3t-pw@proxy.example.com:3128",
            )

        joined = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name == "hivemind_inference.runtime"
        )
        assert "via proxy" in joined
        assert "s3cr3t-pw" not in joined
        assert "svc-user" not in joined
        assert "proxy.example.com" not in joined
        assert "3128" not in joined

    def test_no_proxy_emits_no_egress_line(self, caplog):
        """Non-vacuité : la ligne n'est émise QUE lorsqu'un proxy est
        configuré, donc sa présence ci-dessus prouve bien le signal."""
        import logging

        from hivemind_inference import InferenceRuntime

        from tests.fakes.inference_fakes import make_inference_config

        with caplog.at_level(logging.INFO, logger="hivemind_inference.runtime"):
            InferenceRuntime(make_inference_config(chat=True), proxy_url=None)

        assert not [
            record
            for record in caplog.records
            if record.name == "hivemind_inference.runtime"
        ]


# ─────────────────────────────────────────────────────────────
# P12-1 / P13-1C — LLM health probes honor PROXY_URL through the shared
# inference runtime (discovery-only, zero provider tokens)
# ─────────────────────────────────────────────────────────────

# Unresolvable external origin (RFC 2606): a recorded absolute-form request on
# the emulator-as-proxy is then POSITIVE proof of proxy routing, because a
# direct attempt could never have succeeded.
_EXTERNAL_ORIGIN = "http://llm.p13-1c.invalid/v1"
_EMULATED_CHAT_MODEL = "emulated-chat-model"
_EMULATED_EMBEDDING_MODEL = "emulated-embedding-model"


def _probe_settings(**overrides):
    return _make_settings(**overrides)


def _probe_profiles(endpoint: str, *, chat_model: str = _EMULATED_CHAT_MODEL):
    """Resolved chat+embedding profiles pointing at ``endpoint``."""
    return {
        "chat": make_chat_profile(endpoint=endpoint, configured_model=chat_model),
        "embedding": make_embedding_profile(
            endpoint=endpoint, configured_model=_EMULATED_EMBEDDING_MODEL
        ),
    }


class _ProbeStorage:
    """Offline storage double for the two health probes."""

    def __init__(self, status="ok"):
        self._status = status

    async def test_connection(self):
        return {"status": self._status}

    async def list_prefixes(self, prefix):
        return ["project/", "_system/"]


def _system_health_callable():
    from mcp.server.fastmcp import FastMCP

    from live_mem.tools.system import register as register_system_tools

    mcp = FastMCP(name="probe-test")
    register_system_tools(mcp)
    tool = mcp._tool_manager._tools["system_health"]
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        candidate = getattr(tool, attr, None)
        if callable(candidate):
            return candidate
    raise AssertionError("system_health tool has no callable")


class TestSystemHealthProbeProxy:
    """system_health (authenticated MCP tool) probes through PROXY_URL."""

    async def _run_tool(self, *, proxy_url, endpoint, storage=None, **profile_kwargs):
        fn = _system_health_callable()
        settings = _probe_settings(proxy_url=proxy_url)
        with (
            patch("live_mem.config.get_settings", return_value=settings),
            patch(
                "live_mem.core.storage.get_storage",
                return_value=storage or _ProbeStorage(),
            ),
            core_inference_runtime(
                proxy_url=proxy_url, **_probe_profiles(endpoint, **profile_kwargs)
            ) as runtime,
        ):
            try:
                return await fn()
            finally:
                await runtime.aclose()

    async def test_probe_uses_proxy_and_reports_model_availability(self):
        async with InferenceEmulator() as proxy:
            result = await self._run_tool(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )
        block = result["services"]["llmaas"]
        assert block["status"] == "ok"
        assert block["model"] == _EMULATED_CHAT_MODEL
        assert block["model_available"] is True
        assert isinstance(block["latency_ms"], float)
        # Positive routing proof: absolute-form target toward the unresolvable
        # external origin, and discovery-only (never a paid operation).
        assert proxy.requests
        for request in proxy.requests:
            assert request["method"] == "GET"
            assert request["url"].startswith("http://llm.p13-1c.invalid")
            assert request["url"].endswith("/models")

    async def test_probe_without_proxy_stays_direct(self):
        async with InferenceEmulator() as origin:
            result = await self._run_tool(
                proxy_url=None, endpoint=origin.v1_url
            )
        assert result["services"]["llmaas"]["status"] == "ok"
        assert origin.requests
        # Path-form target = direct connection to the origin itself.
        for request in origin.requests:
            assert request["url"] == "/v1/models"

    async def test_probe_failure_stays_generic(self):
        """A provider failure must never surface provider text on the tool."""
        async with InferenceEmulator(
            scripted=[
                {"status": 500, "body": {"error": {"message": "sk-secret leaked"}}},
                {"status": 500, "body": {"error": {"message": "sk-secret leaked"}}},
            ]
        ) as proxy:
            result = await self._run_tool(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )
        block = result["services"]["llmaas"]
        assert block["status"] == "error"
        assert block["message"] == "LLMaaS unreachable"
        assert "sk-secret" not in json.dumps(result)

    async def test_model_unavailable_is_still_reported(self):
        async with InferenceEmulator() as proxy:
            result = await self._run_tool(
                proxy_url=proxy.url,
                endpoint=_EXTERNAL_ORIGIN,
                chat_model="a-model-the-provider-does-not-list",
            )
        assert result["services"]["llmaas"]["model_available"] is False
        assert result["services"]["llmaas"]["status"] == "ok"

    async def test_authenticated_block_adds_safe_role_identity(self):
        """ADR-0027 additive role children: the authenticated surface may carry
        provider/adapter/model identity and the expected embedding dimension —
        and nothing secret-bearing."""
        async with InferenceEmulator() as proxy:
            result = await self._run_tool(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )
        block = result["services"]["llmaas"]
        assert block["chat"]["provider_id"] == "openai-compatible"
        assert block["chat"]["adapter_id"] == "openai-compatible"
        assert block["chat"]["configured_model"] == _EMULATED_CHAT_MODEL
        assert block["chat"]["readiness"] == "unknown"
        assert block["chat"]["evidence"] == "discovery"
        assert block["embedding"]["expected_dimensions"] == 1024
        serialized = json.dumps(result)
        assert "llm.p13-1c.invalid" not in serialized
        assert "test-key" not in serialized


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


# Exact anonymous ADR-0027 role-child field set for the PUBLIC endpoint. No
# provider, adapter, model, dimension, endpoint/fingerprint, or error category
# may appear below the historical fields (HM-18).
_PUBLIC_CHILD_KEYS = {
    "status",
    "configured",
    "connectivity",
    "discovery",
    "model_available",
    "readiness",
    "evidence",
}


class TestPublicHealthProbeProxy:
    """Public /health endpoint probes through PROXY_URL with redaction."""

    async def _run_health(self, *, proxy_url, endpoint, storage=None, **profile_kwargs):
        from live_mem.auth.middleware import StaticFilesMiddleware

        middleware = object.__new__(StaticFilesMiddleware)
        collector = _SendCollector()
        settings = _probe_settings(proxy_url=proxy_url)

        with (
            patch("live_mem.config.get_settings", return_value=settings),
            patch(
                "live_mem.core.storage.get_storage",
                return_value=storage or _ProbeStorage(),
            ),
            core_inference_runtime(
                proxy_url=proxy_url, **_probe_profiles(endpoint, **profile_kwargs)
            ) as runtime,
        ):
            try:
                await middleware._handle_health(collector)
            finally:
                await runtime.aclose()
        return collector

    async def test_probe_uses_proxy_and_keeps_public_redaction(self):
        async with InferenceEmulator() as proxy:
            collector = await self._run_health(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )

        assert collector.status == 200
        body = collector.body
        assert body["status"] == "healthy"
        block = body["services"]["llmaas"]
        # Public redaction: the historical top-level fields plus the additive
        # ANONYMOUS role children — no model name, no model inventory, no
        # provider identity.
        assert set(block.keys()) == {"status", "latency_ms", "chat", "embedding"}
        assert block["status"] == "ok"
        assert set(block["chat"].keys()) == _PUBLIC_CHILD_KEYS | {"latency_ms"}
        assert set(block["embedding"].keys()) == _PUBLIC_CHILD_KEYS | {"latency_ms"}
        serialized = json.dumps(body)
        for forbidden in (
            _EMULATED_CHAT_MODEL,
            _EMULATED_EMBEDDING_MODEL,
            "openai-compatible",
            "llm.p13-1c.invalid",
            "test-key",
        ):
            assert forbidden not in serialized
        # Positive routing proof through the proxy, discovery-only.
        assert proxy.requests
        for request in proxy.requests:
            assert request["method"] == "GET"
            assert request["url"].endswith("/models")

    async def test_probe_without_proxy_stays_direct(self):
        async with InferenceEmulator() as origin:
            collector = await self._run_health(
                proxy_url=None, endpoint=origin.v1_url
            )

        assert collector.status == 200
        assert origin.requests
        for request in origin.requests:
            assert request["url"] == "/v1/models"

    async def test_probe_failure_keeps_status_semantics(self):
        async with InferenceEmulator(
            scripted=[
                {"status": 500, "body": {"error": {"message": "must not leak"}}},
                {"status": 500, "body": {"error": {"message": "must not leak"}}},
            ]
        ) as proxy:
            collector = await self._run_health(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )

        # S3 ok + LLM down = degraded 200 (unchanged HTTP semantics),
        # generic client message only.
        assert collector.status == 200
        body = collector.body
        assert body["status"] == "degraded"
        block = body["services"]["llmaas"]
        assert block["status"] == "error"
        assert block["message"] == "LLMaaS unreachable"
        assert "must not leak" not in json.dumps(body)
        # The anonymous children still report the failure WITHOUT a category.
        assert block["chat"]["status"] == "error"
        assert "error_category" not in block["chat"]

    async def test_unconfigured_roles_keep_the_historical_warning_shape(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        middleware = object.__new__(StaticFilesMiddleware)
        collector = _SendCollector()
        settings = _probe_settings(proxy_url=None)
        with (
            patch("live_mem.config.get_settings", return_value=settings),
            patch(
                "live_mem.core.storage.get_storage", return_value=_ProbeStorage()
            ),
            core_inference_runtime(chat=False, embedding=False),
        ):
            await middleware._handle_health(collector)

        block = collector.body["services"]["llmaas"]
        assert block["status"] == "warning"
        assert block["message"] == "LLMaaS is not configured"
        assert block["chat"]["configured"] is False
        assert block["chat"]["connectivity"] == "not_configured"
        assert block["chat"]["discovery"] == "not_run"
        assert block["embedding"]["configured"] is False

    async def test_endpoint_without_model_listing_is_not_a_failure(self):
        """ADR-0027 (credit @sylvainkalache, public PR #11): an endpoint that
        answers but does not implement ``/models`` is reachable, not down."""
        async with InferenceEmulator(
            scripted=[{"status": 404}, {"status": 404}]
        ) as proxy:
            collector = await self._run_health(
                proxy_url=proxy.url, endpoint=_EXTERNAL_ORIGIN
            )

        body = collector.body
        assert body["status"] == "healthy"
        block = body["services"]["llmaas"]
        assert block["status"] == "ok"
        assert block["chat"]["discovery"] == "unsupported"
        assert block["chat"]["connectivity"] == "reachable"
        assert block["chat"]["model_available"] is None
        assert block["chat"]["evidence"] == "connectivity"
