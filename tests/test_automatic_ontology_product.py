"""Product-path proofs: real server, queue, construction, parser and ingestion.

Only the Graph/S3/vector backends and the normalized inference provider are
doubled. The Graph checkpoint methods reuse their existing conditional-write
backend fixture; no construction/pipeline/extractor operation is mocked.
"""
import asyncio
import base64
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hivemind_inference import ChatResult, EmbeddingResult, InferenceError
from tests.fakes.inference_fakes import make_chat_profile, make_embedding_profile
from tests.test_automatic_ontology_state import graph_env, YAML  # noqa: F401


def documents(count):
    result = []
    for i in range(count):
        content = f"Instrument {i} measures temperature with a sensor.".encode()
        result.append({"filename": f"instrument-{i}.md", "source_path": f"manual/{i}.md",
                       "sha256": hashlib.sha256(content).hexdigest(),
                       "content_base64": base64.b64encode(content).decode()})
    return result


@pytest.fixture
def product(graph_env, monkeypatch):
    from mcp_memory import server
    from mcp_memory.auth.context import current_auth
    from mcp_memory.core import automatic_ontology, graph as graph_module
    from mcp_memory.core import inference_runtime, ingest_pipeline, ingest_queue, ontology
    from mcp_memory.core.extractor import ExtractorService
    from mcp_memory.core.embedder import EmbeddingService
    from mcp_memory.core.chunker import SemanticChunker

    graph, backend, _ = graph_env
    state = SimpleNamespace(
        calls=[], events=[], stored={}, vectors={}, graph_docs={}, extractions=[],
        fail_d2=False, invalid_labels=False, require_frozen=False, pause_first_extraction=False,
        extraction_entered=asyncio.Event(), extraction_release=asyncio.Event(),
    )
    manager = ontology.OntologyManager(str(
        Path(__file__).resolve().parents[1] / "services/graph-memory/ONTOLOGIES"))
    monkeypatch.setattr(ontology, "_ontology_manager", manager)
    token = current_auth.set({"permissions": ["read", "write"], "memory_ids": ["test"]})

    async def memory(memory_id):
        return SimpleNamespace(id=memory_id, name="Synthetic", ontology=backend.node["ontology"],
                               created_at=backend.node["created_at"])

    async def stats(_):
        return SimpleNamespace(document_count=len(state.graph_docs), entity_count=0, relation_count=0)

    async def by_source(_, path):
        return next((deepcopy(d) for d in state.graph_docs.values() if d["source_path"] == path), None)

    async def add_document(**kwargs):
        if state.require_frozen:
            frozen = await graph.load_automatic_ontology("test")
            assert frozen["ontology_yaml"] is not None
            assert frozen["checkpoint"]["diagnostics"]["catalogue_sha256"]
            assert "construction" not in frozen["checkpoint"]
            assert backend.node["ontology"].startswith("auto_")
        state.events.append("graph_document")
        state.graph_docs[kwargs["doc_id"]] = {
            "id": kwargs["doc_id"], "source_path": kwargs["source_path"],
            "hash": kwargs["doc_hash"], "ingestion_status": kwargs["ingestion_status"],
        }
        backend.documents = len(state.graph_docs)

    async def enrich(**kwargs):
        state.extractions.append(kwargs["extraction"])
        state.events.append("graph_facts")
        return {"entities_created": len(kwargs["extraction"].entities),
                "relations_created": len(kwargs["extraction"].relations)}

    async def finalize(**kwargs):
        state.graph_docs[kwargs["doc_id"]]["ingestion_status"] = kwargs["ingestion_status"]

    original_save = graph.save_automatic_ontology
    async def save(*args, **kwargs):
        await original_save(*args, **kwargs)
        state.events.append("freeze" if kwargs.get("ontology_yaml") else "checkpoint")

    graph.get_memory = memory
    graph.get_memory_stats = stats
    graph.get_document_by_source_path = by_source
    graph.add_document = add_document
    graph.add_entities_and_relations = enrich
    graph.update_document_ingestion = finalize
    graph.save_automatic_ontology = save
    monkeypatch.setattr(graph_module, "get_graph_service", lambda: graph)
    monkeypatch.setattr(automatic_ontology, "get_graph_service", lambda: graph)

    async def upload(**kwargs):
        if state.require_frozen:
            assert "freeze" in state.events
        uri = "s3://synthetic/" + kwargs["filename"]
        state.stored[uri] = kwargs["content"]
        state.events.append("s3_upload")
        return {"uri": uri, "size_bytes": len(kwargs["content"])}

    async def delete(_, uri):
        state.stored.pop(uri)
        state.events.append("s3_rollback")
        return True

    storage = SimpleNamespace(upload_document=upload, delete_document=delete,
                              compute_hash=lambda content: hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(server, "get_storage", lambda: storage)
    monkeypatch.setattr(ingest_pipeline, "_storage", lambda: storage)

    async def store_vectors(**kwargs):
        state.vectors[kwargs["doc_id"]] = kwargs["chunks"]
        state.events.append("vectors")
        return len(kwargs["chunks"])
    monkeypatch.setattr(ingest_pipeline, "_vector_store", lambda: SimpleNamespace(store_chunks=store_vectors))

    class Provider:
        async def complete(self, request):
            try:
                packet = json.loads(request.messages[1].content)
            except json.JSONDecodeError:
                packet = None
            if isinstance(packet, dict) and "schema" in packet:
                assert request.retry_policy == "none"
                assert request.reasoning_effort == "low"
                data = packet["data"]
                if "candidate_assertions" in data:
                    phase = "d2" if "existing_ontology" in data else "d1"
                    state.calls.append(phase)
                    if phase == "d2" and state.fail_d2:
                        raise InferenceError(category="unavailable", role="chat",
                            provider_id="openai-compatible", adapter_id="openai-compatible",
                            retryable=False, correlation_id="synthetic-d2")
                    payload = {
                        "entity_types": [{"name": "Sensor" if phase == "d2" else "Device",
                                          "description": "An identified measuring device."}],
                        "relation_types": [{"name": "MEASURES", "description": "Subject measures object."}],
                    }
                    if phase == "d2":
                        payload["mode"] = "complete"
                else:
                    state.calls.append("open_extract")
                    if state.pause_first_extraction and state.calls == ["open_extract"]:
                        state.extraction_entered.set()
                        await state.extraction_release.wait()
                    payload = {"assertions": [{
                        "subject": "Instrument", "predicate": "measures", "object": "temperature",
                        "polarity": "affirmed", "condition": None,
                        "evidence": [{"passage_id": p["passage_id"], "quote": p["text"]}],
                    } for p in data["passages"]]}
            else:
                state.calls.append("document_extract")
                frozen = await graph.load_automatic_ontology("test")
                entity = (yaml.safe_load(frozen["ontology_yaml"])["entity_types"][0]["name"]
                          if frozen is not None and frozen["ontology_yaml"] else "Device")
                assert f'"{entity}"' in request.messages[0].content
                payload = {
                    "entities": [{"name": "Instrument", "type": "Invented" if state.invalid_labels else entity},
                                 {"name": "Temperature", "type": "Other"}],
                    "relations": [{"from_entity": "Instrument", "to_entity": "Temperature", "type": "RELATED_TO"}],
                    "summary": "Instrument measures temperature.", "key_topics": ["Measurement"],
                }
            return ChatResult(text=json.dumps(payload), configured_model="synthetic-chat",
                model_evidence="configured_only", finish_reason="stop",
                input_tokens=10, output_tokens=5, total_tokens=15)

        async def embed(self, request):
            return EmbeddingResult(vectors=tuple((1., 0.) for _ in request.inputs),
                configured_model="synthetic-embedding", model_evidence="configured_only",
                effective_dimensions=2, input_tokens=len(request.inputs), total_tokens=len(request.inputs))

    provider = Provider()
    runtime = SimpleNamespace(config=SimpleNamespace(
        chat=make_chat_profile(configured_model="synthetic-chat", reasoning_effort="low"),
        embedding=make_embedding_profile(configured_model="synthetic-embedding", expected_dimensions=2)),
        chat_provider=lambda: provider, embedding_provider=lambda: provider)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: runtime)
    monkeypatch.setattr(automatic_ontology, "get_inference_runtime", lambda: runtime)
    extractor, embedder, chunker = ExtractorService(), EmbeddingService(), SemanticChunker()
    monkeypatch.setattr(ingest_pipeline, "_extractor", lambda: extractor)
    monkeypatch.setattr(ingest_pipeline, "_embedder", lambda: embedder)
    monkeypatch.setattr(ingest_pipeline, "_chunker", lambda: chunker)

    queue = ingest_queue.IngestQueueService()
    state.queue, state.server, state.graph, state.backend = queue, server, graph, backend
    monkeypatch.setattr(ingest_queue, "get_ingest_queue", lambda: state.queue)
    yield state
    current_auth.reset(token)


