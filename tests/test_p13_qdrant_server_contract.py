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


@pytest.mark.parametrize(("tool_name", "query_field"), [("memory_query", "query"), ("question_answer", "question")])
@pytest.mark.parametrize("threshold", [None, 0.58])
async def test_low_score_passage_is_kept_unless_cutoff_is_explicit(monkeypatch, tool_name, query_field, threshold):
    server = _server(monkeypatch)
    _result, _embedder, _vector, extractor = _wire_search(monkeypatch, server)
    graph = _RetrievalGraph(entity=False, source="docs/maintenance.md")
    vector = _LabeledPassages(answer_doc="answer-doc")
    original_search = vector.search

    async def low_scores(**kwargs):
        results = await original_search(**kwargs)
        for result in results:
            result.score = 0.39
        return results

    vector.search = low_scores
    monkeypatch.setattr(server, "get_graph", lambda: graph)
    monkeypatch.setattr(server, "get_vector_store", lambda: vector)
    monkeypatch.setattr(server, "settings", SimpleNamespace(rag_score_threshold=threshold, rag_chunk_limit=8))
    response = await getattr(server, tool_name)(memory_id="memory-one", **{query_field: "What is the notice?"})
    assert response["status"] == "ok"
    assert vector.search_calls[0]["doc_ids"] == ["graph-doc", "answer-doc"]
    assert vector.search_calls[0]["limit"] == 8
    assert vector.search_calls[0]["query_text"] == "What is the notice?"
    if tool_name == "memory_query":
        assert len(response["rag_chunks"]) == (2 if threshold is None else 0)
        if threshold is None:
            assert response["rag_chunks"][0]["text"] == "Expected passage: notice is 90 days."
            assert response["rag_chunks"][0]["score"] == 0.39
    else:
        assert response["rag_chunks_used"] == (2 if threshold is None else 0)
        assert bool(extractor.prompts) == (threshold is None)


