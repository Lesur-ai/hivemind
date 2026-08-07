# -*- coding: utf-8 -*-
"""
P4-7 — ``long_ingest`` / ``long_query`` MCP tools + the ``LongEngine.plan_ingest``
planning method, proven SAFE, deterministic, and strictly downstream (ADR-0010,
reconciled EPIC-P4, EVOLUTION_LIVE_GRAPH_INTEGRATION.md Vague C, D13 plan-only).

``long_ingest`` is canonical document ingestion as a FIRST-CLASS long-tier
capability, DISTINCT from the filename-keyed ``graph_push`` bank mirror.
Documents are keyed by a stable ``source_path`` (NOT the mutable bank filename)
and carry an optional SHA-256. The ENGINE plans; the server is NOT a blind proxy
(EVOLUTION C-Q2.a). Three modes:

- ``dry-run``     — return the planned ``{source_path, sha256}`` set with ZERO
                    transport write (no GM call at all).
- ``check-remote``— SKIP / UPDATE / INGEST plan by comparing each doc's sha256
                    against the remote (read-only ``document_list``). No writes.
- ``apply``       — DEFERRED in v1 (D13 / EVOLUTION Vague C apply is codex-gated,
                    v2.7.0+): a structured ``applied: false`` result with a clear
                    reason. NO blind ingestion write from the tool in v1.

``long_query`` is a READ-ONLY tool over ``LongEngine.query`` (``memory_query``).

Volatile files (``activeContext.md`` / ``progress.md`` —
``config.GRAPH_PUSH_VOLATILE_FILES``, matched by BASENAME) are REJECTED from
canonical ingestion by default; an opt-in requires the ``manage`` permission and
emits a structured ``long_ingest_volatile_optin`` audit event (reusing the P4-8
``tools/graph.py`` audit pattern on ``logging.getLogger("live_mem.audit")``).
This path NEVER imports or calls the commit path (negative-import AST test).

This suite drives the REAL tool functions (extracted from a real FastMCP built by
``tools.graph.register``) over the same deterministic seam the P4-4 / P4-8 suites
use: a real :class:`LongEngine` wrapping a real :class:`GraphBridgeService` wired
to a :class:`FakeGraphTransport`, over an in-memory :class:`FakeStorage`. NO
network / S3 / Neo4j / Qdrant / LLM. This phase suite asserts only that its
``long_ingest`` / ``long_query`` additions are registered directly by
``tools/graph.py`` and not via ALIAS_MAP; the global registered surface and
count are owned by ``test_mcp_tool_surface.py`` and its canonical fixture.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.engines.long_engine import LongEngine
from live_mem.tools import graph as graph_tools
from live_mem.tools import register_all_tools
from tests.fakes import FakeGraphTransport, GraphLongFakeStorage as FakeStorage


# =============================================================================
# Fixtures / helpers
# =============================================================================

_SPACE = "space-a"
_MEM = "mem-1"
_URL = "https://gm.example.com"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _meta_connected() -> dict:
    """A connected-space ``_meta.json`` (local-only graph_memory block)."""
    return {
        "space_id": _SPACE,
        "version": 1,
        "graph_memory": {
            "url": _URL,
            "token": "tok-secret",
            "memory_id": _MEM,
            "ontology": "general",
            "last_push": None,
            "push_count": 0,
            "files_pushed": 0,
        },
    }


def _token(name: str, permissions: list[str]) -> dict:
    """A token scoped to _SPACE so check_access passes for non-admin tokens.

    ``token_hash`` lets the fresh-token store key it (the check_* helpers read
    the fresh store first); the bytes are deterministic per name."""
    return {
        "client_name": name,
        "permissions": permissions,
        "allowed_resources": [_SPACE],
        "token_hash": "sha256:" + (name[:1] * 64)[:64],
    }


def _set_token(tok: dict):
    """Set both the contextvar and the fresh-token store, mirroring
    test_graph_push_volatile_tool.py. Returns a reset callable."""
    from live_mem.auth.context import update_fresh_token, invalidate_token_in_store

    ctx_tok = current_token_info.set(tok)
    update_fresh_token(tok)

    def _reset():
        current_token_info.reset(ctx_tok)
        invalidate_token_in_store(tok["token_hash"])

    return _reset


class _FakeRegistry:
    """Stands in for the EngineRegistry: ``long_engine()`` returns a real
    LongEngine over a real bridge wired to the fake transport factory."""

    def __init__(self, factory) -> None:
        self._engine = LongEngine(bridge=GraphBridgeService(client_factory=factory))

    def long_engine(self) -> LongEngine:
        return self._engine


def _wire(storage: "FakeStorage", **factory_kwargs):
    """Patch get_engine_registry (the tool imports it locally from
    live_mem.core.engines) + get_storage (bridge module). Returns
    ``(patches, storage_patch, factory)`` so tests can inspect built instances."""
    factory = FakeGraphTransport.factory(**factory_kwargs)
    registry = _FakeRegistry(factory)
    patches = patch.multiple(
        "live_mem.core.engines",
        get_engine_registry=lambda: registry,
    )
    storage_patch = patch(
        "live_mem.core.graph_bridge.get_storage", return_value=storage
    )
    return patches, storage_patch, factory


def _tool_fn(name: str):
    """Extract a real tool function from a freshly-registered graph FastMCP
    (the tool body carries the mode dispatch, permission gates, and audit emit)."""
    mcp = FastMCP(name="test-long-ingest")
    graph_tools.register(mcp)
    return mcp._tool_manager._tools[name].fn


def _doc_list_response(documents: list[dict]) -> dict:
    """Canned ``document_list`` payload shaped like the real GM list response.
    The real GM list surfaces ``filename`` + ``sha256`` (the latter on the node);
    check-remote compares the planned doc's sha256 against this remote set keyed
    by source_path-or-filename."""
    return {"status": "ok", "memory_id": _MEM, "documents": documents}


# A small canonical document set, keyed by stable source_path (NOT bank filename).
_DOC_A = {"source_path": "docs/rfc-0007-routing.md", "content": "routing rules v1"}
_DOC_B = {"source_path": "docs/incident-2031.md", "content": "postmortem body"}


# =============================================================================
# (7) PHASE DELTA — long_ingest + long_query are net-new long_* tools
# registered directly in tools/graph.py (NOT via ALIAS_MAP). The global MCP
# surface/count authority lives in test_mcp_tool_surface.py.
# =============================================================================


def _build_full():
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp


def test_long_ingest_and_long_query_registered() -> None:
    mcp = _build_full()
    names = set(mcp._tool_manager._tools)
    assert "long_ingest" in names
    assert "long_query" in names


def test_new_long_tools_are_not_aliases() -> None:
    """long_ingest / long_query are NET-NEW long_* tools with NO graph_* twin:
    they must NOT be wired through the compatibility ALIAS_MAP."""
    from live_mem.tools.aliases import ALIAS_MAP

    assert "long_ingest" not in ALIAS_MAP.values()
    assert "long_query" not in ALIAS_MAP.values()


def test_graph_register_returns_seven() -> None:
    """The graph category registers the legacy four plus three direct long tools."""
    mcp = FastMCP(name="test-graph-count")
    n = graph_tools.register(mcp)
    assert n == 7
    names = set(mcp._tool_manager._tools)
    assert {"long_ingest", "long_query", "long_reindex"} <= names
    # The legacy four are still there.
    assert {"graph_connect", "graph_push", "graph_status", "graph_disconnect"} <= names


# =============================================================================
# (1) DRY-RUN — returns the planned {source_path, sha256} set and makes ZERO
# transport calls (no GM client built at all).
# =============================================================================


async def test_dry_run_returns_plan_and_makes_zero_transport_calls() -> None:
    """mode='dry-run': the response enumerates the planned docs as
    {source_path, sha256}, computes sha256 server-side when omitted, and builds
    NO GM client (factory.instances == []) — proving zero transport contact."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    documents = [
        {"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]},
        # sha256 supplied explicitly by the caller -> must be echoed verbatim.
        {
            "source_path": _DOC_B["source_path"],
            "content": _DOC_B["content"],
            "sha256": _sha256(_DOC_B["content"]),
        },
    ]

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE, documents=documents, mode="dry-run"
            )
    finally:
        reset()

    assert result["status"] == "ok"
    assert result["mode"] == "dry-run"
    # The planned set is the {source_path, sha256} pairs (sha256 computed when
    # absent, echoed when present), keyed by the STABLE source_path.
    planned = {d["source_path"]: d["sha256"] for d in result["planned"]}
    assert planned == {
        _DOC_A["source_path"]: _sha256(_DOC_A["content"]),
        _DOC_B["source_path"]: _sha256(_DOC_B["content"]),
    }
    # ── The load-bearing assertion: ZERO transport calls on dry-run ──────────
    assert factory.instances == []


