# -*- coding: utf-8 -*-
"""Adversarial contract tests for the P8-6 process-local audit window."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.auth.context import MonoTenantSpaceAllowlistProvider
from live_mem.config import Settings
from live_mem.core import audit_ring
from live_mem.middleware import ResponseLimitMiddleware
from live_mem.tools import admin as admin_tools


ROOT = Path(__file__).resolve().parents[1]
ENTRY_KEYS = {
    "ts",
    "event",
    "tool",
    "arguments_keys",
    "client",
    "auth_type",
}


@pytest.fixture(autouse=True)
def _isolated_ring(monkeypatch):
    """Keep process-global ring state deterministic across every test."""

    monkeypatch.setattr(
        audit_ring,
        "get_settings",
        lambda: SimpleNamespace(admin_audit_ring_size=500),
    )
    audit_ring.reset_for_tests()
    yield
    audit_ring.reset_for_tests()


def _admin_tool():
    mcp = FastMCP(name="test-admin-audit")
    assert admin_tools.register(mcp) == 9
    tool = mcp._tool_manager._tools["admin_audit_recent"]
    return tool, tool.fn


async def _call_as(fn, token_info: dict[str, Any] | None, **kwargs):
    token = current_token_info.set(token_info)
    try:
        return await fn(**kwargs)
    finally:
        current_token_info.reset(token)


def _admin_identity() -> dict[str, Any]:
    return {
        "client_name": "console-admin",
        "permissions": ["read", "write", "admin"],
        "allowed_resources": [],
        "type": "stored",
    }


def _json_content_bytes(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8"))


def test_closed_capture_set_and_strict_six_field_shape():
    assert audit_ring.CAPTURED_EVENTS == frozenset(
        {"auth_rejected", "admin_tool_call", "login_failed", "login_success"}
    )
    for excluded in (
        "request",
        "bulk_update_tokens",
        "graph_push_volatile_optin",
        "long_ingest_volatile_optin",
    ):
        audit_ring.record_event(event=excluded, client="must-not-appear")
    audit_ring.record_event(
        event="login_success",
        tool="space_delete",
        arguments={"space_id": "secret-space"},
        client="console-admin",
        auth_type="stored",
    )

    entries = audit_ring.snapshot()
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == ENTRY_KEYS
    assert entry == {
        "ts": entry["ts"],
        "event": "login_success",
        "tool": None,
        "arguments_keys": None,
        "client": "console-admin",
        "auth_type": "stored",
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["ts"])


def test_argument_values_never_enter_ring_and_secret_like_keys_are_redacted():
    canary = "TOP-SECRET-VALUE-never-store"
    arguments = {
        "space_id": canary,
        "password": canary,
        "api_key": canary,
        "authorization": canary,
        "lm_token": canary,
        "sk_secret": canary,
        "ghp_material": canary,
        "github_pat_material": canary,
        "database_password": canary,
        "service_secret": canary,
        "api_credential": canary,
        "token_hash": canary,
    }
    audit_ring.record_event(
        event="admin_tool_call",
        tool="admin_list_tokens",
        arguments=arguments,
        client="operator",
        auth_type="stored",
    )
    arguments.clear()

    entry = audit_ring.snapshot()[0]
    assert entry["arguments_keys"] == [
        "space_id",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "[redacted key]",
        "token_hash",
    ]
    assert canary not in json.dumps(entry, ensure_ascii=False)


def test_argument_keys_are_bounded_clipped_and_mark_overflow_exactly():
    arguments = {
        f"k{i:02d}_{'x' * 58}": object()
        for i in range(20)
    }
    audit_ring.record_event(
        event="admin_tool_call",
        tool="admin_bulk_update_tokens",
        arguments=arguments,
        client='client"\\\n' * 30,
        auth_type="stored" * 20,
    )

    entry = audit_ring.snapshot()[0]
    keys = entry["arguments_keys"]
    assert keys is not None
    assert len(keys) == 17
    assert keys[-1] == "+4 more"
    assert all(key.endswith("…") for key in keys[:-1])
    assert all(
        _json_content_bytes(key) <= audit_ring.MAX_ARGUMENT_KEY_JSON_BYTES
        for key in keys
    )
    assert len(json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8")) <= 900


def test_unknown_tool_and_unsafe_unicode_are_normalized_or_redacted():
    audit_ring.record_event(
        event="admin_tool_call",
        tool="evil\u200btool\nname",
        arguments={"ok_key": 1, "bad\nkey": 2},
        client="client\nname\u200b",
        auth_type="stored\x00token",
    )

    entry = audit_ring.snapshot()[0]
    assert entry["tool"] == "[unknown/redacted]"
    assert entry["arguments_keys"] == ["ok_key", "[redacted key]"]
    serialized = json.dumps(entry, ensure_ascii=False)
    assert "\n" not in entry["client"]
    assert "\u200b" not in entry["client"]
    assert "\x00" not in entry["auth_type"]
    assert "�" in serialized


def test_client_and_auth_type_use_json_escape_aware_byte_budgets():
    assert audit_ring.MAX_TOOL_JSON_BYTES == 64
    assert audit_ring.MAX_CLIENT_JSON_BYTES == 64
    assert audit_ring.MAX_AUTH_TYPE_JSON_BYTES == 24

    hostile = '\"\\' * 100
    audit_ring.record_event(
        event="admin_tool_call",
        tool="admin_list_tokens",
        arguments={},
        client=hostile,
        auth_type=hostile,
    )

    entry = audit_ring.snapshot()[0]
    assert entry["client"].endswith("…")
    assert entry["auth_type"].endswith("…")
    assert _json_content_bytes(entry["tool"]) <= audit_ring.MAX_TOOL_JSON_BYTES
    assert _json_content_bytes(entry["client"]) <= audit_ring.MAX_CLIENT_JSON_BYTES
    assert (
        _json_content_bytes(entry["auth_type"])
        <= audit_ring.MAX_AUTH_TYPE_JSON_BYTES
    )
    # A character-count implementation would pass up to 64 hostile characters,
    # whose JSON escape expansion would violate the byte budgets above.
    assert len(entry["client"]) < audit_ring.MAX_CLIENT_JSON_BYTES
    assert len(entry["auth_type"]) < audit_ring.MAX_AUTH_TYPE_JSON_BYTES


def test_final_entry_fallback_removes_only_keys_and_recomputes_marker(monkeypatch):
    assert audit_ring.MAX_ENTRY_JSON_BYTES == 900
    arguments = {f"key_{index:02d}_{'x' * 50}": index for index in range(20)}
    monkeypatch.setattr(audit_ring, "MAX_ENTRY_JSON_BYTES", 500)
    audit_ring.record_event(
        event="admin_tool_call",
        tool="admin_bulk_update_tokens",
        arguments=arguments,
        client='client"\\' * 30,
        auth_type="stored" * 20,
    )

    entry = audit_ring.snapshot()[0]
    keys = entry["arguments_keys"]
    assert set(entry) == ENTRY_KEYS
    assert keys is not None
    assert 1 < len(keys) < 17
    assert keys[-1].startswith("+") and keys[-1].endswith(" more")
    omitted = int(keys[-1][1:-5])
    assert omitted == len(arguments) - (len(keys) - 1)
    assert entry["tool"] == "admin_bulk_update_tokens"
    assert entry["client"] is not None
    assert entry["auth_type"] is not None
    assert len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) <= 500


class _ExplodingMapping(Mapping[object, object]):
    def __getitem__(self, key: object) -> object:
        raise RuntimeError("getitem explosion")

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("iteration explosion")

    def __len__(self) -> int:
        raise RuntimeError("length explosion")


def test_record_event_is_a_never_raise_boundary_for_hostile_inputs(monkeypatch):
    audit_ring.record_event(
        event="admin_tool_call",
        tool="admin_list_tokens",
        arguments=_ExplodingMapping(),
    )
    assert audit_ring.snapshot() == []

    def explode():
        raise RuntimeError("append seam explosion")

    monkeypatch.setattr(audit_ring, "_get_ring", explode)
    # A failure at the final append boundary must still be swallowed.
    audit_ring.record_event(event="login_failed")


def test_capacity_evicts_oldest_and_snapshot_is_mutation_isolated(monkeypatch):
    monkeypatch.setattr(
        audit_ring,
        "get_settings",
        lambda: SimpleNamespace(admin_audit_ring_size=3),
    )
    audit_ring.reset_for_tests()
    for index in range(5):
        audit_ring.record_event(event="login_success", client=f"client-{index}")

    first = audit_ring.snapshot()
    assert audit_ring.capacity() == 3
    assert [entry["client"] for entry in first] == ["client-2", "client-3", "client-4"]
    first[0]["client"] = "mutated-copy"
    assert audit_ring.snapshot()[0]["client"] == "client-2"


def test_settings_default_and_fail_closed_capacity_validation():
    assert Settings.model_validate({}).admin_audit_ring_size == 500
    for invalid in (0, 501):
        with pytest.raises(ValueError, match="ADMIN_AUDIT_RING_SIZE"):
            Settings.model_validate({"admin_audit_ring_size": invalid})
    assert Settings.model_validate({"admin_audit_ring_size": 1}).admin_audit_ring_size == 1
    assert Settings.model_validate({"admin_audit_ring_size": 500}).admin_audit_ring_size == 500


def test_policy_allowed_actions_match_canonical_surface_projection():
    fixture = json.loads(
        (ROOT / "tests/fixtures/tool_surface.json").read_text(encoding="utf-8")
    )
    registered_names = set(fixture["historical"]) | set(
        fixture["alias_map"].values()
    )
    assert (
        set(MonoTenantSpaceAllowlistProvider.ALLOWED_ACTIONS)
        == registered_names
    )
    assert "admin_audit_recent" in registered_names


@pytest.mark.asyncio
async def test_admin_tool_permission_boundary_and_read_only_annotation():
    tool, fn = _admin_tool()
    assert tool.annotations.readOnlyHint is True

    missing = await _call_as(fn, None)
    assert missing == {"status": "error", "message": "Authentication required"}

    non_admin = await _call_as(
        fn,
        {
            "client_name": "reader",
            "permissions": ["read", "write"],
            "allowed_resources": [],
        },
    )
    assert non_admin == {
        "status": "error",
            "message": "The 'admin' permission is required for this operation",
    }


@pytest.mark.asyncio
async def test_admin_tool_empty_shape_limit_clamp_and_newest_first():
    _, fn = _admin_tool()
    empty = await _call_as(fn, _admin_identity(), limit=50)
    assert empty == {
        "status": "ok",
        "entries": [],
        "total": 0,
        "capacity": 500,
        "scope_note": audit_ring.AUDIT_SCOPE_NOTE,
    }

    for index in range(3):
        audit_ring.record_event(event="login_success", client=f"client-{index}")

    low = await _call_as(fn, _admin_identity(), limit=0)
    assert low["total"] == 1
    assert low["entries"][0]["client"] == "client-2"

    high = await _call_as(fn, _admin_identity(), limit=50_000)
    assert high["total"] == 3
    assert [entry["client"] for entry in high["entries"]] == [
        "client-2",
        "client-1",
        "client-0",
    ]
    assert all(set(entry) == ENTRY_KEYS for entry in high["entries"])


@pytest.mark.asyncio
async def test_full_500_entry_response_stays_below_middleware_limit():
    arguments = {
        f"key_{index:02d}_{'x' * 50}": "VALUE-MUST-NOT-APPEAR"
        for index in range(32)
    }
    for index in range(500):
        audit_ring.record_event(
            event="admin_tool_call",
            tool="admin_bulk_update_tokens",
            arguments=arguments,
            client=f"client-{index}-" + ('\"\\\n' * 40),
            auth_type="stored-credential-type" * 8,
        )

    _, fn = _admin_tool()
    result = await _call_as(fn, _admin_identity(), limit=500)
    body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")

    assert result["total"] == 500
    assert result["capacity"] == 500
    assert len(body) < 512 * 1024
    assert all(
        len(json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8"))
        <= audit_ring.MAX_ENTRY_JSON_BYTES
        for entry in result["entries"]
    )

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    messages: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    middleware = ResponseLimitMiddleware(app, max_bytes=512 * 1024)
    await middleware(
        {"type": "http", "method": "GET", "path": "/api/tool"},
        receive,
        send,
    )
    headers = dict(messages[0]["headers"])
    assert b"x-response-truncated" not in headers
    assert messages[1]["body"] == body


@pytest.mark.asyncio
async def test_api_tool_read_audits_itself_before_snapshot(monkeypatch):
    """The normal proxy path records the read before invoking the tool."""

    _, fn = _admin_tool()

    async def direct_call(name: str, arguments: dict) -> dict:
        assert name == "admin_audit_recent"
        return await fn(**arguments)

    import live_mem.tools as tools_package
    from live_mem.auth.middleware import StaticFilesMiddleware

    monkeypatch.setattr(tools_package, "call_tool_direct", direct_call)
    body = json.dumps(
        {"tool": "admin_audit_recent", "arguments": {"limit": 500}}
    ).encode("utf-8")
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message):
        messages.append(message)

    token = current_token_info.set(_admin_identity())
    try:
        await StaticFilesMiddleware(None)._api_tool_call(receive, send)
    finally:
        current_token_info.reset(token)

    response = json.loads(messages[-1]["body"])
    assert response["status"] == "ok"
    assert response["entries"][0]["event"] == "admin_tool_call"
    assert response["entries"][0]["tool"] == "admin_audit_recent"
    assert response["entries"][0]["arguments_keys"] == ["limit"]


def _record_event_statement_lists(tree: ast.AST):
    """Yield statement lists that contain a direct record_event call."""

    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            if not isinstance(value, list) or not value:
                continue
            if not all(isinstance(item, ast.stmt) for item in value):
                continue
            if any(
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "record_event"
                for statement in value
            ):
                yield value


def test_exactly_four_emit_sites_follow_existing_audit_log_statement():
    source = (ROOT / "src/live_mem/auth/middleware.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    observed: list[tuple[int, str]] = []

    for statements in _record_event_statement_lists(tree):
        for index, statement in enumerate(statements):
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "record_event"
            ):
                continue
            assert index > 0
            previous = statements[index - 1]
            assert isinstance(previous, ast.Expr)
            assert isinstance(previous.value, ast.Call)
            assert isinstance(previous.value.func, ast.Attribute)
            assert isinstance(previous.value.func.value, ast.Name)
            assert previous.value.func.value.id == "audit_logger"
            assert previous.value.func.attr == "info"

            event_keyword = next(
                keyword for keyword in statement.value.keywords if keyword.arg == "event"
            )
            assert isinstance(event_keyword.value, ast.Constant)
            observed.append((statement.lineno, event_keyword.value.value))

    assert [event for _line, event in sorted(observed)] == [
        "auth_rejected",
        "admin_tool_call",
        "login_failed",
        "login_success",
    ]


def test_ring_has_no_storage_graph_or_protocol_authority_dependencies():
    source = (ROOT / "src/live_mem/core/audit_ring.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in ("boto", "storage", "graph", "hivemind")
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {"put_object", "delete_object", "commit", "rollback", "bank_write"}
    )
