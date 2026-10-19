"""End to end: a relative fact keeps its anchor and proof from ingestion to long_query (T05 / #922).

Real: extractor parser, source scopes, chunker, annotation, ingestion pipeline,
vector store on a local Qdrant, ``memory_query`` rendering, restart, export and
import, and the maintenance reindex. Doubled: the chat provider (a controlled
structural oracle: it proves preservation, never that a model chose the right
anchor), the embedding provider, Neo4j and S3 transports.
"""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import QdrantClient

from tests.fakes.inference_fakes import make_embedding_profile
from tests.test_mid_archive_bridge import archive_case  # noqa: F401  (fixture)
from tests.test_mid_archive_retrieval import retrieval_case  # noqa: F401  (fixture)
from tests.test_automatic_ontology_state import YAML
from tests.test_long_temporal_extraction import TEXT, response
from tests.test_ontology_closed_vocabulary import extractor, payload  # noqa: F401  (fixture)
from tests.test_p13_qdrant_reindex import (
    _FakeGraph, _FakeStorage, _alias_map, _embedding_result, _fix_operation_id, _seed_legacy, _service,
)

MEMORY = "memory-a"
FILENAME = "sessions.md"


@pytest.fixture(autouse=True)
def _maintenance():
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    reset_maintenance_coordinator_for_tests()
    yield
    reset_maintenance_coordinator_for_tests()


@pytest.fixture
def profile():
    return make_embedding_profile(expected_dimensions=3)


class Embedder:
    """Deterministic vectors from the embedded string only."""

    def __init__(self):
        self.inputs = []

    async def embed_texts_result(self, texts):
        self.inputs.extend(texts)
        return _embedding_result(list(texts))

    async def embed_query_result(self, query):
        return _embedding_result([query])


class World:
    """Fake Neo4j and S3 holding exactly what the real pipeline hands them."""

    def __init__(self):
        self.documents, self.extractions, self.texts, self.objects, self.originals = {}, {}, {}, {}, {}

    # graph
    async def get_memory(self, memory_id):
        return SimpleNamespace(ontology="general")

    async def add_document(self, **fields):
        self.documents[fields["doc_id"]] = dict(fields)

    async def add_entities_and_relations(self, *, memory_id, doc_id, extraction):
        self.extractions[doc_id] = extraction
        return {"entities_created": 0, "entities_merged": 0, "relations_created": 0, "relations_merged": 0}

    async def update_document_ingestion(self, *, doc_id, **fields):
        self.documents[doc_id].update(fields)

    async def get_active_doc_ids(self, memory_id):
        return [d for d, f in self.documents.items()
                if f.get("memory_id") == memory_id and f.get("ingestion_status") == "succeeded"]

    async def get_documents_meta(self, memory_id, doc_ids):
        return {d: {"source_path": self.documents[d].get("source_path"),
                    "ingested_at": "2026-10-15T08:00:00Z"} for d in doc_ids if d in self.documents}

    async def search_entities(self, *_args, **_kwargs):
        return []

    # storage
    @staticmethod
    def compute_hash(content):
        return hashlib.sha256(content).hexdigest()

    async def upload_document(self, *, memory_id, filename, content, metadata=None):
        digest = hashlib.sha256(content).hexdigest()
        key = f"{memory_id}/documents/{digest[:8]}_{filename}"
        self.originals[key] = content
        self.objects[key] = {"key": key, "uri": f"s3://test-bucket/{key}", "size_bytes": len(content),
                             "metadata": {"memory_id": memory_id, "original_filename": filename, "doc_hash": digest}}
        return {"uri": self.objects[key]["uri"], "size_bytes": len(content)}

    async def put_extracted_text(self, memory_id, doc_id, text):
        self.texts[doc_id] = text
        return f"{memory_id}/extracted_text/{doc_id}.txt"

    async def read_extracted_text(self, memory_id, doc_id, record):
        text = self.texts[doc_id]
        if hashlib.sha256(text.encode()).hexdigest() != record["sha256"]:
            raise ValueError("retained extracted text does not match its record")
        return text

    async def get_document_details(self, *, memory_id, doc_id):
        d = self.documents.get(doc_id)
        if not d or d.get("memory_id") != memory_id:
            return None
        return {"id": doc_id, "memory_id": memory_id, "sha256": d["doc_hash"], "uri": d["uri"],
                "filename": d["filename"], "ingestion_status": d.get("ingestion_status")}

    def document_key(self, memory_id, uri):
        return uri

    async def read_legacy_document(self, memory_id, uri, expected_hash, max_bytes, deadline):
        key = next(o["key"] for o in self.objects.values() if o["uri"] == uri)
        return self.originals[key]

    async def delete_document(self, memory_id, uri):
        return True

    async def delete_extracted_text(self, memory_id, doc_id):
        return True


def wire(monkeypatch, world, store, embedder, extractor_service, settings):
    from mcp_memory import server
    from mcp_memory.core import ingest_pipeline
    from mcp_memory.core.chunker import SemanticChunker
    chunker = SemanticChunker()
    for module, name, value in [
        (ingest_pipeline, "_graph", lambda: world), (ingest_pipeline, "_storage", lambda: world),
        (ingest_pipeline, "_extractor", lambda: extractor_service), (ingest_pipeline, "_chunker", lambda: chunker),
        (ingest_pipeline, "_embedder", lambda: embedder), (ingest_pipeline, "_vector_store", lambda: store),
        (ingest_pipeline, "get_settings", lambda: settings),
        (server, "check_memory_access", lambda _memory_id: None),
        (server, "get_graph", lambda: world), (server, "get_embedder", lambda: embedder),
        (server, "get_storage", lambda: world),
        (server, "get_vector_store", lambda: store),
        (server, "settings", SimpleNamespace(rag_score_threshold=None, rag_chunk_limit=8)),
    ]:
        monkeypatch.setattr(module, name, value)
    return server


