# -*- coding: utf-8 -*-
"""
P12-3 (#268) baseline re-proven through the P13-1C (#276) shared inference
boundary — Graph Memory `PROXY_URL`: runtime routing, fail-closed trap, client
lifecycle.

The frozen P12-3 egress contract is UNCHANGED; what changed is who owns the
transport. `ExtractorService` / `EmbeddingService` no longer construct an
`AsyncOpenAI` client each: they consume the registered `hivemind_inference`
adapters through the service-wide runtime, which owns the proxied transport and
closes it on the ASGI shutdown path. This file therefore keeps proving the same
properties against the migrated consumers:

- a deterministic in-process listener (the shared dual-shape
  ``InferenceEmulator``, absolute-form request lines, canned OpenAI-shaped
  responses, per-request recording) observes every expected extraction,
  embedding, and provider-health request;
- the LLM origin hostnames use the reserved ``.invalid`` TLD (RFC 2606), so a
  bypassing direct connection cannot succeed even accidentally — any recorded
  proxy request is positive proof of routing, and any successful call proves no
  direct path was used;
- a direct-network trap (live local origin listener + failing proxy) proves
  that proxy connection, authentication (407), and timeout failures raise and
  NEVER fall back to a direct connection;
- retries re-traverse the proxy, never a direct second attempt — under the
  ADR-0027 contract that now replaces the historical `tenacity` layers: SDK
  retries are gone with the SDK, and only an explicitly transient rate limit
  with a bounded ``Retry-After`` (or a pre-send connect failure with NO proxy
  configured) may retry at all, exactly once;
- health probes are discovery-only: the proxy records ``/models`` and NEVER a
  chat completion or an embedding, so a health call spends zero provider
  tokens;
- without a proxy the request reaches the origin directly, in path-form;
- the owned transports live in the service-wide inference runtime: they survive
  an in-flight cancellation and close on the ASGI lifespan shutdown path.

No real network: every listener binds 127.0.0.1 on an ephemeral port; every
must-not-resolve origin uses ``.invalid``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes.inference_emulator import InferenceEmulator, openai_chat_payload

_REPO_ROOT = Path(__file__).resolve().parents[1]

_LLM_ORIGIN = "http://llm.p12-3-hivemind.invalid"
_SECRET_PROXY = "http://svc-user:s3cr3t-pw@proxy.internal:3128"

# Legacy LLMAAS_* resolution through the shared boundary: the embedding
# dimension defaults to the frozen 1.x value, so the emulator must answer with
# exactly that shape or the adapter fails the batch as ``invalid_response``.
_LEGACY_EMBEDDING_DIMENSIONS = 1024
_CERTIFICATION_ENV = (
    "HIVEMIND_CERTIFICATION_BUDGET_PATH",
    "HIVEMIND_CERTIFICATION_RUN_ID",
    "HIVEMIND_CERTIFICATION_SOURCE_SHA",
    "HIVEMIND_CERTIFICATION_PROFILE_ID",
)
_ACTUAL_DISCOVERY_CONTRACT = object()
_HEALTH_DISCOVERY_IDENTITIES = {
    "chat": {
        "role": "chat",
        "provider_id": "test-chat",
        "endpoint": "https://chat.p12-3-hivemind.invalid/v1",
        "configured_model": "configured-chat",
    },
    "embedding": {
        "role": "embedding",
        "provider_id": "test-embedding",
        "endpoint": "https://embedding.p12-3-hivemind.invalid/v1",
        "configured_model": "configured-embedding",
    },
}


# --------------------------------------------------------------------------- #
# Env helper (same contract as tests/test_p7_9_vendored_storage_signature.py) #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _gm_baseline_env(monkeypatch):
    """Order-independence: importing any ``mcp_memory`` module executes the
    module-level ``Settings()`` (required credential fields), so a baseline
    env must exist BEFORE each test body's imports, standalone or full-suite."""
    _set_gm_env(monkeypatch, None)
    for name in _CERTIFICATION_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
