# -*- coding: utf-8 -*-
"""
P4-2 (EPIC #6) — long_* MCP tool surface + graph_* compatibility aliases.

The long-tier surface is already wired by P1 (#25): the four `graph_*` tools are
registered with `long_*` aliases bound to the IDENTICAL implementation
(`tools/aliases.py` ALIAS_MAP, ADR-0002/0005). P4-2 LOCKS the long-tier surface
contract specifically (the full alias-mechanism + 13-alias parity lives in
`test_tool_aliases_smoke.py` / `test_mcp_tool_surface.py`):

- the exact four `graph_* → long_*` mappings;
- each `long_*` is the identical impl as its `graph_*` twin (no rename dropped a
  historical name);
- the spec documents the surface, the non-authoritative boundary, and reserves
  the net-new `long_ingest` / `long_query` tools for P4-7.

Pure in-process FastMCP — no S3 / network / LLM.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from live_mem.tools import register_all_tools
from live_mem.tools.aliases import ALIAS_MAP

_SPEC = Path(__file__).resolve().parents[1] / "DESIGN" / "live-mem" / "MCP_TOOLS_SPEC.md"

# The frozen long-tier surface: graph_* (source) → long_* (alias), all four.
_LONG_TIER = {
    "graph_connect": "long_connect",
    "graph_push": "long_push",
    "graph_status": "long_status",
    "graph_disconnect": "long_disconnect",
}


def _build_tools():
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp._tool_manager._tools


def test_alias_map_has_exactly_the_four_long_tier_mappings():
    long_entries = {h: c for h, c in ALIAS_MAP.items() if c.startswith("long_")}
    assert long_entries == _LONG_TIER, (
        "the long-tier graph_*→long_* alias surface drifted from the frozen four"
    )


def test_both_name_sets_registered_with_identical_impl():
    tools = _build_tools()
    for graph_name, long_name in _LONG_TIER.items():
        assert graph_name in tools, f"historical {graph_name} was dropped (no rename allowed)"
        assert long_name in tools, f"alias {long_name} missing"
        # Identical handler — the alias is a second registration, not a wrapper.
        assert tools[long_name].fn is tools[graph_name].fn, (
            f"{long_name} must dispatch to the identical impl as {graph_name}"
        )


def test_spec_documents_long_surface_and_reserves_new_tools():
    if not _SPEC.exists():
        import pytest

        pytest.skip("DESIGN/live-mem/MCP_TOOLS_SPEC.md is private-only (absent from the public release tree)")
    spec = _SPEC.read_text(encoding="utf-8")
    for long_name in _LONG_TIER.values():
        assert long_name in spec, f"{long_name} undocumented in MCP_TOOLS_SPEC"
    # The net-new tools are reserved (documented) for P4-7.
    assert "long_ingest" in spec and "long_query" in spec, (
        "MCP_TOOLS_SPEC must reserve the net-new long_ingest / long_query tools"
    )
    # The non-authoritative boundary is documented for the long tier.
    assert "non-authoritative" in spec.lower() or "not an authoritative" in spec.lower()
