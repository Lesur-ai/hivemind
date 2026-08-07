# -*- coding: utf-8 -*-
"""
Tier-canonical alias registration (P1 — issue #22).

Registers the canonical ``short_*`` / ``mid_*`` / ``long_*`` aliases as a
SECOND registration bound to the IDENTICAL handler object as their historical
source. No copy, no wrapper, no behavior change: each alias reuses the source's
identical ``fn`` and re-passes its ``title`` / ``description`` / ``annotations``
/ ``icons`` / ``meta``. Because the input and output schema are re-derived from
that same ``fn``, every field a client sees via ``list_tools`` matches the
source — so the permission gate, the fixed note-category set, the destructive
contracts and the long-tier non-authoritative posture are inherited by identity.

Normative mapping: ``DESIGN/hivemind/TOOL_MAPPING.md`` (governed by ADR-0002
grammar + ADR-0005 alias lifecycle). The complete expected registered surface
and its counts have one test authority in ``tests/fixtures/tool_surface.json``;
this module owns only the executable alias mapping and registration mechanism.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

# Single-owner, string-keyed mapping ``historical_name -> canonical tier name``.
# The handlers are nested closures inside each module's ``register(mcp)`` and are
# NOT importable at module scope, so sources are resolved by NAME from the tool
# manager after every tier register() has run — no closure references needed.
ALIAS_MAP: dict[str, str] = {
    # short tier — live_* -> short_*
    "live_note": "short_note",
    "live_read": "short_read",
    "live_search": "short_search",
    # mid tier — bank_* -> mid_*; bank ops without a tier twin stay direct-only
    "bank_read": "mid_read",
    "bank_read_all": "mid_read_all",
    "bank_list": "mid_list",
    "bank_write": "mid_write",
    "bank_consolidate": "mid_consolidate",
    "bank_delete": "mid_delete",
    # long tier — graph_* -> long_* (non-authoritative; protocol-derived only)
    "graph_connect": "long_connect",
    "graph_push": "long_push",
    "graph_status": "long_status",
    "graph_disconnect": "long_disconnect",
}


def register_tier_aliases(
    mcp: FastMCP, alias_map: dict[str, str] | None = None
) -> int:
    """
    Register the tier-canonical aliases over already-registered tools. Fail-closed.

    MUST be called AFTER every tier ``register(mcp)`` so the historical sources
    already exist in the tool manager. Defaults to the frozen ``ALIAS_MAP``;
    accepts an explicit map so the negative paths are testable through this same
    public seam.

    Fails closed with ``RuntimeError`` — never a silent skip — on:
    - an intra-map duplicate canonical name (two historicals targeting one alias),
    - a missing source tool,
    - a source tool with no callable (``fn is None``),
    - a canonical name that already exists (alias or historical) — no overwrite.

    Returns the number of aliases registered from the selected mapping.
    """
    alias_map = ALIAS_MAP if alias_map is None else alias_map

    # Up-front integrity: no two historicals may target the same canonical name.
    canonicals = list(alias_map.values())
    if len(set(canonicals)) != len(canonicals):
        dupes = sorted({c for c in canonicals if canonicals.count(c) > 1})
        raise RuntimeError(
            f"ALIAS_MAP has duplicate canonical names {dupes}; refusing to "
            f"register (fail-closed)"
        )

    tools = mcp._tool_manager._tools
    registered = 0
    for historical, canonical in alias_map.items():
        source = tools.get(historical)
        if source is None:
            raise RuntimeError(
                f"alias source tool {historical!r} is not registered; cannot "
                f"create alias {canonical!r} (fail-closed)"
            )
        if getattr(source, "fn", None) is None:
            raise RuntimeError(
                f"alias source tool {historical!r} has no callable; cannot "
                f"create alias {canonical!r} (fail-closed)"
            )
        if canonical in tools:
            raise RuntimeError(
                f"name {canonical!r} is already registered (collides with an "
                f"existing tool); refusing to overwrite (fail-closed)"
            )
        mcp.add_tool(
            source.fn,
            name=canonical,
            title=source.title,
            description=source.description,
            annotations=source.annotations,
            icons=source.icons,
            meta=source.meta,
        )
        registered += 1
    return registered
