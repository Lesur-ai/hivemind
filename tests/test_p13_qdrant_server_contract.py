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
            assert response["rag_chunks"][0]["indexed_text"] == "Expected passage: notice is 90 days."
            assert "Expected passage: notice is 90 days." in response["rag_chunks"][0]["text"]
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
            relations=[{"type": "RELATED_TO", "description": "Useful graph relationship",
                        "source": "Known entity", "target": "Recovery", "direction": "outgoing"}],
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
        assert response["rag_chunks"][0]["indexed_text"] == "Expected passage: notice is 90 days."
        assert "Expected passage: notice is 90 days." in response["rag_chunks"][0]["text"]
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
            assert chunks[0]["indexed_text"] == "Expected passage: notice is 90 days."
            assert "Expected passage: notice is 90 days." in chunks[0]["text"]
            assert chunks[0]["source_path"] == source
        else:
            assert chunks == []
        assert bool(response["entities"]) == entity
        if entity:
            relation = response["entities"][0]["relations"][0]
            assert relation["description"] == "Useful graph relationship"
            assert (relation["source"], relation["target"], relation["direction"]) == ("Known entity", "Recovery", "outgoing")
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


# T06: extend the native retrieval harness; source I/O uses the real storage
# boundary over the deletion suite's in-memory object store, never a provider.
@pytest.fixture
def legacy_case(monkeypatch):
    from hashlib import sha256
    from unittest.mock import AsyncMock, Mock
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core.storage import StorageService
    from io import BytesIO

    server = _server(monkeypatch)
    _, _, _, extractor = _wire_search(monkeypatch, server)
    text = '# Session\n12 April 2022\n\n' + ('Background.\n\n' * 400) + 'Mira adopted Pip last week.\n'
    content = text.encode()
    digest = sha256(content).hexdigest()
    uri = 'memory-one/documents/source.md'
    doc = dict(id='answer-doc', memory_id='memory-one', uri=uri, sha256=digest,
               filename='source.md', ingestion_status='succeeded')
    # The public document-read harness also uses stdlib Mock/BytesIO. A
    # private test-module import cannot be shipped in this copied suite.
    store = SimpleNamespace(objects={uri: content}, deleted=[], put_object=Mock(), delete_object=Mock())
    store.get_object = Mock(side_effect=lambda **kw: {'Body': BytesIO(store.objects[kw['Key']])})
    storage = StorageService.__new__(StorageService)
    storage._bucket, storage._client, storage._legacy_client = 'test-bucket', store, store
    graph = _RetrievalGraph(entity=False, source='docs/source.md')
    graph.get_document_details = AsyncMock(side_effect=lambda **kwargs: dict(doc))
    graph.get_documents_meta = AsyncMock(return_value={'answer-doc': doc})
    vector = _LabeledPassages(answer_doc='answer-doc')
    async def search(**kwargs):
        return [SimpleNamespace(score=.8, chunk=SimpleNamespace(
            memory_id='memory-one', doc_id='answer-doc', filename='source.md',
            text='[Session] Mira adopted Pip last week.', section_title='Session',
            article_number=None, index=i)) for i in range(2)]
    vector.search = search
    monkeypatch.setattr(server, 'get_graph', lambda: graph)
    monkeypatch.setattr(server, 'get_vector_store', lambda: vector)
    monkeypatch.setattr(server, 'get_storage', lambda: storage)
    monkeypatch.setattr(server, 'settings', SimpleNamespace(rag_score_threshold=None, rag_chunk_limit=8))
    yield SimpleNamespace(server=server, graph=graph, vector=vector, storage=storage,
                          store=store, doc=doc, text=text, extractor=extractor)
    store.put_object.assert_not_called()
    store.delete_object.assert_not_called()


