# -*- coding: utf-8 -*-
"""
Tests unitaires et d'intégration pour l'ingestion asynchrone et le remplacement transactionnel (Issue #450).

Vérifie :
1. Enregistrement FastMCP des 4 outils :
   - long_ingest_async
   - long_ingest_status
   - long_ingest_list
   - long_ingest_cancel
2. Gouvernance Auth & Contrôle d'accès :
   - Rejet unauthenticated fail-closed (0 appel bridge).
   - Rejet unauthorized space fail-closed (0 appel bridge).
   - Exigence inconditionnelle de la permission 'manage' sur long_ingest_async.
   - Garde volatile : rejet par défaut de activeContext.md / progress.md.
   - Opt-in volatile : manage requis et émission de l'événement d'audit.
3. Validation des entrées et typage strict (R2-F3, R2-F4) :
   - Rejet strict des coercions ambiguës sur options.replace_existing ("false", "0", list, dict).
   - Validation stricte des bornes de pagination sur limit (1..100) et offset (>= 0).
4. Délégations LongEngine et GraphBridge :
   - Délégations fidèles de LongEngine vers GraphBridge.
   - Alignement exact avec les outils FastMCP Graph Memory :
     * memory_ingest_batch_async(memory_id, documents, replace_existing)
     * ingest_job_status(job_id, expected_memory_id)
     * ingest_job_list(memory_id, status, source_path, batch_id, limit, offset)
     * ingest_job_cancel(job_id, expected_memory_id)
   - Résolution unlinked space en fallback sur runtime embarqué pour la lecture/statut.
   - Protection anti-SSRF via _guard_url (rejet avant création de client).
5. Remplacement transactionnel (Safe Replace / Double-Buffering / Atomic Swap) dans IngestPipeline (R2-F1, R2-F2) :
   - Candidat créé sans source_path actif (source_path=None) pour respecter l'unicité (memory_id, source_path).
   - Commutation atomique promote_candidate_document exécutée après validation complète du candidat (S3, LLM, Neo4j, Qdrant).
   - Purge de l'ancienne version exécutée post-commutation et traçabilité des purge_errors / cleanup_pending.
   - Rollback isolé en cas d'erreur ou d'annulation à chaque étape (S3, LLM, graphe, Qdrant) préservant l'ancien document 100% intact.
6. Backend Graph Memory FastMCP Server & Memory Scoping Contract :
   - Cloisonnement strict par expected_memory_id.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from mcp.server.fastmcp import FastMCP
from live_mem.tools import register_all_tools
from live_mem.auth.context import current_token_info
from live_mem.core.engines import get_engine_registry
from live_mem.core.graph_bridge import GraphBridgeService, get_graph_bridge
from live_mem.core.models import GraphMemoryConfig
from live_mem.config import Settings


@pytest.fixture(autouse=True)
def mock_gm_env(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "mock-s3-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "mock-s3-secret")
    monkeypatch.setenv("NEO4J_PASSWORD", "mock-neo4j-pass")


@pytest.fixture
def mcp_server():
    mcp = FastMCP("test-async-ingest-mcp")
    register_all_tools(mcp)
    return mcp


@pytest.fixture
def auth_manage_token():
    token_dict = {
        "client_name": "test-admin",
        "permissions": ["read", "write", "manage"],
        "allowed_resources": ["test-space"],
        "token_hash": "sha256:dummy",
    }
    tok = current_token_info.set(token_dict)
    try:
        yield token_dict
    finally:
        current_token_info.reset(tok)


@pytest.fixture
def auth_write_token():
    token_dict = {
        "client_name": "test-writer",
        "permissions": ["read", "write"],
        "allowed_resources": ["test-space"],
        "token_hash": "sha256:dummy",
    }
    tok = current_token_info.set(token_dict)
    try:
        yield token_dict
    finally:
        current_token_info.reset(tok)


@pytest.fixture
def auth_read_token():
    token_dict = {
        "client_name": "test-reader",
        "permissions": ["read"],
        "allowed_resources": ["test-space"],
        "token_hash": "sha256:dummy",
    }
    tok = current_token_info.set(token_dict)
    try:
        yield token_dict
    finally:
        current_token_info.reset(tok)


# =============================================================================
# 1. Enregistrement FastMCP
# =============================================================================

@pytest.mark.asyncio
async def test_async_ingest_tools_registered(mcp_server):
    """Vérifie que les 4 outils long_ingest_* sont enregistrés sur FastMCP."""
    tool_names = set(mcp_server._tool_manager._tools.keys())
    assert "long_ingest_async" in tool_names
    assert "long_ingest_status" in tool_names
    assert "long_ingest_list" in tool_names
    assert "long_ingest_cancel" in tool_names


def test_async_ingest_mcp_description_requires_backend_document_fields(mcp_server):
    """The public MCP description must not promise a plain-text conversion."""
    description = mcp_server._tool_manager._tools["long_ingest_async"].description
    for required in ("filename", "source_path", "content_base64", "sha256"):
        assert required in description
    assert "content/content_base64" not in description
    assert "does not convert plain content" in description
    assert "does not compute a missing checksum" in description


# =============================================================================
# 2. Gouvernance Auth & Contrôle d'accès
# =============================================================================

@pytest.mark.parametrize("tool_name,args", [
    ("long_ingest_async", {"space_id": "test-space", "documents": [{"source_path": "doc.md", "content": "text"}]}),
    ("long_ingest_status", {"space_id": "test-space", "job_id": "job-123"}),
    ("long_ingest_list", {"space_id": "test-space"}),
    ("long_ingest_cancel", {"space_id": "test-space", "job_id": "job-123"}),
])
@pytest.mark.asyncio
async def test_async_ingest_unauthenticated_rejected(mcp_server, tool_name, args):
    """Vérifie le rejet immédiat d'un appel non authentifié sans aucun appel bridge."""
    tok = current_token_info.set(None)
    try:
        tool_fn = mcp_server._tool_manager._tools[tool_name].fn
        with patch.object(get_graph_bridge(), "ingest_async", new_callable=AsyncMock) as mock_async, \
             patch.object(get_graph_bridge(), "ingest_status", new_callable=AsyncMock) as mock_status, \
             patch.object(get_graph_bridge(), "ingest_list", new_callable=AsyncMock) as mock_list, \
             patch.object(get_graph_bridge(), "ingest_cancel", new_callable=AsyncMock) as mock_cancel:

            res = await tool_fn(**args)
            assert res["status"] == "error"
            assert "authentication required" in res["message"].lower()
            mock_async.assert_not_called()
            mock_status.assert_not_called()
            mock_list.assert_not_called()
            mock_cancel.assert_not_called()
    finally:
        current_token_info.reset(tok)


