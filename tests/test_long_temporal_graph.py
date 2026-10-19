"""T04 (#921): temporal occurrences in the Neo4j graph projection.

Structural oracle only. Records come from the real T03 extractor with a
controlled completion; no model semantics are qualified. The recording
session proves the Python-side guards run before any graph effect. The opt-in
case at the end (``HIVEMIND_TEST_NEO4J_URI``) qualifies the Cypher itself on
an isolated Neo4j 5 Community instance; it is not part of required CI.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import copy
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from tests.test_automatic_ontology_state import YAML
from tests.test_graph_document_ownership import _archive
from tests.test_long_temporal_extraction import TEXT, response
from tests.test_ontology_closed_vocabulary import extractor  # noqa: F401 - fixture
from tests.test_p13_qdrant_backup_identity import (
    BACKUP_ID, GRAPH_DATA, _artifact_set, _service as _backup_service, backup_module,  # noqa: F401
)


MEMORY = "temporal-memory"
DOC_A = "doc-a"
DOC_B = "doc-b"
SHA = hashlib.sha256(TEXT.encode()).hexdigest()


async def bound(extractor, doc_id, index=0, memory_id=MEMORY):
    """One real T03 occurrence (session ``index``) bound to a generation."""
    from mcp_memory.core.temporal import ExtractionSource, bind_source
    source = ExtractionSource(TEXT, markdown=True)
    body = response(source, index)
    body["entities"] = [{"name": "Mira", "type": "Device"}, {"name": "Pip", "type": "Device"}]
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=json.dumps(body)))
    result = await extractor.extract_with_ontology(TEXT, YAML, source_context=source)
    return bind_source(result, source, memory_id, doc_id, SHA,
                       {"converter_id": "test", "converter_version": "1"})


class _Result:
    def __init__(self, records):
        self._records = list(records)

    async def single(self):
        return self._records[0] if self._records else None

    async def consume(self):
        return None

    def __aiter__(self):
        async def rows():
            for record in self._records:
                yield record
        return rows()


class _Session:
    """Records every statement; ``responder`` scripts the returned records."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda query, params: [])

    async def run(self, query, **params):
        self.calls.append((str(query), params))
        return _Result(self.responder(str(query), params))


def _graph_service(monkeypatch, session):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
    from tests.fakes.neo4j_fakes import bind_fake_neo4j
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    apply_graph_memory_baseline_env(monkeypatch)
    reset_maintenance_coordinator_for_tests()
    graph = bind_fake_neo4j(monkeypatch)
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(s3_bucket_name="test-bucket"))
    service = object.__new__(graph.GraphService)

    @asynccontextmanager
    async def context():
        yield session

    service.session = context
    return service


def impossible_anchor(row):
    """R1 F2 witness: only anchor.scope changes, so the occurrence ID is unchanged."""
    from mcp_memory.core.temporal import Scope
    anchor = row.event_time.anchor.model_copy(update={"scope": Scope(start=0, end=1)})
    return row.model_copy(update={"event_time": row.event_time.model_copy(update={"anchor": anchor})})


# --- Write path: guards before the first graph effect -----------------------

@pytest.mark.parametrize(("fault", "code"), [
    ("foreign_memory", "foreign_temporal_occurrence"),
    ("foreign_document", "foreign_temporal_occurrence"),
    ("substituted_proof", "temporal_identity_mismatch"),
    ("substituted_version", "temporal_identity_mismatch"),
    ("unknown_triple", "unknown_temporal_fact"),
    ("unbound", "unbound_temporal_occurrence"),
    ("impossible_anchor_scope", "invalid_temporal_association"),
    ("unquoted_quote_length", "invalid_temporal_association"),
    ("narrow_anchor_scope", "invalid_temporal_association"),
    ("disabled_provenance", "disabled_temporal_occurrences"),
])
async def test_occurrence_write_refuses_foreign_or_substituted_records_before_any_graph_write(
        monkeypatch, extractor, fault, code):
    from mcp_memory.core.temporal import TemporalOutputError
    extraction = await bound(extractor, DOC_A)
    row = extraction.occurrences[0]
    ref = row.source_ref
    updates = {
        "foreign_memory": {"source_ref": ref.model_copy(update={"memory_id": "other-memory"})},
        "foreign_document": {"source_ref": ref.model_copy(update={"doc_id": DOC_B})},
        "substituted_proof": {"fact_evidence": [row.fact_evidence[0].model_copy(
            update={"quote": "Mira adopted Pip yesterday."})]},
        "substituted_version": {"source_ref": ref.model_copy(update={"text_sha256": "0" * 64})},
        "unbound": {"occurrence_id": None},
    }
    if fault == "unknown_triple":
        extraction = extraction.model_copy(update={"relations": []})
    elif fault == "disabled_provenance":  # #999: valid records under a generation that never requested them
        extraction = extraction.model_copy(update={"text_provenance": dict(extraction.text_provenance,
                                                                           occurrences="disabled")})
    elif fault == "impossible_anchor_scope":
        extraction = extraction.model_copy(update={"occurrences": [impossible_anchor(row)]})
    elif fault == "unquoted_quote_length":
        # Identity recomputed: only the quote/range consistency rule can refuse it.
        from mcp_memory.core.temporal import occurrence_identity
        span = row.fact_evidence[0]
        bad = row.model_copy(update={"fact_evidence": [span.model_copy(update={"end": span.end + 1})]})
        bad = bad.model_copy(update={"occurrence_id": occurrence_identity(bad, ref)})
        extraction = extraction.model_copy(update={"occurrences": [bad]})
    elif fault == "narrow_anchor_scope":
        # The anchor scope holds its own proof but not the fact (sibling-like).
        from mcp_memory.core.temporal import Scope
        anchor = row.event_time.anchor
        proof = anchor.expression_evidence[0]
        anchor = anchor.model_copy(update={"scope": Scope(start=proof.start, end=proof.end)})
        extraction = extraction.model_copy(update={"occurrences": [row.model_copy(update={
            "event_time": row.event_time.model_copy(update={"anchor": anchor})})]})
    else:
        extraction = extraction.model_copy(update={"occurrences": [row.model_copy(update=updates[fault])]})
    session = _Session()
    service = _graph_service(monkeypatch, session)

    with pytest.raises(TemporalOutputError, match=f"^{code}$"):
        await service.add_entities_and_relations(MEMORY, DOC_A, extraction)
    assert session.calls == []


