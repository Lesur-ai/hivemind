"""T03 structural oracle; controlled providers do not qualify model semantics."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_ontology_closed_vocabulary import extractor, payload


TEXT = "# Session A\n12 April 2022\n\nMira adopted Pip last week.\n\n# Session B\n12 January 2024\n\nMira adopted Pip last week.\n"


def time_value(expression=None, evidence=None, **updates):
    value = dict(status="unknown", expression=expression,
                 expression_evidence=evidence or [], precision="unknown",
                 normalized=None, anchor=None, anchor_candidates=[])
    return value | updates


def response(source, index=0):
    """Use source inventory, never feed an expected answer into inference."""
    facts = [p for p in source.passages if "Mira adopted" in p.quote]
    dates = [p for p in source.passages if "2022" in p.quote or "2024" in p.quote]
    fact, date = facts[index], dates[index]
    owner = source.owner(fact)
    anchor = dict(expression=date.quote, expression_evidence=[date.passage_id],
                  scope_id=owner.scope_id, normalized=None)
    row = dict(fact=dict(from_entity="Mira", to_entity="Pip", type="CONNECTS"),
               fact_evidence=[fact.passage_id], scope_id=owner.scope_id,
               source_time=time_value(date.quote, [date.passage_id], status="explicit", precision="day"),
               event_time=time_value("last week", [fact.passage_id], status="anchored_relative",
                                     precision="week", anchor=anchor), valid_time=None)
    body = json.loads(payload())
    body["relations"] = [dict(row["fact"], description="adoption")]
    body.update(temporal_schema_version=1, occurrences=[row])
    return body


@pytest.mark.parametrize("ontology", ["general", "yaml"])
async def test_temporal_native_short_parser_and_generation_identity(extractor, ontology):
    from mcp_memory.core.temporal import ExtractionSource, bind_source
    from tests.test_automatic_ontology_state import YAML
    source = ExtractionSource(TEXT, markdown=True)
    body = response(source)
    if ontology == "general":
        body["entities"][0]["type"] = "Other"
        body["relations"][0]["type"] = "RELATED_TO"
        body["occurrences"][0]["fact"]["type"] = "RELATED_TO"
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=json.dumps(body)))
    result = await extractor.extract_with_ontology(TEXT, YAML if ontology == "yaml" else ontology,
                                                   source_context=source)
    assert result.temporal_schema_version == 1
    assert len(result.occurrences) == 1
    occurrence = result.occurrences[0]
    assert occurrence.event_time.expression == "last week"
    assert occurrence.event_time.anchor.expression == "12 April 2022"
    assert occurrence.event_time.normalized is None
    assert occurrence.fact_evidence[0].quote == "Mira adopted Pip last week."
    assert extractor._complete.await_count == 1
    assert "temporal_schema_version" in str(extractor._complete.await_args)
    checksum = hashlib.sha256(TEXT.encode()).hexdigest()
    bound = bind_source(result, source, "memory-A", "generation-A", checksum,
                        {"converter_id": "utf8", "converter_version": "1"})
    again = bind_source(result, source, "memory-A", "generation-A", checksum,
                        {"converter_id": "utf8", "converter_version": "1"})
    assert bound.model_dump() == again.model_dump()
    record = bound.occurrences[0]
    assert record.source_ref.text_sha256 == checksum
    assert record.source_ref.memory_id == "memory-A"
    identity = ["long-occurrence-v1", "memory-A", "generation-A", checksum, checksum,
                "Mira", body["relations"][0]["type"], "Pip",
                [[p.start, p.end, p.quote] for p in record.fact_evidence]]
    expected = "occ1:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert record.occurrence_id == expected
    for memory, generation in [("memory-A", "generation-B"), ("memory-B", "generation-A")]:
        other = bind_source(result, source, memory, generation, checksum, {})
        assert other.occurrences[0].occurrence_id != expected
    assert bound.source_text == TEXT
    assert bound.text_provenance["sha256"] == checksum
    assert result.occurrences[0].occurrence_id is None  # no caller mutation


async def test_temporal_native_chunk_merge_keeps_two_sessions_and_replay(extractor, monkeypatch):
    from mcp_memory.core.temporal import ExtractionSource
    from tests.test_automatic_ontology_state import YAML
    from mcp_memory.core import extractor as module
    source = ExtractionSource(TEXT, markdown=True)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        extraction_chunk_size=65, extraction_max_text_length=10000))
    outputs = []
    async def complete(messages):
        # The source packet identifies exactly the passages available in this call.
        packet = json.loads(messages[1]["content"].split("TEMPORAL_SOURCE_JSON\n", 1)[1])
        text = "\n".join(p["quote"] for p in packet["passages"] if p["in_slice"])
        body = response(source, 1 if "Session B" in text or "2024" in text else 0)
        if "Mira adopted" not in text:
            body["relations"] = body["occurrences"] = []
        outputs.append(body)
        return SimpleNamespace(text=json.dumps(body))
    extractor._complete = AsyncMock(side_effect=complete)
    result = await extractor.extract_with_ontology_chunked(TEXT, YAML, source_context=source)
    assert len(result.relations) == 1
    assert len(result.occurrences) == 2
    assert {o.event_time.anchor.expression for o in result.occurrences} == {"12 April 2022", "12 January 2024"}
    assert len({o.scope.start for o in result.occurrences}) == 2
    merged = extractor._merge_extraction_results(result.entities, result.relations * 2, [], [],
                                                result.occurrences * 2, temporal_schema_version=1)
    assert merged.occurrences == result.occurrences
    conflicting = result.occurrences[0].model_copy(deep=True)
    conflicting.event_time = conflicting.event_time.model_copy(update={"precision": "unknown"})
    with pytest.raises(ValueError, match="temporal_occurrence_conflict"):
        extractor._merge_extraction_results([], result.relations, [], [],
                                           [result.occurrences[0], conflicting], temporal_schema_version=1)


@pytest.mark.parametrize("fault", ["sibling", "widened_scope", "reference", "version_bool", "status",
                                    "normalized_relative", "identity", "missing", "uncovered_relation",
                                    "source_relative", "expression", "candidate_type", "extra_time_key",
                                    "impossible_date", "closed_missing_endpoint", "unexpressed_validity"])
async def test_temporal_invalid_output_uses_bounded_correction(extractor, fault, capsys):
    from mcp_memory.core.temporal import ExtractionSource
    from tests.test_automatic_ontology_state import YAML
    source = ExtractionSource(TEXT, markdown=True)
    body = response(source)
    row = body["occurrences"][0]
    if fault == "sibling":
        row["event_time"]["anchor"] = response(source, 1)["occurrences"][0]["event_time"]["anchor"]
    elif fault == "widened_scope":
        row["event_time"]["anchor"]["scope_id"] = source.scopes[0].scope_id
    elif fault == "reference": row["fact_evidence"] = ["foreign-document"]
    elif fault == "version_bool": body["temporal_schema_version"] = True
    elif fault == "status": row["event_time"]["status"] = "guessed"
    elif fault == "normalized_relative":
        row["event_time"]["normalized"] = dict(kind="point", value="2022-04-05", precision="day", timezone=None)
    elif fault == "identity": row["source_ref"] = {"memory_id": "foreign"}
    elif fault == "missing": del body["temporal_schema_version"]
    elif fault == "uncovered_relation": body["relations"].append(dict(from_entity="Other", to_entity="Pip", type="CONNECTS"))
    elif fault == "source_relative": row["source_time"] = deepcopy(row["event_time"])
    elif fault == "expression": row["event_time"]["expression"] = "invented-date-PRIVATE"
    elif fault == "candidate_type": row["event_time"]["anchor_candidates"] = "wrong"
    elif fault == "extra_time_key": row["event_time"]["confidence"] = 1
    elif fault == "impossible_date":
        row["source_time"]["normalized"] = dict(kind="point", value="2022-02-30", precision="day", timezone=None)
    elif fault == "closed_missing_endpoint":
        row["source_time"]["normalized"] = dict(kind="interval", start=None, end=None, bounds="[]")
    elif fault == "unexpressed_validity": row["valid_time"] = time_value()
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=json.dumps(body)))
    with pytest.raises(ValueError, match="frozen ontology"):
        await extractor.extract_with_ontology(TEXT, YAML, source_context=source)
    assert extractor._complete.await_count == 2
    captured = capsys.readouterr()
    assert "12 April 2022" not in captured.out + captured.err


async def test_temporal_distant_context_and_no_cumulative_date(extractor, monkeypatch):
    from mcp_memory.core.temporal import ExtractionSource
    source = ExtractionSource("# Log\n12 April 2022\n\n" + "filler\n\n" * 100 + "Mira adopted Pip last week.\n", markdown=True)
    fact = source.passages[-1]
    packet = source.slice(fact.start, fact.end).packet()
    assert any(p["quote"] == "12 April 2022" and not p["in_slice"] for p in packet["passages"])
    assert packet["context_status"] == "complete"
    too_large = ExtractionSource("# Log\n" + "x" * 2100 + "\n\nFact.\n", markdown=True)
    limited = too_large.slice(len(too_large.text) - 6, len(too_large.text))
    assert limited.packet()["context_status"] == "partial"
    assert limited.packet()["warnings"] == ["context_budget_exceeded"]
    assert sum(len(p["quote"]) for p in limited.packet()["passages"] if not p["in_slice"]) <= 8192
    omitted = too_large.passages[1].passage_id
    # A truncated introduction has its own bounded reference; the original
    # full line cannot be restored from a shorter supplied passage.
    assert all(p["end"] <= 2054 for p in limited.packet()["passages"] if not p["in_slice"])
    assert omitted not in limited.passages
    with pytest.raises(ValueError, match="invalid_temporal_reference"):
        limited.evidence([omitted])
    nested = ExtractionSource("".join("#" * level + " Header\n" + "z" * 1700 + "\n\n" for level in range(1, 7)) + "Fact.\n", markdown=True)
    nested_fact = nested.passages[-1]
    bounded = nested.slice(nested_fact.start, nested_fact.end).packet()
    assert bounded["context_status"] == "partial"
    assert sum(len(p["quote"]) for p in bounded["passages"] if not p["in_slice"]) <= 8192
    # A cancelled provider cannot produce an apparently successful annotation.
    from tests.test_automatic_ontology_state import YAML
    extractor._complete = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await extractor.extract_with_ontology(source.text, YAML, source_context=source)
    assert extractor._complete.await_count == 1


def test_temporal_extension_never_silently_becomes_legacy(extractor):
    body = json.loads(payload()) | {"temporal_schema_version": 99, "occurrences": []}
    with pytest.raises(ValueError, match="temporal"):
        extractor._parse_extraction(json.dumps(body))
    assert extractor._parse_extraction(payload()).relations  # unchanged legacy contract
    # Occurrence extraction off (#999): fields nobody asked for are removed
    # before any validation and counted, never validated, kept or rejected.
    from mcp_memory.core.temporal import ExtractionSource
    source = ExtractionSource(TEXT, markdown=True)
    spontaneous = response(source)
    spontaneous["occurrences"][0]["event_time"]["anchor_candidates"] = [
        spontaneous["occurrences"][0]["event_time"]["anchor"]]  # anchor + candidates: invalid when requested
    types = dict(known_entity_types={"Device", "Other"}, known_relation_types={"CONNECTS", "OTHER", "RELATED_TO"})
    for strict in (False, True):
        for unasked in (body, spontaneous):
            plain = {k: v for k, v in unasked.items() if k not in ("temporal_schema_version", "occurrences")}
            expected = extractor._parse_extraction(json.dumps(plain), strict=strict, **types)
            result = extractor._parse_extraction(json.dumps(unasked), strict=strict, discard_temporal_output=True, **types)
            assert (result.temporal_schema_version, result.occurrences, result.temporal_output_discarded) == (None, [], 2)
            assert expected.temporal_output_discarded == 0 and expected.relations
            assert result.model_dump(exclude={"temporal_output_discarded"}) == expected.model_dump(
                exclude={"temporal_output_discarded"})
    with pytest.raises(ValueError, match="temporal"):  # the same output, requested, is refused
        extractor._parse_extraction(json.dumps(spontaneous), strict=False, source_slice=source.slice(0, len(TEXT)))
    with pytest.raises(AssertionError):  # requesting and discarding are exclusive
        extractor._parse_extraction(payload(), source_slice=source.slice(0, len(TEXT)), discard_temporal_output=True)


@pytest.mark.parametrize("status", ["unknown", "ambiguous", "validity"])
async def test_temporal_preserves_absence_ambiguity_and_interval(extractor, status):
    from mcp_memory.core.temporal import ExtractionSource
    from tests.test_automatic_ontology_state import YAML
    source = ExtractionSource(TEXT, markdown=True)
    body = response(source)
    row = body["occurrences"][0]
    if status == "unknown":
        row["source_time"] = time_value()
        row["event_time"] = time_value("last week", row["fact_evidence"], precision="week")
    elif status == "ambiguous":
        candidate = row["event_time"]["anchor"]
        row["event_time"] = time_value("last week", row["fact_evidence"], status="ambiguous",
                                        precision="week", anchor_candidates=[candidate])
    else:
        row["valid_time"] = time_value("12 April 2022", row["source_time"]["expression_evidence"],
                                       status="explicit", precision="interval",
                                       normalized={"kind": "interval", "start": {
                                           "kind": "point", "value": "2022-04-12", "precision": "day", "timezone": None},
                                                   "end": None, "bounds": "[)"})
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=json.dumps(body)))
    value = (await extractor.extract_with_ontology(TEXT, YAML, source_context=source)).occurrences[0]
    if status == "unknown":
        assert value.source_time.expression is None
        assert value.event_time.status == "unknown" and value.event_time.expression == "last week"
        assert value.event_time.anchor is None
    elif status == "ambiguous":
        assert value.event_time.anchor is None
        assert value.event_time.anchor_candidates[0].expression == "12 April 2022"
    else:
        assert value.valid_time.normalized.end is None
        assert value.valid_time.normalized.bounds == "[)"
        assert value.valid_time.normalized.start.precision == "day"
    # All fields survive the public DTO serialization used by T08/T04.
    from mcp_memory.core.models import ExtractionResult
    restored = ExtractionResult.model_validate_json(ExtractionResult(
        temporal_schema_version=1, occurrences=[value]).model_dump_json())
    assert restored.occurrences == [value]


def test_temporal_structure_fallback_and_exact_unicode_context():
    from mcp_memory.core.temporal import ExtractionSource
    text = "# Journal\r\nÉcrit le 12 avril 2022   \r\n\r\n```\r\n# fake session\r\n```\r\n\r\nUn fait énoncé.\r\n"
    assert len(ExtractionSource(text).scopes) == 1  # non-Markdown # lines are prose
    source = ExtractionSource(text, markdown=True)
    assert len(source.scopes) == 2  # fenced content is not a session
    fact = source.passages[-1]
    packet = source.slice(fact.start, fact.end).packet()
    assert any(p["quote"] == "Écrit le 12 avril 2022" for p in packet["passages"])
    for p in packet["passages"]:
        assert text[p["start"]:p["end"]] == p["quote"]
    assert source.owner(fact).scope_id == "s1"  # root/heading have same extent
    plain = ExtractionSource("Date 2022\n\nA fact\n\nDate 2024\n\nAnother fact\n   ")
    assert len(plain.scopes) == 1  # blank lines/dates never invent sessions


async def test_temporal_correction_restates_occurrence_contract(extractor):
    from mcp_memory.core.temporal import ExtractionSource
    from tests.test_automatic_ontology_state import YAML
    source = ExtractionSource(TEXT, markdown=True)
    valid = response(source)
    invalid = deepcopy(valid)
    invalid["occurrences"][0]["valid_time"] = time_value()
    extractor._complete = AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(invalid)),
                                                  SimpleNamespace(text=json.dumps(valid))])
    result = await extractor.extract_with_ontology(TEXT, YAML, source_context=source)
    assert len(result.occurrences) == 1
    assert extractor._complete.await_count == 2
    correction = extractor._complete.await_args.args[0][-1]["content"]
    assert "temporal_schema_version" in correction and "occurrences" in correction
    assert "passage" in correction and "scope" in correction
