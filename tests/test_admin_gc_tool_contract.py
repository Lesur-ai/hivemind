# -*- coding: utf-8 -*-
"""Focused MCP-wrapper contract tests for ``admin_gc_notes``.

The GC service itself is covered separately.  These tests pin the public admin
tool boundary: argument validation, dry-run redaction/token exposure, delete
precondition forwarding, and stable fail-closed route error mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.core.engines import RegistryRefused
from live_mem.core.hivemind import CorruptedStateError, WriteRoute
from live_mem.core.write_sink import StagedWriteNotImplemented
from live_mem.tools.admin import register as register_admin_tools


def _registered_gc_tool():
    mcp = FastMCP(name="test-admin-gc-contract")
    register_admin_tools(mcp)
    tool = mcp._tool_manager._tools["admin_gc_notes"]
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return tool, fn
    raise AssertionError("admin_gc_notes has no callable")


def _gc_mock() -> SimpleNamespace:
    return SimpleNamespace(
        scan_old_notes=AsyncMock(),
        consolidate_old_notes=AsyncMock(),
        delete_old_notes=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _admin_token():
    token = current_token_info.set(
        {
            "client_name": "admin-test",
            "permissions": ["read", "write", "manage", "admin"],
            "allowed_resources": [],
        }
    )
    try:
        yield
    finally:
        current_token_info.reset(token)


def test_gc_schema_pins_non_negative_age_and_snapshot_input() -> None:
    tool, _ = _registered_gc_tool()
    properties = tool.parameters["properties"]

    assert properties["max_age_days"]["minimum"] == 0
    assert "expected_eligible_set_token" in properties


@pytest.mark.asyncio
async def test_gc_negative_age_is_rejected_before_service_call() -> None:
    _, gc_tool = _registered_gc_tool()

    with patch("live_mem.core.gc.get_gc_service") as get_gc_service:
        result = await gc_tool(space_id="alpha", max_age_days=-1)

    assert result == {
        "status": "error",
        "reason": "invalid_max_age_days",
        "message": "max_age_days doit être supérieur ou égal à 0.",
    }
    get_gc_service.assert_not_called()


@pytest.mark.asyncio
async def test_gc_dry_run_keeps_opaque_token_and_strips_exact_keys() -> None:
    _, gc_tool = _registered_gc_tool()
    gc = _gc_mock()
    raw_key = "alpha/live/20260101T000000_agent_observation_deadbeef.md"
    opaque_token = "gc-set-v1:" + "a" * 64
    gc.scan_old_notes.return_value = {
        "status": "ok",
        "max_age_days": 7,
        "cutoff_date": "2026-07-06T00:00:00+00:00",
        "spaces": {
            "alpha": {
                "total_notes": 1,
                "old_notes": 1,
                "old_notes_size": 12,
                "by_agent": {"agent": 1},
                "oldest": "20260101T000000",
                "keys": [raw_key],
            }
        },
        "total_old_notes": 1,
        "total_old_size": 12,
        "eligible_set_token": opaque_token,
    }

    with patch("live_mem.core.gc.get_gc_service", return_value=gc):
        result = await gc_tool(space_id="alpha", max_age_days=7, confirm=False)

    gc.scan_old_notes.assert_awaited_once_with(space_id="alpha", max_age_days=7)
    assert result["status"] == "ok"
    assert result["mode"] == "dry-run"
    assert result["eligible_set_token"] == opaque_token
    assert result["spaces"]["alpha"]["keys_count"] == 1
    assert "keys" not in result["spaces"]["alpha"]
    assert raw_key not in repr(result)


@pytest.mark.asyncio
async def test_gc_delete_forwards_expected_eligible_set_token_verbatim() -> None:
    _, gc_tool = _registered_gc_tool()
    gc = _gc_mock()
    opaque_token = "gc-set-v1:" + "b" * 64
    expected = {"status": "deleted", "action": "delete", "deleted": 2}
    gc.delete_old_notes.return_value = expected

    with patch("live_mem.core.gc.get_gc_service", return_value=gc):
        result = await gc_tool(
            space_id="alpha",
            max_age_days=9,
            confirm=True,
            delete_only=True,
            expected_eligible_set_token=opaque_token,
        )

    assert result is expected
    gc.delete_old_notes.assert_awaited_once_with(
        space_id="alpha",
        max_age_days=9,
        expected_eligible_set_token=opaque_token,
    )
    gc.scan_old_notes.assert_not_awaited()
    gc.consolidate_old_notes.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_factory", "reason"),
    [
        (
            lambda: StagedWriteNotImplemented(
                op="consolidate", key="secret-space/live/"
            ),
            "route_staged_not_implemented",
        ),
        (
            lambda: RegistryRefused("secret-space", WriteRoute.REFUSE),
            "route_refused",
        ),
        (
            lambda: CorruptedStateError("secret corrupted payload"),
            "state_corrupt",
        ),
    ],
)
async def test_gc_known_route_errors_are_stably_mapped_before_safe_error(
    exception_factory, reason: str
) -> None:
    _, gc_tool = _registered_gc_tool()
    gc = _gc_mock()
    gc.consolidate_old_notes.side_effect = exception_factory()

    with patch("live_mem.core.gc.get_gc_service", return_value=gc), patch(
        "live_mem.auth.context.safe_error"
    ) as safe_error:
        result = await gc_tool(space_id="alpha", confirm=True)

    assert result["status"] == "error"
    assert result["reason"] == reason
    assert "secret" not in result["message"]
    safe_error.assert_not_called()