async def test_dry_run_is_plan_only_no_apply_no_writes() -> None:
    """dry-run never reports applied:true and never issues a write call. Even if
    a client were built (it must not be), no memory_ingest / document_delete is
    recorded."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[{"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]}],
                mode="dry-run",
            )
    finally:
        reset()

    assert result.get("applied", False) is False
    # No client, hence no write tool name anywhere.
    flat = [n for inst in factory.instances for n in inst.tool_names()]
    assert "memory_ingest" not in flat
    assert "document_delete" not in flat


# =============================================================================
# (2) CHECK-REMOTE — SKIP / UPDATE / INGEST plan vs a fake remote document_list
# (seeded remote sha256s); a READ-ONLY document_list, NO write calls.
# =============================================================================


async def test_check_remote_classifies_skip_update_ingest() -> None:
    """mode='check-remote' compares each planned doc's sha256 against the remote
    document_list (seeded) and yields a per-doc action:

    - SKIP   — remote sha256 matches the planned sha256 (already current);
    - UPDATE — remote has the doc but the sha256 differs (content changed);
    - INGEST — remote has no entry for this source_path (net-new).

    It issues a single read-only document_list and ZERO write calls."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())

    skip_doc = {"source_path": "docs/skip.md", "content": "unchanged"}
    update_doc = {"source_path": "docs/update.md", "content": "new body"}
    ingest_doc = {"source_path": "docs/new.md", "content": "brand new"}

    # Remote already holds skip.md at the SAME sha256 and update.md at a STALE one.
    responses = {
        "document_list": _doc_list_response(
            [
                {"source_path": "docs/skip.md", "sha256": _sha256(skip_doc["content"])},
                {"source_path": "docs/update.md", "sha256": _sha256("OLD body")},
            ]
        )
    }
    patches, storage_patch, factory = _wire(storage, responses=responses)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[skip_doc, update_doc, ingest_doc],
                mode="check-remote",
            )
    finally:
        reset()

    assert result["status"] == "ok"
    assert result["mode"] == "check-remote"
    plan = {d["source_path"]: d["action"] for d in result["plan"]}
    assert plan == {
        "docs/skip.md": "SKIP",
        "docs/update.md": "UPDATE",
        "docs/new.md": "INGEST",
    }

    # ── Read-only: exactly one document_list, ZERO writes ────────────────────
    inst = factory.instances[-1]
    assert inst.tool_names() == ["document_list"]
    flat = inst.tool_names()
    assert "memory_ingest" not in flat
    assert "document_delete" not in flat