@pytest.mark.parametrize("tool_name,args", [
    ("long_ingest_async", {"space_id": "forbidden-space", "documents": [{"source_path": "doc.md", "content": "text"}]}),
    ("long_ingest_status", {"space_id": "forbidden-space", "job_id": "job-123"}),
    ("long_ingest_list", {"space_id": "forbidden-space"}),
    ("long_ingest_cancel", {"space_id": "forbidden-space", "job_id": "job-123"}),
])
@pytest.mark.asyncio
async def test_async_ingest_access_denied_matrix(mcp_server, auth_manage_token, tool_name, args):
    """Vérifie le rejet d'accès sur un espace non autorisé sans aucun appel bridge."""
    tool_fn = mcp_server._tool_manager._tools[tool_name].fn
    with patch.object(get_graph_bridge(), "ingest_async", new_callable=AsyncMock) as mock_async, \
         patch.object(get_graph_bridge(), "ingest_status", new_callable=AsyncMock) as mock_status, \
         patch.object(get_graph_bridge(), "ingest_list", new_callable=AsyncMock) as mock_list, \
         patch.object(get_graph_bridge(), "ingest_cancel", new_callable=AsyncMock) as mock_cancel:

        res = await tool_fn(**args)
        assert res["status"] == "error"
        assert "not authorized" in res["message"].lower() or "access denied" in res["message"].lower()
        mock_async.assert_not_called()
        mock_status.assert_not_called()
        mock_list.assert_not_called()
        mock_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_long_ingest_async_requires_manage_permission(mcp_server, auth_write_token):
    """Vérifie que long_ingest_async exige la permission manage (le token write seul est rejeté)."""
    tool_fn = mcp_server._tool_manager._tools["long_ingest_async"].fn
    with patch.object(get_graph_bridge(), "ingest_async", new_callable=AsyncMock) as mock_async:
        res = await tool_fn(space_id="test-space", documents=[{"source_path": "doc.md", "content": "text"}])
        assert res["status"] == "error"
        assert "manage" in res["message"].lower() and "permission" in res["message"].lower()
        mock_async.assert_not_called()


@pytest.mark.asyncio
async def test_long_ingest_async_volatile_guard(mcp_server, auth_manage_token):
    """Vérifie le rejet des fichiers volatils par défaut et l'audit lors de l'opt-in."""
    tool_fn = mcp_server._tool_manager._tools["long_ingest_async"].fn
    docs = [{"source_path": "docs/activeContext.md", "content": "context"}]

    # 1. Rejet par défaut
    res = await tool_fn(space_id="test-space", documents=docs, include_volatile=False)
    assert res["status"] == "error"
    assert "volatile files" in res["message"].lower()

    # 2. Succès avec include_volatile=True et manage permission
    with patch.object(get_graph_bridge(), "ingest_async", new_callable=AsyncMock) as mock_async, \
         patch("live_mem.tools.graph._emit_long_ingest_volatile_optin_audit") as mock_audit:
        mock_async.return_value = {"status": "ok", "batch_id": "b-1", "jobs": []}
        res_ok = await tool_fn(space_id="test-space", documents=docs, include_volatile=True)
        assert res_ok["status"] == "ok"
        mock_audit.assert_called_once_with("test-space", ["docs/activeContext.md"])
        mock_async.assert_called_once()


