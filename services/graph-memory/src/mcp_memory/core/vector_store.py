# -*- coding: utf-8 -*-
"""Qdrant storage with fail-closed embedding identity (#277)."""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import warnings
from dataclasses import dataclass, field as dataclass_field
from functools import wraps
from typing import List, Mapping, Optional
from uuid import UUID, uuid4

from hivemind_inference import (
    EmbeddingCollectionIdentity,
    EmbeddingIdentityError,
    EmbeddingResult,
    ResolvedEmbeddingProfile,
    build_configured_embedding_collection_identity,
    build_embedding_collection_identity,
    canonical_qdrant_collection_name,
    parse_embedding_collection_identity,
    validate_embedding_collection_identity,
)
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .models import Chunk, ChunkResult

DEFAULT_LEGACY_COLLECTION_PREFIX = "memory_"
MAX_QDRANT_COLLECTION_NAME_LENGTH = 255
LEGACY_PREFIX_DIAGNOSTIC = (
    "QDRANT_COLLECTION_PREFIX is legacy-probe-only; non-default values do not "
    "scope canonical collections; shared Qdrant deployments are unsupported"
)

_prefix_diagnostic_lock = threading.Lock()
_prefix_diagnostic_emitted = False


async def _run_blocking_maintenance_call(callable_, /, *args, **kwargs):
    """Offload Qdrant work without releasing exclusion before it really stops.

    ``asyncio.to_thread`` cannot stop its worker when the awaiting task is
    cancelled.  Returning immediately from a cancelled final activation would
    release the maintenance gate while the worker could still switch the alias.
    Shield the worker and drain it under repeated cancellation before
    propagating cancellation to the caller.  Every underlying Qdrant request
    retains its own finite transport timeout.
    """

    worker = asyncio.create_task(asyncio.to_thread(callable_, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                # The worker reached a terminal error while the caller was
                # already cancelled. Retrieve it below, then preserve the
                # authoritative caller cancellation.
                break
        try:
            worker.result()
        except BaseException:
            # Cancellation remains authoritative for the abandoned caller, but
            # retrieving the terminal result prevents a lost worker exception.
            pass
        raise cancelled


def _guard_namespace_mutation(method):
    """Hold the process-local ordinary-mutation admission for one async call."""

    @wraps(method)
    async def guarded(self, memory_id: str, *args, **kwargs):
        from .maintenance import get_maintenance_coordinator

        async with get_maintenance_coordinator().ordinary(memory_id):
            return await method(self, memory_id, *args, **kwargs)

    return guarded


def _guard_memory_operation(method):
    """Serialize one exact namespace before any lock-taking operation."""

    @wraps(method)
    async def guarded(self, memory_id: str, *args, **kwargs):
        async with self._memory_async_lock(memory_id):
            return await method(self, memory_id, *args, **kwargs)

    return guarded


def _emit_legacy_prefix_diagnostic_once(prefix: str) -> None:
    global _prefix_diagnostic_emitted
    if prefix == DEFAULT_LEGACY_COLLECTION_PREFIX:
        return
    with _prefix_diagnostic_lock:
        if _prefix_diagnostic_emitted:
            return
        warnings.warn(LEGACY_PREFIX_DIAGNOSTIC, FutureWarning, stacklevel=3)
        _prefix_diagnostic_emitted = True


def _reset_legacy_prefix_diagnostic_for_tests() -> None:
    global _prefix_diagnostic_emitted
    with _prefix_diagnostic_lock:
        _prefix_diagnostic_emitted = False


class EmbeddingCollectionError(RuntimeError):
    state = "unavailable"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"embedding collection {self.state}: {reason}")


class EmbeddingCollectionReindexRequired(EmbeddingCollectionError):
    state = "reindex_required"


class EmbeddingCollectionUnavailable(EmbeddingCollectionError):
    state = "unavailable"


class ReindexPostSwitchError(EmbeddingCollectionUnavailable):
    """The atomic alias batch may have committed; rollback is forbidden."""

    activated = True


@dataclass(frozen=True, slots=True)
class _ResolvedCollection:
    name: str
    identity: EmbeddingCollectionIdentity
    points_count: int
    active_alias: str | None = None


@dataclass(frozen=True, slots=True)
class ReindexTargetSnapshot:
    """Value-only selector snapshot used to detect a pre-cutover race."""

    active_alias_records: tuple[str | None, ...] = dataclass_field(repr=False)
    active_alias_present: bool
    active_alias_target: str | None = dataclass_field(repr=False)
    canonical_exists: bool
    legacy_exists: bool


class VectorStoreService:
    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        profile: ResolvedEmbeddingProfile | None = None,
        legacy_prefix: str | None = None,
    ):
        settings = None
        if client is None or legacy_prefix is None:
            from ..config import get_settings

            settings = get_settings()
        self._client = client or QdrantClient(
            url=settings.qdrant_url,
            timeout=30,
        )
        self._profile_override = profile
        self._legacy_prefix = (
            legacy_prefix
            if legacy_prefix is not None
            else settings.qdrant_collection_prefix
        )
        if type(self._legacy_prefix) is not str:
            raise ValueError("QDRANT_COLLECTION_PREFIX must be a string")
        _emit_legacy_prefix_diagnostic_once(self._legacy_prefix)
        self._locks_guard = threading.Lock()
        self._memory_locks: dict[str, threading.RLock] = {}
        self._memory_async_locks: dict[str, asyncio.Lock] = {}

    def _memory_lock(self, memory_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._memory_locks.setdefault(memory_id, threading.RLock())

    def _memory_async_lock(self, memory_id: str) -> asyncio.Lock:
        with self._locks_guard:
            return self._memory_async_locks.setdefault(memory_id, asyncio.Lock())

    def _embedding_profile(self) -> ResolvedEmbeddingProfile:
        try:
            profile = self._profile_override
            if profile is None:
                from .inference_runtime import resolved_embedding_profile

                profile = resolved_embedding_profile()
            if type(profile) is not ResolvedEmbeddingProfile:
                raise RuntimeError("resolved embedding profile is invalid")
            return profile
        except Exception:
            raise EmbeddingCollectionUnavailable(
                "embedding_profile_unavailable"
            ) from None

    def _canonical_name(self, memory_id: str) -> str:
        try:
            return canonical_qdrant_collection_name(memory_id)
        except EmbeddingIdentityError:
            raise ValueError("memory_id is invalid") from None

    def _legacy_name(self, memory_id: str) -> str:
        # Reproduce the deployed lossy probe; never use it as a semantic target.
        safe_id = "".join(c if c.isalnum() else "_" for c in memory_id)
        return f"{self._legacy_prefix}{safe_id}"

    def _active_alias_name(self, memory_id: str) -> str:
        name = f"{self._canonical_name(memory_id)}__active_v1"
        if len(name) > MAX_QDRANT_COLLECTION_NAME_LENGTH:
            raise ValueError("active alias name is too long")
        return name

    def _shadow_name(self, memory_id: str, operation_id: str) -> str:
        if (
            type(operation_id) is not str
            or len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
        ):
            raise ValueError("operation_id must be 32 lowercase hex characters")
        name = f"{self._canonical_name(memory_id)}__shadow_v1_{operation_id}"
        if len(name) > MAX_QDRANT_COLLECTION_NAME_LENGTH:
            raise ValueError("shadow collection name is too long")
        return name

    def _active_alias_records(self, memory_id: str) -> tuple[str | None, ...]:
        alias_name = self._active_alias_name(memory_id)
        try:
            aliases = self._client.get_aliases().aliases
        except Exception:
            raise EmbeddingCollectionUnavailable(
                "active_alias_unreadable"
            ) from None
        if type(aliases) is not list:
            raise EmbeddingCollectionUnavailable("active_alias_unreadable")
        try:
            targets = [
                getattr(alias, "collection_name", None)
                for alias in aliases
                if getattr(alias, "alias_name", None) == alias_name
            ]
        except Exception:
            raise EmbeddingCollectionUnavailable(
                "active_alias_unreadable"
            ) from None
        if any(target is not None and type(target) is not str for target in targets):
            raise EmbeddingCollectionUnavailable("active_alias_unreadable")
        return tuple(
            sorted(
                targets,
                key=lambda target: (target is not None, target or ""),
            )
        )

    def _active_alias_target_from_records(
        self,
        memory_id: str,
        records: tuple[str | None, ...],
    ) -> str | None:
        if not records:
            return None
        if len(records) != 1:
            raise EmbeddingCollectionReindexRequired("active_alias_invalid")
        target = records[0]
        alias_name = self._active_alias_name(memory_id)
        if type(target) is not str or not target or target == alias_name:
            raise EmbeddingCollectionReindexRequired("active_alias_invalid")
        return target

    def _active_alias_target(self, memory_id: str) -> str | None:
        return self._active_alias_target_from_records(
            memory_id,
            self._active_alias_records(memory_id),
        )

    def _reindex_target_snapshot(self, memory_id: str) -> ReindexTargetSnapshot:
        records = self._active_alias_records(memory_id)
        if records:
            try:
                target = self._active_alias_target_from_records(memory_id, records)
            except EmbeddingCollectionReindexRequired:
                target = None
            return ReindexTargetSnapshot(
                active_alias_records=records,
                active_alias_present=True,
                active_alias_target=target,
                canonical_exists=False,
                legacy_exists=False,
            )
        canonical_name = self._canonical_name(memory_id)
        legacy_name = self._legacy_name(memory_id)
        return ReindexTargetSnapshot(
            active_alias_records=(),
            active_alias_present=False,
            active_alias_target=None,
            canonical_exists=self._collection_exists(canonical_name),
            legacy_exists=(
                legacy_name != canonical_name and self._collection_exists(legacy_name)
            ),
        )

    @staticmethod
    def _memory_filter(
        memory_id: str,
        *conditions: qmodels.FieldCondition,
    ) -> qmodels.Filter:
        return qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="memory_id",
                    match=qmodels.MatchValue(value=memory_id),
                ),
                *conditions,
            ]
        )

    @staticmethod
    def _raise_identity_error(error: EmbeddingIdentityError) -> None:
        raise EmbeddingCollectionReindexRequired(error.reason) from None

    def _collection_exists(self, name: str) -> bool:
        try:
            return bool(self._client.collection_exists(name))
        except Exception:
            raise EmbeddingCollectionUnavailable("qdrant_unreadable") from None

    @staticmethod
    def _require_completed_mutation(result: object) -> None:
        """Reject false/no-op Qdrant acknowledgements for synchronous writes."""
        if result is True:
            return
        if getattr(result, "status", None) == qmodels.UpdateStatus.COMPLETED:
            return
        raise EmbeddingCollectionUnavailable("shadow_creation_failed")

    def _uses_local_qdrant(self) -> bool:
        client = self._client
        for _ in range(4):
            inner = getattr(client, "_client", None)
            if inner is None or inner is client:
                break
            client = inner
        return client.__class__.__module__.startswith("qdrant_client.local")

    def _validate_reindex_payload_schema(self, name: str) -> None:
        """Verify required indexes when the backend exposes real index state."""
        if self._uses_local_qdrant():
            return
        try:
            info = self._client.get_collection(collection_name=name)
            payload_schema = info.payload_schema
        except Exception:
            raise EmbeddingCollectionUnavailable(
                "shadow_validation_failed"
            ) from None
        expected = {
            "doc_id": qmodels.PayloadSchemaType.KEYWORD,
            "memory_id": qmodels.PayloadSchemaType.KEYWORD,
            "chunk_index": qmodels.PayloadSchemaType.INTEGER,
        }
        if type(payload_schema) is not dict:
            raise EmbeddingCollectionReindexRequired("shadow_invalid")
        for field_name, field_type in expected.items():
            schema = payload_schema.get(field_name)
            actual_type = getattr(schema, "data_type", schema)
            if actual_type != field_type:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")

    def _probe_legacy(self, memory_id: str, canonical_name: str) -> None:
        legacy_name = self._legacy_name(memory_id)
        if legacy_name == canonical_name or not self._collection_exists(legacy_name):
            return
        try:
            count = self._client.count(
                collection_name=legacy_name,
                exact=True,
            ).count
        except Exception:
            raise EmbeddingCollectionReindexRequired(
                "legacy_unreadable"
            ) from None
        if type(count) is not int or count < 0:
            raise EmbeddingCollectionReindexRequired("legacy_unreadable")
        if count:
            raise EmbeddingCollectionReindexRequired("legacy_nonempty")

    def _read_canonical(
        self,
        memory_id: str,
        name: str,
        *,
        result: EmbeddingResult | None,
    ) -> _ResolvedCollection:
        try:
            info = self._client.get_collection(collection_name=name)
        except Exception:
            raise EmbeddingCollectionUnavailable("canonical_unreadable") from None

        profile = self._embedding_profile()
        try:
            identity = parse_embedding_collection_identity(info.config.metadata)
            validate_embedding_collection_identity(
                identity,
                memory_id=memory_id,
                profile=profile,
                result=result,
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)
        except Exception:
            raise EmbeddingCollectionUnavailable("canonical_unreadable") from None

        try:
            vectors = info.config.params.vectors
            if (
                type(vectors) is not qmodels.VectorParams
                or type(vectors.size) is not int
                or vectors.size != identity.dimensions
                or vectors.distance != qmodels.Distance.COSINE
            ):
                raise EmbeddingCollectionReindexRequired(
                    "vector_config_mismatch"
                )
        except EmbeddingCollectionReindexRequired:
            raise
        except Exception:
            raise EmbeddingCollectionReindexRequired(
                "vector_config_mismatch"
            ) from None

        try:
            total = self._client.count(
                collection_name=name,
                exact=True,
            ).count
        except Exception:
            raise EmbeddingCollectionUnavailable("canonical_unreadable") from None
        if type(total) is not int or total < 0:
            raise EmbeddingCollectionUnavailable("canonical_unreadable")
        return _ResolvedCollection(
            name=name,
            identity=identity,
            points_count=total,
        )

    def _create_canonical(
        self,
        memory_id: str,
        name: str,
        identity: EmbeddingCollectionIdentity,
    ) -> None:
        try:
            validate_embedding_collection_identity(
                identity,
                memory_id=memory_id,
                profile=self._embedding_profile(),
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)
        try:
            self._client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=identity.dimensions,
                    distance=qmodels.Distance.COSINE,
                ),
                metadata=identity.to_mapping(),
            )
            self._client.create_payload_index(
                collection_name=name,
                field_name="doc_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
                wait=True,
            )
            self._client.create_payload_index(
                collection_name=name,
                field_name="memory_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception:
            # Accept a concurrent create only after the resolver re-reads it.
            if not self._collection_exists(name):
                raise EmbeddingCollectionUnavailable("creation_failed") from None

    def _resolve_collection(
        self,
        memory_id: str,
        *,
        result: EmbeddingResult | None = None,
        create_identity: EmbeddingCollectionIdentity | None = None,
        target_snapshot: ReindexTargetSnapshot | None = None,
    ) -> _ResolvedCollection | None:
        """Resolve the only concrete target; never issue a transferable grant."""
        active_alias = self._active_alias_name(memory_id)
        if target_snapshot is None:
            alias_target = self._active_alias_target(memory_id)
        elif target_snapshot.active_alias_present:
            alias_target = target_snapshot.active_alias_target
            if alias_target is None:
                raise EmbeddingCollectionReindexRequired("active_alias_invalid")
        else:
            alias_target = None
        if alias_target is not None:
            if not self._collection_exists(alias_target):
                raise EmbeddingCollectionReindexRequired("active_alias_invalid")
            resolved = self._read_canonical(
                memory_id,
                alias_target,
                result=result,
            )
            self._validate_reindex_payload_schema(alias_target)
            if create_identity is not None and resolved.identity != create_identity:
                raise EmbeddingCollectionReindexRequired(
                    "creation_identity_mismatch"
                )
            return _ResolvedCollection(
                name=resolved.name,
                identity=resolved.identity,
                points_count=resolved.points_count,
                active_alias=active_alias,
            )

        name = self._canonical_name(memory_id)
        self._probe_legacy(memory_id, name)
        if not self._collection_exists(name):
            if create_identity is None:
                return None
            self._create_canonical(memory_id, name, create_identity)
        resolved = self._read_canonical(memory_id, name, result=result)
        if create_identity is not None and resolved.identity != create_identity:
            raise EmbeddingCollectionReindexRequired(
                "creation_identity_mismatch"
            )
        return resolved

    @_guard_memory_operation
    async def ensure_collection(self, memory_id: str) -> bool:
        with self._memory_lock(memory_id):
            return self._resolve_collection(memory_id) is not None

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def delete_collection(self, memory_id: str) -> bool:
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                return False
            if resolved.active_alias is not None:
                raise EmbeddingCollectionUnavailable(
                    "active_alias_delete_unsupported"
                )
            initial_selector = (resolved.name, resolved.active_alias)
            # Re-resolve immediately before the destructive operation.
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                raise EmbeddingCollectionUnavailable("collection_race")
            if resolved.active_alias is not None:
                raise EmbeddingCollectionUnavailable(
                    "active_alias_delete_unsupported"
                )
            if (resolved.name, resolved.active_alias) != initial_selector:
                raise EmbeddingCollectionUnavailable("collection_race")
            self._validate_owner(resolved.name, memory_id, resolved.points_count)
            try:
                self._client.delete_collection(collection_name=resolved.name)
            except Exception:
                raise EmbeddingCollectionUnavailable("delete_failed") from None
        return True

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def store_chunks(
        self,
        memory_id: str,
        doc_id: str,
        filename: str,
        chunks: List[Chunk],
        *,
        embedding_result: EmbeddingResult,
    ) -> int:
        if type(embedding_result) is not EmbeddingResult:
            raise ValueError("embedding_result must be an EmbeddingResult")
        if len(chunks) != len(embedding_result.vectors):
            raise ValueError("chunk and embedding cardinality mismatch")
        profile = self._embedding_profile()
        try:
            identity = build_embedding_collection_identity(
                memory_id,
                profile,
                embedding_result,
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)

        points = []
        for chunk, embedding in zip(chunks, embedding_result.vectors):
            points.append(
                qmodels.PointStruct(
                    id=str(uuid4()),
                    vector=list(embedding),
                    payload={
                        "memory_id": memory_id,
                        "doc_id": doc_id,
                        "filename": filename,
                        "text": chunk.text,
                        "chunk_index": chunk.index,
                        "total_chunks": chunk.total_chunks,
                        "section_title": chunk.section_title,
                        "article_number": chunk.article_number,
                        "heading_hierarchy": chunk.heading_hierarchy,
                        "char_count": chunk.char_count,
                        "token_estimate": chunk.token_estimate,
                    },
                )
            )

        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(
                memory_id,
                result=embedding_result,
                create_identity=identity,
            )
            if resolved is None:  # pragma: no cover - create_identity forbids it
                raise EmbeddingCollectionUnavailable("collection_race")
            self._validate_owner(resolved.name, memory_id, resolved.points_count)
            try:
                self._client.upsert(
                    collection_name=resolved.name,
                    points=points,
                    wait=True,
                )
            except Exception:
                raise EmbeddingCollectionUnavailable("upsert_failed") from None
        return len(points)

    @staticmethod
    def _validate_returned_payload(payload: object, memory_id: str) -> dict:
        if type(payload) is not dict or payload.get("memory_id") != memory_id:
            raise EmbeddingCollectionReindexRequired(
                "payload_ownership_mismatch"
            )
        doc_id = payload.get("doc_id")
        if type(doc_id) is not str or not doc_id:
            raise EmbeddingCollectionReindexRequired("payload_schema_mismatch")
        return payload

    def _validate_owner(
        self,
        name: str,
        memory_id: str,
        expected_count: int,
        *,
        scroll_filter: qmodels.Filter | None = None,
    ) -> None:
        seen = 0
        offset = None
        while True:
            try:
                points, next_offset = self._client.scroll(
                    collection_name=name,
                    scroll_filter=scroll_filter,
                    limit=256,
                    offset=offset,
                    with_payload=["memory_id", "doc_id"],
                    with_vectors=False,
                )
                for point in points:
                    self._validate_returned_payload(point.payload, memory_id)
            except EmbeddingCollectionReindexRequired:
                raise
            except Exception:
                raise EmbeddingCollectionUnavailable(
                    "canonical_unreadable"
                ) from None
            seen += len(points)
            if next_offset is None:
                break
            offset = next_offset
        if seen != expected_count:
            raise EmbeddingCollectionUnavailable("canonical_unreadable")

    @_guard_memory_operation
    async def search(
        self,
        memory_id: str,
        *,
        embedding_result: EmbeddingResult,
        doc_ids: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[ChunkResult]:
        """Search only owned payloads after dynamic embedding compatibility."""
        if (
            type(embedding_result) is not EmbeddingResult
            or len(embedding_result.vectors) != 1
        ):
            raise ValueError("query embedding_result must contain one vector")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        conditions: list[qmodels.FieldCondition] = []
        if doc_ids:
            if type(doc_ids) is not list or any(
                type(doc_id) is not str or not doc_id for doc_id in doc_ids
            ):
                raise ValueError("doc_ids must be a list of non-empty strings")
            conditions.append(
                qmodels.FieldCondition(
                    key="doc_id",
                    match=qmodels.MatchAny(any=doc_ids),
                )
            )
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(
                memory_id,
                result=embedding_result,
            )
            if resolved is None:
                return []
            try:
                response = self._client.query_points(
                    collection_name=resolved.name,
                    query=list(embedding_result.vectors[0]),
                    query_filter=self._memory_filter(memory_id, *conditions),
                    limit=limit,
                    with_payload=True,
                )
            except Exception:
                raise EmbeddingCollectionUnavailable("search_failed") from None

        payloads = [
            self._validate_returned_payload(point.payload, memory_id)
            for point in response.points
        ]
        return [
            ChunkResult(
                chunk=Chunk(
                    text=payload.get("text", ""),
                    index=payload.get("chunk_index", 0),
                    total_chunks=payload.get("total_chunks", 0),
                    doc_id=payload.get("doc_id"),
                    memory_id=payload["memory_id"],
                    filename=payload.get("filename"),
                    section_title=payload.get("section_title"),
                    article_number=payload.get("article_number"),
                    heading_hierarchy=payload.get("heading_hierarchy", []),
                    char_count=payload.get("char_count", 0),
                    token_estimate=payload.get("token_estimate", 0),
                ),
                score=point.score,
            )
            for point, payload in zip(response.points, payloads)
        ]

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def delete_document_chunks(self, memory_id: str, doc_id: str) -> int:
        """Delete an owned document only after an immediate identity re-read."""
        condition = qmodels.FieldCondition(
            key="doc_id",
            match=qmodels.MatchValue(value=doc_id),
        )
        delete_filter = self._memory_filter(memory_id, condition)
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                return 0
            try:
                count = self._client.count(
                    collection_name=resolved.name,
                    count_filter=delete_filter,
                    exact=True,
                ).count
            except Exception:
                raise EmbeddingCollectionUnavailable("count_failed") from None
            if type(count) is not int or count < 0:
                raise EmbeddingCollectionUnavailable("count_failed")
            if count == 0:
                return 0
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                raise EmbeddingCollectionUnavailable("collection_race")
            self._validate_owner(
                resolved.name,
                memory_id,
                resolved.points_count,
            )
            try:
                self._client.delete(
                    collection_name=resolved.name,
                    points_selector=qmodels.FilterSelector(filter=delete_filter),
                    wait=True,
                )
            except Exception:
                raise EmbeddingCollectionUnavailable("delete_failed") from None
        return count

    @_guard_memory_operation
    async def count_document_chunks(self, memory_id: str, doc_id: str) -> int:
        count_filter = self._memory_filter(
            memory_id,
            qmodels.FieldCondition(
                key="doc_id",
                match=qmodels.MatchValue(value=doc_id),
            ),
        )
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                return 0
            try:
                count = self._client.count(
                    collection_name=resolved.name,
                    count_filter=count_filter,
                    exact=True,
                ).count
            except Exception:
                raise EmbeddingCollectionUnavailable("count_failed") from None
            if type(count) is not int or count < 0:
                raise EmbeddingCollectionUnavailable("count_failed")
            self._validate_owner(
                resolved.name,
                memory_id,
                count,
                scroll_filter=count_filter,
            )
            return count

    @_guard_memory_operation
    async def list_doc_ids(self, memory_id: str) -> set[str]:
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                return set()
            doc_ids: set[str] = set()
            offset = None
            while True:
                try:
                    points, next_offset = self._client.scroll(
                        collection_name=resolved.name,
                        limit=200,
                        offset=offset,
                        with_payload=["memory_id", "doc_id"],
                        with_vectors=False,
                    )
                except Exception:
                    raise EmbeddingCollectionUnavailable("scroll_failed") from None
                for point in points:
                    payload = self._validate_returned_payload(
                        point.payload, memory_id
                    )
                    doc_id = payload.get("doc_id")
                    if type(doc_id) is not str or not doc_id:
                        raise EmbeddingCollectionReindexRequired(
                            "payload_schema_mismatch"
                        )
                    doc_ids.add(doc_id)
                if next_offset is None:
                    return doc_ids
                offset = next_offset

    @staticmethod
    def _validate_vector(vector: object, dimensions: int) -> list[float]:
        if type(vector) is not list or len(vector) != dimensions:
            raise EmbeddingCollectionReindexRequired("backup_point_invalid")
        for component in vector:
            if (
                not isinstance(component, (int, float))
                or isinstance(component, bool)
            ):
                raise EmbeddingCollectionReindexRequired("backup_point_invalid")
            try:
                finite = math.isfinite(component)
            except (OverflowError, TypeError, ValueError):
                finite = False
            if not finite:
                raise EmbeddingCollectionReindexRequired("backup_point_invalid")
        return list(vector)

    @staticmethod
    def _validate_point_id(point_id: object) -> int | str:
        if type(point_id) is int and 0 <= point_id <= (1 << 64) - 1:
            return point_id
        if type(point_id) is str:
            try:
                if str(UUID(point_id)) == point_id.lower():
                    return point_id
            except (AttributeError, ValueError):
                pass
        raise EmbeddingCollectionReindexRequired("backup_point_invalid")

    def _validate_backup_points(
        self,
        memory_id: str,
        points: object,
        dimensions: int,
    ) -> list[dict]:
        if type(points) is not list:
            raise EmbeddingCollectionReindexRequired("backup_point_invalid")
        validated = []
        for point in points:
            if type(point) is not dict or set(point) != {"id", "vector", "payload"}:
                raise EmbeddingCollectionReindexRequired("backup_point_invalid")
            point_id = self._validate_point_id(point["id"])
            payload = self._validate_returned_payload(
                point["payload"], memory_id
            )
            validated.append(
                {
                    "id": point_id,
                    "vector": self._validate_vector(
                        point["vector"], dimensions
                    ),
                    "payload": dict(payload),
                }
            )
        return validated

    def _validate_backup_identity(
        self,
        memory_id: str,
        identity_mapping: object,
    ) -> EmbeddingCollectionIdentity:
        try:
            identity = parse_embedding_collection_identity(identity_mapping)
            return validate_embedding_collection_identity(
                identity,
                memory_id=memory_id,
                profile=self._embedding_profile(),
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)

    def _prepare_import(
        self,
        memory_id: str,
        identity_mapping: object,
        points: object,
        *,
        scan_target: bool = True,
    ) -> tuple[EmbeddingCollectionIdentity, list[dict]]:
        identity = self._validate_backup_identity(memory_id, identity_mapping)
        validated_points = self._validate_backup_points(
            memory_id,
            points,
            identity.dimensions,
        )
        target = self._resolve_collection(memory_id)
        if target is not None:
            if target.identity != identity:
                raise EmbeddingCollectionReindexRequired(
                    "backup_identity_mismatch"
                )
            if scan_target:
                self._validate_owner(target.name, memory_id, target.points_count)
        return identity, validated_points

    @_guard_memory_operation
    async def preflight_import(
        self,
        memory_id: str,
        identity: object,
        points: object,
    ) -> None:
        """Validate an entire restore bundle without mutating any backend."""
        with self._memory_lock(memory_id):
            self._prepare_import(memory_id, identity, points)

    @_guard_memory_operation
    async def export_collection(self, memory_id: str) -> dict:
        """Export exact stored identity and owned points as one logical bundle."""
        with self._memory_lock(memory_id):
            resolved = self._resolve_collection(memory_id)
            if resolved is None:
                try:
                    identity = build_configured_embedding_collection_identity(
                        memory_id,
                        self._embedding_profile(),
                    )
                except EmbeddingIdentityError as error:
                    self._raise_identity_error(error)
                return {"identity": identity.to_mapping(), "points": []}

            all_points: list[dict] = []
            offset = None
            while True:
                try:
                    points, next_offset = self._client.scroll(
                        collection_name=resolved.name,
                        limit=100,
                        offset=offset,
                        with_payload=True,
                        with_vectors=True,
                    )
                except Exception:
                    raise EmbeddingCollectionUnavailable("export_failed") from None
                for point in points:
                    payload = self._validate_returned_payload(
                        point.payload, memory_id
                    )
                    all_points.append(
                        {
                            "id": self._validate_point_id(point.id),
                            "vector": self._validate_vector(
                                list(point.vector)
                                if type(point.vector) is list
                                else point.vector,
                                resolved.identity.dimensions,
                            ),
                            "payload": dict(payload),
                        }
                    )
                if next_offset is None:
                    break
                offset = next_offset
            if len(all_points) != resolved.points_count:
                raise EmbeddingCollectionUnavailable("export_failed")
        return {
            "identity": resolved.identity.to_mapping(),
            "points": all_points,
        }

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def import_collection(
        self,
        memory_id: str,
        points_data: list[dict],
        *,
        identity: object,
        batch_size: int = 100,
    ) -> int:
        """Restore only a fully prevalidated exact-current identity bundle."""
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        with self._memory_lock(memory_id):
            parsed_identity, points = self._prepare_import(
                memory_id,
                identity,
                points_data,
                scan_target=False,
            )
            if not points:
                return 0
            resolved = self._resolve_collection(
                memory_id,
                create_identity=parsed_identity,
            )
            if resolved is None:  # pragma: no cover - create_identity forbids it
                raise EmbeddingCollectionUnavailable("collection_race")
            self._validate_owner(resolved.name, memory_id, resolved.points_count)
            total = 0
            for start in range(0, len(points), batch_size):
                batch = points[start : start + batch_size]
                # Re-resolve identity immediately before every vector mutation.
                resolved = self._resolve_collection(memory_id)
                if resolved is None:
                    raise EmbeddingCollectionUnavailable("collection_race")
                if resolved.identity != parsed_identity:
                    raise EmbeddingCollectionReindexRequired(
                        "backup_identity_mismatch"
                    )
                structs = [
                    qmodels.PointStruct(
                        id=point["id"],
                        vector=point["vector"],
                        payload=point["payload"],
                    )
                    for point in batch
                ]
                try:
                    self._client.upsert(
                        collection_name=resolved.name,
                        points=structs,
                        wait=True,
                    )
                except Exception:
                    raise EmbeddingCollectionUnavailable("import_failed") from None
                total += len(structs)
        return total

    def reindex_profile(self) -> ResolvedEmbeddingProfile:
        """Return the process-frozen profile object for an in-process race check."""
        return self._embedding_profile()

    @_guard_memory_operation
    async def inspect_reindex_state(
        self,
        memory_id: str,
    ) -> tuple[dict, ReindexTargetSnapshot]:
        """Read the bounded state plus the non-transferable active selector."""
        return await _run_blocking_maintenance_call(
            self._inspect_reindex_state,
            memory_id,
        )

    def _inspect_reindex_state(
        self,
        memory_id: str,
    ) -> tuple[dict, ReindexTargetSnapshot]:
        """Run the synchronous Qdrant selector read outside the event loop."""
        with self._memory_lock(memory_id):
            snapshot = self._reindex_target_snapshot(memory_id)
            try:
                resolved = self._resolve_collection(
                    memory_id,
                    target_snapshot=snapshot,
                )
            except EmbeddingCollectionReindexRequired as error:
                return (
                    {"state": error.state, "reason": error.reason},
                    snapshot,
                )
            except EmbeddingCollectionUnavailable as error:
                return (
                    {"state": error.state, "reason": error.reason},
                    snapshot,
                )
            if resolved is None:
                return ({"state": "missing"}, snapshot)
            return (
                {
                    "state": "ready",
                    "profile_fingerprint": resolved.identity.profile_fingerprint,
                    "points_count": resolved.points_count,
                },
                snapshot,
            )

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def create_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        embedding_result: EmbeddingResult,
    ) -> EmbeddingCollectionIdentity:
        """Create one fresh attributable shadow; never adopt a collision."""
        return await _run_blocking_maintenance_call(
            self._create_reindex_shadow,
            memory_id,
            operation_id,
            embedding_result=embedding_result,
        )

    def _create_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        embedding_result: EmbeddingResult,
    ) -> EmbeddingCollectionIdentity:
        """Run synchronous Qdrant shadow creation outside the event loop."""
        if type(embedding_result) is not EmbeddingResult:
            raise ValueError("embedding_result must be an EmbeddingResult")
        profile = self._embedding_profile()
        try:
            identity = build_embedding_collection_identity(
                memory_id,
                profile,
                embedding_result,
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)
        name = self._shadow_name(memory_id, operation_id)
        with self._memory_lock(memory_id):
            if self._collection_exists(name):
                raise EmbeddingCollectionReindexRequired("shadow_collision")
            try:
                self._require_completed_mutation(
                    self._client.create_collection(
                        collection_name=name,
                        vectors_config=qmodels.VectorParams(
                            size=identity.dimensions,
                            distance=qmodels.Distance.COSINE,
                        ),
                        metadata=identity.to_mapping(),
                    )
                )
                for field_name, field_schema in (
                    ("doc_id", qmodels.PayloadSchemaType.KEYWORD),
                    ("memory_id", qmodels.PayloadSchemaType.KEYWORD),
                    ("chunk_index", qmodels.PayloadSchemaType.INTEGER),
                ):
                    self._require_completed_mutation(
                        self._client.create_payload_index(
                            collection_name=name,
                            field_name=field_name,
                            field_schema=field_schema,
                            wait=True,
                        )
                    )
            except Exception:
                raise EmbeddingCollectionUnavailable(
                    "shadow_creation_failed"
                ) from None
            resolved = self._read_canonical(
                memory_id,
                name,
                result=embedding_result,
            )
            if resolved.identity != identity or resolved.points_count != 0:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            self._validate_reindex_payload_schema(name)
        return identity

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def store_reindex_chunks(
        self,
        memory_id: str,
        operation_id: str,
        doc_id: str,
        filename: str,
        chunks: List[Chunk],
        *,
        embedding_result: EmbeddingResult,
        identity: EmbeddingCollectionIdentity,
    ) -> int:
        """Append one verified provider batch directly to the fresh shadow."""
        return await _run_blocking_maintenance_call(
            self._store_reindex_chunks,
            memory_id,
            operation_id,
            doc_id,
            filename,
            chunks,
            embedding_result=embedding_result,
            identity=identity,
        )

    def _store_reindex_chunks(
        self,
        memory_id: str,
        operation_id: str,
        doc_id: str,
        filename: str,
        chunks: List[Chunk],
        *,
        embedding_result: EmbeddingResult,
        identity: EmbeddingCollectionIdentity,
    ) -> int:
        """Run one synchronous Qdrant upsert outside the event loop."""
        if (
            type(doc_id) is not str
            or not doc_id
            or type(filename) is not str
            or not filename
            or type(chunks) is not list
            or not chunks
            or type(embedding_result) is not EmbeddingResult
            or len(chunks) != len(embedding_result.vectors)
            or type(identity) is not EmbeddingCollectionIdentity
        ):
            raise ValueError("invalid reindex batch")
        try:
            validate_embedding_collection_identity(
                identity,
                memory_id=memory_id,
                profile=self._embedding_profile(),
                result=embedding_result,
            )
        except EmbeddingIdentityError as error:
            self._raise_identity_error(error)

        points = []
        for chunk, vector in zip(chunks, embedding_result.vectors):
            if (
                type(chunk) is not Chunk
                or type(chunk.index) is not int
                or chunk.index < 0
                or type(chunk.total_chunks) is not int
                or chunk.total_chunks < 1
            ):
                raise ValueError("invalid reindex chunk")
            points.append(
                qmodels.PointStruct(
                    id=str(uuid4()),
                    vector=list(vector),
                    payload={
                        "memory_id": memory_id,
                        "doc_id": doc_id,
                        "filename": filename,
                        "text": chunk.text,
                        "chunk_index": chunk.index,
                        "total_chunks": chunk.total_chunks,
                        "section_title": chunk.section_title,
                        "article_number": chunk.article_number,
                        "heading_hierarchy": chunk.heading_hierarchy,
                        "char_count": chunk.char_count,
                        "token_estimate": chunk.token_estimate,
                    },
                )
            )

        name = self._shadow_name(memory_id, operation_id)
        with self._memory_lock(memory_id):
            if not self._collection_exists(name):
                raise EmbeddingCollectionUnavailable("shadow_missing")
            resolved = self._read_canonical(
                memory_id,
                name,
                result=embedding_result,
            )
            if resolved.identity != identity:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            try:
                self._client.upsert(
                    collection_name=name,
                    points=points,
                    wait=True,
                )
            except Exception:
                raise EmbeddingCollectionUnavailable("shadow_write_failed") from None
        return len(points)

    def _validate_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        identity: EmbeddingCollectionIdentity,
        expected_chunks: Mapping[str, int],
    ) -> int:
        if (
            type(identity) is not EmbeddingCollectionIdentity
            or not isinstance(expected_chunks, Mapping)
            or not expected_chunks
        ):
            raise ValueError("invalid reindex validation contract")
        normalized_expected: dict[str, int] = {}
        for doc_id, count in expected_chunks.items():
            if (
                type(doc_id) is not str
                or not doc_id
                or type(count) is not int
                or count < 1
            ):
                raise ValueError("invalid expected reindex accounting")
            normalized_expected[doc_id] = count

        name = self._shadow_name(memory_id, operation_id)
        if not self._collection_exists(name):
            raise EmbeddingCollectionUnavailable("shadow_missing")
        resolved = self._read_canonical(memory_id, name, result=None)
        if resolved.identity != identity:
            raise EmbeddingCollectionReindexRequired("shadow_invalid")
        self._validate_reindex_payload_schema(name)

        indexes: dict[str, set[int]] = {
            doc_id: set() for doc_id in normalized_expected
        }
        expected_total = sum(normalized_expected.values())
        seen = 0
        offset = None
        offset_keys: set[tuple[str, str]] = set()
        page_count = 0
        while True:
            page_count += 1
            if page_count > expected_total + 1:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            try:
                points, next_offset = self._client.scroll(
                    collection_name=name,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
            except Exception:
                raise EmbeddingCollectionUnavailable(
                    "shadow_validation_failed"
                ) from None
            if type(points) is not list:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            if not points and next_offset is not None:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            for point in points:
                self._validate_point_id(point.id)
                vector = point.vector
                self._validate_vector(
                    list(vector) if type(vector) is list else vector,
                    identity.dimensions,
                )
                payload = self._validate_returned_payload(
                    point.payload,
                    memory_id,
                )
                doc_id = payload["doc_id"]
                chunk_index = payload.get("chunk_index")
                total_chunks = payload.get("total_chunks")
                if (
                    doc_id not in normalized_expected
                    or type(chunk_index) is not int
                    or chunk_index < 0
                    or type(total_chunks) is not int
                    or total_chunks != normalized_expected[doc_id]
                    or chunk_index >= total_chunks
                    or chunk_index in indexes[doc_id]
                ):
                    raise EmbeddingCollectionReindexRequired("shadow_invalid")
                indexes[doc_id].add(chunk_index)
                seen += 1
                if seen > expected_total:
                    raise EmbeddingCollectionReindexRequired("shadow_invalid")
            if next_offset is None:
                break
            offset_key = (type(next_offset).__name__, str(next_offset))
            if offset_key in offset_keys:
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
            offset_keys.add(offset_key)
            offset = next_offset

        if seen != resolved.points_count or seen != expected_total:
            raise EmbeddingCollectionReindexRequired("shadow_invalid")
        for doc_id, expected in normalized_expected.items():
            if indexes[doc_id] != set(range(expected)):
                raise EmbeddingCollectionReindexRequired("shadow_invalid")
        return seen

    @_guard_memory_operation
    async def validate_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        identity: EmbeddingCollectionIdentity,
        expected_chunks: Mapping[str, int],
    ) -> int:
        return await _run_blocking_maintenance_call(
            self._validate_reindex_shadow_locked,
            memory_id,
            operation_id,
            identity=identity,
            expected_chunks=expected_chunks,
        )

    def _validate_reindex_shadow_locked(
        self,
        memory_id: str,
        operation_id: str,
        *,
        identity: EmbeddingCollectionIdentity,
        expected_chunks: Mapping[str, int],
    ) -> int:
        """Run the synchronous exhaustive Qdrant scan outside the event loop."""
        with self._memory_lock(memory_id):
            return self._validate_reindex_shadow(
                memory_id,
                operation_id,
                identity=identity,
                expected_chunks=expected_chunks,
            )

    @_guard_namespace_mutation
    @_guard_memory_operation
    async def activate_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        identity: EmbeddingCollectionIdentity,
        expected_chunks: Mapping[str, int],
        expected_target: ReindexTargetSnapshot,
    ) -> int:
        """Validate, compare, switch once, then perform read-only verification."""
        return await _run_blocking_maintenance_call(
            self._activate_reindex_shadow,
            memory_id,
            operation_id,
            identity=identity,
            expected_chunks=expected_chunks,
            expected_target=expected_target,
        )

    def _activate_reindex_shadow(
        self,
        memory_id: str,
        operation_id: str,
        *,
        identity: EmbeddingCollectionIdentity,
        expected_chunks: Mapping[str, int],
        expected_target: ReindexTargetSnapshot,
    ) -> int:
        """Run the final synchronous Qdrant transaction outside the event loop."""
        if type(expected_target) is not ReindexTargetSnapshot:
            raise ValueError("invalid reindex target snapshot")
        name = self._shadow_name(memory_id, operation_id)
        alias_name = self._active_alias_name(memory_id)
        expected_total = sum(expected_chunks.values())

        with self._memory_lock(memory_id):
            self._validate_reindex_shadow(
                memory_id,
                operation_id,
                identity=identity,
                expected_chunks=expected_chunks,
            )
            if self._reindex_target_snapshot(memory_id) != expected_target:
                raise EmbeddingCollectionReindexRequired("active_target_changed")

            operations: list[object] = []
            if expected_target.active_alias_present:
                operations.append(
                    qmodels.DeleteAliasOperation(
                        delete_alias=qmodels.DeleteAlias(alias_name=alias_name)
                    )
                )
            operations.append(
                qmodels.CreateAliasOperation(
                    create_alias=qmodels.CreateAlias(
                        collection_name=name,
                        alias_name=alias_name,
                    )
                )
            )

            try:
                switched = self._client.update_collection_aliases(operations)
            except Exception:
                raise ReindexPostSwitchError("activation_unverified") from None
            if switched is not True:
                raise ReindexPostSwitchError("activation_unverified")

            # The alias batch above is the final mutation. Everything below is
            # deliberately read-only and must never attempt rollback or cleanup.
            try:
                if self._active_alias_target(memory_id) != name:
                    raise RuntimeError("alias target mismatch")
                resolved = self._read_canonical(memory_id, name, result=None)
                if (
                    resolved.identity != identity
                    or resolved.points_count != expected_total
                ):
                    raise RuntimeError("active target mismatch")
                self._validate_reindex_payload_schema(name)
            except Exception:
                raise ReindexPostSwitchError("post_switch_unverified") from None
        return expected_total

    async def test_connection(self) -> dict:
        try:
            collections = self._client.get_collections()
            return {
                "status": "ok",
                "collections": len(collections.collections),
                "message": "Qdrant OK",
            }
        except Exception:
            return {
                "status": "error",
                "message": "Qdrant unavailable",
            }

    @_guard_memory_operation
    async def get_collection_info(self, memory_id: str) -> dict:
        try:
            with self._memory_lock(memory_id):
                resolved = self._resolve_collection(memory_id)
        except EmbeddingCollectionReindexRequired as error:
            return {"state": error.state, "reason": error.reason}
        except EmbeddingCollectionUnavailable as error:
            return {"state": error.state, "reason": error.reason}
        if resolved is None:
            return {"state": "missing"}
        return {
            "state": "ready",
            "profile_fingerprint": resolved.identity.profile_fingerprint,
            "points_count": resolved.points_count,
        }


_vector_store: Optional[VectorStoreService] = None


def get_vector_store() -> VectorStoreService:
    """Return the process-wide Graph Memory vector store."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStoreService()
    return _vector_store
