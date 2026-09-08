"""Per-test fake ``neo4j`` binding for the embedded Graph Memory service.

The root Hivemind test environment does not install the ``neo4j`` driver, so
several test modules install a fake ``neo4j`` module before importing
``mcp_memory.core.graph``. That module binds ``Query``, ``AsyncGraphDatabase``,
``AsyncDriver``, ``AsyncSession``, ``ServiceUnavailable`` and ``AuthError`` at
import time: a fake placed only in ``sys.modules`` is seen by the first importer
of the session, and every later test silently inherits that first fake.
Tests can then pass in isolation but fail or hang when the suite's import
order changes.

``bind_fake_neo4j`` makes the binding per test: it installs the fake module
(so a first import succeeds), imports the graph module, then rebinds the six
names on the module itself under ``monkeypatch``, which restores them at
teardown. Import order no longer matters.
"""

from __future__ import annotations

import sys
import types


class FakeQuery(str):
    """``neo4j.Query`` stand-in: the Cypher text with its ``timeout`` attached."""

    def __new__(cls, text: str, *, timeout: float | None = None):
        value = str.__new__(cls, text)
        value.timeout = timeout
        return value


_BOUND_NAMES = (
    "AsyncGraphDatabase",
    "AsyncDriver",
    "AsyncSession",
    "Query",
    "ServiceUnavailable",
    "AuthError",
)


def bind_fake_neo4j(
    monkeypatch,
    *,
    graph_database=object,
    driver=object,
    session=object,
    query=FakeQuery,
):
    """Install a fake ``neo4j`` for this test and bind it on the graph module.

    Returns the ``mcp_memory.core.graph`` module with the fake names bound.
    """

    neo4j = types.ModuleType("neo4j")
    neo4j.AsyncGraphDatabase = graph_database
    neo4j.AsyncDriver = driver
    neo4j.AsyncSession = session
    neo4j.Query = query
    exceptions = types.ModuleType("neo4j.exceptions")
    exceptions.ServiceUnavailable = type("ServiceUnavailable", (Exception,), {})
    exceptions.AuthError = type("AuthError", (Exception,), {})
    neo4j.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "neo4j", neo4j)
    monkeypatch.setitem(sys.modules, "neo4j.exceptions", exceptions)

    from mcp_memory.core import graph as graph_module

    bound = {
        "AsyncGraphDatabase": graph_database,
        "AsyncDriver": driver,
        "AsyncSession": session,
        "Query": query,
        "ServiceUnavailable": exceptions.ServiceUnavailable,
        "AuthError": exceptions.AuthError,
    }
    for name in _BOUND_NAMES:
        monkeypatch.setattr(graph_module, name, bound[name])
    return graph_module