def sessions_response(source):
    body = response(source, 0)
    body["occurrences"].append(response(source, 1)["occurrences"][0])  # same triple, second session
    return body


async def ingest(world, extractor_service, source_text=TEXT, filename=FILENAME, memory_id=MEMORY, complete=None):
    from mcp_memory.core import ingest_pipeline
    from mcp_memory.core.temporal import ExtractionSource
    content = source_text.encode("utf-8")
    extractor_service._complete = complete or AsyncMock(return_value=SimpleNamespace(
        text=json.dumps(sessions_response(ExtractionSource(source_text, markdown=filename.endswith(".md"))))))
    return await ingest_pipeline.run_ingest_pipeline(
        memory_id=memory_id, content=content, filename=filename, doc_hash=hashlib.sha256(content).hexdigest(),
        ontology=YAML)


@pytest.fixture
def journey(tmp_path, monkeypatch, profile, extractor):
    from mcp_memory.core.vector_store import VectorStoreService
    raw = QdrantClient(path=str(tmp_path / "qdrant"))
    store = VectorStoreService(client=raw, profile=profile, legacy_prefix="memory_")
    world, embedder = World(), Embedder()
    # The T05 journeys below run the occurrence path; T11 journeys turn it off (#999).
    settings = SimpleNamespace(temporal_occurrence_extraction=True)
    server = wire(monkeypatch, world, store, embedder, extractor, settings)
    yield SimpleNamespace(raw=raw, store=store, world=world, embedder=embedder, server=server,
                          extractor=extractor, path=tmp_path / "qdrant", profile=profile, monkeypatch=monkeypatch,
                          settings=settings)
    raw.close()


def two_anchors(rows):
    by_chunk = sorted(rows, key=lambda r: r["chunk_index"])
    return [[a["event_time"]["anchor"]["expression"] for a in r["temporal_annotations"]] for r in by_chunk]


async def query(journey):
    result = await journey.server.memory_query(memory_id=MEMORY, query="Mira adopted Pip", limit=5)
    assert result["status"] == "ok", result
    return result


async def test_each_session_passage_returns_with_its_own_anchor_and_proof(journey):
    ingested = await ingest(journey.world, journey.extractor)
    assert ingested["status"] == "ok" and ingested["chunks_stored"] == 2
    result = await query(journey)
    rows = sorted(result["rag_chunks"], key=lambda r: r["chunk_index"])
    assert two_anchors(rows) == [["12 April 2022"], ["12 January 2024"]]
    assert (result["partial"], result["warnings"]) == (False, [])
    for row, anchor, other in zip(rows, ("12 April 2022", "12 January 2024"), ("12 January 2024", "12 April 2022")):
        assert row["annotation_origin"] == "extracted" and row["context_status"] == "complete"
        assert f"Selected anchor: {anchor}" in row["text"] and other not in row["text"]
        assert "Event time: anchored_relative; expression=\"last week\"; precision=week; normalized=null" in row["text"]
        assert f"Anchor evidence [{row['temporal_annotations'][0]['event_time']['anchor']['expression_evidence'][0]['start']}:" in row["text"]
        assert "Technical ingestion time (not historical): 2026-10-15T08:00:00Z" in row["text"]
        source = journey.world.texts[row["doc_id"]]
        assert [source[s["start"]:s["end"]] for s in row["source_spans"]] == [s["quote"] for s in row["source_spans"]]
        assert row["chunk_text"] == row["source_spans"][0]["quote"] != row["indexed_text"]
        assert row["source_ref"]["text_sha256"] == hashlib.sha256(source.encode()).hexdigest()
        # The fact, its time and its proof arrive together, in text and in structure.
        assert row["temporal_annotations"][0]["fact_evidence"][0]["quote"] in row["text"]
    # No model was called to produce the answer; the extractor ran once at ingestion.
    assert journey.extractor._complete.await_count == 1


async def test_embedding_input_and_ranking_are_unchanged_by_the_annotation(journey, tmp_path):
    from mcp_memory.core.vector_store import VectorStoreService
    await ingest(journey.world, journey.extractor)
    doc_id = next(iter(journey.world.documents))
    annotated = await journey.store.search(
        MEMORY, embedding_result=_embedding_result(["Mira adopted Pip"]), doc_ids=[doc_id], limit=5)
    # Same chunks, annotation removed, stored in a second namespace of the same index.
    plain = [c.chunk.model_copy(update={"annotation": None, "memory_id": "memory-b"}) for c in annotated]
    other = QdrantClient(path=str(tmp_path / "plain"))
    try:
        store = VectorStoreService(client=other, profile=journey.profile, legacy_prefix="memory_")
        vectors = _embedding_result([c.text for c in plain])
        await store.store_chunks("memory-b", doc_id, FILENAME, plain, embedding_result=vectors)
        baseline = await store.search("memory-b", embedding_result=_embedding_result(["Mira adopted Pip"]), limit=5)
    finally:
        other.close()
    assert [(r.chunk.text, r.chunk.index, r.score) for r in annotated] == \
           [(r.chunk.text, r.chunk.index, r.score) for r in baseline]
    assert all(r.chunk.annotation for r in annotated) and not any(r.chunk.annotation for r in baseline)
    # The embedded strings are exactly the chunk texts.
    assert journey.embedder.inputs == [c.chunk.text for c in sorted(annotated, key=lambda r: r.chunk.index)]


