# -*- coding: utf-8 -*-
"""
P12-3 (#268) — Graph Memory `PROXY_URL`: runtime routing, fail-closed trap,
client lifecycle.

Runtime evidence complementing ``tests/test_p12_3_gm_proxy_config.py``:

- a deterministic in-process fake HTTP proxy (absolute-form request lines,
  canned OpenAI-shaped responses, per-request recording) observes every
  expected Graph Memory extraction, embedding, and provider-health request;
- the LLM origin hostnames use the reserved ``.invalid`` TLD (RFC 2606), so a
  bypassing direct connection cannot succeed even accidentally — any recorded
  proxy request is positive proof of routing, and any successful call proves
  no direct path was used;
- a direct-network trap (live local origin listener + failing proxy) proves
  that proxy connection, authentication (407), and timeout failures raise and
  NEVER fall back to a direct connection, across the openai and tenacity retry
  layers;
- retry attempts re-traverse the proxy (never a direct second attempt);
- without a proxy the vendored direct behavior is preserved;
- owned transports exist only when a proxy is configured and close on normal
  shutdown, double-close, construction failure, and the ASGI lifespan path,
  while a cancelled in-flight call must NOT tear down the shared service
  transport.

No real network: every listener binds 127.0.0.1 on an ephemeral port; every
must-not-resolve origin uses ``.invalid``. tenacity waits are neutralized via
each decorated method's ``retry.wait`` so retries stay deterministic and fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
import tenacity

_REPO_ROOT = Path(__file__).resolve().parents[1]

_LLM_ORIGIN = "http://llm.p12-3-hivemind.invalid"
_SECRET_PROXY = "http://svc-user:s3cr3t-pw@proxy.internal:3128"


# --------------------------------------------------------------------------- #
# Env helper (same contract as tests/test_p7_9_vendored_storage_signature.py) #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _gm_baseline_env(monkeypatch):
    """Order-independence: importing any ``mcp_memory`` module executes the
    module-level ``Settings()`` (required credential fields), so a baseline
    env must exist BEFORE each test body's imports, standalone or full-suite."""
    _set_gm_env(monkeypatch, None)
    yield


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
    """Build a GM service with fresh settings pinned to this test's env."""
    _set_gm_env(monkeypatch, proxy_url, api_url=api_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from mcp_memory.config import get_settings

    get_settings.cache_clear()
    try:
        return factory()
    finally:
        get_settings.cache_clear()


def _quiet_retries(monkeypatch, *decorated_methods):
    """Neutralize tenacity waits on decorated GM methods (deterministic)."""
    for method in decorated_methods:
        monkeypatch.setattr(method.retry, "wait", tenacity.wait_none())


# --------------------------------------------------------------------------- #
# Deterministic fake HTTP proxy / origin listeners                             #
# --------------------------------------------------------------------------- #

_EXTRACTION_JSON = {
    "entities": [
        {"name": "Hivemind", "type": "Product", "description": "memory service"}
    ],
    "relations": [],
    "summary": "canned summary",
    "key_topics": ["memory"],
}


def _chat_payload():
    return {
        "id": "chatcmpl-p12-3",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(_EXTRACTION_JSON),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _embeddings_payload(count):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3, 0.4]}
            for i in range(count)
        ],
        "model": "test-embed-model",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def _models_payload():
    return {"object": "list", "data": [{"id": "test-model", "object": "model"}]}