# =============================================================================
# 3. Validation des entrées et typage strict (R2-F3, R2-F4)
# =============================================================================

@pytest.mark.parametrize("invalid_opt", [
    {"replace_existing": "false"},
    {"replace_existing": "0"},
    {"replace_existing": [False]},
    {"replace_existing": {"value": False}},
    "not_a_dict",
])
@pytest.mark.asyncio
async def test_graph_bridge_rejects_malformed_replace_existing(invalid_opt):
    """Vérifie que les options malformées ou les coercions ambiguës sur replace_existing sont rejetées fail-closed."""
    bridge = GraphBridgeService()
    with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load, \
         patch.object(bridge, "_guard_url", return_value=None):
        mock_load.return_value = (GraphMemoryConfig(url="http://127.0.0.1:8765/mcp", token="tok", memory_id="m1"), None)
        res = await bridge.ingest_async("test-space", documents=[{"filename": "a.md"}], options=invalid_opt)
        assert res["status"] == "error"
        assert "must be a boolean" in res["message"] or "must be a dict" in res["message"]


@pytest.mark.parametrize("limit,offset", [
    (0, 0),
    (-1, 0),
    (101, 0),
    (50, -1),
    ("50", 0),
    (50, "0"),
])
@pytest.mark.asyncio
async def test_long_ingest_list_rejects_out_of_bounds_pagination(mcp_server, auth_read_token, limit, offset):
    """Vérifie que long_ingest_list rejette fail-closed toute pagination hors bornes ou mal typée."""
    tool_fn = mcp_server._tool_manager._tools["long_ingest_list"].fn
    res = await tool_fn(space_id="test-space", limit=limit, offset=offset)
    assert res["status"] == "error"
    assert "limit must be an integer between 1 and 100" in res["message"] or "offset must be a non-negative integer" in res["message"]


# =============================================================================
# 4. Intégration LongEngine & GraphBridge
# =============================================================================

@pytest.mark.asyncio
async def test_long_engine_async_ingest_delegations():
    """Vérifie la délégation fidèle de LongEngine vers GraphBridge."""
    long_eng = get_engine_registry().long_engine()
    bridge = get_graph_bridge()

    with patch.object(bridge, "ingest_async", new_callable=AsyncMock) as mock_async, \
         patch.object(bridge, "ingest_status", new_callable=AsyncMock) as mock_status, \
         patch.object(bridge, "ingest_list", new_callable=AsyncMock) as mock_list, \
         patch.object(bridge, "ingest_cancel", new_callable=AsyncMock) as mock_cancel:

        mock_async.return_value = {"status": "ok", "batch_id": "b-1"}
        mock_status.return_value = {"status": "ok", "job_id": "j-1"}
        mock_list.return_value = {"status": "ok", "jobs": []}
        mock_cancel.return_value = {"status": "ok", "cancelled": True}

        # 1. async
        r1 = await long_eng.ingest_async("test-space", documents=[{"source_path": "a.md"}], options={"replace_existing": True})
        assert r1["status"] == "ok"
        mock_async.assert_called_once_with(space_id="test-space", documents=[{"source_path": "a.md"}], options={"replace_existing": True})

        # 2. status
        r2 = await long_eng.ingest_status("test-space", "j-1")
        assert r2["status"] == "ok"
        mock_status.assert_called_once_with(space_id="test-space", job_id="j-1")

        # 3. list
        r3 = await long_eng.ingest_list("test-space", limit=10, offset=5)
        assert r3["status"] == "ok"
        mock_list.assert_called_once_with(space_id="test-space", batch_id=None, status=None, limit=10, offset=5)

        # 4. cancel
        r4 = await long_eng.ingest_cancel("test-space", "j-1")
        assert r4["status"] == "ok"
        mock_cancel.assert_called_once_with(space_id="test-space", job_id="j-1")


