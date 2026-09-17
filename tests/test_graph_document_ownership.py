"""#541: imported and persisted references cannot authorize foreign S3 reads."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
from tests.test_p13_qdrant_backup_identity import (
    BACKUP_ID, DOCUMENT_KEYS, GRAPH_DATA, MEMORY_ID, _artifact_set, _service,
    backup_module,
)
from tests.test_p13_reindex_sources import _GraphSession, _SessionContext


BAD_REFERENCES = [
    "s3://foreign-bucket/memory-one/documents/source.txt",
    "s3://test-bucket/other-memory/documents/source.txt",
    "s3://test-bucket/memory-one/bank/activeContext.md",
    "s3://test-bucket/memory-one/_meta.json",
    "s3://test-bucket/_system/tokens.json",
    "memory-one/documents/",
    "memory-one/documents/../bank/activeContext.md",
    "s3://test-bucket/%6demory-one/documents/source.txt",
    "https://test-bucket/memory-one/documents/source.txt",
]


@pytest.fixture
def storage(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core.storage import StorageService

    service = StorageService.__new__(StorageService)
    service._bucket = "test-bucket"
    service._client = MagicMock()
    service._client_v4 = service._client
    service._client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(b"document"), "ContentLength": 8,
    }
    return service


@pytest.mark.parametrize("reference", BAD_REFERENCES)
async def test_document_download_rejects_reference_before_s3(storage, reference):
    with pytest.raises((ValueError, PermissionError)):
        await storage.download_document(MEMORY_ID, reference)
    storage._client.get_object.assert_not_called()


@pytest.mark.parametrize("as_uri", [False, True])
@pytest.mark.parametrize("filename", ["source.txt", "résumé 100%?#.txt", "%2e%2e%2fbank.txt"])
async def test_document_download_preserves_literal_valid_keys(storage, as_uri, filename):
    key = f"{MEMORY_ID}/documents/{filename}"
    reference = f"s3://test-bucket/{key}" if as_uri else key
    assert await storage.download_document(MEMORY_ID, reference) == b"document"
    storage._client.get_object.assert_called_once_with(Bucket="test-bucket", Key=key)


@pytest.mark.parametrize("memory_id", ["", "memory-one\n", "memory-one/other", "_system"])
async def test_document_download_requires_canonical_memory_id(storage, memory_id):
    with pytest.raises((ValueError, PermissionError)):
        await storage.download_document(memory_id, f"{memory_id}/documents/source.txt")
    storage._client.get_object.assert_not_called()


def _graph_data(reference):
    data = copy.deepcopy(GRAPH_DATA)
    data["documents"] = [{"id": "doc-1", "filename": "source.txt", "uri": reference}]
    return data


def _artifacts(reference, *, document_keys=None):
    artifacts = _artifact_set(document_keys=[] if document_keys is None else document_keys)
    graph_bytes = json.dumps(_graph_data(reference), ensure_ascii=False, indent=2).encode()
    artifacts["graph_data.json"] = graph_bytes
    manifest = json.loads(artifacts["manifest.json"])
    manifest["checksums"]["graph_data"] = hashlib.sha256(graph_bytes).hexdigest()
    artifacts["manifest.json"] = json.dumps(manifest).encode()
    return artifacts


def _archive(artifacts):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for filename, content in artifacts.items():
            member = tarfile.TarInfo(f"backup/{filename}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _real_storage(service, storage, objects):
    service._storage = storage
    storage._client.get_object.side_effect = lambda *, Bucket, Key: {
        "Body": io.BytesIO(objects[Key]), "ContentLength": len(objects[Key]),
    }


@pytest.mark.parametrize("route", ["archive", "s3"])
@pytest.mark.parametrize("reference", BAD_REFERENCES)
async def test_restore_rejects_graph_uri_without_document_keys_before_effects(
    backup_module, storage, route, reference,
):
    artifacts = _artifacts(reference)
    if route == "archive":
        del artifacts["document_keys.json"]
    service, operations, objects = _service(backup_module, artifacts=artifacts)
    _real_storage(service, storage, objects)
    with pytest.raises((ValueError, PermissionError)):
        if route == "archive":
            await service.restore_from_archive(_archive(artifacts))
        else:
            await service.restore_backup(BACKUP_ID)
    assert not any(op in operations for op in ("graph.import", "vector.import"))
    storage._client.put_object.assert_not_called()
    storage._client.head_object.assert_not_called()
    assert all(call.kwargs["Key"].startswith(f"_backups/{BACKUP_ID}/")
               for call in storage._client.get_object.call_args_list)


@pytest.mark.parametrize("reference", BAD_REFERENCES)
async def test_backup_create_refuses_persisted_graph_uri(backup_module, storage, reference):
    service, operations, objects = _service(backup_module, memory_exists=True)
    _real_storage(service, storage, objects)
    service._graph.export_memory_data = AsyncMock(return_value=_graph_data(reference))
    with pytest.raises((ValueError, PermissionError)):
        await service.create_backup(MEMORY_ID)
    storage._client.get_object.assert_not_called()
    storage._client.put_object.assert_not_called()


@pytest.mark.parametrize("reference", BAD_REFERENCES)
async def test_download_refuses_historical_forged_document_key(backup_module, storage, reference):
    key = reference.split("/", 3)[-1] if reference.startswith("s3://") else reference
    forged = [{**DOCUMENT_KEYS[0], "key": key, "uri": reference}]
    artifacts = _artifacts(DOCUMENT_KEYS[0]["uri"], document_keys=forged)
    service, operations, objects = _service(backup_module, artifacts=artifacts)
    objects[key] = b"foreign bytes must never be read"
    _real_storage(service, storage, objects)
    with pytest.raises((ValueError, PermissionError)):
        await service.download_backup(BACKUP_ID, include_documents=True)
    assert all(call.kwargs["Key"].startswith(f"_backups/{BACKUP_ID}/")
               for call in storage._client.get_object.call_args_list)


@pytest.mark.parametrize("reference", BAD_REFERENCES)
async def test_direct_graph_import_refuses_unsafe_uri(monkeypatch, storage, reference):
    from tests.fakes.neo4j_fakes import bind_fake_neo4j

    graph = bind_fake_neo4j(monkeypatch)

    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(s3_bucket_name="test-bucket"))
    service = graph.GraphService.__new__(graph.GraphService)
    service.get_memory = AsyncMock(side_effect=AssertionError("unexpected graph access"))
    with pytest.raises((ValueError, PermissionError)):
        await service.import_memory_data(_graph_data(reference))
    service.get_memory.assert_not_awaited()


@pytest.mark.parametrize("route", ["exists", "check", "presign", "reindex"])
@pytest.mark.parametrize("reference", BAD_REFERENCES[:5])
async def test_sibling_document_reads_require_same_ownership(storage, route, reference):
    with pytest.raises((ValueError, PermissionError)):
        if route == "exists":
            await storage.document_exists(reference, memory_id=MEMORY_ID)
        elif route == "check":
            await storage.check_documents([(MEMORY_ID, DOCUMENT_KEYS[0]["uri"]), (MEMORY_ID, reference)])
        elif route == "presign":
            await storage.get_signed_url(reference, memory_id=MEMORY_ID)
        else:
            await storage.read_reindex_object(MEMORY_ID, reference, 8)
    storage._client.get_object.assert_not_called()
    storage._client.head_object.assert_not_called()
    storage._client.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("full", [False, True])
async def test_valid_archive_round_trip_keeps_references_and_content(backup_module, storage, full):
    reference = DOCUMENT_KEYS[0]["uri"]
    artifacts = _artifacts(reference, document_keys=DOCUMENT_KEYS)
    service, operations, objects = _service(backup_module, artifacts=artifacts)
    objects[DOCUMENT_KEYS[0]["key"]] = b"document"
    _real_storage(service, storage, objects)
    downloaded = await service.download_backup(BACKUP_ID, include_documents=full)
    with tarfile.open(fileobj=io.BytesIO(downloaded), mode="r:gz") as archive:
        document_members = [member for member in archive.getmembers() if "/documents/" in member.name]
        assert len(document_members) == int(full)
        if full:
            assert archive.extractfile(document_members[0]).read() == b"document"
    service._graph.import_memory_data = AsyncMock(return_value={"documents": 1})
    result = await service.restore_from_archive(downloaded)
    assert result["status"] == "ok"
    service._graph.import_memory_data.assert_awaited_once_with(_graph_data(reference))
    assert operations == ["vector.preflight", "vector.import"]
    assert storage._client.put_object.call_count == int(full)


@pytest.mark.parametrize("reference", [None, "", DOCUMENT_KEYS[0]["uri"]])
async def test_direct_graph_import_keeps_valid_and_metadata_only_documents(monkeypatch, storage, reference):
    from tests.fakes.neo4j_fakes import bind_fake_neo4j

    graph = bind_fake_neo4j(monkeypatch)
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(s3_bucket_name="test-bucket"))
    service = graph.GraphService.__new__(graph.GraphService)
    service.get_memory = AsyncMock(return_value=None)
    session = _GraphSession([])
    service.session = lambda: _SessionContext(session)
    result = await service.import_memory_data(_graph_data(reference))
    assert result["documents"] == 1
    params = [params for query, params in session.calls if "CREATE (d:Document" in query]
    assert params[0]["uri"] == reference


@pytest.mark.parametrize("route", ["s3", "archive"])
async def test_restore_rejects_mismatched_graph_memory_before_effects(backup_module, storage, route):
    artifacts = _artifacts(DOCUMENT_KEYS[0]["uri"])
    data = json.loads(artifacts["graph_data.json"])
    data["memory"]["id"] = "other-memory"
    data["documents"] = []
    artifacts["graph_data.json"] = json.dumps(data, ensure_ascii=False, indent=2).encode()
    manifest = json.loads(artifacts["manifest.json"])
    manifest["checksums"]["graph_data"] = hashlib.sha256(artifacts["graph_data.json"]).hexdigest()
    artifacts["manifest.json"] = json.dumps(manifest).encode()
    service, operations, objects = _service(backup_module, artifacts=artifacts)
    _real_storage(service, storage, objects)
    with pytest.raises(ValueError, match="namespace mismatch"):
        if route == "s3":
            await service.restore_backup(BACKUP_ID)
        else:
            await service.restore_from_archive(_archive(artifacts))
    assert operations == []
    storage._client.put_object.assert_not_called()