async def test_check_remote_makes_no_write_calls_when_remote_empty() -> None:
    """An empty remote means every planned doc is INGEST — still a read-only
    plan, never a write."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    # Default FakeGraphTransport document_list is {"status":"ok","documents":[]}.
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[
                    {"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]},
                    {"source_path": _DOC_B["source_path"], "content": _DOC_B["content"]},
                ],
                mode="check-remote",
            )
    finally:
        reset()

    actions = {d["source_path"]: d["action"] for d in result["plan"]}
    assert actions == {
        _DOC_A["source_path"]: "INGEST",
        _DOC_B["source_path"]: "INGEST",
    }
    inst = factory.instances[-1]
    assert inst.tool_names() == ["document_list"]
    assert "memory_ingest" not in inst.tool_names()


# =============================================================================
# (3) APPLY — DEFERRED in v1: applied:false with a clear reason, NO blind ingest.
# =============================================================================


async def test_apply_is_deferred_no_blind_ingest() -> None:
    """mode='apply' returns status ok + applied:false + a clear deferral reason
    (D13 / EVOLUTION Vague C: apply is codex-gated, v2.7.0+). It must NOT perform
    any ingestion write — no GM client built, no memory_ingest issued."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("manager", ["read", "write", "manage"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[{"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]}],
                mode="apply",
            )
    finally:
        reset()

    assert result["status"] == "ok"
    assert result["mode"] == "apply"
    assert result["applied"] is False
    # A clear, structured reason — not a silent no-op.
    assert isinstance(result.get("reason"), str) and result["reason"]
    assert "defer" in result["reason"].lower() or "plan-only" in result["reason"].lower()

    # ── No blind ingest: nothing was written to GM ───────────────────────────
    assert factory.instances == []
    flat = [n for inst in factory.instances for n in inst.tool_names()]
    assert "memory_ingest" not in flat


