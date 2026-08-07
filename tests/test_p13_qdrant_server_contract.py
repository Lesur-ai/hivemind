# -*- coding: utf-8 -*-
"""#277 locks for Graph Memory's operator-facing vector identity seams."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hivemind_inference import EmbeddingResult
from mcp_memory.core.vector_store import (
    EmbeddingCollectionReindexRequired,
    EmbeddingCollectionUnavailable,
)
from tests.fakes.inference_fakes import apply_graph_memory_baseline_env


def _query_result() -> EmbeddingResult:
    return EmbeddingResult(
        vectors=((1.0, 0.0, 0.0),),
        configured_model="configured-model",
        resolved_model="resolved-model",
        model_evidence="provider_reported",
        effective_dimensions=3,
    )


def _server(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory import server

    return server


class _Graph:
    def __init__(self, *, with_entity: bool = False):
        self.with_entity = with_entity

    async def search_entities(self, memory_id, search_query, limit):
        if self.with_entity:
            return [{"name": "Known entity", "type": "Concept"}]
        return []

    async def get_entity_context(self, memory_id, entity_name, depth):
        return SimpleNamespace(
            documents=[],
            relations=[],
            related_entities=[],
        )


class _Embedder:
    def __init__(self, result: EmbeddingResult):
        self.result = result
        self.queries: list[str] = []

    async def embed_query_result(self, query: str) -> EmbeddingResult:
        self.queries.append(query)
        return self.result

    async def embed_query(self, query: str):
        raise AssertionError("the legacy vector-only wrapper was called")


class _VectorStore:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.search_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return []


class _Extractor:
    def __init__(self):
        self.prompts: list[str] = []

    async def generate_answer(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "graph-only answer"


def _wire_search(
    monkeypatch,
    server,
    *,
    failure: Exception | None = None,
    graph_entity: bool = False,
):
    result = _query_result()
    embedder = _Embedder(result)
    vector_store = _VectorStore(failure=failure)
    extractor = _Extractor()
    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(
        server,
        "get_graph",
        lambda: _Graph(with_entity=graph_entity),
    )
    monkeypatch.setattr(server, "get_embedder", lambda: embedder)
    monkeypatch.setattr(server, "get_vector_store", lambda: vector_store)
    monkeypatch.setattr(server, "get_extractor", lambda: extractor)
    return result, embedder, vector_store, extractor


@pytest.mark.parametrize(
    ("tool_name", "query_field"),
    (
        ("question_answer", "question"),
        ("memory_query", "query"),
    ),
)
async def test_search_paths_preserve_the_exact_embedding_result(
    monkeypatch, tool_name, query_field
):
    server = _server(monkeypatch)
    result, embedder, vector_store, _extractor = _wire_search(
        monkeypatch,
        server,
    )
    query = "Which evidence applies?"

    response = await getattr(server, tool_name)(
        memory_id="memory-one",
        **{query_field: query},
    )

    assert response["status"] == "ok"
    assert embedder.queries == [query]
    assert len(vector_store.search_calls) == 1
    search_call = vector_store.search_calls[0]
    assert search_call["embedding_result"] is result
    assert "query_embedding" not in search_call


@pytest.mark.parametrize(
    ("failure_type", "reason", "expected"),
    (
        (
            EmbeddingCollectionReindexRequired,
            "legacy_nonempty",
            {"state": "reindex_required", "reason": "legacy_nonempty"},
        ),
        (
            EmbeddingCollectionUnavailable,
            "canonical_unreadable",
            {"state": "unavailable", "reason": "canonical_unreadable"},
        ),
        (
            EmbeddingCollectionUnavailable,
            "embedding_profile_unavailable",
            {
                "state": "unavailable",
                "reason": "embedding_profile_unavailable",
            },
        ),
    ),
)
@pytest.mark.parametrize(
    ("tool_name", "query_field"),
    (
        ("question_answer", "question"),
        ("memory_query", "query"),
    ),
)
async def test_collection_failures_are_not_masked_by_graph_only_fallback(
    monkeypatch, failure_type, reason, expected, tool_name, query_field
):
    server = _server(monkeypatch)
    _result, _embedder, vector_store, extractor = _wire_search(
        monkeypatch,
        server,
        failure=failure_type(reason),
        graph_entity=True,
    )

    response = await getattr(server, tool_name)(
        memory_id="memory-one",
        **{query_field: "question"},
    )

    assert response == {
        "status": "error",
        "embedding_collection": expected,
    }
    assert len(vector_store.search_calls) == 1
    assert extractor.prompts == []
    assert "message" not in response


async def test_memory_stats_includes_only_the_safe_collection_contract(monkeypatch):
    server = _server(monkeypatch)
    safe_collection = {
        "state": "reindex_required",
        "reason": "legacy_nonempty",
    }

    class StatsGraph:
        async def get_memory_stats(self, memory_id):
            return SimpleNamespace(
                document_count=3,
                entity_count=5,
                relation_count=8,
                top_entities=[{"name": "safe"}],
            )

    class StatsVectorStore:
        async def get_collection_info(self, memory_id):
            assert memory_id == "memory-one"
            return safe_collection

    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(server, "get_graph", lambda: StatsGraph())
    monkeypatch.setattr(
        server,
        "get_vector_store",
        lambda: StatsVectorStore(),
    )

    response = await server.memory_stats("memory-one")

    assert response == {
        "status": "ok",
        "memory_id": "memory-one",
        "document_count": 3,
        "entity_count": 5,
        "relation_count": 8,
        "top_entities": [{"name": "safe"}],
        "embedding_collection": safe_collection,
    }


async def test_memory_stats_redacts_unexpected_qdrant_failures(monkeypatch):
    server = _server(monkeypatch)

    class StatsGraph:
        async def get_memory_stats(self, memory_id):
            return SimpleNamespace(
                document_count=3,
                entity_count=5,
                relation_count=8,
                top_entities=[],
            )

    class FailingVectorStore:
        async def get_collection_info(self, memory_id):
            raise RuntimeError("https://secret-qdrant.internal:6333")

    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: None)
    monkeypatch.setattr(server, "get_graph", lambda: StatsGraph())
    monkeypatch.setattr(
        server,
        "get_vector_store",
        lambda: FailingVectorStore(),
    )

    response = await server.memory_stats("memory-one")

    assert response["status"] == "ok"
    assert response["embedding_collection"] == {
        "state": "unavailable",
        "reason": "qdrant_unreadable",
    }
    assert "secret-qdrant" not in str(response)
