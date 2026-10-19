"""Graph reads the maintenance reindex uses to rebuild passage annotations (T05 / #922).

Query shape and bounds are asserted on a recording session; the Cypher itself
is qualified against a real Neo4j by the opt-in case in test_long_temporal_graph.
"""
import hashlib
import json

import pytest

from tests.test_p13_reindex_sources import _SessionContext, _graph_service_class

MEMORY, DOC = "memory-a", "doc-a"
TEXT = "".join(f"Mira adopted pet {i}.\n" for i in range(6))


class Pages:
    """A session whose ``run`` returns one prepared page of rows per call."""

    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    async def run(self, query, **params):
        self.calls.append((query, params))
        rows = self.pages.pop(0) if self.pages else []

        class Result:
            def __aiter__(self):
                async def iterate():
                    for row in rows:
                        yield row
                return iterate()

        return Result()


def graph(monkeypatch, session):
    cls = _graph_service_class(monkeypatch)
    service = object.__new__(cls)
    service.session = lambda: _SessionContext(session)
    return service


def stored(index, *, doc=DOC, memory=MEMORY):
    from mcp_memory.core import temporal
    ref = temporal.SourceRef(memory_id=memory, doc_id=doc, sha256="a" * 64,
                             text_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
    start = TEXT.index(f"Mira adopted pet {index}.")
    quote = f"Mira adopted pet {index}."
    unknown = dict(status="unknown", expression=None, expression_evidence=[], precision="unknown",
                   normalized=None, anchor=None, anchor_candidates=[])
    row = temporal.Occurrence.model_validate(dict(
        temporal_schema_version=1, fact=dict(from_entity="Mira", to_entity=f"Pet{index}", type="ADOPTED"),
        fact_evidence=[dict(start=start, end=start + len(quote), quote=quote)],
        scope=dict(start=0, end=len(TEXT)), source_time=unknown, event_time=unknown, valid_time=None))
    row = row.model_copy(update={"source_ref": ref, "occurrence_id": temporal.occurrence_identity(row, ref)})
    return {"id": row.occurrence_id, "doc_id": doc, "record_json": temporal.canonical_record(row)}


def ordered(count, **kwargs):
    return sorted((stored(i, **kwargs) for i in range(count)), key=lambda r: r["id"])


async def test_text_records_are_read_by_document_with_one_bounded_query(monkeypatch):
    session = Pages([[{"document_id": "doc-a", "record": "{\"a\":1}"}, {"document_id": "doc-b", "record": "{\"b\":2}"}]])
    records = await graph(monkeypatch, session).list_reindex_text_records(MEMORY)
    assert records == {"doc-a": "{\"a\":1}", "doc-b": "{\"b\":2}"}
    [(query, params)] = session.calls
    assert "MATCH (d:Document {memory_id: $memory_id})" in query
    assert "d.extracted_text_json IS NOT NULL" in query and "LIMIT $limit" in query
    assert params == {"memory_id": MEMORY, "limit": 10_001}


async def test_text_records_beyond_the_document_cap_fail_closed(monkeypatch):
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.maintenance import ReindexSourceLimitExceeded
    monkeypatch.setattr(graph_module, "MAX_REINDEX_SOURCE_DOCUMENTS", 1)
    session = Pages([[{"document_id": "doc-a", "record": "{}"}, {"document_id": "doc-b", "record": "{}"}]])
    with pytest.raises(ReindexSourceLimitExceeded):
        await graph(monkeypatch, session).list_reindex_text_records(MEMORY)


async def test_occurrences_are_read_in_ascending_pages_of_one_published_document(monkeypatch):
    from mcp_memory.core import graph as graph_module, temporal
    monkeypatch.setattr(graph_module, "REINDEX_OCCURRENCE_PAGE", 2)
    rows = ordered(5)
    session = Pages([rows[0:2], rows[2:4], rows[4:5]])
    found = await graph(monkeypatch, session).list_reindex_occurrences(MEMORY, DOC)
    assert [o.occurrence_id for o in found] == [r["id"] for r in rows]
    assert all(isinstance(o, temporal.Occurrence) for o in found)
    assert [c[1]["after"] for c in session.calls] == ["", rows[1]["id"], rows[3]["id"]]
    assert {c[1]["limit"] for c in session.calls} == {2}
    query = session.calls[0][0]
    assert "ingestion_status: 'succeeded'" in query and "o.doc_id = d.id" in query
    assert "o.id > $after" in query and "ORDER BY o.id" in query and "HAS_TEMPORAL_OCCURRENCE" in query
    assert all(c[1]["memory_id"] == MEMORY and c[1]["doc_id"] == DOC for c in session.calls)


async def test_an_exact_page_boundary_reads_one_more_page_then_stops(monkeypatch):
    from mcp_memory.core import graph as graph_module
    monkeypatch.setattr(graph_module, "REINDEX_OCCURRENCE_PAGE", 2)
    rows = ordered(4)
    session = Pages([rows[0:2], rows[2:4], []])
    found = await graph(monkeypatch, session).list_reindex_occurrences(MEMORY, DOC)
    assert len(found) == 4 and len(session.calls) == 3


@pytest.mark.parametrize("damage", ["corrupt_json", "foreign_document", "tampered_identity", "wrong_schema"])
async def test_an_unverifiable_stored_record_is_never_returned(monkeypatch, damage):
    from mcp_memory.core.temporal import TemporalOutputError
    row = stored(1)
    if damage == "corrupt_json":
        row["record_json"] = "{not json"
    elif damage == "foreign_document":
        row = stored(1, doc="doc-b")  # the node sits under doc-a, the record is bound to doc-b
    elif damage == "tampered_identity":
        record = json.loads(row["record_json"])
        record["fact"]["to_entity"] = "Someone else"
        row["record_json"] = json.dumps(record)
    elif damage == "wrong_schema":
        record = json.loads(row["record_json"])
        record["temporal_schema_version"] = 2
        row["record_json"] = json.dumps(record)
    with pytest.raises(TemporalOutputError):
        await graph(monkeypatch, Pages([[row]])).list_reindex_occurrences(MEMORY, DOC)


async def test_a_document_above_the_occurrence_cap_is_refused_not_truncated(monkeypatch):
    from mcp_memory.core import graph as graph_module
    from mcp_memory.core.maintenance import ReindexSourceLimitExceeded
    monkeypatch.setattr(graph_module, "REINDEX_OCCURRENCE_PAGE", 2)
    monkeypatch.setattr(graph_module, "MAX_REINDEX_OCCURRENCES_PER_DOCUMENT", 3)
    rows = ordered(5)
    with pytest.raises(ReindexSourceLimitExceeded):
        await graph(monkeypatch, Pages([rows[0:2], rows[2:4], rows[4:5]])).list_reindex_occurrences(MEMORY, DOC)