async def _gm_runtime_reset():
    """Close and reset the Graph Memory inference runtime around every test.

    The runtime snapshot is per PROCESS and its adapters own real ``httpx``
    transports bound to this test's env and emulator, so a leaked runtime would
    both leak a socket and silently serve the next test the previous test's
    profiles.
    """
    from mcp_memory.core.inference_runtime import (
        close_inference_runtime_if_initialized,
        reset_inference_runtime_for_tests,
    )

    reset_inference_runtime_for_tests()
    yield
    # Close FIRST (releasing any real transport), then lift the terminal
    # shutdown flag: leaving it raised would make every later test in the
    # session see a service that has already shut down.
    await close_inference_runtime_if_initialized()
    reset_inference_runtime_for_tests()


def _set_gm_env(monkeypatch, proxy_url, api_url=_LLM_ORIGIN + "/v1"):
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3.test.invalid:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_REGION_NAME", "fr1")
    monkeypatch.setenv("LLMAAS_API_KEY", "test-llm-key")
    monkeypatch.setenv("LLMAAS_API_URL", api_url)
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")
    monkeypatch.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    if proxy_url is None:
        monkeypatch.delenv("PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("PROXY_URL", proxy_url)


def _fresh(monkeypatch, factory, proxy_url, api_url=_LLM_ORIGIN + "/v1", **env):
    """Build a GM service with fresh settings AND a fresh inference runtime
    pinned to this test's env (both snapshots are per-process)."""
    _set_gm_env(monkeypatch, proxy_url, api_url=api_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from mcp_memory.config import get_settings
    from mcp_memory.core.inference_runtime import reset_inference_runtime_for_tests

    get_settings.cache_clear()
    reset_inference_runtime_for_tests()
    try:
        return factory()
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Canned payloads                                                             #
# --------------------------------------------------------------------------- #

_EXTRACTION_JSON = {
    "entities": [
        {"name": "Hivemind", "type": "Product", "description": "memory service"}
    ],
    "relations": [],
    "summary": "canned summary",
    "key_topics": ["memory"],
}


def _extraction_emulator(scripted=None) -> InferenceEmulator:
    """Emulator whose canned chat answer parses as an extraction result and
    whose embeddings match the legacy 1024-dimension profile."""
    emulator = InferenceEmulator(
        scripted, embedding_dimensions=_LEGACY_EMBEDDING_DIMENSIONS
    )
    canned = emulator._canned

    def _extraction_canned(method, target, body):
        status, payload = canned(method, target, body)
        if method == "POST" and target.split("?", 1)[0].endswith("/chat/completions"):
            payload = openai_chat_payload(json.dumps(_EXTRACTION_JSON))
        return status, payload

    emulator._canned = _extraction_canned
    return emulator


def _extractor():
    from mcp_memory.core.extractor import ExtractorService

    return ExtractorService()


def _embedder():
    from mcp_memory.core.embedder import EmbeddingService

    return EmbeddingService()


async def _closed_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


def _runtime():
    from mcp_memory.core.inference_runtime import get_inference_runtime

    return get_inference_runtime()


def _recording_health_service(
    monkeypatch,
    role: str,
    *,
    discovery_contract: str | None | object = _ACTUAL_DISCOVERY_CONTRACT,
):
    """Build one Graph health consumer with a kwarg-recording fake probe."""

    from mcp_memory.core import inference_runtime as gm_runtime
    from mcp_memory.core import embedder as embedder_module
    from mcp_memory.core import extractor as extractor_module

    calls: list[dict] = []
    discovery_calls: list[dict] = []

    class _Probe:
        async def probe(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(healthy=True)

    probe = _Probe()
    chat = SimpleNamespace(**_HEALTH_DISCOVERY_IDENTITIES["chat"])
    embedding = SimpleNamespace(**_HEALTH_DISCOVERY_IDENTITIES["embedding"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(chat=chat, embedding=embedding),
        chat_probe=lambda: probe,
        embedding_probe=lambda: probe,
    )
    monkeypatch.setattr(gm_runtime, "get_inference_runtime", lambda: runtime)

    if role == "chat":
        consumer_module = extractor_module
        service = object.__new__(extractor_module.ExtractorService)
        service._model = chat.configured_model
    else:
        consumer_module = embedder_module
        service = object.__new__(embedder_module.EmbeddingService)
        service._model = embedding.configured_model
        service._dimensions = _LEGACY_EMBEDDING_DIMENSIONS

    actual_discovery = consumer_module.protected_certification_model_discovery

    def _discovery(**kwargs):
        discovery_calls.append(kwargs)
        if discovery_contract is _ACTUAL_DISCOVERY_CONTRACT:
            return actual_discovery(**kwargs)
        return discovery_contract

    monkeypatch.setattr(
        consumer_module,
        "protected_certification_model_discovery",
        _discovery,
    )
    return service, calls, discovery_calls


def _activate_strict_certification_environment(monkeypatch) -> None:
    values = {
        "HIVEMIND_CERTIFICATION_BUDGET_PATH": (
            "/run/hivemind-provider-certification/budget.sqlite3"
        ),
        "HIVEMIND_CERTIFICATION_RUN_ID": "12345.1",
        "HIVEMIND_CERTIFICATION_SOURCE_SHA": "a" * 40,
        "HIVEMIND_CERTIFICATION_PROFILE_ID": "public-test-reference",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


# --------------------------------------------------------------------------- #
# Routing through the fake proxy (extraction / embeddings / provider-health)  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ("chat", "embedding"))
async def test_graph_health_preserves_adapter_probe_default_outside_certification(
    monkeypatch, role
):
    service, calls, discovery_calls = _recording_health_service(monkeypatch, role)

    result = await service.test_connection()

    assert result["status"] == "ok"
    assert calls == [{}]
    assert discovery_calls == [_HEALTH_DISCOVERY_IDENTITIES[role]]


@pytest.mark.parametrize("role", ("chat", "embedding"))
async def test_graph_health_uses_exact_strict_certification_discovery_timeout(
    monkeypatch, role
):
    _activate_strict_certification_environment(monkeypatch)
    service, calls, discovery_calls = _recording_health_service(
        monkeypatch,
        role,
        discovery_contract="available",
    )

    result = await service.test_connection()

    assert result["status"] == "ok"
    assert calls == [{"timeout_seconds": 60.0}]
    assert discovery_calls == [_HEALTH_DISCOVERY_IDENTITIES[role]]


@pytest.mark.parametrize("role", ("chat", "embedding"))
async def test_graph_health_skips_unsupported_catalogue_in_strict_mode(
    monkeypatch, role
):
    _activate_strict_certification_environment(monkeypatch)
    service, calls, discovery_calls = _recording_health_service(
        monkeypatch,
        role,
        discovery_contract="unsupported",
    )

    result = await service.test_connection()

    assert result["status"] == "ok"
    assert calls == []
    assert discovery_calls == [_HEALTH_DISCOVERY_IDENTITIES[role]]
    assert "Catalogue" in result["message"]


@pytest.mark.parametrize("role", ("chat", "embedding"))
async def test_graph_health_partial_certification_context_fails_before_probe(
    monkeypatch, role
):
    monkeypatch.setenv("HIVEMIND_CERTIFICATION_RUN_ID", "12345.1")
    service, calls, discovery_calls = _recording_health_service(monkeypatch, role)

    result = await service.test_connection()

    assert result["status"] == "error"
    assert calls == []
    assert discovery_calls == [_HEALTH_DISCOVERY_IDENTITIES[role]]


class TestProxiedRouting:
    async def test_extraction_goes_through_proxy(self, monkeypatch):
        async with _extraction_emulator() as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            result = await svc.extract_from_text("Hivemind stores memory.")
        assert result.summary == "canned summary"
        assert len(result.entities) == 1
        assert len(proxy.requests) == 1
        request = proxy.requests[0]
        # Absolute-form target = true proxy semantics toward the unresolvable
        # external origin (a direct attempt could never succeed on .invalid).
        assert request["method"] == "POST"
        assert request["url"].startswith(_LLM_ORIGIN)
        assert request["url"].endswith("/chat/completions")

    async def test_embeddings_and_query_go_through_proxy(self, monkeypatch):
        async with _extraction_emulator() as proxy:
            svc = _fresh(monkeypatch, _embedder, proxy.url)
            vectors = await svc.embed_texts(["a", "b", "c"])
            query_vec = await svc.embed_query("question")
        assert len(vectors) == 3
        assert len(query_vec) == _LEGACY_EMBEDDING_DIMENSIONS
        assert len(proxy.requests) == 2
        for request in proxy.requests:
            assert request["url"].startswith(_LLM_ORIGIN)
            assert request["url"].endswith("/embeddings")

    async def test_provider_health_probes_go_through_proxy_and_spend_nothing(
        self, monkeypatch
    ):
        """GM ``system_health`` provider probes must traverse the proxy;
        internal graph/qdrant/s3 probes are stubbed direct-local.

        P13-1C also makes this the zero-cost proof: the probes are
        discovery-only, so the recorded traffic is ``/models`` and NOTHING
        else — no chat completion, no embedding, no provider tokens spent by a
        health call (HM-12 / ADR-0027).
        """
        import mcp_memory.server as srv

        async with _extraction_emulator() as proxy:
            extractor = _fresh(monkeypatch, _extractor, proxy.url)
            embedder = _fresh(monkeypatch, _embedder, proxy.url)

            class _OkStub:
                async def test_connection(self):
                    return {"status": "ok"}

            monkeypatch.setattr(srv, "get_storage", lambda: _OkStub())
            monkeypatch.setattr(srv, "get_graph", lambda: _OkStub())
            monkeypatch.setattr(srv, "get_vector_store", lambda: _OkStub())
            monkeypatch.setattr(srv, "get_extractor", lambda: extractor)
            monkeypatch.setattr(srv, "get_embedder", lambda: embedder)
            result = await srv.system_health()

        assert result["services"]["llmaas"]["status"] == "ok"
        assert result["services"]["embedding"]["status"] == "ok"
        targets = [r["url"] for r in proxy.requests]
        assert targets, "no provider request reached the proxy"
        assert all(t.startswith(_LLM_ORIGIN) for t in targets)
        assert all(t.endswith("/models") for t in targets)
        assert all(r["method"] == "GET" for r in proxy.requests)

    @pytest.mark.parametrize(
        ("schema_ready", "admissions_available", "expected_status"),
        (
            (True, True, "ok"),
            (False, True, "error"),
            (True, False, "error"),
        ),
    )
    async def test_system_health_includes_process_admission_state(
        self,
        monkeypatch,
        schema_ready,
        admissions_available,
        expected_status,
    ):
        import mcp_memory.server as srv
        from mcp_memory.core import maintenance

        class _Ok:
            async def test_connection(self):
                return {"status": "ok"}

        class _Graph(_Ok):
            def document_schema_status(self):
                return {
                    "status": "ok" if schema_ready else "error",
                    "ready": schema_ready,
                }

        class _Coordinator:
            def health_status(self):
                return {
                    "status": "ok" if admissions_available else "error",
                    "admissions_available": admissions_available,
                }

        monkeypatch.setattr(srv, "get_storage", lambda: _Ok())
        monkeypatch.setattr(srv, "get_graph", lambda: _Graph())
        monkeypatch.setattr(srv, "get_vector_store", lambda: _Ok())
        monkeypatch.setattr(srv, "get_extractor", lambda: _Ok())
        monkeypatch.setattr(srv, "get_embedder", lambda: _Ok())
        monkeypatch.setattr(
            maintenance,
            "get_maintenance_coordinator",
            lambda: _Coordinator(),
        )

        result = await srv.system_health()

        assert result["status"] == expected_status
        assert result["services"]["document_schema"] == {
            "status": "ok" if schema_ready else "error",
            "ready": schema_ready,
        }
        assert result["services"]["maintenance"] == {
            "status": "ok" if admissions_available else "error",
            "admissions_available": admissions_available,
        }
        assert "memory" not in json.dumps(result)

    async def test_retry_attempt_stays_on_proxy(self, monkeypatch):
        """The single ADR-0027 retry must re-traverse the proxy, never fall
        back to a direct second attempt.

        Only an EXPLICITLY transient rate limit carrying a bounded
        ``Retry-After`` authorizes it, so that is what the emulator scripts;
        every other failure family is terminal (asserted below).
        """
        transient = {
            "status": 429,
            "headers": {"retry-after": "0"},
            "body": {
                "error": {
                    "message": "slow down",
                    "code": "rate_limit_exceeded",
                }
            },
        }
        async with _extraction_emulator(scripted=[transient]) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            result = await svc.extract_from_text("retry me")
        assert result.summary == "canned summary"
        assert len(proxy.requests) == 2
        assert all(r["url"].startswith(_LLM_ORIGIN) for r in proxy.requests)

    async def test_terminal_provider_error_is_not_retried(self, monkeypatch):
        """Companion to the retry proof: a 4xx that is NOT an explicitly
        transient rate limit issues exactly ONE paid request. The historical
        3-attempt ``tenacity`` layer would have issued three."""
        from hivemind_inference import InferenceError

        async with _extraction_emulator(scripted=[{"status": 400}]) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            with pytest.raises(InferenceError) as excinfo:
                await svc.extract_from_text("terminal")
        assert excinfo.value.category == "invalid_request"
        assert len(proxy.requests) == 1

    async def test_https_origin_sends_connect_through_proxy(self, monkeypatch):
        """HTTPS egress tunnels through the proxy with CONNECT: the tunnel
        request reaches the proxy with the https origin's authority, and the
        refused tunnel fails closed (the ``.invalid`` origin leaves no possible
        direct path). A proxy-hop failure is never retried (ADR-0027), so
        exactly one tunnel attempt is recorded."""
        async with _extraction_emulator() as proxy:
            svc = _fresh(
                monkeypatch,
                _extractor,
                proxy.url,
                api_url="https://llm.p12-3-hivemind.invalid/v1",
            )
            with pytest.raises(Exception):
                await svc.extract_from_text("https routing proof")
        assert len(proxy.requests) == 1
        request = proxy.requests[0]
        assert request["method"] == "CONNECT"
        assert request["url"] == "llm.p12-3-hivemind.invalid:443"

    async def test_no_proxy_stays_direct(self, monkeypatch):
        """Without PROXY_URL the direct behavior is preserved: the request
        reaches the origin itself, in path-form."""
        async with _extraction_emulator() as origin:
            svc = _fresh(
                monkeypatch, _extractor, None, api_url=origin.url + "/v1"
            )
            result = await svc.extract_from_text("direct")
        assert result.summary == "canned summary"
        assert len(origin.requests) == 1
        assert origin.requests[0]["url"] == "/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Direct-network trap — proxy failure must never fall back to direct          #
# --------------------------------------------------------------------------- #

class TestDirectFallbackTrap:
    async def test_proxy_connect_failure_never_reaches_direct_origin(
        self, monkeypatch
    ):
        """Dead proxy port + LIVE direct origin: the call must raise and the
        origin listener must see ZERO connections."""
        dead_port = await _closed_port()
        async with _extraction_emulator() as origin:
            svc = _fresh(
                monkeypatch,
                _extractor,
                f"http://127.0.0.1:{dead_port}",
                api_url=origin.url + "/v1",
            )
            with pytest.raises(Exception):
                await svc.extract_from_text("must fail closed")
            assert origin.connections == 0
            assert origin.requests == []

    async def test_proxy_auth_failure_never_reaches_direct_origin(
        self, monkeypatch
    ):
        """407 from the proxy: the failure stays on the proxy and the direct
        origin sees nothing."""
        async with _extraction_emulator(scripted=[{"status": 407}]) as proxy:
            async with _extraction_emulator() as origin:
                svc = _fresh(
                    monkeypatch,
                    _embedder,
                    proxy.url,
                    api_url=origin.url + "/v1",
                )
                with pytest.raises(Exception):
                    await svc.embed_texts(["x"])
                assert origin.connections == 0
                assert len(proxy.requests) == 1

    async def test_proxy_timeout_never_reaches_direct_origin(self, monkeypatch):
        """Stalling proxy + 1 s deadline: the call times out closed instead of
        retrying directly."""
        async with _extraction_emulator(scripted=[{"action": "stall"}]) as proxy:
            async with _extraction_emulator() as origin:
                svc = _fresh(
                    monkeypatch,
                    _extractor,
                    proxy.url,
                    api_url=origin.url + "/v1",
                    EXTRACTION_TIMEOUT_SECONDS="1",
                )
                with pytest.raises(Exception):
                    await svc.extract_from_text("stalling proxy")
                assert origin.connections == 0
                assert len(proxy.requests) == 1


# --------------------------------------------------------------------------- #
# Health redaction (runtime choke point)                                      #
# --------------------------------------------------------------------------- #

class TestHealthRedaction:
    async def test_ready_reflects_terminal_admission_but_health_stays_liveness(
        self, monkeypatch
    ):
        from mcp_memory.auth.middleware import StaticFilesMiddleware
        from mcp_memory.core import maintenance

        class _Graph:
            def document_schema_status(self):
                return {"status": "ok", "ready": True}

        class _PoisonedCoordinator:
            def health_status(self):
                return {"status": "error", "admissions_available": False}

        middleware = StaticFilesMiddleware(None)
        middleware._graph_service = _Graph()
        monkeypatch.setattr(
            maintenance,
            "get_maintenance_coordinator",
            lambda: _PoisonedCoordinator(),
        )

        async def invoke(path: str):
            sent: list[dict] = []

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "path": path, "method": "GET"},
                None,
                send,
            )
            status = next(
                item["status"]
                for item in sent
                if item["type"] == "http.response.start"
            )
            body = json.loads(
                next(
                    item["body"]
                    for item in sent
                    if item["type"] == "http.response.body"
                )
            )
            return status, body

        ready_status, ready_body = await invoke("/ready")
        health_status, health_body = await invoke("/health")

        assert ready_status == 503
        assert ready_body["status"] == "error"
        assert health_status == 200
        assert health_body["status"] == "healthy"

    async def test_system_health_redacts_proxy_secrets(self, monkeypatch):
        import mcp_memory.server as srv

        class _Boom:
            async def test_connection(self):
                # R5 : mot de passe avec '@' brut — le dernier '@' délimite.
                raise RuntimeError(
                    "Failed to connect to proxy URL: "
                    '"http://svc-user:s3cr3t@pw@proxy.internal:3128'
                    '?access_token=qs3cr3t#fr4g"'
                )

        class _Ok:
            async def test_connection(self):
                return {"status": "ok"}

        monkeypatch.setattr(srv, "get_storage", lambda: _Boom())
        monkeypatch.setattr(srv, "get_graph", lambda: _Ok())
        monkeypatch.setattr(srv, "get_vector_store", lambda: _Ok())
        monkeypatch.setattr(srv, "get_extractor", lambda: _Ok())
        monkeypatch.setattr(srv, "get_embedder", lambda: _Ok())
        result = await srv.system_health()
        assert result["status"] == "error"
        message = result["services"]["s3"]["message"]
        assert "s3cr3t" not in message
        assert "pw@" not in message
        assert "svc-user" not in message
        assert "qs3cr3t" not in message
        assert "fr4g" not in message
        assert "proxy.internal:3128" in message


    async def test_system_health_recovered_s3_client_error_is_redacted(
        self, monkeypatch
    ):
        """R7 (Codex round 7): ``storage.test_connection()`` RECOVERS
        ClientError into its returned health payload (never re-raised, so the
        method decorator cannot rewrite it) and ``system_health`` copies that
        payload verbatim — the real recovered path must be redacted."""
        from botocore.exceptions import ClientError

        import mcp_memory.server as srv

        def _storage():
            from mcp_memory.core.storage import StorageService

            return StorageService()

        storage = _fresh(monkeypatch, _storage, _SECRET_PROXY)
        secret_msg = (
            "denied via http://svc-user:s3cr3t@pw@proxy.internal:3128/x"
            "?access_token=qs3cr3t#fr4g"
        )

        def _boom(**_kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": secret_msg}},
                "PutObject",
            )

        monkeypatch.setattr(storage._client_v2, "put_object", _boom)

        class _Ok:
            async def test_connection(self):
                return {"status": "ok"}

        monkeypatch.setattr(srv, "get_storage", lambda: storage)
        monkeypatch.setattr(srv, "get_graph", lambda: _Ok())
        monkeypatch.setattr(srv, "get_vector_store", lambda: _Ok())
        monkeypatch.setattr(srv, "get_extractor", lambda: _Ok())
        monkeypatch.setattr(srv, "get_embedder", lambda: _Ok())
        result = await srv.system_health()
        assert result["status"] == "error"
        message = result["services"]["s3"]["message"]
        assert "AccessDenied" in message
        assert "s3cr3t" not in message
        assert "pw@" not in message
        assert "svc-user" not in message
        assert "qs3cr3t" not in message
        assert "fr4g" not in message


# --------------------------------------------------------------------------- #
# Inference error boundary (R8)                                                #
# --------------------------------------------------------------------------- #

_R8_SECRET_MSG = (
    'proxy refused: "http://svc-user:s3cr3t@pw@proxy.internal:3128'
    '?access_token=qs3cr3t#fr4g"'
)


def _assert_r8_clean(surface):
    assert "s3cr3t" not in surface
    assert "pw@" not in surface
    assert "svc-user" not in surface
    assert "qs3cr3t" not in surface
    assert "fr4g" not in surface


class TestInferenceErrorRedaction:
    """R8 (Codex round 8): ordinary extraction/embedding failures log the
    provider error and re-raise it into the ingestion/server error handlers —
    both the stderr log and the escaping exception text must be free of
    secrets.

    P13-1C strengthens this from "sanitized on the way out" to "never
    constructed": every failure now escapes as an ``InferenceError``, whose
    message is assembled exclusively from six registry-bound safe fields, so a
    provider message cannot be in it at all. Each test therefore injects a
    secret-bearing provider body and asserts the escaping text carries neither
    the secret NOR the endpoint.
    """

    async def test_embed_texts_error_boundary_redacts_log_and_raise(
        self, monkeypatch, capsys
    ):
        from hivemind_inference import InferenceError

        secret_body = {"error": {"message": _R8_SECRET_MSG, "type": "server_error"}}
        async with _extraction_emulator(
            scripted=[{"status": 500, "body": secret_body}]
        ) as proxy:
            svc = _fresh(monkeypatch, _embedder, proxy.url)
            with pytest.raises(InferenceError) as excinfo:
                await svc.embed_texts(["x"])
        _assert_r8_clean(capsys.readouterr().err)
        _assert_r8_clean(str(excinfo.value))
        assert "proxy.internal" not in str(excinfo.value)
        assert excinfo.value.category == "unavailable"

    async def test_extract_error_boundary_redacts_log_and_raise(
        self, monkeypatch, capsys
    ):
        from hivemind_inference import InferenceError

        secret_body = {"error": {"message": _R8_SECRET_MSG, "type": "server_error"}}
        async with _extraction_emulator(
            scripted=[{"status": 500, "body": secret_body}]
        ) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            with pytest.raises(InferenceError) as excinfo:
                await svc.extract_from_text("boom")
        _assert_r8_clean(capsys.readouterr().err)
        _assert_r8_clean(str(excinfo.value))
        assert "proxy.internal" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Owned-transport lifecycle                                                    #
# --------------------------------------------------------------------------- #

class TestOwnedTransportLifecycle:
    """P13-1C: the owned transports moved from the two services to the ONE
    service-wide runtime. The per-adapter construction and idempotent-close
    contracts are proven where the transports are built
    (``tests/test_p13_inference_adapters.py``); what belongs here is that the
    Graph Memory runtime is the single owner and that the service shutdown path
    reaches it.
    """

    async def test_services_own_no_transport(self, monkeypatch):
        """The per-service owned client is GONE: a proxied service holds no
        transport attribute at all, so nothing but the runtime can close one."""
        extractor = _fresh(monkeypatch, _extractor, _SECRET_PROXY)
        embedder = _fresh(monkeypatch, _embedder, _SECRET_PROXY)
        for service in (extractor, embedder):
            assert not hasattr(service, "_owned_http_client")
            assert not hasattr(service, "_client")
            # close() stays callable for historical shutdown ordering and is a
            # no-op, so a stray call can never release the shared transport.
            await service.close()
            await service.close()

    async def test_runtime_builds_each_adapter_once_and_closes_them(
        self, monkeypatch
    ):
        """One transport per role, reused across calls, released by aclose()."""
        _fresh(monkeypatch, _extractor, _SECRET_PROXY)
        runtime = _runtime()
        chat_a = runtime.chat_provider()
        chat_b = runtime.chat_provider()
        embedding = runtime.embedding_provider()
        assert chat_a is chat_b
        assert chat_a is not embedding
        transports = [
            chat_a._owned_http_client,
            embedding._owned_http_client,
        ]
        assert all(not transport.is_closed for transport in transports)
        await runtime.aclose()
        assert all(transport.is_closed for transport in transports)
        await runtime.aclose()  # idempotent

    async def test_cancellation_keeps_shared_transport_usable(self, monkeypatch):
        """Cancelling one in-flight call must not tear down the runtime-owned
        transport: the next call still works, then aclose() releases it."""
        async with _extraction_emulator(scripted=[{"action": "stall"}]) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            runtime = _runtime()
            task = asyncio.create_task(svc.extract_from_text("hang"))
            # Let the request reach the stalling proxy deterministically.
            for _ in range(200):
                if proxy.requests:
                    break
                await asyncio.sleep(0.01)
            assert proxy.requests, "in-flight request never reached proxy"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            transport = runtime.chat_provider()._owned_http_client
            assert not transport.is_closed
            result = await svc.extract_from_text("after cancel")
            assert result.summary == "canned summary"
            await runtime.aclose()
            assert transport.is_closed

    async def test_asgi_lifespan_shutdown_closes_runtime_transports(
        self, monkeypatch
    ):
        """The shared guard must close the inference runtime (and reset both
        service singletons) at process shutdown.

        The adapters are built AFTER the lifespan has started, which is both
        the realistic sequence — a transport is opened lazily by request work
        inside a serving window — and the only admissible one: the startup hook
        refuses to open a window over a previous one's unreleased transports
        (``InferenceRuntimeHolder.validate_startup``), and that refusal path
        does not run cleanup. Building them first would therefore assert
        nothing about closing.
        """
        import mcp_memory.core.embedder as embedder_mod
        import mcp_memory.core.extractor as extractor_mod
        import mcp_memory.server as srv

        extractor = _fresh(monkeypatch, _extractor, _SECRET_PROXY)
        embedder = _fresh(monkeypatch, _embedder, _SECRET_PROXY)
        monkeypatch.setattr(srv, "_extractor_service", extractor)
        monkeypatch.setattr(srv, "_embedding_service", embedder)
        monkeypatch.setattr(extractor_mod, "_extractor_service", extractor)
        monkeypatch.setattr(embedder_mod, "_embedding_service", embedder)

        received = []

        async def _inner_app(scope, receive, send):
            assert scope["type"] == "lifespan"
            while True:
                message = await receive()
                received.append(message["type"])
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        import uvicorn
        from uvicorn.lifespan.on import LifespanOn

        # The scenario isolates provider-transport shutdown.  Neo4j schema
        # initialization has its own fail-closed P13 tests and requires the
        # Graph image's service-only dependencies, which the public top-level
        # test environment deliberately does not install.
        monkeypatch.setattr(
            srv,
            "_initialize_graph_document_schema",
            lambda: None,
        )
        monkeypatch.setattr(srv.mcp, "streamable_http_app", lambda: _inner_app)
        app = srv._create_app()
        state = LifespanOn(
            uvicorn.Config(app, lifespan="auto", log_config=None)
        )
        await asyncio.wait_for(state.startup(), timeout=2.0)
        assert not state.startup_failed

        # Serving window is open: request work now builds the owned transports.
        runtime = _runtime()
        transports = [
            runtime.chat_provider()._owned_http_client,
            runtime.embedding_provider()._owned_http_client,
        ]
        assert all(not transport.is_closed for transport in transports)

        await asyncio.wait_for(state.shutdown(), timeout=2.0)

        assert received == ["lifespan.startup", "lifespan.shutdown"]
        assert not state.shutdown_failed
        # `shutdown_failed` alone does not prove a terminal message reached the
        # wire: an application that raises out of the scope without sending one
        # leaves it False and sets `error_occurred` instead.
        assert not state.error_occurred
        assert all(transport.is_closed for transport in transports)
        assert srv._extractor_service is None
        assert srv._embedding_service is None

    def test_main_wires_shared_lifespan_guard_outermost(self, monkeypatch):
        """The factory returns the shared guard as the outermost object."""
        from hivemind_inference.asgi_lifespan import LifespanGuard
        import mcp_memory.server as srv

        async def inner(scope, receive, send):
            return None

        monkeypatch.setattr(srv.mcp, "streamable_http_app", lambda: inner)
        app = srv._create_app()
        assert isinstance(app, LifespanGuard)
        names = []
        current = app.app
        while current is not None:
            names.append(type(current).__name__)
            current = getattr(current, "app", None)
        assert names[:3] == [
            "AuthMiddleware",
            "LoggingMiddleware",
            "StaticFilesMiddleware",
        ]
