# -*- coding: utf-8 -*-
"""
Tests for P4-4 — LongEngine typed methods through the REAL GraphBridgeService
seam + a deterministic FakeGraphTransport (no network / S3 / Neo4j / Qdrant /
LLM).

Unlike ``test_engine_long.py`` (which injects a BRIDGE-layer fake to prove the
engine forwards verbatim), this suite drives the REAL bridge:

    GraphBridgeService(client_factory=FakeGraphTransport.factory(...))
        wrapped by  LongEngine(bridge=...)
        over an in-memory FakeStorage seeded with a connected-space _meta.json.

So it exercises the actual client-construction seam, the SSRF guard, the GM
tool mapping, and the byte-for-byte preservation of connect/push/status/
disconnect.

Groups:
- A — typed methods (ingest/list_ontologies/query/search) + status batch.
- B — SSRF refused BEFORE any client is built (adapter path).
- C — byte-for-byte for the legacy methods (seam regression).
- D — import gate / no-commit-path (AST).
- E — SSRF function relocation regression.
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from live_mem.core.graph_bridge import GraphBridgeService, GraphMemoryClient
from live_mem.core.engines.long_engine import LongEngine
from tests.fakes import FakeGraphTransport, RecordedCall


# =============================================================================
# In-memory storage fake (idiom lifted from test_hivemind_state.py) — only the
# methods the bridge touches: get_json / put_json / list_and_get.
# =============================================================================


class FakeStorage:
    """Minimal in-memory StorageService stand-in. No S3, fully deterministic."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append(
                    {"Key": key, "Size": len(self.objects[key]), "LastModified": ""}
                )
        return out

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        results: list[dict] = []
        for obj in await self.list_objects(prefix):
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": "",
                    }
                )
        return results

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)


# =============================================================================
# Helpers
# =============================================================================

_CONNECTED_URL = "https://gm.example.com"


def _meta_connected(url: str = _CONNECTED_URL, memory_id: str = "mem-1") -> dict:
    return {
        "space_id": "space-a",
        "version": 1,
        "graph_memory": {
            "url": url,
            "token": "tok-secret",
            "memory_id": memory_id,
            "ontology": "general",
            "last_push": None,
            "push_count": 0,
            "files_pushed": 0,
        },
    }


def _build(storage: FakeStorage, **factory_kwargs):
    """Wire a real bridge (fake client factory) under a real engine.

    Returns ``(engine, bridge, factory)``. The storage is patched at the bridge
    module level (``graph_bridge.get_storage``), matching how the codebase's
    other suites redirect storage to an in-memory fake.
    """
    factory = FakeGraphTransport.factory(**factory_kwargs)
    bridge = GraphBridgeService(client_factory=factory)
    engine = LongEngine(bridge=bridge)
    return engine, bridge, factory


def _patch_storage(storage: FakeStorage):
    return patch("live_mem.core.graph_bridge.get_storage", return_value=storage)


# =============================================================================
# GROUP A — typed methods through real bridge + fake transport
# =============================================================================


async def test_ingest_calls_memory_ingest_with_base64_filename() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await engine.ingest(
            "space-a",
            filename="doc.md",
            content="hello world",
            source_path="repo/doc.md",
            force=True,
        )

    # Exactly one ingest call, args carry the config memory_id + base64 content.
    assert factory.instances[-1].tool_names() == ["memory_ingest"]
    (args,) = factory.instances[-1].args_for("memory_ingest")
    assert args["memory_id"] == "mem-1"
    assert args["content_base64"] == base64.b64encode(b"hello world").decode("ascii")
    assert args["filename"] == "doc.md"
    assert args["force"] is True
    # Optional arg present only because passed.
    assert args["source_path"] == "repo/doc.md"
    assert "source_modified_at" not in args
    assert "metadata" not in args
    # Canned dict returned unreshaped.
    assert result == {"status": "ok", "ingested": True}


async def test_ingest_content_base64_passthrough_no_double_encode() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    raw_b64 = base64.b64encode(b"already-encoded").decode("ascii")
    with _patch_storage(storage):
        await engine.ingest("space-a", filename="x.md", content_base64=raw_b64)

    (args,) = factory.instances[-1].args_for("memory_ingest")
    # Forwarded verbatim — NOT re-encoded.
    assert args["content_base64"] == raw_b64


