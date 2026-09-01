# -*- coding: utf-8 -*-
"""#277 — backup/restore keeps Qdrant embedding identity fail-closed.

The existing backup format still stores points as checksum-protected JSONL.
These tests lock the additional compact identity in ``manifest.json`` and,
for both restore entry points, prove that the read-only vector preflight is
the last guard before any Neo4j, S3, or Qdrant mutation.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from hivemind_inference import (
    EmbeddingResult,
    build_embedding_collection_identity,
)
from tests.fakes.inference_fakes import (
    apply_graph_memory_baseline_env,
    make_embedding_profile,
)


MEMORY_ID = "memory-one"
BACKUP_ID = f"{MEMORY_ID}/2026-07-30T00-00-00"
BACKUP_FORMAT_VERSION = "1.0"
_PROFILE = make_embedding_profile(expected_dimensions=2)
_RESULT = EmbeddingResult(
    vectors=((1.0, 0.0),),
    configured_model=_PROFILE.configured_model,
    resolved_model="resolved-model",
    model_evidence="provider_reported",
    effective_dimensions=2,
)
IDENTITY = build_embedding_collection_identity(
    MEMORY_ID,
    _PROFILE,
    _RESULT,
).to_mapping()
POINTS = [
    {
        "id": "point-1",
        "vector": [1.0, 0.0],
        "payload": {
            "memory_id": MEMORY_ID,
            "doc_id": "doc-1",
            "text": "alpha",
        },
    }
]
GRAPH_DATA = {
    "memory": {"id": MEMORY_ID, "name": "Memory", "ontology": "general"},
    "documents": [],
    "entities": [],
    "relations": [],
    "mentions": [],
}
DOCUMENT_KEYS = [
    {
        "doc_id": "doc-1",
        "filename": "document.txt",
        "uri": "s3://test-bucket/memory-one/documents/document.txt",
        "key": "memory-one/documents/document.txt",
        "hash": "sha256",
        "size_bytes": 8,
    }
]
_MISSING = object()


def _json_bytes(value: object, *, indent: int | None = None) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=indent,
    ).encode("utf-8")


def _artifact_set(
    identity: object = IDENTITY,
    *,
    document_keys: object = DOCUMENT_KEYS,
) -> dict[str, bytes]:
    graph_bytes = _json_bytes(GRAPH_DATA, indent=2)
    qdrant_bytes = "\n".join(
        json.dumps(point, ensure_ascii=False) for point in POINTS
    ).encode("utf-8")
    document_keys_bytes = _json_bytes(document_keys, indent=2)
    manifest = {
        "version": BACKUP_FORMAT_VERSION,
        "backup_id": BACKUP_ID,
        "memory_id": MEMORY_ID,
        "memory_name": "Memory",
        "memory_ontology": "general",
        "created_at": "2026-07-30T00:00:00Z",
        "stats": {
            "documents": 0,
            "entities": 0,
            "relations": 0,
            "mentions": 0,
            "qdrant_vectors": len(POINTS),
            "document_files": len(DOCUMENT_KEYS),
            "total_document_size_bytes": 8,
        },
        "checksums": {
            "graph_data": hashlib.sha256(graph_bytes).hexdigest(),
            "qdrant_vectors": hashlib.sha256(qdrant_bytes).hexdigest(),
            "document_keys": hashlib.sha256(document_keys_bytes).hexdigest(),
        },
        "files": [
            "manifest.json",
            "graph_data.json",
            "qdrant_vectors.jsonl",
            "document_keys.json",
        ],
    }
    if identity is not _MISSING:
        manifest["qdrant_identity"] = identity
    return {
        "manifest.json": _json_bytes(manifest, indent=2),
        "graph_data.json": graph_bytes,
        "qdrant_vectors.jsonl": qdrant_bytes,
        "document_keys.json": document_keys_bytes,
    }


def _archive_bytes(
    identity: object = IDENTITY,
    *,
    document_keys: object = DOCUMENT_KEYS,
) -> bytes:
    artifacts = _artifact_set(identity, document_keys=document_keys)
    artifacts["documents/document.txt"] = b"document"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for filename, content in artifacts.items():
            info = tarfile.TarInfo(name=f"backup-{MEMORY_ID}/{filename}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class _Body:
    def __init__(self, content: bytes):
        self._content = content

    def read(self) -> bytes:
        return self._content


class _StorageClient:
    def __init__(self, objects: dict[str, bytes], operations: list[str]):
        self.objects = objects
        self.operations = operations

    def get_object(self, *, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.operations.append("storage.put")
        self.objects[Key] = bytes(Body)


class _Storage:
    _bucket = "test-bucket"

    def __init__(self, objects: dict[str, bytes], operations: list[str]):
        self._client = _StorageClient(objects, operations)
        self._operations = operations

    @staticmethod
    def _parse_key(uri: str) -> str:
        return uri.split("test-bucket/", 1)[1]

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        return "text/plain"

    @staticmethod
    def _sanitize_metadata_value(value: str) -> str:
        return value

    async def document_exists(self, uri: str) -> bool:
        return True

    async def upload_document(self, **kwargs):
        self._operations.append("storage.upload")
        return {"uri": f"s3://test-bucket/{kwargs['filename']}"}


class _Graph:
    def __init__(self, operations: list[str], *, exists: bool = False):
        self._operations = operations
        self._exists = exists

    async def get_memory(self, memory_id: str):
        if not self._exists:
            return None
        return SimpleNamespace(name="Memory", ontology="general")

    async def export_memory_data(self, memory_id: str):
        return GRAPH_DATA

    async def import_memory_data(self, graph_data):
        self._operations.append("graph.import")
        return {"memories": 1}


class _Vectors:
    def __init__(
        self,
        operations: list[str],
        *,
        accepted_identity: object = IDENTITY,
    ):
        self._operations = operations
        self._accepted_identity = accepted_identity

    async def export_collection(self, memory_id: str):
        return {"identity": dict(IDENTITY), "points": list(POINTS)}

    async def preflight_import(self, memory_id: str, identity, points) -> None:
        self._operations.append("vector.preflight")
        if (
            memory_id != MEMORY_ID
            or identity != self._accepted_identity
            or points != POINTS
        ):
            raise RuntimeError("backup identity refused")

    async def import_collection(self, memory_id: str, points, *, identity) -> int:
        self._operations.append("vector.import")
        assert memory_id == MEMORY_ID
        assert points == POINTS
        assert identity == self._accepted_identity
        return len(points)


@pytest.fixture
def backup_module(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core import backup

    monkeypatch.setattr(
        backup,
        "get_settings",
        lambda: SimpleNamespace(
            s3_backup_prefix="_backups",
            backup_retention_count=0,
        ),
    )
    return backup


def _service(
    backup_module,
    *,
    artifacts: dict[str, bytes] | None = None,
    identity: object = IDENTITY,
    memory_exists: bool = False,
):
    operations: list[str] = []
    objects: dict[str, bytes] = {}
    if artifacts:
        prefix = f"_backups/{BACKUP_ID}"
        objects.update(
            {f"{prefix}/{filename}": content for filename, content in artifacts.items()}
        )
    service = backup_module.BackupService(
        _Graph(operations, exists=memory_exists),
        _Vectors(operations, accepted_identity=identity),
        _Storage(objects, operations),
    )
    return service, operations, objects


async def test_export_persists_identity_beside_checksum_protected_jsonl(
    backup_module,
):
    service, operations, objects = _service(
        backup_module,
        memory_exists=True,
    )

    result = await service.create_backup(MEMORY_ID)

    assert result["status"] == "ok"
    manifest_key = next(key for key in objects if key.endswith("/manifest.json"))
    manifest = json.loads(objects[manifest_key])
    vectors = objects[manifest_key.replace("manifest.json", "qdrant_vectors.jsonl")]
    assert manifest["qdrant_identity"] == IDENTITY
    assert json.loads(vectors.decode("utf-8")) == POINTS[0]
    assert manifest["checksums"]["qdrant_vectors"] == hashlib.sha256(
        vectors
    ).hexdigest()
    assert operations.count("storage.put") == 4


async def test_s3_restore_preflights_immediately_before_mutations(
    backup_module,
):
    service, operations, _ = _service(
        backup_module,
        artifacts=_artifact_set(),
        identity=IDENTITY,
    )

    result = await service.restore_backup(BACKUP_ID)

    assert result["status"] == "ok"
    assert operations == [
        "vector.preflight",
        "graph.import",
        "vector.import",
    ]


async def test_archive_restore_preflights_before_s3_graph_and_vector_mutations(
    backup_module,
):
    service, operations, _ = _service(
        backup_module,
        identity=IDENTITY,
    )

    result = await service.restore_from_archive(_archive_bytes())

    assert result["status"] == "ok"
    assert operations == [
        "vector.preflight",
        "storage.put",
        "graph.import",
        "vector.import",
    ]


async def test_archive_restore_rejects_cross_namespace_document_key_before_effects(
    backup_module,
):
    service, operations, _ = _service(
        backup_module,
        identity=IDENTITY,
    )
    forged_keys = [
        {
            **DOCUMENT_KEYS[0],
            "uri": "s3://test-bucket/other-memory/documents/document.txt",
            "key": "other-memory/documents/document.txt",
        }
    ]

    with pytest.raises(ValueError, match=r"^Invalid document_keys\.json$"):
        await service.restore_from_archive(
            _archive_bytes(document_keys=forged_keys)
        )

    assert operations == []


@pytest.mark.parametrize(
    ("case", "identity"),
    [
        ("missing", _MISSING),
        ("malformed", {"schema_version": 1}),
        ("drifted", {**IDENTITY, "configured_model": "other-model"}),
    ],
)
@pytest.mark.parametrize("route", ["s3", "archive"])
async def test_invalid_identity_refuses_every_backend_mutation(
    backup_module,
    route,
    case,
    identity,
):
    del case
    artifacts = _artifact_set(identity)
    service, operations, _ = _service(
        backup_module,
        artifacts=artifacts if route == "s3" else None,
        identity=IDENTITY,
    )

    with pytest.raises(RuntimeError, match=r"^backup identity refused$"):
        if route == "s3":
            await service.restore_backup(BACKUP_ID)
        else:
            await service.restore_from_archive(_archive_bytes(identity))

    assert operations == ["vector.preflight"]