def stable(rows):
    return [{k: v for k, v in row.items() if k != "score"}
            for row in sorted(rows, key=lambda r: r["chunk_index"])]


@pytest.mark.parametrize("origin", ["extracted", "context_only"])
async def test_annotations_survive_restart_export_and_import(journey, tmp_path, origin):
    from mcp_memory.core.vector_store import VectorStoreService
    journey.settings.temporal_occurrence_extraction = origin == "extracted"  # #999
    await ingest(journey.world, journey.extractor)
    before = stable((await query(journey))["rag_chunks"])
    assert len(before) == 2
    assert {row["annotation_origin"] for row in before} == {origin}

    # Restart: a new client and store over the same files.
    journey.raw.close()
    journey.raw = QdrantClient(path=str(journey.path))
    restarted = VectorStoreService(client=journey.raw, profile=journey.profile, legacy_prefix="memory_")
    journey.monkeypatch.setattr(journey.server, "get_vector_store", lambda: restarted)
    assert stable((await query(journey))["rag_chunks"]) == before

    # Export, then import into a fresh index of the same embedding identity.
    bundle = await restarted.export_collection(MEMORY)
    assert all(set(p["payload"]) >= {"source_ref", "temporal_annotations"} for p in bundle["points"])
    target = QdrantClient(path=str(tmp_path / "imported"))
    try:
        imported = VectorStoreService(client=target, profile=journey.profile, legacy_prefix="memory_")
        assert await imported.import_collection(MEMORY, bundle["points"], identity=bundle["identity"]) == 2
        journey.monkeypatch.setattr(journey.server, "get_vector_store", lambda: imported)
        assert stable((await query(journey))["rag_chunks"]) == before
    finally:
        target.close()


async def test_memory_query_reports_the_response_wide_occurrence_bound(journey, monkeypatch):
    from mcp_memory.core import temporal
    await ingest(journey.world, journey.extractor)
    whole = await query(journey)
    sizes = [len(json.dumps(r["temporal_annotations"][0], ensure_ascii=False, separators=(",", ":"),
                            sort_keys=True).encode()) for r in whole["rag_chunks"]]
    monkeypatch.setattr(temporal, "OCCURRENCE_BYTES_PER_RESPONSE", sizes[0] + 1)  # only the first fits
    result = await query(journey)
    kept = [bool(r["temporal_annotations"]) for r in result["rag_chunks"]]
    assert kept == [True, False]
    lost = result["rag_chunks"][1]
    assert lost["occurrences_partial"] is True and lost["context_status"] == "partial"
    assert "Derived temporal annotation" not in lost["text"]
    assert result["partial"] is True
    assert result["warnings"] == [
        {"memory_id": MEMORY, "doc_id": lost["doc_id"], "reason": "occurrence_budget_exceeded"}]


@pytest.mark.parametrize("fault", ["record_tampered", "foreign_namespace", "version_two", "partial_keys", "span_quote",
                                   "span_quote_same_length", "swapped_annotation", "record_quote_same_length"])
async def test_a_forged_or_foreign_annotation_is_refused_before_import_writes(journey, tmp_path, fault):
    from mcp_memory.core.vector_store import EmbeddingCollectionReindexRequired, VectorStoreService
    await ingest(journey.world, journey.extractor)
    bundle = await journey.store.export_collection(MEMORY)
    points = copy.deepcopy(bundle["points"])
    payload = points[0]["payload"]
    if fault == "record_tampered":
        payload["temporal_annotations"][0]["fact"]["to_entity"] = "Someone else"
    elif fault == "foreign_namespace":
        payload["source_ref"]["memory_id"] = "memory-z"
    elif fault == "version_two":
        payload["temporal_schema_version"] = 2
    elif fault == "partial_keys":
        del payload["source_context"]
    elif fault == "span_quote":
        payload["source_spans"][0]["quote"] = "forged"
    elif fault == "span_quote_same_length":
        quote = payload["source_spans"][0]["quote"]
        payload["source_spans"][0]["quote"] = "X" * len(quote)  # round-1 F2: length alone is not a check
    elif fault == "swapped_annotation":
        other = points[1]["payload"]
        for key in ("source_ref", "source_spans", "source_context", "temporal_annotations"):
            payload[key] = other[key]  # a real annotation of the other passage
    elif fault == "record_quote_same_length":
        evidence = payload["temporal_annotations"][0]["fact_evidence"][0]
        evidence["quote"] = "X" * len(evidence["quote"])
    target = QdrantClient(path=str(tmp_path / "refused"))
    try:
        store = VectorStoreService(client=target, profile=journey.profile, legacy_prefix="memory_")
        with pytest.raises(EmbeddingCollectionReindexRequired):
            await store.import_collection(MEMORY, points, identity=bundle["identity"])
        assert target.get_collections().collections == []  # nothing was created or written
    finally:
        target.close()


async def test_the_annotation_of_another_passage_never_reaches_the_index(journey):
    await ingest(journey.world, journey.extractor)
    found = await journey.store.search(MEMORY, embedding_result=_embedding_result(["Mira adopted Pip"]), limit=5)
    first, second = sorted((r.chunk for r in found), key=lambda c: c.index)
    swapped = [first.model_copy(update={"annotation": second.annotation}),
               second.model_copy(update={"annotation": first.annotation})]
    before = journey.raw.count(collection_name=journey.store._resolve_collection(MEMORY).name, exact=True).count
    with pytest.raises(ValueError, match="invalid chunk annotation"):
        await journey.store.store_chunks(MEMORY, first.doc_id, FILENAME, swapped,
                                         embedding_result=_embedding_result([c.text for c in swapped]))
    assert journey.raw.count(collection_name=journey.store._resolve_collection(MEMORY).name, exact=True).count == before


