"""MCP wire-byte budgets, before SDK parsing and without response buffering."""
from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest
from starlette.requests import Request

from live_mem.middleware import MCPRequestLimitMiddleware

ROOT = Path(__file__).resolve().parents[1]


def _scope(path="/mcp", headers=(), method="POST"):
    return {
        "type": "http", "asgi": {"version": "3.0"}, "method": method,
        "path": path, "headers": list(headers), "query_string": b"",
    }


async def _run(app, chunks, *, scope=None):
    messages, reads = [], []
    chunks = iter(chunks)

    async def receive():
        reads.append(True)
        return next(chunks)

    async def send(message):
        messages.append(message)

    await app(scope or _scope(), receive, send)
    return messages, len(reads)


def _chunk(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


async def _reader(scope, receive, send):
    # Same buffering boundary used by the locked MCP SDK.
    body = await Request(scope, receive).body()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.mark.parametrize("length", [
    b"33", b"00033", pytest.param(b"9" * 5000, id="5000-digit-content-length"),
])
async def test_oversized_content_length_rejected_without_read_or_sdk(length):
    async def forbidden(*args):
        pytest.fail("oversized body reached SDK")

    app = MCPRequestLimitMiddleware(forbidden, max_bytes=32)
    messages, reads = await _run(app, [], scope=_scope(headers=[(b"content-length", length)]))
    assert reads == 0
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["max_bytes"] == 32


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"-1")], [(b"content-length", b"1.0")],
    [(b"content-length", b"+1")], [(b"content-length", b"")],
    [(b"content-length", b"1, 1")],
    [(b"content-length", b"1"), (b"content-length", b"1")],
])
async def test_invalid_content_length_fails_before_read(headers):
    app = MCPRequestLimitMiddleware(_reader, max_bytes=32)
    messages, reads = await _run(app, [], scope=_scope(headers=headers))
    assert messages[0]["status"] == 400
    assert reads == 0


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")], [(b"content-length", b"32")]])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/variant"])
async def test_actual_chunk_count_stops_before_overflow_reaches_sdk(headers, path):
    async def forbidden(*args):
        pytest.fail("oversized body reached SDK")

    app = MCPRequestLimitMiddleware(forbidden, max_bytes=32)
    messages, reads = await _run(
        app, [_chunk(b"x" * 16, True), _chunk(b"x" * 16, True), _chunk(b"x", True)],
        scope=_scope(path, headers),
    )
    assert messages[0]["status"] == 413
    assert reads == 3  # No draining/reading the remaining attacker stream.


async def test_single_oversized_chunk_is_rejected_before_copying_or_sdk():
    app = MCPRequestLimitMiddleware(_reader, max_bytes=32)
    messages, reads = await _run(app, [_chunk(b"x" * 1024, True)])
    assert messages[0]["status"] == 413
    assert reads == 1


async def test_oversized_transport_chunk_is_not_copied_into_buffer():
    # Allocate a modest transport-owned chunk before measurement. The guard
    # must reject it without adding another allocation proportional to it.
    incoming = _chunk(b"x" * (4 * 1024 * 1024))
    app = MCPRequestLimitMiddleware(_reader, max_bytes=1024)
    tracemalloc.start()
    try:
        messages, _ = await _run(app, [incoming])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert messages[0]["status"] == 413
    assert peak < 32 * 1024


@pytest.mark.parametrize("size", [0, 31, 32])
async def test_accepted_body_is_replayed_byte_for_byte(size):
    app = MCPRequestLimitMiddleware(_reader, max_bytes=32)
    body = bytes(range(size))
    messages, reads = await _run(
        app, [_chunk(body[:12], True), _chunk(b"", True), _chunk(body[12:])],
        scope=_scope(headers=[(b"content-length", str(size).encode())]),
    )
    assert reads == 3
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == body


async def test_disconnect_before_complete_body_never_dispatches():
    async def forbidden(*args):
        pytest.fail("incomplete request reached SDK")

    app = MCPRequestLimitMiddleware(forbidden, max_bytes=32)
    messages, reads = await _run(app, [_chunk(b"x", True), {"type": "http.disconnect"}])
    assert messages == []
    assert reads == 2


@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_sse_messages_are_forwarded_immediately_and_disconnect_preserved(method):
    forwarded = []
    response = [
        {"type": "http.response.start", "status": 200,
         "headers": [(b"content-type", b"text/event-stream")]},
        {"type": "http.response.body", "body": b"data: " + b"x" * 128, "more_body": True},
        {"type": "http.response.body", "body": b"\n\n", "more_body": False},
    ]

    async def inner(scope, receive, send):
        assert await Request(scope, receive).body() == b"{}"
        for message in response:
            await send(message)
            assert forwarded[-1] is message
        assert await receive() == {"type": "http.disconnect"}

    chunks = iter([_chunk(b"{}"), {"type": "http.disconnect"}])

    async def receive():
        return next(chunks)

    async def send(message):
        forwarded.append(message)

    await MCPRequestLimitMiddleware(inner, max_bytes=32)(_scope(method=method), receive, send)
    assert forwarded == response


