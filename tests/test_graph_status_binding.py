# -*- coding: utf-8 -*-
"""P8-3/G4 — additive binding classification on bound graph_status views."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL
from tests.fakes import FakeGraphTransport


SPACE_ID = "space-a"
META_KEY = f"{SPACE_ID}/_meta.json"
EMBEDDED_URL = "http://graph-memory:8002"


class FakeStorage:
    def __init__(self, meta: dict) -> None:
        self.objects = {META_KEY: json.dumps(meta)}

    async def get_json(self, key: str):
        raw = self.objects.get(key)
        return json.loads(raw) if raw is not None else None


def _settings():
    return SimpleNamespace(
        long_embedded_url=EMBEDDED_URL,
        long_embedded_token="live-embedded-secret",
        long_embedded_token_file="/does/not/exist",
    )


def _graph_block(binding: str) -> dict:
    return {
        "url": EMBEDDED_URL if binding == "embedded" else "https://gm.example.com",
        "token": EMBEDDED_TOKEN_SENTINEL if binding == "embedded" else "explicit-secret",
        "memory_id": "mem-a",
        "ontology": "general",
        "binding": binding,
    }


async def _status(
    binding: str,
    memory_stats,
    *,
    memory_graph: dict | None = None,
    include_graph: bool = False,
) -> dict:
    storage = FakeStorage(
        {
            "space_id": SPACE_ID,
            "version": 1,
            "graph_memory": _graph_block(binding),
        }
    )
    responses = {"memory_stats": memory_stats}
    if memory_graph is not None:
        responses["memory_graph"] = memory_graph
    factory = FakeGraphTransport.factory(responses=responses)
    bridge = GraphBridgeService(
        client_factory=factory,
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
    ):
        return await bridge.status(SPACE_ID, include_graph=include_graph)


@pytest.mark.parametrize("binding", ["embedded", "explicit"])
async def test_reachable_status_exposes_persisted_binding(binding: str) -> None:
    result = await _status(
        binding,
        {
            "status": "ok",
            "document_count": 2,
            "entity_count": 7,
            "relation_count": 4,
        },
    )

    assert result["reachable"] is True
    assert result["binding"] == binding


@pytest.mark.parametrize("binding", ["embedded", "explicit"])
async def test_connection_error_status_exposes_persisted_binding(binding: str) -> None:
    def raise_connection_error(_arguments):
        raise ConnectionError("runtime unavailable")

    result = await _status(binding, raise_connection_error)

    assert result["reachable"] is False
    assert result["binding"] == binding
    assert "last_push" in result


@pytest.mark.parametrize("binding", ["embedded", "explicit"])
async def test_generic_error_status_exposes_persisted_binding(binding: str) -> None:
    def raise_generic_error(_arguments):
        raise RuntimeError("malformed runtime response")

    result = await _status(binding, raise_generic_error)

    assert result["reachable"] is False
    assert result["binding"] == binding
    assert "last_push" not in result


@pytest.mark.parametrize("embedded_runtime", [True, False])
async def test_unbound_status_does_not_invent_binding(embedded_runtime: bool) -> None:
    storage = FakeStorage({"space_id": SPACE_ID, "version": 1})
    settings = _settings()
    if not embedded_runtime:
        settings.long_embedded_url = ""
    bridge = GraphBridgeService(
        client_factory=FakeGraphTransport.factory(),
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=settings),
    ):
        result = await bridge.status(SPACE_ID)

    assert result["connected"] is False
    assert "binding" not in result


async def test_legacy_embedded_sentinel_is_classified_without_url_inference() -> None:
    storage = FakeStorage(
        {
            "space_id": SPACE_ID,
            "version": 1,
            "graph_memory": {
                **_graph_block("embedded"),
                "binding": None,
            },
        }
    )
    bridge = GraphBridgeService(
        client_factory=FakeGraphTransport.factory(),
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=_settings()),
    ):
        result = await bridge.status(SPACE_ID)

    assert result["binding"] == "embedded"


async def test_graph_preview_is_synthetic_whitelisted_and_secret_free() -> None:
    raw = {
        "status": "ok",
        "nodes": [
            {
                "id": "Secret entity id",
                "label": "Visible entity",
                "type": "Service",
                "description": "Safe description",
                "mentions": 7,
                "source_docs": ["s3://private/source.md"],
                "node_type": "entity",
            },
            {
                "id": "doc:database-secret-id",
                "label": "Document",
                "type": "Document",
                "description": "URI: s3://private/bucket/file.md",
                "uri": "s3://private/bucket/file.md",
                "hash": "super-secret-hash",
                "source_path": "/private/repository/file.md",
                "filename": "file.md",
                "node_type": "document",
            },
        ],
        "edges": [
            {
                "from": "doc:database-secret-id",
                "to": "Secret entity id",
                "type": "MENTIONS",
                "description": "edge-secret-source-path",
                "weight": 1,
            }
        ],
        "documents": [
            {
                "uri": "s3://private/bucket/file.md",
                "hash": "super-secret-hash",
                "source_path": "/private/repository/file.md",
            }
        ],
    }
    result = await _status(
        "embedded",
        {"status": "ok"},
        memory_graph=raw,
        include_graph=True,
    )

    preview = result["graph_view"]
    assert preview["status"] == "ok"
    assert [node["id"] for node in preview["nodes"]] == ["n1", "n2"]
    assert preview["edges"][0]["from"] == "n2"
    assert preview["edges"][0]["to"] == "n1"
    document = next(node for node in preview["nodes"] if node["node_type"] == "document")
    assert document["description"] == ""
    serialized = json.dumps(preview)
    for secret in (
        "s3://private",
        "super-secret-hash",
        "/private/repository",
        "database-secret-id",
        "Secret entity id",
        "source_docs",
        '"documents"',
        "edge-secret-source-path",
    ):
        assert secret not in serialized


async def test_graph_preview_is_bounded() -> None:
    nodes = [
        {
            "id": f"node-{index}",
            "label": f"Node {index}",
            "node_type": "entity",
            "mentions": index,
        }
        for index in range(220)
    ]
    edges = [
        {"from": f"node-{index % 220}", "to": f"node-{(index + 1) % 220}"}
        for index in range(500)
    ]
    result = await _status(
        "explicit",
        {"status": "ok"},
        memory_graph={"status": "ok", "nodes": nodes, "edges": edges},
        include_graph=True,
    )
    preview = result["graph_view"]
    assert preview["node_count"] <= 160
    assert preview["edge_count"] <= 320
    assert preview["total_node_count"] == 220
    assert preview["total_edge_count"] == 500
    assert preview["truncated"] is True
