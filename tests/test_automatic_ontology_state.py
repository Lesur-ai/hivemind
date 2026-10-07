"""Automatic catalogue checkpoints stay scoped, immutable after freeze and restorable."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from neo4j.time import DateTime as Neo4jDateTime

CREATED = datetime(2026, 9, 16, tzinfo=timezone.utc)
BATCH = "a" * 64
YAML = '''name: example
version: "1"
description: Synthetic catalogue
entity_types:
  - name: Device
    description: An identified physical device
relation_types:
  - name: CONNECTS
    description: A directed physical connection
'''


@pytest.fixture
def graph_env(monkeypatch):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
    from tests.fakes.neo4j_fakes import bind_fake_neo4j
    from mcp_memory.core.maintenance import reset_maintenance_coordinator_for_tests
    apply_graph_memory_baseline_env(monkeypatch)
    reset_maintenance_coordinator_for_tests()
    module = bind_fake_neo4j(monkeypatch)
    node = {"id": "test", "created_at": CREATED, "ontology": "general",
            "ontology_uri": "s3://synthetic/general.yaml"}
    state = SimpleNamespace(node=node, documents=0, entities=0, queries=[], before_write=None,
                            reindex_documents=[], driver_utc_alias=False)

    class Result:
        def __init__(self, record, rows=()):
            self.single = AsyncMock(return_value=record)
            self.rows = rows

        def __aiter__(self):
            # This backend fixture models Memory properties and conditional
            # writes, with explicit retained-document rows for reindex.
            async def rows():
                for record in self.rows:
                    yield record
            return rows()

    class Session:
        async def run(self, query, **params):
            state.queries.append((query, params))
            if "RETURN m.ontology_uri as ontology_uri" in query:
                return Result({"ontology_uri": state.node.get("ontology_uri")} if state.node else None)
            if "RETURN d.memory_id as memory_id" in query:
                return Result(None, deepcopy(state.reindex_documents[:params["limit"]]))
            if "SET m.automatic_ontology_json" in query:
                if state.before_write:
                    state.before_write()
                # Model the conditional Cypher write, not the Python guard.
                admissible = state.node is not None
                if "m.created_at = datetime($expected_created_at)" in query:
                    admissible &= state.node["created_at"].isoformat() == params["expected_created_at"]
                if "expected_node_created_at" in params:
                    stored = state.node["created_at"]
                    expected = params["expected_node_created_at"]
                    # Real Bolt round-trip: offset UTC can return as named UTC.
                    # Neo4j zoned equality distinguishes those representations;
                    # Python datetime equality alone would miss the defect.
                    if state.driver_utc_alias:
                        expected = expected.replace(tzinfo=ZoneInfo("UTC"))
                    if "m.created_at = $expected_node_created_at" in query:
                        admissible &= (stored == expected and
                            getattr(stored.tzinfo, "key", None) == getattr(expected.tzinfo, "key", None))
                    if "m.created_at.epochSeconds = $expected_node_created_at.epochSeconds" in query:
                        def epoch_seconds(value):
                            native = value.to_native() if isinstance(value, Neo4jDateTime) else value
                            return int((native.replace(microsecond=0) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
                        admissible &= epoch_seconds(stored) == epoch_seconds(expected)
                    if "m.created_at.nanosecond = $expected_node_created_at.nanosecond" in query:
                        def nanoseconds(value):
                            return value.nanosecond if isinstance(value, Neo4jDateTime) else value.microsecond * 1000
                        admissible &= nanoseconds(stored) == nanoseconds(expected)
                if "NOT EXISTS { MATCH (:Document" in query:
                    admissible &= state.documents == 0
                if "NOT EXISTS { MATCH (:Entity" in query:
                    admissible &= state.entities == 0
                if "coalesce(m.automatic_ontology_json, '') = $previous" in query:
                    admissible &= (state.node.get("automatic_ontology_json") or "") == params["previous"]
                if admissible:
                    state.node["automatic_ontology_json"] = params["state_json"]
                    state.node["ontology"] = params["ontology"]
                    if "m.ontology_uri = $ontology_uri" in query:
                        if params["ontology_uri"] is None:
                            state.node.pop("ontology_uri", None)
                        else:
                            state.node["ontology_uri"] = params["ontology_uri"]
                return Result({"m": state.node} if admissible else None)
            return Result({"m": deepcopy(state.node)} if state.node else None)

    @asynccontextmanager
    async def session():
        yield Session()

    graph = object.__new__(module.GraphService)
    graph.session = session
    yield graph, state, module
    reset_maintenance_coordinator_for_tests()


async def test_checkpoint_reload_freeze_and_exact_replay(graph_env):
    graph, backend, _ = graph_env
    assert await graph.load_automatic_ontology("test") is None
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint={"D1": ["complete"]})
    saved = await graph.load_automatic_ontology("test")
    assert saved["checkpoint"] == {"D1": ["complete"]}
    assert saved["ontology_yaml"] is None
    assert backend.node["ontology"] == "general"
    assert backend.node["ontology_uri"] == "s3://synthetic/general.yaml"
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint={"D1": ["complete"], "D2": ["complete"]}, ontology_yaml=YAML)
    frozen = await graph.load_automatic_ontology("test")
    assert frozen["ontology_yaml"] == YAML
    assert backend.node["ontology"].startswith("auto_")
    assert YAML not in backend.node["ontology"]
    assert backend.node.get("ontology_uri") is None
    backend.documents = 3
    writes = sum("SET m.automatic_ontology_json" in q for q, _ in backend.queries)
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint=frozen["checkpoint"], ontology_yaml=YAML)
    assert writes == sum("SET m.automatic_ontology_json" in q for q, _ in backend.queries)


async def test_cancelled_preparation_stops_before_parsing_or_inference(graph_env, monkeypatch):
    from mcp_memory.core import automatic_ontology as automatic
    from mcp_memory.core.ingest_pipeline import IngestCancelled
    from mcp_memory.core.maintenance import get_maintenance_coordinator
    graph, backend, _ = graph_env
    graph.get_memory = AsyncMock(return_value=SimpleNamespace(created_at=CREATED))
    graph.get_memory_stats = AsyncMock(return_value=SimpleNamespace(
        document_count=0, entity_count=0, relation_count=0))
    monkeypatch.setattr(automatic, "get_graph_service", lambda: graph)
    parsing = AsyncMock(side_effect=AssertionError("cancelled batch must not parse"))
    runtime = Mock(side_effect=AssertionError("cancelled batch must not infer"))
    monkeypatch.setattr(automatic.asyncio, "to_thread", parsing)
    monkeypatch.setattr(automatic, "get_inference_runtime", runtime)
    batch = automatic.AutomaticOntologyBatch(CREATED.isoformat(), BATCH, [
        {"source_path": "manual.md", "filename": "manual.md", "content": b"Synthetic"},
    ])
    with pytest.raises(IngestCancelled):
        await batch.prepare("test", cancel_check=lambda: True)
    parsing.assert_not_awaited()
    runtime.assert_not_called()
    assert "automatic_ontology_json" not in backend.node
    assert batch.documents == []
    assert batch._failure_code == "automatic_ontology_interrupted_resubmit_batch"
    async with get_maintenance_coordinator().ordinary("test"):
        pass  # Cancellation has released the exclusive maintenance admission.


@pytest.mark.parametrize("driver_utc_alias", [False, True])
@pytest.mark.parametrize("changed", [None, "nanosecond", "instant"])
async def test_assignment_preserves_neo4j_timestamp_nanoseconds(graph_env, changed, driver_utc_alias):
    graph, backend, _ = graph_env
    exact = Neo4jDateTime(2026, 9, 16, 0, 0, 0, 123456789, tzinfo=timezone.utc)
    backend.node["created_at"] = exact
    backend.driver_utc_alias = driver_utc_alias
    if changed:
        replacement = (exact.replace(nanosecond=123456999) if changed == "nanosecond"
                       else exact.replace(second=1))
        if changed == "nanosecond":
            assert replacement.to_native() == exact.to_native()
        backend.before_write = lambda: backend.node.update(created_at=replacement)
        with pytest.raises(ValueError, match="assignment refused"):
            await graph.save_automatic_ontology("test", expected_created_at=exact.to_native().isoformat(), batch_fingerprint=BATCH, checkpoint={})
        assert "automatic_ontology_json" not in backend.node
    else:
        await graph.save_automatic_ontology("test", expected_created_at=exact.to_native().isoformat(), batch_fingerprint=BATCH, checkpoint={})
        assert (await graph.load_automatic_ontology("test"))["checkpoint"] == {}
    write = next(params for query, params in backend.queries if "SET m.automatic_ontology_json" in query)
    assert write["expected_node_created_at"] == exact
    assert write["expected_node_created_at"].nanosecond == 123456789


@pytest.mark.parametrize("fault", ["populated", "entities", "recreated", "other_batch", "changed_after_freeze", "invalid_yaml"])
async def test_assignment_guards_preserve_prior_state(graph_env, fault):
    graph, backend, _ = graph_env
    args = dict(expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint={"D1": []})
    await graph.save_automatic_ontology("test", **args)
    if fault == "populated":
        backend.documents = 1
    elif fault == "entities":
        backend.entities = 1
    elif fault == "recreated":
        backend.before_write = lambda: backend.node.update(created_at=CREATED.replace(day=17))
    elif fault == "other_batch":
        args["batch_fingerprint"] = "b" * 64
    elif fault == "changed_after_freeze":
        await graph.save_automatic_ontology("test", **args, ontology_yaml=YAML)
        args["checkpoint"] = {"changed": True}
    prior = deepcopy(backend.node)
    with pytest.raises(ValueError):
        await graph.save_automatic_ontology("test", **args, ontology_yaml="invalid" if fault == "invalid_yaml" else YAML)
    assert backend.node.get("automatic_ontology_json") == prior.get("automatic_ontology_json")
    assert backend.node["ontology"] == prior["ontology"]
    assert backend.node.get("ontology_uri") == prior.get("ontology_uri")


@pytest.mark.parametrize("bad", ["not-json", "[]", '{"version":2}', '{"version":NaN}'])
async def test_corrupt_checkpoint_never_looks_empty(graph_env, bad):
    graph, backend, _ = graph_env
    backend.node["automatic_ontology_json"] = bad
    with pytest.raises(ValueError, match="automatic ontology"):
        await graph.load_automatic_ontology("test")


async def test_missing_frozen_catalogue_is_not_general_fallback(graph_env):
    graph, backend, _ = graph_env
    backend.node["ontology"] = "auto_0123456789abcdef"
    with pytest.raises(ValueError, match="automatic ontology"):
        await graph.load_automatic_ontology("test")


async def test_chosen_auto_cloud_name_is_not_intercepted(graph_env, monkeypatch):
    graph, backend, _ = graph_env
    from mcp_memory.core import ingest_pipeline as pipeline
    backend.node["ontology"] = "auto_cloud"
    assert await graph.load_automatic_ontology("test") is None
    graph.get_memory = AsyncMock(return_value=SimpleNamespace(ontology="auto_cloud"))
    graph.load_automatic_ontology = AsyncMock(side_effect=AssertionError("chosen path must not load automatic state"))
    storage = SimpleNamespace(upload_document=AsyncMock(side_effect=RuntimeError("stop after ontology resolution")))
    monkeypatch.setattr(pipeline, "_graph", lambda: graph)
    monkeypatch.setattr(pipeline, "_storage", lambda: storage)
    monkeypatch.setattr(pipeline, "_rollback", AsyncMock(return_value={}))
    await pipeline.run_ingest_pipeline(memory_id="test", content=b"x", filename="x.md", doc_hash="a" * 64)
    graph.load_automatic_ontology.assert_not_awaited()
    storage.upload_document.assert_awaited_once()


async def test_backup_restores_checkpoint_and_default(graph_env):
    graph, backend, _ = graph_env
    checkpoint = {"profile_sha256": "b" * 64, "diagnostics": {"catalogue_sha256": "c" * 64}}
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint=checkpoint, ontology_yaml=YAML)
    exported = await graph.export_memory_data("test")
    props = exported["memory"]
    assert json.loads(props["automatic_ontology_json"])["checkpoint"] == checkpoint
    assert props.get("ontology_uri") is None
    props["created_at"] = CREATED.isoformat()
    graph.get_memory = AsyncMock(return_value=None)
    graph._run_consumed = AsyncMock()
    await graph.import_memory_data({"memory": props})
    call = graph._run_consumed.await_args_list[0]
    assert "automatic_ontology_json" in call.args[1]
    assert call.kwargs["automatic_ontology_json"] == props["automatic_ontology_json"]
    assert call.kwargs["ontology"] == props["ontology"]
    assert call.kwargs["ontology_uri"] is None
    props["automatic_ontology_json"] = "{}"
    graph._run_consumed.reset_mock()
    with pytest.raises(ValueError):
        await graph.import_memory_data({"memory": props})
    graph._run_consumed.assert_not_awaited()


@pytest.mark.parametrize("fault", [None, "metadata", "path", "ownership", "second_config", "extra_object"])
async def test_reindex_after_automatic_freeze_admits_only_original_config(graph_env, tmp_path, fault):
    from qdrant_client import QdrantClient
    from mcp_memory.core.reindex import ReindexService
    from mcp_memory.core.vector_store import VectorStoreService
    from tests.fakes.inference_fakes import make_embedding_profile
    from tests.test_p13_qdrant_reindex import (
        _MEMORY_ID, _source_bundle, _add_ontology_config, _seed_legacy,
        _FakeStorage, _FakeChunker, _FakeEmbedder, _alias_map,
    )

    graph, backend, _ = graph_env
    documents, objects, content_by_key = _source_bundle()
    config_key = _add_ontology_config(objects, content_by_key)
    config = objects[-1]
    backend.node.update(id=_MEMORY_ID, ontology_uri=config["uri"])
    await graph.save_automatic_ontology(
        _MEMORY_ID, expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH,
        checkpoint={"profile_sha256": "b" * 64, "diagnostics": {}}, ontology_yaml=YAML,
    )
    frozen = await graph.load_automatic_ontology(_MEMORY_ID)
    assert frozen["ontology_yaml"] == YAML
    assert await graph.get_reindex_ontology_uri(_MEMORY_ID) is None
    backend.documents = len(documents)
    backend.reindex_documents = documents

    if fault == "metadata":
        config["metadata"]["ontology_name"] = "mismatched-name"
    elif fault == "path":
        config["key"] += ".unexpected"
    elif fault == "ownership":
        config["metadata"]["memory_id"] = "another-memory"
    elif fault in {"second_config", "extra_object"}:
        extra = deepcopy(config)
        filename = "_ontology_cloud.yaml" if fault == "second_config" else "orphan.txt"
        extra["metadata"]["original_filename"] = filename
        extra["metadata"]["ontology_name"] = "cloud"
        if fault == "extra_object":
            extra["metadata"].pop("type")
        extra["key"] = f"{_MEMORY_ID}/documents/{extra['metadata']['doc_hash'][:8]}_{filename}"
        extra["uri"] = "s3://test-bucket/" + extra["key"]
        objects.append(extra)

    storage = _FakeStorage(objects, content_by_key)
    embedder = _FakeEmbedder()
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    try:
        legacy_name = _seed_legacy(client)
        vectors = VectorStoreService(
            client=client, profile=make_embedding_profile(expected_dimensions=3),
            legacy_prefix="memory_",
        )
        result = await ReindexService(
            graph=graph, storage=storage, chunker=_FakeChunker(), embedder=embedder,
            vectors=vectors, text_extractor=lambda content, _: content.decode("utf-8"),
        ).reindex(_MEMORY_ID)
        if fault is None:
            assert result["status"] == "ok", result
            assert result["activated"] is True
            assert result["source_documents"] == 2
            assert result["vectors_written"] == 8
            assert config_key not in storage.read_calls
        else:
            assert result["status"] == "error", result
            assert result["phase"] == "snapshot"
            assert result["reason"] == ("source_ownership_invalid" if fault == "ownership" else "source_object_mismatch")
            assert result["activated"] is False
            assert embedder.calls == []
            assert storage.read_calls == []
            assert _alias_map(client) == {}
        assert client.count(collection_name=legacy_name, exact=True).count == 1
        assert storage.objects == objects
        assert await graph.load_automatic_ontology(_MEMORY_ID) == frozen
    finally:
        client.close()


async def test_pipeline_uses_frozen_default_before_first_io(graph_env, monkeypatch):
    graph, backend, _ = graph_env
    from mcp_memory.core import ingest_pipeline as pipeline
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint={}, ontology_yaml=YAML)
    graph.get_memory = AsyncMock(return_value=SimpleNamespace(ontology=backend.node["ontology"]))
    storage = SimpleNamespace(upload_document=AsyncMock(side_effect=RuntimeError("stop after ontology resolution")))
    monkeypatch.setattr(pipeline, "_graph", lambda: graph)
    monkeypatch.setattr(pipeline, "_storage", lambda: storage)
    monkeypatch.setattr(pipeline, "_rollback", AsyncMock(return_value={}))
    original_load = graph.load_automatic_ontology
    graph.load_automatic_ontology = AsyncMock(wraps=original_load)
    await pipeline.run_ingest_pipeline(memory_id="test", content=b"x", filename="x.md", doc_hash="a" * 64)
    graph.load_automatic_ontology.assert_awaited_once_with("test")
    storage.upload_document.assert_awaited_once()
    backend.node["automatic_ontology_json"] = "{}"
    storage.upload_document.reset_mock()
    result = await pipeline.run_ingest_pipeline(memory_id="test", content=b"x", filename="x.md", doc_hash="a" * 64)
    assert result["status"] == "error"
    storage.upload_document.assert_not_awaited()


@pytest.mark.parametrize("invalid", [False, True])
async def test_pipeline_actual_extractor_applies_frozen_vocabulary_before_graph(graph_env, monkeypatch, invalid):
    from mcp_memory.core import ingest_pipeline as pipeline
    from mcp_memory.core.extractor import ExtractorService
    from unittest.mock import MagicMock
    graph, backend, _ = graph_env
    await graph.save_automatic_ontology("test", expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH, checkpoint={}, ontology_yaml=YAML)
    graph.get_memory = AsyncMock(return_value=SimpleNamespace(ontology=backend.node["ontology"]))
    graph.add_document = AsyncMock()
    graph.add_entities_and_relations = AsyncMock(return_value={"entities_created": 2, "relations_created": 2})
    graph.update_document_ingestion = AsyncMock()
    storage = SimpleNamespace(upload_document=AsyncMock(return_value={"uri": "s3://bucket/test/document.md", "size_bytes": 30}))
    extractor = object.__new__(ExtractorService)
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=json.dumps({
        "entities": [{"name": "Switch", "type": "INVENTED" if invalid else "Device"}, {"name": "Unclassified", "type": "Other"}],
        "relations": [{"from_entity": "Switch", "to_entity": "Unclassified", "type": "RELATED_TO"}, {"from_entity": "Unclassified", "to_entity": "Switch", "type": "Other"}],
    })))
    monkeypatch.setattr(pipeline, "_graph", lambda: graph)
    monkeypatch.setattr(pipeline, "_storage", lambda: storage)
    monkeypatch.setattr(pipeline, "_extractor", lambda: extractor)
    monkeypatch.setattr(pipeline, "_chunker", lambda: SimpleNamespace(chunk_document=MagicMock(return_value=[])))
    monkeypatch.setattr(pipeline, "_rollback", AsyncMock(return_value={}))
    monkeypatch.setattr("mcp_memory.server._extract_text", lambda *_: "Switch connects to an unclassified item.")
    result = await pipeline.run_ingest_pipeline(memory_id="test", content=b"text", filename="x.md", doc_hash="a" * 64)
    if invalid:
        assert result["status"] == "error"
        graph.add_document.assert_not_awaited()
        graph.add_entities_and_relations.assert_not_awaited()
        assert extractor._complete.await_count == 2
    else:
        assert result["status"] == "ok"
        graph.add_document.assert_awaited_once()
        assert result["entity_types"] == {"Device": 1, "Other": 1}
        assert result["ontology_diagnostics"] == {
            "entity_other": {"count": 1, "denominator": 2, "rate": .5},
            "relation_other": {"count": 1, "denominator": 2, "rate": .5},
            "relation_related_to": {"count": 1, "denominator": 2, "rate": .5},
        }
        assert extractor._complete.await_count == 1


async def test_competing_checkpoint_cannot_overwrite_progress(graph_env):
    graph, backend, _ = graph_env
    kwargs = dict(expected_created_at=CREATED.isoformat(), batch_fingerprint=BATCH)
    await graph.save_automatic_ontology("test", **kwargs, checkpoint={"first": True})
    competing = json.loads(backend.node["automatic_ontology_json"])
    competing["checkpoint"] = {"other_valid_step": True}
    encoded = json.dumps(competing)
    backend.before_write = lambda: backend.node.update(automatic_ontology_json=encoded)
    with pytest.raises(ValueError):
        await graph.save_automatic_ontology("test", **kwargs, checkpoint={"second": True})
    assert backend.node["automatic_ontology_json"] == encoded
