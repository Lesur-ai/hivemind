# -*- coding: utf-8 -*-
"""
Module d'authentification pour MCP Memory.

- TokenManager : Gestion des tokens clients (CRUD)
- Middleware : Vérification des tokens Bearer
"""

# P7-4 LOCAL MODIFICATION (Hivemind, ADR-0019 — see THIRD_PARTY_NOTICES.md):
# the heavy re-exports are made LAZY (PEP 562 __getattr__) so importing the
# import-light submodules (`auth.context`, `auth.s3_token_validator`) does NOT
# pull `token_manager` (-> neo4j) or `middleware`. The public attribute API
# (`from mcp_memory.auth import AuthMiddleware`, etc.) is preserved; the runtime
# (server.py) imports the submodules directly, so this is behaviour-preserving.

__all__ = [
    "TokenManager", "get_token_manager",
    "AuthMiddleware", "LoggingMiddleware", "StaticFilesMiddleware",
]

_LAZY = {
    "TokenManager": (".token_manager", "TokenManager"),
    "get_token_manager": (".token_manager", "get_token_manager"),
    "AuthMiddleware": (".middleware", "AuthMiddleware"),
    "LoggingMiddleware": (".middleware", "LoggingMiddleware"),
    "StaticFilesMiddleware": (".middleware", "StaticFilesMiddleware"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    return getattr(module, target[1])


def __dir__():
    return sorted(__all__)