@pytest.mark.asyncio
async def test_graph_bridge_calls_real_gm_tool_names():
    """Vérifie que GraphBridge appelle les vrais noms d'outils Graph Memory avec leurs arguments exacts."""
    mock_settings = Settings(long_embedded_enabled=False)
    gm_config = GraphMemoryConfig(url="http://127.0.0.1:8765/mcp", token="secret", memory_id="mem-123")

    with patch("live_mem.core.graph_bridge.get_settings", return_value=mock_settings), \
         patch("live_mem.core.graph_bridge.GraphMemoryClient") as mock_client_cls:

        mock_instance = MagicMock()
        mock_instance.call_tool = AsyncMock(return_value={"status": "ok"})
        mock_client_cls.return_value = mock_instance

        bridge = GraphBridgeService(client_factory=mock_client_cls)
        with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load, \
             patch.object(bridge, "_guard_url", return_value=None):

            mock_load.return_value = (gm_config, None)

            # 1. memory_ingest_batch_async
            await bridge.ingest_async("test-space", documents=[{"filename": "a.md"}], options={"replace_existing": True})
            mock_instance.call_tool.assert_called_with(
                "memory_ingest_batch_async",
                {"memory_id": "mem-123", "documents": [{"filename": "a.md"}], "replace_existing": True},
            )

            # 2. ingest_job_status
            await bridge.ingest_status("test-space", "job-999")
            mock_instance.call_tool.assert_called_with(
                "ingest_job_status",
                {"job_id": "job-999", "expected_memory_id": "mem-123"},
            )

            # 3. ingest_job_list
            await bridge.ingest_list("test-space", limit=20, offset=10)
            mock_instance.call_tool.assert_called_with(
                "ingest_job_list",
                {"memory_id": "mem-123", "limit": 20, "offset": 10},
            )

            # 4. ingest_job_cancel
            await bridge.ingest_cancel("test-space", "job-999")
            mock_instance.call_tool.assert_called_with(
                "ingest_job_cancel",
                {"job_id": "job-999", "expected_memory_id": "mem-123"},
            )

            # 5. memory_ingest_batch_async with ontology option
            await bridge.ingest_async("test-space", documents=[{"filename": "a.md"}], options={"ontology": "cloud"})
            mock_instance.call_tool.assert_called_with(
                "memory_ingest_batch_async",
                {"memory_id": "mem-123", "documents": [{"filename": "a.md"}], "replace_existing": False, "ontology": "cloud"},
            )


@pytest.mark.asyncio
async def test_graph_bridge_async_ingest_ssrf_protection():
    """Vérifie que _guard_url bloque les URL interdites sur les outils d'ingestion asynchrone."""
    mock_settings = Settings(long_embedded_enabled=False)
    blocked_config = GraphMemoryConfig(url="http://169.254.169.254/mcp", token="secret")

    with patch("live_mem.core.graph_bridge.get_settings", return_value=mock_settings), \
         patch("live_mem.core.graph_bridge.GraphMemoryClient") as mock_client_cls:
        bridge = GraphBridgeService(client_factory=mock_client_cls)
        with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = (blocked_config, None)
            res = await bridge.ingest_async("test-space", documents=[{"filename": "a.md"}])
            assert res["status"] == "error"
            assert "not allowed" in res["message"].lower() or "blocked" in res["message"].lower()
            mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_graph_bridge_ingest_status_unlinked_fallback():
    """Vérifie que ingest_status utilise le runtime embarqué sans muter S3 pour les espaces non liés."""
    mock_settings = Settings(
        long_embedded_url="http://127.0.0.1:8765/mcp",
        long_embedded_enabled=True,
    )
    mock_storage = MagicMock()
    mock_storage.get_json = AsyncMock(return_value={"space_id": "test-space"})

    with patch("live_mem.core.graph_bridge.get_settings", return_value=mock_settings), \
         patch("live_mem.core.graph_bridge.get_storage", return_value=mock_storage), \
         patch("live_mem.core.graph_bridge.resolve_embedded_token", return_value="token-123") as mock_tok, \
         patch("live_mem.core.graph_bridge.GraphMemoryClient") as mock_client_cls:

        mock_instance = MagicMock()
        mock_instance.call_tool = AsyncMock(return_value={"status": "ok", "job_id": "j-1", "job_status": "queued"})
        mock_client_cls.return_value = mock_instance

        bridge = GraphBridgeService(client_factory=mock_client_cls)
        with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load, \
             patch.object(bridge, "_guard_url", return_value=None):

            mock_load.return_value = (None, {"status": "error", "message": "Space 'test-space' is not connected to Graph Memory"})
            res = await bridge.ingest_status("test-space", "j-1")
            assert res["status"] == "ok"
            mock_instance.call_tool.assert_called_once_with(
                "ingest_job_status",
                {"job_id": "j-1", "expected_memory_id": "test-space"},
            )
            mock_tok.assert_called_once_with(mock_settings, generate=False)


# =============================================================================
# 5. Backend Graph Memory FastMCP Server & Memory Scoping Contract
# =============================================================================

