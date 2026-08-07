# -*- coding: utf-8 -*-
"""Focused contract tests for the bounded unified reindex façade."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import MonoTenantSpaceAllowlistProvider
from live_mem.core.engines.long_engine import LongEngine
from live_mem.core.graph_bridge import GraphBridgeService, _reindex_result_view
from live_mem.core.memory_id import derive_memory_id
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL
from live_mem.tools import graph as graph_tools
from live_mem.tools.aliases import ALIAS_MAP
from live_mem.tools.exposure import (
    DISCOVERY_NAMES_BY_PERMISSION,
    TOOL_EXPOSURES,
    ToolAudience,
    ToolOperation,
    ToolPermission,
)
from tests.fakes import FakeGraphTransport, GraphLongFakeStorage


_SPACE_ID = "space-a"
_MEMORY_ID = derive_memory_id(_SPACE_ID)
_EMBEDDED_URL = "http://graph-memory:8002"
_META_KEY = f"{_SPACE_ID}/_meta.json"


def _tool_and_registration_count():
    mcp = FastMCP(name="test-reindex-tool")
    count = graph_tools.register(mcp)
    return mcp._tool_manager._tools["long_reindex"], count


def _embedded_meta() -> dict:
    return {
        "space_id": _SPACE_ID,
        "version": 1,
        "graph_memory": {
            "binding": "embedded",
            "url": _EMBEDDED_URL,
            "token": EMBEDDED_TOKEN_SENTINEL,
            "memory_id": _MEMORY_ID,
            "ontology": "general",
        },
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        long_embedded_url=_EMBEDDED_URL,
        long_embedded_token="unused-by-patched-resolver",
        long_embedded_token_file="/does/not/exist",
    )


def _error_result(reason: str) -> dict:
    return {
        "status": "error",
        "phase": "admission",
        "reason": reason,
        "operation_id": None,
        "source_documents": 0,
        "source_chunks": 0,
        "vectors_written": 0,
        "activated": False,
        "active_state": "unavailable",
    }


def _uncertain_result(reason: str) -> dict:
    result = _error_result(reason)
    result.update({"phase": "activated", "activated": True})
    return result


def test_long_reindex_is_direct_hidden_non_idempotent_operator_tool() -> None:
    tool, count = _tool_and_registration_count()

    assert count == 7
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
    assert "long_reindex" not in ALIAS_MAP
    assert "long_reindex" not in ALIAS_MAP.values()

    exposure = next(
        entry for entry in TOOL_EXPOSURES if entry.canonical_name == "long_reindex"
    )
    assert exposure.audience is ToolAudience.OPERATOR
    assert exposure.minimum_permission is ToolPermission.MANAGE
    assert exposure.operation is ToolOperation.MUTATION
    assert exposure.space_scope_argument == "space_id"
    assert all(
        "long_reindex" not in names
        for names in DISCOVERY_NAMES_BY_PERMISSION.values()
    )
    assert len(DISCOVERY_NAMES_BY_PERMISSION[ToolPermission.ADMIN]) == 24
    assert "long_reindex" in MonoTenantSpaceAllowlistProvider.ALLOWED_ACTIONS


async def test_tool_checks_access_then_manage_before_engine_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _tool_and_registration_count()
    events: list[str] = []

    class _Engine:
        async def reindex(self, space_id: str) -> dict:
            events.append(f"reindex:{space_id}")
            return {"status": "ok", "phase": "verified"}

    class _Registry:
        def long_engine(self) -> _Engine:
            events.append("engine")
            return _Engine()

    def _check_access(space_id: str):
        events.append(f"access:{space_id}")
        return None

    def _check_manage_permission():
        events.append("manage")
        return None

    monkeypatch.setattr("live_mem.auth.context.check_access", _check_access)
    monkeypatch.setattr(
        "live_mem.auth.context.check_manage_permission", _check_manage_permission
    )
    monkeypatch.setattr(
        "live_mem.core.engines.get_engine_registry", lambda: _Registry()
    )

    result = await tool.fn(space_id=_SPACE_ID)

    assert result == {"status": "ok", "phase": "verified"}
    assert events == [
        f"access:{_SPACE_ID}",
        "manage",
        "engine",
        f"reindex:{_SPACE_ID}",
    ]


@pytest.mark.parametrize("denied_gate", ["access", "manage"])
async def test_denied_tool_gate_prevents_engine_resolution(
    monkeypatch: pytest.MonkeyPatch,
    denied_gate: str,
) -> None:
    tool, _ = _tool_and_registration_count()
    events: list[str] = []
    denied = {"status": "error", "message": f"{denied_gate} denied"}

    def _check_access(space_id: str):
        events.append(f"access:{space_id}")
        return denied if denied_gate == "access" else None

    def _check_manage_permission():
        events.append("manage")
        return denied if denied_gate == "manage" else None

    def _unexpected_registry():
        raise AssertionError("engine registry resolved before authorization")

    monkeypatch.setattr("live_mem.auth.context.check_access", _check_access)
    monkeypatch.setattr(
        "live_mem.auth.context.check_manage_permission", _check_manage_permission
    )
    monkeypatch.setattr(
        "live_mem.core.engines.get_engine_registry", _unexpected_registry
    )

    result = await tool.fn(space_id=_SPACE_ID)

    assert result == denied
    assert events == (
        [f"access:{_SPACE_ID}"]
        if denied_gate == "access"
        else [f"access:{_SPACE_ID}", "manage"]
    )


@pytest.mark.parametrize("failure_stage", ["registry", "engine", "dispatch"])
async def test_authorized_tool_collapses_engine_failures_to_exact_envelope(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    tool, _ = _tool_and_registration_count()
    events: list[str] = []
    secret = "https://secret.internal token=do-not-leak"

    class _Engine:
        async def reindex(self, space_id: str) -> dict:
            events.append(f"dispatch:{space_id}")
            raise RuntimeError(secret)

    class _Registry:
        def long_engine(self) -> _Engine:
            events.append("engine")
            if failure_stage == "engine":
                raise RuntimeError(secret)
            return _Engine()

    def _get_registry() -> _Registry:
        events.append("registry")
        if failure_stage == "registry":
            raise RuntimeError(secret)
        return _Registry()

    def _check_access(space_id: str):
        events.append(f"access:{space_id}")
        return None

    def _check_manage_permission():
        events.append("manage")
        return None

    monkeypatch.setattr("live_mem.auth.context.check_access", _check_access)
    monkeypatch.setattr(
        "live_mem.auth.context.check_manage_permission", _check_manage_permission
    )
    monkeypatch.setattr("live_mem.core.engines.get_engine_registry", _get_registry)

    result = await tool.fn(space_id=_SPACE_ID)

    assert result == _error_result("reindex_failed")
    assert events[:2] == [f"access:{_SPACE_ID}", "manage"]
    serialized = json.dumps(result) + caplog.text
    assert "secret.internal" not in serialized
    assert "do-not-leak" not in serialized


async def test_long_engine_reindex_is_a_verbatim_pass_through() -> None:
    expected = {"status": "ok", "phase": "verified", "activated": True}
    bridge = SimpleNamespace(reindex=AsyncMock(return_value=expected))
    engine = LongEngine(bridge=bridge)

    result = await engine.reindex(_SPACE_ID)

    assert result is expected
    bridge.reindex.assert_awaited_once_with(_SPACE_ID)


@pytest.mark.parametrize(
    "graph_memory",
    [
        None,
        {
            "binding": "explicit",
            "url": "https://custom.example.test",
            "token": "operator-token",
            "memory_id": _MEMORY_ID,
            "ontology": "general",
        },
        {
            "url": _EMBEDDED_URL,
            "token": EMBEDDED_TOKEN_SENTINEL,
            "memory_id": _MEMORY_ID,
            "ontology": "general",
        },
        {
            "binding": "embedded",
            "url": _EMBEDDED_URL,
            "token": "raw-token-must-not-be-persisted",
            "memory_id": _MEMORY_ID,
            "ontology": "general",
        },
    ],
    ids=(
        "missing",
        "explicit-graph-connect",
        "legacy-unclassified",
        "embedded-with-raw-token",
    ),
)
async def test_bridge_rejects_every_non_explicitly_embedded_binding_without_client(
    graph_memory: dict | None,
) -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(
        _META_KEY,
        {"space_id": _SPACE_ID, "version": 1, "graph_memory": graph_memory},
    )
    factory = FakeGraphTransport.factory()

    def _unexpected_url_validation(*_args, **_kwargs):
        raise AssertionError("unsupported runtime reached URL validation")

    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=_unexpected_url_validation,
    )
    with patch("live_mem.core.graph_bridge.get_storage", return_value=storage):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _error_result("unsupported_runtime")
    assert factory.instances == []


async def test_bridge_normalizes_missing_space_without_client() -> None:
    storage = GraphLongFakeStorage()
    factory = FakeGraphTransport.factory()
    bridge = GraphBridgeService(client_factory=factory)

    with patch("live_mem.core.graph_bridge.get_storage", return_value=storage):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _error_result("space_not_found")
    assert factory.instances == []


async def test_bridge_normalizes_binding_resolution_error_without_reflection() -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())
    factory = FakeGraphTransport.factory()
    bridge = GraphBridgeService(client_factory=factory)
    leaked = {
        "status": "error",
        "message": "https://secret.internal token=do-not-leak",
        "long_authority": "unavailable",
    }

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch.object(
            bridge,
            "_resolve_or_embedded",
            new=AsyncMock(return_value=(None, None, leaked)),
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _error_result("binding_unavailable")
    assert "secret.internal" not in json.dumps(result)
    assert "do-not-leak" not in json.dumps(result)
    assert factory.instances == []


async def test_bridge_calls_internal_memory_reindex_exactly_once_for_embedded_binding() -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())
    expected = {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": "a" * 32,
        "source_documents": 2,
        "source_chunks": 3,
        "vectors_written": 3,
        "activated": True,
        "active_state": "ready",
    }
    factory = FakeGraphTransport.factory(
        responses={"memory_reindex": expected}
    )
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == expected
    assert len(factory.instances) == 1
    client = factory.instances[0]
    assert client.token == "internal-read-write-token"
    assert client.timeout == 7 * 24 * 60 * 60
    assert client.tool_names() == ["memory_reindex"]
    assert client.args_for("memory_reindex") == [{"memory_id": _MEMORY_ID}]


async def test_bridge_rejects_non_exact_backend_result_without_reflection() -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())
    factory = FakeGraphTransport.factory(
        responses={
            "memory_reindex": {
                "status": "ok",
                "phase": "verified",
                "endpoint": "https://secret.internal/token=do-not-leak",
            }
        }
    )
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _uncertain_result("invalid_result")
    assert "secret.internal" not in json.dumps(result)
    assert "do-not-leak" not in json.dumps(result)


@pytest.mark.parametrize(
    "counts",
    [
        (10_001, 0, 0),
        (1, 250_001, 0),
        (1, 1, 2),
    ],
    ids=("documents-over-cap", "chunks-over-cap", "writes-over-chunks"),
)
def test_bridge_rejects_out_of_contract_result_counts(
    counts: tuple[int, int, int],
) -> None:
    raw = _error_result("backend_unavailable")
    raw.update(
        {
            "phase": "rebuild",
            "operation_id": "b" * 32,
            "source_documents": counts[0],
            "source_chunks": counts[1],
            "vectors_written": counts[2],
        }
    )

    assert _reindex_result_view(raw) == _uncertain_result("invalid_result")


def test_bridge_rejects_verified_result_with_fewer_chunks_than_documents() -> None:
    raw = {
        "status": "ok",
        "phase": "verified",
        "reason": None,
        "operation_id": "b" * 32,
        "source_documents": 2,
        "source_chunks": 1,
        "vectors_written": 1,
        "activated": True,
        "active_state": "ready",
    }

    assert _reindex_result_view(raw) == _uncertain_result("invalid_result")


@pytest.mark.parametrize(
    "contradiction",
    [
        {"phase": "activated", "activated": False, "active_state": "unavailable"},
        {"phase": "activated", "activated": True, "active_state": "ready"},
        {
            "phase": "pre_switch",
            "activated": False,
            "active_state": "reindex_required",
        },
        {
            "reason": "backend_unavailable",
            "phase": "activated",
            "activated": True,
            "active_state": "unavailable",
        },
    ],
    ids=(
        "activated-phase-with-false-flag",
        "activated-error-claims-ready",
        "post-switch-reason-before-activation",
        "pre-switch-reason-after-activation",
    ),
)
async def test_bridge_rejects_contradictory_post_switch_result(
    contradiction: dict,
) -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())
    raw = _error_result("post_switch_unverified")
    raw["operation_id"] = "b" * 32
    raw.update(contradiction)
    factory = FakeGraphTransport.factory(responses={"memory_reindex": raw})
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _uncertain_result("invalid_result")


@pytest.mark.parametrize(
    "reason",
    ["activation_unverified", "post_switch_unverified"],
)
async def test_bridge_accepts_only_declared_post_switch_reasons_as_activated(
    reason: str,
) -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())
    raw = _uncertain_result(reason)
    raw["operation_id"] = "b" * 32
    factory = FakeGraphTransport.factory(responses={"memory_reindex": raw})
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == raw


async def test_bridge_collapses_storage_exception_without_logging_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingStorage:
        async def get_json(self, _key: str):
            raise RuntimeError("https://secret.internal token=do-not-leak")

    bridge = GraphBridgeService()
    with patch(
        "live_mem.core.graph_bridge.get_storage",
        return_value=_FailingStorage(),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _error_result("reindex_failed")
    serialized = json.dumps(result) + caplog.text
    assert "secret.internal" not in serialized
    assert "do-not-leak" not in serialized


async def test_bridge_rechecks_binding_before_constructing_client() -> None:
    embedded = _embedded_meta()
    explicit = json.loads(json.dumps(embedded))
    explicit["graph_memory"].update(
        {
            "binding": "explicit",
            "url": "https://custom.example.test",
            "token": "operator-token",
        }
    )

    class _RacingStorage:
        def __init__(self) -> None:
            self.reads = 0

        async def get_json(self, key: str) -> dict:
            assert key == _META_KEY
            self.reads += 1
            source = embedded if self.reads == 1 else explicit
            return json.loads(json.dumps(source))

    storage = _RacingStorage()
    factory = FakeGraphTransport.factory()
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_runtime"
    assert storage.reads == 2
    assert factory.instances == []


async def test_bridge_rejects_corrupt_embedded_memory_namespace() -> None:
    storage = GraphLongFakeStorage()
    meta = _embedded_meta()
    meta["graph_memory"]["memory_id"] = "wrong-memory"
    await storage.put_json(_META_KEY, meta)
    factory = FakeGraphTransport.factory()
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )

    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_runtime"
    assert factory.instances == []


@pytest.mark.parametrize(
    ("dispatch_error", "reason"),
    [
        (ConnectionError("transport failed"), "runtime_unavailable"),
        (RuntimeError("dispatch failed"), "reindex_failed"),
    ],
)
async def test_bridge_collapses_post_dispatch_details_as_retry_unsafe(
    dispatch_error: Exception,
    reason: str,
) -> None:
    storage = GraphLongFakeStorage()
    await storage.put_json(_META_KEY, _embedded_meta())

    class _FailingClient:
        async def call_tool(self, tool_name: str, arguments: dict) -> dict:
            assert tool_name == "memory_reindex"
            assert arguments == {"memory_id": _MEMORY_ID}
            raise dispatch_error from RuntimeError(
                "https://secret.internal token=do-not-leak"
            )

    bridge = GraphBridgeService(
        client_factory=lambda *_args, **_kwargs: _FailingClient(),
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
        patch(
            "live_mem.core.graph_bridge.resolve_embedded_token",
            return_value="internal-read-write-token",
        ),
    ):
        result = await bridge.reindex(_SPACE_ID)

    assert result == _uncertain_result(reason)
    serialized = json.dumps(result)
    assert "secret.internal" not in serialized
    assert "do-not-leak" not in serialized