# =============================================================================
# (4) LONG_QUERY — read-only, returns FakeGraphTransport (memory_query) results.
# =============================================================================


async def test_long_query_returns_memory_query_results() -> None:
    """long_query is a thin READ-ONLY tool over LongEngine.query: it issues a
    single memory_query and returns the GM payload verbatim."""
    long_query = _tool_fn("long_query")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    canned = {"status": "ok", "results": [{"id": 1, "text": "routing answer"}]}
    patches, storage_patch, factory = _wire(storage, responses={"memory_query": canned})

    reset = _set_token(_token("reader", ["read"]))
    try:
        with patches, storage_patch:
            result = await long_query(space_id=_SPACE, query="how does routing work", limit=5)
    finally:
        reset()

    # Returned verbatim from the fake transport.
    assert result == canned
    inst = factory.instances[-1]
    assert inst.tool_names() == ["memory_query"]
    (args,) = inst.args_for("memory_query")
    assert args == {"memory_id": _MEM, "query": "how does routing work", "limit": 5}


def test_long_query_is_read_only_annotation() -> None:
    """long_query carries readOnlyHint=True (the contract: a thin read tool)."""
    mcp = FastMCP(name="test-ro")
    graph_tools.register(mcp)
    tool = mcp._tool_manager._tools["long_query"]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True


# =============================================================================
# (5) VOLATILE — source_path basename activeContext.md / progress.md is REJECTED
# by default; include_volatile opt-in requires 'manage' (refused without) and
# emits the long_ingest_volatile_optin audit.
# =============================================================================


async def test_volatile_source_path_rejected_by_default() -> None:
    """A planned doc whose source_path basename is a configured volatile file
    (activeContext.md / progress.md) is REJECTED from canonical ingestion by
    default — even on a harmless dry-run, the guard fires before planning."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[
                    {"source_path": "memory-bank/activeContext.md", "content": "ctx"},
                    {"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]},
                ],
                mode="dry-run",
            )
    finally:
        reset()

    assert result["status"] == "error"
    # The rejected volatile basename is named in the message.
    assert "activeContext.md" in result["message"]
    assert "volatile" in result["message"].lower()
    # Refused before any transport contact.
    assert factory.instances == []


async def test_volatile_optin_refused_without_manage() -> None:
    """include_volatile=True requires the 'manage' permission. A write-only
    token is REFUSED with the manage error; nothing is planned, no audit fires,
    no transport built (gate is BEFORE the audit emit)."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[{"source_path": "memory-bank/progress.md", "content": "prog"}],
                mode="dry-run",
                include_volatile=True,
            )
    finally:
        reset()

    assert result["status"] == "error"
    assert "manage" in result["message"]
    assert factory.instances == []


