# -*- coding: utf-8 -*-
"""
Tests unitaires et d'intégration pour le catalogue documentaire et l'index SHA-256 (Issue #464).

Vérifie :
1. Enregistrement FastMCP des 2 nouveaux outils :
   - long_document_list
   - long_document_get
2. Gouvernance Auth & Contrôle d'accès :
   - Rejet unauthenticated fail-closed.
   - Rejet unauthorized space fail-closed.
   - Autorisation avec permission 'read'.
3. Validation des entrées et typage strict :
   - Bornes de pagination sur limit (1..100) et offset (>= 0).
   - Exigence d'au moins un de 'document_id' ou 'source_path' sur long_document_get.
   - Rejet direct-dispatch des types invalides pour include_content (ex: "false", 123).
4. Délégations LongEngine et GraphBridge :
   - Délégations fidèles de LongEngine vers GraphBridge.
   - Alignement avec les outils FastMCP Graph Memory :
     * document_list(memory_id, limit, offset, status, query)
     * document_get(memory_id, document_id, source_path, include_content)
   - Résolution unlinked space en fallback sur runtime embarqué pour la lecture.
   - Protection anti-SSRF via _guard_url.
   - Projection propre cachant les secrets d'infrastructure (uri, memory_id interne).
   - Projection fidèle des champs de contenu (content, content_format, content_base64, content_note).
   - Résolution atomique unique isolée (_resolve_read_client_and_memory) sur toutes les opérations de lecture.
5. GraphService & Support d'inventaire complet :
   - get_documents_meta retourne bien le dictionnaire de métadonnées.
   - list_documents_catalog sans limit (limit=None) génère un Cypher sans SKIP/LIMIT (inventaire complet).
   - Validation mutationnelle stricte de la clause Cypher (ORDER BY d.ingested_at DESC, d.id ASC SKIP $offset LIMIT $limit) et du dictionnaire exhaustif de paramètres.
"""

import base64
from io import BytesIO
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock


import pytest

from mcp.server.fastmcp import FastMCP
from live_mem.tools import register_all_tools, call_tool_direct
from live_mem.auth.context import current_token_info
from live_mem.core.engines import get_engine_registry
from live_mem.core.graph_bridge import GraphBridgeService, get_graph_bridge
from live_mem.core.models import GraphMemoryConfig
from tests.fakes.neo4j_fakes import bind_fake_neo4j


@pytest.fixture(autouse=True)
def fake_neo4j(monkeypatch):
    # The neo4j driver is a service-runtime dependency absent from the root test
    # environment. Bind a per-test fake on the graph module instead of mutating
    # sys.modules at import time, which leaked into every later test module.
    return bind_fake_neo4j(
        monkeypatch,
        graph_database=MagicMock(),
        driver=MagicMock(),
        session=MagicMock(),
        query=MagicMock(),
    )


