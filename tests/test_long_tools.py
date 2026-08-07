# -*- coding: utf-8 -*-
"""
P4-2 (EPIC #6) — long_* MCP tool surface + graph_* compatibility aliases.

The long-tier surface is already wired by P1 (#25): the `graph_*` tools are
registered with `long_*` aliases bound to the identical implementation
(`tools/aliases.py` ALIAS_MAP, ADR-0002/0005). The exhaustive mapping,
callability, identity, and metadata contract has one authority in
`test_mcp_tool_surface.py`; this phase test keeps only its distinct
documentation invariant:

- the spec documents every canonical long alias, the non-authoritative
  boundary, and the direct `long_ingest` / `long_query` tools.

Pure in-process FastMCP — no S3 / network / LLM.
"""

from __future__ import annotations

from pathlib import Path

from live_mem.tools.aliases import ALIAS_MAP

_SPEC = Path(__file__).resolve().parents[1] / "DESIGN" / "live-mem" / "MCP_TOOLS_SPEC.md"

def test_spec_documents_canonical_long_aliases_and_direct_tools():
    if not _SPEC.exists():
        import pytest

        pytest.skip("DESIGN/live-mem/MCP_TOOLS_SPEC.md is private-only (absent from the public release tree)")
    spec = _SPEC.read_text(encoding="utf-8")
    long_aliases = {
        canonical for canonical in ALIAS_MAP.values() if canonical.startswith("long_")
    }
    for long_name in long_aliases:
        assert long_name in spec, f"{long_name} undocumented in MCP_TOOLS_SPEC"
    # The direct tools are documented separately from the alias mapping.
    assert "long_ingest" in spec and "long_query" in spec, (
        "MCP_TOOLS_SPEC must document the direct long_ingest / long_query tools"
    )
    # The non-authoritative boundary is documented for the long tier.
    assert "non-authoritative" in spec.lower() or "not an authoritative" in spec.lower()
