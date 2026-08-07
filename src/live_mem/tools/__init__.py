# -*- coding: utf-8 -*-
"""
Package tools — Enregistrement des outils MCP par catégorie.

Chaque module (system, space, live, bank, graph, backup, admin, access) expose
une fonction `register(mcp)` qui déclare ses outils via @mcp.tool().

Cette architecture maintient chaque fichier sous 500 lignes
tout en gardant une organisation claire par domaine fonctionnel.

Usage dans server.py :
    from .tools import register_all_tools
    register_all_tools(mcp)
"""

import json as _json
import logging

from mcp.server.fastmcp import FastMCP

_log = logging.getLogger("live_mem.tool_proxy")

# Reference to the FastMCP instance, set during register_all_tools()
_mcp_ref: FastMCP | None = None


def register_all_tools(mcp: FastMCP) -> int:
    """
    Enregistre tous les outils MCP depuis les modules de catégorie.

    Args:
        mcp: Instance FastMCP sur laquelle enregistrer les outils

    Returns:
        Nombre total d'outils enregistrés
    """
    global _mcp_ref

    # Importer et enregistrer chaque catégorie
    from .system import register as register_system
    from .space import register as register_space
    from .live import register as register_live
    from .bank import register as register_bank
    from .graph import register as register_graph
    from .backup import register as register_backup
    from .admin import register as register_admin
    from .access import register as register_access

    count = 0
    count += register_system(mcp)
    count += register_space(mcp)
    count += register_live(mcp)
    count += register_bank(mcp)
    count += register_graph(mcp)
    count += register_backup(mcp)
    count += register_admin(mcp)
    count += register_access(mcp)

    # Tier-canonical aliases (P1, #22): short_*/mid_*/long_* re-registrations of
    # the identical handlers. MUST run AFTER every tier register() so the
    # historical sources exist. Owns and returns its own alias count; tier
    # register() return literals are left untouched.
    from .aliases import register_tier_aliases

    count += register_tier_aliases(mcp)

    # P10-1: the exposure metadata is a total, fail-closed startup contract.
    # Validate only after every canonical and historical name is registered so
    # missing entries, overwrites, collisions, and alias drift cannot serve.
    from .exposure import validate_tool_exposure_registry

    validate_tool_exposure_registry(
        mcp,
        declared_registration_count=count,
    )

    # Store reference for the /api/tool proxy
    _mcp_ref = mcp

    return count


async def call_tool_direct(name: str, arguments: dict) -> dict:
    """
    Call an MCP tool directly, bypassing the MCP protocol layer.

    Used by the /api/tool REST endpoint to let the admin web UI
    invoke any MCP tool. Auth context (current_token_info) is
    already set by AuthMiddleware before this is called.

    Args:
        name: Tool name (e.g. "space_list")
        arguments: Tool arguments dict

    Returns:
        Tool result as a dict
    """
    if _mcp_ref is None:
        return {"status": "error", "message": "Server not initialized"}

    tool_mgr = _mcp_ref._tool_manager
    tools_dict = getattr(tool_mgr, "_tools", {})

    if name not in tools_dict:
        return {"status": "error", "message": f"Unknown tool: {name}"}

    tool_obj = tools_dict[name]

    # Find the callable function on the internal tool object
    fn = None
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        fn = getattr(tool_obj, attr, None)
        if fn and callable(fn):
            break

    if fn is None:
        _log.error(
            "Tool %s: no callable found. attrs=%s",
            name,
            [a for a in dir(tool_obj) if not a.startswith("__")],
        )
        return {"status": "error", "message": f"Tool {name}: internal error"}

    try:
        result = await fn(**arguments)
        return result if isinstance(result, dict) else {"status": "ok", "data": result}
    except Exception as e:
        # HM-09 fix : router via safe_error() au lieu de renvoyer str(e) brut.
        # call_tool_direct RETOURNE un dict (ne re-lève pas), donc le wrapper
        # safe_error du handler /api/tool (middleware.py) n'était jamais atteint
        # et la trace d'exception (potentiellement botocore : endpoint S3, etc.)
        # fuyait au client quel que soit MCP_SERVER_DEBUG. Aligne le chemin proxy
        # sur la posture no-leak ADM-02 / VULN-27 du reste de la surface d'outils.
        _log.exception("Tool %s failed", name)
        from ..auth.context import safe_error

        return safe_error(e, name)
