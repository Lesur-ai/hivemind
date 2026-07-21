# -*- coding: utf-8 -*-
"""
Core Services pour MCP Memory.

- GraphService : Client Neo4j + requêtes Cypher
- StorageService : Client S3 (boto3)
- ExtractorService : Extraction via LLMaaS
"""

# P7-9 LOCAL MODIFICATION (Hivemind, #135 — see THIRD_PARTY_NOTICES.md): the
# heavy re-exports are made LAZY (PEP 562 __getattr__), mirroring the P7-4
# treatment of `auth/__init__.py`, so importing the import-light submodule
# `core.storage` (boto3 only) does NOT pull `core.graph` (-> neo4j) or
# `core.extractor` (-> openai). The public attribute API
# (`from mcp_memory.core import StorageService`, etc.) is preserved and the
# runtime (server.py) imports submodules directly, so this is
# behaviour-preserving; it makes the storage signature-mode regression tests
# runnable from the Hivemind venv.

__all__ = ["GraphService", "StorageService", "ExtractorService"]

_LAZY = {
    "GraphService": (".graph", "GraphService"),
    "StorageService": (".storage", "StorageService"),
    "ExtractorService": (".extractor", "ExtractorService"),
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