class _HttpEndpoint:
    """Minimal deterministic HTTP/1.1 listener (Connection: close per request).

    As a *proxy* it receives absolute-form request targets and answers itself
    (no upstream connection is ever attempted — fully deterministic). As a
    *direct origin* it receives path-form targets. ``scripted`` entries
    override the default responder per request, in order:

    - ``"407"``: proxy-auth failure status with a JSON body;
    - ``"500"`` / ``"400"``: provider-style error status;
    - ``"stall"``: read the request then never answer (timeout path);
    - ``None``: canned success for the requested path.
    """

    def __init__(self, scripted=None):
        self.requests = []
        self.connections = 0
        self.scripted = list(scripted or [])
        self._server = None
        self.port = None
        self._stall = asyncio.Event()

    async def __aenter__(self):
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._stall.set()
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def _canned(self, target, body):
        if "/chat/completions" in target:
            return b"200 OK", _chat_payload()
        if "/embeddings" in target:
            try:
                parsed = json.loads(body or b"{}")
                raw_input = parsed.get("input", [])
                count = len(raw_input) if isinstance(raw_input, list) else 1
            except ValueError:
                count = 1
            return b"200 OK", _embeddings_payload(max(count, 1))
        if "/models" in target:
            return b"200 OK", _models_payload()
        return b"404 Not Found", {"error": "unknown path"}

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split(" ")
            method, target = parts[0], parts[1]
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            body = b""
            length = int(headers.get("content-length", "0") or "0")
            if length:
                body = await reader.readexactly(length)
            self.requests.append(
                {"method": method, "url": target, "body": body}
            )
            if method == "CONNECT":
                # Routing proof for https origins: record the tunnel request
                # and refuse it (no TLS stack in the fake) — fail-closed.
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            action = self.scripted.pop(0) if self.scripted else None
            if action == "stall":
                await self._stall.wait()
                return
            if action in ("407", "400", "500"):
                reasons = {
                    "407": b"407 Proxy Authentication Required",
                    "400": b"400 Bad Request",
                    "500": b"500 Internal Server Error",
                }
                status, payload = reasons[action], {"error": {"message": "no"}}
            else:
                status, payload = self._canned(target, body)
            data = json.dumps(payload).encode()
            writer.write(
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(data)).encode() + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + data
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


def _extractor():
    from mcp_memory.core.extractor import ExtractorService

    return ExtractorService()


def _embedder():
    from mcp_memory.core.embedder import EmbeddingService

    return EmbeddingService()


# --------------------------------------------------------------------------- #
# Routing through the fake proxy (extraction / embeddings / provider-health)  #
# --------------------------------------------------------------------------- #

class TestProxiedRouting:
    async def test_extraction_goes_through_proxy(self, monkeypatch):
        async with _HttpEndpoint() as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            try:
                result = await svc.extract_from_text("Hivemind stores memory.")
            finally:
                await svc.close()
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
        async with _HttpEndpoint() as proxy:
            svc = _fresh(monkeypatch, _embedder, proxy.url)
            try:
                vectors = await svc.embed_texts(["a", "b", "c"])
                query_vec = await svc.embed_query("question")
            finally:
                await svc.close()
        assert len(vectors) == 3
        assert len(query_vec) == 4
        assert len(proxy.requests) == 2
        for request in proxy.requests:
            assert request["url"].startswith(_LLM_ORIGIN)
            assert request["url"].endswith("/embeddings")

    async def test_provider_health_probes_go_through_proxy(self, monkeypatch):
        """GM ``system_health`` LLM and embedding probes must traverse the
        proxy; internal graph/qdrant/s3 probes are stubbed direct-local."""
        import mcp_memory.server as srv

        async with _HttpEndpoint() as proxy:
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
            try:
                result = await srv.system_health()
            finally:
                await extractor.close()
                await embedder.close()

        assert result["services"]["llmaas"]["status"] == "ok"
        assert result["services"]["embedding"]["status"] == "ok"
        targets = [r["url"] for r in proxy.requests]
        assert any(t.endswith("/chat/completions") for t in targets)
        assert any(t.endswith("/embeddings") for t in targets)
        assert all(t.startswith(_LLM_ORIGIN) for t in targets)

    async def test_retry_attempts_stay_on_proxy(self, monkeypatch):
        """A failed attempt must be retried through the proxy again, never
        through a direct fallback: both attempts are recorded by the proxy."""
        from mcp_memory.core.extractor import ExtractorService

        _quiet_retries(monkeypatch, ExtractorService.extract_from_text)
        async with _HttpEndpoint(scripted=["400"]) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            try:
                result = await svc.extract_from_text("retry me")
            finally:
                await svc.close()
        assert result.summary == "canned summary"
        assert len(proxy.requests) == 2
        assert all(r["url"].startswith(_LLM_ORIGIN) for r in proxy.requests)

    async def test_https_origin_sends_connect_through_proxy(self, monkeypatch):
        """HTTPS egress tunnels through the proxy with CONNECT: every retry's
        tunnel request reaches the proxy with the https origin's authority,
        and the refused tunnel fails closed (the ``.invalid`` origin leaves no
        possible direct path)."""
        from mcp_memory.core.extractor import ExtractorService

        _quiet_retries(monkeypatch, ExtractorService.extract_from_text)
        async with _HttpEndpoint() as proxy:
            svc = _fresh(
                monkeypatch,
                _extractor,
                proxy.url,
                api_url="https://llm.p12-3-hivemind.invalid/v1",
            )
            svc._client.max_retries = 0
            try:
                with pytest.raises(Exception):
                    await svc.extract_from_text("https routing proof")
            finally:
                await svc.close()
        assert len(proxy.requests) == 3
        for request in proxy.requests:
            assert request["method"] == "CONNECT"
            assert request["url"] == "llm.p12-3-hivemind.invalid:443"

    async def test_no_proxy_stays_direct(self, monkeypatch):
        """Without PROXY_URL the vendored direct behavior is preserved: the
        request reaches the origin itself, in path-form, with no owned
        transport."""
        async with _HttpEndpoint() as origin:
            svc = _fresh(
                monkeypatch, _extractor, None, api_url=origin.url + "/v1"
            )
            try:
                assert svc._owned_http_client is None
                result = await svc.extract_from_text("direct")
            finally:
                await svc.close()
        assert result.summary == "canned summary"
        assert len(origin.requests) == 1
        assert origin.requests[0]["url"] == "/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Direct-network trap — proxy failure must never fall back to direct          #
