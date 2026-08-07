# -*- coding: utf-8 -*-
"""#277 integration locks for the real Graph Memory Qdrant consumer.

The tests use qdrant-client 1.18's local persistence engine rather than a
hand-written fake, so collection metadata, exact counts, filters, payloads and
restart round-trips exercise the same public client API as Qdrant 1.16.
"""

from __future__ import annotations

import os
import sys
import uuid
import warnings
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models as qmodels

from hivemind_inference import EmbeddingResult
from hivemind_inference.collection_identity import (
    build_embedding_collection_identity,
    canonical_qdrant_collection_name,
)
from mcp_memory.core.models import Chunk
from mcp_memory.core.vector_store import (
    LEGACY_PREFIX_DIAGNOSTIC,
    EmbeddingCollectionReindexRequired,
    EmbeddingCollectionUnavailable,
    VectorStoreService,
    _ResolvedCollection,
    _reset_legacy_prefix_diagnostic_for_tests,
)
from tests.fakes.inference_fakes import make_embedding_profile


def _result(*vectors, resolved_model="provider-model") -> EmbeddingResult:
    return EmbeddingResult(
        vectors=vectors or ((1.0, 0.0, 0.0),),
        configured_model="test-embedding-model",
        resolved_model=resolved_model,
        model_evidence="provider_reported",
        effective_dimensions=3,
    )


def _chunk(text="alpha") -> Chunk:
    return Chunk(
        text=text,
        index=0,
        total_chunks=1,
        char_count=len(text),
        token_estimate=1,
    )


class _CountingClient:
    def __init__(self, client):
        self._client = client
        self.scroll_calls = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    def scroll(self, *args, **kwargs):
        self.scroll_calls.append(kwargs)
        return self._client.scroll(*args, **kwargs)


class _ConcurrentCreateClient(_CountingClient):
    def __init__(self, client, name: str, identity: dict, memory_id: str):
        super().__init__(client)
        self._name = name
        self._identity = identity
        self._memory_id = memory_id
        self._canonical_checks = 0

    def collection_exists(self, name):
        if name == self._name:
            self._canonical_checks += 1
            if self._canonical_checks == 2 and not self._client.collection_exists(name):
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=qmodels.VectorParams(
                        size=3, distance=qmodels.Distance.COSINE
                    ),
                    metadata=self._identity,
                )
                self._client.upsert(
                    collection_name=name,
                    points=[
                        qmodels.PointStruct(
                            id=1,
                            vector=[1.0, 0.0, 0.0],
                            payload={
                                "memory_id": [
                                    self._memory_id,
                                    "foreign-memory",
                                ],
                                "doc_id": "doc-race",
                            },
                        )
                    ],
                )
        return self._client.collection_exists(name)


def _seed_collection(client, profile, memory_id: str, count: int) -> str:
    identity = build_embedding_collection_identity(
        memory_id, profile, _result()
    )
    name = canonical_qdrant_collection_name(memory_id)
    client.create_collection(
        collection_name=name,
        vectors_config=qmodels.VectorParams(
            size=3, distance=qmodels.Distance.COSINE
        ),
        metadata=identity.to_mapping(),
    )
    if count:
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=index,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": memory_id,
                        "doc_id": f"doc-{index}",
                        "text": f"point-{index}",
                    },
                )
                for index in range(count)
            ],
        )
    return name


@pytest.fixture
def profile():
    return make_embedding_profile(expected_dimensions=3)


@pytest.fixture
def client(tmp_path):
    instance = QdrantClient(path=str(tmp_path / "qdrant"))
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def store(client, profile):
    return VectorStoreService(
        client=client,
        profile=profile,
        legacy_prefix="memory_",
    )


