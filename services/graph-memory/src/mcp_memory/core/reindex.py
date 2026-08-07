# -*- coding: utf-8 -*-
"""Bounded source-to-shadow embedding reindex for one Graph Memory runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as url_quote
from uuid import uuid4

from hivemind_inference import EmbeddingCollectionIdentity, EmbeddingResult

from .maintenance import (
    MAX_REINDEX_SOURCE_DOCUMENTS,
    MAX_REINDEX_SOURCE_TOTAL_BYTES,
    MaintenanceAdmissionError,
    MaintenanceCoordinator,
    ReindexSourceLimitExceeded,
    get_maintenance_coordinator,
)
from .models import Chunk
from .validators import MAX_INGEST_SIZE_BYTES
from .vector_store import (
    EmbeddingCollectionError,
    ReindexPostSwitchError,
    VectorStoreService,
)

_GRAPH_SOURCE_FIELDS = {
    "memory_id",
    "document_id",
    "filename",
    "uri",
    "sha256",
    "size_bytes",
    "status",
    "chunk_count",
}
_OBJECT_SOURCE_FIELDS = {"key", "uri", "size_bytes", "metadata"}
_HEX = frozenset("0123456789abcdef")
_EMBED_BATCH_SIZE = 5
_MAX_REINDEX_CHUNKS = 250_000

_SAFE_REASON_BY_VECTOR_REASON = {
    "active_target_changed": "active_target_changed",
    "activation_unverified": "activation_unverified",
    "post_switch_unverified": "post_switch_unverified",
    "shadow_collision": "shadow_collision",
    "shadow_creation_failed": "shadow_creation_failed",
    "shadow_invalid": "shadow_invalid",
    "shadow_missing": "shadow_invalid",
    "shadow_validation_failed": "shadow_invalid",
    "shadow_write_failed": "shadow_write_failed",
}


class ReindexValidationError(RuntimeError):
    """A value-free, phase-local maintenance refusal."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _contains_source_limit(error: BaseException) -> bool:
    """Recognize a TaskGroup-wrapped storage cap without exposing details."""
    if isinstance(error, ReindexSourceLimitExceeded):
        return True
    return any(
        _contains_source_limit(nested)
        for nested in getattr(error, "exceptions", ())
    )


def _find_post_switch_error(
    error: BaseException,
) -> ReindexPostSwitchError | None:
    """Find a retry-unsafe marker hidden by a failing context cleanup."""

    limit = 16
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    scheduled: set[int] = {id(error)}
    while pending and len(seen) < limit:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, ReindexPostSwitchError):
            return current
        nested_errors: tuple[BaseException | None, ...] = (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        )
        for nested in nested_errors:
            if len(scheduled) >= limit:
                break
            if isinstance(nested, BaseException) and id(nested) not in scheduled:
                scheduled.add(id(nested))
                pending.append(nested)
        if isinstance(current, BaseExceptionGroup):
            for nested in current.exceptions:
                if len(scheduled) >= limit:
                    break
                if id(nested) not in scheduled:
                    scheduled.add(id(nested))
                    pending.append(nested)
    return None


@dataclass(frozen=True, slots=True)
class _SourceDocument:
    memory_id: str
    document_id: str
    filename: str
    uri: str
    sha256: str
    size_bytes: int
    status: str
    chunk_count: int
    object_key: str
    content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    documents: tuple[_SourceDocument, ...]
    fingerprint: str


IdleCheck = Callable[[str], Awaitable[bool]]
TextExtractor = Callable[[bytes, str], str]