@pytest.fixture(autouse=True)
def mock_gm_env(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "mock-s3-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "mock-s3-secret")
    monkeypatch.setenv("NEO4J_PASSWORD", "mock-neo4j-pass")


@pytest.fixture
def mcp_server():
    mcp = FastMCP("test-doc-catalog-mcp")
    register_all_tools(mcp)
    return mcp


def _token(name="test", perms=None, spaces=None):
    return {
        "client_name": name,
        "token_hash": f"sha256:{name}",
        "permissions": perms if perms is not None else ["read"],
        "allowed_resources": spaces if spaces is not None else ["test-space"],
    }


# ─────────────────────────────────────────────────────────────
# 1. Enregistrement FastMCP
# ─────────────────────────────────────────────────────────────

def test_document_catalog_tools_registered(mcp_server):
    tools = mcp_server._tool_manager._tools
    assert "long_document_list" in tools
    assert "long_document_get" in tools


# ─────────────────────────────────────────────────────────────
# 2. Gouvernance Auth & Contrôle d'accès
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_document_list_unauthenticated_fails_closed():
    tok = current_token_info.set(None)
    try:
        res = await call_tool_direct("long_document_list", {"space_id": "test-space"})
        assert res["status"] == "error"
        assert "Authentication required" in res["message"] or "Access denied" in res["message"]
    finally:
        current_token_info.reset(tok)


@pytest.mark.asyncio
async def test_document_get_unauthenticated_fails_closed():
    tok = current_token_info.set(None)
    try:
        res = await call_tool_direct(
            "long_document_get",
            {"space_id": "test-space", "source_path": "/docs/test.md"},
        )
        assert res["status"] == "error"
        assert "Authentication required" in res["message"] or "Access denied" in res["message"]
    finally:
        current_token_info.reset(tok)


@pytest.mark.asyncio
async def test_document_list_unauthorized_space_fails_closed():
    tok = current_token_info.set(_token(spaces=["other-space"]))
    try:
        res = await call_tool_direct("long_document_list", {"space_id": "test-space"})
        assert res["status"] == "error"
        assert "Access denied" in res["message"]
    finally:
        current_token_info.reset(tok)


# ─────────────────────────────────────────────────────────────
# 3. Validation des entrées et typage strict
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_document_list_limit_and_offset_bounds():
    tok = current_token_info.set(_token(spaces=["test-space"]))
    try:
        # Limit out of bounds (< 1)
        res = await call_tool_direct("long_document_list", {"space_id": "test-space", "limit": 0})
        assert res["status"] == "error"
        assert "limit must be an integer between 1 and 100" in res["message"]

        # Limit out of bounds (> 100)
        res = await call_tool_direct("long_document_list", {"space_id": "test-space", "limit": 101})
        assert res["status"] == "error"
        assert "limit must be an integer between 1 and 100" in res["message"]

        # Offset negative (< 0)
        res = await call_tool_direct("long_document_list", {"space_id": "test-space", "offset": -1})
        assert res["status"] == "error"
        assert "offset must be a non-negative integer" in res["message"]
    finally:
        current_token_info.reset(tok)


@pytest.mark.asyncio
async def test_document_get_requires_identifier_or_path():
    tok = current_token_info.set(_token(spaces=["test-space"]))
    try:
        res = await call_tool_direct("long_document_get", {"space_id": "test-space"})
        assert res["status"] == "error"
        assert "At least one of 'document_id' or 'source_path' must be provided" in res["message"]
    finally:
        current_token_info.reset(tok)


@pytest.mark.asyncio
async def test_document_get_rejects_non_boolean_include_content():
    tok = current_token_info.set(_token(spaces=["test-space"]))
    try:
        res = await call_tool_direct(
            "long_document_get",
            {
                "space_id": "test-space",
                "source_path": "/docs/test.md",
                "include_content": "false",
            },
        )
        assert res["status"] == "error"
        assert "'include_content' must be a boolean" in res["message"]

        res2 = await call_tool_direct(
            "long_document_get",
            {
                "space_id": "test-space",
                "source_path": "/docs/test.md",
                "include_content": 123,
            },
        )
        assert res2["status"] == "error"
        assert "'include_content' must be a boolean" in res2["message"]
    finally:
        current_token_info.reset(tok)


# ─────────────────────────────────────────────────────────────
# 4. Délégations LongEngine et GraphBridge
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_long_engine_delegates_list_documents():
    mock_bridge = MagicMock()
    mock_bridge.list_documents = AsyncMock(return_value={"status": "ok", "documents": [], "total_count": 0})
    from live_mem.core.engines.long_engine import LongEngine

    engine = LongEngine(bridge=mock_bridge)
    res = await engine.list_documents(
        space_id="test-space",
        limit=20,
        offset=5,
        status="succeeded",
        query="report",
    )
    assert res["status"] == "ok"
    mock_bridge.list_documents.assert_awaited_once_with(
        space_id="test-space",
        limit=20,
        offset=5,
        status="succeeded",
        query="report",
    )


@pytest.mark.asyncio
async def test_long_engine_delegates_get_document():
    mock_bridge = MagicMock()
    mock_bridge.get_document = AsyncMock(return_value={"status": "ok", "document": {"document_id": "doc1"}})
    from live_mem.core.engines.long_engine import LongEngine

    engine = LongEngine(bridge=mock_bridge)
    res = await engine.get_document(
        space_id="test-space",
        document_id="doc1",
        source_path="/docs/file.pdf",
        include_content=True,
    )
    assert res["status"] == "ok"
    mock_bridge.get_document.assert_awaited_once_with(
        space_id="test-space",
        document_id="doc1",
        source_path="/docs/file.pdf",
        include_content=True,
        content_format="text",
    )


@pytest.mark.asyncio
async def test_graph_bridge_list_documents_calls_mcp_and_projects_clean_schema():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={
        "status": "ok",
        "memory_id": "secret-backend-id",
        "total_count": 1,
        "limit": 50,
        "offset": 0,
        "documents": [
            {
                "id": "doc1",
                "filename": "guide.md",
                "uri": "s3://secret-bucket/guide.md",
                "hash": "abc123sha",
                "source_path": "/docs/guide.md",
                "status": "succeeded",
            }
        ],
    })

    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "test-space", None))):
        res = await bridge.list_documents(
            space_id="test-space",
            limit=10,
            offset=0,
            status="succeeded",
            query="guide",
        )
        assert res["status"] == "ok"
        assert res["space_id"] == "test-space"
        assert "memory_id" not in res  # Verifying no backend memory_id leak
        assert res["total_count"] == 1
        assert len(res["documents"]) == 1
        doc = res["documents"][0]
        assert doc["document_id"] == "doc1"
        assert doc["sha256"] == "abc123sha"
        assert "uri" not in doc  # Verifying no s3 uri leak
        mock_client.call_tool.assert_awaited_once_with(
            "document_list",
            {
                "memory_id": "test-space",
                "limit": 10,
                "offset": 0,
                "status": "succeeded",
                "query": "guide",
            },
        )


