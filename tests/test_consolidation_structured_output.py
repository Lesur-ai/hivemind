"""Constrained generation never substitutes for MID pre-write validation."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import jsonschema
import pytest

from hivemind_inference.records import ChatMessage, ChatRequest, ChatResult
from live_mem.core import consolidator as core
from tests.test_p13_inference_adapters import complete_with, openai_chat_profile, anthropic_chat_profile
from tests.fakes.inference_emulator import InferenceEmulator
from hivemind_inference.errors import InferenceError


def request(**kwargs):
    return ChatRequest(
        messages=(ChatMessage(role="user", content="Return JSON."),),
        timeout_seconds=5, **kwargs,
    )


def test_schema_request_is_immutable_and_not_rendered():
    schema = '{"type":"object","description":"private-schema"}'
    item = request(response_schema_json=schema)
    assert item.response_schema_json == schema
    assert "private-schema" not in repr(item)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.response_schema_json = '{}'


@pytest.mark.parametrize("invalid", [True, {}, "", "[]", "null", "{broken"])
def test_invalid_schema_request_is_rejected(invalid):
    with pytest.raises(ValueError):
        request(response_schema_json=invalid)


async def test_schema_reaches_wire_without_changing_other_request_fields():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    async with InferenceEmulator() as emulator:
        await complete_with(openai_chat_profile(emulator.v1_url),
                            request(response_schema_json=json.dumps(schema), retry_policy="none"))
    assert len(emulator.requests) == 1
    body = emulator.requests[0]["json"]
    assert body["response_format"] == {
        "type": "json_schema", "json_schema": {
            "name": "hivemind_response", "strict": True, "schema": schema,
        },
    }
    assert body["model"] == "emulated-chat-model"
    assert body["max_tokens"] == 128


async def test_plain_request_keeps_existing_wire_contract():
    async with InferenceEmulator() as emulator:
        await complete_with(openai_chat_profile(emulator.v1_url), request())
    assert "response_format" not in emulator.requests[0]["json"]


async def test_endpoint_refusal_never_downgrades_to_free_form():
    async with InferenceEmulator([{"status": 400, "body": {"error": {"type": "invalid_request_error"}}}]) as emulator:
        with pytest.raises(InferenceError):
            await complete_with(openai_chat_profile(emulator.v1_url),
                                request(response_schema_json='{"type":"object"}', retry_policy="none"))
    assert len(emulator.requests) == 1
    assert "response_format" in emulator.requests[0]["json"]


async def test_native_adapter_refuses_unimplemented_constraint_before_http():
    async with InferenceEmulator() as emulator:
        with pytest.raises(InferenceError) as error:
            await complete_with(anthropic_chat_profile(emulator.url),
                                request(response_schema_json='{"type":"object"}'))
    assert error.value.category == "unsupported"
    assert emulator.requests == []


@pytest.mark.parametrize("identity", ["openai-compatible", "anthropic", "cloud-temple"])
async def test_normal_initial_and_correction_are_constrained_but_merge_is_plain(monkeypatch, identity):
    calls = []
    async def complete(item):
        calls.append(item)
        return ChatResult(text='{"file_edits":[],"discarded_notes":[],"synthesis":"ok"}',
                          configured_model="qwen3.8:27b", model_evidence="configured_only", finish_reason="stop")
    provider = SimpleNamespace(profile=SimpleNamespace(provider_id=identity, adapter_id="anthropic" if identity == "anthropic" else "openai-compatible"), complete=complete)
    from live_mem.core import inference_runtime
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: SimpleNamespace(chat_provider=lambda: provider))
    service = object.__new__(core.ConsolidatorService)
    service._timeout, service._max_tokens, service._context_window = 5, 32768, 250000
    messages = [{"role": "user", "content": "Return the normal plan."}]
    await service._call_llm(messages)
    await service._call_llm(core._corrective_messages(messages, None, None, "invalid_normal_consolidation_json"))
    await service._complete_chat(messages, 500)
    assert (calls[0].response_schema_json is not None) == (identity == "openai-compatible")
    assert calls[1].response_schema_json == calls[0].response_schema_json
    assert calls[2].response_schema_json is None
    assert calls[0].retry_policy == calls[1].retry_policy == "none"
    assert messages == [{"role": "user", "content": "Return the normal plan."}]


def test_schema_preserves_supported_operation_variants_and_rejects_extras():
    schema = json.loads(core._NORMAL_RESPONSE_SCHEMA_JSON)
    validator = jsonschema.Draft202012Validator(schema)
    assert set(schema["properties"]["discarded_notes"]["items"]["properties"]["reason"]["enum"]) == core._NORMAL_DISCARD_REASONS
    operations = [
        {"type": kind, "heading": "## Status", "content": "Fact", "reason": "New", "notes": [1]}
        for kind in ("append_to_section", "prepend_to_section", "replace_section", "add_section")
    ]
    operations += [{**operations[-1], "after": "## Before"},
                   {"type": "delete_section", "heading": "## Status", "reason": "Replaced", "notes": [1]}]
    edits = [{"filename": "facts.md", "action": "edit", "operations": [op]} for op in operations]
    edits += [{"filename": "facts.md", "action": kind, "content": "# Facts\nNew", "reason": "New", "notes": [1]}
              for kind in ("create", "rewrite")]
    for edit in edits:
        plan = {"file_edits": [edit], "discarded_notes": [], "synthesis": "ok"}
        validator.validate(plan)
        assert not core._normal_output_schema_failures(plan)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**plan, "ignored": True})
    validator.validate({"file_edits": [], "discarded_notes": [{"note": 1, "reason": "already_in_bank"}], "synthesis": "Already represented"})
    for invalid_notes in ([], [0]):
        plan = {"file_edits": [{"filename": "facts.md", "action": "create", "content": "# Facts", "reason": "New", "notes": invalid_notes}], "discarded_notes": [], "synthesis": "ok"}
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(plan)