class TestCanonicalCollectionResolution:
    async def test_fresh_store_creates_exact_metadata_and_survives_restart(
        self, tmp_path, profile
    ):
        path = tmp_path / "restart"
        memory_id = "a-b"
        result = _result()

        first_client = QdrantClient(path=str(path))
        first = VectorStoreService(
            client=first_client, profile=profile, legacy_prefix="memory_"
        )
        assert await first.store_chunks(
            memory_id,
            "doc-1",
            "source.txt",
            [_chunk()],
            embedding_result=result,
        ) == 1
        name = canonical_qdrant_collection_name(memory_id)
        info = first_client.get_collection(name)
        assert info.config.metadata == build_embedding_collection_identity(
            memory_id, profile, result
        ).to_mapping()
        assert not first_client.collection_exists("memory_a_b")
        first_client.close()

        second_client = QdrantClient(path=str(path))
        try:
            second = VectorStoreService(
                client=second_client, profile=profile, legacy_prefix="memory_"
            )
            status = await second.get_collection_info(memory_id)
            assert status["state"] == "ready"
            assert status["points_count"] == 1
            assert "collection_name" not in status
        finally:
            second_client.close()

    async def test_empty_legacy_is_untouched_and_canonical_is_distinct(
        self, client, profile
    ):
        client.create_collection(
            collection_name="memory_a_b",
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        await service.store_chunks(
            "a-b",
            "doc-1",
            "source.txt",
            [_chunk()],
            embedding_result=_result(),
        )

        assert client.collection_exists("memory_a_b")
        assert client.count("memory_a_b", exact=True).count == 0
        assert client.collection_exists(canonical_qdrant_collection_name("a-b"))

    async def test_nonempty_legacy_blocks_without_canonical_mutation(
        self, client, profile
    ):
        client.create_collection(
            collection_name="memory_a_b",
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
        )
        client.upsert(
            collection_name="memory_a_b",
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={"memory_id": "a_b"},
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.store_chunks(
                "a-b",
                "doc-1",
                "source.txt",
                [_chunk()],
                embedding_result=_result(),
            )

        assert excinfo.value.reason == "legacy_nonempty"
        assert not client.collection_exists(
            canonical_qdrant_collection_name("a-b")
        )
        assert client.count("memory_a_b", exact=True).count == 1

    @pytest.mark.optional
    async def test_pinned_qdrant_server_contract_when_available(self, profile):
        url = os.environ.get("HIVEMIND_QDRANT_TEST_URL")
        if not url:
            pytest.skip("set HIVEMIND_QDRANT_TEST_URL for Qdrant 1.16 server proof")
        server_client = QdrantClient(url=url, timeout=10)
        memory_id = f"P13_{uuid.uuid4().hex}"
        name = canonical_qdrant_collection_name(memory_id)
        service = VectorStoreService(
            client=server_client,
            profile=profile,
            legacy_prefix="memory_",
        )
        try:
            assert await service.store_chunks(
                memory_id,
                "doc-1",
                "source.txt",
                [_chunk()],
                embedding_result=_result(),
            ) == 1
            info = server_client.get_collection(name)
            assert info.config.metadata["memory_namespace"] == memory_id
            assert set(info.payload_schema) >= {"memory_id", "doc_id"}
            found = await service.search(
                memory_id,
                embedding_result=_result(),
            )
            assert [item.chunk.text for item in found] == ["alpha"]
            assert await service.delete_document_chunks(memory_id, "doc-1") == 1
            assert await service.delete_collection(memory_id) is True
            assert not server_client.collection_exists(name)
        finally:
            if server_client.collection_exists(name):
                server_client.delete_collection(name)
            server_client.close()

    async def test_live_profile_failure_is_typed_unavailable_without_mutation(
        self, client, profile, monkeypatch
    ):
        memory_id = "memory-one"
        name = _seed_collection(client, profile, memory_id, 1)

        def fail_profile():
            raise RuntimeError("secret-live-profile")

        monkeypatch.setitem(
            sys.modules,
            "mcp_memory.core.inference_runtime",
            SimpleNamespace(resolved_embedding_profile=fail_profile),
        )
        service = VectorStoreService(
            client=client, profile=None, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionUnavailable) as search_error:
            await service.search(memory_id, embedding_result=_result())
        assert search_error.value.reason == "embedding_profile_unavailable"

        assert await service.get_collection_info(memory_id) == {
            "state": "unavailable",
            "reason": "embedding_profile_unavailable",
        }

        with pytest.raises(EmbeddingCollectionUnavailable) as store_error:
            await service.store_chunks(
                memory_id,
                "doc-new",
                "source.txt",
                [_chunk()],
                embedding_result=_result(),
            )
        assert store_error.value.reason == "embedding_profile_unavailable"
        assert client.count(name, exact=True).count == 1


class TestDriftAndOwnershipGuards:
    async def test_copied_fingerprint_does_not_validate_tampered_metadata(
        self, client, profile
    ):
        memory_id = "memory-one"
        result = _result()
        identity = build_embedding_collection_identity(
            memory_id, profile, result
        ).to_mapping()
        identity["configured_model"] = "foreign-model"
        name = canonical_qdrant_collection_name(memory_id)
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
            metadata=identity,
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        status = await service.get_collection_info(memory_id)

        assert status == {
            "state": "reindex_required",
            "reason": "fingerprint_mismatch",
        }

    async def test_multivalue_owner_returned_by_search_blocks_all_results(
        self, client, profile
    ):
        memory_id = "memory-one"
        result = _result()
        identity = build_embedding_collection_identity(
            memory_id, profile, result
        ).to_mapping()
        name = canonical_qdrant_collection_name(memory_id)
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
            metadata=identity,
        )
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": memory_id,
                        "doc_id": "doc-1",
                        "text": "owned",
                        "chunk_index": 0,
                        "total_chunks": 1,
                    },
                ),
                qmodels.PointStruct(
                    id=2,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": [memory_id, "foreign-memory"],
                        "doc_id": "doc-2",
                        "text": "foreign",
                        "chunk_index": 0,
                        "total_chunks": 1,
                    },
                ),
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.search(memory_id, embedding_result=result)

        assert excinfo.value.reason == "payload_ownership_mismatch"

    async def test_multivalue_owner_cannot_pass_before_collection_deletion(
        self, client, profile
    ):
        memory_id = "memory-one"
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        ).to_mapping()
        name = canonical_qdrant_collection_name(memory_id)
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
            metadata=identity,
        )
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": [memory_id, "foreign-memory"],
                        "doc_id": "doc-1",
                    },
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.delete_collection(memory_id)

        assert excinfo.value.reason == "payload_ownership_mismatch"
        assert client.collection_exists(name)
        assert client.count(name, exact=True).count == 1

    async def test_multivalue_owner_blocks_append_and_import_mutations(
        self, client, profile
    ):
        memory_id = "memory-one"
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        )
        name = canonical_qdrant_collection_name(memory_id)
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
            metadata=identity.to_mapping(),
        )
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": [memory_id, "foreign-memory"],
                        "doc_id": "doc-1",
                    },
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired):
            await service.store_chunks(
                memory_id,
                "doc-new",
                "source.txt",
                [_chunk()],
                embedding_result=_result(),
            )
        with pytest.raises(EmbeddingCollectionReindexRequired):
            await service.import_collection(
                memory_id,
                [
                    {
                        "id": 2,
                        "vector": [1.0, 0.0, 0.0],
                        "payload": {
                            "memory_id": memory_id,
                            "doc_id": "doc-new",
                        },
                    }
                ],
                identity=identity.to_mapping(),
            )

        assert client.count(name, exact=True).count == 1

    async def test_multivalue_doc_id_cannot_be_counted_or_deleted(
        self, client, profile
    ):
        memory_id = "memory-one"
        name = _seed_collection(client, profile, memory_id, 0)
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": memory_id,
                        "doc_id": ["doc-1", "foreign-doc"],
                    },
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as count_error:
            await service.count_document_chunks(memory_id, "doc-1")
        assert count_error.value.reason == "payload_schema_mismatch"

        with pytest.raises(EmbeddingCollectionReindexRequired) as delete_error:
            await service.delete_document_chunks(memory_id, "doc-1")
        assert delete_error.value.reason == "payload_schema_mismatch"
        assert client.count(name, exact=True).count == 1

    async def test_unrelated_ownership_contradiction_blocks_document_delete(
        self, client, profile
    ):
        memory_id = "memory-one"
        name = _seed_collection(client, profile, memory_id, 0)
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={"memory_id": memory_id, "doc_id": "target"},
                ),
                qmodels.PointStruct(
                    id=2,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": [memory_id, "foreign-memory"],
                        "doc_id": "unrelated",
                    },
                ),
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.delete_document_chunks(memory_id, "target")

        assert excinfo.value.reason == "payload_ownership_mismatch"
        assert client.count(name, exact=True).count == 2

    async def test_list_doc_ids_detects_foreign_scalar_in_canonical_collection(
        self, client, profile
    ):
        memory_id = "memory-one"
        name = _seed_collection(client, profile, memory_id, 1)
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1000,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "memory_id": "foreign-memory",
                        "doc_id": "foreign-doc",
                    },
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.list_doc_ids(memory_id)

        assert excinfo.value.reason == "payload_ownership_mismatch"

    async def test_dynamic_model_evidence_drift_blocks_query(self, store):
        await store.store_chunks(
            "memory-one",
            "doc-1",
            "source.txt",
            [_chunk()],
            embedding_result=_result(),
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await store.search(
                "memory-one",
                embedding_result=_result(resolved_model="changed-model"),
            )

        assert excinfo.value.reason == "dynamic_evidence_mismatch"


class TestBackupIdentityBoundary:
    async def test_export_import_round_trips_identity_and_points(
        self, tmp_path, profile
    ):
        source_client = QdrantClient(path=str(tmp_path / "source"))
        target_client = QdrantClient(path=str(tmp_path / "target"))
        try:
            source = VectorStoreService(
                client=source_client, profile=profile, legacy_prefix="memory_"
            )
            target = VectorStoreService(
                client=target_client, profile=profile, legacy_prefix="memory_"
            )
            result = _result()
            await source.store_chunks(
                "memory-one",
                "doc-1",
                "source.txt",
                [_chunk()],
                embedding_result=result,
            )

            bundle = await source.export_collection("memory-one")
            await target.preflight_import(
                "memory-one", bundle["identity"], bundle["points"]
            )
            assert await target.import_collection(
                "memory-one",
                bundle["points"],
                identity=bundle["identity"],
            ) == 1
            restored = await target.export_collection("memory-one")

            assert restored == bundle
        finally:
            source_client.close()
            target_client.close()

    async def test_incompatible_backup_is_refused_before_collection_creation(
        self, client, profile
    ):
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )
        identity = build_embedding_collection_identity(
            "memory-one", profile, _result()
        ).to_mapping()
        identity["dimensions"] = 4

        with pytest.raises(EmbeddingCollectionReindexRequired):
            await service.preflight_import("memory-one", identity, [])

        assert not client.collection_exists(
            canonical_qdrant_collection_name("memory-one")
        )

    @pytest.mark.parametrize(
        "point_id",
        ["1", "not-a-uuid", -1, 1 << 64, True],
    )
    async def test_preflight_rejects_point_ids_qdrant_cannot_accept(
        self, client, profile, point_id
    ):
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )
        identity = build_embedding_collection_identity(
            "memory-one", profile, _result()
        ).to_mapping()
        points = [
            {
                "id": point_id,
                "vector": [1.0, 0.0, 0.0],
                "payload": {"memory_id": "memory-one", "doc_id": "doc-1"},
            }
        ]

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.preflight_import("memory-one", identity, points)

        assert excinfo.value.reason == "backup_point_invalid"
        assert not client.collection_exists(
            canonical_qdrant_collection_name("memory-one")
        )

    async def test_integer_point_ids_remain_restorable_in_export(
        self, client, profile
    ):
        memory_id = "memory-one"
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        )
        name = canonical_qdrant_collection_name(memory_id)
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=3, distance=qmodels.Distance.COSINE
            ),
            metadata=identity.to_mapping(),
        )
        client.upsert(
            collection_name=name,
            points=[
                qmodels.PointStruct(
                    id=1,
                    vector=[1.0, 0.0, 0.0],
                    payload={"memory_id": memory_id, "doc_id": "doc-1"},
                )
            ],
        )
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )

        bundle = await service.export_collection(memory_id)
        await service.preflight_import(
            memory_id,
            bundle["identity"],
            bundle["points"],
        )

        assert bundle["points"][0]["id"] == 1
        assert type(bundle["points"][0]["id"]) is int

    async def test_preflight_rejects_multivalue_document_identity(
        self, client, profile
    ):
        memory_id = "memory-one"
        service = VectorStoreService(
            client=client, profile=profile, legacy_prefix="memory_"
        )
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        ).to_mapping()

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.preflight_import(
                memory_id,
                identity,
                [
                    {
                        "id": 1,
                        "vector": [1.0, 0.0, 0.0],
                        "payload": {
                            "memory_id": memory_id,
                            "doc_id": ["doc-1", "foreign-doc"],
                        },
                    }
                ],
            )

        assert excinfo.value.reason == "payload_schema_mismatch"

    async def test_import_scans_a_concurrently_created_exact_collection(
        self, client, profile
    ):
        memory_id = "memory-one"
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        )
        name = canonical_qdrant_collection_name(memory_id)
        racing_client = _ConcurrentCreateClient(
            client,
            name,
            identity.to_mapping(),
            memory_id,
        )
        service = VectorStoreService(
            client=racing_client,
            profile=profile,
            legacy_prefix="memory_",
        )

        with pytest.raises(EmbeddingCollectionReindexRequired) as excinfo:
            await service.import_collection(
                memory_id,
                [
                    {
                        "id": 2,
                        "vector": [1.0, 0.0, 0.0],
                        "payload": {
                            "memory_id": memory_id,
                            "doc_id": "doc-new",
                        },
                    }
                ],
                identity=identity.to_mapping(),
            )

        assert excinfo.value.reason == "payload_ownership_mismatch"
        assert client.count(name, exact=True).count == 1