@pytest.mark.asyncio
async def test_gm_server_ingest_job_status_and_cancel_memory_scoping():
    """Vérifie que ingest_job_status et ingest_job_cancel rejettent les accès cross-memory via expected_memory_id."""
    from mcp_memory.server import ingest_job_status, ingest_job_cancel

    mock_queue = MagicMock()
    mock_queue.get_job = AsyncMock(return_value={"status": "ok", "job_id": "j-1", "memory_id": "space-A"})
    mock_queue.cancel = AsyncMock(return_value={"status": "ok", "job_id": "j-1"})

    with patch("mcp_memory.core.ingest_queue.get_ingest_queue", return_value=mock_queue), \
         patch("mcp_memory.server.check_memory_access", return_value=None), \
         patch("mcp_memory.server.check_write_permission", return_value=None):

        # 1. Succès quand expected_memory_id correspond
        res_ok = await ingest_job_status(job_id="j-1", expected_memory_id="space-A")
        assert res_ok["status"] == "ok"

        # 2. Rejet quand expected_memory_id ne correspond pas
        res_err = await ingest_job_status(job_id="j-1", expected_memory_id="space-B")
        assert res_err["status"] == "error"
        assert "does not belong to memory" in res_err["message"]

        # 3. Cancel rejeté quand expected_memory_id ne correspond pas
        cancel_err = await ingest_job_cancel(job_id="j-1", expected_memory_id="space-B")
        assert cancel_err["status"] == "error"
        assert "does not belong to memory" in cancel_err["message"]
        mock_queue.cancel.assert_not_called()


# =============================================================================
# 6. Remplacement transactionnel (Safe Replace / Candidate Staging / Atomic Promotion)
# =============================================================================

@pytest.mark.asyncio
async def test_run_ingest_pipeline_safe_replace_candidate_staging_and_atomic_promotion():
    """Vérifie que le candidat est créé sans source_path actif (staging), promu atomiquement, puis l'ancien doc purgé."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractedEntity, ExtractedRelation, ExtractionResult

    execution_order = []

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)

    async def mock_add_doc(*args, **kwargs):
        execution_order.append(("add_document", kwargs.get("doc_id"), kwargs.get("source_path")))
        return True

    async def mock_promote(*args, **kwargs):
        execution_order.append(("promote_candidate_document", kwargs.get("old_doc_id"), kwargs.get("new_doc_id")))
        return True

    mock_graph.add_document = AsyncMock(side_effect=mock_add_doc)
    mock_graph.promote_candidate_document = AsyncMock(side_effect=mock_promote)
    mock_graph.add_entities_and_relations = AsyncMock(
        return_value={"entities_created": 1, "entities_merged": 0, "relations_created": 1, "relations_merged": 0}
    )
    mock_graph.update_document_ingestion = AsyncMock()

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/new-doc.md", "size_bytes": 100})

    mock_extractor = MagicMock()
    mock_extraction = ExtractionResult(
        entities=[ExtractedEntity(name="E1", type="Concept", description="D1")],
        relations=[ExtractedRelation(from_entity="E1", to_entity="E1", type="REL", description="R1")],
        summary="Sum",
        key_topics=["T1"],
    )
    mock_extractor.extract_with_ontology_chunked = AsyncMock(return_value=mock_extraction)

    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])

    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    async def mock_delete_everywhere(memory_id, doc_id, **kwargs):
        execution_order.append(("delete_document_everywhere", doc_id))
        return {"neo4j_deleted": True, "qdrant_chunks_deleted": 1, "s3_deleted": True, "errors": []}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", side_effect=mock_delete_everywhere):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"new content",
            filename="doc.md",
            doc_hash="sha-new",
            source_path="docs/doc.md",
            replace_doc_id="old-doc-456",
        )

        assert res["status"] == "ok"
        assert res["document_id"] is not None
        assert res["purge_status"] == "purged"

        # 1. Le candidat DOIT être créé avec source_path=None (staging, pas de conflit d'unicité)
        add_entry = next(e for e in execution_order if e[0] == "add_document")
        assert add_entry[2] is None, f"Candidate doc must be created with source_path=None (got {add_entry[2]})"

        # 2. La promotion atomique DOIT précéder la purge de l'ancien document
        promote_entry = ("promote_candidate_document", "old-doc-456", res["document_id"])
        purge_entry = ("delete_document_everywhere", "old-doc-456")
        assert promote_entry in execution_order
        assert purge_entry in execution_order

        prom_idx = execution_order.index(promote_entry)
        purge_idx = execution_order.index(purge_entry)
        assert prom_idx < purge_idx, f"Promotion ({prom_idx}) must precede post-activation purge ({purge_idx})"


@pytest.mark.asyncio
async def test_run_ingest_pipeline_safe_replace_purge_warning_is_observable():
    """Vérifie que les erreurs de purge post-commutation sont capturées et renvoyées (cleanup_pending)."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractedEntity, ExtractedRelation, ExtractionResult

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.promote_candidate_document = AsyncMock(return_value=True)
    mock_graph.add_entities_and_relations = AsyncMock(return_value={})

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/new.md", "size_bytes": 50})

    mock_extractor = MagicMock()
    mock_extractor.extract_with_ontology_chunked = AsyncMock(
        return_value=ExtractionResult(entities=[], relations=[], summary="S", key_topics=[])
    )

    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])

    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    async def mock_delete_with_errors(memory_id, doc_id, **kwargs):
        return {"neo4j_deleted": True, "qdrant_chunks_deleted": 0, "s3_deleted": False, "errors": ["S3 DeleteObject timeout"]}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", side_effect=mock_delete_with_errors):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"content",
            filename="doc.md",
            doc_hash="sha",
            replace_doc_id="old-doc-456",
        )

        assert res["status"] == "ok"
        assert res["purge_status"] == "cleanup_pending"
        assert "S3 DeleteObject timeout" in res["purge_errors"]


