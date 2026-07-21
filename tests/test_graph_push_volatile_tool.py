# -*- coding: utf-8 -*-
"""
P4-8 — TOOL-LAYER half of the volatile-file guardrail: the ``manage`` permission
gate + the structured ``graph_push_volatile_optin`` audit live in
``tools/graph.py::graph_push`` (ADR-0010: engine/bridge are verbatim pass-through
with NO auth and NO logging — the gate/audit cannot live below the tool layer).

These cases drive the REAL ``graph_push`` tool function (extracted from a real
FastMCP built by ``tools.graph.register``) with a seeded ``current_token_info``
token, over:

- a patched ``get_engine_registry`` whose ``long_engine()`` returns a REAL
  :class:`LongEngine` wrapping a REAL :class:`GraphBridgeService` wired to a
  deterministic :class:`FakeGraphTransport` (no network / S3 / Neo4j / LLM);
- a patched ``get_storage`` -> in-memory :class:`FakeStorage`.

Pinned here:

9.  ``include_volatile=True`` requires ``manage`` — a write-only token is
    REFUSED (manage error dict, no client built, no _meta mutation); a manage
    token PROCEEDS and force-pushes the volatile files.
10. The ``graph_push_volatile_optin`` audit fires EXACTLY once on an authorized
    opt-in, and NEVER on a default push or a refused opt-in (audit is AFTER the
    gate).
11. The DEFAULT permission surface is unchanged: ``include_volatile=False`` with
    a write-only token still succeeds (no manage required) — back-compat.
"""

from __future__ import annotations

import base64
import json
import logging
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.engines.long_engine import LongEngine
from live_mem.tools import graph as graph_tools
from tests.fakes import FakeGraphTransport


# =============================================================================
# In-memory storage fake — same idiom as the bridge-level suite.
# =============================================================================