@pytest.mark.asyncio
async def test_graph_bridge_get_document_calls_mcp_and_projects_clean_schema():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={
        "status": "ok",
        "memory_id": "secret-backend-id",
        "document": {
            "id": "doc1",
            "filename": "guide.md",
            "uri": "s3://secret-bucket/guide.md",
            "sha256": "abc123sha",
            "source_path": "/docs/guide.md",
            "status": "succeeded",
        },
    })

    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "test-space", None))):
        res = await bridge.get_document(
            space_id="test-space",
            source_path="/docs/guide.md",
            include_content=False,
        )
        assert res["status"] == "ok"
        assert res["space_id"] == "test-space"
        assert "memory_id" not in res
        assert res["document"]["sha256"] == "abc123sha"
        assert "uri" not in res["document"]
        mock_client.call_tool.assert_awaited_once_with(
            "document_get",
            {
                "memory_id": "test-space",
                "source_path": "/docs/guide.md",
                "include_content": False,
                "content_format": "text",
            },
        )


@pytest.mark.asyncio
async def test_graph_bridge_get_document_projects_extracted_content_fields():
    """Vérifie que GraphBridge projette fidèlement le contenu extrait de Graph Memory (R1-03)."""
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={
        "status": "ok",
        "memory_id": "secret-backend-id",
        "document": {
            "id": "doc1",
            "filename": "guide.docx",
            "uri": "s3://secret-bucket/guide.docx",
            "sha256": "abc123sha",
            "source_path": "/docs/guide.docx",
            "status": "succeeded",
        },
        "content": "# Extracted Title\n\nExtracted body content from docx.",
        "content_format": "text",
        "content_note": "Text extracted from binary document.",
    })

    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "test-space", None))):
        res = await bridge.get_document(
            space_id="test-space",
            source_path="/docs/guide.docx",
            include_content=True,
            content_format="text",
        )
        assert res["status"] == "ok"
        assert res["space_id"] == "test-space"
        assert "memory_id" not in res
        assert "uri" not in res
        assert "uri" not in res["document"]
        assert res["content"] == "# Extracted Title\n\nExtracted body content from docx."
        assert res["document"]["content"] == "# Extracted Title\n\nExtracted body content from docx."
        assert res["content_format"] == "text"
        assert res["content_note"] == "Text extracted from binary document."


@pytest.mark.asyncio
async def test_graph_bridge_get_document_projects_binary_raw_content():
    """Vérifie que GraphBridge projette les bytes raw en base64 pour les fichiers binaires."""
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={
        "status": "ok",
        "memory_id": "secret-backend-id",
        "document": {
            "id": "doc2",
            "filename": "archive.tar",
            "uri": "s3://secret-bucket/archive.tar",
            "sha256": "raw123sha",
            "source_path": "/docs/archive.tar",
            "status": "succeeded",
        },
        "content_base64": "H4sICDY521wCA...",
        "content_format": "raw",
        "content_note": "Original binary file encoded as base64",
    })

    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "test-space", None))):
        res = await bridge.get_document(
            space_id="test-space",
            source_path="/docs/archive.tar",
            include_content=True,
            content_format="raw",
        )
        assert res["status"] == "ok"
        assert res["space_id"] == "test-space"
        assert "memory_id" not in res
        assert "uri" not in res
        assert res["content_base64"] == "H4sICDY521wCA..."
        assert res["document"]["content_base64"] == "H4sICDY521wCA..."
        assert res["content_format"] == "raw"
        assert res["content_note"] == "Original binary file encoded as base64"