@pytest.mark.parametrize("fault_stage", ["llm", "embedding", "graph_write", "vector_store"])
@pytest.mark.asyncio
async def test_run_ingest_pipeline_safe_replace_fault_injections_preserve_old_document(fault_stage):
    """Vérifie qu'un échec à n'importe quel stade (LLM, embedding, graph, vector) nettoie le candidat sans toucher à l'ancien doc."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractedEntity, ExtractedRelation, ExtractionResult

    deleted_docs = []

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.promote_candidate_document = AsyncMock()

    if fault_stage == "graph_write":
        mock_graph.add_entities_and_relations = AsyncMock(side_effect=RuntimeError("Neo4j Lock Timeout"))
    else:
        mock_graph.add_entities_and_relations = AsyncMock(return_value={})

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/cand.md", "size_bytes": 100})
    mock_storage.delete_document = AsyncMock(return_value=True)

    mock_extractor = MagicMock()
    if fault_stage == "llm":
        mock_extractor.extract_with_ontology_chunked = AsyncMock(side_effect=RuntimeError("LLM 500 error"))
    else:
        mock_extractor.extract_with_ontology_chunked = AsyncMock(
            return_value=ExtractionResult(
                entities=[ExtractedEntity(name="E1", type="Concept", description="D1")],
                relations=[],
                summary="S",
                key_topics=[],
            )
        )

    mock_chunker = MagicMock()
    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text"
    mock_chunker.chunk_document = MagicMock(return_value=[mock_chunk])

    mock_embedder = MagicMock()
    if fault_stage == "embedding":
        mock_embedder.embed_texts_result = AsyncMock(side_effect=RuntimeError("Embedding Rate Limit"))
    else:
        from hivemind_inference import EmbeddingResult
        mock_embedder.embed_texts_result = AsyncMock(
            return_value=EmbeddingResult(
                vectors=((0.1,)*1536,),
                configured_model="mock",
                model_evidence="configured_only",
                effective_dimensions=1536,
            )
        )

    mock_vector = MagicMock()
    if fault_stage == "vector_store":
        mock_vector.store_chunks = AsyncMock(side_effect=RuntimeError("Qdrant Connection Reset"))
    else:
        mock_vector.store_chunks = AsyncMock(return_value=1)

    async def mock_delete_everywhere(memory_id, doc_id, **kwargs):
        deleted_docs.append(doc_id)
        return {"neo4j_deleted": True, "qdrant_chunks_deleted": 0, "s3_deleted": True, "errors": []}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._embedder", return_value=mock_embedder), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", side_effect=mock_delete_everywhere):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"candidate bytes",
            filename="doc.md",
            doc_hash="sha-cand",
            source_path="docs/doc.md",
            replace_doc_id="old-doc-456",
        )

        assert res["status"] == "error"
        # L'ancien document ne doit JAMAIS avoir été supprimé ou promu
        assert "old-doc-456" not in deleted_docs
        mock_graph.promote_candidate_document.assert_not_called()


@pytest.mark.asyncio
async def test_run_ingest_pipeline_post_promotion_error_preserves_new_active_document():
    """Vérifie qu'une erreur post-promotion (ex: erreur de purge) ne détruit JAMAIS le nouveau document actif."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractionResult

    rolled_back_docs = []

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.promote_candidate_document = AsyncMock(return_value=True)
    mock_graph.add_entities_and_relations = AsyncMock(return_value={})

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/new.md", "size_bytes": 100})
    mock_storage.delete_document = AsyncMock()

    mock_extractor = MagicMock()
    mock_extractor.extract_with_ontology_chunked = AsyncMock(
        return_value=ExtractionResult(entities=[], relations=[], summary="S", key_topics=[])
    )

    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])

    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    async def mock_delete_throws(memory_id, doc_id, **kwargs):
        raise RuntimeError("Post-promotion S3 delete catastrophic failure")

    async def mock_rollback(memory_id, doc_id, *args, **kwargs):
        rolled_back_docs.append(doc_id)
        return {"neo4j_deleted": True}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", side_effect=mock_delete_throws), \
         patch("mcp_memory.core.ingest_pipeline._rollback", side_effect=mock_rollback):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"content",
            filename="doc.md",
            doc_hash="sha-new",
            source_path="docs/doc.md",
            replace_doc_id="old-doc-456",
        )

        assert res["status"] == "ok"
        assert res["purge_status"] == "cleanup_pending"
        # Le nouveau document actif ne doit JAMAIS être rollbacké
        assert res["document_id"] not in rolled_back_docs


