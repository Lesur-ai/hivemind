"""Actual simple/chunked extraction must honor the supplied frozen catalogue."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def extractor(monkeypatch):
    from tests.fakes.inference_fakes import apply_graph_memory_baseline_env
    apply_graph_memory_baseline_env(monkeypatch)
    from mcp_memory.core.extractor import ExtractorService
    return object.__new__(ExtractorService)


def payload(entity="Device", relation="CONNECTS"):
    return json.dumps({"entities": [{"name": "Switch", "type": entity}], "relations": [{"from_entity": "Switch", "to_entity": "Router", "type": relation}]})


@pytest.mark.parametrize("bad", [payload(relation="INVENTED"), payload(entity="Invented"), payload(relation="SIGNED_BY"), "not-json", "[]"])
async def test_one_correction_then_explicit_failure(extractor, bad):
    from tests.test_automatic_ontology_state import YAML
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=bad))
    with pytest.raises(ValueError, match="frozen ontology"):
        await extractor.extract_with_ontology("A switch connects to a router.", YAML)
    assert extractor._complete.await_count == 2
    assert "CONNECTS" in str(extractor._complete.await_args)


async def test_valid_correction_does_not_change_catalogue(extractor):
    from tests.test_automatic_ontology_state import YAML
    extractor._complete = AsyncMock(side_effect=[SimpleNamespace(text=payload(relation="INVENTED")), SimpleNamespace(text=payload())])
    result = await extractor.extract_with_ontology("A switch connects to a router.", YAML)
    assert [r.type for r in result.relations] == ["CONNECTS"]
    assert extractor._complete.await_count == 2
    messages = extractor._complete.await_args.args[0]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == payload(relation="INVENTED")


@pytest.mark.parametrize("empty", ["", None, " \n\t"])
async def test_empty_completion_can_be_corrected_without_empty_assistant(extractor, empty):
    from tests.test_automatic_ontology_state import YAML
    calls = 0
    async def strict_provider(messages):
        nonlocal calls
        calls += 1
        if any(message["role"] == "assistant" and not message["content"].strip() for message in messages):
            raise RuntimeError("Endpoint rejects empty assistant content")
        return SimpleNamespace(text=empty if calls == 1 else payload())
    extractor._complete = AsyncMock(side_effect=strict_provider)
    result = await extractor.extract_with_ontology("A switch connects to a router.", YAML)
    assert [relation.type for relation in result.relations] == ["CONNECTS"]
    assert calls == 2
    messages = extractor._complete.await_args.args[0]
    assert [message["role"] for message in messages] == ["system", "user", "user"]
    assert "corrected complete JSON" in messages[-1]["content"]


async def test_correction_output_is_bounded_and_not_logged(extractor, capsys):
    from tests.test_automatic_ontology_state import YAML
    marker = "PRIVATE-FAULTY-OUTPUT:"
    bad = marker + "é" * 20_000
    extractor._complete = AsyncMock(side_effect=[SimpleNamespace(text=bad), SimpleNamespace(text=payload())])
    await extractor.extract_with_ontology("A switch connects to a router.", YAML)
    messages = extractor._complete.await_args.args[0]
    assert messages[-2]["role"] == "assistant"
    excerpt = messages[-2]["content"]
    assert excerpt.startswith(marker)
    assert len(excerpt.encode("utf-8")) <= 16_384
    assert "corrected complete JSON" in messages[-1]["content"]
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err


async def test_valid_catalogue_relation_punctuation_is_preserved(extractor):
    from tests.test_automatic_ontology_state import YAML
    catalogue = YAML.replace("CONNECTS", "CONNECTS-TO")
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=payload(relation="CONNECTS-TO")))
    result = await extractor.extract_with_ontology("A switch connects to a router.", catalogue)
    assert result.relations[0].type == "CONNECTS-TO"


@pytest.mark.parametrize("relation", ["RELATED_TO", "Other"])
async def test_explicit_fallbacks_are_retained(extractor, relation):
    from tests.test_automatic_ontology_state import YAML
    extractor._complete = AsyncMock(return_value=SimpleNamespace(text=payload("Other", relation)))
    result = await extractor.extract_with_ontology("Ambiguous sourced statement.", YAML)
    assert result.entities[0].type == "Other"
    assert result.relations[0].type == relation.upper()
    assert extractor._complete.await_count == 1


async def test_chunked_path_has_same_guard(extractor, monkeypatch):
    from tests.test_automatic_ontology_state import YAML
    from mcp_memory.core import extractor as module
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(extraction_chunk_size=5, extraction_max_text_length=1000))
    monkeypatch.setattr(extractor, "_split_text_for_extraction", lambda *_: ["first", "second"])
    extractor._complete = AsyncMock(side_effect=[SimpleNamespace(text=payload()), SimpleNamespace(text=payload(relation="INVENTED")), SimpleNamespace(text=payload(relation="INVENTED"))])
    with pytest.raises(ValueError, match="frozen ontology"):
        await extractor.extract_with_ontology_chunked("first second", YAML)
    assert extractor._complete.await_count == 3


def test_legacy_parser_remains_tolerant(extractor):
    assert extractor._parse_extraction("not-json").entities == []
    assert extractor._parse_extraction(payload(relation="INVENTED")).relations[0].type == "INVENTED"


@pytest.mark.parametrize("failed_chunk", [0, 1, 2])
@pytest.mark.parametrize("category", ["timeout", "invalid_response"])
async def test_chunk_failure_never_returns_partial_success(
    extractor, monkeypatch, failed_chunk, category,
):
    from hivemind_inference import InferenceError
    from tests.test_automatic_ontology_state import YAML
    from mcp_memory.core import extractor as module
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        extraction_chunk_size=5, extraction_max_text_length=1000,
    ))
    chunks = ["first", "second", "third"]
    monkeypatch.setattr(extractor, "_split_text_for_extraction", lambda *_: chunks)
    error = InferenceError(
        category=category, role="chat", provider_id="openai-compatible",
        adapter_id="openai-compatible", retryable=False,
        correlation_id="chunk-completeness-test",
    )
    responses = [SimpleNamespace(text=payload()) for _ in chunks]
    responses[failed_chunk] = error
    extractor._complete = AsyncMock(side_effect=responses)
    progress = AsyncMock()

    with pytest.raises(InferenceError) as raised:
        await extractor.extract_with_ontology_chunked(
            "first second third", YAML, progress_callback=progress,
        )

    assert raised.value is error
    assert extractor._complete.await_count == failed_chunk + 1
    completed = [call.args[1]["chunk"] for call in progress.await_args_list
                 if call.args[0] == "extraction_chunk_done"]
    assert completed == list(range(1, failed_chunk + 1))


async def test_complete_chunked_extraction_retains_every_chunk(extractor, monkeypatch):
    from tests.test_automatic_ontology_state import YAML
    from mcp_memory.core import extractor as module
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        extraction_chunk_size=5, extraction_max_text_length=1000,
    ))
    names = ["FirstSwitch", "SecondSwitch", "ThirdSwitch"]
    monkeypatch.setattr(extractor, "_split_text_for_extraction", lambda *_: names)
    extractor._complete = AsyncMock(side_effect=[
        SimpleNamespace(text=payload().replace('"Switch"', json.dumps(name)))
        for name in names
    ])
    progress = AsyncMock()

    result = await extractor.extract_with_ontology_chunked(
        "first second third", YAML, progress_callback=progress,
    )

    assert {entity.name for entity in result.entities} == set(names)
    assert extractor._complete.await_count == 3
    completed = [call.args[1]["chunk"] for call in progress.await_args_list
                 if call.args[0] == "extraction_chunk_done"]
    assert completed == [1, 2, 3]
