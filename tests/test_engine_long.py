# -*- coding: utf-8 -*-
"""
Tests for P3-5 (issue #54) — LongEngine port + thin adapter over
GraphBridgeService (no Graph Memory import).

Deterministic and offline: a FAKE graph bridge records every call and returns
canned dicts. No real Neo4j / Qdrant / MCP / network / S3 / LLM.

Surfaces verified (ADR-0006 + ADR-0010 long-authority invariant):
- Every method (connect/push/status/disconnect) passes the call through to the
  wrapped bridge UNCHANGED — same args, same returned dict identity (no
  transformation, no reshape).
- Calls are forwarded with exact args / kwargs and no reordering.
- The default-None constructor lazily resolves get_graph_bridge() (monkeypatched
  to a sentinel) — no real MCP/network client is built at def/import time.
- The four original bridge methods remain exact pass-throughs. The current
  ten-member public async surface and the no-authority name set have one
  canonical assertion in ``test_long_engine.py``.
- The constructor accepts NO write_sink param and the module never imports/uses
  WriteSink (LongEngine never writes _hivemind/).
- Grep the long_engine.py source: no neo4j / qdrant / mcp client / new Graph
  Memory dependency import (import gate held).
- The class docstring carries the downstream / watermark-only / never-on-commit
  invariant and cites ADR-0006 + ADR-0010.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from live_mem.core.engines.long_engine import LongEngine


# =============================================================================
# Fake graph bridge — offline, records calls, returns canned dicts.
# Duck-typed (no need to subclass GraphBridgeService); matches its async
# connect/push/status/disconnect signatures incl. connect's keyword params.
# =============================================================================


class FakeGraphBridge:
    """Records (method, args, kwargs) and returns a per-method canned dict.

    The returned dicts are stored so tests can assert the adapter returns the
    SAME object (identity) without any transformation.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.connect_result = {"status": "connected", "marker": "connect"}
        self.push_result = {
            "status": "ok",
            "marker": "push",
            "bank_version": 7,
            "commit_id": "c-7",
        }
        self.status_result = {"status": "ok", "marker": "status", "connected": True}
        self.disconnect_result = {"status": "disconnected", "marker": "disconnect"}
        # P4-4 typed methods — canned per-method dicts (identity-checked below).
        self.ingest_result = {"status": "ok", "marker": "ingest", "ingested": True}
        self.list_ontologies_result = {
            "status": "ok",
            "marker": "list_ontologies",
            "ontologies": ["general"],
        }
        self.query_result = {"status": "ok", "marker": "query", "results": []}
        self.search_result = {"status": "ok", "marker": "search", "results": []}

    async def connect(
        self,
        space_id: str,
        url: str,
        token: str,
        memory_id: str,
        ontology: str = "general",
    ) -> dict:
        self.calls.append(
            (
                "connect",
                (),
                {
                    "space_id": space_id,
                    "url": url,
                    "token": token,
                    "memory_id": memory_id,
                    "ontology": ontology,
                },
            )
        )
        return self.connect_result

    async def push(self, space_id: str, *, include_volatile: bool = False) -> dict:
        # P4-8: the engine forwards include_volatile VERBATIM (keyword-only), so
        # the fake mirrors the bridge signature and records it for exact-args
        # assertions.
        self.calls.append(("push", (space_id,), {"include_volatile": include_volatile}))
        return self.push_result

    async def status(self, space_id: str) -> dict:
        self.calls.append(("status", (space_id,), {}))
        return self.status_result

    async def disconnect(
        self, space_id: str, *, use_embedded: bool = False
    ) -> dict:
        kwargs = {"use_embedded": True} if use_embedded else {}
        self.calls.append(("disconnect", (space_id,), kwargs))
        return self.disconnect_result

    async def ingest(
        self,
        space_id: str,
        *,
        filename: str,
        content: str | None = None,
        content_base64: str | None = None,
        source_path: str | None = None,
        source_modified_at: str | None = None,
        metadata: dict | None = None,
        force: bool = False,
    ) -> dict:
        self.calls.append(
            (
                "ingest",
                (space_id,),
                {
                    "filename": filename,
                    "content": content,
                    "content_base64": content_base64,
                    "source_path": source_path,
                    "source_modified_at": source_modified_at,
                    "metadata": metadata,
                    "force": force,
                },
            )
        )
        return self.ingest_result

    async def list_ontologies(self, space_id: str) -> dict:
        self.calls.append(("list_ontologies", (space_id,), {}))
        return self.list_ontologies_result

    async def query(self, space_id: str, query: str, limit: int = 10) -> dict:
        self.calls.append(("query", (space_id, query), {"limit": limit}))
        return self.query_result

    async def search(self, space_id: str, query: str, limit: int = 10) -> dict:
        self.calls.append(("search", (space_id, query), {"limit": limit}))
        return self.search_result


