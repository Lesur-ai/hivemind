"""SDK 2 wire contract through the production factories and auth middleware.

No provider/store traffic: process-owned infrastructure hooks are covered by the
lifespan suites. These tests isolate the actual MCP HTTP app/auth boundary.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import socket

import uvicorn

import pytest
from starlette.testclient import TestClient

from tests.fakes.inference_fakes import apply_graph_memory_baseline_env

KEY = "mcp2-wire-fixture-bootstrap-key-32chars"
HEADERS = {"Authorization": f"Bearer {KEY}",
           "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}


@pytest.fixture(params=["core", "graph"])
def wire_app(request, monkeypatch):
    if request.param == "core":
        from live_mem import server
        from live_mem.auth.context import bind_mcp_request_identity
        from live_mem.auth.middleware import AuthMiddleware
        from live_mem.tools import register_all_tools
        from live_mem.tools.exposure import HivemindMCPServer
        from tests.test_asgi_lifespan_integration import _prepare_core_session_preflight
        _prepare_core_session_preflight(monkeypatch)
        mcp = HivemindMCPServer("core-wire", version="1.6.1",
                               lifespan=server._lifespan,
                               middleware=[bind_mcp_request_identity])
        register_all_tools(mcp)
        monkeypatch.setattr(server, "mcp", mcp)
        monkeypatch.setattr(server.settings, "hivemind_mesh_enabled", "false")
        monkeypatch.setattr(server.settings, "admin_bootstrap_key", KEY)
        factory = server.create_app()
        limit = server.settings.mcp_request_max_bytes
    else:
        from mcp.server.mcpserver import MCPServer
        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server
        from mcp_memory.auth.context import bind_mcp_auth
        from mcp_memory.auth.middleware import AuthMiddleware
        mcp = MCPServer("graph-wire", tools=server.mcp._tool_manager.list_tools(),
                        middleware=[bind_mcp_auth])
        monkeypatch.setattr(server, "mcp", mcp)
        monkeypatch.setattr(server.settings, "admin_bootstrap_key", KEY)
        factory = server._create_app()
        limit = server.settings.mcp_request_max_bytes
    # Preserve the factory's SDK configuration, isolate infrastructure hooks,
    # and put the real component auth middleware around its HTTP transport.
    inner = factory
    while hasattr(inner, "app"):
        inner = inner.app
    return AuthMiddleware(inner), limit, request.param


def _rpc(client, method, params, *, size=None, headers=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    if size is not None:
        assert size >= len(body)
        body += b" " * (size - len(body))
    headers = dict(headers or HEADERS)
    if headers.get("MCP-Protocol-Version") == "2026-07-28":
        headers["MCP-Method"] = method
        if "name" in params:
            headers["MCP-Name"] = params["name"]
    return client.post("/mcp", headers=headers, content=body)


def _initialize(client, *, size=None):
    return _rpc(client, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "wire-fixture", "version": "1"},
    }, size=size)


def test_factory_accepts_above_four_mib_and_exact_wire_limit(wire_app):
    app, limit, _ = wire_app
    assert limit == 75 * 1024 * 1024
    with TestClient(app, base_url="http://localhost:8002") as client:
        assert _initialize(client, size=5 * 1024 * 1024).status_code == 200
        assert _initialize(client, size=limit).status_code == 200
        assert _initialize(client, size=limit + 1).status_code == 413


def test_modern_stateless_request_preserves_real_auth_context(wire_app):
    app, _, kind = wire_app
    params = {
        "name": "system_whoami", "arguments": {},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }
    with TestClient(app, base_url="http://localhost:8002") as client:
        response = _rpc(client, "tools/call", params, headers={**HEADERS,
            "MCP-Protocol-Version": "2026-07-28"})
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers
        # Both production handlers expose the authenticated caller, not an
        # inherited session identity or missing-context fallback.
        if response.headers["content-type"].startswith("text/event-stream"):
            messages = [json.loads(line[6:]) for line in response.text.splitlines()
                        if line.startswith("data: ")]
            message = next(m for m in messages if m.get("id") == 1)
        else:
            message = response.json()
        result = json.loads(message["result"]["content"][0]["text"])
        assert result["status"] == "ok"
        assert result["auth_type"] == "bootstrap"
        assert {"read", "write", "admin"} <= set(result["permissions"])
        denied = _rpc(client, "tools/call", params, headers={
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Accept": "application/json, text/event-stream",
        })
        assert denied.status_code == 401


@asynccontextmanager
async def _running_http(app):
    from sse_starlette.sse import AppStatus

    # sse-starlette 3.3.4 observes Uvicorn shutdown via process-global state.
    # Closing this test server must not drain the next test's fresh server.
    previous_sse_exit = AppStatus.should_exit
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, lifespan="on", access_log=False,
        ))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise AssertionError("HTTP server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=5)
            finally:
                AppStatus.should_exit = previous_sse_exit


async def test_owned_cli_and_bridge_clients_over_real_http(wire_app):
    """Exercise both shipped Python adapters against each production transport."""
    from scripts.cli.client import MCPClient
    from live_mem.core.graph_bridge import GraphMemoryClient

    app, _, kind = wire_app
    async with _running_http(app) as url:
        cli = MCPClient(url, KEY, timeout=5)
        bridge = GraphMemoryClient(url, KEY, timeout=5)
        results = [await cli.call_tool("system_whoami", {}),
                   await bridge.call_tool("system_whoami", {})]
        results += await bridge.call_tools_batch([
            ("system_whoami", {}), ("system_whoami", {}),
        ])
        assert len(results) == 4
        for result in results:
            assert result["status"] == "ok", (kind, result)
            assert result["auth_type"] == "bootstrap"
            assert {"read", "write", "admin"} <= set(result["permissions"])


def _response_message(response):
    assert response.status_code == 200, response.text[:1000]
    if response.headers["content-type"].startswith("text/event-stream"):
        return next(json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ") and json.loads(line[6:]).get("id") == 1)
    return response.json()


def test_production_core_discovery_binds_current_identity(monkeypatch):
    """Do not replace server.mcp: pin its actual registration and middleware.

    A session opened by an admin must project the reader's current permissions,
    and modern stateless discovery must use the same production wiring.
    """
    from live_mem import server
    from live_mem.auth.middleware import AuthMiddleware
    from live_mem.tools.exposure import DISCOVERY_NAMES_BY_PERMISSION, ToolPermission
    from tests.test_asgi_lifespan_integration import _prepare_core_session_preflight

    _prepare_core_session_preflight(monkeypatch)
    monkeypatch.setattr(server.settings, "hivemind_mesh_enabled", "false")
    monkeypatch.setattr(server.settings, "admin_bootstrap_key", KEY)
    app = server.create_app()
    while hasattr(app, "app"):
        app = app.app
    app = AuthMiddleware(app)
    validate_token = app._validate_token

    async def validate_fixture(token):
        if token == "reader-fixture":
            return {"type": "token", "client_name": "reader",
                    "permissions": ["read"], "allowed_resources": ["fixture"]}
        return await validate_token(token)

    monkeypatch.setattr(app, "_validate_token", validate_fixture)
    with TestClient(app, base_url="http://localhost:8002") as client:
        initialized = _initialize(client)
        assert initialized.status_code == 200
        session_headers = {**HEADERS,
            "MCP-Session-Id": initialized.headers["mcp-session-id"],
            "MCP-Protocol-Version": "2025-11-25"}
        ready = client.post("/mcp", headers=session_headers,
                            json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert ready.status_code == 202
        for modern in (False, True):
            for permission, token in ((ToolPermission.ADMIN, KEY),
                                      (ToolPermission.READ, "reader-fixture")):
                headers = {**(HEADERS if modern else session_headers),
                           "Authorization": f"Bearer {token}"}
                params = {}
                if modern:
                    headers["MCP-Protocol-Version"] = "2026-07-28"
                    params["_meta"] = {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                message = _response_message(_rpc(client, "tools/list", params, headers=headers))
                assert [tool["name"] for tool in message["result"]["tools"]] == list(
                    DISCOVERY_NAMES_BY_PERMISSION[permission])


def test_production_graph_installs_request_identity_boundary(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory import server
    from mcp_memory.auth.context import bind_mcp_auth

    assert bind_mcp_auth in server.mcp.middleware


@pytest.mark.parametrize("adapter", ["cli", "bridge", "bridge-batch"])
async def test_owned_clients_receive_exports_above_request_envelope(adapter):
    """SDK 1 allowed large SSE replies: the request cap must not cap backups."""
    from mcp.server.mcpserver import MCPServer
    from scripts.cli.client import MCPClient
    from live_mem.core.graph_bridge import GraphMemoryClient

    mcp = MCPServer("large-export-fixture")
    payload_size = 76 * 1024 * 1024

    @mcp.tool()
    def backup_download() -> dict:
        return {"status": "ok", "archive_base64": "A" * payload_size}

    async with _running_http(mcp.streamable_http_app()) as url:
        if adapter == "cli":
            result = await MCPClient(url, timeout=30).call_tool("backup_download", {})
        else:
            bridge = GraphMemoryClient(url, "", timeout=30)
            if adapter == "bridge-batch":
                result, = await bridge.call_tools_batch([("backup_download", {})])
            else:
                result = await bridge.call_tool("backup_download", {})
        assert result["status"] == "ok"
        assert len(result["archive_base64"]) == payload_size


@pytest.mark.parametrize("batch", [False, True])
async def test_bridge_refuses_invalid_operator_ca_file(monkeypatch, tmp_path, batch):
    """CA environment remains effective even while proxy routing is disabled."""
    from live_mem.core.graph_bridge import GraphMemoryClient

    ca = tmp_path / "invalid-ca.pem"
    ca.write_text("this is not a certificate")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    bridge = GraphMemoryClient("https://127.0.0.1:1", "", timeout=1)
    if batch:
        result, = await bridge.call_tools_batch([("system_whoami", {})])
        assert result["status"] == "error"
        assert "certificate" in result["message"].lower()
    else:
        with pytest.raises(ConnectionError, match="(?i)certificate"):
            await bridge.call_tool("system_whoami", {})


@pytest.mark.parametrize("fresh", [None, {
    "type": "token", "client_name": "reader", "permissions": ["read"],
    "memory_ids": ["allowed"],
}])
async def test_graph_request_middleware_overrides_stale_admin_identity(fresh):
    from types import SimpleNamespace
    from mcp_memory.auth.context import (
        bind_mcp_auth, check_admin_permission, check_memory_access, current_auth,
    )
    old = {"type": "bootstrap", "permissions": ["admin", "read", "write"]}
    token = current_auth.set(old)
    context = SimpleNamespace(request=SimpleNamespace(scope={"auth": fresh}))

    async def call_next(_context):
        assert current_auth.get() == fresh
        assert check_admin_permission()["status"] == "error"
        assert check_memory_access("denied")["status"] == "error"
        raise RuntimeError("handler failure")

    try:
        with pytest.raises(RuntimeError, match="handler failure"):
            await bind_mcp_auth(context, call_next)
        assert current_auth.get() is old
    finally:
        current_auth.reset(token)