@pytest.mark.asyncio
async def test_ingest_queue_propagates_cleanup_pending_to_job_status():
    """Vérifie que IngestJob et ingest_job_status propagent purge_status et purge_errors."""
    from mcp_memory.core.ingest_queue import IngestQueueService, IngestJob
    from mcp_memory.server import ingest_job_status

    q = IngestQueueService()
    job = IngestJob(
        job_id="job-cleanup-test",
        memory_id="mem-test",
        source_path="docs/doc.md",
        sha256="sha-123",
        filename="doc.md",
    )
    async with q._state_lock:
        q._jobs[job.job_id] = job
        q._apply_result_locked(
            job,
            {
                "status": "ok",
                "document_id": "doc-new-123",
                "purge_status": "cleanup_pending",
                "purge_errors": ["S3 DeleteObject connection reset"],
            },
        )

    with patch("mcp_memory.core.ingest_queue.get_ingest_queue", return_value=q), \
         patch("mcp_memory.server.check_memory_access", return_value=None):

        res = await ingest_job_status(job_id="job-cleanup-test", expected_memory_id="mem-test")
        assert res["status"] == "succeeded"
        assert res["purge_status"] == "cleanup_pending"
        assert res["purge_errors"] == ["S3 DeleteObject connection reset"]


@pytest.mark.asyncio
async def test_run_ingest_pipeline_ambiguous_promotion_reconciliation():
    """Vérifie qu'une exception lors de la promotion avec succès effectif dans Neo4j préserve le document actif."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractionResult

    rolled_back_docs = []

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.promote_candidate_document = AsyncMock(side_effect=RuntimeError("Neo4j TCP connection reset after commit"))
    mock_graph.add_entities_and_relations = AsyncMock(return_value={})

    # Simuler que la relecture après exception confirme que le candidat EST actif dans Neo4j
    async def mock_get_doc_by_source_path(memory_id, source_path):
        return {"id": "cand-doc-999", "ingestion_status": "succeeded"}

    mock_graph.get_document_by_source_path = AsyncMock(side_effect=mock_get_doc_by_source_path)

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/cand.md", "size_bytes": 100})
    mock_storage.delete_document = AsyncMock()

    mock_extractor = MagicMock()
    mock_extractor.extract_with_ontology_chunked = AsyncMock(
        return_value=ExtractionResult(entities=[], relations=[], summary="S", key_topics=[])
    )
    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])
    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    async def mock_rollback(memory_id, doc_id, *args, **kwargs):
        rolled_back_docs.append(doc_id)
        return {"neo4j_deleted": True}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", new_callable=AsyncMock), \
         patch("mcp_memory.core.ingest_pipeline._rollback", side_effect=mock_rollback), \
         patch("uuid.uuid4", return_value="cand-doc-999"):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"content",
            filename="doc.md",
            doc_hash="sha-cand",
            source_path="docs/doc.md",
            replace_doc_id="old-doc-456",
        )

        assert res["status"] == "ok"
        assert res["document_id"] == "cand-doc-999"
        assert res["purge_status"] == "cleanup_pending"
        assert "cand-doc-999" not in rolled_back_docs


@pytest.mark.asyncio
async def test_run_ingest_pipeline_cancellation_before_promote_cleans_candidate():
    """Vérifie qu'une annulation déclenchée à la frontière before_promote nettoie le candidat sans toucher à l'ancien doc."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractionResult

    rolled_back_docs = []
    deleted_docs = []

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.promote_candidate_document = AsyncMock()
    mock_graph.add_entities_and_relations = AsyncMock(return_value={})

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/cand.md", "size_bytes": 100})
    mock_storage.delete_document = AsyncMock()

    mock_extractor = MagicMock()
    mock_extractor.extract_with_ontology_chunked = AsyncMock(
        return_value=ExtractionResult(entities=[], relations=[], summary="S", key_topics=[])
    )
    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])
    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    # Simuler une annulation demandée
    cancel_count = 0
    def cancel_check():
        nonlocal cancel_count
        cancel_count += 1
        return cancel_count >= 3  # s'active aux frontières

    async def mock_rollback(memory_id, doc_id, *args, **kwargs):
        rolled_back_docs.append(doc_id)
        return {"neo4j_deleted": True}

    async def mock_delete_everywhere(memory_id, doc_id, *args, **kwargs):
        deleted_docs.append(doc_id)
        return {"neo4j_deleted": True}

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"), \
         patch("mcp_memory.core.ingest_pipeline.delete_document_everywhere", side_effect=mock_delete_everywhere), \
         patch("mcp_memory.core.ingest_pipeline._rollback", side_effect=mock_rollback):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"content",
            filename="doc.md",
            doc_hash="sha-cand",
            source_path="docs/doc.md",
            replace_doc_id="old-doc-456",
            cancel_check=cancel_check,
        )

        assert res["status"] == "cancelled"
        mock_graph.promote_candidate_document.assert_not_called()
        assert "old-doc-456" not in deleted_docs