@pytest.mark.asyncio
async def test_graph_bridge_ingest_status_atomic_resolution():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={"status": "ok"})
    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "mem-A", None))) as mock_resolve:
        with patch.object(bridge, "_load_gm_config", AsyncMock(side_effect=AssertionError("Separate load!"))):
            await bridge.ingest_status("test-space", "j-1")
            mock_resolve.assert_awaited_once_with("test-space")
            mock_client.call_tool.assert_awaited_once_with("ingest_job_status", {"job_id": "j-1", "expected_memory_id": "mem-A"})


@pytest.mark.asyncio
async def test_graph_bridge_ingest_list_atomic_resolution():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={"status": "ok", "jobs": []})
    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "mem-A", None))) as mock_resolve:
        with patch.object(bridge, "_load_gm_config", AsyncMock(side_effect=AssertionError("Separate load!"))):
            await bridge.ingest_list("test-space")
            mock_resolve.assert_awaited_once_with("test-space")
            mock_client.call_tool.assert_awaited_once_with("ingest_job_list", {"memory_id": "mem-A", "limit": 50, "offset": 0})


@pytest.mark.asyncio
async def test_graph_bridge_list_documents_atomic_resolution():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={"status": "ok", "documents": []})
    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "mem-A", None))) as mock_resolve:
        with patch.object(bridge, "_load_gm_config", AsyncMock(side_effect=AssertionError("Separate load!"))):
            await bridge.list_documents("test-space")
            mock_resolve.assert_awaited_once_with("test-space")
            mock_client.call_tool.assert_awaited_once_with("document_list", {"memory_id": "mem-A"})


@pytest.mark.asyncio
async def test_graph_bridge_get_document_atomic_resolution():
    bridge = GraphBridgeService()
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={"status": "ok", "document": {}})
    with patch.object(bridge, "_resolve_read_client_and_memory", AsyncMock(return_value=(mock_client, "mem-A", None))) as mock_resolve:
        with patch.object(bridge, "_load_gm_config", AsyncMock(side_effect=AssertionError("Separate load!"))):
            await bridge.get_document("test-space", source_path="/test.md")
            mock_resolve.assert_awaited_once_with("test-space")
            mock_client.call_tool.assert_awaited_once_with("document_get", {"memory_id": "mem-A", "include_content": False, "content_format": "text", "source_path": "/test.md"})


# ─────────────────────────────────────────────────────────────
# 5. FastMCP Execution End-to-End avec Token Autorisé
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_tool_execution_authorized():
    tok = current_token_info.set(_token(spaces=["test-space"]))
    mock_long_engine = MagicMock()
    mock_long_engine.list_documents = AsyncMock(return_value={
        "status": "ok",
        "space_id": "test-space",
        "total_count": 2,
        "documents": [
            {"document_id": "doc1", "sha256": "hash1"},
            {"document_id": "doc2", "sha256": "hash2"},
        ],
    })
    mock_long_engine.get_document = AsyncMock(return_value={
        "status": "ok",
        "space_id": "test-space",
        "document": {"document_id": "doc1", "sha256": "hash1", "source_path": "/docs/doc1.md"},
    })

    try:
        with patch.object(get_engine_registry(), "long_engine", return_value=mock_long_engine):
            list_res = await call_tool_direct(
                "long_document_list",
                {"space_id": "test-space", "limit": 10, "status": "succeeded"},
            )
            assert list_res["status"] == "ok"
            assert list_res["total_count"] == 2
            mock_long_engine.list_documents.assert_awaited_once_with(
                space_id="test-space",
                limit=10,
                offset=0,
                status="succeeded",
                query=None,
            )

            get_res = await call_tool_direct(
                "long_document_get",
                {"space_id": "test-space", "source_path": "/docs/doc1.md"},
            )
            assert get_res["status"] == "ok"
            assert get_res["document"]["sha256"] == "hash1"
            mock_long_engine.get_document.assert_awaited_once_with(
                space_id="test-space",
                document_id=None,
                source_path="/docs/doc1.md",
                include_content=False,
            )
    finally:
        current_token_info.reset(tok)