_SOURCE_PATH = Path(
    inspect.getsourcefile(LongEngine)  # type: ignore[arg-type]
)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def _import_statements() -> list[str]:
    """All real ``import`` / ``from ... import`` statements (AST, not grep).

    Parsing via AST means docstring/comment prose that merely *starts with*
    'import ' or 'from ' (e.g. a wrapped sentence) is never mistaken for an
    actual import — only real import nodes are returned.
    """
    out: list[str] = []
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            out.append("import " + ", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            out.append(
                f"from {prefix}{node.module or ''} import "
                + ", ".join(a.name for a in node.names)
            )
    return out


def _code_without_docstrings() -> str:
    """Source with module/class/function docstrings blanked out.

    Lets WriteSink/GM grep assertions target real *code* (imports, annotations,
    constructions) without tripping on docstring prose that documents the
    invariants (the docstring is REQUIRED to say 'takes NO WriteSink').
    """
    docstrings: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            ds = ast.get_docstring(node, clean=False)
            if ds:
                docstrings.add(ds)
    stripped = _SOURCE
    for ds in docstrings:
        stripped = stripped.replace(ds, "")
    return stripped


# =============================================================================
# Pass-through behavior — verbatim delegation, zero transformation.
# =============================================================================


@pytest.mark.asyncio
async def test_long_engine_connect_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.connect(
        space_id="space-a",
        url="http://gm.local",
        token="tok",
        memory_id="mem-1",
    )

    # Returned dict is the bridge's, unchanged (identity + equality).
    assert result is bridge.connect_result
    assert bridge.calls == [
        (
            "connect",
            (),
            {
                "space_id": "space-a",
                "url": "http://gm.local",
                "token": "tok",
                "memory_id": "mem-1",
                # ontology default forwarded verbatim.
                "ontology": "general",
            },
        )
    ]


@pytest.mark.asyncio
async def test_long_engine_connect_forwards_explicit_ontology() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    await engine.connect(
        space_id="space-a",
        url="http://gm.local",
        token="tok",
        memory_id="mem-1",
        ontology="domain-x",
    )

    assert bridge.calls[0][2]["ontology"] == "domain-x"


@pytest.mark.asyncio
async def test_long_engine_push_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.push("space-b")

    # Zero transformation: same dict, including the watermark fields which are
    # inputs-only and must NOT be reshaped.
    assert result is bridge.push_result
    assert result["bank_version"] == 7
    assert result["commit_id"] == "c-7"
    # P4-8: include_volatile is forwarded VERBATIM (default False) — the engine
    # adds no other transformation.
    assert bridge.calls == [("push", ("space-b",), {"include_volatile": False})]


@pytest.mark.asyncio
async def test_long_engine_status_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.status("space-c")

    assert result is bridge.status_result
    assert bridge.calls == [("status", ("space-c",), {})]


@pytest.mark.asyncio
async def test_long_engine_disconnect_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.disconnect("space-d")

    assert result is bridge.disconnect_result
    assert bridge.calls == [("disconnect", ("space-d",), {})]


@pytest.mark.asyncio
async def test_long_engine_disconnect_forwards_embedded_maintenance_mode() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.disconnect("space-d", use_embedded=True)

    assert result is bridge.disconnect_result
    assert bridge.calls == [
        ("disconnect", ("space-d",), {"use_embedded": True})
    ]