class FakeStorage:
    """Minimal in-memory StorageService stand-in. No S3, fully deterministic."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append(
                    {"Key": key, "Size": len(self.objects[key]), "LastModified": ""}
                )
        return out

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        results: list[dict] = []
        for obj in await self.list_objects(prefix):
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": "",
                    }
                )
        return results

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)


# =============================================================================
# Helpers
# =============================================================================

_SPACE = "space-a"
_MEM = "mem-1"
_URL = "https://gm.example.com"


def _meta_connected() -> dict:
    return {
        "space_id": _SPACE,
        "version": 1,
        "graph_memory": {
            "url": _URL,
            "token": "tok-secret",
            "memory_id": _MEM,
            "ontology": "general",
            "last_push": None,
            "push_count": 0,
            "files_pushed": 0,
        },
    }


def _token(name: str, permissions: list[str]) -> dict:
    """A token with no space restriction (allowed_resources empty for admin;
    here we just include _SPACE explicitly so check_access passes for non-admin
    write/manage tokens)."""
    return {
        "client_name": name,
        "permissions": permissions,
        "allowed_resources": [_SPACE],
        "token_hash": "sha256:" + (name[:1] * 64)[:64],
    }


def _graph_push_fn():
    """Extract the real ``graph_push`` tool function from a freshly-registered
    FastMCP (the tool body carries the manage gate + audit emit)."""
    mcp = FastMCP(name="test-graph-push")
    graph_tools.register(mcp)
    return mcp._tool_manager._tools["graph_push"].fn


def _graph_disconnect_fn():
    """Extract the real graph_disconnect/long_disconnect shared handler."""
    mcp = FastMCP(name="test-graph-disconnect")
    graph_tools.register(mcp)
    return mcp._tool_manager._tools["graph_disconnect"].fn


class _FakeRegistry:
    """Stands in for the EngineRegistry: ``long_engine()`` returns a real
    LongEngine over a real bridge wired to the fake transport factory."""

    def __init__(self, factory) -> None:
        self._engine = LongEngine(bridge=GraphBridgeService(client_factory=factory))

    def long_engine(self) -> LongEngine:
        return self._engine


def _wire(storage: FakeStorage, **factory_kwargs):
    """Patch get_engine_registry (the tool imports it locally from
    live_mem.core.engines) + get_storage (bridge module). Returns the patch
    context manager and the factory so tests can inspect built instances."""
    factory = FakeGraphTransport.factory(**factory_kwargs)
    registry = _FakeRegistry(factory)
    patches = patch.multiple(
        "live_mem.core.engines",
        get_engine_registry=lambda: registry,
    )
    storage_patch = patch(
        "live_mem.core.graph_bridge.get_storage", return_value=storage
    )
    return patches, storage_patch, factory


def _set_token(tok: dict):
    """Set both the contextvar and the fresh-token store (check_* helpers read
    the fresh store first). Returns a reset callable."""
    from live_mem.auth.context import update_fresh_token, invalidate_token_in_store

    ctx_tok = current_token_info.set(tok)
    update_fresh_token(tok)

    def _reset():
        current_token_info.reset(ctx_tok)
        invalidate_token_in_store(tok["token_hash"])

    return _reset


def _ingested_filenames(inst) -> list[str]:
    return [a["filename"] for a in inst.args_for("memory_ingest")]


# =============================================================================
# CASE 9 — include_volatile=True requires manage.
# =============================================================================


async def test_graph_push_volatile_requires_manage() -> None:
    """A write-only token + include_volatile=True is REFUSED with the manage
    error; a manage token PROCEEDS and force-pushes the volatile files."""
    graph_push = _graph_push_fn()

    # ── write-only token: refused, nothing built, no _meta mutation ──────────
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await graph_push(space_id=_SPACE, include_volatile=True)
    finally:
        reset()

    assert result["status"] == "error"
    assert "manage" in result["message"]
    # The refusal happened before any GM client was built.
    assert factory.instances == []
    # _meta was not mutated (no push recorded).
    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    assert meta["graph_memory"]["push_count"] == 0

    # ── manage token: proceeds, volatile files ARE force-pushed ──────────────
    storage2 = FakeStorage()
    await storage2.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage2.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage2.put(f"{_SPACE}/bank/progress.md", "prog")
    await storage2.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches2, storage_patch2, factory2 = _wire(storage2)

    reset2 = _set_token(_token("manager", ["read", "write", "manage"]))
    try:
        with patches2, storage_patch2:
            result2 = await graph_push(space_id=_SPACE, include_volatile=True)
    finally:
        reset2()

    assert result2["status"] == "ok"
    inst = factory2.instances[-1]
    ingested = set(_ingested_filenames(inst))
    assert ingested == {"activeContext.md", "progress.md", "systemPatterns.md"}
    assert result2["pushed"] == 3
    assert result2["skipped_volatile"] == []


# =============================================================================
# CASE 10 — audit fires only on an authorized opt-in.
# =============================================================================


async def test_graph_push_volatile_audit_only_on_authorized_optin(caplog) -> None:
    """Exactly one graph_push_volatile_optin audit line on an authorized opt-in;
    NONE on a default push; NONE on a refused (write-only) opt-in attempt."""
    graph_push = _graph_push_fn()

    def _optin_lines() -> list:
        return [
            r
            for r in caplog.records
            if r.name == "live_mem.audit"
            and "graph_push_volatile_optin" in r.message
        ]

    # ── (a) authorized opt-in -> exactly one audit line ──────────────────────
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/progress.md", "prog")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches, storage_patch, _factory = _wire(storage)

    reset = _set_token(_token("manager", ["read", "write", "manage"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches, storage_patch:
            await graph_push(space_id=_SPACE, include_volatile=True)
    finally:
        reset()

    lines = _optin_lines()
    assert len(lines) == 1
    payload = json.loads(lines[-1].message)
    assert payload["event"] == "graph_push_volatile_optin"
    assert payload["space_id"] == _SPACE
    assert payload["caller"] == "manager"
    assert set(payload["volatile_files"]) == {"activeContext.md", "progress.md"}

    # ── (b) default push -> no audit line ────────────────────────────────────
    caplog.clear()
    storage2 = FakeStorage()
    await storage2.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage2.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches2, storage_patch2, _f2 = _wire(storage2)

    reset2 = _set_token(_token("writer", ["read", "write"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches2, storage_patch2:
            await graph_push(space_id=_SPACE)  # default include_volatile=False
    finally:
        reset2()

    assert _optin_lines() == []

    # ── (c) refused opt-in (write-only) -> no audit line (gate first) ────────
    caplog.clear()
    storage3 = FakeStorage()
    await storage3.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage3.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches3, storage_patch3, _f3 = _wire(storage3)

    reset3 = _set_token(_token("writer", ["read", "write"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches3, storage_patch3:
            res = await graph_push(space_id=_SPACE, include_volatile=True)
    finally:
        reset3()

    assert res["status"] == "error"  # refused
    assert _optin_lines() == []  # audit fires AFTER the gate, so never here


# =============================================================================
# CASE 11 — default permission surface unchanged (back-compat).
# =============================================================================


async def test_graph_push_default_permission_surface_unchanged() -> None:
    """graph_push with include_volatile=False and a WRITE-only token still
    succeeds (no manage required) — pins back-compat of the default surface."""
    graph_push = _graph_push_fn()

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    await storage.put(f"{_SPACE}/bank/activeContext.md", "ctx")
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await graph_push(space_id=_SPACE)  # default
    finally:
        reset()

    assert result["status"] == "ok"
    inst = factory.instances[-1]
    ingested = _ingested_filenames(inst)
    # Volatile skipped, durable pushed — the write-only default path works.
    assert ingested == ["systemPatterns.md"]
    assert result["pushed"] == 1
    assert result["skipped_volatile"] == ["activeContext.md"]


async def test_graph_disconnect_use_embedded_requires_manage_and_forwards_flag() -> None:
    """The new maintenance branch is stronger than legacy disconnect.

    A write token can still disconnect normally, but cannot replace a legacy
    override with the embedded runtime.  A manage token forwards the exact flag
    to LongEngine; no bridge/storage/network is needed for this tool-layer test.
    """
    graph_disconnect = _graph_disconnect_fn()
    calls: list[tuple[str, bool]] = []

    class _RecordingLong:
        async def disconnect(self, space_id: str, *, use_embedded: bool = False):
            calls.append((space_id, use_embedded))
            return {
                "status": "connected" if use_embedded else "disconnected",
                "space_id": space_id,
            }

    class _RecordingRegistry:
        def long_engine(self):
            return _RecordingLong()

    registry_patch = patch(
        "live_mem.core.engines.get_engine_registry",
        return_value=_RecordingRegistry(),
    )

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with registry_patch:
            denied = await graph_disconnect(
                space_id=_SPACE, use_embedded=True
            )
    finally:
        reset()

    assert denied["status"] == "error"
    assert "manage" in denied["message"]
    assert calls == []

    reset2 = _set_token(_token("writer", ["read", "write"]))
    try:
        with registry_patch:
            legacy = await graph_disconnect(space_id=_SPACE)
    finally:
        reset2()

    assert legacy["status"] == "disconnected"
    assert calls == [(_SPACE, False)]

    reset3 = _set_token(_token("manager", ["read", "write", "manage"]))
    try:
        with registry_patch:
            migrated = await graph_disconnect(
                space_id=_SPACE, use_embedded=True
            )
    finally:
        reset3()

    assert migrated["status"] == "connected"
    assert calls == [(_SPACE, False), (_SPACE, True)]
