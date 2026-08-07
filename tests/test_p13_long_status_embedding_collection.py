# -*- coding: utf-8 -*-
"""P13-1D (#277) — safe embedding identity in the unified long status.

The embedded Graph Memory ``memory_stats`` result is an untrusted service
boundary.  These tests drive the real ``GraphBridgeService.status`` reshape and
prove that only the fixed, value-free collection-status vocabulary reaches the
unified Hivemind response.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from live_mem.core.graph_bridge import GraphBridgeService
from mcp_memory.core.vector_store import LEGACY_PREFIX_DIAGNOSTIC
from tests.fakes import FakeGraphTransport


_SPACE_ID = "space-a"
_META_KEY = f"{_SPACE_ID}/_meta.json"
_GRAPH_URL = "https://graph.example.test"
_FINGERPRINT = "a" * 64

_REINDEX_REASONS = (
    "active_alias_invalid",
    "fingerprint_mismatch",
    "invalid_metadata",
    "legacy_nonempty",
    "legacy_unreadable",
    "memory_namespace_mismatch",
    "payload_ownership_mismatch",
    "shadow_invalid",
    "static_profile_mismatch",
    "vector_config_mismatch",
)
_UNAVAILABLE_REASONS = (
    "active_alias_unreadable",
    "canonical_unreadable",
    "embedding_profile_unavailable",
    "qdrant_unreadable",
    "shadow_validation_failed",
)


class _FakeStorage:
    def __init__(self) -> None:
        self._meta = {
            "space_id": _SPACE_ID,
            "version": 1,
            "graph_memory": {
                "url": _GRAPH_URL,
                "token": "test-only-token",
                "memory_id": "memory-a",
                "ontology": "general",
                "binding": "explicit",
            },
        }

    async def get_json(self, key: str):
        if key != _META_KEY:
            return None
        return json.loads(json.dumps(self._meta))


class _DictSubclass(dict):
    """Benign proof that only the exact built-in ``dict`` is accepted."""


def _settings():
    return SimpleNamespace(
        long_embedded_url="http://graph-memory:8002",
        long_embedded_token="unused",
        long_embedded_token_file="/does/not/exist",
    )


async def _long_status(embedding_collection: object, *, include_field: bool = True):
    memory_stats = {
        "status": "ok",
        "document_count": 2,
        "entity_count": 3,
        "relation_count": 4,
        "top_entities": [],
    }
    if include_field:
        memory_stats["embedding_collection"] = embedding_collection

    bridge = GraphBridgeService(
        client_factory=FakeGraphTransport.factory(
            responses={"memory_stats": memory_stats}
        ),
        url_validator=lambda _url, **_kwargs: None,
    )
    with (
        patch(
            "live_mem.core.graph_bridge.get_storage",
            return_value=_FakeStorage(),
        ),
        patch(
            "live_mem.core.graph_bridge.get_settings",
            return_value=_settings(),
        ),
    ):
        return await bridge.status(_SPACE_ID)


@pytest.mark.parametrize(
    "collection_status",
    [
        {"state": "missing"},
        {
            "state": "ready",
            "profile_fingerprint": _FINGERPRINT,
            "points_count": 0,
        },
        {
            "state": "ready",
            "profile_fingerprint": "0123456789abcdef" * 4,
            "points_count": 17,
        },
        *[
            {"state": "reindex_required", "reason": reason}
            for reason in _REINDEX_REASONS
        ],
        *[
            {"state": "unavailable", "reason": reason}
            for reason in _UNAVAILABLE_REASONS
        ],
    ],
)
async def test_long_status_propagates_only_exact_collection_statuses(
    collection_status: dict,
) -> None:
    result = await _long_status(collection_status)

    assert result["status"] == "ok"
    assert result["reachable"] is True
    assert result["graph_stats"] == {
        "document_count": 2,
        "entity_count": 3,
        "relation_count": 4,
    }
    assert result["embedding_collection"] == collection_status
    assert type(result["embedding_collection"]) is dict
    assert result["embedding_collection"] is not collection_status


@pytest.mark.parametrize(
    "invalid_status",
    [
        None,
        [],
        "ready",
        {1: "missing"},
        _DictSubclass(state="missing"),
        {},
        {"state": b"missing"},
        {"state": "unknown"},
        {"state": "missing", "reason": "extra"},
        {"state": "ready"},
        {
            "state": "ready",
            "profile_fingerprint": _FINGERPRINT,
            "points_count": 0,
            "extra": "raw",
        },
        {
            "state": "ready",
            "profile_fingerprint": "A" * 64,
            "points_count": 0,
        },
        {
            "state": "ready",
            "profile_fingerprint": "a" * 63,
            "points_count": 0,
        },
        {
            "state": "ready",
            "profile_fingerprint": b"a" * 64,
            "points_count": 0,
        },
        {
            "state": "ready",
            "profile_fingerprint": _FINGERPRINT,
            "points_count": True,
        },
        {
            "state": "ready",
            "profile_fingerprint": _FINGERPRINT,
            "points_count": -1,
        },
        {
            "state": "ready",
            "profile_fingerprint": _FINGERPRINT,
            "points_count": 1.0,
        },
        {"state": "reindex_required"},
        {"state": "reindex_required", "reason": "not_a_real_reason"},
        {"state": "reindex_required", "reason": b"invalid_metadata"},
        {
            "state": "reindex_required",
            "reason": "invalid_metadata",
            "message": "raw backend text",
        },
        {"state": "unavailable"},
        {"state": "unavailable", "reason": "not_a_real_reason"},
        {"state": "unavailable", "reason": b"qdrant_unreadable"},
        {
            "state": "unavailable",
            "reason": "qdrant_unreadable",
            "message": "raw backend text",
        },
        # Fixed reasons keep their vector-store state classification.
        {"state": "unavailable", "reason": "fingerprint_mismatch"},
        {"state": "reindex_required", "reason": "qdrant_unreadable"},
        # Real vector-store codes outside get_collection_info stay private.
        {"state": "reindex_required", "reason": "dynamic_evidence_mismatch"},
        {"state": "reindex_required", "reason": "backup_point_invalid"},
        {"state": "unavailable", "reason": "collection_race"},
    ],
)
async def test_invalid_collection_status_fails_closed_value_free(
    invalid_status: object,
) -> None:
    result = await _long_status(invalid_status)

    assert result["embedding_collection"] == {
        "state": "unavailable",
        "reason": "invalid_status",
    }
    serialized = json.dumps(result["embedding_collection"])
    assert "not_a_real_reason" not in serialized
    assert "raw backend text" not in serialized


async def test_missing_memory_stats_collection_status_fails_closed() -> None:
    result = await _long_status(None, include_field=False)

    assert result["embedding_collection"] == {
        "state": "unavailable",
        "reason": "invalid_status",
    }


def test_deployment_pins_status_diagnostics() -> None:
    deployment = (
        Path(__file__).resolve().parents[1] / "docs" / "DEPLOYMENT.md"
    ).read_text(encoding="utf-8")

    assert f"```text\n{LEGACY_PREFIX_DIAGNOSTIC}\n```" in deployment
    assert "embedding_profile_unavailable" in deployment
    assert "shadow_validation_failed" in deployment
    assert "invalid_status" in deployment
