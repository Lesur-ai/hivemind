"""HTTP200 invalid content must use the existing bounded application recovery."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hivemind_inference.adapters.openai_compatible import OpenAICompatibleChatProvider
from hivemind_inference.errors import InferenceError
from live_mem.core import inference_runtime
from tests.test_note_disposition import (
    SPACE, RecordingStorage, _seed_bank, _seed_notes, _service,
    _bind_direct_local, _plan, _edit,
)
from tests.test_p13_inference_adapters import openai_chat_profile as chat_profile


def _body(content, **message_fields):
    return json.dumps({
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": content, **message_fields,
        }}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    }).encode()


def _error(category="invalid_response", role="chat"):
    return InferenceError(category=category, role=role,
        provider_id="openai-compatible", adapter_id="openai-compatible",
        retryable=False, correlation_id="a" * 32)


def _setup(monkeypatch, count=2):
    storage = RecordingStorage()
    _seed_bank(storage)
    notes = _seed_notes(storage, count)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    return storage, notes, service


@pytest.mark.parametrize("content", [None, [], {"text": "not a valid wire answer"}])
async def test_invalid_http200_recovers_before_any_write_and_continues(
    monkeypatch, caplog, content,
):
    storage, notes, service = _setup(monkeypatch, count=4)
    before = dict(storage.objects)
    provider = OpenAICompatibleChatProvider(chat_profile(
        "https://example.test/v1", max_output_tokens=4096,
    ))
    responses = iter([
        _body(content, reasoning_content="PRIVATE_REASONING_NOT_A_PLAN"),
        _body(json.dumps(_plan([_edit([1, 2], content="- first batch")]))),
        _body(json.dumps(_plan([_edit([1, 2], content="- next batch")]))),
    ])
    requests = []
    async def request(*args, **kwargs):
        requests.append(kwargs["json_body"])
        if len(requests) <= 2:
            assert storage.objects == before, "invalid response must not cause a write"
        return 200, {}, next(responses)
    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda:
        SimpleNamespace(chat_provider=lambda: provider))
    try:
        result = await service.consolidate(SPACE, enforce_cooldown=False)
    finally:
        await provider.aclose()
    assert result["status"] == "ok"
    assert len(requests) == 3  # batch1 initial + recovery, batch2 initial
    assert result["notes_deleted"] == 4 and result["notes_remaining"] == 0
    assert all(note not in storage.objects for note in notes)
    bank = storage.objects[f"{SPACE}/bank/facts.md"]
    assert bank.count("- first batch") == bank.count("- next batch") == 1
    assert requests[1]["messages"][:-1] == requests[0]["messages"]
    assert requests[1]["model"] == requests[0]["model"]
    assert "invalid_normal_provider_response" in requests[1]["messages"][-1]["content"]
    assert "PRIVATE_REASONING_NOT_A_PLAN" not in json.dumps(requests)
    assert "PRIVATE_REASONING_NOT_A_PLAN" not in caplog.text
    assert "usage unavailable" in caplog.text
    assert result["llm_tokens_used"] == 20  # only the two available usage reports


@pytest.mark.parametrize("outcomes", [
    [_error(), _error()],
    [_error(), _body("not JSON")],
    [_body("not JSON"), _error()],
])
async def test_mixed_second_failure_never_gets_a_third_attempt_or_changes_state(
    monkeypatch, outcomes,
):
    storage, notes, service = _setup(monkeypatch)
    before = dict(storage.objects)
    provider = OpenAICompatibleChatProvider(chat_profile(
        "https://example.test/v1", max_output_tokens=4096))
    calls = []
    async def complete(request):
        calls.append(request)
        response = outcomes[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return provider._normalize_chat_response(response, request)
    monkeypatch.setattr(provider, "complete", complete)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda:
        SimpleNamespace(chat_provider=lambda: provider))
    try:
        result = await service.consolidate(SPACE, enforce_cooldown=False)
    finally:
        await provider.aclose()
    assert len(calls) == 2
    assert result["status"] == "error"
    assert result["notes_remaining"] == 2
    assert storage.objects == before
    assert all(note in storage.objects for note in notes)


@pytest.mark.parametrize("error", [
    _error(category) for category in (
        "auth", "quota_exhausted", "rate_limited", "timeout", "unsupported",
        "invalid_request", "content_rejected", "unavailable",
    )
] + [_error(role="embedding"), RuntimeError("PRIVATE_EXCEPTION")])
async def test_other_provider_failures_remain_terminal(monkeypatch, error):
    storage, _, service = _setup(monkeypatch)
    before = dict(storage.objects)
    service._complete_chat = AsyncMock(side_effect=error)
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert service._complete_chat.await_count == 1
    assert result["status"] == "error" and storage.objects == before
    assert "PRIVATE_EXCEPTION" not in repr(result)


async def test_cancellation_during_recovery_propagates_without_writes(monkeypatch):
    storage, _, service = _setup(monkeypatch)
    before = dict(storage.objects)
    service._complete_chat = AsyncMock(side_effect=[_error(), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await service.consolidate(SPACE, enforce_cooldown=False)
    assert service._complete_chat.await_count == 2
    assert storage.objects == before


async def test_exhausted_recovery_finalizes_only_the_previous_verified_batch(monkeypatch):
    from hivemind_inference.records import ChatResult

    storage, notes, service = _setup(monkeypatch, count=4)
    original_notes = {key: storage.objects[key] for key in notes}
    first = ChatResult(text=json.dumps(_plan([_edit([1, 2], content="- verified prefix")])),
        configured_model="test-model", model_evidence="configured_only",
        finish_reason="stop", input_tokens=9, output_tokens=1, total_tokens=10)
    service._complete_chat = AsyncMock(side_effect=[first, _error(), _error()])
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert service._complete_chat.await_count == 3
    assert result["status"] == "partial" and result["failed_batch"] == 2
    assert result["notes_deleted"] == 2 and result["notes_remaining"] == 2
    assert result["notes_applied"] == [1, 2]
    assert all(key not in storage.objects for key in notes[:2])
    assert {key: storage.objects[key] for key in notes[2:]} == {
        key: original_notes[key] for key in notes[2:]}
    assert storage.objects[f"{SPACE}/bank/facts.md"].count("- verified prefix") == 1
    assert result["llm_tokens_used"] == 10