@pytest.mark.asyncio
async def test_memory_query_filters_by_active_doc_ids():
    """Vérifie que memory_query filtre les recherches vectorielles pour exclure les candidats non promus."""
    from mcp_memory.server import memory_query
    from hivemind_inference import EmbeddingResult

    mock_graph = MagicMock()
    mock_graph.get_active_doc_ids = AsyncMock(return_value=["doc-active-1", "doc-active-2"])
    mock_graph.search_entities = AsyncMock(return_value=[])

    mock_embedder = MagicMock()
    mock_embedder.embed_query_result = AsyncMock(
        return_value=EmbeddingResult(
            vectors=((0.1,)*1536,),
            configured_model="mock",
            model_evidence="configured_only",
            effective_dimensions=1536,
        )
    )

    mock_vector = MagicMock()
    mock_vector.search = AsyncMock(return_value=[])

    with patch("mcp_memory.server.get_graph", return_value=mock_graph), \
         patch("mcp_memory.server.get_embedder", return_value=mock_embedder), \
         patch("mcp_memory.server.get_vector_store", return_value=mock_vector), \
         patch("mcp_memory.server.check_memory_access", return_value=None), \
         patch("mcp_memory.server.validate_memory_id", return_value=None):

        res = await memory_query(memory_id="test-mem", query="test query?")
        assert res["status"] == "ok"
        mock_graph.get_active_doc_ids.assert_called_once_with("test-mem")
        # Vérifier que search a bien reçu la liste des doc_ids actifs
        mock_vector.search.assert_called_once()
        call_kwargs = mock_vector.search.call_args[1]
        assert call_kwargs["doc_ids"] == ["doc-active-1", "doc-active-2"]


@pytest.mark.asyncio
async def test_run_ingest_pipeline_with_ontology_override():
    """Vérifie que run_ingest_pipeline utilise l'ontologie fournie en override lors de l'extraction."""
    from mcp_memory.core.ingest_pipeline import run_ingest_pipeline
    from mcp_memory.core.models import ExtractedEntity, ExtractionResult

    mock_memory = MagicMock()
    mock_memory.ontology = "general"

    mock_graph = MagicMock()
    mock_graph.get_memory = AsyncMock(return_value=mock_memory)
    mock_graph.add_document = AsyncMock()
    mock_graph.add_entities_and_relations = AsyncMock(return_value={})
    mock_graph.finalize_document = AsyncMock()
    mock_graph.update_document_ingestion = AsyncMock()

    mock_storage = MagicMock()
    mock_storage.upload_document = AsyncMock(return_value={"uri": "s3://bucket/doc.md", "size_bytes": 100})

    mock_extractor = MagicMock()
    mock_extractor.extract_with_ontology_chunked = AsyncMock(
        return_value=ExtractionResult(
            entities=[ExtractedEntity(name="App", type="Software", description="An app")],
            relations=[],
            summary="App doc",
            key_topics=["tech"],
        )
    )

    mock_chunker = MagicMock()
    mock_chunker.chunk_document = MagicMock(return_value=[])

    from hivemind_inference import EmbeddingResult
    mock_embedder = MagicMock()
    mock_embedder.embed_texts_result = AsyncMock(
        return_value=EmbeddingResult(
            vectors=((0.1,)*1536,),
            configured_model="mock",
            model_evidence="configured_only",
            effective_dimensions=1536,
        )
    )

    mock_vector = MagicMock()
    mock_vector.store_chunks = AsyncMock(return_value=0)

    with patch("mcp_memory.core.ingest_pipeline._graph", return_value=mock_graph), \
         patch("mcp_memory.core.ingest_pipeline._storage", return_value=mock_storage), \
         patch("mcp_memory.core.ingest_pipeline._extractor", return_value=mock_extractor), \
         patch("mcp_memory.core.ingest_pipeline._chunker", return_value=mock_chunker), \
         patch("mcp_memory.core.ingest_pipeline._embedder", return_value=mock_embedder), \
         patch("mcp_memory.core.ingest_pipeline._vector_store", return_value=mock_vector), \
         patch("mcp_memory.server._extract_text", return_value="some text"):

        res = await run_ingest_pipeline(
            memory_id="test-mem",
            content=b"content",
            filename="doc.md",
            doc_hash="sha",
            ontology="cloud",
        )

        assert res["status"] == "ok"
        mock_extractor.extract_with_ontology_chunked.assert_called_once()
        assert mock_extractor.extract_with_ontology_chunked.call_args[0][1] == "cloud"
