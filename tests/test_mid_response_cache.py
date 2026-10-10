"""Fresh MID operations escape a gateway caching invalid HTTP-200 responses."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from hivemind_inference.adapters.openai_compatible import OpenAICompatibleChatProvider
from hivemind_inference.errors import InferenceError
from hivemind_inference.records import ChatMessage, ChatRequest
from live_mem.core import inference_runtime
from tests.fakes.inference_emulator import InferenceEmulator, openai_chat_payload
from tests.test_consolidation_invalid_response_recovery import _setup, _body
from tests.test_note_disposition import SPACE, _plan, _edit
from tests.test_p13_inference_adapters import openai_chat_profile as chat_profile


class ResponseCache:
    """Cache by actual wire messages, including invalid responses, like the incident."""

    def __init__(self, storage, *, fail_fresh=2, echo_nonce=False):
        self.storage = storage
        self.before = dict(storage.objects)
        self.entries = {}
        self.requests = []
        self.hits = []
        self.fail_fresh = fail_fresh
        self.echo_nonce = echo_nonce

    async def request(self, *args, **kwargs):
        body = copy.deepcopy(kwargs["json_body"])
        self.requests.append(body)
        key = json.dumps(body["messages"], sort_keys=True)
        hit = key in self.entries
        self.hits.append(hit)
        if not hit:
            if self.fail_fresh:
                self.fail_fresh -= 1
                result = _body(None, reasoning="PRIVATE_REASONING")
            else:
                content = "- fresh fact"
                if self.echo_nonce:
                    content += body["messages"][-1]["content"].split("nonce=", 1)[1].strip()
                result = _body(json.dumps(_plan([_edit([1, 2], content=content)])))
            self.entries[key] = result
        assert self.storage.objects == self.before, "no writes before a usable result"
        return 200, {"x-cache": "hit" if hit else "miss"}, self.entries[key]


def _bind(monkeypatch, service, gateway):
    provider = OpenAICompatibleChatProvider(chat_profile(
        "https://example.test/v1", max_output_tokens=4096,
    ))
    monkeypatch.setattr(provider, "_request", gateway.request)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda:
        SimpleNamespace(chat_provider=lambda: provider))
    return provider


async def test_failed_job_then_same_job_generates_fresh_and_preserves_failed_state(monkeypatch):
    storage, notes, service = _setup(monkeypatch)
    gateway = ResponseCache(storage)
    provider = _bind(monkeypatch, service, gateway)
    try:
        first = await service.consolidate(SPACE, enforce_cooldown=False)
        assert first["status"] == "error"
        assert storage.objects == gateway.before
        assert len(gateway.requests) == 2
        second = await service.consolidate(SPACE, enforce_cooldown=False)
    finally:
        await provider.aclose()
    assert second["status"] == "ok"
    assert len(gateway.requests) == 3
    assert gateway.hits == [False, False, False]
    assert all(note not in storage.objects for note in notes)
    assert "- fresh fact" in storage.objects[f"{SPACE}/bank/facts.md"]
    assert all("nonce=" not in value for value in storage.objects.values())
    first_wire, correction_wire, second_wire = gateway.requests
    # Initial messages are otherwise byte-identical between failed/restarted jobs.
    assert first_wire["messages"][:-1] == second_wire["messages"][:-1]
    assert first_wire["messages"][-1]["content"].split("\n\nRequest metadata", 1)[0] == (
        second_wire["messages"][-1]["content"].split("\n\nRequest metadata", 1)[0])
    assert "invalid_normal_provider_response" in correction_wire["messages"][-1]["content"]


async def test_nonce_recopied_into_valid_plan_never_reaches_bank_or_deletes_notes(monkeypatch):
    storage, notes, service = _setup(monkeypatch)
    gateway = ResponseCache(storage, fail_fresh=0, echo_nonce=True)
    provider = _bind(monkeypatch, service, gateway)
    try:
        result = await service.consolidate(SPACE, enforce_cooldown=False)
    finally:
        await provider.aclose()
    assert result["status"] == "error"
    assert len(gateway.requests) == 2
    assert storage.objects == gateway.before
    assert all(note in storage.objects for note in notes)


async def test_suffix_preserves_input_and_prefix_and_uses_unique_correlation_id(monkeypatch):
    storage, _, service = _setup(monkeypatch)
    gateway = ResponseCache(storage, fail_fresh=0)
    provider = _bind(monkeypatch, service, gateway)
    messages = [{"role": "system", "content": "instructions"},
                {"role": "user", "content": "original input"},
                {"role": "assistant", "content": "previous answer"}]
    original = copy.deepcopy(messages)
    try:
        for _ in range(2):
            await service._complete_chat(messages, 4000, retry_policy="none")
    finally:
        await provider.aclose()
    assert messages == original
    assert gateway.hits == [False, False]
    for body in gateway.requests:
        assert body["messages"][0] == original[0]
        assert body["messages"][2] == original[2]
        assert body["messages"][1]["content"].startswith("original input\n\nRequest metadata")
        assert body["max_tokens"] == 4000


async def test_suffix_is_included_in_final_context_budget(monkeypatch):
    storage, _, service = _setup(monkeypatch)
    service._context_window = 60
    gateway = ResponseCache(storage, fail_fresh=0)
    provider = _bind(monkeypatch, service, gateway)
    try:
        await service._complete_chat([{"role": "user", "content": "input"}], 60,
                                     retry_policy="none")
    finally:
        await provider.aclose()
    wire = gateway.requests[0]
    estimated_input = sum(len(m["content"]) for m in wire["messages"]) // 4
    assert estimated_input + wire["max_tokens"] <= 60
    assert wire["max_tokens"] > 0


@pytest.mark.parametrize("messages", [
    [{"role": "system", "content": "no user"}],
    [{"role": "user", "content": "x" * 240}],
])
async def test_no_user_or_exhausted_context_refuses_before_egress(monkeypatch, messages):
    storage, _, service = _setup(monkeypatch)
    service._context_window = 60
    gateway = ResponseCache(storage, fail_fresh=0)
    provider = _bind(monkeypatch, service, gateway)
    try:
        with pytest.raises(InferenceError) as excinfo:
            await service._complete_chat(messages, 1, retry_policy="none")
    finally:
        await provider.aclose()
    assert excinfo.value.category == "invalid_request"
    assert gateway.requests == []


@pytest.mark.parametrize("header,expected", [
    ("HIT", "hit"), ("miss", "miss"), (None, "unknown"),
    ("PRIVATE_HEADER", "unknown"),
])
async def test_cache_status_logs_closed_values_on_valid_and_invalid_answers(
    caplog, header, expected,
):
    headers = {"x-cache-fp": "PRIVATE_FINGERPRINT"}
    if header is not None:
        headers["x-cache"] = header
    body = openai_chat_payload(None, usage={"completion_tokens": 0})
    body["choices"][0]["message"]["reasoning"] = "PRIVATE_REASONING"
    with caplog.at_level("INFO", logger="hivemind_inference.adapters"):
        async with InferenceEmulator([
            {"headers": headers, "body": openai_chat_payload("usable")},
            {"headers": headers, "body": body},
        ]) as emulator:
            provider = OpenAICompatibleChatProvider(chat_profile(emulator.v1_url))
            request = ChatRequest((ChatMessage("user", "PRIVATE_PROMPT"),),
                                  timeout_seconds=2, retry_policy="none")
            try:
                assert (await provider.complete(request)).text == "usable"
                with pytest.raises(InferenceError) as excinfo:
                    await provider.complete(request)
            finally:
                await provider.aclose()
    assert excinfo.value.category == "invalid_response"
    assert len(emulator.requests) == 2
    assert caplog.text.count(f"cache={expected}") == 2
    assert "diagnostic=reasoning_only" in caplog.text
    assert all(value not in caplog.text for value in (
        "PRIVATE_HEADER", "PRIVATE_FINGERPRINT", "PRIVATE_REASONING", "PRIVATE_PROMPT"))


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_reasoning_only_never_becomes_success(field, caplog):
    body = openai_chat_payload(None, usage={"completion_tokens": 0})
    body["choices"][0]["message"][field] = "PRIVATE_REASONING"
    provider = OpenAICompatibleChatProvider(chat_profile("https://example.test/v1"))
    request = ChatRequest((ChatMessage("user", "prompt"),), timeout_seconds=2)
    try:
        with pytest.raises(InferenceError) as excinfo:
            provider._normalize_chat_response(json.dumps(body).encode(), request)
    finally:
        await provider.aclose()
    assert excinfo.value.category == "invalid_response"
    assert "diagnostic=reasoning_only" in caplog.text
    assert "PRIVATE_REASONING" not in caplog.text


@pytest.mark.parametrize("change", ["tool_calls", "no_reasoning", "nonzero_output", "boolean_output", "length"])
async def test_other_invalid_shapes_keep_invalid_content_diagnostic(change, caplog):
    body = openai_chat_payload(None, usage={"completion_tokens": 0})
    message = body["choices"][0]["message"]
    message["reasoning"] = "private"
    if change == "tool_calls":
        message["tool_calls"] = [{"id": "call-1"}]
    elif change == "no_reasoning":
        message["reasoning"] = "  "
    elif change == "nonzero_output":
        body["usage"]["completion_tokens"] = 1
    elif change == "boolean_output":
        body["usage"]["completion_tokens"] = False
    else:
        body["choices"][0]["finish_reason"] = "length"
    provider = OpenAICompatibleChatProvider(chat_profile("https://example.test/v1"))
    request = ChatRequest((ChatMessage("user", "prompt"),), timeout_seconds=2)
    try:
        with pytest.raises(InferenceError) as excinfo:
            provider._normalize_chat_response(json.dumps(body).encode(), request)
    finally:
        await provider.aclose()
    assert excinfo.value.category == "invalid_response"
    assert "diagnostic=invalid_content" in caplog.text
    assert "diagnostic=reasoning_only" not in caplog.text