def _write_responder(stored=None, written=1, linked=1):
    def responder(query, params):
        if "WHERE o.id IN $ids" in query:
            return stored or []
        if "UNWIND $rows AS row" in query:
            return [{"written": written, "linked": linked}]
        if "MERGE (e:Entity" in query:
            return [{"was_created": True}]
        return []
    return responder


@pytest.mark.parametrize("stored", ["other_annotation", "other_document", "identical"])
async def test_existing_record_is_never_overwritten_and_conflict_precedes_entity_writes(
        monkeypatch, extractor, stored):
    from mcp_memory.core.temporal import TemporalOccurrenceConflict, canonical_record
    extraction = await bound(extractor, DOC_A)
    row = extraction.occurrences[0]
    record = canonical_record(row)
    if stored == "other_annotation":
        # Annotations are outside the identity: same ID, different content.
        record = canonical_record(row.model_copy(update={
            "event_time": row.event_time.model_copy(update={"precision": "day"})}))
    existing = [{"id": row.occurrence_id, "record_json": record,
                 "doc_id": DOC_B if stored == "other_document" else DOC_A}]
    session = _Session(_write_responder(existing))
    service = _graph_service(monkeypatch, session)

    if stored != "identical":
        with pytest.raises(TemporalOccurrenceConflict, match="^temporal_occurrence_conflict$"):
            await service.add_entities_and_relations(MEMORY, DOC_A, extraction)
        assert len(session.calls) == 1 and "WHERE o.id IN $ids" in session.calls[0][0]
        return
    result = await service.add_entities_and_relations(MEMORY, DOC_A, extraction)
    assert result["occurrences_stored"] == 1
    write = [params for query, params in session.calls if "UNWIND $rows AS row" in query]
    assert write == [{"rows": [{"id": row.occurrence_id, "from_entity": "Mira", "type": "CONNECTS",
                                "to_entity": "Pip", "record_json": canonical_record(row)}],
                      "doc_id": DOC_A, "memory_id": MEMORY}]
    assert "ON CREATE SET" in session.calls[-1][0] and "ON MATCH" not in session.calls[-1][0]


@pytest.mark.parametrize(("written", "linked", "error"), [
    (0, 0, "temporal_document_unavailable"),
    (1, 0, "temporal_occurrence_conflict"),
])
async def test_unlinked_or_missing_generation_fails_the_candidate(monkeypatch, extractor, written, linked, error):
    from mcp_memory.core.temporal import TemporalOutputError
    extraction = await bound(extractor, DOC_A)
    service = _graph_service(monkeypatch, _Session(_write_responder(written=written, linked=linked)))
    with pytest.raises(TemporalOutputError, match=f"^{error}$"):
        await service.add_entities_and_relations(MEMORY, DOC_A, extraction)


async def test_legacy_extraction_adds_no_temporal_statement(monkeypatch):
    from mcp_memory.core.models import ExtractedEntity, ExtractionResult
    session = _Session(_write_responder())
    service = _graph_service(monkeypatch, session)
    result = await service.add_entities_and_relations(
        MEMORY, DOC_A, ExtractionResult(entities=[ExtractedEntity(name="Mira", type="Device")]))
    assert result["occurrences_stored"] == 0
    assert all("TemporalOccurrence" not in query for query, _ in session.calls)


# --- Bounded read (contract §5) ---------------------------------------------

