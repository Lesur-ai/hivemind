"""Synthetic evidence for the frozen D1/D2 construction contract (#533)."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from hivemind_inference.records import ChatResult
from hivemind_inference.errors import InferenceError
from mcp_memory.core import ontology_construction as core


def document(path, text):
    return {"source_path": path, "text": text, "sha256": sha256(text.encode()).hexdigest()}


def catalogue(name="Actor"):
    return {"entity_types": [{"name": name, "description": "A named actor."}],
            "relation_types": [{"name": "KNOWS", "description": "Subject knows object."}]}


def result(value, **kwargs):
    return ChatResult(text=json.dumps(value), configured_model="synthetic",
                      model_evidence="configured_only", finish_reason="stop",
                      input_tokens=10, output_tokens=5, total_tokens=15, **kwargs)


class Provider:
    def __init__(self):
        self.requests = []

    async def __call__(self, messages):
        packet = json.loads(messages[1].content)
        self.requests.append(packet)
        data = packet["data"]
        if "candidate_assertions" in data:
            value = catalogue("Revised" if "existing_ontology" in data else "Initial")
            if "existing_ontology" in data:
                value["mode"] = "complete"
            return result(value)
        return result({"assertions": [
            {"subject": "Alice", "predicate": "knows", "object": None,
             "polarity": "affirmed", "condition": None,
             "evidence": [{"passage_id": p["passage_id"]}]}
            for p in data["passages"] if p["text"].strip()]})


class Saved:
    def __init__(self):
        self.latest = {}
        self.snapshots = []

    async def __call__(self, state):
        self.latest = deepcopy(state)
        self.snapshots.append(deepcopy(state))


async def construct(docs, provider=None, state=None, saved=None, budget=100_000):
    return await core.construct_ontology(
        docs, checkpoint={} if state is None else state,
        complete=provider or Provider(), save_checkpoint=saved or Saved(),
        input_budget_bytes=budget)


async def test_virgin_corpus_d1_d2_replace_and_resume_without_inference():
    docs = [document(f"stratum-{i}/source.md", f"Actor {i} knows a fact.") for i in range(14)]
    provider, state, saved = Provider(), {}, Saved()
    actual = await construct(docs, provider, state, saved)
    assert actual["catalogue"] == catalogue("Revised")
    assert len(actual["selection"]["d1"]) == 8
    assert len(actual["selection"]["d2"]) == 4
    assert {x["id"] for x in actual["selection"]["d1"]}.isdisjoint(
        x["id"] for x in actual["selection"]["d2"])
    assert len(provider.requests) == 14  # twelve extractions, two catalogue calls
    second = provider.requests[-1]
    assert second["data"]["existing_ontology"] == catalogue("Initial")
    assert "FULL effective catalogue" in second["instruction"]
    assert "Do not include RELATED_TO; it is supplied by the runtime." in second["instruction"]
    assert actual["usage"]["total_tokens"] == 210
    assert actual["usage"]["cost_eur"] is None
    assert actual["catalogue_sha256"] == core._digest(actual["catalogue"])
    async def forbidden(_):
        raise AssertionError("an admitted call was replayed")
    resumed = await construct(list(reversed(docs)), forbidden, deepcopy(saved.latest), Saved())
    assert resumed == actual


async def test_small_corpus_uses_available_passage_once_without_fake_d2():
    provider = Provider()
    actual = await construct([document("one.md", "Alice knows Bob.")], provider)
    assert actual["catalogue"] == catalogue("Initial")
    assert len(actual["selection"]["d1"]) == 1
    assert actual["selection"]["d2"] == []
    assert len(provider.requests) == 2
    instruction = provider.requests[0]["instruction"]
    assert "The predicate is a short faithful phrase, not an ontology label." in instruction
    assert "Preserve exact quantities and units" in instruction
    assert "Do not omit a fact just because it has no ontology type." in instruction


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("indent", [None, 2])
async def test_whole_json_fence_preserves_construction_and_resume(newline, indent):
    docs = [document(f"{i}.md", f"Actor {i} knows Bob.") for i in range(9)]
    direct = await construct(docs)
    provider, state = Provider(), {}
    async def fenced(messages):
        response = await provider(messages)
        payload = json.dumps(json.loads(response.text), indent=indent).replace("\n", newline)
        return replace(response, text=f" \t{newline}```json{newline}{payload}{newline}```{newline} ")
    actual = await construct(docs, fenced, state)
    assert actual["catalogue"] == direct["catalogue"] == catalogue("Revised")
    assert actual["catalogue_sha256"] == direct["catalogue_sha256"]
    assert len(provider.requests) == len(state["calls"]) == 11
    assert all(row["status"] == "admitted" for row in state["attempts"])
    assert all(isinstance(row["payload"], dict) for row in state["calls"].values())
    async def forbidden(_):
        raise AssertionError("an admitted fenced response was replayed")
    assert await construct(docs, forbidden, deepcopy(state)) == actual


@pytest.mark.parametrize("raw", [
    '```JSON\n{}\n```', '```\n{}\n```',
    '```json {}\n```', '```json\n{}```',
    '```json\n{}\n```\n```json\n{}\n```',
    '```json\n{"a":1,"a":2}\n```',
    '```json\n{"nested":{"a":1,"a":2}}\n```',
    '```json\n{"a":NaN}\n```', '```json\n{"a":Infinity}\n```',
    '```json\n{"a":-Infinity}\n```',
    '```json\n{"a":}\n```',
])
def test_json_fence_rejects_other_envelopes_and_invalid_json(raw):
    with pytest.raises(ValueError):
        core._parse(raw)


@pytest.mark.parametrize("failure", ["schema", "evidence"])
async def test_json_fence_cannot_admit_invalid_extraction(failure, monkeypatch):
    provider, state = Provider(), {}
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    async def invalid(messages):
        response = await provider(messages)
        payload = json.loads(response.text)
        if failure == "schema":
            payload["assertions"] = "invalid"
        else:
            payload["assertions"][0]["evidence"][0]["passage_id"] = "unknown"
        return replace(response, text="```json\n" + json.dumps(payload) + "\n```")
    with pytest.raises(core.OntologyConstructionError, match="^invalid_output$"):
        await construct([document("a.md", "A claim.")], invalid, state)
    assert len(provider.requests) == 2
    assert state["calls"] == {}
    assert [row["status"] for row in state["attempts"]] == ["rejected", "rejected"]
    assert "frozen" not in state


async def test_selection_covers_document_tails_and_deduplicates_sources():
    text = "".join(f"Quartile {i}. " + chr(65 + i) * 1600 + "\n\n" for i in range(12))
    docs = [document("notes/v1.md", text), document("copy.md", text)]
    actual = await construct(docs)
    selected = actual["selection"]["d1"] + actual["selection"]["d2"]
    assert len({x["text_sha256"] for x in selected}) == len(selected)
    assert any(x["text_start"] > len(text) * .75 for x in selected)
    assert {q for x in actual["selection"]["d1"] for q in x["quartiles"]} == {1, 2, 3, 4}


async def test_checkpoint_rejects_changed_corpus_and_corrupt_admitted_payload():
    docs = [document("one.md", "Alice knows Bob.")]
    saved = Saved()
    await construct(docs, saved=saved)
    for bad_docs, state in [([document("one.md", "A changed claim.")], deepcopy(saved.latest)),
                            (docs, deepcopy(saved.latest))]:
        if bad_docs == docs:
            next(iter(state["calls"].values()))["payload"] = {"assertions": []}
        with pytest.raises(core.OntologyConstructionError, match="checkpoint"):
            await construct(bad_docs, state=state)


async def test_resume_after_catalogue_failure_keeps_all_admitted_extractions(monkeypatch):
    docs = [document(f"{i}.md", f"Actor {i} knows Bob.") for i in range(3)]
    good, saved, state = Provider(), Saved(), {}
    async def interrupt(messages):
        if "candidate_assertions" in json.loads(messages[1].content)["data"]:
            raise asyncio.CancelledError()
        return await good(messages)
    with pytest.raises(asyncio.CancelledError):
        await construct(docs, interrupt, state, saved)
    after = Provider()
    await construct(docs, after, deepcopy(saved.latest))
    assert len(good.requests) == 3
    assert len(after.requests) == 1
    assert "candidate_assertions" in after.requests[0]["data"]


async def test_retry_only_known_transient_errors_with_248_backoff(monkeypatch):
    sleeps, calls = [], 0
    async def sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(core.asyncio, "sleep", sleep)
    good = Provider()
    async def transient(messages):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise InferenceError(category="rate_limited", role="chat",
                                 provider_id="openai-compatible", adapter_id="openai-compatible",
                                 retryable=True, correlation_id="test")
        return await good(messages)
    actual = await construct([document("a.md", "A claim.")], transient)
    assert sleeps == [2, 4, 8]
    assert len(actual["usage"]["attempts"]) == 5


async def test_auth_failure_is_not_retried_and_never_falls_back():
    calls = 0
    async def denied(_):
        nonlocal calls
        calls += 1
        raise InferenceError(category="auth", role="chat", provider_id="openai-compatible",
                             adapter_id="openai-compatible", retryable=False, correlation_id="test")
    with pytest.raises(core.OntologyConstructionError, match="inference_auth"):
        await construct([document("a.md", "A claim.")], denied)
    assert calls == 1


async def test_invalid_reference_gets_one_correction_then_explicit_failure():
    calls = 0
    async def fabricated(messages):
        nonlocal calls
        calls += 1
        packet = json.loads(messages[1].content)
        passage = packet["data"]["passages"][0]
        return result({"assertions": [{"subject": "A", "predicate": "B", "object": None,
            "polarity": "affirmed", "condition": None,
            "evidence": [{"passage_id": passage["passage_id"] + "-unknown"}]}]})
    with pytest.raises(core.OntologyConstructionError, match="invalid_output"):
        await construct([document("a.md", "A claim.")], fabricated)
    assert calls == 2


async def test_input_budget_and_length_split_without_truncation_or_success_replay():
    docs = [document(f"s/{i}.md", f"Unique assertion {i}: " + chr(65 + i) * 200) for i in range(12)]
    provider, state, saved = Provider(), {}, Saved()
    oversize = []
    async def bounded(messages):
        size = len(core._json([{"role": m.role, "content": m.content} for m in messages]).encode())
        assert size <= 9000
        data = json.loads(messages[1].content)["data"]
        if "candidate_assertions" in data and len(data["candidate_assertions"]) > 2:
            oversize.append(data)
            return ChatResult(text="", configured_model="synthetic", model_evidence="configured_only",
                              finish_reason="length")
        return await provider(messages)
    actual = await construct(docs, bounded, state, saved, budget=9000)
    all_ids = [row["id"] for request in provider.requests
               for row in request["data"].get("candidate_assertions", [])]
    assert len(all_ids) == len(set(all_ids)) == 12
    assert actual["catalogue"] == catalogue("Revised")
    assert oversize
    async def forbidden(_):
        raise AssertionError("a completed or split request was replayed")
    assert await construct(docs, forbidden, deepcopy(saved.latest), budget=9000) == actual


async def test_unsplittable_input_is_explicit_without_provider_call():
    async def forbidden(_):
        raise AssertionError("oversized request reached provider")
    with pytest.raises(core.OntologyConstructionError, match="physical_input_limit"):
        await construct([document("a.md", "A claim.")], forbidden, budget=10)


async def test_failed_checkpoint_write_cannot_admit_in_memory_result():
    state, saved = {}, Saved()
    async def fail_on_admission(value):
        if value["calls"]:
            raise OSError("synthetic persistence failure")
        await saved(value)
    with pytest.raises(OSError):
        await construct([document("a.md", "A claim.")], state=state, saved=fail_on_admission)
    assert not state["calls"]


async def test_interrupted_d2_reuses_d1_and_its_extractions():
    docs = [document(f"{i}.md", f"Statement {i}.") for i in range(12)]
    provider, saved = Provider(), Saved()
    async def stop_at_d2(messages):
        if "existing_ontology" in json.loads(messages[1].content)["data"]:
            raise asyncio.CancelledError()
        return await provider(messages)
    with pytest.raises(asyncio.CancelledError):
        await construct(docs, stop_at_d2, saved=saved)
    assert len(provider.requests) == 13
    resumed_provider = Provider()
    actual = await construct(docs, resumed_provider, deepcopy(saved.latest))
    assert len(resumed_provider.requests) == 1
    assert resumed_provider.requests[0]["data"]["existing_ontology"] == catalogue("Initial")
    assert actual["catalogue"] == catalogue("Revised")
    assert actual["usage"]["total_tokens"] is None  # interrupted attempt is unknown, not zero


@pytest.mark.parametrize("category,retryable,expected", [
    ("unavailable", True, 4), ("timeout", False, 1), ("quota_exhausted", True, 1),
])
async def test_retry_bounds_survive_error_category_hints(monkeypatch, category, retryable, expected):
    calls = 0
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    async def fail(_):
        nonlocal calls
        calls += 1
        raise InferenceError(category=category, role="chat", provider_id="openai-compatible",
                             adapter_id="openai-compatible", retryable=retryable, correlation_id="test")
    with pytest.raises(core.OntologyConstructionError, match="inference_" + category):
        await construct([document("a.md", "A claim.")], fail)
    assert calls == expected


async def test_correction_changes_only_instruction_and_counts_both_responses(monkeypatch):
    requests, provider = [], Provider()
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    async def once_invalid(messages):
        requests.append(json.loads(messages[1].content))
        if len(requests) == 1:
            return result({"assertions": "invalid"})
        return await provider(messages)
    actual = await construct([document("a.md", "A claim.")], once_invalid)
    assert requests[0]["data"] == requests[1]["data"]
    assert requests[0]["schema"] == requests[1]["schema"]
    assert requests[0]["instruction"] != requests[1]["instruction"]
    assert actual["usage"]["total_tokens"] == 45
    assert actual["usage"]["attempts"][0]["request_sha256"] != actual["usage"]["attempts"][1]["request_sha256"]


@pytest.mark.parametrize("bad", [
    {"entity_types": [{"name": "Other", "description": "Catchall"}], "relation_types": catalogue()["relation_types"]},
    {"entity_types": [{"name": "Actor", "description": "A"}, {"name": "actor", "description": "B"}],
     "relation_types": catalogue()["relation_types"]},
    {"entity_types": catalogue()["entity_types"], "relation_types": [{"name": "bad relation", "description": "Bad"}]},
    {"entity_types": [], "relation_types": catalogue()["relation_types"]},
    pytest.param({"entity_types": catalogue()["entity_types"], "relation_types": [
        {"name": "RELATED_TO", "description": "Generic relation."}]}, id="runtime-related-to"),
])
async def test_invalid_catalogue_never_freezes(bad, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    provider, state = Provider(), {}
    async def fail(messages):
        if "candidate_assertions" in json.loads(messages[1].content)["data"]:
            return result(bad)
        return await provider(messages)
    with pytest.raises(core.OntologyConstructionError, match="invalid_output"):
        await construct([document("a.md", "A claim.")], fail, state)
    assert "frozen" not in state


async def test_provider_exception_message_is_not_exposed_or_persisted():
    state = {}
    async def unsafe(_):
        raise RuntimeError("private source and provider token")
    with pytest.raises(core.OntologyConstructionError) as error:
        await construct([document("a.md", "A claim.")], unsafe, state)
    assert str(error.value) == "inference_failed"
    assert "private source" not in json.dumps(state)


@pytest.mark.parametrize("bad", [
    [], [document("a.md", "   ")],
    [{"source_path": "a.md", "text": "A claim.", "sha256": "0" * 64}],
    [document("a.md", "one"), document("a.md", "two")],
])
async def test_invalid_documents_fail_before_provider(bad):
    async def forbidden(_):
        raise AssertionError("invalid corpus reached provider")
    with pytest.raises(core.OntologyConstructionError):
        await construct(bad, forbidden)


@pytest.mark.parametrize("change", [
    "missing_tokens", "string_elapsed", "nan_elapsed", "negative_tokens", "bool_tokens", "not_a_row",
])
async def test_corrupt_attempts_rejected_before_resumed_inference(change):
    docs, state = [document("a.md", "A claim.")], {}
    await construct(docs, state=state)
    state.pop("frozen")
    state["calls"].pop(next(reversed(state["calls"])))
    if change == "missing_tokens":
        state["attempts"][0].pop("input_tokens")
    elif change == "string_elapsed":
        state["attempts"][0]["elapsed_seconds"] = "private malformed value"
    elif change == "nan_elapsed":
        state["attempts"][0]["elapsed_seconds"] = float("nan")
    elif change == "negative_tokens":
        state["attempts"][0]["total_tokens"] = -1
    elif change == "bool_tokens":
        state["attempts"][0]["output_tokens"] = True
    else:
        state["attempts"][0] = "private malformed row"
    calls = []
    async def forbidden(messages):
        calls.append(messages)
        raise AssertionError("corrupt attempts reached provider")
    with pytest.raises(core.OntologyConstructionError, match="^checkpoint_attempts_invalid$"):
        await construct(docs, forbidden, state)
    assert calls == []


@pytest.mark.parametrize("phase", ["extraction", "catalogue"])
async def test_unsplittable_output_limit_survives_resume_without_provider(phase):
    docs, state, provider = [document("a.md", "A claim.")], {}, Provider()
    async def truncated(messages):
        is_catalogue = "candidate_assertions" in json.loads(messages[1].content)["data"]
        if is_catalogue == (phase == "catalogue"):
            return ChatResult(text="", configured_model="synthetic", model_evidence="configured_only",
                              finish_reason="length")
        return await provider(messages)
    with pytest.raises(core.OntologyConstructionError, match="^physical_output_limit$"):
        await construct(docs, truncated, state)
    assert state["splits"]
    calls = []
    async def forbidden(messages):
        calls.append(messages)
        raise AssertionError("saved output truncation was replayed")
    with pytest.raises(core.OntologyConstructionError, match="^physical_output_limit$"):
        await construct(docs, forbidden, deepcopy(state))
    assert calls == []


async def test_empty_condition_is_corrected_once_then_rejected(monkeypatch):
    provider, state = Provider(), {}
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    async def empty_condition(messages):
        response = await provider(messages)
        payload = json.loads(response.text)
        if "assertions" in payload:
            payload["assertions"][0]["condition"] = ""
        return result(payload)
    with pytest.raises(core.OntologyConstructionError, match="^invalid_output$"):
        await construct([document("a.md", "A claim.")], empty_condition, state)
    assert len(provider.requests) == 2
    assert state["calls"] == {}
    assert "frozen" not in state


def test_source_quotes_normalized_after_reference_validation():
    units = [{"passage_id": "p1", "text": "Alice knows Bob.", "text_start": 0},
             {"passage_id": "p2", "text": "Carol funds Dave.", "text_start": 20}]
    expected = {"assertions": [
        {"subject": subject, "predicate": predicate, "object": object_,
         "polarity": "affirmed", "condition": None,
         "evidence": [{"passage_id": unit["passage_id"], "quote": unit["text"]} for unit in units]}
        for subject, predicate, object_ in (("Alice", "knows", "Bob"), ("Carol", "funds", "Dave"))]}
    payload = deepcopy(expected)
    for row in payload["assertions"]:
        for evidence in row["evidence"]:
            evidence["quote"] += "!"
    facts = core._validate_facts(payload, units)
    assert facts == expected["assertions"]
    assert payload == expected  # invoke persists this payload, not only the return value.
    assert facts is not payload["assertions"]
    assert facts[0] is not payload["assertions"][0]


@pytest.mark.parametrize("bad_index", [0, 1])
def test_source_quote_from_different_known_unit_is_rejected(bad_index):
    units = [{"passage_id": "p1", "text": "Alice knows Bob.", "text_start": 0},
             {"passage_id": "p2", "text": "Carol funds Dave.", "text_start": 20}]
    payload = {"assertions": [{"subject": "Carol", "predicate": "funds", "object": "Dave",
        "polarity": "affirmed", "condition": None,
        "evidence": [{"passage_id": unit["passage_id"], "quote": unit["text"]} for unit in units]}]}
    payload["assertions"][0]["evidence"][bad_index]["quote"] = units[1 - bad_index]["text"]
    before = deepcopy(payload)
    with pytest.raises(core.OntologyConstructionError, match="^invalid_extraction$"):
        core._validate_facts(payload, units)
    assert payload == before


@pytest.mark.parametrize("failure", ["unknown", "duplicate", "order", "missing_quote", "empty_quote", "mispaired_quote"])
def test_source_quote_normalization_keeps_reference_and_schema_guards(failure):
    units = [{"passage_id": f"p{i}", "text": f"Claim {i}.", "text_start": i * 10}
             for i in (1, 2)]
    row = {"subject": "Alice", "predicate": "knows", "object": None,
           "polarity": "affirmed", "condition": None,
           "evidence": [{"passage_id": "p1", "quote": "Typo."}]}
    invalid = deepcopy(row)
    if failure == "unknown":
        invalid["evidence"][0]["passage_id"] = "unknown"
    elif failure == "duplicate":
        invalid["evidence"] *= 2
    elif failure == "order":
        invalid["evidence"] = [{"passage_id": pid, "quote": "Typo."} for pid in ("p2", "p1")]
    elif failure == "missing_quote":
        invalid["evidence"][0].pop("quote")
    elif failure == "mispaired_quote":
        invalid["evidence"][0]["quote"] = units[1]["text"]
    else:
        invalid["evidence"][0]["quote"] = ""
    payload = {"assertions": [row, invalid]}
    before = deepcopy(payload)
    with pytest.raises(core.OntologyConstructionError, match="^invalid_extraction$"):
        core._validate_facts(payload, units)
    assert payload == before  # All references must pass before any quote is replaced.


async def test_reference_quotes_are_canonical_in_checkpoint_and_resume_without_inference(monkeypatch):
    docs = [document("a.md", "Alice knows Bob.")]
    provider, state, saved = Provider(), {}, Saved()
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, "sleep", no_wait)
    actual = await construct(docs, provider, state, saved)
    assert len(provider.requests) == 2  # one extraction, one catalogue; no correction call
    assert [r["status"] for r in state["attempts"]] == ["admitted", "admitted"]
    extraction_snapshots = [entry for snapshot in saved.snapshots
        for entry in snapshot["calls"].values() if "assertions" in entry["payload"]]
    assert extraction_snapshots
    for entry in extraction_snapshots:
        assert entry["payload"]["assertions"][0]["evidence"][0]["quote"] == docs[0]["text"]
        assert entry["payload_sha256"] == core._digest(entry["payload"])
    async def forbidden(_):
        raise AssertionError("an admitted canonical response was replayed")
    resumed_state = deepcopy(saved.latest)
    assert await construct(docs, forbidden, resumed_state) == actual
    assert resumed_state == saved.latest


@pytest.mark.parametrize("prefix,suffix", [("Here is the result:\n", ""), ("", "\nDone."), ("<think>reasoning</think>\n", "")])
def test_single_complete_json_fence_with_prose_is_supported(prefix, suffix):
    assert core._parse(prefix + '```json\n{"count":1}\n```' + suffix) == {"count":1}


@pytest.mark.parametrize("raw", [
    'Prose ```json\n{"a":1,"a":2}\n```',
    'Prose ```json\n{"nested":{"a":1,"a":2}}\n```',
    'Prose ```json\n{"a":NaN}\n```',
    'Prose ```json\n{"a":',
    'Prose ```json\n{}\n``` and ```json\n{}\n```',
    'Prose ```json\n{}\n```\nand ```json\n{}\n```',
    '```text\nprose\n``` followed by ```json\n{}\n```',
    'Prose without a fence: {}',
])
def test_external_prose_cannot_bypass_strict_or_single_json_frame(raw):
    with pytest.raises((ValueError, TypeError)):
        core._parse(raw)


@pytest.mark.parametrize("prefix", ["", "Explanation before the result:\n"])
def test_single_open_json_fence_requires_complete_json_through_eof(prefix):
    assert core._parse(prefix + '```json\n{"count":1}') == {"count":1}


@pytest.mark.parametrize("body", ['{"count":', '{"count":"unfinished', '{} trailing prose', '{"a":1,"a":2}', '{"a":NaN}', '{}\n```text\nmore'])
def test_open_json_fence_never_repairs_or_truncates_inner_payload(body):
    with pytest.raises((ValueError,TypeError)):
        core._parse('Prose ```json\n'+body)