# --------------------------------------------------------------------------- #

async def _closed_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class TestDirectFallbackTrap:
    async def test_proxy_connect_failure_never_reaches_direct_origin(
        self, monkeypatch
    ):
        """Dead proxy port + LIVE direct origin: the call must raise and the
        origin listener must see ZERO connections across every retry layer."""
        from mcp_memory.core.extractor import ExtractorService

        _quiet_retries(monkeypatch, ExtractorService.extract_from_text)
        dead_port = await _closed_port()
        async with _HttpEndpoint() as origin:
            svc = _fresh(
                monkeypatch,
                _extractor,
                f"http://127.0.0.1:{dead_port}",
                api_url=origin.url + "/v1",
            )
            svc._client.max_retries = 0
            try:
                with pytest.raises(Exception):
                    await svc.extract_from_text("must fail closed")
            finally:
                await svc.close()
            assert origin.connections == 0
            assert origin.requests == []

    async def test_proxy_auth_failure_never_reaches_direct_origin(
        self, monkeypatch
    ):
        """407 from the proxy: every retry stays on the proxy; the direct
        origin sees nothing."""
        from mcp_memory.core.embedder import EmbeddingService

        _quiet_retries(monkeypatch, EmbeddingService.embed_texts)
        async with _HttpEndpoint(scripted=["407", "407", "407"]) as proxy:
            async with _HttpEndpoint() as origin:
                svc = _fresh(
                    monkeypatch,
                    _embedder,
                    proxy.url,
                    api_url=origin.url + "/v1",
                )
                svc._client.max_retries = 0
                try:
                    with pytest.raises(Exception):
                        await svc.embed_texts(["x"])
                finally:
                    await svc.close()
                assert origin.connections == 0
                assert len(proxy.requests) == 3

    async def test_proxy_timeout_never_reaches_direct_origin(self, monkeypatch):
        """Stalling proxy + 1 s client timeout: the call times out closed
        instead of retrying directly."""
        from mcp_memory.core.extractor import ExtractorService

        _quiet_retries(monkeypatch, ExtractorService.extract_from_text)
        async with _HttpEndpoint(
            scripted=["stall", "stall", "stall"]
        ) as proxy:
            async with _HttpEndpoint() as origin:
                svc = _fresh(
                    monkeypatch,
                    _extractor,
                    proxy.url,
                    api_url=origin.url + "/v1",
                    EXTRACTION_TIMEOUT_SECONDS="1",
                )
                svc._client.max_retries = 0
                try:
                    with pytest.raises(Exception):
                        await svc.extract_from_text("stalling proxy")
                finally:
                    await svc.close()
                assert origin.connections == 0
                assert len(proxy.requests) == 3


# --------------------------------------------------------------------------- #
# Health redaction (runtime choke point)                                      #
# --------------------------------------------------------------------------- #

