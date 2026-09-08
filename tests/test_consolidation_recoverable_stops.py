"""Recoverable generation faults share the existing two-operation batch budget."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from hivemind_inference.adapters.openai_compatible import OpenAICompatibleChatProvider
from hivemind_inference.records import ChatResult
from tests.test_consolidation_invalid_response_recovery import _setup, _error
from tests.test_note_disposition import SPACE, _plan, _edit
from tests.test_p13_inference_adapters import openai_chat_profile, chat_request


def _completion(plan, *, finish="stop"):
    return ChatResult(text=json.dumps(plan), configured_model="test-model",
        model_evidence="configured_only", finish_reason=finish,
        input_tokens=9, output_tokens=1, total_tokens=10)


@pytest.mark.parametrize("wire_finish", ["length", "unknown-provider-finish", None])
async def test_nonterminal_wire_answer_is_rejected_then_regenerated(monkeypatch, wire_finish):
    storage, notes, service = _setup(monkeypatch)
    before = dict(storage.objects)
    invalid_plan = _plan([_edit([1, 2], content="- DO_NOT_APPLY_FIRST_ANSWER")])
    provider = OpenAICompatibleChatProvider(openai_chat_profile(
        "https://example.test/v1", max_output_tokens=4096))
    raw = json.dumps({"choices": [{"finish_reason": wire_finish,
        "message": {"content": json.dumps(invalid_plan)}}]}).encode()
    try:
        first = provider._normalize_chat_response(raw, chat_request())
    finally:
        await provider.aclose()
    calls = []
    async def complete(messages, output_budget, **kwargs):
        calls.append(messages)
        assert storage.objects == before
        return first if len(calls) == 1 else _completion(
            _plan([_edit([1, 2], content="- verified replacement")]))
    service._complete_chat = complete
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "ok"
    assert len(calls) == 2 and result["notes_deleted"] == 2
    assert calls[1][:-1] == calls[0]
    assert "DO_NOT_APPLY_FIRST_ANSWER" not in json.dumps(calls)
    bank = storage.objects[f"{SPACE}/bank/facts.md"]
    assert "DO_NOT_APPLY_FIRST_ANSWER" not in bank
    assert bank.count("- verified replacement") == 1
    assert all(note not in storage.objects for note in notes)
    if wire_finish == "length":
        assert "compact" in calls[1][-1]["content"]


@pytest.mark.parametrize("first_finish", ["length", "other"])
@pytest.mark.parametrize("second_fault", ["length", "other", "schema", "provider"])
async def test_second_fault_after_nonterminal_answer_never_buys_a_third(
    monkeypatch, first_finish, second_fault,
):
    storage, _, service = _setup(monkeypatch)
    before = dict(storage.objects)
    plan = _plan([_edit([1, 2])])
    first = _completion(plan, finish=first_finish)
    if second_fault == "provider":
        second = _error()
    elif second_fault == "schema":
        second = _completion({"file_edits": []})
    else:
        second = _completion(plan, finish=second_fault)
    service._complete_chat = AsyncMock(side_effect=[first, second, _completion(plan)])
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert service._complete_chat.await_count == 2
    assert result["status"] == "error" and storage.objects == before
    assert result["notes_remaining"] == 2


def _create(content):
    return _plan([{"filename": "new.md", "action": "create", "content": content,
        "reason": "New facts from both notes", "notes": [1, 2]}])


@pytest.mark.parametrize("bad_content", [
    "# New\n\n```python\nx = 1\n",
    "# New\n\n<div>new facts</div>\n",
    "# New\n\nSection\n---\nnew facts\n",
])
async def test_invalid_generated_file_is_a_correctable_model_fault(monkeypatch, bad_content):
    storage, notes, service = _setup(monkeypatch)
    before = dict(storage.objects)
    valid_content = "# New\n\n## Facts\n\nNew facts from both notes.\n"
    # F3: the unchanged dedup gate already refuses these generated structures.
    # Prevalidation changes their recovery classification, not admissibility.
    assert await service._deduplicate_content(bad_content, "new.md") == (
        bad_content, 0, "deduplication_invalid_structure"
    )
    calls = []
    async def complete(messages, output_budget, **kwargs):
        assert storage.objects == before
        calls.append(messages)
        return _completion(_create(bad_content if len(calls) == 1 else valid_content))
    service._complete_chat = complete
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "ok" and len(calls) == 2
    assert "invalid_normal_replacement_structure" in calls[1][-1]["content"]
    assert storage.objects[f"{SPACE}/bank/new.md"] == valid_content
    assert all(note not in storage.objects for note in notes)


@pytest.mark.parametrize("bad_source", ["\n```python\nx = 1\n", "\n<div>old facts</div>\n"])
async def test_invalid_existing_file_is_not_relabelled_as_generated(monkeypatch, bad_source):
    storage, _, service = _setup(monkeypatch)
    storage.objects[f"{SPACE}/bank/facts.md"] += bad_source
    before = dict(storage.objects)
    service._complete_chat = AsyncMock(return_value=_completion(_plan([_edit([1, 2])])))
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert service._complete_chat.await_count == 1
    assert result["status"] == "error" and storage.objects == before
    assert result["notes_remaining"] == 2


async def test_bank_count_failure_after_verified_writes_does_not_abort(monkeypatch, caplog):
    storage, notes, service = _setup(monkeypatch)
    service._complete_chat = AsyncMock(return_value=_completion(_create(
        "# New\n\n## Facts\n\nNew facts from both notes.\n")))
    # This read is only the final count: normal source collection uses
    # list_and_get, and the actual writes/readbacks are exercised unchanged.
    storage.list_objects = AsyncMock(side_effect=RuntimeError("PRIVATE_LIST_ERROR"))
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert storage.list_objects.await_count == 1
    assert result["status"] == "ok"
    assert result["bank_files_created"] == 1
    assert result["bank_files_unchanged"] == 1
    assert result["notes_deleted"] == 2 and result["notes_remaining"] == 0
    assert all(note not in storage.objects for note in notes)
    assert "New facts from both notes." in storage.objects[f"{SPACE}/bank/new.md"]
    assert "file-count refresh failed" in caplog.text
    assert "PRIVATE_LIST_ERROR" not in caplog.text


async def test_excessively_nested_model_json_is_corrected_without_a_generic_exception(
    monkeypatch,
):
    storage, _, service = _setup(monkeypatch)
    before = dict(storage.objects)
    first = ChatResult(text="[" * 2000 + "0" + "]" * 2000,
        configured_model="test-model", model_evidence="configured_only",
        finish_reason="stop", total_tokens=7)
    responses = iter([first, _completion(_plan([_edit([1, 2])]))])
    calls = []
    async def complete(messages, output_budget, **kwargs):
        assert storage.objects == before
        calls.append(messages)
        return next(responses)
    service._complete_chat = complete
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "ok" and len(calls) == 2
    assert result["notes_deleted"] == 2
    assert result["llm_tokens_used"] == 17
    assert "invalid_normal_consolidation_json" in calls[1][-1]["content"]
    assert "[" * 100 not in json.dumps(calls)


async def test_cancellation_of_file_count_still_propagates(monkeypatch):
    import asyncio

    storage, notes, service = _setup(monkeypatch)
    service._complete_chat = AsyncMock(return_value=_completion(_create(
        "# New\n\n## Facts\n\nNew facts from both notes.\n")))
    storage.list_objects = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.consolidate(SPACE, enforce_cooldown=False)
    assert all(note in storage.objects for note in notes)
    assert "New facts from both notes." in storage.objects[f"{SPACE}/bank/new.md"]


@pytest.mark.parametrize("bad_body", [
    "\n```python\nx = 1\n",
    "\n<div>new facts</div>\n",
    "\nSection\n---\nnew facts\n",
])
async def test_malformed_rewrite_already_uses_h1_correction_before_dedup(
    monkeypatch, bad_body,
):
    """F2: rewrite's existing H1 gate rejects malformed model-owned content."""
    storage, notes, service = _setup(monkeypatch)
    before = dict(storage.objects)
    source = storage.objects[f"{SPACE}/bank/facts.md"]
    valid = source + "\nNew facts from both notes.\n"
    calls = []
    async def complete(messages, output_budget, **kwargs):
        assert storage.objects == before
        calls.append(messages)
        return _completion(_plan([{
            "filename": "facts.md", "action": "rewrite",
            "content": source + bad_body if len(calls) == 1 else valid,
            "reason": "Exceptional rewrite with facts from both notes",
            "notes": [1, 2],
        }]))
    service._complete_chat = complete
    dedup = service._deduplicate_content
    service._deduplicate_content = AsyncMock(wraps=dedup)
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "ok" and len(calls) == 2
    assert "normal_h1_not_preserved" in calls[1][-1]["content"]
    service._deduplicate_content.assert_awaited_once_with(valid, "facts.md")
    assert storage.objects[f"{SPACE}/bank/facts.md"] == valid
    assert all(note not in storage.objects for note in notes)


