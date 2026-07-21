# -*- coding: utf-8 -*-
"""
Focused smoke test for the P1 tier-alias registration mechanism (#22).

The full registration-surface enumeration (48 direct names, 33 bank-op /
cross-cutting no-alias negative assertion, plus 2 direct net-new long tools and
auth-deny parity) lives in P1-6
(``tests/test_mcp_tool_surface.py``). This module pins only the *mechanism*:

- ``register_all_tools`` wires the alias pass in and totals 61 (48 + 13);
- each alias is the IDENTICAL callable as its source, with full metadata parity
  (input schema, title, description, annotations, icons, meta);
- the mechanism fails closed on a duplicate canonical, a missing source, or a
  colliding canonical name.

Pure in-process FastMCP — no S3, no network, no LLM.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.tools import register_all_tools
from live_mem.tools.aliases import ALIAS_MAP, register_tier_aliases


EXPECTED_HISTORICAL = 48  # LM2-11 adds token_create + space_invite_token
EXPECTED_ALIASES = 13
EXPECTED_TOTAL = EXPECTED_HISTORICAL + EXPECTED_ALIASES  # 61


def _build():
    mcp = FastMCP(name="test")
    total = register_all_tools(mcp)
    return mcp, total


def test_alias_map_is_the_frozen_3_6_4():
    short = [c for c in ALIAS_MAP.values() if c.startswith("short_")]
    mid = [c for c in ALIAS_MAP.values() if c.startswith("mid_")]
    long_ = [c for c in ALIAS_MAP.values() if c.startswith("long_")]
    assert (len(short), len(mid), len(long_)) == (3, 6, 4)
    assert len(ALIAS_MAP) == EXPECTED_ALIASES
    # No duplicate sources or duplicate canonical targets (copy-paste guard).
    assert len(set(ALIAS_MAP)) == EXPECTED_ALIASES
    assert len(set(ALIAS_MAP.values())) == EXPECTED_ALIASES


def test_register_all_tools_totals_61():
    mcp, total = _build()
    # The alias pass owns the +13 (48 + 13); long_ingest / long_query are net-new
    # long_* tools and admin_audit_recent is cross-cutting, all with no alias.
    assert total == EXPECTED_TOTAL
    assert len(mcp._tool_manager._tools) == EXPECTED_TOTAL


def test_aliases_reuse_identical_handler_and_metadata():
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    for historical, canonical in ALIAS_MAP.items():
        assert historical in tools, f"source {historical} missing"
        assert canonical in tools, f"alias {canonical} missing"
        src, alias = tools[historical], tools[canonical]
        # Identical handler object — not a wrapper or copy.
        assert alias.fn is src.fn, f"{canonical} is not the identical fn as {historical}"
        # Full metadata parity (locks the contract against future SDK/source drift).
        assert alias.description == src.description, f"{canonical} description drift"
        assert alias.annotations == src.annotations, f"{canonical} annotations drift"
        assert alias.parameters == src.parameters, f"{canonical} input-schema drift"
        assert alias.title == src.title, f"{canonical} title drift"
        assert alias.icons == src.icons, f"{canonical} icons drift"
        assert alias.meta == src.meta, f"{canonical} meta drift"


def test_historical_names_remain_registered():
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    for historical in ALIAS_MAP:
        assert historical in tools


def test_fail_closed_on_missing_source():
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    with pytest.raises(RuntimeError, match="not registered"):
        register_tier_aliases(mcp, {"does_not_exist_tool": "zzz_alias"})


def test_fail_closed_on_duplicate_canonical():
    mcp = FastMCP(name="test")
    register_all_tools(mcp)  # short_note already registered by the alias pass
    with pytest.raises(RuntimeError, match="already registered"):
        register_tier_aliases(mcp, {"live_note": "short_note"})


def test_fail_closed_on_intra_map_duplicate_canonical():
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    with pytest.raises(RuntimeError, match="duplicate canonical"):
        register_tier_aliases(
            mcp, {"live_note": "x_dup", "live_read": "x_dup"}
        )