class TestHealthRedaction:
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
    APIError and re-raise it into the ingestion/server error handlers — both
    the stderr log and the escaping exception text must be redacted at the
    service boundary (the shared decorator makes every downstream ``str(e)``
    consumer — sync results, async job status — inherit the sanitized text)."""

    async def test_embed_texts_error_boundary_redacts_log_and_raise(
        self, monkeypatch, capsys
    ):
        import httpx
        from openai import APIError

        from mcp_memory.core.embedder import EmbeddingService

        _quiet_retries(monkeypatch, EmbeddingService.embed_texts)
        svc = _fresh(monkeypatch, _embedder, _SECRET_PROXY)
        request = httpx.Request("POST", _LLM_ORIGIN + "/v1/embeddings")

        class _Boom:
            async def create(self, **_kw):
                raise APIError(_R8_SECRET_MSG, request, body=None)

        monkeypatch.setattr(svc._client, "embeddings", _Boom())
        try:
            with pytest.raises(APIError) as excinfo:
                await svc.embed_texts(["x"])
        finally:
            await svc.close()
        _assert_r8_clean(capsys.readouterr().err)
        _assert_r8_clean(str(excinfo.value))
        assert "proxy.internal:3128" in str(excinfo.value)

    async def test_extract_error_boundary_redacts_log_and_raise(
        self, monkeypatch, capsys
    ):
        import httpx
        from openai import APIError

        from mcp_memory.core.extractor import ExtractorService

        _quiet_retries(monkeypatch, ExtractorService.extract_from_text)
        svc = _fresh(monkeypatch, _extractor, _SECRET_PROXY)
        request = httpx.Request("POST", _LLM_ORIGIN + "/v1/chat/completions")

        class _BoomCompletions:
            async def create(self, **_kw):
                raise APIError(_R8_SECRET_MSG, request, body=None)

        class _BoomChat:
            completions = _BoomCompletions()

        monkeypatch.setattr(svc._client, "chat", _BoomChat())
        try:
            with pytest.raises(APIError) as excinfo:
                await svc.extract_from_text("boom")
        finally:
            await svc.close()
        _assert_r8_clean(capsys.readouterr().err)
        _assert_r8_clean(str(excinfo.value))


# --------------------------------------------------------------------------- #
# Owned-transport lifecycle                                                    #
# --------------------------------------------------------------------------- #

class TestOwnedTransportLifecycle:
    async def test_owned_client_only_when_proxy_configured(self, monkeypatch):
        direct = _fresh(monkeypatch, _extractor, None)
        assert direct._owned_http_client is None
        await direct.close()

        proxied = _fresh(monkeypatch, _extractor, "http://127.0.0.1:9")
        assert proxied._owned_http_client is not None
        assert not proxied._owned_http_client.is_closed
        await proxied.close()

    async def test_close_closes_owned_client_and_is_idempotent(self, monkeypatch):
        svc = _fresh(monkeypatch, _embedder, "http://127.0.0.1:9")
        owned = svc._owned_http_client
        await svc.close()
        assert owned.is_closed
        await svc.close()  # double-close must be safe
        assert owned.is_closed

    async def test_construction_failure_closes_owned_client(self, monkeypatch):
        """If the AsyncOpenAI constructor fails after the owned transport was
        created, the transport must not leak."""
        import mcp_memory.core.egress as egress
        import mcp_memory.core.extractor as extractor_mod

        created = []
        real_builder = egress.build_owned_async_http_client

        def _recording_builder(proxy_url, timeout):
            client = real_builder(proxy_url, timeout)
            created.append(client)
            return client

        monkeypatch.setattr(
            extractor_mod, "build_owned_async_http_client", _recording_builder
        )

        def _boom(**_kwargs):
            raise RuntimeError("constructor exploded")

        monkeypatch.setattr(extractor_mod, "AsyncOpenAI", _boom)
        with pytest.raises(RuntimeError, match="constructor exploded"):
            _fresh(monkeypatch, _extractor, "http://127.0.0.1:9")
        assert len(created) == 1
        # Dans un contexte async, la fermeture du constructeur est planifiée
        # sur la boucle courante — lui laisser des ticks déterministes.
        for _ in range(100):
            if created[0].is_closed:
                break
            await asyncio.sleep(0.01)
        assert created[0].is_closed

    async def test_construction_failure_closes_owned_client_embedder(
        self, monkeypatch
    ):
        """Same constructor-failure guard on the embedding service."""
        import mcp_memory.core.egress as egress
        import mcp_memory.core.embedder as embedder_mod

        created = []
        real_builder = egress.build_owned_async_http_client

        def _recording_builder(proxy_url, timeout):
            client = real_builder(proxy_url, timeout)
            created.append(client)
            return client

        monkeypatch.setattr(
            embedder_mod, "build_owned_async_http_client", _recording_builder
        )

        def _boom(**_kwargs):
            raise RuntimeError("constructor exploded")

        monkeypatch.setattr(embedder_mod, "AsyncOpenAI", _boom)
        with pytest.raises(RuntimeError, match="constructor exploded"):
            _fresh(monkeypatch, _embedder, "http://127.0.0.1:9")
        assert len(created) == 1
        for _ in range(100):
            if created[0].is_closed:
                break
            await asyncio.sleep(0.01)
        assert created[0].is_closed

    async def test_cancellation_keeps_shared_transport_usable(self, monkeypatch):
        """Cancelling one in-flight call must not tear down the service-owned
        transport: the next call still works, then close() releases it."""
        async with _HttpEndpoint(scripted=["stall"]) as proxy:
            svc = _fresh(monkeypatch, _extractor, proxy.url)
            try:
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
                assert not svc._owned_http_client.is_closed
                result = await svc.extract_from_text("after cancel")
                assert result.summary == "canned summary"
            finally:
                await svc.close()
            assert svc._owned_http_client.is_closed

    async def test_asgi_lifespan_shutdown_closes_singletons(self, monkeypatch):
        """The server's lifespan shim must close and reset both inference
        singletons on lifespan.shutdown (service-shutdown path)."""
        import mcp_memory.core.embedder as embedder_mod
        import mcp_memory.core.extractor as extractor_mod
        import mcp_memory.server as srv

        extractor = _fresh(monkeypatch, _extractor, "http://127.0.0.1:9")
        embedder = _fresh(monkeypatch, _embedder, "http://127.0.0.1:9")
        monkeypatch.setattr(srv, "_extractor_service", extractor)
        monkeypatch.setattr(srv, "_embedding_service", embedder)
        monkeypatch.setattr(extractor_mod, "_extractor_service", extractor)
        monkeypatch.setattr(embedder_mod, "_embedding_service", embedder)
        extractor_owned = extractor._owned_http_client
        embedder_owned = embedder._owned_http_client

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

        app = srv.EgressLifecycleMiddleware(_inner_app)
        incoming = [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
        sent = []

        async def _receive():
            return incoming.pop(0)

        async def _send(message):
            sent.append(message)

        await app({"type": "lifespan"}, _receive, _send)

        assert received == ["lifespan.startup", "lifespan.shutdown"]
        assert sent[-1] == {"type": "lifespan.shutdown.complete"}
        assert extractor_owned.is_closed
        assert embedder_owned.is_closed
        assert srv._extractor_service is None
        assert srv._embedding_service is None

    def test_main_wires_lifecycle_middleware_outermost(self):
        """Structural guard: ``main()`` must wrap the composed ASGI stack with
        the egress lifecycle middleware so a real uvicorn shutdown reaches the
        close path."""
        import ast as ast_mod

        source = (
            _REPO_ROOT
            / "services"
            / "graph-memory"
            / "src"
            / "mcp_memory"
            / "server.py"
        ).read_text(encoding="utf-8")
        tree = ast_mod.parse(source)
        main_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast_mod.FunctionDef) and node.name == "main"
        )
        wrapped = sorted(
            (node.lineno, node.func.id)
            for node in ast_mod.walk(main_fn)
            if isinstance(node, ast_mod.Call)
            and isinstance(node.func, ast_mod.Name)
        )
        names = [name for _, name in wrapped]
        assert "EgressLifecycleMiddleware" in names
        # Outermost = applied after (i.e. wrapping) the auth middleware.
        assert names.index("EgressLifecycleMiddleware") > names.index(
            "AuthMiddleware"
        )