def test_rag_cutoff_is_disabled_by_default(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    monkeypatch.delenv("RAG_SCORE_THRESHOLD", raising=False)
    from mcp_memory.config import Settings
    assert Settings(_env_file=None).rag_score_threshold is None


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

    async def get_active_doc_ids(self, memory_id):
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
        async def get_collection_info(self, memory_id, *, include_identity=False):
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
        "entity_types": {},
        "top_entities": [{"name": "safe"}],
        "embedding_collection": safe_collection,
        "embedding_identity": None,
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
        async def get_collection_info(self, memory_id, *, include_identity=False):
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


class _RetrievalGraph(_Graph):
    """Graph relevance is deliberately independent from passage relevance."""

    def __init__(self, *, entity: bool, source: str):
        super().__init__(with_entity=entity)
        self.source = source

    async def get_active_doc_ids(self, memory_id):
        assert memory_id == "memory-one"
        return ["graph-doc", "answer-doc"]

    async def get_entity_context(self, memory_id, entity_name, depth):
        return SimpleNamespace(
            documents=[{"id": "graph-doc", "filename": "general.md", "source_path": self.source}],
            relations=[{"type": "RELATED_TO", "description": "Useful graph relationship"}],
            related_entities=[],
        )

    async def get_documents_meta(self, memory_id, doc_ids):
        return {doc_id: {"source_path": self.source} for doc_id in doc_ids}


class _LabeledPassages(_VectorStore):
    """Controlled scores test selection, not an embedding model's accuracy."""

    def __init__(self, *, answer_doc: str | None):
        super().__init__()
        self.answer_doc = answer_doc

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        assert kwargs["memory_id"] == "memory-one"
        candidates = [
            ("foreign-memory", "answer-doc", 0.99, "Foreign secret"),
            ("memory-one", "candidate-doc", 0.98, "Unpromoted candidate"),
            ("memory-one", self.answer_doc or "answer-doc", 0.95 if self.answer_doc else 0.10, "Expected passage: notice is 90 days."),
            ("memory-one", "answer-doc" if self.answer_doc == "graph-doc" else "graph-doc", 0.75 if self.answer_doc else 0.10, "General contract duration is one year."),
        ]
        allowed = kwargs["doc_ids"]
        return [
            SimpleNamespace(
                score=score,
                context_text=f"[Source: {doc_id}] {text}",
                chunk=SimpleNamespace(doc_id=doc_id, filename=f"{doc_id}.md", text=text,
                                      section_title="Notice", article_number="", index=0),
            )
            for memory_id, doc_id, score, text in candidates
            if memory_id == kwargs["memory_id"] and (allowed is None or doc_id in allowed)
        ][:kwargs["limit"]]


@pytest.mark.parametrize(("tool_name", "query_field"), [("memory_query", "query"), ("question_answer", "question")])
@pytest.mark.parametrize("with_entity", [False, True])
async def test_archive_passages_carry_capture_identity_and_distinguish_ingestion(monkeypatch, tool_name, query_field, with_entity):
    server = _server(monkeypatch)
    _, embedder, _, extractor = _wire_search(monkeypatch, server)
    capture = {"source_path": "archives/capture-1/bank/activeContext.md", "sha256": "a" * 64,
               "provenance": "mid_archive", "preimage_id": "capture-1", "bank_path": "bank/activeContext.md",
               "captured_at": "2026-09-24T20:42:48.429919Z", "ingested_at": "2026-10-05T17:31:49Z"}

    class Captures(_RetrievalGraph):
        async def get_documents_meta(self, memory_id, doc_ids):
            assert memory_id == "memory-one" and set(doc_ids) <= {"answer-doc", "graph-doc"}
            return {doc_id: capture for doc_id in doc_ids}

        async def get_entity_context(self, *args, **kwargs):
            ctx = await super().get_entity_context(*args, **kwargs)
            ctx.documents = [{**d, **capture} for d in ctx.documents]
            return ctx

    monkeypatch.setattr(server, "get_graph", lambda: Captures(entity=with_entity, source=capture["source_path"]))
    monkeypatch.setattr(server, "get_vector_store", lambda: _LabeledPassages(answer_doc="answer-doc"))
    monkeypatch.setattr(server, "settings", SimpleNamespace(rag_score_threshold=None, rag_chunk_limit=8))
    response = await getattr(server, tool_name)(memory_id="memory-one", **{query_field: "What changed in Atlas?"})
    assert response["status"] == "ok" and embedder.queries == ["What changed in Atlas?"]
    for doc in response["source_documents"]:
        for key, value in capture.items():
            assert doc[key] == value
    if tool_name == "memory_query":
        for chunk in response["rag_chunks"]:
            for key in ("sha256", "captured_at", "preimage_id", "bank_path", "provenance", "ingested_at"):
                assert chunk[key] == capture[key]
        assert response["rag_chunks"][0]["text"] == "Expected passage: notice is 90 days."
    else:
        prompt, = extractor.prompts
        assert "answer-doc" in prompt and capture["source_path"] in prompt and capture["sha256"] in prompt
        assert "Captured at: " + capture["captured_at"] in prompt
        assert "Ingested at: " + capture["ingested_at"] in prompt
        assert "capture time is not a fact's validity date" in prompt
        assert "Removal from MID does not invalidate a historical fact" in prompt


@pytest.mark.parametrize("archived", [False, True])
async def test_qa_graph_sources_distinguish_same_filename_versions(monkeypatch, archived):
    server = _server(monkeypatch)
    _, _, _, extractor = _wire_search(monkeypatch, server, graph_entity=True)
    documents = []
    for number in (1, 2):
        doc = {"id": f"version-{number}", "filename": "activeContext.md",
               "sha256": str(number) * 64,
               "ingested_at": f"2026-10-05T17:3{number}:00Z"}
        if archived:
            doc.update(preimage_id=f"capture-{number}",
                       captured_at=f"2026-09-24T20:4{number}:00Z",
                       source_path=f"archives/capture-{number}/activeContext.md")
        documents.append(doc)

    class Versions(_Graph):
        async def search_entities(self, *args, **kwargs):
            return [{"name": name, "type": "Service"} for name in ("Atlas", "Recovery")]

        async def get_entity_context(self, *args, **kwargs):
            return SimpleNamespace(documents=documents, relations=[], related_entities=[])

    monkeypatch.setattr(server, "get_graph", lambda: Versions(with_entity=True))
    monkeypatch.setattr(server, "settings", SimpleNamespace(rag_score_threshold=None, rag_chunk_limit=8))
    response = await server.question_answer(memory_id="memory-one", question="What changed?")
    assert response["status"] == "ok"
    context = response["context_used"]
    prompt, = extractor.prompts
    catalogue = prompt.split("Available source documents:\n", 1)[1].split("=== CONTEXT 1:", 1)[0]
    for doc in documents:
        assert context.count(doc["id"]) == 2
        assert doc["ingested_at"] not in context
        assert catalogue.count("Document id: " + doc["id"]) == 1
        assert catalogue.count("SHA256: " + doc["sha256"]) == 1
        assert catalogue.count("Ingested at: " + doc["ingested_at"]) == 1
        if archived:
            assert doc["captured_at"] not in context and doc["source_path"] not in context
            assert catalogue.count("Capture id: " + doc["preimage_id"]) == 1
            assert catalogue.count("Captured at: " + doc["captured_at"]) == 1
            assert catalogue.count("Source path: " + doc["source_path"]) == 1
    if not archived:
        assert "Captured at:" not in catalogue
    assert "not proof that every claim appears in every source" in prompt


@pytest.mark.parametrize(("tool_name", "query_field"), [("memory_query", "query"), ("question_answer", "question")])
@pytest.mark.parametrize(("case", "entity", "answer_doc", "source"), [
    ("misleading-graph", True, "answer-doc", "docs/maintenance.md"),
    ("useful-exact-graph", True, "graph-doc", "docs/general.md"),
    ("synonym-no-entity", False, "answer-doc", "docs/maintenance.md"),
    ("historical-archive", True, "answer-doc", "archives/capture-2026-09-24.md"),
    ("absent-answer", False, None, "docs/maintenance.md"),
])
async def test_labeled_passages_remain_reachable_with_graph_context(
    monkeypatch, tool_name, query_field, case, entity, answer_doc, source
):
    server = _server(monkeypatch)
    _result, embedder, _vector, extractor = _wire_search(monkeypatch, server)
    graph = _RetrievalGraph(entity=entity, source=source)
    vector = _LabeledPassages(answer_doc=answer_doc)
    monkeypatch.setattr(server, "get_graph", lambda: graph)
    monkeypatch.setattr(server, "get_vector_store", lambda: vector)
    monkeypatch.setattr(server, "settings", SimpleNamespace(rag_score_threshold=0.58, rag_chunk_limit=8))
    question = "When must I notify the supplier to end support?" if case == "synonym-no-entity" else "What is the maintenance termination notice?"
    response = await getattr(server, tool_name)(memory_id="memory-one", **{query_field: question})
    assert response["status"] == "ok"
    # One embedding/search, same excerpt budget; graph may not narrow active IDs.
    assert embedder.queries == [question]
    assert len(vector.search_calls) == 1
    assert vector.search_calls[0]["doc_ids"] == ["graph-doc", "answer-doc"]
    assert vector.search_calls[0]["limit"] == 8
    if tool_name == "memory_query":
        chunks = response["rag_chunks"]
        assert response["retrieval_mode"] == ("graph+rag" if entity else "rag-only")
        if answer_doc:
            assert chunks[0]["doc_id"] == answer_doc
            assert chunks[0]["text"] == "Expected passage: notice is 90 days."
            assert chunks[0]["source_path"] == source
        else:
            assert chunks == []
        assert bool(response["entities"]) == entity
        if entity:
            assert response["entities"][0]["relations"][0]["description"] == "Useful graph relationship"
    elif answer_doc:
        assert "Expected passage: notice is 90 days." in extractor.prompts[0]
        if entity:
            assert "Useful graph relationship" in extractor.prompts[0]
    else:
        assert response["rag_chunks_used"] == 0
        assert "could not find" in response["answer"]
        assert extractor.prompts == []
    assert all(doc["id"] != "candidate-doc" for doc in response["source_documents"])
    assert "Foreign secret" not in str(response) + str(extractor.prompts)


@pytest.mark.parametrize(("tool_name", "query_field"), [("memory_query", "query"), ("question_answer", "question")])
async def test_graph_documents_cannot_override_empty_active_set(monkeypatch, tool_name, query_field):
    server = _server(monkeypatch)
    _result, _embedder, _vector, extractor = _wire_search(monkeypatch, server)
    graph = _RetrievalGraph(entity=True, source="docs/maintenance.md")
    async def no_active(_memory_id):
        return []
    graph.get_active_doc_ids = no_active
    vector = _LabeledPassages(answer_doc="answer-doc")
    monkeypatch.setattr(server, "get_graph", lambda: graph)
    monkeypatch.setattr(server, "get_vector_store", lambda: vector)
    response = await getattr(server, tool_name)(memory_id="memory-one", **{query_field: "notice"})
    assert response["status"] == "ok"
    assert vector.search_calls[0]["doc_ids"] == []
    if tool_name == "memory_query":
        assert response["rag_chunks"] == []
    else:
        assert response["rag_chunks_used"] == 0
        assert "Expected passage" not in extractor.prompts[0]


@pytest.mark.parametrize(("tool_name", "query_field"), [("memory_query", "query"), ("question_answer", "question")])
async def test_denied_retrieval_does_not_embed_or_search(monkeypatch, tool_name, query_field):
    server = _server(monkeypatch)
    _result, embedder, vector, extractor = _wire_search(monkeypatch, server)
    denied = {"status": "error", "message": "denied"}
    monkeypatch.setattr(server, "check_memory_access", lambda _memory_id: denied)
    assert await getattr(server, tool_name)(memory_id="memory-one", **{query_field: "notice"}) == denied
    assert embedder.queries == vector.search_calls == extractor.prompts == []
