# -*- coding: utf-8 -*-
"""
P3-7 (issue #56) — STATIC source guard: durable mutations route through the
engine registry / WriteSink, never through an ad-hoc get_storage()/core-service
singleton.

This is a deterministic, OFFLINE source-inspection gate (no live_mem behaviour,
no FakeStorage, no boto3 / network / S3 / LLM). It pins the P3-7 contract so a
regression goes RED:

  * Every DURABLE-MUTATION tool path in tools/live.py, tools/bank.py,
    tools/graph.py acquires the per-space route gate
    (``get_engine_registry().resolve_sink(...)`` directly, or via
    ``short_engine`` / ``mid_engine`` which resolve it internally) BEFORE the
    durable op, and performs the durable op THROUGH the resolved ``sink`` (or a
    gated engine method) — NOT through a bare ``storage.put`` / ``storage.delete``
    / ``storage.delete_many`` nor an un-gated core singleton.

  * READS stay on ``StorageService`` at the tool layer and are NOT forced
    through the registry (the P3-6 "bank read deferred to P3-7" note resolved as
    reads-stay): ``bank_read`` / ``bank_read_all`` / ``bank_list`` /
    ``bank_consolidation_status`` / ``bank_consolidation_queues`` /
    ``bank_stale_spaces`` / ``live_read`` / ``live_search`` keep their
    ``get_storage()`` / queue read methods.

  * graph_* tools are downstream-derived (ADR-0010): they delegate to
    ``long_engine()`` and have NO ``resolve_sink`` gate (LongEngine takes no
    WriteSink). The ``_validate_gm_url`` SSRF check stays in the tool layer.

Mechanics: we parse each tool module with ``ast``, isolate each tool function's
own source span (excluding nested helpers), and assert substring invariants on
that span. AST gives a precise per-function source slice; substring checks keep
the assertions readable and resilient to formatting.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = _REPO_ROOT / "src" / "live_mem" / "tools"


def _module_source(name: str) -> str:
    return (_TOOLS_DIR / name).read_text(encoding="utf-8")


def _strip_comments_and_strings(src: str) -> str:
    """Return ``src`` with all comments and string-literal CONTENT removed.

    The guard asserts on EXECUTABLE code only — docstrings and explanatory
    comments (which legitimately mention ``resolve_sink`` / ``get_storage`` while
    documenting the routing contract) must NOT trip a substring check. We
    tokenize and drop COMMENT tokens and blank-out STRING tokens, preserving
    everything else (and source positions, so call expressions stay intact).
    """
    out: list[str] = []
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok_type, tok_str, _start, _end, _line in toks:
        if tok_type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if tok_type in (tokenize.INDENT, tokenize.DEDENT):
            continue
        if tok_type == tokenize.STRING:
            out.append('""')  # collapse literal content
            continue
        out.append(tok_str)
    # Whitespace-collapsed token stream: assertions use whitespace-free needles
    # (see _code_contains), so `resolve_sink(space_id)` matches regardless of the
    # tokenizer's inter-token spacing.
    return "".join(out)


def _tool_func_source(module_src: str, func_name: str) -> str:
    """Return the EXECUTABLE source of the tool coroutine ``func_name``.

    Tools are nested ``async def`` inside ``register(mcp)``; we walk the AST,
    slice the function's own line span so a sibling tool's body never leaks into
    the assertion, then strip comments + string content so only real code is
    matched.
    """
    tree = ast.parse(module_src)
    lines = module_src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno  # 1-based inclusive -> slice end
            span = "\n".join(lines[start:end])
            return _strip_comments_and_strings(span)
    raise AssertionError(f"tool function {func_name!r} not found in module")


def _has(code: str, needle: str) -> bool:
    """Whitespace-insensitive substring check over the collapsed token stream."""
    return "".join(needle.split()) in code


# =============================================================================
# Durable-mutation tools: gate present + durable op routed through the sink.
# =============================================================================


def test_live_note_routes_through_registry_not_adhoc_storage() -> None:
    src = _tool_func_source(_module_source("live.py"), "live_note")
    # SINGLE-resolution gate (codex PR #64): build the engine (resolves the route
    # once) and gate on its OWN resolved sink — NOT a separate resolve_sink call
    # whose verdict could differ from the engine's (that double-resolve let an
    # observed STAGED fall through to the inert legacy write).
    assert _has(src, "get_engine_registry()")
    assert _has(src, "short_engine(space_id)")
    assert _has(src, "engine.write_sink")
    assert not _has(src, "resolve_sink(space_id)")  # no double-resolution
    # The durable write goes through the engine (DIRECT_LOCAL) or the typed
    # staged refusal — never a bare get_storage().put for the live note.
    assert not _has(src, ".put(")  # no ad-hoc tool-layer storage.put for the note
    assert not _has(src, "get_storage()")


def test_bank_write_durable_ops_route_through_sink() -> None:
    src = _tool_func_source(_module_source("bank.py"), "bank_write")
    assert _has(src, "get_engine_registry()")
    assert _has(src, "resolve_sink(space_id)")
    # Durable ops THROUGH the resolved sink.
    assert _has(src, "sink.put(")
    assert _has(src, "sink.delete(")
    # No ad-hoc tool-layer durable mutation on the raw storage handle.
    assert not _has(src, "storage.put(")
    assert not _has(src, "storage.delete(")
    assert not _has(src, "storage.delete_many(")
    # Reads still allowed on storage (exists/list_objects).
    assert _has(src, "storage.exists(") or _has(src, "storage.list_objects(")


def test_bank_repair_apply_branch_routes_through_sink() -> None:
    src = _tool_func_source(_module_source("bank.py"), "bank_repair")
    assert _has(src, "resolve_sink(space_id)")
    assert _has(src, "sink.put(")
    assert _has(src, "sink.delete(")
    # The apply branch must not put/delete on the raw storage handle. The read
    # (storage.get of the original key) is allowed and expected.
    assert not _has(src, "storage.put(")
    assert not _has(src, "storage.delete(")
    assert not _has(src, "storage.delete_many(")
    assert _has(src, "storage.get(")  # read stays


def test_bank_delete_routes_delete_many_through_sink() -> None:
    src = _tool_func_source(_module_source("bank.py"), "bank_delete")
    assert _has(src, "resolve_sink(space_id)")
    assert _has(src, "sink.delete_many(")
    assert not _has(src, "storage.delete_many(")
    assert not _has(src, "storage.delete(")
    # Scan read stays on storage.
    assert _has(src, "storage.list_objects(")


def test_bank_compact_apply_gated_dry_run_read_stays() -> None:
    src = _tool_func_source(_module_source("bank.py"), "bank_compact")
    # Apply branch: initial tool-gate resolution on the engine's OWN sink
    # (codex PR #64), then delegation to the mid engine. The compactor's later
    # final transaction-boundary route check remains below this tool layer.
    assert _has(src, "mid_engine(space_id)")
    assert _has(src, "engine.write_sink")
    assert not _has(src, "resolve_sink(space_id)")  # no double-resolution
    assert _has(src, "compact_bank(space_id, dry_run=False)")
    # The dry_run (read-only) scan stays on the consolidator singleton, NOT
    # routed through resolve_sink.
    assert _has(src, "get_consolidator().compact_bank(space_id, dry_run=True)")
    # Lock + conflict-check stay in the tool layer (single-writer-per-space).
    assert _has(src, "get_lock_manager().consolidation(space_id)")
    assert _has(src, "lock.locked()")


def test_bank_consolidate_gates_before_enqueue() -> None:
    """The fail-closed-routing fix: bank_consolidate resolves the route BEFORE
    enqueue so a Hivemind/unsafe/corrupt space never queues a worker (whose
    direct get_storage writes would bypass the seam)."""
    src = _tool_func_source(_module_source("bank.py"), "bank_consolidate")
    assert _has(src, "resolve_sink(space_id)")
    # The gate raises the staged refusal before enqueue for non-DIRECT_LOCAL.
    assert _has(src, "DirectLocalWriteSink")
    assert _has(src, "StagedWriteNotImplemented")
    # The gate (resolve_sink) appears BEFORE the enqueue in source order.
    gate_idx = src.index("resolve_sink(space_id)")
    enqueue_idx = src.index(".enqueue(")
    assert gate_idx < enqueue_idx, "route gate must precede enqueue"


# =============================================================================
# graph_* tools — long_engine() delegation, NO resolve_sink gate (ADR-0010).
# =============================================================================


@pytest.mark.parametrize(
    "func_name",
    ["graph_connect", "graph_push", "graph_status", "graph_disconnect"],
)
def test_graph_tools_use_long_engine_no_resolve_sink(func_name: str) -> None:
    src = _tool_func_source(_module_source("graph.py"), func_name)
    assert _has(src, "long_engine()")
    # Downstream-derived: never a WriteSink path.
    assert not _has(src, "resolve_sink")
    assert not _has(src, "short_engine")
    assert not _has(src, "mid_engine")
    assert not _has(src, "sink.")


def test_graph_connect_keeps_ssrf_check_in_tool() -> None:
    src = _tool_func_source(_module_source("graph.py"), "graph_connect")
    assert _has(src, "_validate_gm_url(url)")


# =============================================================================
# Reads stay on StorageService — NOT routed through the registry (reads-stay).
# =============================================================================


@pytest.mark.parametrize(
    "func_name",
    [
        "bank_read",
        "bank_read_all",
        "bank_list",
        "bank_consolidation_status",
        "bank_consolidation_queues",
        "bank_stale_spaces",
    ],
)
def test_bank_read_tools_are_not_gated(func_name: str) -> None:
    src = _tool_func_source(_module_source("bank.py"), func_name)
    # Read tools must NOT acquire a route gate (reads-stay invariant).
    assert not _has(src, "resolve_sink")
    assert not _has(src, "mid_engine")
    assert not _has(src, "short_engine")


@pytest.mark.parametrize("func_name", ["live_read", "live_search"])
def test_live_read_tools_are_not_gated(func_name: str) -> None:
    src = _tool_func_source(_module_source("live.py"), func_name)
    assert not _has(src, "resolve_sink")
    # Reads stay on the live service / storage seam.
    assert _has(src, "get_live_service()")