class TestLegacyPrefixDiagnostic:
    def test_nondefault_prefix_warns_once_without_echoing_value(
        self, client, profile
    ):
        _reset_legacy_prefix_diagnostic_for_tests()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            VectorStoreService(
                client=client,
                profile=profile,
                legacy_prefix="secret-tenant-value",
            )
            VectorStoreService(
                client=client,
                profile=profile,
                legacy_prefix="another-value",
            )

        assert [str(item.message) for item in caught] == [
            LEGACY_PREFIX_DIAGNOSTIC
        ]
        assert "secret-tenant-value" not in str(caught[0].message)


class TestResolverCostBounds:
    async def test_status_and_search_do_not_scroll_the_collection(
        self, client, profile
    ):
        memory_id = "memory-one"
        _seed_collection(client, profile, memory_id, 700)
        counting_client = _CountingClient(client)
        service = VectorStoreService(
            client=counting_client,
            profile=profile,
            legacy_prefix="memory_",
        )

        status = await service.get_collection_info(memory_id)
        assert status["state"] == "ready"
        assert status["points_count"] == 700
        assert counting_client.scroll_calls == []

        results = await service.search(
            memory_id,
            embedding_result=_result(),
        )
        assert len(results) == 5
        assert counting_client.scroll_calls == []

    async def test_import_scans_existing_ownership_once_not_per_batch(
        self, client, profile
    ):
        memory_id = "memory-one"
        _seed_collection(client, profile, memory_id, 600)
        identity = build_embedding_collection_identity(
            memory_id, profile, _result()
        )
        points = [
            {
                "id": 1000 + index,
                "vector": [1.0, 0.0, 0.0],
                "payload": {
                    "memory_id": memory_id,
                    "doc_id": f"new-doc-{index}",
                },
            }
            for index in range(600)
        ]
        counting_client = _CountingClient(client)
        service = VectorStoreService(
            client=counting_client,
            profile=profile,
            legacy_prefix="memory_",
        )

        imported = await service.import_collection(
            memory_id,
            points,
            identity=identity.to_mapping(),
            batch_size=100,
        )

        assert imported == 600
        assert len(counting_client.scroll_calls) == 3
        assert client.count(
            canonical_qdrant_collection_name(memory_id),
            exact=True,
        ).count == 1200