async def submit_and_finish(state, docs, mode):
    response = await state.server.memory_ingest_batch_async("test", docs, ontology=mode)
    assert response["status"] == "ok", response
    # Submission has not run the worker's provider calls; no arbitrary sleep.
    workers = list(state.queue._workers.values())
    if workers:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=15)
    statuses = [await state.queue.get_job(item["job_id"]) for item in response["items"]]
    return response, statuses


async def test_chosen_yaml_reaches_pipeline_without_any_calibration(product):
    _, jobs = await submit_and_finish(product, documents(2), YAML)
    assert {job["status"] for job in jobs} == {"succeeded"}
    assert product.calls == ["document_extract", "document_extract"]
    assert await product.graph.load_automatic_ontology("test") is None
    assert product.backend.node["ontology"] == "general"
    assert len(product.graph_docs) == len(product.vectors) == 2
    assert [row.entities[0].type for row in product.extractions] == ["Device", "Device"]


async def test_automatic_batch_freezes_once_before_document_ingestion(product):
    product.require_frozen = True
    response = await product.server.memory_ingest_batch_async("test", documents(12), ontology="auto")
    assert response["status"] == "ok"
    assert product.calls == []
    await asyncio.wait_for(asyncio.gather(*list(product.queue._workers.values())), timeout=15)
    jobs = [await product.queue.get_job(item["job_id"]) for item in response["items"]]
    assert {job["status"] for job in jobs} == {"succeeded"}
    assert Counter(product.calls) == {"open_extract": 12, "d1": 1, "d2": 1, "document_extract": 12}
    assert product.events.index("freeze") < product.events.index("s3_upload") < product.events.index("graph_document")
    assert product.events.count("freeze") == 1
    frozen = await product.graph.load_automatic_ontology("test")
    assert set(frozen["checkpoint"]) == {"profile_sha256", "diagnostics"}
    assert "Instrument 0 measures" not in json.dumps(frozen)
    assert [x["name"] for x in yaml.safe_load(frozen["ontology_yaml"])["entity_types"]] == ["Sensor"]
    assert len(product.graph_docs) == len(product.vectors) == 12
    assert {row.entities[0].type for row in product.extractions} == {"Sensor"}
    assert all(job["ontology_diagnostics"]["relation_related_to"] == {"count": 1, "denominator": 1, "rate": 1.0} for job in jobs)