async def test_ingest_rejects_both_or_neither_content() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        both = await engine.ingest(
            "space-a", filename="x.md", content="a", content_base64="b"
        )
        neither = await engine.ingest("space-a", filename="x.md")

    assert both["status"] == "error"
    assert "exactement un" in both["message"]
    assert neither["status"] == "error"
    # XOR guard fires before any client is built.
    assert factory.instances == []


async def test_list_ontologies_calls_ontology_list_no_memory_id() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await engine.list_ontologies("space-a")

    assert factory.instances[-1].tool_names() == ["ontology_list"]
    (args,) = factory.instances[-1].args_for("ontology_list")
    # NO memory_id — passing one would 400 in prod.
    assert args == {}
    assert result == {"status": "ok", "ontologies": []}


async def test_query_calls_memory_query() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.query("space-a", "q", limit=5)

    assert factory.instances[-1].tool_names() == ["memory_query"]
    (args,) = factory.instances[-1].args_for("memory_query")
    assert args == {"memory_id": "mem-1", "query": "q", "limit": 5}


async def test_search_calls_memory_search() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.search("space-a", "q", limit=7)

    inst = factory.instances[-1]
    assert inst.tool_names() == ["memory_search"]
    (args,) = inst.args_for("memory_search")
    assert args == {"memory_id": "mem-1", "query": "q", "limit": 7}
    # Distinct tool name from query.
    assert "memory_query" not in inst.tool_names()


async def test_status_unchanged_batch() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await engine.status("space-a")

    inst = factory.instances[-1]
    # Still a single batch of (memory_stats, document_list), both via batch.
    assert inst.tool_names() == ["memory_stats", "document_list"]
    assert all(c.via == "call_tools_batch" for c in inst.calls)
    # Reshaped status dict unchanged.
    assert result["status"] == "ok"
    assert result["connected"] is True
    assert result["reachable"] is True
    assert result["graph_stats"] == {
        "document_count": 0,
        "entity_count": 0,
        "relation_count": 0,
    }


async def test_determinism_same_inputs_same_calls() -> None:
    async def run() -> tuple[list[str], dict]:
        storage = FakeStorage()
        await storage.put_json("space-a/_meta.json", _meta_connected())
        engine, _bridge, factory = _build(storage)
        with _patch_storage(storage):
            r1 = await engine.query("space-a", "who", limit=2)
            r2 = await engine.list_ontologies("space-a")
        names = [n for inst in factory.instances for n in inst.tool_names()]
        return names, {"query": r1, "ontologies": r2}

    names_a, ret_a = await run()
    names_b, ret_b = await run()
    assert names_a == names_b
    assert ret_a == ret_b


async def test_call_recording_order_and_args() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.ingest("space-a", filename="d.md", content="c")
        await engine.list_ontologies("space-a")
        await engine.query("space-a", "q")

    # Each method builds its own fake client; flatten the recorded tool names.
    flat = [n for inst in factory.instances for n in inst.tool_names()]
    assert flat == ["memory_ingest", "ontology_list", "memory_query"]


async def test_canned_response_keyed_by_tool_configurable() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    custom = {"status": "ok", "results": [{"id": 1, "text": "answer"}]}
    engine, bridge, factory = _build(storage, responses={"memory_query": custom})

    with _patch_storage(storage):
        result = await engine.query("space-a", "q")

    assert result == custom


async def test_ingest_uses_180s_timeout() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.ingest("space-a", filename="d.md", content="c")

    # The ingest path mirrors push: timeout=180.0 reaches the built client.
    assert factory.instances[-1].timeout == 180.0


# =============================================================================
# GROUP B — SSRF before any connection (adapter path)
# =============================================================================