@pytest.mark.parametrize('tool,field', [('memory_query', 'query'), ('question_answer', 'question')])
async def test_legacy_context_is_reconstructed_once_without_temporal_extraction(legacy_case, tool, field):
    c = legacy_case
    result = await getattr(c.server, tool)('memory-one', **{field: 'When was Pip adopted?'})
    assert result['status'] == 'ok'
    assert result['partial'] is False
    assert c.store.get_object.call_count == 1
    assert result['legacy_context_cost']['source_reads'] == 1
    assert result['legacy_context_cost']['source_bytes'] == len(c.text.encode())
    assert result['legacy_context_cost']['text_codepoints'] == len(c.text)
    assert result['legacy_context_cost']['elapsed_ms'] >= 0
    if tool == 'memory_query':
        assert len(result['rag_chunks']) == 2
        for row in result['rag_chunks']:
            assert row['indexed_text'] == '[Session] Mira adopted Pip last week.'
            assert row['chunk_text'] == 'Mira adopted Pip last week.'
            assert row['annotation_origin'] == 'legacy_unannotated'
            assert row['temporal_annotations'] == []
            assert row['context_status'] == 'complete'
            assert row['source_ref']['sha256'] == c.doc['sha256']
            assert '12 April 2022' in row['text']
            assert 'last week' in row['text']
    else:
        assert '12 April 2022' in c.extractor.prompts[0]
        assert 'legacy_unannotated' in c.extractor.prompts[0]
    assert c.store.deleted == []


@pytest.mark.parametrize('fault,reason,reads', [
    ('foreign_uri', 'source_unavailable', 0),
    ('foreign_bucket', 'source_unavailable', 0),
    ('foreign_memory', 'source_version_mismatch', 0),
    ('foreign_doc', 'source_version_mismatch', 0),
    ('inactive', 'source_version_mismatch', 0),
    ('hash_changed', 'source_version_mismatch', 0),
    ('bad_bytes', 'source_version_mismatch', 1),
    ('missing', 'source_unavailable', 1),
    ('replacement', 'source_version_mismatch', 1),
    ('binary', 'source_unavailable', 0),
    ('invalid_utf8', 'source_unavailable', 1),
    ('ambiguous', 'source_scope_ambiguous', 1),
    ('byte_budget', 'context_budget_exceeded', 1),
    ('text_budget', 'context_budget_exceeded', 1),
    ('deadline', 'context_budget_exceeded', 0),
])
async def test_legacy_context_failures_keep_index_and_never_repair(legacy_case, monkeypatch, fault, reason, reads):
    from hashlib import sha256
    from unittest.mock import AsyncMock
    from mcp_memory.core import temporal
    c = legacy_case
    if fault == 'foreign_uri': c.doc['uri'] = 'other/documents/source.md'
    elif fault == 'foreign_bucket': c.doc['uri'] = 's3://foreign/memory-one/documents/source.md'
    elif fault == 'foreign_memory': c.doc['memory_id'] = 'other'
    elif fault == 'foreign_doc': c.doc['id'] = 'other-doc'
    elif fault == 'inactive': c.doc['ingestion_status'] = 'pending'
    elif fault == 'hash_changed':
        original = dict(c.doc)
        c.graph.get_documents_meta.return_value = {'answer-doc': original}
        c.doc['sha256'] = '0' * 64
    elif fault == 'bad_bytes': c.store.objects[c.doc['uri']] = b'wrong source'
    elif fault == 'missing': c.store.objects.clear()
    elif fault == 'replacement':
        original = dict(c.doc)
        c.graph.get_document_details = AsyncMock(side_effect=[original, dict(original, ingestion_status='superseded')])
    elif fault == 'binary': c.doc['filename'] = 'source.pdf'
    elif fault in ('invalid_utf8', 'ambiguous'):
        content = b'\xff' if fault == 'invalid_utf8' else c.text.encode() + b'Mira adopted Pip last week.\n'
        c.store.objects[c.doc['uri']] = content
        c.doc['sha256'] = sha256(content).hexdigest()
    elif fault == 'byte_budget': monkeypatch.setattr(temporal, 'LEGACY_SOURCE_BYTES', 32)
    elif fault == 'text_budget': monkeypatch.setattr(temporal, 'LEGACY_TEXT_CODEPOINTS', 32)
    elif fault == 'deadline': monkeypatch.setattr(temporal, 'LEGACY_SECONDS', 0)
    before = dict(c.store.objects)
    result = await c.server.memory_query('memory-one', query='When?')
    assert result['status'] == 'ok' and result['partial'] is True
    assert reason in {w['reason'] for w in result['warnings']}
    assert all(w['memory_id'] == 'memory-one' for w in result['warnings'])
    assert c.store.get_object.call_count == reads
    assert c.store.objects == before and c.store.deleted == []
    for row in result['rag_chunks']:
        assert row['indexed_text'] == '[Session] Mira adopted Pip last week.'
        assert row['chunk_text'] is None and row['source_spans'] == [] and row['source_ref'] is None
        assert row['temporal_annotations'] == []
        assert row['context_status'] == ('ambiguous' if fault == 'ambiguous' else 'unavailable')
        assert row['indexed_text'] in row['text']