@pytest.mark.parametrize("scope", [{"type": "lifespan"}, _scope("/api/tool"), _scope("/mesh/v1/sync")])
async def test_other_namespaces_and_lifespan_receive_unchanged(scope):
    async def receive():
        pytest.fail("middleware read outside MCP")

    async def inner(actual_scope, actual_receive, send):
        assert actual_scope is scope
        assert actual_receive is receive

    await MCPRequestLimitMiddleware(inner, max_bytes=32)(scope, receive, None)


async def test_tiny_chunks_retain_only_bounded_buffer_not_chunk_metadata():
    limit = 64 * 1024
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        assert calls <= limit + 1
        return _chunk(b"x", True)

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 413

    async def forbidden(*args):
        pytest.fail("oversized stream reached SDK")

    app = MCPRequestLimitMiddleware(forbidden, max_bytes=limit)
    tracemalloc.start()
    try:
        await app(_scope(), receive, send)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert calls == limit + 1
    # A chunk list would retain >=512KiB of pointers alone. This is a safe
    # local fixture, not a total-RSS claim for SDK parsing or concurrency.
    assert peak < 2 * limit + 32 * 1024


def test_real_sdk_accepts_exact_limit_initialize_and_streams_sse():
    from mcp.server.mcpserver import MCPServer
    from starlette.testclient import TestClient

    limit = 64 * 1024
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "fixture", "version": "1"},
    }}
    body = json.dumps(payload).encode()
    # JSON trailing whitespace is legal and exercises the complete wire cap.
    body += b" " * (limit - len(body))
    mcp = MCPServer("budget-fixture")
    app = MCPRequestLimitMiddleware(mcp.streamable_http_app(stateless_http=True), max_bytes=limit)
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://localhost:8002") as client:
        response = client.post("/mcp", content=body, headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"serverInfo"' in response.text
        assert '"id":1' in response.text
        response = client.post("/mcp", content=body + b" ", headers=headers)
        assert response.status_code == 413  # SDK cannot translate this to500.
        response = client.post("/mcp", content=iter([body, b" "]), headers=headers)
        assert response.status_code == 413


async def test_factory_places_configured_budget_after_auth_before_sdk(monkeypatch):
    from live_mem import server
    from live_mem.auth.middleware import AuthMiddleware

    monkeypatch.setattr(server, "_reject_weak_bootstrap_key", lambda key: None)
    monkeypatch.setattr(server.settings, "hivemind_mesh_enabled", "false")
    monkeypatch.setattr(server.settings, "mcp_request_max_bytes", 32)
    app = server.create_app()
    wrappers = []
    while hasattr(app, "app"):
        wrappers.append(app)
        app = app.app
    guard = next(layer for layer in wrappers if isinstance(layer, MCPRequestLimitMiddleware))
    auth = next(layer for layer in wrappers if isinstance(layer, AuthMiddleware))
    assert wrappers.index(auth) < wrappers.index(guard)
    assert guard.max_bytes == 32
    messages, _ = await _run(guard, [_chunk(b"x" * 33)])
    assert messages[0]["status"] == 413


def test_mcp_edge_and_compose_share_application_budget_default():
    import yaml
    from live_mem.config import Settings

    default = Settings.model_fields["mcp_request_max_bytes"].default
    assert default == 75 * 1024 * 1024
    # 50MiB document base64 plus 1MiB of envelope/metadata still fits.
    assert 4 * ((50 * 1024 * 1024 + 2) // 3) + 1024 * 1024 < default
    setting = f"MCP_REQUEST_MAX_BYTES=${{MCP_REQUEST_MAX_BYTES:-{default}}}"
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for service in ["hivemind", "waf"]:
        assert setting in compose["services"][service]["environment"]
    source = (ROOT / "waf/Caddyfile").read_text()
    mcp = source.split("\thandle /mcp* {", 1)[1].split("\n\t}", 1)[0]
    assert f"max_size {{$MCP_REQUEST_MAX_BYTES:{default}}}" in mcp
    assert mcp.index("request_body {") < mcp.index("reverse_proxy")
    assert "coraza_waf" not in mcp
    assert "flush_interval -1" in mcp


def test_budget_configuration_reads_integer_bytes_from_environment(monkeypatch):
    from live_mem.config import Settings

    monkeypatch.setenv("MCP_REQUEST_MAX_BYTES", "65536")
    assert Settings(_env_file=None).mcp_request_max_bytes == 65536
    monkeypatch.setenv("MCP_REQUEST_MAX_BYTES", "75MiB")
    with pytest.raises(ValueError, match="mcp_request_max_bytes"):
        Settings(_env_file=None)


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_budget_fails_closed(limit):
    from live_mem.config import Settings

    with pytest.raises(ValueError, match="MCP_REQUEST_MAX_BYTES"):
        Settings(_env_file=None, mcp_request_max_bytes=limit)
    with pytest.raises(ValueError, match="positive"):
        MCPRequestLimitMiddleware(_reader, max_bytes=limit)