def _synthetic(base, index, quote="x", to_entity="Pip"):
    from mcp_memory.core.temporal import Scope, Span, canonical_record, occurrence_identity
    wide = Scope(start=0, end=10**7)
    anchor = base.event_time.anchor.model_copy(update={"scope": wide})
    row = base.model_copy(update={
        "scope": wide, "event_time": base.event_time.model_copy(update={"anchor": anchor}),
        "fact": base.fact.model_copy(update={"to_entity": to_entity}),
        "fact_evidence": [Span(start=100 + index, end=100 + index + len(quote), quote=quote)]})
    row = row.model_copy(update={"occurrence_id": occurrence_identity(row, row.source_ref)})
    return row.occurrence_id, row.source_ref.doc_id, canonical_record(row)


def _read_responder(store, limits):
    def responder(query, params):
        assert "ingestion_status: 'succeeded'" in query
        limits.append(params["limit"])
        rows = sorted(store.get(params["to_entity"], []))
        return [{"id": i, "doc_id": d, "record_json": r} for i, d, r in rows[:params["limit"]]]
    return responder


async def test_relation_read_selects_ascending_ids_under_every_bound(monkeypatch, extractor):
    from mcp_memory.core import temporal
    base = (await bound(extractor, DOC_A)).occurrences[0]
    # Isolate the count bounds; the byte bound has its own case below.
    monkeypatch.setattr(temporal, "OCCURRENCE_BYTES_PER_RESPONSE", 10**9)
    store = {"R0": [_synthetic(base, i, to_entity="R0") for i in range(40)]}
    for r in range(1, 5):
        store[f"R{r}"] = [_synthetic(base, i, to_entity=f"R{r}") for i in range(30)]
    limits = []
    service = _graph_service(monkeypatch, _Session(_read_responder(store, limits)))

    result = await service.get_relation_occurrences(
        MEMORY, [("Mira", "CONNECTS", f"R{r}") for r in range(5)] + [("Mira", "CONNECTS", "none")])

    assert set(limits) == {33}
    rows = result["relations"]
    assert rows[0]["occurrence_ids"] == sorted(i for i, _, _ in store["R0"])[:32]
    assert rows[0]["occurrences_partial"] is True
    assert [len(r["occurrence_ids"]) for r in rows[1:5]] == [30, 30, 30, 6]
    assert [r["occurrences_partial"] for r in rows[1:5]] == [False, False, False, True]
    assert sum(len(r["occurrence_ids"]) for r in rows) == 128
    assert rows[5] == {"from_entity": "Mira", "type": "CONNECTS", "to_entity": "none",
                       "occurrence_ids": [], "temporal_annotations": [], "occurrences_partial": False}
    for relation in rows:
        assert [a["occurrence_id"] for a in relation["temporal_annotations"]] == relation["occurrence_ids"]
    assert result["partial"] is True and result["warnings"] == ["occurrence_budget_exceeded"]


async def test_relation_read_counts_repeated_payload_bytes_and_omits_whole_records(monkeypatch, extractor):
    base = (await bound(extractor, DOC_A)).occurrences[0]
    store = {"Pip": [_synthetic(base, i, quote="q" * 6000) for i in range(6)]}
    size = len(store["Pip"][0][2].encode())
    service = _graph_service(monkeypatch, _Session(_read_responder(store, [])))

    result = await service.get_relation_occurrences(MEMORY, [("Mira", "CONNECTS", "Pip")] * 2)

    first, second = result["relations"]
    assert len(first["occurrence_ids"]) == 6 and first["occurrences_partial"] is False
    fits = 65_536 // size - 6
    assert second["occurrence_ids"] == first["occurrence_ids"][:fits] and second["occurrences_partial"] is True
    assert result["warnings"] == ["occurrence_budget_exceeded"]


@pytest.mark.parametrize("corruption", ["not_json", "unknown_version", "foreign_id", "foreign_document",
                                        "impossible_anchor_scope"])
async def test_relation_read_never_presents_an_invalid_record(monkeypatch, extractor, corruption):
    base = (await bound(extractor, DOC_A)).occurrences[0]
    good = _synthetic(base, 1)
    bad_id, bad_doc, bad_json = _synthetic(base, 2)
    if corruption == "not_json":
        bad_json = bad_json[:-1]
    elif corruption == "unknown_version":
        bad_json = bad_json.replace('"temporal_schema_version":1', '"temporal_schema_version":2')
    elif corruption == "foreign_id":
        bad_id = good[0]
    elif corruption == "impossible_anchor_scope":
        from mcp_memory.core.temporal import Occurrence, canonical_record
        bad_json = canonical_record(impossible_anchor(Occurrence.model_validate_json(bad_json)))
    else:
        bad_doc = DOC_B
    store = {"Pip": [good, (bad_id, bad_doc, bad_json)]}
    service = _graph_service(monkeypatch, _Session(_read_responder(store, [])))

    result = await service.get_relation_occurrences(MEMORY, [("Mira", "CONNECTS", "Pip")])

    relation = result["relations"][0]
    assert good[0] in relation["occurrence_ids"] and len(relation["occurrence_ids"]) == 1
    assert relation["occurrences_partial"] is True
    assert result["warnings"] == ["unsupported_temporal_schema"]


