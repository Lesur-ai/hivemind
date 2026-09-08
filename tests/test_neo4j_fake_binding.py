"""The fake neo4j binding must hold whatever test module imported the graph first."""

from unittest.mock import MagicMock

from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
from tests.fakes.neo4j_fakes import FakeQuery, bind_fake_neo4j


def test_bind_fake_neo4j_rebinds_a_graph_module_imported_earlier(monkeypatch):
    apply_graph_memory_baseline_env(monkeypatch)
    # An earlier test module (the document-catalog tests) imported the graph
    # module with MagicMock stand-ins: this is the state the reindex tests met.
    first_query = MagicMock()
    graph_module = bind_fake_neo4j(monkeypatch, query=first_query)
    assert graph_module.Query is first_query

    # A later test asking for the str-based query must get it on the module,
    # not only in sys.modules where the already-imported module never looks.
    rebound = bind_fake_neo4j(monkeypatch)
    assert rebound is graph_module
    assert graph_module.Query is FakeQuery
    query = graph_module.Query("MATCH (n) RETURN n", timeout=3)
    assert isinstance(query, str) and query.timeout == 3
    assert graph_module.AsyncGraphDatabase is object
    assert issubclass(graph_module.ServiceUnavailable, Exception)