@pytest.mark.parametrize(
    "method, url, needle",
    [
        ("ingest", "http://10.0.0.1", "Private IP address"),
        ("query", "http://127.0.0.1", "loopback"),
        ("list_ontologies", "http://169.254.169.254", "link-local"),
        ("search", "file:///etc/passwd", "scheme"),
    ],
)
async def test_typed_methods_reject_unsafe_url_before_client_built(
    method: str, url: str, needle: str
) -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected(url=url))
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        if method == "ingest":
            result = await engine.ingest("space-a", filename="d.md", content="c")
        elif method == "query":
            result = await engine.query("space-a", "q")
        elif method == "search":
            result = await engine.search("space-a", "q")
        else:
            result = await engine.list_ontologies("space-a")

    assert result["status"] == "error"
    assert needle in result["message"]
    # Zero clients built — SSRF guard fired before _make_client.
    assert factory.instances == []


async def test_valid_https_url_reaches_transport() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected(url="https://gm.example.com"))
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.query("space-a", "q")

    # One instance built, call recorded.
    assert len(factory.instances) == 1
    assert factory.instances[0].tool_names() == ["memory_query"]


# =============================================================================
# GROUP C — byte-for-byte for legacy methods (seam regression)
# =============================================================================


async def test_connect_byte_for_byte() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", {"space_id": "space-a", "version": 1})
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await engine.connect(
            space_id="space-a",
            url="https://gm.example.com",
            token="tok",
            memory_id="mem-1",
            ontology="general",
        )

    inst = factory.instances[-1]
    # health → list → create (memory absent from empty memory_list).
    assert inst.tool_names() == ["system_health", "memory_list", "memory_create"]
    create_args = inst.args_for("memory_create")[0]
    assert create_args["memory_id"] == "mem-1"
    assert create_args["ontology"] == "general"
    assert result["status"] == "connected"
    assert result["graph_memory"]["memory_created"] is True
    # _meta.json now carries the graph_memory block.
    meta = await storage.get_json("space-a/_meta.json")
    assert meta["graph_memory"]["memory_id"] == "mem-1"
    assert meta["graph_memory"]["url"] == "https://gm.example.com"


async def test_push_byte_for_byte() -> None:
    storage = FakeStorage()
    # P4-8: activeContext.md/progress.md are now volatile (skipped by default).
    # Seed a DURABLE file so a real ingest happens, and seed the prior
    # bank_mirror ledger with a stale entry so the orphan-clean still counts.
    meta = _meta_connected()
    meta["graph_memory"]["bank_mirror"] = ["systemPatterns.md", "stale.md"]
    await storage.put_json("space-a/_meta.json", meta)
    await storage.put("space-a/bank/systemPatterns.md", "patterns")

    # document_list reports the durable doc (re-ingested) + one recorded-mirror
    # orphan to clean (stale.md, was mirrored before, now gone from the bank).
    # P7-8: each listed doc carries its GM ``id`` — deletes are keyed by
    # document_id resolved from this list, never by filename.
    responses = {
        "document_list": {
            "status": "ok",
            "documents": [
                {"id": "uuid-patterns", "filename": "systemPatterns.md"},
                {"id": "uuid-stale", "filename": "stale.md"},
            ],
        }
    }
    engine, bridge, factory = _build(storage, responses=responses)

    with _patch_storage(storage):
        result = await engine.push("space-a")

    inst = factory.instances[-1]
    names = inst.tool_names()
    # document_list issued first via call_tool, then the batch.
    assert names[0] == "document_list"
    assert inst.calls[0].via == "call_tool"
    # batch: delete(systemPatterns) + ingest(systemPatterns) + clean orphan
    # (stale.md). Order of bank files preserved.
    batch_names = names[1:]
    assert batch_names.count("memory_ingest") == 1  # only the durable file
    assert batch_names.count("document_delete") == 2  # 1 re-ingest delete + 1 orphan
    # P7-8: both deletes are keyed by the GM document_id resolved from
    # document_list — never by filename (the real GM tool rejects filenames).
    delete_args = inst.args_for("document_delete")
    assert sorted(a["document_id"] for a in delete_args) == [
        "uuid-patterns",
        "uuid-stale",
    ]
    assert all("filename" not in a for a in delete_args)
    # ingest content is base64-encoded.
    ingest_args = inst.args_for("memory_ingest")
    encoded = {a["filename"]: a["content_base64"] for a in ingest_args}
    assert encoded["systemPatterns.md"] == base64.b64encode(b"patterns").decode("ascii")
    # metrics dict shape unchanged.
    assert result["status"] == "ok"
    assert result["pushed"] == 1
    assert result["deleted_before_reingest"] == 1
    assert result["cleaned_orphans"] == 1
    assert result["errors"] == 0
    # Additive P4-8 fields (volatile guardrail): nothing volatile in this bank.
    assert result["skipped_volatile"] == []
    assert result["pushed_files"] == ["systemPatterns.md"]
    # push metrics persisted to _meta.
    meta = await storage.get_json("space-a/_meta.json")
    assert meta["graph_memory"]["push_count"] == 1
    assert meta["graph_memory"]["files_pushed"] == 1
    # The bank-mirror ledger is rewritten to the CURRENT mirror set.
    assert meta["graph_memory"]["bank_mirror"] == ["systemPatterns.md"]