class ReindexService:
    """Execute one explicit non-resumable reindex under maintenance exclusion."""

    def __init__(
        self,
        *,
        graph: Any,
        storage: Any,
        chunker: Any,
        embedder: Any,
        vectors: VectorStoreService,
        text_extractor: TextExtractor,
        coordinator: MaintenanceCoordinator | None = None,
        idle_check: IdleCheck | None = None,
    ) -> None:
        self._graph = graph
        self._storage = storage
        self._chunker = chunker
        self._embedder = embedder
        self._vectors = vectors
        self._text_extractor = text_extractor
        self._coordinator = coordinator
        self._idle_check = idle_check

    @staticmethod
    def _base_result(operation_id: str) -> dict:
        return {
            "status": "error",
            "phase": "admission",
            "reason": None,
            "operation_id": operation_id,
            "source_documents": 0,
            "source_chunks": 0,
            "vectors_written": 0,
            "activated": False,
            "active_state": "unavailable",
        }

    @staticmethod
    def _validate_graph_row(memory_id: str, row: object) -> dict:
        if type(row) is not dict or set(row) != _GRAPH_SOURCE_FIELDS:
            raise ReindexValidationError("source_inventory_invalid")
        for key in ("memory_id", "document_id", "filename", "uri", "sha256"):
            if type(row[key]) is not str or not row[key]:
                raise ReindexValidationError("source_inventory_invalid")
        if row["memory_id"] != memory_id:
            raise ReindexValidationError("source_ownership_invalid")
        sha256 = row["sha256"]
        if len(sha256) != 64 or any(character not in _HEX for character in sha256):
            raise ReindexValidationError("source_inventory_invalid")
        for key in ("size_bytes", "chunk_count"):
            value = row[key]
            if type(value) is not int or value < 0:
                raise ReindexValidationError("source_inventory_invalid")
        if row["status"] != "succeeded":
            raise ReindexValidationError("source_status_invalid")
        return row

    @staticmethod
    def _validate_object_row(memory_id: str, row: object) -> dict:
        if type(row) is not dict or set(row) != _OBJECT_SOURCE_FIELDS:
            raise ReindexValidationError("source_inventory_invalid")
        key = row["key"]
        uri = row["uri"]
        size = row["size_bytes"]
        metadata = row["metadata"]
        if (
            type(key) is not str
            or not key.startswith(f"{memory_id}/documents/")
            or key == f"{memory_id}/documents/"
            or type(uri) is not str
            or not uri
            or type(size) is not int
            or size < 0
            or type(metadata) is not dict
            or any(
                type(meta_key) is not str or type(meta_value) is not str
                for meta_key, meta_value in metadata.items()
            )
        ):
            raise ReindexValidationError("source_inventory_invalid")
        if metadata.get("memory_id") != memory_id:
            raise ReindexValidationError("source_ownership_invalid")
        return row

    @staticmethod
    def _stored_filename(filename: str) -> str:
        try:
            filename.encode("ascii")
            return filename
        except UnicodeEncodeError:
            return url_quote(filename, safe="")

    @staticmethod
    def _is_ontology_config(
        memory_id: str,
        ontology_uri: str,
        row: dict,
    ) -> bool:
        """Recognize only one unreferenced object shape written by memory_create."""
        metadata = row["metadata"]
        ontology_name = metadata.get("ontology_name")
        filename = metadata.get("original_filename")
        digest = metadata.get("doc_hash")
        if (
            metadata.get("type") != "ontology"
            or metadata.get("memory_id") != memory_id
            or type(ontology_name) is not str
            or not ontology_name
            or type(filename) is not str
            or filename != f"_ontology_{ontology_name}.yaml"
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in _HEX for character in digest)
        ):
            return False
        return (
            row["uri"] == ontology_uri
            and row["key"]
            == f"{memory_id}/documents/{digest[:8]}_{filename}"
        )

    async def _capture_source_snapshot(self, memory_id: str) -> _SourceSnapshot:
        try:
            list_documents = self._graph.list_reindex_documents
            list_objects = self._storage.list_reindex_objects
            get_ontology_uri = self._graph.get_reindex_ontology_uri

            async def invoke(callable_):
                return await callable_(memory_id)

            async with asyncio.TaskGroup() as group:
                documents_task = group.create_task(invoke(list_documents))
                objects_task = group.create_task(invoke(list_objects))
                ontology_task = group.create_task(invoke(get_ontology_uri))
            raw_documents = documents_task.result()
            raw_objects = objects_task.result()
            ontology_uri = ontology_task.result()
        except Exception as error:
            reason = (
                "source_size_limit_exceeded"
                if _contains_source_limit(error)
                else "source_inventory_unavailable"
            )
            raise ReindexValidationError(reason) from None
        if type(raw_documents) is not list or type(raw_objects) is not list:
            raise ReindexValidationError("source_inventory_invalid")
        if ontology_uri is not None and (
            type(ontology_uri) is not str or not ontology_uri
        ):
            raise ReindexValidationError("source_inventory_invalid")

        documents = [
            self._validate_graph_row(memory_id, row) for row in raw_documents
        ]
        objects = [
            self._validate_object_row(memory_id, row) for row in raw_objects
        ]
        if (
            len(documents) > MAX_REINDEX_SOURCE_DOCUMENTS
            or any(
                row["size_bytes"] > MAX_INGEST_SIZE_BYTES for row in objects
            )
            or sum(row["size_bytes"] for row in objects)
            > MAX_REINDEX_SOURCE_TOTAL_BYTES
            or sum(row["size_bytes"] for row in documents)
            > MAX_REINDEX_SOURCE_TOTAL_BYTES
            or sum(row["chunk_count"] for row in documents)
            > _MAX_REINDEX_CHUNKS
        ):
            raise ReindexValidationError("source_size_limit_exceeded")
        if not documents:
            raise ReindexValidationError("source_inventory_empty")

        documents.sort(key=lambda row: row["document_id"])
        objects.sort(key=lambda row: row["key"])
        if len({row["document_id"] for row in documents}) != len(documents):
            raise ReindexValidationError("source_document_duplicate")
        if len({row["key"] for row in objects}) != len(objects):
            raise ReindexValidationError("source_object_duplicate")

        object_by_uri = {row["uri"]: row for row in objects}
        if len(object_by_uri) != len(objects):
            raise ReindexValidationError("source_object_duplicate")
        referenced_uris = {row["uri"] for row in documents}
        if not referenced_uris.issubset(object_by_uri):
            raise ReindexValidationError("source_object_mismatch")
        unreferenced_uris = set(object_by_uri) - referenced_uris
        expected_unreferenced: set[str] = set()
        if ontology_uri is not None:
            ontology_object = object_by_uri.get(ontology_uri)
            if (
                ontology_object is None
                or ontology_uri in referenced_uris
                or not self._is_ontology_config(
                    memory_id,
                    ontology_uri,
                    ontology_object,
                )
            ):
                raise ReindexValidationError("source_object_mismatch")
            expected_unreferenced.add(ontology_uri)
        if unreferenced_uris != expected_unreferenced:
            raise ReindexValidationError("source_object_mismatch")

        validated_documents: list[_SourceDocument] = []
        content_by_key: dict[str, bytes] = {}
        for row in documents:
            obj = object_by_uri[row["uri"]]
            metadata = obj["metadata"]
            if row["size_bytes"] != obj["size_bytes"]:
                raise ReindexValidationError("source_size_mismatch")
            expected_filename = self._stored_filename(row["filename"])
            if metadata.get("original_filename") != expected_filename:
                raise ReindexValidationError("source_metadata_mismatch")
            metadata_hash = metadata.get("doc_hash")
            if metadata_hash is not None and metadata_hash != row["sha256"]:
                raise ReindexValidationError("source_metadata_mismatch")
            content = content_by_key.get(obj["key"])
            if content is None:
                try:
                    content = await self._storage.read_reindex_object(
                        memory_id,
                        obj["key"],
                        obj["size_bytes"],
                    )
                except Exception:
                    raise ReindexValidationError(
                        "source_object_unavailable"
                    ) from None
                content_by_key[obj["key"]] = content
            if (
                len(content) != row["size_bytes"]
                or hashlib.sha256(content).hexdigest() != row["sha256"]
            ):
                raise ReindexValidationError("source_hash_mismatch")
            validated_documents.append(
                _SourceDocument(
                    memory_id=row["memory_id"],
                    document_id=row["document_id"],
                    filename=row["filename"],
                    uri=row["uri"],
                    sha256=row["sha256"],
                    size_bytes=row["size_bytes"],
                    status=row["status"],
                    chunk_count=row["chunk_count"],
                    object_key=obj["key"],
                    content=content,
                )
            )

        fingerprint_payload = {
            "ontology_uri": ontology_uri,
            "documents": [
                {
                    "memory_id": doc.memory_id,
                    "document_id": doc.document_id,
                    "filename": doc.filename,
                    "uri": doc.uri,
                    "sha256": doc.sha256,
                    "size_bytes": doc.size_bytes,
                    "status": doc.status,
                    "chunk_count": doc.chunk_count,
                    "object_key": doc.object_key,
                }
                for doc in validated_documents
            ],
            "objects": [
                {
                    "key": row["key"],
                    "uri": row["uri"],
                    "size_bytes": row["size_bytes"],
                    "metadata": row["metadata"],
                }
                for row in objects
            ],
        }
        encoded = json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return _SourceSnapshot(
            documents=tuple(validated_documents),
            fingerprint=hashlib.sha256(encoded).hexdigest(),
        )

    async def _prepare_chunks(
        self,
        snapshot: _SourceSnapshot,
    ) -> dict[str, list[Chunk]]:
        chunks_by_document: dict[str, list[Chunk]] = {}
        for document in snapshot.documents:
            try:
                text = await asyncio.to_thread(
                    self._text_extractor,
                    document.content,
                    document.filename,
                )
            except Exception:
                raise ReindexValidationError("source_extraction_failed") from None
            if type(text) is not str or not text.strip():
                raise ReindexValidationError("source_extraction_failed")
            try:
                chunks = await asyncio.to_thread(
                    self._chunker.chunk_document,
                    text,
                    document.filename,
                )
            except Exception:
                raise ReindexValidationError("source_chunking_failed") from None
            if type(chunks) is not list or len(chunks) != document.chunk_count:
                raise ReindexValidationError("source_chunk_accounting_mismatch")
            if not chunks:
                raise ReindexValidationError("source_chunks_empty")
            for expected_index, chunk in enumerate(chunks):
                if (
                    type(chunk) is not Chunk
                    or type(chunk.index) is not int
                    or chunk.index != expected_index
                    or type(chunk.total_chunks) is not int
                    or chunk.total_chunks != len(chunks)
                    or type(chunk.text) is not str
                    or not chunk.text
                ):
                    raise ReindexValidationError(
                        "source_chunk_accounting_mismatch"
                    )
                chunk.doc_id = document.document_id
                chunk.memory_id = document.memory_id
            chunks_by_document[document.document_id] = chunks
        return chunks_by_document

    @staticmethod
    def _evidence(result: EmbeddingResult) -> tuple:
        return (
            result.configured_model,
            result.resolved_model,
            result.model_evidence,
            result.effective_dimensions,
        )

    async def reindex(self, memory_id: str) -> dict:
        operation_id = uuid4().hex
        result = self._base_result(operation_id)
        phase = "admission"
        initial_state = "unavailable"
        identity: EmbeddingCollectionIdentity | None = None

        try:
            coordinator = self._coordinator or get_maintenance_coordinator()
            async with coordinator.maintenance(
                memory_id,
                idle_check=(
                    None
                    if self._idle_check is None
                    else lambda: self._idle_check(memory_id)
                ),
            ):
                state, initial_target = await self._vectors.inspect_reindex_state(
                    memory_id
                )
                initial_state = state.get("state", "unavailable")
                result["active_state"] = initial_state
                if initial_state != "reindex_required":
                    raise ReindexValidationError("initial_state_invalid")

                initial_profile = self._vectors.reindex_profile()
                try:
                    initial_chunking = self._chunker.configuration_signature()
                except Exception:
                    raise ReindexValidationError("chunking_config_unavailable") from None
                if (
                    type(initial_chunking) is not tuple
                    or len(initial_chunking) != 2
                    or any(type(value) is not int or value < 0 for value in initial_chunking)
                ):
                    raise ReindexValidationError("chunking_config_unavailable")

                phase = "snapshot"
                result["phase"] = phase
                snapshot = await self._capture_source_snapshot(memory_id)
                result["source_documents"] = len(snapshot.documents)

                chunks_by_document = await self._prepare_chunks(snapshot)
                expected_chunks = {
                    document_id: len(chunks)
                    for document_id, chunks in chunks_by_document.items()
                }
                result["source_chunks"] = sum(expected_chunks.values())

                phase = "rebuild"
                result["phase"] = phase
                expected_evidence = None
                for document in snapshot.documents:
                    chunks = chunks_by_document[document.document_id]
                    for start in range(0, len(chunks), _EMBED_BATCH_SIZE):
                        batch = chunks[start : start + _EMBED_BATCH_SIZE]
                        try:
                            embedding_result = await self._embedder.embed_texts_result(
                                [chunk.text for chunk in batch]
                            )
                        except Exception:
                            raise ReindexValidationError("embedding_failed") from None
                        if (
                            type(embedding_result) is not EmbeddingResult
                            or len(embedding_result.vectors) != len(batch)
                        ):
                            raise ReindexValidationError("embedding_invalid")
                        evidence = self._evidence(embedding_result)
                        if expected_evidence is None:
                            expected_evidence = evidence
                            identity = await self._vectors.create_reindex_shadow(
                                memory_id,
                                operation_id,
                                embedding_result=embedding_result,
                            )
                        elif evidence != expected_evidence:
                            raise ReindexValidationError("embedding_identity_changed")
                        if identity is None:
                            raise ReindexValidationError("embedding_invalid")
                        written = await self._vectors.store_reindex_chunks(
                            memory_id,
                            operation_id,
                            document.document_id,
                            document.filename,
                            batch,
                            embedding_result=embedding_result,
                            identity=identity,
                        )
                        if written != len(batch):
                            raise ReindexValidationError("shadow_write_failed")
                        result["vectors_written"] += written

                if identity is None or result["vectors_written"] != result["source_chunks"]:
                    raise ReindexValidationError("shadow_write_failed")

                phase = "validate"
                result["phase"] = phase
                validated = await self._vectors.validate_reindex_shadow(
                    memory_id,
                    operation_id,
                    identity=identity,
                    expected_chunks=expected_chunks,
                )
                if validated != result["source_chunks"]:
                    raise ReindexValidationError("shadow_invalid")

                phase = "pre_switch"
                result["phase"] = phase
                final_snapshot = await self._capture_source_snapshot(memory_id)
                if final_snapshot.fingerprint != snapshot.fingerprint:
                    raise ReindexValidationError("source_changed")
                if self._chunker.configuration_signature() != initial_chunking:
                    raise ReindexValidationError("chunking_config_changed")
                if self._vectors.reindex_profile() != initial_profile:
                    raise ReindexValidationError("embedding_profile_changed")
                final_state, final_target = await self._vectors.inspect_reindex_state(
                    memory_id
                )
                if final_state != state or final_target != initial_target:
                    raise ReindexValidationError("active_target_changed")

                try:
                    activated_count = await self._vectors.activate_reindex_shadow(
                        memory_id,
                        operation_id,
                        identity=identity,
                        expected_chunks=expected_chunks,
                        expected_target=initial_target,
                    )
                    if activated_count != result["source_chunks"]:
                        raise ReindexPostSwitchError("post_switch_unverified")
                except ReindexPostSwitchError as error:
                    # Record the retry-unsafe boundary while still inside the
                    # maintenance context. Its cleanup can itself fail and
                    # replace this exception before the outer handlers see it.
                    result.update(
                        {
                            "phase": "activated",
                            "activated": bool(error.activated),
                            "active_state": "unavailable",
                        }
                    )
                    raise

                result.update(
                    {
                        "status": "ok",
                        "phase": "verified",
                        "reason": None,
                        "activated": True,
                        "active_state": "ready",
                    }
                )
                return result

        except MaintenanceAdmissionError:
            result.update(
                {
                    "phase": "admission",
                    "reason": "namespace_busy",
                    "active_state": "unavailable",
                }
            )
            return result
        except ReindexPostSwitchError as error:
            result.update(
                {
                    "phase": "activated",
                    "reason": _SAFE_REASON_BY_VECTOR_REASON.get(
                        error.reason,
                        "post_switch_unverified",
                    ),
                    "activated": True,
                    "active_state": "unavailable",
                }
            )
            return result
        except ReindexValidationError as error:
            result.update(
                {
                    "phase": phase,
                    "reason": error.reason,
                    "active_state": initial_state,
                }
            )
            return result
        except EmbeddingCollectionError as error:
            result.update(
                {
                    "phase": phase,
                    "reason": _SAFE_REASON_BY_VECTOR_REASON.get(
                        error.reason,
                        "backend_unavailable",
                    ),
                    "active_state": initial_state,
                }
            )
            return result
        except Exception as error:
            post_switch_error = _find_post_switch_error(error)
            if post_switch_error is not None:
                result.update(
                    {
                        "status": "error",
                        "phase": "activated",
                        "reason": _SAFE_REASON_BY_VECTOR_REASON.get(
                            post_switch_error.reason,
                            "post_switch_unverified",
                        ),
                        "activated": bool(post_switch_error.activated),
                        "active_state": "unavailable",
                    }
                )
            elif result["activated"]:
                # The alias was switched and positively re-read before the
                # maintenance context began releasing its ownership.  A later
                # cleanup/coordinator failure must not preserve the already-set
                # success status or pretend this retry-unsafe operation is still
                # pre-switch.  Keep both generations intact and surface the
                # canonical post-switch uncertainty envelope.
                result.update(
                    {
                        "status": "error",
                        "phase": "activated",
                        "reason": "post_switch_unverified",
                        "active_state": "unavailable",
                    }
                )
            else:
                result.update(
                    {
                        "status": "error",
                        "phase": phase,
                        "reason": "backend_unavailable",
                        "active_state": initial_state,
                    }
                )
            return result


_reindex_service: ReindexService | None = None


def get_reindex_service() -> ReindexService:
    """Return the process-wide service, wired only to embedded GM singletons."""
    global _reindex_service
    if _reindex_service is None:
        from ..server import _extract_text
        from .chunker import get_chunker
        from .embedder import get_embedding_service
        from .graph import get_graph_service
        from .ingest_queue import get_ingest_queue
        from .storage import get_storage_service
        from .vector_store import get_vector_store

        queue = get_ingest_queue()
        _reindex_service = ReindexService(
            graph=get_graph_service(),
            storage=get_storage_service(),
            chunker=get_chunker(),
            embedder=get_embedding_service(),
            vectors=get_vector_store(),
            text_extractor=_extract_text,
            idle_check=queue.is_idle_for_memory,
        )
    return _reindex_service


def reset_reindex_service_for_tests() -> None:
    global _reindex_service
    _reindex_service = None