class TestDeleteCollectionSelectorRevalidation:
    @pytest.mark.parametrize(
        ("second_name", "second_alias", "expected_reason"),
        [
            (
                "collection-one",
                "active-alias",
                "active_alias_delete_unsupported",
            ),
            ("collection-two", None, "collection_race"),
        ],
        ids=("alias-appears", "physical-target-changes"),
    )
    async def test_refuses_before_delete(
        self,
        profile,
        second_name,
        second_alias,
        expected_reason,
    ):
        identity = build_embedding_collection_identity(
            "memory-one",
            profile,
            _result(),
        )

        class Client:
            def __init__(self):
                self.delete_calls = []

            def delete_collection(self, *, collection_name):
                self.delete_calls.append(collection_name)

        client = Client()
        service = VectorStoreService(
            client=client,
            profile=profile,
            legacy_prefix="memory_",
        )
        resolutions = iter(
            [
                _ResolvedCollection(
                    name="collection-one",
                    identity=identity,
                    points_count=0,
                ),
                _ResolvedCollection(
                    name=second_name,
                    identity=identity,
                    points_count=0,
                    active_alias=second_alias,
                ),
            ]
        )
        service._resolve_collection = lambda *_args, **_kwargs: next(
            resolutions
        )
        service._validate_owner = lambda *_args, **_kwargs: None

        with pytest.raises(EmbeddingCollectionUnavailable) as refusal:
            await service.delete_collection("memory-one")

        assert refusal.value.reason == expected_reason
        assert client.delete_calls == []