# --- Export / import ---------------------------------------------------------

def _export_responder(rows):
    def responder(query, params):
        if "MATCH (m:Memory {id: $id})" in query:
            return [{"m": {"id": MEMORY, "name": "M", "ontology": "general"}}]
        if "HAS_TEMPORAL_OCCURRENCE" in query:
            return [{"id": i, "doc_id": d, "record_json": r} for i, d, r in rows]
        return []
    return responder


async def test_export_declares_occurrences_only_when_present(monkeypatch, extractor):
    from mcp_memory.core.temporal import canonical_record
    row = (await bound(extractor, DOC_A)).occurrences[0]
    legacy = await _graph_service(monkeypatch, _Session(_export_responder([]))).export_memory_data(MEMORY)
    assert list(legacy) == ["memory", "documents", "entities", "relations", "mentions"]

    service = _graph_service(monkeypatch, _Session(_export_responder(
        [(row.occurrence_id, DOC_A, canonical_record(row))])))
    exported = await service.export_memory_data(MEMORY)
    assert exported["temporal_schema_version"] == 1
    assert exported["occurrences"] == [row.model_dump(mode="json")]

    tampered = canonical_record(row).replace('"Mira"', '"Mallory"')
    service = _graph_service(monkeypatch, _Session(_export_responder([(row.occurrence_id, DOC_A, tampered)])))
    with pytest.raises(ValueError, match="^temporal occurrence record is invalid$"):
        await service.export_memory_data(MEMORY)


def _temporal_export(rows, memory_id=MEMORY):
    return {
        "memory": {"id": memory_id, "name": "M", "ontology": "general"},
        "documents": [{"id": DOC_A, "memory_id": memory_id, "uri": "", "filename": "a.md", "hash": SHA,
                       "ingestion_status": "succeeded", "chunk_count": 2, "last_ingest_job_id": "job-1"}],
        "entities": [], "relations": [], "mentions": [],
        "temporal_schema_version": 1,
        "occurrences": [row.model_dump(mode="json") for row in rows],
    }


@pytest.mark.parametrize("fault", [
    "remapped_namespace", "missing_document", "source_version", "identity", "extra_field",
    "duplicate", "version_bool", "version_two", "version_text", "undeclared", "not_a_list",
    "lifecycle_type", "impossible_anchor_scope", "unsorted_proofs", "quote_length", "null_record",
])
async def test_import_refuses_invalid_temporal_state_before_any_write(monkeypatch, extractor, fault):
    row = (await bound(extractor, DOC_A)).occurrences[0]
    data = _temporal_export([row])
    record = data["occurrences"][0]
    if fault == "remapped_namespace":
        data = _temporal_export([row], memory_id="other-memory")
    elif fault == "missing_document":
        data["documents"] = []
    elif fault == "source_version":
        data["documents"][0]["hash"] = "f" * 64
    elif fault == "identity":
        record["occurrence_id"] = "occ1:" + "0" * 64
    elif fault == "extra_field":
        record["confidence"] = 0.9
    elif fault == "duplicate":
        data["occurrences"].append(copy.deepcopy(record))
    elif fault == "version_bool":
        data["temporal_schema_version"] = True
    elif fault == "version_two":
        data["temporal_schema_version"] = 2
    elif fault == "version_text":
        data["temporal_schema_version"] = "1"
    elif fault == "undeclared":
        del data["temporal_schema_version"]
    elif fault == "not_a_list":
        data["occurrences"] = {"0": record}
    elif fault == "lifecycle_type":
        data["documents"][0]["chunk_count"] = "2"
    elif fault == "impossible_anchor_scope":
        data = _temporal_export([impossible_anchor(row)])
    elif fault == "unsorted_proofs":
        record["fact_evidence"] = list(reversed(record["fact_evidence"] + [
            {"start": 0, "end": 11, "quote": "# Session A"}]))
    elif fault == "quote_length":
        record["fact_evidence"][0]["end"] += 1
    else:
        data["occurrences"] = [None]
    session = _Session()
    service = _graph_service(monkeypatch, session)
    service.get_memory = AsyncMock(return_value=None)

    with pytest.raises(ValueError):
        await service.import_memory_data(data)
    assert session.calls == []
    service.get_memory.assert_not_awaited()