async def test_an_invalid_annotation_never_reaches_the_index(journey):
    from mcp_memory.core.models import Chunk
    chunk = Chunk(text="x", index=0, total_chunks=1, annotation={"temporal_schema_version": 1})
    with pytest.raises(ValueError, match="invalid chunk annotation"):
        await journey.store.store_chunks(MEMORY, "doc", FILENAME, [chunk], embedding_result=_embedding_result(["x"]))
    assert journey.raw.get_collections().collections == []


# --- reindex -------------------------------------------------------------------

class ReindexGraph(_FakeGraph):
    """The persisted facts the maintenance reindex may read: records, never a model."""

    def __init__(self, world, rows):
        super().__init__(rows, memory_id=MEMORY)
        self.world = world
        self.reads = []

    async def list_reindex_text_records(self, memory_id):
        # Exactly what the pipeline stored on each Document, serialized as the graph does.
        return {doc_id: json.dumps(fields["extracted_text"], sort_keys=True, separators=(",", ":"))
                for doc_id, fields in self.world.documents.items() if fields.get("extracted_text") is not None}

    async def list_reindex_occurrences(self, memory_id, doc_id):
        self.reads.append(doc_id)
        return list(self.world.extractions[doc_id].occurrences)


def reindex_rows(world, doc_id):
    document = world.documents[doc_id]
    return [{"memory_id": MEMORY, "document_id": doc_id, "filename": document["filename"],
             "uri": document["uri"], "sha256": document["doc_hash"], "size_bytes": document["size_bytes"],
             "status": "succeeded", "chunk_count": document["chunk_count"]}]


class ReindexStorage(_FakeStorage):
    def __init__(self, world):
        super().__init__(list(world.objects.values()), world.originals, memory_id=MEMORY)
        self.world = world

    async def read_extracted_text(self, memory_id, doc_id, record):
        return await self.world.read_extracted_text(memory_id, doc_id, record)


async def rebuild(journey, doc_ids, mutate=None):
    """Reindex the memory's documents into a shadow index from what is persisted."""
    from mcp_memory.core.chunker import SemanticChunker
    from mcp_memory.core.maintenance import get_maintenance_coordinator
    from mcp_memory.core.vector_store import VectorStoreService
    # A different index of the same memory that needs rebuilding: reindex fills a shadow.
    other = QdrantClient(path=str(journey.path.parent / "rebuilt"))
    legacy = _seed_legacy(other, MEMORY)
    store = VectorStoreService(client=other, profile=journey.profile, legacy_prefix="memory_")
    graph = ReindexGraph(journey.world, [row for doc_id in doc_ids for row in reindex_rows(journey.world, doc_id)])
    storage = ReindexStorage(journey.world)
    if mutate:
        mutate(journey.world, doc_ids[0], graph, storage)
    extracted = []
    service = _service(graph=graph, storage=storage, chunker=SemanticChunker(), embedder=journey.embedder, vectors=store,
                       text_extractor=lambda content, name: extracted.append(name) or content.decode("utf-8"),
                       coordinator=get_maintenance_coordinator())
    result = await service.reindex(MEMORY)
    return SimpleNamespace(result=result, store=store, other=other, graph=graph, extracted=extracted, legacy=legacy)


async def reindexed(journey, mutate=None):
    await ingest(journey.world, journey.extractor)
    doc_id = next(iter(journey.world.documents))
    before = stable((await query(journey))["rag_chunks"])
    run = await rebuild(journey, [doc_id], mutate)
    run.before, run.doc_id = before, doc_id
    return run


@pytest.mark.parametrize("origin", ["extracted", "context_only"])
async def test_reindex_rebuilds_the_same_annotations_from_retained_text_and_stored_records(journey, monkeypatch, origin):
    _fix_operation_id(monkeypatch)
    journey.settings.temporal_occurrence_extraction = origin == "extracted"  # #999
    run = await reindexed(journey)
    try:
        assert {row["annotation_origin"] for row in run.before} == {origin}
        assert run.result["status"] == "ok" and run.result["activated"] is True, run.result
        assert run.graph.reads == [run.doc_id]
        assert run.extracted == []  # the retained text is used: no re-conversion, no re-extraction
        assert journey.extractor._complete.await_count == 1
        monkeypatch.setattr(journey.server, "get_vector_store", lambda: run.store)
        assert stable((await query(journey))["rag_chunks"]) == run.before
        assert _alias_map(run.other)
    finally:
        run.other.close()


async def test_a_document_ingested_before_retention_reindexes_as_legacy_without_annotation(journey, monkeypatch):
    _fix_operation_id(monkeypatch)

    def forget(world, doc_id, graph, storage):
        world.extractions[doc_id] = world.extractions[doc_id].model_copy(update={"occurrences": []})
        graph.list_reindex_text_records = AsyncMock(return_value={})

    run = await reindexed(journey, forget)
    try:
        assert run.result["status"] == "ok", run.result
        assert run.extracted == [FILENAME]  # legacy path: converted from the original, as before
        monkeypatch.setattr(journey.server, "get_vector_store", lambda: run.store)
        result = await query(journey)
        rows = result["rag_chunks"]
        assert {r["annotation_origin"] for r in rows} == {"legacy_unannotated"}
        assert all(r["temporal_annotations"] == [] for r in rows)
        # Not annotated, not guessed: context is rebuilt from the verified original.
        assert all(r["context_status"] == "complete" and r["chunk_text"] for r in rows)
        assert all(r["text"].startswith("Legacy context: legacy_unannotated; complete") for r in rows)
        assert result["partial"] is False
        assert {w["reason"] for w in result["warnings"]} == {"legacy_unannotated"}
    finally:
        run.other.close()