# Owner decision 2026-09-07: bounded transient retry belongs to consolidation1.5.0.
@pytest.mark.parametrize("category", ["timeout", "rate_limited", "unavailable"])
async def test_transient_retry_waits_before_writing_and_reports_resume(monkeypatch, caplog, category):
    caplog.set_level(20, logger="live_mem.consolidator")
    from live_mem.core import consolidator as module
    storage, notes, service = _setup(monkeypatch)
    service._transient_retries = 3
    before = dict(storage.objects)
    progress, waits = [], []
    async def sleep(seconds):
        waits.append(seconds)
        assert storage.objects == before
        assert progress[-1]["phase"] == "batch_retry_wait"
        assert progress[-1]["retry_reason"] == category
        assert progress[-1]["retry_attempt"] == 1
        assert progress[-1]["retry_limit"] == 3
        assert progress[-1]["retry_delay_seconds"] == 60
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    service._complete_chat = AsyncMock(side_effect=[_error(category), _completion(_plan([_edit([1, 2])]))])
    result = await service.consolidate(SPACE, enforce_cooldown=False, progress_callback=progress.append)
    assert result["status"] == "ok" and result["notes_deleted"] == 2
    assert waits == [60]
    calls = service._complete_chat.await_args_list
    assert len(calls) == 2 and calls[0].args == calls[1].args
    assert all(call.kwargs["retry_policy"] == "none" for call in calls)
    assert all(key not in storage.objects for key in notes)
    assert any(p["phase"] == "batch_running" and p.get("retry_attempt") == 1 and p.get("retry_delay_seconds") == 0 for p in progress)
    assert "retry 1/3 in 60s" in caplog.text
    assert "resuming retry 1/3" in caplog.text