async def test_import_restores_records_and_publication_state(monkeypatch, extractor):
    from mcp_memory.core.temporal import canonical_record
    row = (await bound(extractor, DOC_A)).occurrences[0]

    def responder(query, params):
        return [{"created": len(params["rows"])}] if "RETURN count(o) AS created" in query else []
    session = _Session(responder)
    service = _graph_service(monkeypatch, session)
    service.get_memory = AsyncMock(return_value=None)

    counters = await service.import_memory_data(_temporal_export([row]))

    assert counters["occurrences"] == 1
    states = [p["states"] for q, p in session.calls if "UNWIND $states AS state" in q]
    assert states == [[{"id": DOC_A, "ingestion_status": "succeeded", "chunk_count": 2,
                        "last_ingest_job_id": "job-1", "extracted_text_json": None}]]
    created = [p["rows"] for q, p in session.calls if "RETURN count(o) AS created" in q]
    assert created == [[{"id": row.occurrence_id, "doc_id": DOC_A, "from_entity": "Mira", "type": "CONNECTS",
                         "to_entity": "Pip", "record_json": canonical_record(row)}]]

    legacy_session = _Session()
    legacy = _graph_service(monkeypatch, legacy_session)
    legacy.get_memory = AsyncMock(return_value=None)
    data = _temporal_export([])
    del data["temporal_schema_version"], data["occurrences"]
    assert "occurrences" not in await legacy.import_memory_data(data)
    assert all("TemporalOccurrence" not in q and "$states" not in q for q, _ in legacy_session.calls)


# --- Backup format 1.1 -------------------------------------------------------

def _artifacts(graph_data, **manifest_updates):
    artifacts = _artifact_set()
    graph_bytes = json.dumps(graph_data, ensure_ascii=False, indent=2).encode()
    artifacts["graph_data.json"] = graph_bytes
    manifest = json.loads(artifacts["manifest.json"])
    manifest["checksums"]["graph_data"] = hashlib.sha256(graph_bytes).hexdigest()
    for key, value in manifest_updates.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    artifacts["manifest.json"] = json.dumps(manifest).encode()
    return artifacts


TEMPORAL_GRAPH = dict(GRAPH_DATA, temporal_schema_version=1, occurrences=[])


@pytest.mark.parametrize("route", ["s3", "archive"])
@pytest.mark.parametrize(("case", "graph_data", "manifest"), [
    ("temporal", TEMPORAL_GRAPH, {"version": "1.1", "temporal_schema_version": 1}),
    ("undeclared_manifest", TEMPORAL_GRAPH, {"version": "1.1"}),
    ("boolean_declaration", TEMPORAL_GRAPH, {"version": "1.1", "temporal_schema_version": True}),
    ("undeclared_graph", GRAPH_DATA, {"version": "1.1", "temporal_schema_version": 1}),
    ("smuggled_in_legacy", TEMPORAL_GRAPH, {"version": "1.0"}),
    ("declared_legacy", GRAPH_DATA, {"version": "1.0", "temporal_schema_version": 1}),
    ("unknown_version", TEMPORAL_GRAPH, {"version": "1.2", "temporal_schema_version": 1}),
])
async def test_restore_accepts_only_declared_backup_formats_before_effects(
        backup_module, route, case, graph_data, manifest):
    artifacts = _artifacts(graph_data, **manifest)
    service, operations, _ = _backup_service(backup_module, artifacts=artifacts if route == "s3" else None)

    async def restore():
        if route == "s3":
            return await service.restore_backup(BACKUP_ID)
        return await service.restore_from_archive(_archive(artifacts))

    if case == "temporal":
        assert (await restore())["status"] == "ok"
        assert "graph.import" in operations
        return
    with pytest.raises(ValueError, match="Incompatible backup version|temporal schema declaration"):
        await restore()
    assert operations == []


@pytest.mark.parametrize("route", ["s3", "archive"])
@pytest.mark.parametrize("contents", ["null_record", "impossible_anchor_scope", "remapped_namespace"])
async def test_restore_validates_occurrence_contents_before_any_storage_effect(
        backup_module, extractor, route, contents):
    """R1 F1: archive document uploads happen before graph import."""
    from tests.test_p13_qdrant_backup_identity import MEMORY_ID
    row = (await bound(extractor, DOC_A, memory_id=MEMORY_ID)).occurrences[0]
    graph_data = _temporal_export([row], memory_id=MEMORY_ID)
    graph_data["documents"] = [dict(graph_data["documents"][0], filename="document.txt")]
    if contents == "null_record":
        graph_data["occurrences"] = [None]
    elif contents == "impossible_anchor_scope":
        graph_data["occurrences"] = [impossible_anchor(row).model_dump(mode="json")]
    else:
        graph_data["occurrences"] = [(await bound(extractor, DOC_A)).occurrences[0].model_dump(mode="json")]
    artifacts = _artifacts(graph_data, version="1.1", temporal_schema_version=1)
    service, operations, objects = _backup_service(backup_module, artifacts=artifacts if route == "s3" else None)
    before = dict(objects)

    with pytest.raises(ValueError, match="^backup temporal occurrence is invalid$"):
        if route == "s3":
            await service.restore_backup(BACKUP_ID)
        else:
            await service.restore_from_archive(_archive(dict(artifacts, **{"documents/document.txt": b"new"})))
    assert operations == [] and objects == before


