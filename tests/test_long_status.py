# -*- coding: utf-8 -*-
"""
P4-6 (EPIC #6) — long_status observability surface (protocol-derived, read-only).

Extends the long status surface to report, alongside the existing connection /
stats / documents, the P4-5 derived watermark and an EXPLICIT
"protocol-derived, not authoritative" marker (ADR-0010). Read-only: status never
writes, never leaks the token, and a disconnected space returns a clean
"not connected", not a crash.

Drives the REAL GraphBridgeService through the P4-4 client seam
(client_factory=FakeGraphTransport.factory(...)) over an in-memory FakeStorage.
No network / S3 / Neo4j / Qdrant / LLM.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from live_mem.core.engines.long_engine import LongEngine
from live_mem.core.graph_bridge import GraphBridgeService
from tests.fakes import FakeGraphTransport

_SPACE = "space-a"
_META = f"{_SPACE}/_meta.json"
_URL = "https://gm.example.com"

_OK_STATS = {"memory_stats": {"status": "ok", "document_count": 3,
                              "entity_count": 10, "relation_count": 4},
             "document_list": {"status": "ok", "documents": []}}


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str):
        return self.objects.get(key)

    async def get_json(self, key: str):
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        return []

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)


def _meta_connected(extra_gm: dict | None = None) -> dict:
    gm = {
        "url": _URL, "token": "tok-secret", "memory_id": "mem-1",
        "ontology": "general", "last_push": None, "push_count": 2, "files_pushed": 5,
    }
    if extra_gm:
        gm.update(extra_gm)
    return {"space_id": _SPACE, "version": 1, "graph_memory": gm}


def _build(**factory_kwargs):
    factory = FakeGraphTransport.factory(**factory_kwargs)
    return LongEngine(bridge=GraphBridgeService(client_factory=factory)), factory


def _patch(storage: FakeStorage):
    return patch("live_mem.core.graph_bridge.get_storage", return_value=storage)


async def test_status_includes_protocol_derived_marker() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, _meta_connected())
    engine, _ = _build(responses=_OK_STATS)
    with _patch(storage):
        r = await engine.status(_SPACE)
    assert r["connected"] is True
    marker = r["long_authority"]
    assert marker["derived"] is True
    assert marker["authoritative"] is False
    assert "not authoritative" in marker["authority_note"].lower()
    assert "watermark" in r


async def test_status_surfaces_recorded_watermark() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, _meta_connected({
        "bank_version": 7, "commit_id": "c-7", "term": 3,
        "provenance": "mid-consolidation",
        "recorded_at": "2026-06-18T12:00:00+00:00", "flagged": False,
    }))
    engine, _ = _build(responses=_OK_STATS)
    with _patch(storage):
        r = await engine.status(_SPACE)
    wm = r["watermark"]
    assert wm["bank_version"] == 7
    assert wm["commit_id"] == "c-7"
    assert wm["term"] == 3
    assert wm["provenance"] == "mid-consolidation"
    assert wm["recorded_at"] == "2026-06-18T12:00:00+00:00"
    assert wm["flagged"] is False


async def test_status_watermark_absent_is_null_not_fabricated() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, _meta_connected())  # never pushed -> no coords
    engine, _ = _build(responses=_OK_STATS)
    with _patch(storage):
        r = await engine.status(_SPACE)
    wm = r["watermark"]
    assert wm["bank_version"] is None
    assert wm["commit_id"] is None
    assert wm["flagged"] is False


async def test_status_never_leaks_token() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, _meta_connected())
    engine, _ = _build(responses=_OK_STATS)
    with _patch(storage):
        r = await engine.status(_SPACE)
    assert "tok-secret" not in json.dumps(r)


async def test_disconnected_space_clean_not_connected() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, {"space_id": _SPACE, "version": 1})  # no graph_memory
    engine, _ = _build()
    with _patch(storage):
        r = await engine.status(_SPACE)
    assert r["status"] == "ok"
    assert r["connected"] is False
    assert r["long_authority"]["authoritative"] is False
    assert "watermark" not in r  # not connected -> nothing to derive


async def test_status_performs_no_writes() -> None:
    storage = FakeStorage()
    await storage.put_json(_META, _meta_connected())
    engine, _ = _build(responses=_OK_STATS)
    before = storage.snapshot()
    with _patch(storage):
        await engine.status(_SPACE)
    assert storage.snapshot() == before  # read-only surface