# Actual embedded document reads through the public facade, with synthetic I/O.
@pytest.fixture
def document_read_runtime(monkeypatch, mcp_server):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    from mcp_memory import server
    from mcp_memory.core.storage import StorageService
    from live_mem.core.engines.long_engine import LongEngine

    document = {
        "document_id": "doc1",
        "filename": "guide.txt",
        "source_path": "docs/guide.txt",
        "uri": "s3://test-bucket/test-space/documents/guide.txt",
        "content_type": "txt",
    }
    graph = MagicMock()
    graph.get_document_details = AsyncMock(side_effect=lambda **kwargs: dict(document))
    # Bypass construction: use the real storage method but never create an S3
    # client, consult credentials or perform network I/O.
    storage = StorageService.__new__(StorageService)
    storage._bucket = "test-bucket"
    storage._client = MagicMock()
    storage._client.get_object.return_value = {"Body": BytesIO(b"Readable text")}
    monkeypatch.setattr(server, "check_memory_access", lambda memory_id: None)
    monkeypatch.setattr(server, "get_graph", lambda: graph)
    monkeypatch.setattr(server, "get_storage", lambda: storage)
    backend_results = []

    async def backend_tool(name, arguments):
        assert name == "document_get"
        result = await server.document_get(**arguments)
        backend_results.append(result)
        return result

    bridge = GraphBridgeService()
    client = SimpleNamespace(call_tool=backend_tool)
    monkeypatch.setattr(
        bridge, "_resolve_read_client_and_memory",
        AsyncMock(return_value=(client, "test-space", None)),
    )
    monkeypatch.setattr(get_engine_registry(), "long_engine", lambda: LongEngine(bridge=bridge))

    async def read(route="public", *, include_content=True, by_path=False, content_format="text"):
        selector = {"source_path": "docs/guide.txt"} if by_path else {"document_id": "doc1"}
        if route == "backend":
            return await server.document_get(
                memory_id="test-space", **selector,
                include_content=include_content, content_format=content_format,
            )
        if route == "bridge":
            return await bridge.get_document(
                space_id="test-space", **selector,
                include_content=include_content, content_format=content_format,
            )
        assert route == "public" and content_format == "text"
        tok = current_token_info.set(_token())
        try:
            return await call_tool_direct("long_document_get", {
                "space_id": "test-space", **selector, "include_content": include_content,
            })
        finally:
            current_token_info.reset(tok)

    yield SimpleNamespace(
        server=server, document=document, graph=graph, storage=storage,
        read=read, backend_results=backend_results,
    )
    # Read success and failure must never repair, delete or reingest source data.
    assert all(call[0] == "get_document_details" for call in graph.mock_calls)
    assert all(call[0] == "get_object" for call in storage._client.mock_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["backend", "public"])
@pytest.mark.parametrize("failure", [
    "missing-object", "download-timeout", "body-read", "ownership", "extractor",
])
async def test_document_read_failures_are_sanitized_errors(
    document_read_runtime, monkeypatch, route, failure,
):
    from botocore.exceptions import ClientError

    runtime = document_read_runtime
    # Deliberately include distinct internal values: none may enter the public
    # error, even after the storage layer's existing proxy redaction.
    detail = "synthetic-private-key https://user:password@example.test/object?signature=private"
    client = runtime.storage._client
    if failure == "missing-object":
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": detail}}, "GetObject",
        )
    elif failure == "download-timeout":
        client.get_object.side_effect = TimeoutError(detail)
    elif failure == "body-read":
        client.get_object.return_value = {"Body": SimpleNamespace(read=MagicMock(side_effect=OSError(detail)))}
    elif failure == "ownership":
        runtime.document["uri"] = "s3://test-bucket/another-space/documents/private-key"
    else:
        runtime.document.update(filename="guide.pdf", content_type="pdf")
        monkeypatch.setattr(runtime.server, "_extract_text", MagicMock(side_effect=RuntimeError(detail)))

    result = await runtime.read(route)
    assert result == {"status": "error", "message": "Document content could not be read."}
    if route == "public":
        assert runtime.backend_results == [result]
    if failure == "ownership":
        client.get_object.assert_not_called()
    else:
        client.get_object.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("by_path", [False, True], ids=["document-id", "source-path"])