@pytest.mark.parametrize(("fault", "reason"), [
    ("text_corrupt", "retained_text_invalid"),
    ("text_missing", "retained_text_unavailable"),
    ("record_foreign_document", "source_annotation_invalid"),
    ("record_other_text_version", "source_annotation_invalid"),
    ("record_unreadable", "source_annotation_invalid"),
    ("record_altered_anchor", "source_annotation_invalid"),
    ("record_borrowed_sibling_anchor", "source_annotation_invalid"),
    ("disabled_with_records", "source_annotation_invalid"),
])
async def test_reindex_refuses_unverifiable_sources_and_activates_nothing(journey, monkeypatch, fault, reason):
    _fix_operation_id(monkeypatch)

    def damage(world, doc_id, graph, storage):
        if fault == "text_corrupt":
            world.texts[doc_id] = world.texts[doc_id] + " "
        elif fault == "text_missing":
            del world.texts[doc_id]
        elif fault == "record_foreign_document":
            async def foreign(memory_id, requested):
                row = world.extractions[requested].occurrences[0]
                return [row.model_copy(update={"source_ref": row.source_ref.model_copy(update={"doc_id": "other"})})]
            graph.list_reindex_occurrences = foreign
        elif fault == "record_other_text_version":
            async def stale(memory_id, requested):
                row = world.extractions[requested].occurrences[0]
                return [row.model_copy(update={"source_ref": row.source_ref.model_copy(update={"text_sha256": "b" * 64})})]
            graph.list_reindex_occurrences = stale
        elif fault == "record_unreadable":
            from mcp_memory.core.temporal import TemporalOutputError

            async def unreadable(memory_id, requested):
                raise TemporalOutputError("unsupported_temporal_schema")
            graph.list_reindex_occurrences = unreadable
        elif fault == "record_altered_anchor":
            from mcp_memory.core.chunk_render import canonical
            from mcp_memory.core.temporal import load_record

            # An equal-length change of a stored anchor, outside every passage
            # and introduction. The record identity does not cover anchors, so
            # the altered record still loads as a valid record of this document.
            def altered(row):
                event = row.event_time
                if not (event and event.anchor and event.anchor.expression == "12 April 2022"):
                    return row
                anchor = event.anchor.model_copy(update={
                    "expression": "12 April 2023",
                    "expression_evidence": [s.model_copy(update={"quote": s.quote.replace("2022", "2023")})
                                            for s in event.anchor.expression_evidence]})
                return row.model_copy(update={"event_time": event.model_copy(update={"anchor": anchor})})

            rows = [altered(row) for row in world.extractions[doc_id].occurrences]
            changed = [row for row, old in zip(rows, world.extractions[doc_id].occurrences) if row is not old]
            assert changed and all(
                load_record(canonical(row.model_dump(mode="json")), MEMORY, doc_id, row.occurrence_id)
                for row in changed)

            async def stored(memory_id, requested):
                return list(rows)
            graph.list_reindex_occurrences = stored
        elif fault == "record_borrowed_sibling_anchor":
            from mcp_memory.core.chunk_render import canonical
            from mcp_memory.core.temporal import Scope, load_record

            # Exact-head R1 C3-F1: the other session's authentic anchor, its scope
            # widened to the document so the source-independent invariants pass.
            original = world.extractions[doc_id].occurrences
            other = next(r.event_time.anchor for r in original if r.event_time.anchor.expression == "12 January 2024")
            widened = other.model_copy(update={"scope": Scope(start=0, end=len(TEXT))})
            rows = [row.model_copy(update={"event_time": row.event_time.model_copy(update={"anchor": widened})})
                    if row.event_time.anchor.expression == "12 April 2022" else row for row in original]
            changed = [row for row, old in zip(rows, original) if row is not old]
            assert changed and all(
                load_record(canonical(row.model_dump(mode="json")), MEMORY, doc_id, row.occurrence_id)
                for row in changed)

            async def borrowed(memory_id, requested):
                return list(rows)
            graph.list_reindex_occurrences = borrowed
        elif fault == "disabled_with_records":
            # #999: authentic records under a generation whose record says they were never requested.
            world.documents[doc_id]["extracted_text"] = dict(world.documents[doc_id]["extracted_text"],
                                                             occurrences="disabled")

    run = await reindexed(journey, damage)
    try:
        assert run.result["status"] == "error" and run.result["reason"] == reason, run.result
        assert run.result["activated"] is False
        assert _alias_map(run.other) == {}
    finally:
        run.other.close()


# --- occurrence extraction off by default (T11 / #999) ---------------------------

# Provider messages of the 1.6.2 extractor (private main c4a50a5d, VERSION 1.6.2)
# for TEXT, the YAML catalogue and the replies below, captured once from that
# commit with this canonical JSON (PLAN-999). The default ingestion sends them.
PROMPT_1_6_2 = {
    "single_with_correction": "e28ae0d78551055b4f7c12d2ece9b4bd641199077aeb7a4c302a5b7c571fd2c5",
    "chunked_cumulative": "545fb8b588d9f5907abb2f11508f446d897270ed20926c4ab8bacd08003140b3",
}


def recording(replies):
    seen = []

    async def complete(messages):
        seen.append(copy.deepcopy(messages))  # the extractor appends its correction turn in place
        return SimpleNamespace(text=replies[len(seen) - 1])
    return seen, complete