@pytest.mark.parametrize("recover", [False, True])
async def test_three_transient_retries_use_exact_delays_and_stop_at_four_calls(monkeypatch, recover):
    from live_mem.core import consolidator as module
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 3
    before, waits = dict(storage.objects), []
    async def sleep(seconds):
        assert storage.objects == before
        waits.append(seconds)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    valid = _completion(_plan([_edit([1, 2])]))
    service._complete_chat = AsyncMock(side_effect=[_error("timeout")] * 3 + [valid if recover else _error("timeout"), valid])
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert waits == [60, 120, 300]
    assert service._complete_chat.await_count == 4
    assert result["status"] == ("ok" if recover else "error")
    if not recover:
        assert storage.objects == before and result["notes_remaining"] == 2


@pytest.mark.parametrize("recover", [False, True])
async def test_transient_budget_is_shared_across_model_correction(monkeypatch, recover):
    from live_mem.core import consolidator as module
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 3
    before, waits = dict(storage.objects), []
    async def sleep(seconds):
        assert storage.objects == before
        waits.append(seconds)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    valid = _completion(_plan([_edit([1, 2])]))
    service._complete_chat = AsyncMock(side_effect=[
        _error("timeout"), _error(), _error("unavailable"), _error("rate_limited"),
        valid if recover else _error("timeout"), valid,
    ])
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert waits == [60, 120, 300] and service._complete_chat.await_count == 5
    assert result["status"] == ("ok" if recover else "error")
    if not recover:
        assert storage.objects == before


async def test_transient_retry_zero_disables_wait_and_retry(monkeypatch):
    from live_mem.core import consolidator as module
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 0
    before = dict(storage.objects)
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    service._complete_chat = AsyncMock(side_effect=_error("timeout"))
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "error" and storage.objects == before
    assert service._complete_chat.await_count == 1 and sleep.await_count == 0