async def test_disconnect_byte_for_byte() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        result = await engine.disconnect("space-a")

    assert result["status"] == "disconnected"
    assert result["was_connected_to"]["memory_id"] == "mem-1"
    # No transport built for a pure local-meta operation.
    assert factory.instances == []
    meta = await storage.get_json("space-a/_meta.json")
    assert meta["graph_memory"] is None


async def test_default_factory_builds_real_client() -> None:
    # GraphBridgeService() with no injection builds the REAL GraphMemoryClient.
    bridge = GraphBridgeService()
    client = bridge._make_client("https://x", "t")
    ref = GraphMemoryClient("https://x", "t")
    assert isinstance(client, GraphMemoryClient)
    assert client._base_url == ref._base_url
    assert client._token == ref._token
    assert client._timeout == ref._timeout


def test_default_client_factory_is_the_class() -> None:
    bridge = GraphBridgeService()
    # Default factory IS the class object, not a wrapper lambda.
    assert bridge._client_factory is GraphMemoryClient


async def test_push_timeout_kwarg_preserved() -> None:
    storage = FakeStorage()
    await storage.put_json("space-a/_meta.json", _meta_connected())
    # P4-8: seed a DURABLE file so a client is built and the push proceeds even
    # under the default volatile filter (activeContext.md alone would be skipped,
    # but the original-bank-non-empty path still builds the client either way).
    await storage.put("space-a/bank/systemPatterns.md", "patterns")
    engine, bridge, factory = _build(storage)

    with _patch_storage(storage):
        await engine.push("space-a")

    # Push builds the client with timeout=180.0 (unchanged from legacy).
    assert factory.instances[-1].timeout == 180.0


# =============================================================================
# GROUP D — import gate / no-commit-path (AST)
# =============================================================================