def spontaneous_response(source):
    """Temporal fields nobody asked for, in the shape that rejected whole documents (#925)."""
    body = sessions_response(source)
    event = body["occurrences"][0]["event_time"]
    event["anchor_candidates"] = [event["anchor"]]  # anchor and candidates: invalid when requested
    return body


@pytest.mark.parametrize(("case", "chunk_size", "replies"), [
    ("single_with_correction", 25_000, [payload(relation="INVENTED"), payload()]),
    ("chunked_cumulative", 60, [payload(), payload()]),
])
async def test_default_ingestion_sends_the_1_6_2_prompt_bytes(journey, monkeypatch, case, chunk_size, replies):
    from mcp_memory.config import Settings
    from mcp_memory.core import extractor as module
    assert Settings.model_fields["temporal_occurrence_extraction"].default is False
    monkeypatch.setenv("TEMPORAL_OCCURRENCE_EXTRACTION", "true")
    assert Settings().temporal_occurrence_extraction is True  # the documented variable switches it on
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        extraction_chunk_size=chunk_size, extraction_max_text_length=950_000))
    journey.settings.temporal_occurrence_extraction = False
    seen, complete = recording(replies)
    result = await ingest(journey.world, journey.extractor, complete=complete)
    assert result["status"] == "ok", result
    encoded = json.dumps(seen, ensure_ascii=False, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode()).hexdigest() == PROMPT_1_6_2[case]
    assert "TEMPORAL" not in encoded and "temporal_schema_version" not in encoded
    # Requested, the same document carries the occurrence contract: the switch is causal.
    journey.settings.temporal_occurrence_extraction = True
    requested, complete = recording([payload()] * 4)
    await ingest(journey.world, journey.extractor, complete=complete)
    assert "TEMPORAL CONTRACT v1" in requested[0][1]["content"]


async def test_default_ingestion_keeps_source_context_without_requesting_occurrences(journey):
    from mcp_memory.core.temporal import ExtractionSource
    reply = SimpleNamespace(text=json.dumps(spontaneous_response(ExtractionSource(TEXT, markdown=True))))
    # Requested, this output fails the whole document (after its bounded correction).
    refused = await ingest(journey.world, journey.extractor, complete=AsyncMock(return_value=reply))
    assert refused["status"] == "error" and journey.world.documents == {}

    journey.settings.temporal_occurrence_extraction = False
    provider = AsyncMock(return_value=reply)
    ingested = await ingest(journey.world, journey.extractor, complete=provider)
    assert ingested["status"] == "ok" and ingested["chunks_stored"] == 2, ingested
    assert provider.await_count == 1  # accepted as returned: no correction turn, no temporal rejection
    (doc_id, document), = journey.world.documents.items()
    extraction = journey.world.extractions[doc_id]
    assert (extraction.temporal_schema_version, extraction.occurrences) == (1, [])
    assert extraction.temporal_output_discarded == 2  # counted, never validated or stored
    # The exact text is retained, bound by its hash, and the record says occurrences were not requested.
    record = document["extracted_text"]
    assert journey.world.texts[doc_id] == TEXT
    assert record["sha256"] == hashlib.sha256(TEXT.encode()).hexdigest() and record["occurrences"] == "disabled"
    assert await journey.world.read_extracted_text(MEMORY, doc_id, record) == TEXT

    result = await query(journey)
    rows = sorted(result["rag_chunks"], key=lambda r: r["chunk_index"])
    assert (result["partial"], result["warnings"]) == (False, [])
    for row, heading, date, other in zip(rows, ("# Session A", "# Session B"), ("12 April 2022", "12 January 2024"),
                                         ("12 January 2024", "12 April 2022")):
        assert row["annotation_origin"] == "context_only" and row["temporal_annotations"] == []
        assert (row["context_status"], row["occurrences_partial"]) == ("complete", False)
        assert {(e["role"], e["evidence"][0]["quote"]) for e in row["source_context"]} == {
            ("heading", heading), ("introduction", date)}
        assert f"Source context [heading; " in row["text"] and f"]: {date}\n" in row["text"]
        assert other not in row["text"] and "Derived temporal annotation" not in row["text"]
        assert row["source_ref"]["text_sha256"] == record["sha256"]
        assert [TEXT[s["start"]:s["end"]] for s in row["source_spans"]] == [s["quote"] for s in row["source_spans"]]


# A date stated once at the top, far from the passage, with and without an
# introduction beyond the contract's 2,048 code points.
DISTANT_TEXT = "Recorded 12 April 2022.\n\n" + "".join(
    f"Line {i} describes the shelter routine in plain words.\n" for i in range(60)) + "\nMira adopted Pip last week.\n"
OVERFLOW_TEXT = "Recorded 12 April 2022. " + "H" * 5000 + "\n\nMira adopted Pip last week.\n"


@pytest.mark.parametrize(("source_text", "status"), [(DISTANT_TEXT, "complete"), (OVERFLOW_TEXT, "partial")],
                         ids=["distant", "overflow"])
async def test_default_ingestion_bounds_distant_context_and_never_claims_a_cut_one_complete(journey, source_text, status):
    journey.settings.temporal_occurrence_extraction = False
    ingested = await ingest(journey.world, journey.extractor, source_text=source_text, filename="log.txt",
                            complete=AsyncMock(return_value=SimpleNamespace(text=payload())))
    assert ingested["status"] == "ok", ingested
    result = await journey.server.memory_query(memory_id=MEMORY, query="Mira adopted Pip last week", limit=5)
    row = next(r for r in result["rag_chunks"] if "Mira adopted Pip" in r["chunk_text"])
    assert "Recorded 12 April 2022" not in row["chunk_text"]  # the passage itself does not carry the date
    intro = next(e for e in row["source_context"] if e["role"] == "introduction")
    assert intro["evidence"][0]["quote"].startswith("Recorded 12 April 2022.")
    assert len(intro["evidence"][0]["quote"]) <= 2048
    assert "]: Recorded 12 April 2022." in row["text"]
    assert (row["annotation_origin"], row["context_status"]) == ("context_only", status)
    warning = {"memory_id": MEMORY, "doc_id": row["doc_id"], "reason": "context_budget_exceeded"}
    assert (warning in result["warnings"], result["partial"]) == ((status == "partial"),) * 2


