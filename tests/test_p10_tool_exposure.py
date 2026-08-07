"""P10-1 fail-closed, request-scoped MCP discovery contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
import warnings
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.context import RequestContext
from mcp.types import CallToolRequest, ListToolsRequest
from starlette.requests import Request

import live_mem.auth.context as auth_context
from live_mem.auth.context import (
    REQUEST_TOKEN_INFO_STATE_KEY,
    current_token_info,
)
from live_mem.auth.middleware import AuthMiddleware
from live_mem.core.models import TokenInfo, TokensStore
from live_mem.core.tokens import TokenService
from live_mem.tools import call_tool_direct, register_all_tools
from live_mem.tools.exposure import (
    DISCOVERY_NAMES_BY_PERMISSION,
    DISCOVERY_SCHEMA_BUDGET_BYTES,
    TOOL_EXPOSURES,
    HivemindFastMCP,
    ToolAudience,
    ToolOperation,
    ToolPermission,
    exposure_manifest,
    registered_exposure_names,
    validate_tool_exposure_registry,
)

ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "tool_discovery.json").read_text(
        encoding="utf-8"
    )
)
COMPLETE_FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "tool_surface.json").read_text(
        encoding="utf-8"
    )
)


def _identity(
    permission: str,
    *,
    allowed: tuple[str, ...] = ("proj",),
    client_name: str | None = None,
    hash_char: str = "a",
) -> dict:
    return {
        "type": "token",
        "client_name": client_name or f"{permission}-client",
        "permissions": [permission],
        "allowed_resources": list(allowed),
        "token_hash": "sha256:" + hash_char * 64,
    }


def _request_context(token_info: dict | None) -> RequestContext:
    state = {}
    if token_info is not None:
        state[REQUEST_TOKEN_INFO_STATE_KEY] = token_info
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [],
            "state": state,
        }
    )
    return RequestContext(
        request_id=1,
        meta=None,
        session=None,  # type: ignore[arg-type]
        lifespan_context={},
        request=request,
    )


@contextmanager
def _mcp_request(token_info: dict | None) -> Iterator[None]:
    token = request_ctx.set(_request_context(token_info))
    try:
        yield
    finally:
        request_ctx.reset(token)


@pytest.fixture
def exposed_mcp() -> HivemindFastMCP:
    mcp = HivemindFastMCP("p10-tool-exposure")
    register_all_tools(mcp)
    return mcp


@pytest.fixture(autouse=True)
def _clear_auth_stores() -> Iterator[None]:
    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()
    yield
    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()


async def _list_result(mcp: HivemindFastMCP, token_info: dict | None):
    handler = mcp._mcp_server.request_handlers[ListToolsRequest]
    with _mcp_request(token_info):
        return await handler(ListToolsRequest())


async def _call_result(
    mcp: HivemindFastMCP,
    name: str,
    arguments: dict,
    token_info: dict,
):
    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        params={"name": name, "arguments": arguments}
    )
    with _mcp_request(token_info):
        return await handler(request)


def _call_payload(result) -> dict:
    text_blocks = [
        block.text
        for block in result.root.content
        if getattr(block, "type", None) == "text"
    ]
    assert len(text_blocks) == 1
    return json.loads(text_blocks[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    tuple(DISCOVERY_FIXTURE["discovery"]),
)
async def test_low_level_tools_list_is_exact_and_ordered_by_request_permission(
    exposed_mcp: HivemindFastMCP,
    permission: str,
) -> None:
    result = await _list_result(exposed_mcp, _identity(permission))
    names = [tool.name for tool in result.root.tools]

    assert names == DISCOVERY_FIXTURE["discovery"][permission]
    assert names == list(
        DISCOVERY_NAMES_BY_PERMISSION[ToolPermission(permission)]
    )
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_discovery_never_advertises_alias_operator_or_mesh_names(
    exposed_mcp: HivemindFastMCP,
) -> None:
    alias_names = {alias for entry in TOOL_EXPOSURES for alias in entry.aliases}
    operator_names = {
        entry.canonical_name
        for entry in TOOL_EXPOSURES
        if entry.audience is ToolAudience.OPERATOR
    }

    for permission in ("read", "write", "manage", "admin"):
        result = await _list_result(exposed_mcp, _identity(permission))
        names = {tool.name for tool in result.root.tools}
        assert names.isdisjoint(alias_names)
        assert names.isdisjoint(operator_names)
        assert not any("mesh" in name for name in names)


@pytest.mark.asyncio
async def test_write_discovery_restores_canonical_own_note_consolidation(
    exposed_mcp: HivemindFastMCP,
) -> None:
    result = await _list_result(exposed_mcp, _identity("write"))
    names = [tool.name for tool in result.root.tools]

    assert "mid_consolidate" in names
    assert "bank_consolidate" not in names


@pytest.mark.asyncio
async def test_adr0022_provisioning_is_discovered_only_at_manage_or_admin(
    exposed_mcp: HivemindFastMCP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = type(
        "FakeSpaceService",
        (),
        {"create": AsyncMock(return_value={"status": "created", "space_id": "new"})},
    )()
    monkeypatch.setattr("live_mem.core.space.get_space_service", lambda: service)

    listed = {}
    for permission in ("read", "write", "manage", "admin"):
        result = await _list_result(exposed_mcp, _identity(permission))
        listed[permission] = {tool.name for tool in result.root.tools}

    provisioning = {"space_create", "token_create", "space_invite_token"}
    assert listed["read"].isdisjoint(provisioning)
    assert listed["write"].isdisjoint(provisioning)
    assert provisioning <= listed["manage"]
    assert provisioning <= listed["admin"]

    denied = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_create",
            {"space_id": "new", "description": "new", "rules": "# Rules"},
            _identity("write"),
        )
    )
    allowed = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_create",
            {"space_id": "new", "description": "new", "rules": "# Rules"},
            _identity("manage", hash_char="8"),
        )
    )

    assert denied["status"] == "error"
    assert "manage" in denied["message"]
    assert allowed == {"status": "created", "space_id": "new"}
    service.create.assert_awaited_once_with(
        space_id="new",
        description="new",
        rules="# Rules",
        owner="",
        actor_token_hash="sha256:" + "8" * 64,
        bootstrap_admin=False,
    )


@pytest.mark.asyncio
async def test_largest_real_low_level_discovery_response_stays_below_64_kib(
    exposed_mcp: HivemindFastMCP,
) -> None:
    sizes = []
    for permission in ("read", "write", "manage", "admin"):
        result = await _list_result(exposed_mcp, _identity(permission))
        wire = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": result.root.model_dump(by_alias=True, exclude_none=True),
        }
        sizes.append(
            len(
                json.dumps(
                    wire,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        )

    assert max(sizes) <= DISCOVERY_SCHEMA_BUDGET_BYTES


def test_complete_exposure_registry_matches_canonical_surface(
    exposed_mcp: HivemindFastMCP,
) -> None:
    registered = set(exposed_mcp._tool_manager._tools)
    exposure_names = registered_exposure_names()
    complete_aliases = set(COMPLETE_FIXTURE["alias_map"].values())
    complete_names = set(COMPLETE_FIXTURE["historical"]) | complete_aliases

    # This P10 test owns only exposure-projection parity. The fixture's full
    # surface/count and live registration are validated canonically in
    # test_mcp_tool_surface.py.
    assert len(exposure_names) == len(set(exposure_names)), (
        "exposure registry must not contain duplicate registered names"
    )
    assert registered == complete_names == set(exposure_names)


@pytest.mark.asyncio
async def test_discovery_fails_closed_without_valid_request_identity(
    exposed_mcp: HivemindFastMCP,
) -> None:
    assert await exposed_mcp.list_tools() == []
    assert (await _list_result(exposed_mcp, None)).root.tools == []
    malformed = _identity("read")
    malformed["permissions"] = ["read", "read"]
    assert (await _list_result(exposed_mcp, malformed)).root.tools == []
    wrong_container = _identity("read")
    wrong_container["permissions"] = {"read"}
    assert (await _list_result(exposed_mcp, wrong_container)).root.tools == []


@pytest.mark.asyncio
async def test_request_identity_overrides_stale_ambient_context_on_next_call(
    exposed_mcp: HivemindFastMCP,
) -> None:
    ambient_manage = current_token_info.set(_identity("manage", hash_char="b"))
    try:
        read_names = [
            tool.name
            for tool in (
                await _list_result(exposed_mcp, _identity("read", hash_char="c"))
            ).root.tools
        ]
    finally:
        current_token_info.reset(ambient_manage)

    ambient_read = current_token_info.set(_identity("read", hash_char="d"))
    try:
        manage_names = [
            tool.name
            for tool in (
                await _list_result(
                    exposed_mcp,
                    _identity("manage", hash_char="e"),
                )
            ).root.tools
        ]
    finally:
        current_token_info.reset(ambient_read)

    assert read_names == DISCOVERY_FIXTURE["discovery"]["read"]
    assert manage_names == DISCOVERY_FIXTURE["discovery"]["manage"]


@pytest.mark.asyncio
async def test_alternating_and_concurrent_requests_never_union_discovery_cache(
    exposed_mcp: HivemindFastMCP,
) -> None:
    alternating = []
    for permission in ("read", "manage", "read"):
        result = await _list_result(exposed_mcp, _identity(permission))
        alternating.append([tool.name for tool in result.root.tools])
    assert alternating == [
        DISCOVERY_FIXTURE["discovery"]["read"],
        DISCOVERY_FIXTURE["discovery"]["manage"],
        DISCOVERY_FIXTURE["discovery"]["read"],
    ]

    async def discover(permission: str) -> tuple[str, list[str]]:
        await asyncio.sleep(0)
        result = await _list_result(exposed_mcp, _identity(permission))
        return permission, [tool.name for tool in result.root.tools]

    profiles = ["read", "admin", "write", "manage"] * 4
    results = await asyncio.gather(*(discover(profile) for profile in profiles))
    for permission, names in results:
        assert names == DISCOVERY_FIXTURE["discovery"][permission]

    cached = list(exposed_mcp._mcp_server._tool_cache)
    assert cached in [
        DISCOVERY_FIXTURE["discovery"][permission]
        for permission in ("read", "write", "manage", "admin")
    ]


@pytest.mark.asyncio
async def test_system_about_uses_same_compact_projection_without_secondary_leak(
    exposed_mcp: HivemindFastMCP,
) -> None:
    result = await _call_result(
        exposed_mcp,
        "system_about",
        {},
        _identity("read"),
    )
    payload = _call_payload(result)
    names = [tool["name"] for tool in payload["tools"]]

    assert payload["tools_count"] == len(names)
    assert names == DISCOVERY_FIXTURE["discovery"]["read"]


@pytest.mark.asyncio
async def test_system_about_keeps_projection_through_direct_console_proxy(
    exposed_mcp: HivemindFastMCP,
) -> None:
    ambient = current_token_info.set(_identity("write"))
    try:
        payload = await call_tool_direct("system_about", {})
    finally:
        current_token_info.reset(ambient)

    names = [tool["name"] for tool in payload["tools"]]
    assert payload["tools_count"] == len(names)
    assert names == DISCOVERY_FIXTURE["discovery"]["write"]


def test_current_agent_name_prefers_fresh_request_identity_over_stale_session() -> None:
    from live_mem.auth.context import get_current_agent_name

    ambient = current_token_info.set(
        _identity("admin", client_name="stale-session", hash_char="6")
    )
    try:
        with _mcp_request(
            _identity("write", client_name="fresh-request", hash_char="7")
        ):
            assert get_current_agent_name() == "fresh-request"
    finally:
        current_token_info.reset(ambient)


@pytest.mark.asyncio
async def test_hidden_historical_alias_is_callable_and_refuses_identically(
    exposed_mcp: HivemindFastMCP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLiveService:
        read_notes = AsyncMock(
            return_value={"status": "ok", "notes": [], "total": 0}
        )

    fake = FakeLiveService()
    monkeypatch.setattr("live_mem.core.live.get_live_service", lambda: fake)
    await _list_result(exposed_mcp, _identity("read"))

    allowed = _call_payload(
        await _call_result(
            exposed_mcp,
            "live_read",
            {"space_id": "proj"},
            _identity("read", allowed=("proj",)),
        )
    )
    denied_alias = _call_payload(
        await _call_result(
            exposed_mcp,
            "live_read",
            {"space_id": "proj"},
            _identity("read", allowed=()),
        )
    )
    denied_canonical = _call_payload(
        await _call_result(
            exposed_mcp,
            "short_read",
            {"space_id": "proj"},
            _identity("read", allowed=()),
        )
    )

    assert allowed["status"] == "ok"
    assert denied_alias == denied_canonical
    assert denied_alias["status"] == "error"


@pytest.mark.asyncio
async def test_hidden_operator_call_uses_current_request_not_ambient_identity(
    exposed_mcp: HivemindFastMCP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = type(
        "FakeTokenService",
        (),
        {
            "list_tokens": AsyncMock(
                return_value={"status": "ok", "tokens": [], "total": 0}
            )
        },
    )()
    monkeypatch.setattr("live_mem.core.tokens.get_token_service", lambda: service)
    await _list_result(exposed_mcp, _identity("read"))

    ambient_admin = current_token_info.set(_identity("admin", hash_char="f"))
    try:
        denied = _call_payload(
            await _call_result(
                exposed_mcp,
                "admin_list_tokens",
                {},
                _identity("read", hash_char="1"),
            )
        )
    finally:
        current_token_info.reset(ambient_admin)

    ambient_read = current_token_info.set(_identity("read", hash_char="2"))
    try:
        allowed = _call_payload(
            await _call_result(
                exposed_mcp,
                "admin_list_tokens",
                {},
                _identity("admin", hash_char="3"),
            )
        )
    finally:
        current_token_info.reset(ambient_read)

    assert denied["status"] == "error"
    assert "admin" in denied["message"]
    assert allowed == {"status": "ok", "tokens": [], "total": 0}
    service.list_tokens.assert_awaited_once()


@pytest.mark.asyncio
async def test_hidden_write_manage_and_destructive_operators_keep_live_guards(
    exposed_mcp: HivemindFastMCP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = type(
        "FakeSpaceService",
        (),
        {
            "update": AsyncMock(return_value={"status": "ok", "op": "update"}),
            "update_rules": AsyncMock(
                return_value={"status": "ok", "op": "rules"}
            ),
            "delete": AsyncMock(return_value={"status": "deleted"}),
        },
    )()
    monkeypatch.setattr("live_mem.core.space.get_space_service", lambda: service)

    write_allowed = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_update",
            {"space_id": "proj", "description": "updated"},
            _identity("write"),
        )
    )
    write_denied = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_update",
            {"space_id": "proj", "description": "updated"},
            _identity("read"),
        )
    )
    manage_allowed = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_update_rules",
            {"space_id": "proj", "rules": "# Rules"},
            _identity("manage"),
        )
    )
    manage_denied = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_update_rules",
            {"space_id": "proj", "rules": "# Rules"},
            _identity("write"),
        )
    )
    destructive_allowed = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_delete",
            {"space_id": "proj", "confirm": True},
            _identity("manage"),
        )
    )
    destructive_denied = _call_payload(
        await _call_result(
            exposed_mcp,
            "space_delete",
            {"space_id": "proj", "confirm": True},
            _identity("write"),
        )
    )

    assert write_allowed == {"status": "ok", "op": "update"}
    assert write_denied["status"] == "error"
    assert manage_allowed == {"status": "ok", "op": "rules"}
    assert manage_denied["status"] == "error"
    assert destructive_allowed == {"status": "deleted"}
    assert destructive_denied["status"] == "error"
    service.update.assert_awaited_once()
    service.update_rules.assert_awaited_once()
    service.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_middleware_attaches_isolated_sanitized_request_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict] = {}

    async def app(scope, _receive, _send):
        captured[scope["path"]] = scope["state"][REQUEST_TOKEN_INFO_STATE_KEY]

    middleware = AuthMiddleware(app)
    identities = {
        "secret-read": _identity("read", hash_char="4"),
        "secret-admin": _identity("admin", hash_char="5"),
    }
    both_entered = asyncio.Event()
    entered = 0

    async def validate(raw_token: str):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return identities[raw_token]

    monkeypatch.setattr(middleware, "_validate_token", validate)

    async def invoke(path: str, bearer: str) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"authorization", f"Bearer {bearer}".encode())],
            "state": {},
            "client": ("127.0.0.1", 1),
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)

    await asyncio.gather(
        invoke("/mcp/read", "secret-read"),
        invoke("/mcp/admin", "secret-admin"),
    )

    assert captured["/mcp/read"]["permissions"] == ["read"]
    assert captured["/mcp/admin"]["permissions"] == ["admin"]
    assert "secret-read" not in repr(captured)
    assert "secret-admin" not in repr(captured)


def test_streamable_http_same_session_observes_permission_and_scope_rescope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise middleware -> SDK metadata -> discovery/guard end to end."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    raw_token = "lm_p10_same_session_transport"
    token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
    token_record = TokenInfo(
        hash=token_hash,
        name="same-session",
        permissions=["read"],
        space_ids=[],
        created_at="2026-07-15T00:00:00+00:00",
    )
    store = TokensStore(tokens=[token_record])
    token_service = TokenService()
    monkeypatch.setattr(
        token_service,
        "_load_store",
        AsyncMock(return_value=store),
    )
    monkeypatch.setattr(token_service, "_save_store", AsyncMock())

    live_service = type(
        "FakeLiveService",
        (),
        {
            "read_notes": AsyncMock(
                return_value={"status": "ok", "notes": [], "total": 0}
            )
        },
    )()
    monkeypatch.setattr(
        "live_mem.core.live.get_live_service",
        lambda: live_service,
    )

    mcp = HivemindFastMCP(
        "p10-streamable-http",
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["testserver"]
        ),
    )
    register_all_tools(mcp)
    app = AuthMiddleware(mcp.streamable_http_app())
    monkeypatch.setattr(app, "_validate_token", token_service.validate_token)

    base_headers = {
        "Authorization": f"Bearer {raw_token}",
        "Accept": "application/json, text/event-stream",
    }

    def rpc(client, headers, request_id, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        return client.post("/mcp", headers=headers, json=payload)

    def listed_names(client, headers, request_id):
        response = rpc(client, headers, request_id, "tools/list", {})
        assert response.status_code == 200
        return [tool["name"] for tool in response.json()["result"]["tools"]]

    def call_payload(client, headers, request_id):
        response = rpc(
            client,
            headers,
            request_id,
            "tools/call",
            {"name": "live_read", "arguments": {"space_id": "proj"}},
        )
        assert response.status_code == 200
        text = response.json()["result"]["content"][0]["text"]
        return json.loads(text)

    with TestClient(app) as client:
        initialized = rpc(
            client,
            base_headers,
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "p10-test", "version": "1"},
            },
        )
        assert initialized.status_code == 200
        session_headers = {
            **base_headers,
            "mcp-session-id": initialized.headers["mcp-session-id"],
            "mcp-protocol-version": "2025-06-18",
        }
        assert rpc(
            client,
            session_headers,
            None,
            "notifications/initialized",
        ).status_code == 202

        assert listed_names(client, session_headers, 2) == (
            DISCOVERY_FIXTURE["discovery"]["read"]
        )
        promoted = client.portal.call(
            partial(
                token_service.update_token,
                token_hash=token_hash,
                permissions="read,write,manage",
            )
        )
        assert promoted["mcp_reconnect_required"] is True
        assert listed_names(client, session_headers, 3) == (
            DISCOVERY_FIXTURE["discovery"]["manage"]
        )

        downgraded = client.portal.call(
            partial(
                token_service.update_token,
                token_hash=token_hash,
                permissions="read",
            )
        )
        assert downgraded["mcp_reconnect_required"] is True
        assert listed_names(client, session_headers, 4) == (
            DISCOVERY_FIXTURE["discovery"]["read"]
        )

        assert call_payload(client, session_headers, 5)["status"] == "error"
        granted = client.portal.call(
            partial(
                token_service.update_token,
                token_hash=token_hash,
                space_ids_add="proj",
            )
        )
        assert granted["mcp_reconnect_required"] is True
        assert call_payload(client, session_headers, 6)["status"] == "ok"

        removed = client.portal.call(
            partial(
                token_service.update_token,
                token_hash=token_hash,
                space_ids_remove="proj",
            )
        )
        assert removed["mcp_reconnect_required"] is True
        assert call_payload(client, session_headers, 7)["status"] == "error"


def _registry_replacing(name: str, **changes) -> tuple:
    entries = list(TOOL_EXPOSURES)
    index = next(
        index
        for index, entry in enumerate(entries)
        if entry.canonical_name == name
    )
    entries[index] = replace(entries[index], **changes)
    return tuple(entries)


def test_registry_mutations_fail_closed(exposed_mcp: HivemindFastMCP) -> None:
    mutations = [
        TOOL_EXPOSURES[:-1],
        _registry_replacing("system_about", aliases=("system_health",)),
        _registry_replacing("system_about", audience=ToolAudience.OPERATOR),
        _registry_replacing("space_create", audience=ToolAudience.OPERATOR),
        _registry_replacing("token_create", audience=ToolAudience.OPERATOR),
        _registry_replacing(
            "space_invite_token",
            audience=ToolAudience.OPERATOR,
        ),
        _registry_replacing("short_note", minimum_permission=ToolPermission.READ),
        _registry_replacing(
            "admin_gc_notes",
            minimum_permission=ToolPermission.READ,
        ),
        _registry_replacing("system_about", operation=ToolOperation.MUTATION),
        _registry_replacing("system_about", space_scope_argument="space_id"),
        _registry_replacing("space_info", space_scope_argument=None),
    ]
    duplicate = list(TOOL_EXPOSURES)
    duplicate[-1] = duplicate[0]
    mutations.append(tuple(duplicate))

    for registry in mutations:
        with pytest.raises(RuntimeError):
            validate_tool_exposure_registry(exposed_mcp, registry=registry)

    with pytest.raises(RuntimeError, match="duplicate overwrite"):
        validate_tool_exposure_registry(
            exposed_mcp,
            declared_registration_count=len(registered_exposure_names()) + 1,
        )


def test_alias_metadata_drift_fails_closed(
    exposed_mcp: HivemindFastMCP,
) -> None:
    exposed_mcp._tool_manager._tools["live_read"].description += " drift"
    with pytest.raises(RuntimeError, match="alias description differs"):
        validate_tool_exposure_registry(exposed_mcp)


def test_generated_fixture_docs_and_console_mapping_match_registry() -> None:
    manifest = exposure_manifest()
    assert manifest.pop("registered_total") == COMPLETE_FIXTURE["total"]
    # These are different name spaces: registry entries use tier names while
    # historical_count covers the historical source names. Their cardinality
    # equality pins the current one-entry-per-historical-tool contract.
    assert (
        manifest.pop("registry_entries")
        == COMPLETE_FIXTURE["historical_count"]
    )
    assert DISCOVERY_FIXTURE == manifest
    result = subprocess.run(
        [sys.executable, "scripts/generate_tool_exposure.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    generated_js = (
        ROOT
        / "src"
        / "live_mem"
        / "static"
        / "js"
        / "admin-tool-capabilities.generated.js"
    ).read_text(encoding="utf-8")
    serialized = generated_js.split("Object.freeze(", 1)[1].rsplit(");", 1)[0]
    capabilities = json.loads(serialized)
    assert set(capabilities) == set(registered_exposure_names())

    literal_calls: set[str] = set()
    for path in (ROOT / "src" / "live_mem" / "static" / "js").rglob("*.js"):
        literal_calls.update(
            re.findall(r"callTool\(\s*['\"]([a-z][a-z0-9_]*)['\"]", path.read_text())
        )
    assert literal_calls <= set(capabilities)

    admin_html = (ROOT / "src" / "live_mem" / "static" / "admin.html").read_text()
    assert admin_html.index("admin-tool-capabilities.generated.js") < admin_html.index(
        "admin-app.js"
    )
    operator_js = (
        ROOT
        / "src"
        / "live_mem"
        / "static"
        / "js"
        / "admin"
        / "views-operator.js"
    ).read_text()
    assert "toolCapabilityHint('bank_compact'" in operator_js
    assert "toolCapabilityHint('admin_gc_notes'" in operator_js