@pytest.mark.parametrize("category,role", [
    ("auth", "chat"), ("quota_exhausted", "chat"), ("content_rejected", "chat"),
    ("invalid_request", "chat"), ("unsupported", "chat"), ("timeout", "embedding"),
])
async def test_transient_retry_never_retries_other_categories_or_roles(monkeypatch, category, role):
    from live_mem.core import consolidator as module
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 3
    before = dict(storage.objects)
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    service._complete_chat = AsyncMock(side_effect=_error(category, role))
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "error" and storage.objects == before
    assert service._complete_chat.await_count == 1 and sleep.await_count == 0


async def test_transient_retry_cancellation_in_wait_preserves_notes(monkeypatch):
    import asyncio
    from live_mem.core import consolidator as module
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 3
    before = dict(storage.objects)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    service._complete_chat = AsyncMock(side_effect=_error("timeout"))
    with pytest.raises(asyncio.CancelledError):
        await service.consolidate(SPACE, enforce_cooldown=False)
    assert storage.objects == before and service._complete_chat.await_count == 1


async def test_transient_retry_exhaustion_keeps_previous_batch_committed(monkeypatch):
    from live_mem.core import consolidator as module
    storage, notes, service = _setup(monkeypatch, count=4)
    service._transient_retries = 3
    originals = {key: storage.objects[key] for key in notes}
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    service._complete_chat = AsyncMock(side_effect=[
        _completion(_plan([_edit([1, 2], content="- verified prefix")]))] + [_error("timeout")] * 4)
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "partial" and result["notes_deleted"] == 2
    assert result["notes_remaining"] == 2 and result["failed_batch"] == 2
    assert service._complete_chat.await_count == 5
    assert all(key not in storage.objects for key in notes[:2])
    assert all(storage.objects[key] == originals[key] for key in notes[2:])
    assert storage.objects[f"{SPACE}/bank/facts.md"].count("- verified prefix") == 1


async def test_transient_retry_policy_reaches_actual_provider_request(monkeypatch):
    from types import SimpleNamespace
    from live_mem.core import consolidator as module, inference_runtime
    storage, _, service = _setup(monkeypatch)
    service._transient_retries = 3
    requests = []
    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise _error("timeout")
            return _completion(_plan([_edit([1, 2])]))
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: SimpleNamespace(chat_provider=lambda: Provider()))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] == "ok"
    assert len(requests) == 2 and all(r.retry_policy == "none" for r in requests)
    from dataclasses import replace
    assert requests[0].correlation_id != requests[1].correlation_id
    assert replace(requests[0], correlation_id=requests[1].correlation_id) == requests[1]


async def test_transient_retry_does_not_replay_a_failed_bank_write(monkeypatch):
    from live_mem.core import consolidator as module
    storage, notes, service = _setup(monkeypatch)
    service._transient_retries = 3
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    service._complete_chat = AsyncMock(return_value=_completion(_plan([_edit([1, 2])])))
    original_put = storage.put
    async def failed_put(key, content, content_type="text/plain"):
        await original_put(key, content, content_type)
        if key.endswith("/bank/facts.md"):
            raise _error("timeout")  # Similar category, different persistence boundary.
    storage.put = failed_put
    result = await service.consolidate(SPACE, enforce_cooldown=False)
    assert result["status"] in {"error", "partial"}
    assert all(key in storage.objects for key in notes)
    assert service._complete_chat.await_count == 1 and sleep.await_count == 0


def test_transient_retry_default_env_and_service_wiring(monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from live_mem.config import Settings
    from live_mem.core import consolidator as module, inference_runtime
    monkeypatch.delenv("CONSOLIDATION_TRANSIENT_RETRIES", raising=False)
    assert Settings(_env_file=None).consolidation_transient_retries == 3
    monkeypatch.setenv("CONSOLIDATION_TRANSIENT_RETRIES", "2")
    settings = Settings(_env_file=None)
    assert settings.consolidation_transient_retries == 2
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: SimpleNamespace(config=SimpleNamespace(chat=None)))
    assert module.ConsolidatorService()._transient_retries == 2
    assert "CONSOLIDATION_TRANSIENT_RETRIES=3" in (
        Path(__file__).resolve().parents[1] / ".env.example"
    ).read_text()


@pytest.mark.parametrize("count", [-1, 4, 100])
def test_transient_retry_setting_rejects_unbounded_values(count):
    from live_mem.config import Settings
    with pytest.raises(ValueError, match="CONSOLIDATION_TRANSIENT_RETRIES"):
        Settings(_env_file=None, consolidation_transient_retries=count)