async def test_mixed_generations_keep_their_origin_through_export_import_and_reindex_after_the_switch_flips(
        journey, monkeypatch, tmp_path):
    """Old (no marker), extracted and context-only generations in one memory (#999)."""
    from mcp_memory.core.vector_store import VectorStoreService
    _fix_operation_id(monkeypatch)
    world, origins = journey.world, {}
    for filename, requested in (("old.md", True), ("extracted.md", True), ("context.md", False)):
        journey.settings.temporal_occurrence_extraction = requested
        ingested = await ingest(world, journey.extractor, filename=filename)
        assert ingested["status"] == "ok", ingested
        origins[ingested["document_id"]] = "extracted" if requested else "context_only"
    old = next(d for d, f in world.documents.items() if f["filename"] == "old.md")
    # A generation retained before the switch existed: its record has no marker.
    world.documents[old]["extracted_text"] = {k: v for k, v in world.documents[old]["extracted_text"].items()
                                              if k != "occurrences"}
    assert sorted(f["extracted_text"].get("occurrences", "-") for f in world.documents.values()) == [
        "-", "disabled", "extracted"]

    def by_document(result):
        rows = sorted(result["rag_chunks"], key=lambda r: (r["doc_id"], r["chunk_index"]))
        assert {r["doc_id"]: r["annotation_origin"] for r in rows} == origins
        assert all(bool(r["temporal_annotations"]) == (origins[r["doc_id"]] == "extracted") for r in rows)
        return [{k: v for k, v in row.items() if k != "score"} for row in rows]

    before = by_document(await query(journey))
    assert len(before) == 6

    # Export, then import into a fresh index of the same embedding identity.
    bundle = await journey.store.export_collection(MEMORY)
    target = QdrantClient(path=str(tmp_path / "imported"))
    try:
        imported = VectorStoreService(client=target, profile=journey.profile, legacy_prefix="memory_")
        assert await imported.import_collection(MEMORY, bundle["points"], identity=bundle["identity"]) == 6
        monkeypatch.setattr(journey.server, "get_vector_store", lambda: imported)
        assert by_document(await query(journey)) == before
    finally:
        target.close()

    # The setting flips before the reindex: every generation keeps its own origin.
    journey.settings.temporal_occurrence_extraction = True
    run = await rebuild(journey, sorted(origins))
    try:
        assert run.result["status"] == "ok" and run.result["activated"] is True, run.result
        assert run.extracted == []  # every generation is rebuilt from its retained text
        # Records are read for every generation: one marked disabled must own none.
        assert sorted(run.graph.reads) == sorted(origins)
        monkeypatch.setattr(journey.server, "get_vector_store", lambda: run.store)
        assert by_document(await query(journey)) == before
    finally:
        run.other.close()


# --- document and archive namespaces through the façade -----------------------------

ARCHIVE_TEXT = TEXT.replace("12 April 2022", "14 August 2022").replace("12 January 2024", "3 March 2024")


async def routed(case, journey):
    """The façade's graph client now reaches the real ``memory_query`` of each namespace."""
    previous = case.client.call_tool.side_effect

    async def call(name, args):
        if name == "memory_query" and not (case.error and args["memory_id"] == case.archive_id):
            return await journey.server.memory_query(
                memory_id=args["memory_id"], query=args["query"], limit=args["limit"])
        return await previous(name, args)

    case.client.call_tool.side_effect = call


TEMPORAL_KEYS = ["source_ref", "source_spans", "source_context", "temporal_annotations", "annotation_origin",
                 "context_status", "occurrences_partial", "temporal_warnings", "temporal_schema_version"]


def make_legacy(journey, memory_id):
    """A generation written before the extension: same chunks, no stored annotation."""
    name = journey.store._resolve_collection(memory_id).name
    for point in journey.raw.scroll(collection_name=name, limit=100, with_payload=True)[0]:
        journey.raw.delete_payload(collection_name=name, keys=TEMPORAL_KEYS, points=[point.id])


async def two_namespaces(retrieval_case, journey, *, archive_annotated=True):
    c = retrieval_case
    await ingest(journey.world, journey.extractor, memory_id="documentary")
    await ingest(journey.world, journey.extractor, source_text=ARCHIVE_TEXT, memory_id=c.archive_id)
    if not archive_annotated:
        make_legacy(journey, c.archive_id)
    await routed(c, journey)
    return c


def doc_of(journey, memory_id):
    return next(d for d, f in journey.world.documents.items() if f["memory_id"] == memory_id)


def by_namespace(result):
    grouped = {}
    for row in result["rag_chunks"]:
        grouped.setdefault(row["memory_id"], []).append(row)
    return grouped


