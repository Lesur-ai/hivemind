# -*- coding: utf-8 -*-
"""P13-1D (#277) — normalized embedding evidence reaches Qdrant intact.

These tests pin the narrow consumer seam: providers return an immutable
``EmbeddingResult``; ingestion must keep that evidence across batches and
refuse drift before asking the vector store to create or mutate anything.
The historical list-returning embedder methods remain compatibility wrappers.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hivemind_inference import EmbeddingResult
from tests.fakes.inference_fakes import apply_graph_memory_baseline_env


@pytest.fixture
def gm_modules(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core.embedder import EmbeddingService
    from mcp_memory.core.ingest_pipeline import _merge_embedding_results

    return EmbeddingService, _merge_embedding_results


def _result(
    *,
    vectors=((1.0, 2.0),),
    configured_model="configured-model",
    resolved_model=None,
    model_evidence="configured_only",
    effective_dimensions=2,
    input_tokens=None,
    total_tokens=None,
) -> EmbeddingResult:
    return EmbeddingResult(
        vectors=vectors,
        configured_model=configured_model,
        resolved_model=resolved_model,
        model_evidence=model_evidence,
        effective_dimensions=effective_dimensions,
        input_tokens=input_tokens,
        total_tokens=total_tokens,
    )


async def test_embedder_result_methods_preserve_the_exact_normalized_record(gm_modules):
    EmbeddingService, _ = gm_modules
    result = _result()
    service = object.__new__(EmbeddingService)
    service._model = "configured-model"
    service._dimensions = 2
    service._embed = AsyncMock(return_value=result)

    assert await service.embed_texts_result(["document"]) is result
    assert await service.embed_query_result("question") is result


async def test_provider_result_is_not_flattened_before_the_public_result_seam(
    gm_modules, monkeypatch
):
    EmbeddingService, _ = gm_modules
    from mcp_memory.core import inference_runtime

    result = _result()

    class Provider:
        request = None

        async def embed(self, request):
            self.request = request
            return result

    provider = Provider()
    runtime = SimpleNamespace(embedding_provider=lambda: provider)
    monkeypatch.setattr(
        inference_runtime,
        "get_inference_runtime",
        lambda: runtime,
    )
    service = object.__new__(EmbeddingService)

    observed = await service._embed(["document"], "document")

    assert observed is result
    assert provider.request.inputs == ("document",)
    assert provider.request.input_type == "document"


async def test_historical_embedder_wrappers_still_return_mutable_vector_lists(gm_modules):
    EmbeddingService, _ = gm_modules
    document_result = _result(vectors=((1.0, 2.0), (3.0, 4.0)))
    query_result = _result(vectors=((5.0, 6.0),))
    service = object.__new__(EmbeddingService)
    service._model = "configured-model"
    service._dimensions = 2
    service.embed_texts_result = AsyncMock(return_value=document_result)
    service.embed_query_result = AsyncMock(return_value=query_result)

    assert await service.embed_texts(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    assert await service.embed_query("question") == [5.0, 6.0]
    assert await service.embed_texts([]) == []
    service.embed_texts_result.assert_awaited_once_with(["a", "b"])


def test_batch_results_are_merged_without_losing_identity_or_usage(gm_modules):
    _, _merge_embedding_results = gm_modules
    first = _result(
        vectors=((1.0, 2.0),),
        input_tokens=2,
        total_tokens=2,
    )
    second = _result(
        vectors=((3.0, 4.0), (5.0, 6.0)),
        input_tokens=3,
        total_tokens=3,
    )

    merged = _merge_embedding_results([first, second])

    assert type(merged) is EmbeddingResult
    assert merged.vectors == ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0))
    assert merged.configured_model == "configured-model"
    assert merged.resolved_model is None
    assert merged.model_evidence == "configured_only"
    assert merged.effective_dimensions == 2
    assert merged.input_tokens == 5
    assert merged.total_tokens == 5


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            _result(configured_model="model-a"),
            _result(configured_model="model-b"),
        ),
        (
            _result(
                resolved_model="resolved-a",
                model_evidence="provider_reported",
            ),
            _result(
                resolved_model="resolved-b",
                model_evidence="provider_reported",
            ),
        ),
        (
            _result(),
            _result(model_evidence="immutable_digest"),
        ),
        (
            _result(),
            _result(vectors=((1.0, 2.0, 3.0),), effective_dimensions=3),
        ),
    ],
    ids=(
        "configured-model",
        "resolved-model",
        "model-evidence",
        "effective-dimensions",
    ),
)
def test_every_batch_identity_drift_fails_value_free(first, second, gm_modules):
    _, _merge_embedding_results = gm_modules
    with pytest.raises(
        RuntimeError,
        match=r"^embedding identity changed between batches$",
    ) as excinfo:
        _merge_embedding_results([first, second])

    rendered = str(excinfo.value)
    for forbidden in ("model-a", "model-b", "resolved-a", "resolved-b"):
        assert forbidden not in rendered


def test_merge_rejects_an_empty_or_noncanonical_result_sequence(gm_modules):
    _, _merge_embedding_results = gm_modules
    with pytest.raises(
        RuntimeError,
        match=r"^embedding provider returned no normalized batch result$",
    ):
        _merge_embedding_results([])

    class Lookalike:
        vectors = ((1.0, 2.0),)
        configured_model = "configured-model"
        resolved_model = None
        model_evidence = "configured_only"
        effective_dimensions = 2

    with pytest.raises(
        RuntimeError,
        match=r"^embedding provider returned an invalid normalized batch result$",
    ):
        _merge_embedding_results([Lookalike()])


def test_ingestion_routes_results_without_precreating_a_collection(gm_modules):
    _, _merge_embedding_results = gm_modules
    from mcp_memory.core import ingest_pipeline

    source = inspect.getsource(ingest_pipeline.run_ingest_pipeline)
    assert "ensure_collection" not in source
    assert ".embed_texts_result(" in source
    assert "_merge_embedding_results(" in source
    assert "embedding_result=embedding_result" in source


@pytest.mark.parametrize(
    ("case", "expected_message"),
    (
        ("identity-drift", "embedding identity changed between batches"),
        (
            "shifted-cardinality",
            "embedding provider returned an invalid batch cardinality",
        ),
    ),
)
async def test_invalid_batch_reaches_no_vector_store_operation(
    gm_modules, monkeypatch, case, expected_message
):
    _, _merge_embedding_results = gm_modules
    from mcp_memory import server
    from mcp_memory.core import ingest_pipeline

    class Graph:
        deleted = False

        async def get_memory(self, memory_id):
            return SimpleNamespace(ontology="general")

        async def add_document(self, **kwargs):
            return None

        async def add_entities_and_relations(self, **kwargs):
            return {}

        async def delete_document(self, memory_id, doc_id):
            self.deleted = True
            return {
                "deleted": True,
                "entities_deleted": 0,
                "relations_deleted": 0,
            }

    class Storage:
        deleted = False

        async def upload_document(self, **kwargs):
            return {
                "uri": "s3://test-bucket/memory/document.txt",
                "size_bytes": len(kwargs["content"]),
            }

        async def delete_document(self, memory_id, uri):
            self.deleted = True
            return True

    class Extractor:
        async def extract_with_ontology_chunked(self, *args, **kwargs):
            return SimpleNamespace(
                entities=[],
                relations=[],
                summary="",
                key_topics=[],
            )

    class Chunker:
        def chunk_document(self, text, filename):
            return [SimpleNamespace(text=f"chunk-{i}") for i in range(6)]

    class Embedder:
        def __init__(self):
            if case == "identity-drift":
                self.results = [
                    _result(
                        vectors=tuple(
                            (float(i), float(i + 1)) for i in range(5)
                        ),
                        configured_model="model-a",
                    ),
                    _result(
                        vectors=((6.0, 7.0),),
                        configured_model="model-b",
                    ),
                ]
            else:
                self.results = [
                    _result(
                        vectors=tuple(
                            (float(i), float(i + 1)) for i in range(4)
                        )
                    ),
                    _result(vectors=((4.0, 5.0), (6.0, 7.0))),
                ]

        async def embed_texts_result(self, texts):
            return self.results.pop(0)

    class VectorStore:
        def __init__(self):
            self.operations = []

        async def ensure_collection(self, memory_id):
            self.operations.append("ensure_collection")

        async def store_chunks(self, **kwargs):
            self.operations.append("store_chunks")
            return len(kwargs["chunks"])

        async def delete_document_chunks(self, memory_id, doc_id):
            self.operations.append("delete_document_chunks")
            return 0

    graph = Graph()
    storage = Storage()
    embedder = Embedder()
    vector_store = VectorStore()
    monkeypatch.setattr(ingest_pipeline, "_graph", lambda: graph)
    monkeypatch.setattr(ingest_pipeline, "_storage", lambda: storage)
    monkeypatch.setattr(ingest_pipeline, "_extractor", lambda: Extractor())
    monkeypatch.setattr(ingest_pipeline, "_chunker", lambda: Chunker())
    monkeypatch.setattr(ingest_pipeline, "_embedder", lambda: embedder)
    monkeypatch.setattr(ingest_pipeline, "_vector_store", lambda: vector_store)
    monkeypatch.setattr(ingest_pipeline, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(server, "_extract_text", lambda content, filename: "text")

    result = await ingest_pipeline.run_ingest_pipeline(
        memory_id="memory",
        content=b"text",
        filename="document.txt",
        doc_hash="sha256",
    )

    assert result["status"] == "error"
    assert expected_message in result["message"]
    assert vector_store.operations == []
    assert graph.deleted is True
    assert storage.deleted is True


async def test_identity_refusal_stops_delete_before_graph_and_storage(
    gm_modules, monkeypatch
):
    _, _ = gm_modules
    from mcp_memory.core import ingest_pipeline
    from mcp_memory.core.vector_store import (
        EmbeddingCollectionReindexRequired,
    )

    class VectorStore:
        async def delete_document_chunks(self, memory_id, doc_id):
            raise EmbeddingCollectionReindexRequired("legacy_nonempty")

    graph = SimpleNamespace(
        get_document=AsyncMock(return_value={"uri": "s3://bucket/document"}),
        delete_document=AsyncMock(),
    )
    storage = SimpleNamespace(delete_document=AsyncMock())
    monkeypatch.setattr(ingest_pipeline, "_vector_store", lambda: VectorStore())
    monkeypatch.setattr(ingest_pipeline, "_graph", lambda: graph)
    monkeypatch.setattr(ingest_pipeline, "_storage", lambda: storage)

    with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
        await ingest_pipeline.delete_document_everywhere("memory", "doc")

    assert excinfo.value.reason == "legacy_nonempty"
    graph.delete_document.assert_not_awaited()
    storage.delete_document.assert_not_awaited()