@pytest.mark.parametrize('source,indexed,title,article,status,quote', [
    ('Été\r\n  Mira\tadopted Pip.\r\n', '[Log] Été Mira adopted Pip.', 'Log', None, 'complete', 'Été\r\n  Mira\tadopted Pip.'),
    ('Evidence.', '[Article 2 - Scope] Evidence.', 'Scope', '2', 'complete', 'Evidence.'),
    ('Evidence.', '[Article 2] Evidence.', 'Scope', '2', 'complete', 'Evidence.'),
    ('Evidence.', '[' + 'T' * 60 + '] Evidence.', 'T' * 90, None, 'complete', 'Evidence.'),
    ('Evidence.', '[' + 'T' * 77 + '...] Evidence.', 'T' * 90, None, 'complete', 'Evidence.'),
    ('[Log] Evidence.', '[Log] Evidence.', 'Log', None, 'ambiguous', None),
    ('[other] Evidence.', '[other] Evidence.', 'Log', None, 'complete', '[other] Evidence.'),
    ('Evidence.\nEvidence.', 'Evidence.', None, None, 'ambiguous', None),
    ('Evidence.', 'evidence.', None, None, 'unavailable', None),
    ('Evidence!', 'Evidence.', None, None, 'unavailable', None),
])
def test_legacy_alignment_only_replays_native_transforms(source, indexed, title, article, status, quote):
    from mcp_memory.core.temporal import ExtractionSource, _legacy_normalize, legacy_alignment
    chunk = SimpleNamespace(text=indexed, section_title=title, article_number=article, index=999)
    span, actual = legacy_alignment(ExtractionSource(source), *_legacy_normalize(source), chunk)
    assert actual == status
    assert (span.quote if span else None) == quote
    if span:
        assert 0 <= span.start < span.end <= len(source)
        assert span.end - span.start == len(span.quote)
        assert source[span.start:span.end] == span.quote


async def test_legacy_context_closure_reports_truncation(legacy_case):
    from hashlib import sha256
    c = legacy_case
    text = '# Session\n' + 'Long introduction ' * 200 + '\n\nMira adopted Pip last week.\n'
    c.store.objects[c.doc['uri']] = text.encode()
    c.doc['sha256'] = sha256(text.encode()).hexdigest()
    result = await c.server.memory_query('memory-one', query='When?')
    assert result['partial'] is True
    assert {w['reason'] for w in result['warnings']} == {'legacy_unannotated', 'context_budget_exceeded'}
    for row in result['rag_chunks']:
        assert row['context_status'] == 'partial'
        assert row['chunk_text'] == 'Mira adopted Pip last week.'
        assert sum(len(e['quote']) for ctx in row['source_context'] for e in ctx['evidence']) <= 8192