@pytest.mark.parametrize("temporal", [False, True])
async def test_backup_uses_format_1_1_only_for_occurrence_bearing_exports(backup_module, temporal):
    service, _, objects = _backup_service(backup_module, memory_exists=True)
    graph_data = dict(GRAPH_DATA, temporal_schema_version=1, occurrences=[{"occurrence": 1}]) if temporal else GRAPH_DATA
    service._graph.export_memory_data = AsyncMock(return_value=graph_data)

    result = await service.create_backup("memory-one")

    manifest = json.loads(objects[next(k for k in objects if k.endswith("/manifest.json"))])
    assert manifest["version"] == ("1.1" if temporal else "1.0")
    assert ("temporal_schema_version" in manifest) is temporal
    assert ("occurrences" in manifest["stats"]) is temporal
    if temporal:
        assert manifest["temporal_schema_version"] == 1 and manifest["stats"]["occurrences"] == 1
    service._storage._client.delete_object = lambda **_: None
    assert (await service.delete_backup(result["backup_id"]))["files_deleted"] == 4


# --- Opt-in real Neo4j qualification ----------------------------------------

NEO4J_URI = os.environ.get("HIVEMIND_TEST_NEO4J_URI")


@pytest.mark.skipif(not NEO4J_URI, reason="set HIVEMIND_TEST_NEO4J_URI to an isolated Neo4j 5 instance")
async def test_real_neo4j_lifecycle_restart_and_round_trip(monkeypatch, extractor):
    """Cypher qualification on an isolated, disposable Neo4j database."""
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.temporal import TemporalOccurrenceConflict
    import neo4j

    apply_graph_memory_baseline_env(monkeypatch)
    reset_maintenance_coordinator_for_tests()
    for name in ("AsyncGraphDatabase", "AsyncDriver", "AsyncSession", "Query"):
        monkeypatch.setattr(graph_module, name, getattr(neo4j, name))
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(
        neo4j_uri=NEO4J_URI, neo4j_user=os.environ.get("HIVEMIND_TEST_NEO4J_USER", "neo4j"),
        neo4j_password=os.environ["HIVEMIND_TEST_NEO4J_PASSWORD"], neo4j_database="neo4j",
        neo4j_query_timeout_seconds=30, s3_bucket_name="test-bucket"))
    memory = "t04-" + uuid.uuid4().hex[:8]
    doc_a, doc_b, doc_c, doc_d = (f"{memory}-{x}" for x in "abcd")

    async def document(service, doc_id, status):
        await service.add_document(memory_id=memory, doc_id=doc_id, uri="", filename=doc_id + ".md",
                                   doc_hash=SHA, ingestion_status=status)

    async def read(service):
        result = await service.get_relation_occurrences(memory, [("Mira", "CONNECTS", "Pip")])
        relation = result["relations"][0]
        return {a["source_ref"]["doc_id"]: a["event_time"]["anchor"]["expression"]
                for a in relation["temporal_annotations"]}

    async def stored(service):
        async with service.session() as session:
            result = await session.run(
                "MATCH (o:TemporalOccurrence {memory_id: $m}) RETURN o.doc_id AS d, o.record_json AS r",
                m=memory)
            return {(r["d"], r["r"]) async for r in result}

    service = graph_module.GraphService()
    try:
        await service.initialize_document_schema()
        await service.ensure_document_lookup_index()
        await service.create_memory(memory, "T04 qualification")
        a, b = await bound(extractor, doc_a, 0, memory), await bound(extractor, doc_b, 1, memory)

        # Two sessions, same triple, different anchors: distinct, both published.
        await document(service, doc_a, "succeeded")
        await service.add_entities_and_relations(memory, doc_a, a)
        await document(service, doc_b, "running")
        await service.add_entities_and_relations(memory, doc_b, b)
        assert await read(service) == {doc_a: "12 April 2022"}  # candidate invisible
        await service.update_document_ingestion(memory, doc_b, "succeeded")
        assert await read(service) == {doc_a: "12 April 2022", doc_b: "12 January 2024"}

        # Same-generation replay is idempotent; a conflicting replay changes nothing.
        before = await stored(service)
        await service.add_entities_and_relations(memory, doc_a, a)
        assert await stored(service) == before
        row = a.occurrences[0]
        conflicting = a.model_copy(update={"occurrences": [row.model_copy(update={
            "event_time": row.event_time.model_copy(update={"precision": "day"})})]})
        with pytest.raises(TemporalOccurrenceConflict):
            await service.add_entities_and_relations(memory, doc_a, conflicting)
        assert await stored(service) == before

        # Safe replace of A by C: candidate hidden, promotion swaps, purge is scoped.
        c = await bound(extractor, doc_c, 0, memory)
        await document(service, doc_c, "running")
        await service.add_entities_and_relations(memory, doc_c, c)
        assert set(await read(service)) == {doc_a, doc_b}
        assert await service.promote_candidate_document(memory, doc_a, doc_c, "a.md", 0)
        assert set(await read(service)) == {doc_b, doc_c}
        assert (await service.delete_document(memory, doc_a))["occurrences_deleted"] == 1
        assert {d for d, _ in await stored(service)} == {doc_b, doc_c}

        # Rollback/cancellation of a candidate removes only that generation.
        await document(service, doc_d, "running")
        await service.add_entities_and_relations(memory, doc_d, await bound(extractor, doc_d, 1, memory))
        assert (await service.delete_document(memory, doc_d))["occurrences_deleted"] == 1
        assert {d for d, _ in await stored(service)} == {doc_b, doc_c}
        published = await read(service)
    finally:
        await service.close()

    restarted = graph_module.GraphService()
    try:
        assert await read(restarted) == published
        exported = await restarted.export_memory_data(memory)
        assert [o["source_ref"]["doc_id"] for o in exported["occurrences"]] == sorted([doc_b, doc_c])
        records = await stored(restarted)
        await restarted.delete_memory(memory)
        assert await stored(restarted) == set()
        counters = await restarted.import_memory_data(exported)
        assert counters["occurrences"] == 2
        assert await stored(restarted) == records
        assert await read(restarted) == published
        again = await restarted.export_memory_data(memory)
        assert again["occurrences"] == exported["occurrences"]
        assert [(d["id"], d["ingestion_status"]) for d in again["documents"]] == [
            (d["id"], d["ingestion_status"]) for d in exported["documents"]]

        # A corrupted stored record is never presented, and blocks a backup.
        async with restarted.session() as session:
            await (await session.run(
                "MATCH (o:TemporalOccurrence {memory_id: $m, doc_id: $d}) SET o.record_json = $r",
                m=memory, d=doc_b, r="{")).consume()
        result = await restarted.get_relation_occurrences(memory, [("Mira", "CONNECTS", "Pip")])
        assert result["warnings"] == ["unsupported_temporal_schema"]
        assert [a["source_ref"]["doc_id"] for a in result["relations"][0]["temporal_annotations"]] == [doc_c]
        with pytest.raises(ValueError, match="temporal occurrence record is invalid"):
            await restarted.export_memory_data(memory)
    finally:
        await restarted.delete_memory(memory)
        await restarted.close()