async def test_volatile_optin_with_manage_emits_audit_and_plans(caplog) -> None:
    """include_volatile=True + a 'manage' token PROCEEDS: the volatile doc is
    planned (dry-run), and EXACTLY one structured long_ingest_volatile_optin
    audit line fires on live_mem.audit (reuse of the P4-8 tool-layer pattern)."""
    long_ingest = _tool_fn("long_ingest")

    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, factory = _wire(storage)

    def _optin_lines() -> list:
        return [
            r
            for r in caplog.records
            if r.name == "live_mem.audit" and "long_ingest_volatile_optin" in r.message
        ]

    vol_doc = {"source_path": "memory-bank/activeContext.md", "content": "ctx"}

    reset = _set_token(_token("manager", ["read", "write", "manage"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches, storage_patch:
            result = await long_ingest(
                space_id=_SPACE,
                documents=[vol_doc],
                mode="dry-run",
                include_volatile=True,
            )
    finally:
        reset()

    # The opt-in let the volatile doc through the plan.
    assert result["status"] == "ok"
    planned = {d["source_path"] for d in result["planned"]}
    assert "memory-bank/activeContext.md" in planned
    # dry-run is still zero-transport even on the opt-in path.
    assert factory.instances == []

    # ── Exactly one audit line, structured, naming the volatile source_path ──
    lines = _optin_lines()
    assert len(lines) == 1
    payload = json.loads(lines[-1].message)
    assert payload["event"] == "long_ingest_volatile_optin"
    assert payload["space_id"] == _SPACE
    assert payload["caller"] == "manager"
    # The audit enumerates the volatile source_paths that were force-admitted.
    assert "memory-bank/activeContext.md" in payload["volatile_source_paths"]


async def test_volatile_optin_no_audit_on_default_or_refused(caplog) -> None:
    """The audit fires ONLY on an authorized opt-in: never on a default (no
    include_volatile) ingest of durable docs, and never on a refused opt-in
    (write-only token) — the audit is emitted AFTER the manage gate."""
    long_ingest = _tool_fn("long_ingest")

    def _optin_lines() -> list:
        return [
            r
            for r in caplog.records
            if r.name == "live_mem.audit" and "long_ingest_volatile_optin" in r.message
        ]

    # ── (a) default dry-run of a durable doc -> no audit ─────────────────────
    storage = FakeStorage()
    await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches, storage_patch, _f = _wire(storage)
    reset = _set_token(_token("writer", ["read", "write"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches, storage_patch:
            await long_ingest(
                space_id=_SPACE,
                documents=[{"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]}],
                mode="dry-run",
            )
    finally:
        reset()
    assert _optin_lines() == []

    # ── (b) refused opt-in (write-only) -> no audit (gate first) ─────────────
    caplog.clear()
    storage2 = FakeStorage()
    await storage2.put_json(f"{_SPACE}/_meta.json", _meta_connected())
    patches2, storage_patch2, _f2 = _wire(storage2)
    reset2 = _set_token(_token("writer", ["read", "write"]))
    try:
        with caplog.at_level(logging.INFO, logger="live_mem.audit"), patches2, storage_patch2:
            res = await long_ingest(
                space_id=_SPACE,
                documents=[{"source_path": "memory-bank/progress.md", "content": "prog"}],
                mode="dry-run",
                include_volatile=True,
            )
    finally:
        reset2()
    assert res["status"] == "error"
    assert _optin_lines() == []


# =============================================================================
# (6) NEGATIVE-IMPORT — no long_ingest/long_query code path imports the commit
# path. AST over tools/graph.py + long_engine.py + graph_bridge.py.
# =============================================================================

import live_mem.core.graph_bridge as _gb_mod  # noqa: E402

_GRAPH_TOOLS_SRC = Path(inspect.getsourcefile(graph_tools)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_GRAPH_TOOLS_TREE = ast.parse(_GRAPH_TOOLS_SRC)

_LONG_SRC = Path(inspect.getsourcefile(LongEngine)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_LONG_TREE = ast.parse(_LONG_SRC)

_BRIDGE_SRC = Path(inspect.getsourcefile(_gb_mod)).read_text(encoding="utf-8")  # type: ignore[arg-type]
_BRIDGE_TREE = ast.parse(_BRIDGE_SRC)


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


# Commit-path modules the long/graph tool + engine + bridge path must never
# import. ``hivemind`` is the strict blanket marker (its __init__ eagerly loads
# the commit-state module), matching test_long_engine.py.
_COMMIT_MODULE_MARKERS = (
    "consolidator",
    "hivemind",
    "write_sink",
    "engines.mid",
    "mid_engine",
)


@pytest.mark.parametrize(
    "label, tree",
    [
        ("tools/graph.py", _GRAPH_TOOLS_TREE),
        ("long_engine.py", _LONG_TREE),
        ("graph_bridge.py", _BRIDGE_TREE),
    ],
)
def test_long_path_imports_no_commit_module(label: str, tree: ast.AST) -> None:
    """No module on the long_ingest / long_query code path imports a commit-path
    module (consolidator / hivemind / write_sink / mid engine)."""
    for imp in _imports_of(tree):
        low = imp.lower()
        for marker in _COMMIT_MODULE_MARKERS:
            assert marker not in low, f"{label}: forbidden commit-path import: {imp}"


@pytest.mark.parametrize(
    "label, tree",
    [
        ("tools/graph.py", _GRAPH_TOOLS_TREE),
        ("long_engine.py", _LONG_TREE),
        ("graph_bridge.py", _BRIDGE_TREE),
    ],
)
def test_long_path_has_no_commit_authority_symbols(label: str, tree: ast.AST) -> None:
    """No commit-authority name/attr (assert_commit_allowed) appears anywhere on
    the long path — the AST ignores docstring/comment prose that merely NAMES the
    forbidden symbol as forbidden."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "assert_commit_allowed", label
        if isinstance(node, ast.Attribute):
            assert node.attr != "assert_commit_allowed", label


def test_plan_ingest_is_async_and_adds_no_apply_authority() -> None:
    """P4-7 contributes a planner, never an apply seam.

    The complete ten-member public async surface is owned by
    test_long_engine.py; this phase test asserts only its unique delta.
    """
    assert inspect.iscoroutinefunction(LongEngine.plan_ingest)
    assert not hasattr(LongEngine, "apply")


# =============================================================================
# Determinism — same inputs, same plan + same recorded calls (no clock/uuid).
# =============================================================================


async def test_check_remote_is_deterministic() -> None:
    async def run() -> tuple[dict, list[str]]:
        storage = FakeStorage()
        await storage.put_json(f"{_SPACE}/_meta.json", _meta_connected())
        responses = {
            "document_list": _doc_list_response(
                [{"source_path": _DOC_A["source_path"], "sha256": _sha256("stale")}]
            )
        }
        patches, storage_patch, factory = _wire(storage, responses=responses)
        long_ingest = _tool_fn("long_ingest")
        reset = _set_token(_token("writer", ["read", "write"]))
        try:
            with patches, storage_patch:
                result = await long_ingest(
                    space_id=_SPACE,
                    documents=[
                        {"source_path": _DOC_A["source_path"], "content": _DOC_A["content"]},
                        {"source_path": _DOC_B["source_path"], "content": _DOC_B["content"]},
                    ],
                    mode="check-remote",
                )
        finally:
            reset()
        names = [n for inst in factory.instances for n in inst.tool_names()]
        return result, names

    r1, names1 = await run()
    r2, names2 = await run()
    assert r1 == r2
    assert names1 == names2