_LONG_SRC = Path(inspect.getsourcefile(LongEngine)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_LONG_TREE = ast.parse(_LONG_SRC)

import live_mem.core.graph_bridge as _gb_mod  # noqa: E402

_BRIDGE_SRC = Path(inspect.getsourcefile(_gb_mod)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_BRIDGE_TREE = ast.parse(_BRIDGE_SRC)

_FAKE_SRC = Path(inspect.getsourcefile(FakeGraphTransport)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_FAKE_TREE = ast.parse(_FAKE_SRC)


def _imports_of(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.append("import " + ", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            out.append(
                f"from {prefix}{node.module or ''} import "
                + ", ".join(a.name for a in node.names)
            )
    return out


# Commit-path modules the long/graph engine path must never import. P4-5 reads
# committed coords for the watermark via HARD-CODED ``_hivemind`` paths (NOT via
# ``hivemind.layout``), precisely so the long tier imports nothing from the
# ``hivemind`` subpackage — whose ``__init__`` eagerly loads the commit-state
# module. The marker is therefore the strict blanket ``hivemind`` again.
_COMMIT_MODULE_MARKERS = (
    "consolidator",
    "hivemind",
    "write_sink",
    "engines.mid",
    "mid_engine",
)


def test_long_engine_still_imports_only_graph_bridge() -> None:
    imports = _imports_of(_LONG_TREE)
    runtime = [
        i for i in imports if "__future__" not in i and "typing" not in i
    ]
    assert runtime == [
        "from ..graph_bridge import GraphBridgeService, get_graph_bridge"
    ], runtime
    lowered = [i.lower() for i in imports]
    for forbidden in ("neo4j", "qdrant", "mcp"):
        assert not any(forbidden in i for i in lowered), forbidden


def test_long_engine_no_assert_commit_allowed_ast() -> None:
    # No assert_commit_allowed name/attr anywhere in the engine module.
    for node in ast.walk(_LONG_TREE):
        if isinstance(node, ast.Name):
            assert node.id != "assert_commit_allowed"
        if isinstance(node, ast.Attribute):
            assert node.attr != "assert_commit_allowed"
    # No commit-path module imported.
    for imp in _imports_of(_LONG_TREE):
        low = imp.lower()
        for marker in _COMMIT_MODULE_MARKERS:
            assert marker not in low, f"forbidden commit-path import: {imp}"


def test_graph_bridge_no_commit_path_import_ast() -> None:
    # graph_bridge legitimately imports mcp (transport) — assert ONLY against
    # commit-path modules, never mcp.
    for imp in _imports_of(_BRIDGE_TREE):
        low = imp.lower()
        for marker in _COMMIT_MODULE_MARKERS:
            assert marker not in low, f"forbidden commit-path import: {imp}"
    # The new methods add no assert_commit_allowed name/attr (AST, so the
    # docstring/comment prose that *names* it as a forbidden symbol is ignored).
    for node in ast.walk(_BRIDGE_TREE):
        if isinstance(node, ast.Name):
            assert node.id != "assert_commit_allowed"
        if isinstance(node, ast.Attribute):
            assert node.attr != "assert_commit_allowed"


def test_long_engine_surface_includes_new_typed_methods() -> None:
    public_async = {
        name
        for name, member in inspect.getmembers(
            LongEngine, predicate=inspect.iscoroutinefunction
        )
        if not name.startswith("_")
    }
    assert public_async == {
        "connect",
        "push",
        "status",
        "disconnect",
        "ingest",
        "list_ontologies",
        "query",
        "search",
        "plan_ingest",
    }
    # No commit/rollback/audit authority method crept in.
    forbidden = ("assert_commit_allowed", "commit", "rollback", "audit", "recover")
    all_names = {name for name, _ in inspect.getmembers(LongEngine)}
    for bad in forbidden:
        assert bad not in all_names


def test_fake_graph_transport_no_network_imports() -> None:
    imports = _imports_of(_FAKE_TREE)
    lowered = [i.lower() for i in imports]
    for forbidden in (
        "mcp",
        "streamablehttp",
        "boto3",
        "openai",
        "httpx",
        "requests",
    ):
        assert not any(forbidden in i for i in lowered), forbidden


# =============================================================================
# GROUP E — SSRF function relocation regression
# =============================================================================


def test_validate_gm_url_still_importable_from_tools_graph() -> None:
    from live_mem.tools.graph import _validate_gm_url

    # Re-export still works and still blocks loopback.
    err = _validate_gm_url("http://127.0.0.1")
    assert err is not None
    assert "loopback" in err
    # Safe URL passes.
    assert _validate_gm_url("https://gm.example.com") is None


# =============================================================================
# GROUP F — fake reuse safety: per-instance FIFO isolation
# =============================================================================


def test_factory_isolates_list_responses_per_instance() -> None:
    """A factory shared across P4-5/7/8 tests must give each built instance its
    OWN copy of list-form (FIFO) responses. The real bridge builds a fresh client
    per method call, so a shared list would drain globally across instances —
    a latent reuse footgun. Two instances must each see the FIFO head."""
    factory = FakeGraphTransport.factory(
        responses={"memory_query": [{"n": 1}, {"n": 2}]}
    )
    inst1 = factory("http://gm.test", "tok")
    inst2 = factory("http://gm.test", "tok")
    # Distinct list objects (not the same shared reference).
    assert inst1._responses["memory_query"] is not inst2._responses["memory_query"]
    # Draining inst1's FIFO must not affect a freshly-built inst2.
    inst1._responses["memory_query"].pop(0)
    assert inst2._responses["memory_query"][0] == {"n": 1}