class TestSingleResolverCoverage:
    @pytest.mark.parametrize(
        ("method_name", "args", "kwargs"),
        [
            ("ensure_collection", ("memory-one",), {}),
            ("delete_collection", ("memory-one",), {}),
            (
                "store_chunks",
                ("memory-one", "doc-1", "source.txt", [_chunk()]),
                {"embedding_result": _result()},
            ),
            (
                "search",
                ("memory-one",),
                {"embedding_result": _result()},
            ),
            (
                "delete_document_chunks",
                ("memory-one", "doc-1"),
                {},
            ),
            (
                "count_document_chunks",
                ("memory-one", "doc-1"),
                {},
            ),
            ("list_doc_ids", ("memory-one",), {}),
            ("export_collection", ("memory-one",), {}),
            (
                "preflight_import",
                ("memory-one",),
                {"identity": None, "points": []},
            ),
            (
                "import_collection",
                ("memory-one", []),
                {"identity": None},
            ),
            ("get_collection_info", ("memory-one",), {}),
        ],
    )
    async def test_every_semantic_method_reaches_the_one_resolver_before_io(
        self,
        method_name,
        args,
        kwargs,
        profile,
    ):
        class NoIoClient:
            def __getattr__(self, name):
                raise AssertionError("Qdrant I/O bypassed the resolver")

        class ResolverReached(Exception):
            pass

        service = VectorStoreService(
            client=NoIoClient(),
            profile=profile,
            legacy_prefix="memory_",
        )
        calls = []

        def stop_at_resolver(*resolver_args, **resolver_kwargs):
            calls.append((resolver_args, resolver_kwargs))
            raise ResolverReached

        service._resolve_collection = stop_at_resolver
        if method_name in {"preflight_import", "import_collection"}:
            kwargs["identity"] = build_embedding_collection_identity(
                "memory-one", profile, _result()
            ).to_mapping()

        with pytest.raises(ResolverReached):
            await getattr(service, method_name)(*args, **kwargs)

        assert len(calls) == 1