async def test_legacy_source_read_checks_metadata_and_closes_bounded_body(legacy_case):
    from io import BytesIO
    from hashlib import sha256
    from time import monotonic
    from unittest.mock import Mock
    from mcp_memory.core.temporal import LegacySourceError
    c = legacy_case
    for metadata in ({'memory-id': 'foreign'}, {'doc_hash': '0' * 64}, {}):
        body = BytesIO(b'body')
        c.store.get_object = Mock(return_value={'Body': body, 'Metadata': metadata})
        with pytest.raises(LegacySourceError, match='source_version_mismatch'):
            await c.storage.read_legacy_document('memory-one', c.doc['uri'], sha256(b'correct').hexdigest(), 20, monotonic() + 1)
        assert body.closed
    body = BytesIO(b'x' * 200)
    c.store.get_object = Mock(return_value={'Body': body, 'ContentLength': 200})
    with pytest.raises(LegacySourceError, match='context_budget_exceeded') as error:
        await c.storage.read_legacy_document('memory-one', c.doc['uri'], c.doc['sha256'], 10, monotonic() + 1)
    assert error.value.bytes_read == 0 and body.closed


async def test_legacy_source_deadline_and_cancellation_discard_late_result(legacy_case, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from mcp_memory.core import temporal
    c = legacy_case
    began = asyncio.Event()
    async def blocked(*args):
        began.set()
        await asyncio.Event().wait()
    c.storage.read_legacy_document = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(temporal, 'LEGACY_SECONDS', .02)
    result = await c.server.memory_query('memory-one', query='When?')
    assert result['partial'] and {w['reason'] for w in result['warnings']} == {'legacy_unannotated', 'context_budget_exceeded'}
    assert c.storage.read_legacy_document.await_count == 1
    monkeypatch.setattr(temporal, 'LEGACY_SECONDS', 30)
    began.clear()
    task = asyncio.create_task(c.server.memory_query('memory-one', query='When?'))
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.store.get_object.call_count == 0


async def test_legacy_budgets_are_shared_and_cached_source_cannot_cross_memory(legacy_case, monkeypatch):
    from unittest.mock import AsyncMock
    from mcp_memory.core import temporal
    c = legacy_case
    # First source succeeds; a second distinct source cannot reuse its allowance.
    monkeypatch.setattr(temporal, 'LEGACY_SOURCE_BYTES', len(c.text.encode()))
    reader = temporal.LegacyContextReader('memory-one', c.graph, c.storage)
    chunk = SimpleNamespace(doc_id='answer-doc', memory_id='memory-one', text='Mira adopted Pip last week.',
                            section_title='Session', article_number=None)
    assert (await reader.enrich(chunk, c.doc))['context_status'] == 'complete'
    import asyncio
    before = reader.summary()['legacy_context_cost']['elapsed_ms']
    await asyncio.sleep(.02)
    assert reader.summary()['legacy_context_cost']['elapsed_ms'] == before
    chunk.memory_id = 'foreign'
    assert (await reader.enrich(chunk, c.doc))['context_status'] == 'unavailable'
    chunk.memory_id, chunk.doc_id = 'memory-one', 'other-doc'
    c.graph.get_document_details = AsyncMock(return_value=dict(c.doc, id='other-doc'))
    assert (await reader.enrich(chunk, c.doc))['context_status'] == 'unavailable'
    assert c.store.get_object.call_count == 1
    assert reader.source_bytes == len(c.text.encode())
    assert 'context_budget_exceeded' in {w['reason'] for w in reader.warnings}


@pytest.mark.parametrize('tool,field', [('memory_query', 'query'), ('question_answer', 'question')])
async def test_legacy_enrichment_never_reads_when_memory_access_is_denied(legacy_case, monkeypatch, tool, field):
    c = legacy_case
    denied = {'status': 'error', 'message': 'denied'}
    monkeypatch.setattr(c.server, 'check_memory_access', lambda _: denied)
    assert await getattr(c.server, tool)('memory-one', **{field: 'When?'}) == denied
    assert c.store.get_object.call_count == 0
    c.graph.get_document_details.assert_not_awaited()


async def test_legacy_cpu_preparation_keeps_event_loop_responsive(legacy_case, monkeypatch):
    import asyncio
    import threading
    from mcp_memory.core import temporal
    c = legacy_case
    released = threading.Event()
    responsive = []
    original = temporal._legacy_normalize
    def controlled(text):
        responsive.append(released.wait(.2))
        return original(text)
    monkeypatch.setattr(temporal, '_legacy_normalize', controlled)
    async def tick():
        await asyncio.sleep(.01)
        released.set()
    heartbeat = asyncio.create_task(tick())
    try:
        result = await c.server.memory_query('memory-one', query='When?')
    finally:
        released.set()
        await heartbeat
    assert responsive and all(responsive)
    assert result['partial'] is False


async def test_legacy_qa_keeps_source_header_for_unavailable_binary(legacy_case):
    c = legacy_case
    original_search = c.vector.search
    async def binary(**kwargs):
        rows = await original_search(**kwargs)
        for row in rows:
            row.chunk.filename, row.chunk.section_title, row.chunk.article_number = 'manual.pdf', 'Scope', '2'
        return rows
    c.vector.search = binary
    c.doc['filename'] = 'manual.pdf'
    result = await c.server.question_answer('memory-one', question='Which section?')
    assert result['partial'] is True
    assert '[Source: manual.pdf > Scope > Art. 2]' in c.extractor.prompts[0]
    assert '[Session] Mira adopted Pip last week.' in c.extractor.prompts[0]
    assert c.store.get_object.call_count == 0
    c.graph.get_document_details.assert_not_awaited()


async def test_legacy_graph_timeout_does_not_claim_an_unobserved_get(legacy_case, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from mcp_memory.core import temporal
    c = legacy_case
    async def blocked(**kwargs):
        await asyncio.Event().wait()
    c.graph.get_document_details = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(temporal, 'LEGACY_SECONDS', .01)
    result = await c.server.memory_query('memory-one', query='When?')
    assert result['partial'] is True
    cost = result['legacy_context_cost']
    assert cost['source_reads'] == cost['source_bytes'] == 0
    assert cost['source_bytes_complete'] is True
    assert c.store.get_object.call_count == 0


async def test_legacy_failed_body_read_reports_incomplete_byte_observation(legacy_case):
    from unittest.mock import Mock
    c = legacy_case
    body = SimpleNamespace(read=Mock(side_effect=OSError('read failed')), close=Mock())
    c.store.get_object.side_effect = lambda **kw: {'Body': body}
    result = await c.server.memory_query('memory-one', query='When?')
    assert result['partial'] is True
    assert result['legacy_context_cost']['source_bytes_complete'] is False
    assert result['legacy_context_cost']['source_reads'] == 1
    body.close.assert_called_once()


@pytest.mark.parametrize('phase', ['prepare', 'locate'])
async def test_legacy_cpu_deadline_discards_late_proof(legacy_case, monkeypatch, phase):
    import asyncio
    import threading
    from mcp_memory.core import temporal
    c = legacy_case
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    name = '_prepare_legacy_source' if phase == 'prepare' else '_locate_legacy_context'
    original = getattr(temporal, name)
    def blocked(*args):
        entered.set()
        release.wait(.5)
        try:
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(temporal, name, blocked)
    monkeypatch.setattr(temporal, 'LEGACY_SECONDS', .05)
    task = asyncio.create_task(c.server.memory_query('memory-one', query='When?'))
    try:
        assert await asyncio.to_thread(entered.wait, .5)
        result = await asyncio.wait_for(task, .2)
        assert result['partial'] is True
        assert all(row['source_ref'] is None and row['source_spans'] == [] for row in result['rag_chunks'])
        assert result['legacy_context_cost']['source_bytes_complete'] is True
        assert result['legacy_context_cost']['text_codepoints_complete'] is (phase == 'locate')
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, .5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