@pytest.mark.skipif(not NEO4J_URI, reason="set HIVEMIND_TEST_NEO4J_URI to an isolated Neo4j 5 instance")
async def test_real_neo4j_pipeline_replace_and_cancellation_follow_published_generation(monkeypatch, extractor):
    """Actual pipeline + GraphService; only provider, S3, chunker and Qdrant are doubled."""
    from unittest.mock import MagicMock, patch
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env, enable_occurrence_extraction
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.temporal import ExtractionSource
    import neo4j

    apply_graph_memory_baseline_env(monkeypatch)
    enable_occurrence_extraction(monkeypatch)  # the T04 occurrence path (#999)
    reset_maintenance_coordinator_for_tests()
    for name in ("AsyncGraphDatabase", "AsyncDriver", "AsyncSession", "Query"):
        monkeypatch.setattr(graph_module, name, getattr(neo4j, name))
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(
        neo4j_uri=NEO4J_URI, neo4j_user=os.environ.get("HIVEMIND_TEST_NEO4J_USER", "neo4j"),
        neo4j_password=os.environ["HIVEMIND_TEST_NEO4J_PASSWORD"], neo4j_database="neo4j",
        neo4j_query_timeout_seconds=30, s3_bucket_name="test-bucket"))
    memory = "t04p-" + uuid.uuid4().hex[:8]
    sessions = iter([0, 1, 1])

    async def completion(messages):
        body = response(ExtractionSource(TEXT, markdown=True), next(sessions))
        body["entities"] = [{"name": "Mira", "type": "Device"}, {"name": "Pip", "type": "Device"}]
        return SimpleNamespace(text=json.dumps(body))
    extractor._complete = AsyncMock(side_effect=completion)
    storage = MagicMock()
    storage.upload_document = AsyncMock(return_value={"uri": "", "size_bytes": len(TEXT)})
    storage.delete_document = AsyncMock(return_value=True)
    storage.put_extracted_text = AsyncMock(return_value="")  # T04b text retention, see its own module
    storage.delete_extracted_text = AsyncMock(return_value=True)
    vectors = MagicMock()
    vectors.store_chunks = AsyncMock(return_value=0)
    vectors.delete_document_chunks = AsyncMock(return_value=0)
    chunker = MagicMock()
    chunker.chunk_document = MagicMock(return_value=[])
    service = graph_module.GraphService()

    async def visible():
        result = await service.get_relation_occurrences(memory, [("Mira", "CONNECTS", "Pip")])
        return [(a["source_ref"]["doc_id"], a["event_time"]["anchor"]["expression"])
                for a in result["relations"][0]["temporal_annotations"]]

    async def owners():
        async with service.session() as session:
            result = await session.run(
                "MATCH (o:TemporalOccurrence {memory_id: $m}) RETURN DISTINCT o.doc_id AS d", m=memory)
            return {r["d"] async for r in result}

    try:
        await service.initialize_document_schema()
        await service.create_memory(memory, "T04 pipeline qualification")
        with patch("mcp_memory.core.ingest_pipeline._graph", return_value=service), \
             patch("mcp_memory.core.ingest_pipeline._storage", return_value=storage), \
             patch("mcp_memory.core.ingest_pipeline._extractor", return_value=extractor), \
             patch("mcp_memory.core.ingest_pipeline._chunker", return_value=chunker), \
             patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=vectors), \
             patch("mcp_memory.server._extract_text", return_value=TEXT):
            async def ingest(**kwargs):
                return await run_ingest_pipeline(memory_id=memory, content=TEXT.encode(), filename="a.md",
                                                 doc_hash=SHA, source_path="a.md", ontology=YAML, **kwargs)
            first = await ingest()
            assert first["status"] == "ok"
            assert await visible() == [(first["document_id"], "12 April 2022")]

            checks = iter(range(100))
            cancelled = await ingest(replace_doc_id=first["document_id"],
                                     cancel_check=lambda: next(checks) == 5)  # before_promote
            assert cancelled["status"] == "cancelled"
            assert await visible() == [(first["document_id"], "12 April 2022")]
            assert await owners() == {first["document_id"]}

            replaced = await ingest(replace_doc_id=first["document_id"])
            assert replaced["status"] == "ok" and replaced["purge_status"] == "purged"
            assert await visible() == [(replaced["document_id"], "12 January 2024")]
            assert await owners() == {replaced["document_id"]}
    finally:
        await service.delete_memory(memory)
        await service.close()