async def test_invalid_document_labels_never_reach_graph_or_vectors(product):
    product.invalid_labels = True
    _, jobs = await submit_and_finish(product, documents(12), "auto")
    assert {job["status"] for job in jobs} == {"failed"}
    assert Counter(product.calls)["document_extract"] == 24
    assert product.graph_docs == product.vectors == product.stored == {}
    assert product.events.count("s3_rollback") == 12
    assert "graph_document" not in product.events
    assert (await product.graph.load_automatic_ontology("test"))["ontology_yaml"] is not None
    backup = await product.graph.export_memory_data("test")
    exported = json.loads(backup["memory"]["automatic_ontology_json"])
    assert "construction" not in exported["checkpoint"]
    assert "Instrument 0 measures" not in json.dumps(exported)


async def test_resubmission_recovers_d2_then_reuses_frozen_success_without_calls(product):
    docs = documents(12)
    product.fail_d2 = True
    _, failed = await submit_and_finish(product, docs, "auto")
    assert {job["status"] for job in failed} == {"failed"}
    assert Counter(product.calls) == {"open_extract": 12, "d1": 1, "d2": 1}
    assert product.graph_docs == product.stored == {}
    pending = await product.graph.load_automatic_ontology("test")
    assert pending["ontology_yaml"] is None
    assert "frozen" not in pending["checkpoint"]["construction"]
    assert len(pending["checkpoint"]["construction"]["calls"]) == 13

    # A fresh in-memory queue represents a client retry after service restart.
    from mcp_memory.core import ingest_queue
    product.queue = ingest_queue.IngestQueueService()
    product.fail_d2 = False
    product.require_frozen = True
    _, resumed = await submit_and_finish(product, docs, "auto")
    assert {job["status"] for job in resumed} == {"succeeded"}
    assert Counter(product.calls) == {"open_extract": 12, "d1": 1, "d2": 2, "document_extract": 12}
    calls = list(product.calls)
    frozen = deepcopy(await product.graph.load_automatic_ontology("test"))
    assert "construction" not in frozen["checkpoint"]
    _, repeated = await submit_and_finish(product, docs, "auto")
    assert {job["status"] for job in repeated} == {"skipped"}
    assert product.calls == calls
    assert await product.graph.load_automatic_ontology("test") == frozen


async def test_cancel_during_construction_keeps_admitted_work_and_releases_memory(product, caplog):
    from mcp_memory.core.maintenance import get_maintenance_coordinator
    docs = documents(2)
    product.pause_first_extraction = True
    response = await product.server.memory_ingest_batch_async("test", docs, ontology="auto")
    assert response["status"] == "ok"
    worker = product.queue._workers["test"]
    await asyncio.wait_for(product.extraction_entered.wait(), timeout=3)
    first_id = response["items"][0]["job_id"]
    try:
        cancellation = await product.queue.cancel(first_id)
        assert cancellation["status"] == "cancelling"
    finally:
        product.extraction_release.set()
        await asyncio.wait_for(worker, timeout=10)
    first, second = [await product.queue.get_job(item["job_id"]) for item in response["items"]]
    assert first["status"] == "cancelled"
    assert second["status"] == "failed"
    assert second["error"] == "automatic_ontology_interrupted_resubmit_batch"
    assert product.calls == ["open_extract"]
    assert not any(first_id in record.getMessage() and record.exc_info for record in caplog.records)
    pending = await product.graph.load_automatic_ontology("test")
    assert pending["ontology_yaml"] is None
    assert len(pending["checkpoint"]["construction"]["calls"]) == 1
    assert product.graph_docs == product.vectors == product.stored == {}
    assert await product.queue.is_idle_for_memory("test")
    async with get_maintenance_coordinator().ordinary("test"):
        pass
    _, resumed = await submit_and_finish(product, docs, "auto")
    assert {job["status"] for job in resumed} == {"succeeded"}
    assert Counter(product.calls) == {"open_extract": 2, "d1": 1, "document_extract": 2}