async def test_document_read_metadata_only_never_downloads(document_read_runtime, by_path):
    runtime = document_read_runtime
    runtime.storage._client.get_object.side_effect = AssertionError("Metadata must not download content")
    result = await runtime.read(include_content=False, by_path=by_path)
    assert result["status"] == "ok"
    assert result["document"]["document_id"] == "doc1"
    assert "uri" not in result["document"]
    assert "content" not in result and "content_base64" not in result
    runtime.storage._client.get_object.assert_not_called()
    runtime.graph.get_document_details.assert_awaited_once_with(
        memory_id="test-space", doc_id=None if by_path else "doc1",
        source_path="docs/guide.txt" if by_path else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "Ordinary document text.",
    "[S3 read error: Document not found: test-space/documents/guide.txt]",
    "A troubleshooting example: [S3 read error: example] is literal document text.",
    "",
])
async def test_document_read_preserves_literal_text(document_read_runtime, content):
    runtime = document_read_runtime
    runtime.storage._client.get_object.return_value = {"Body": BytesIO(content.encode())}
    result = await runtime.read(by_path=True)
    assert result["status"] == "ok"
    assert result["content"] == result["document"]["content"] == content
    assert "content_base64" not in result


@pytest.mark.asyncio
async def test_document_read_extracts_real_pdf(document_read_runtime):
    from tests.test_document_dependency_runtime import _pdf

    runtime = document_read_runtime
    runtime.document.update(filename="guide.pdf", content_type="pdf")
    runtime.storage._client.get_object.return_value = {"Body": BytesIO(_pdf())}
    result = await runtime.read()
    assert result["status"] == "ok" and result["content_format"] == "text"
    assert result["content"] == result["document"]["content"]
    assert result["content"].strip() == "A"
    assert "content_base64" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"", b"not a PDF"])
async def test_document_read_malformed_pdf_preserves_raw_fallback(document_read_runtime, content):
    runtime = document_read_runtime
    runtime.document.update(filename="guide.pdf", content_type="pdf")
    runtime.storage._client.get_object.return_value = {"Body": BytesIO(content)}
    result = await runtime.read()
    assert result["status"] == "ok" and result["content_format"] == "raw"
    assert result["content_base64"] == result["document"]["content_base64"]
    assert base64.b64decode(result["content_base64"]) == content
    assert "content" not in result and "content" not in result["document"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["backend", "bridge"])
async def test_document_read_binary_raw_bypasses_extraction(document_read_runtime, monkeypatch, route):
    runtime = document_read_runtime
    content = b"\x00\xfforiginal binary bytes"
    runtime.document.update(filename="guide.pdf", content_type="pdf")
    runtime.storage._client.get_object.return_value = {"Body": BytesIO(content)}
    extractor = MagicMock(side_effect=AssertionError("Raw reads must not extract text"))
    monkeypatch.setattr(runtime.server, "_extract_text", extractor)
    result = await runtime.read(route, content_format="raw")
    assert result["status"] == "ok" and result["content_format"] == "raw"
    assert base64.b64decode(result["content_base64"]) == content
    assert "content" not in result
    extractor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["backend", "public"])
async def test_document_read_not_found_never_downloads(document_read_runtime, route):
    runtime = document_read_runtime
    runtime.graph.get_document_details.side_effect = None
    runtime.graph.get_document_details.return_value = None
    result = await runtime.read(route)
    assert result == {"status": "error", "message": "Document 'doc1' not found"}
    runtime.storage._client.get_object.assert_not_called()


# ─────────────────────────────────────────────────────────────
# 6. Graph Memory Core: Métadonnées, Pagination & Tie-Breaker
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_documents_meta_returns_populated_dict():
    """Vérifie la correction du fallthrough dans get_documents_meta (R1-02)."""
    from mcp_memory.core.graph import GraphService

    service = GraphService()
    mock_record = {
        "id": "doc-uuid-1",
        "filename": "test.txt",
        "uri": "s3://bucket/test.txt",
        "hash": "sha256-hash-val",
        "source_path": "/path/test.txt",
        "source_modified_at": "2026-09-02T10:00:00Z",
        "ingested_at": None,
        "ingestion_status": "succeeded",
        "last_ingest_job_id": "job-1",
        "chunk_count": 3,
        "size_bytes": 1024,
        "text_length": 500,
        "content_type": "text/plain",
    }

    mock_result = MagicMock()
    async def _async_records():
        yield mock_record
    mock_result.__aiter__.side_effect = _async_records

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)

    with patch.object(service, "session") as mock_session_ctx:
        mock_session_ctx.return_value.__aenter__.return_value = mock_session
        res = await service.get_documents_meta("mem-1", ["doc-uuid-1"])
        assert isinstance(res, dict)
        assert "doc-uuid-1" in res
        meta = res["doc-uuid-1"]
        assert meta["filename"] == "test.txt"
        assert meta["sha256"] == "sha256-hash-val"
        assert meta["source_path"] == "path/test.txt"