async def test_same_file_name_in_both_namespaces_never_crosses_anchors_or_identity(retrieval_case, journey):
    c = await two_namespaces(retrieval_case, journey)
    result = await c.engine.query("space-a", "Mira adopted Pip", limit=5)
    assert result["status"] == "ok" and set(result["memory_ids"]) == {"documentary", c.archive_id}
    grouped = by_namespace(result)
    assert set(grouped) == {"documentary", c.archive_id}
    expected = {"documentary": {"12 April 2022", "12 January 2024"}, c.archive_id: {"14 August 2022", "3 March 2024"}}
    for namespace, rows in grouped.items():
        assert {r["filename"] for r in rows} == {FILENAME}
        anchors = {a["event_time"]["anchor"]["expression"] for r in rows for a in r["temporal_annotations"]}
        assert anchors == expected[namespace]
        for row in rows:
            assert row["source_ref"]["memory_id"] == namespace == row["memory_id"]
            assert f"[Source: {namespace}/{row['doc_id']};" in row["text"]
            assert {r["memory_id"] for r in row["temporal_annotations"][0:0]} == set()  # records carry their own ref
            assert all(a["source_ref"]["memory_id"] == namespace for a in row["temporal_annotations"])
            for foreign in set().union(*(v for k, v in expected.items() if k != namespace)):
                assert foreign not in row["text"]
    assert (result["partial"], result["warnings"]) == (False, [])


async def test_annotated_and_legacy_passages_coexist_across_namespaces_without_mislabelling(retrieval_case, journey):
    c = await two_namespaces(retrieval_case, journey, archive_annotated=False)
    result = await c.engine.query("space-a", "Mira adopted Pip", limit=5)
    grouped = by_namespace(result)
    assert {r["annotation_origin"] for r in grouped["documentary"]} == {"extracted"}
    assert {r["annotation_origin"] for r in grouped[c.archive_id]} == {"legacy_unannotated"}
    # The documentary passages keep their stored anchors; the archive ones get only
    # structural context from their verified original, never an invented annotation.
    assert all(r["temporal_annotations"] for r in grouped["documentary"])
    assert all(r["temporal_annotations"] == [] and r["context_status"] == "complete" for r in grouped[c.archive_id])
    assert all("Derived temporal annotation" in r["text"] for r in grouped["documentary"])
    assert all("Derived temporal annotation" not in r["text"] for r in grouped[c.archive_id])
    assert result["partial"] is False  # complete context on both sides
    assert result["warnings"] == [
        {"memory_id": c.archive_id, "doc_id": doc_of(journey, c.archive_id), "reason": "legacy_unannotated"}]


async def test_archive_outage_keeps_the_documentary_side_warnings(retrieval_case, journey):
    c = await two_namespaces(retrieval_case, journey)
    make_legacy(journey, "documentary")  # the documentary side now carries its own warning
    c.error = ConnectionError("unreachable")
    result = await c.engine.query("space-a", "Mira adopted Pip", limit=5)
    assert result["status"] == "ok" and result["partial"] is True
    assert result["warnings"] == [
        {"memory_id": "documentary", "doc_id": doc_of(journey, "documentary"), "reason": "legacy_unannotated"},
        {"scope": "mid_archive", "reason": "archive_unavailable",
         "message": "Archive evidence is unavailable; documentary results only."},
    ]


async def test_the_response_wide_bounds_apply_again_after_interleaving(retrieval_case, journey, monkeypatch):
    from live_mem.core import chunk_render
    c = await two_namespaces(retrieval_case, journey)
    unbounded = await c.engine.query("space-a", "Mira adopted Pip", limit=5)
    assert len(unbounded["rag_chunks"]) == 4 and unbounded["partial"] is False
    sizes = [len(chunk_render.canonical(r["temporal_annotations"][0]).encode()) for r in unbounded["rag_chunks"]]
    # Each namespace fits its own budget; together they exceed this one by the last record.
    monkeypatch.setattr(chunk_render, "BYTES_PER_RESPONSE", sum(sizes[:3]))
    result = await c.engine.query("space-a", "Mira adopted Pip", limit=5)
    rows = result["rag_chunks"]
    assert [bool(r["temporal_annotations"]) for r in rows] == [True, True, True, False]
    row = rows[3]
    assert row["occurrences_partial"] is True and row["context_status"] == "partial"
    assert "Derived temporal annotation" not in row["text"] and "Context status: partial" in row["text"]
    assert result["partial"] is True
    assert result["warnings"] == [{"memory_id": row["memory_id"], "reason": "occurrence_budget_exceeded"}]
    # The untouched rows still state exactly what their text shows, in the same ranked order.
    assert [r["doc_id"] for r in rows] == [r["doc_id"] for r in unbounded["rag_chunks"]]
    for kept in rows[:3]:
        assert kept["occurrences_partial"] is False
        assert all(a["occurrence_id"] in kept["text"] for a in kept["temporal_annotations"])


class Answerer:
    """Captures the prompt a QA call builds; no model is involved."""

    def __init__(self):
        self.prompts = []

    async def generate_answer(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return "answer"

    async def _complete(self, messages, *args, **kwargs):
        self.prompts.append(str(messages))
        return SimpleNamespace(text="answer")

    def __getattr__(self, name):
        async def call(prompt=None, *args, **kwargs):
            self.prompts.append(str(prompt))
            return "answer"
        return call


async def test_question_answer_gives_an_annotated_passage_its_own_anchor_not_a_legacy_label(journey, monkeypatch):
    answerer = Answerer()
    monkeypatch.setattr(journey.server, "get_extractor", lambda: answerer)
    await ingest(journey.world, journey.extractor)
    result = await journey.server.question_answer(memory_id=MEMORY, question="What happened with Pip?")
    assert result["status"] == "ok", result
    prompt = "\n".join(answerer.prompts)
    assert "Selected anchor: 12 April 2022" in prompt and "Selected anchor: 12 January 2024" in prompt
    assert "Legacy context" not in prompt and "legacy_unannotated" not in prompt
    assert result.get("legacy_context_cost", {}).get("source_reads", 0) == 0  # no original was read