@pytest.mark.skipif(not NEO4J_URI, reason="set HIVEMIND_TEST_NEO4J_URI to an isolated Neo4j 5 instance")
async def test_real_neo4j_reindex_reads_page_one_published_generation_only(monkeypatch, extractor):
    """T05 (#922): the Cypher behind the reindex annotation reads, on a disposable database."""
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.temporal import extracted_text_record
    import neo4j

    apply_graph_memory_baseline_env(monkeypatch)
    reset_maintenance_coordinator_for_tests()
    for name in ("AsyncGraphDatabase", "AsyncDriver", "AsyncSession", "Query"):
        monkeypatch.setattr(graph_module, name, getattr(neo4j, name))
    monkeypatch.setattr(graph_module, "get_settings", lambda: SimpleNamespace(
        neo4j_uri=NEO4J_URI, neo4j_user=os.environ.get("HIVEMIND_TEST_NEO4J_USER", "neo4j"),
        neo4j_password=os.environ["HIVEMIND_TEST_NEO4J_PASSWORD"], neo4j_database="neo4j",
        neo4j_query_timeout_seconds=30, s3_bucket_name="test-bucket"))
    monkeypatch.setattr(graph_module, "REINDEX_OCCURRENCE_PAGE", 1)  # every record is its own page
    memory = "t05-" + uuid.uuid4().hex[:8]
    doc_a, doc_b, doc_c = (f"{memory}-{x}" for x in "abc")

    async def two_sessions(doc_id):
        first, second = await bound(extractor, doc_id, 0, memory), await bound(extractor, doc_id, 1, memory)
        return first.model_copy(update={"occurrences": first.occurrences + second.occurrences})

    service = graph_module.GraphService()
    try:
        await service.initialize_document_schema()
        await service.create_memory(memory, "T05 qualification")
        extraction = await two_sessions(doc_a)
        record = extracted_text_record(extraction.text_provenance, memory, doc_a)
        await service.add_document(memory_id=memory, doc_id=doc_a, uri="", filename="a.md", doc_hash=SHA,
                                   ingestion_status="succeeded", extracted_text=record)
        await service.add_entities_and_relations(memory, doc_a, extraction)
        await service.add_document(memory_id=memory, doc_id=doc_b, uri="", filename="b.md", doc_hash=SHA,
                                   ingestion_status="running")
        await service.add_entities_and_relations(memory, doc_b, await bound(extractor, doc_b, 0, memory))
        await service.add_document(memory_id=memory, doc_id=doc_c, uri="", filename="c.md", doc_hash=SHA,
                                   ingestion_status="succeeded")

        assert await service.list_reindex_text_records(memory) == {
            doc_a: json.dumps(record, sort_keys=True, separators=(",", ":"))}
        read = await service.list_reindex_occurrences(memory, doc_a)
        assert [o.occurrence_id for o in read] == sorted(o.occurrence_id for o in extraction.occurrences)
        assert {o.event_time.anchor.expression for o in read} == {"12 April 2022", "12 January 2024"}
        assert await service.list_reindex_occurrences(memory, doc_b) == []  # candidate generation stays invisible
        assert await service.list_reindex_occurrences(memory, doc_c) == []  # legacy document: none stored
        assert await service.list_reindex_occurrences(memory, "unknown") == []
    finally:
        await service.delete_memory(memory)
        await service.close()