@pytest.mark.asyncio
async def test_list_documents_catalog_pagination_and_tiebreaker_cypher_inspection():
    """Vérifie la pagination sur grand volume (>50 docs), le tri déterministe et inspecte le Cypher généré (R1-01, R1-06)."""
    from mcp_memory.core.graph import GraphService

    service = GraphService()

    # Créer 75 faux documents
    all_docs = []
    for i in range(75):
        all_docs.append({
            "id": f"doc-{i:03d}",
            "filename": f"file_{i:03d}.md",
            "uri": f"s3://bucket/file_{i:03d}.md",
            "hash": f"sha256-{i:03d}",
            "source_path": f"/docs/file_{i:03d}.md",
            "source_modified_at": "2026-09-02T10:00:00Z",
            "ingested_at": None,
            "ingestion_status": "succeeded",
            "last_ingest_job_id": "job-0",
            "chunk_count": 1,
            "size_bytes": 100,
            "text_length": 50,
            "content_type": "text/markdown",
        })

    def make_mock_session(docs_slice, total):
        mock_count_res = MagicMock()
        mock_count_res.single = AsyncMock(return_value={"total_count": total})

        mock_docs_res = MagicMock()
        async def _async_docs():
            for d in docs_slice:
                yield d
        mock_docs_res.__aiter__.side_effect = _async_docs

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(side_effect=[mock_count_res, mock_docs_res])
        return mock_session

    # Test page 1 (limit 50, offset 0) -> Vérifier Cypher et paramètres exacts
    session_p1 = make_mock_session(all_docs[:50], 75)
    with patch.object(service, "session") as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = session_p1
        p1 = await service.list_documents_catalog("mem-1", limit=50, offset=0)
        assert p1["total_count"] == 75
        assert len(p1["documents"]) == 50
        assert p1["documents"][0]["document_id"] == "doc-000"
        assert p1["documents"][49]["document_id"] == "doc-049"

        call_count, call_docs = session_p1.run.call_args_list
        docs_query, docs_params = call_docs[0][0], call_docs[1]
        normalized_query = " ".join(docs_query.split())
        assert "ORDER BY d.ingested_at DESC, d.id ASC SKIP $offset LIMIT $limit" in normalized_query
        assert docs_params == {"memory_id": "mem-1", "limit": 50, "offset": 0}

    # Test page 2 (limit 50, offset 50 -> 25 restants)
    session_p2 = make_mock_session(all_docs[50:], 75)
    with patch.object(service, "session") as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = session_p2
        p2 = await service.list_documents_catalog("mem-1", limit=50, offset=50)
        assert p2["total_count"] == 75
        assert len(p2["documents"]) == 25
        assert p2["documents"][0]["document_id"] == "doc-050"
        assert p2["documents"][24]["document_id"] == "doc-074"

        call_count, call_docs = session_p2.run.call_args_list
        docs_query, docs_params = call_docs[0][0], call_docs[1]
        normalized_query = " ".join(docs_query.split())
        assert "ORDER BY d.ingested_at DESC, d.id ASC SKIP $offset LIMIT $limit" in normalized_query
        assert docs_params == {"memory_id": "mem-1", "limit": 50, "offset": 50}

    # Test sans limit (limit=None -> inventaire complet sans SKIP/LIMIT pour synchronisation interne)
    session_all = make_mock_session(all_docs, 75)
    with patch.object(service, "session") as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = session_all
        all_res = await service.list_documents_catalog("mem-1", limit=None)
        assert all_res["total_count"] == 75
        assert len(all_res["documents"]) == 75
        assert all_res["limit"] is None

        call_count, call_docs = session_all.run.call_args_list
        docs_query, docs_params = call_docs[0][0], call_docs[1]
        normalized_query = " ".join(docs_query.split())
        assert "ORDER BY d.ingested_at DESC, d.id ASC" in normalized_query
        assert "SKIP" not in docs_query
        assert "LIMIT" not in docs_query
        assert docs_params == {"memory_id": "mem-1"}