@pytest.mark.asyncio
async def test_long_engine_ingest_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.ingest(
        "space-e",
        filename="doc.md",
        content="hello",
        force=True,
    )

    assert result is bridge.ingest_result
    assert bridge.calls == [
        (
            "ingest",
            ("space-e",),
            {
                "filename": "doc.md",
                "content": "hello",
                "content_base64": None,
                "source_path": None,
                "source_modified_at": None,
                "metadata": None,
                "force": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_long_engine_list_ontologies_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.list_ontologies("space-f")

    assert result is bridge.list_ontologies_result
    assert bridge.calls == [("list_ontologies", ("space-f",), {})]


@pytest.mark.asyncio
async def test_long_engine_query_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.query("space-g", "who", limit=3)

    assert result is bridge.query_result
    assert bridge.calls == [("query", ("space-g", "who"), {"limit": 3})]


@pytest.mark.asyncio
async def test_long_engine_search_passes_through() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    result = await engine.search("space-h", "what")

    # Default limit forwarded verbatim.
    assert result is bridge.search_result
    assert bridge.calls == [("search", ("space-h", "what"), {"limit": 10})]


@pytest.mark.asyncio
async def test_long_engine_records_call_order_and_exact_args() -> None:
    bridge = FakeGraphBridge()
    engine = LongEngine(bridge=bridge)

    await engine.connect(
        space_id="s", url="u", token="t", memory_id="m", ontology="o"
    )
    await engine.push("s")
    await engine.status("s")
    await engine.disconnect("s")

    # Exact forwarding, no extra/dropped args, no reordering.
    assert [c[0] for c in bridge.calls] == [
        "connect",
        "push",
        "status",
        "disconnect",
    ]
    assert bridge.calls[1] == ("push", ("s",), {"include_volatile": False})
    assert bridge.calls[2] == ("status", ("s",), {})
    assert bridge.calls[3] == ("disconnect", ("s",), {})


# =============================================================================
# Lazy DI default — resolves get_graph_bridge() at construction, not import.
# =============================================================================


def test_long_engine_default_bridge_is_lazy_singleton(monkeypatch) -> None:
    sentinel = object()
    calls = {"n": 0}

    def fake_get_graph_bridge():
        calls["n"] += 1
        return sentinel

    # Patch where LongEngine looks the symbol up (its own module namespace).
    monkeypatch.setattr(
        "live_mem.core.engines.long_engine.get_graph_bridge",
        fake_get_graph_bridge,
    )

    engine = LongEngine()  # no bridge injected -> lazy resolve

    assert engine.bridge is sentinel
    # Resolved exactly once, at construction (not at import/def time).
    assert calls["n"] == 1


def test_long_engine_injected_bridge_skips_singleton(monkeypatch) -> None:
    def boom():  # pragma: no cover - must never be called
        raise AssertionError("get_graph_bridge must not run when bridge injected")

    monkeypatch.setattr(
        "live_mem.core.engines.long_engine.get_graph_bridge", boom
    )
    bridge = FakeGraphBridge()

    engine = LongEngine(bridge=bridge)

    assert engine.bridge is bridge


# =============================================================================
# Constructor / authority boundary — no WriteSink.
# =============================================================================


def test_long_engine_takes_no_writesink() -> None:
    params = inspect.signature(LongEngine.__init__).parameters
    assert "write_sink" not in params
    assert set(params) == {"self", "bridge"}
    # Module CODE never imports or uses WriteSink (docstring prose is allowed to
    # *document* that it takes none — that's the invariant being asserted).
    assert not any(
        "write_sink" in imp.lower() for imp in _import_statements()
    ), _import_statements()
    code = _code_without_docstrings()
    assert "WriteSink" not in code
    assert "write_sink" not in code


# =============================================================================
# Import gate — no Graph Memory / neo4j / qdrant / mcp client import.
# =============================================================================


def test_long_engine_no_graph_memory_import() -> None:
    # AST-based: no GM client dependency imported at the engine layer (one
    # indirection away from graph_bridge.py, which imports mcp itself). Docstring
    # prose that *names* neo4j/qdrant/mcp (to document the gate) is not an import
    # and is correctly ignored here.
    imports = _import_statements()
    lowered = [imp.lower() for imp in imports]
    for forbidden in ("neo4j", "qdrant", "mcp"):
        assert not any(
            forbidden in imp for imp in lowered
        ), f"GM dependency imported: {forbidden} in {imports}"

    # The ONLY runtime import is from ..graph_bridge (typing/__future__ aside).
    runtime_imports = [
        imp
        for imp in imports
        if "__future__" not in imp and "typing" not in imp
    ]
    assert runtime_imports == [
        "from ..graph_bridge import GraphBridgeService, get_graph_bridge"
    ], runtime_imports


# =============================================================================
# Docstring encodes the downstream / watermark-only invariant.
# =============================================================================


def test_long_engine_docstring_encodes_downstream_invariant() -> None:
    doc = (LongEngine.__doc__ or "").lower()
    assert doc, "LongEngine must have a class docstring"
    # Stable substrings (not full prose) so docstring polish doesn't break this.
    assert "adr-0006" in doc
    assert "adr-0010" in doc
    assert "downstream" in doc
    assert "watermark" in doc
    # Never on the commit/rollback/audit/recovery path.
    assert "commit" in doc
    assert "rollback" in doc
    assert "recovery" in doc or "recover" in doc
