"""Useful-memory planner contract; storage transaction stays independently tested."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as module
from tests.test_consolidator_compaction import make_service, _source, _recovery_provider_error


def reply(text, finish="stop"):
    return ChatResult(text=text, configured_model="test", model_evidence="configured_only", finish_reason=finish)


def dated_source():
    return "# Bank\n\n## 2025-01-01\n- OLD_SOURCE_SENTINEL " + "old history " * 150 + "\n## 2026-09-07\n- NEW_SOURCE_SENTINEL: task completed.\n"


async def test_planner_accepts_markdown_without_model_edit_addresses():
    service = make_service()
    service._complete_chat = AsyncMock(return_value=reply("## Current state\nThe task completed; verify the canonical design before further changes."))
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 100, "reference rules")
    assert candidate is not None
    assert candidate.startswith("# Bank\n")
    assert "The task completed" in candidate
    assert "obsolete verbose detail" not in candidate
    assert details["action"] == "edit" and details["operation_reasons"]
    assert service._complete_chat.await_count == 1


async def test_history_is_distilled_before_it_can_reach_current_work():
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[reply("A reusable historical lesson."), reply("## State\nThe latest task completed; there is no pending repeat.")])
    candidate, details = await service._plan_single_file_compaction("progress.md", dated_source(), 100, "rules sentinel")
    assert candidate is not None and details["status"] == "ok"
    calls = service._complete_chat.await_args_list
    assert len(calls) == 2
    first, last = (json.dumps(call.args[0]) for call in calls)
    assert "OLD_SOURCE_SENTINEL" in first and "OLD_SOURCE_SENTINEL" not in last
    assert "A reusable historical lesson." in last and "NEW_SOURCE_SENTINEL" in last
    assert "rules sentinel" in first and "rules sentinel" in last
    assert all(call.kwargs["retry_policy"] == "none" for call in calls)


async def test_original_duplicate_h1s_and_line_endings_are_code_owned():
    source = "prefix\r\n# Root\r\n\r\n" + "first history " * 100 + "\r\n# Root\r\n\r\n" + "second history " * 100
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[reply("# Generated title\nFirst conclusion."), reply("## Another label\nSecond conclusion.")])
    candidate, details = await service._plan_single_file_compaction("facts.md", source, 100, "")
    assert candidate is not None
    assert candidate.startswith("prefix\r\n# Root\r\n")
    assert [h.heading for h in module._strict_compaction_sections(candidate) if h.level == 1] == ["# Root", "# Root"]
    assert not candidate.endswith(("\n", "\r"))
    assert "\n" not in candidate.replace("\r\n", "")


@pytest.mark.parametrize("fault", [
    _recovery_provider_error(), reply("PRIVATE_REJECTED", "length"),
    reply("PRIVATE_REJECTED", "other"), reply("  "),
    reply('{"file_edits": []}'), reply("```markdown\nwrapped\n```"),
    reply("Explanation\n```\nunclosed"),
])
async def test_one_form_correction_never_reuses_rejected_output(fault, caplog):
    service = make_service(max_tokens=200_000, context_window=500_000)
    service._complete_chat = AsyncMock(side_effect=[fault, reply("A useful concise outcome, with a remaining caveat.")])
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 100, "")
    assert candidate is not None
    calls = service._complete_chat.await_args_list
    assert len(calls) == 2
    assert "PRIVATE_REJECTED" not in json.dumps(calls[1].args[0]) + caplog.text
    assert all(call.args[1] == 200_000 for call in calls)


async def test_correction_budget_is_shared_across_history_and_final():
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[reply("bad", "length"), reply("A historical lesson."), reply("bad again", "length")])
    candidate, details = await service._plan_single_file_compaction("progress.md", dated_source(), 100, "")
    assert candidate is None and details["error"] == "compaction_completion_length"
    assert service._complete_chat.await_count == 3


async def test_cancellation_between_stages_propagates():
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[reply("Historical lesson."), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await service._plan_single_file_compaction("progress.md", dated_source(), 100, "")


async def test_useful_small_summary_is_not_rejected_by_a_percentage():
    source = "# Bank\n\n" + "incidental old execution detail " * 500
    service = make_service()
    service._complete_chat = AsyncMock(return_value=reply("## Outcome\nWork completed. Preserve the decision and verify the current design before new changes."))
    candidate, details = await service._plan_single_file_compaction("facts.md", source, 100, "")
    assert candidate is not None and len(candidate.encode()) * 100 < len(source.encode()) * 5
    target, error = module._materialize_prepared_compaction_target(space_id="space-a", source_key="space-a/bank/facts.md", filename="facts.md", source=source, max_size=100, action="edit", result=candidate, reasons=details["operation_reasons"])
    assert error is None and target is not None
    assert module._prepared_compaction_target_error(target, "space-a") is None


@pytest.mark.parametrize("result", ["", " \r\n\t"])
def test_empty_result_still_fails_at_materialization(result):
    target, error = module._materialize_prepared_compaction_target(space_id="space-a", source_key="space-a/bank/facts.md", filename="facts.md", source=_source(), max_size=100, action="edit", result=result, reasons=("Summarize useful memory.",))
    assert target is None and error == "empty_compaction_candidate"


@pytest.mark.parametrize("text", ["[Design](docs/design.md) remains canonical.", "[1] Keep the decision.", "[inferred] Verify the current state."])
def test_summary_can_start_with_markdown_reference(text):
    assert module._compaction_summary_text(text) == (text, None)


def test_prompt_projection_groups_adjacent_records_without_mechanical_metadata():
    source = '# Bank\n## 2026-09-07\n- First\n- Second\n## 2025-01-01\n- Old\n'
    recent, history = module._compaction_partition(source, 100)
    projected = module._compaction_prompt_passages(recent)
    newest = next(item for item in projected if item['date_hint'] == '2026-09-07')
    assert newest['body'] == '- First\n- Second\n'
    assert all(set(item) == {'headings', 'date_hint', 'body'} for item in projected)
    assert ''.join(item['body'] for item in projected) == '- First\n- Second\n- Old\n'


async def test_generated_lessons_are_context_checked_before_the_final_call():
    service = make_service(context_window=10_000)
    service._complete_chat = AsyncMock(return_value=reply('a long historical lesson ' * 2_000))
    candidate, details = await service._plan_single_file_compaction('facts.md', dated_source(), 100, '')
    assert candidate is None and details['error'] == 'compaction_context_exhausted'
    assert service._complete_chat.await_count == 1


@pytest.mark.parametrize('eol', ['\n', '\r\n', '\r'])
def test_renderer_preserves_the_source_terminal_separator(eol):
    source = '# Bank\n' + 'detail' * 30 + eol
    root = module._strict_compaction_sections(source)[0]
    body = module._render_strict_compaction_replacement(source, root, '\nSummary\nSecond line')
    assert body == eol + 'Summary' + eol + 'Second line' + eol


@pytest.mark.parametrize('body', ['x' * 2_000, 'x' * 2_001])
async def test_summary_must_strictly_reduce_the_complete_file(body):
    service = make_service()
    service._complete_chat = AsyncMock(return_value=reply(body))
    source = '# Bank\n\n' + 'x' * 2_000
    candidate, details = await service._plan_single_file_compaction('facts.md', source, 100, '')
    assert candidate is None and details['error'] == 'compaction_not_smaller'
    assert service._complete_chat.await_count == 1


def test_apply_revalidation_rejects_a_consistently_hashed_empty_candidate():
    import dataclasses
    source = _source()
    target, error = module._materialize_prepared_compaction_target(space_id='space-a', source_key='space-a/bank/facts.md', filename='facts.md', source=source, max_size=100, action='edit', result='# Bank\nA useful decision.', reasons=('Summarize useful memory.',))
    assert error is None
    empty = dataclasses.replace(target, result='', result_utf8_bytes=0, expected_result_utf8_bytes=0, result_sha256=module._utf8_sha256(''), expected_result_sha256=module._utf8_sha256(''))
    assert module._prepared_compaction_target_error(empty, 'space-a') == 'empty_compaction_candidate'




def test_recent_selection_uses_heading_dates_not_document_direction():
    source = '# Bank\ncontext\n## 2026-09-07\nnewest statement ' + 'n' * 20 + '\n## 2026-08-01\nold statement ' + 'o' * 20 + '\n'
    current, history = module._compaction_partition(source, recent_bytes=10)
    assert any('newest statement' in unit['body'] for unit in current)
    assert any('old statement' in unit['body'] for unit in history)
    assert any('context' in unit['body'] for unit in current)


def test_latest_date_keeps_current_focus_and_later_entries_together():
    source = (
        '# Bank\n## Current focus\n'
        '- ACTIVE_FOCUS (2026-09-07): release validation is pending.\n'
        '## Execution journal\n'
        '- LATER_DETAIL (2026-09-07): repeated execution detail.\n'
        '- OLD_EVENT (2025-01-01): historical implementation.\n'
    )
    recent, history = module._compaction_partition(source, recent_bytes=1)
    assert {'2026-09-07'} == {u['date_hint'] for u in recent if u['date_hint']}
    assert 'ACTIVE_FOCUS' in ''.join(u['body'] for u in recent)
    assert 'LATER_DETAIL' in ''.join(u['body'] for u in recent)
    assert all(u['date_hint'] == '2025-01-01' for u in history)


def test_recent_boundary_date_includes_its_whole_cohort_in_source_order():
    source = (
        '# Bank\n## 2026-09-08\n- Latest outcome.\n'
        '## 2026-09-07\n- Boundary focus.\n- Large related update ' + 'detail ' * 100 + '\n'
        '## 2025-01-01\n- Historical event.\n'
    )
    recent, history = module._compaction_partition(source, recent_bytes=70)
    assert {u['date_hint'] for u in recent if u['date_hint']} == {'2026-09-08', '2026-09-07'}
    assert 'Large related update' in ''.join(u['body'] for u in recent)
    assert all(u['date_hint'] == '2025-01-01' for u in history)
    units = sorted(recent + history, key=lambda u: u['id'])
    assert ''.join(source[u['start']:u['end']] for u in units) == source


async def test_same_day_focus_reaches_final_generation_without_history_filtering():
    source = (
        '# Bank\n## Current focus\n'
        '- ACTIVE_FOCUS (2026-09-07): release approval is still pending.\n'
        '- LATER_DETAIL (2026-09-07): ' + 'execution detail ' * 80 + '\n'
        '- OLD_EVENT (2025-01-01): ' + 'historical lesson ' * 80 + '\n'
    )
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[
        reply('A reusable historical lesson.'),
        reply('## Current state\nRelease approval is still pending.'),
    ])
    candidate, details = await service._plan_single_file_compaction('activeContext.md', source, 100, '')
    assert candidate is not None and details['status'] == 'ok'
    first, final = (json.dumps(call.args[0]) for call in service._complete_chat.await_args_list)
    assert 'ACTIVE_FOCUS' not in first and 'LATER_DETAIL' not in first
    assert 'ACTIVE_FOCUS' in final and 'LATER_DETAIL' in final
    assert 'OLD_EVENT' in first and 'OLD_EVENT' not in final


def test_date_in_fenced_example_is_not_a_section_or_a_recency_marker():
    source = '# Bank\n## Undated\n```md\n## 2099-01-01\nexample\n```\n## 2026-09-07\nrecent\n## 2025-01-01\nold\n'
    current, history = module._compaction_partition(source, recent_bytes=1)
    units = sorted(current + history, key=lambda unit: unit['id'])
    assert len(units) == 4
    assert any('2099-01-01' in unit['body'] and unit['date_hint'] is None for unit in current)
    assert any('recent' in unit['body'] for unit in current)
    assert any('old' in unit['body'] for unit in history)


def test_partition_is_exhaustive_and_inherits_parent_dates():
    source = '# Bank\r\ncontext Ω\r\n## 2025-01-01\r\nold 😀\r\n### Detail\r\nchild\r\n## 2026-09-07\r\nnew\r\n'
    current, history = module._compaction_partition(source, recent_bytes=1)
    units = sorted(current + history, key=lambda unit: unit['id'])
    assert ''.join(source[unit['start']:unit['end']] for unit in units) == source
    assert len({unit['id'] for unit in units}) == len(units)
    assert next(unit for unit in units if 'child' in unit['body'])['date_hint'] == '2025-01-01'


def test_undated_and_invalid_date_passages_are_not_assumed_obsolete():
    source = '# Bank\nintro\n## Decisions\nstill relevant\n## 2026-99-88\nnot a date\n'
    current, history = module._compaction_partition(source, recent_bytes=1)
    assert history == []
    assert len(current) == 3


def test_dated_list_items_do_not_inherit_an_undated_current_focus_label():
    source = '# Bank\n## Current focus\n- **Done (2026-09-07)**: no notes remain.\n- **Old (2025-01-01)**: drain notes.\n- [ ] Unknown-age task.\n'
    current, history = module._compaction_partition(source, recent_bytes=1)
    assert any('no notes remain' in u['body'] and u['date_hint'] == '2026-09-07' for u in current)
    assert any('drain notes' in u['body'] and u['date_hint'] == '2025-01-01' for u in history)
    assert any('Unknown-age task' in u['body'] and u['date_hint'] is None for u in current)
    units = sorted(current + history, key=lambda u: u['id'])
    assert ''.join(source[u['start']:u['end']] for u in units) == source


def test_list_markers_inside_fences_or_nested_lists_do_not_split_records():
    source = '# Bank\n## 2026-09-07\n- Item\n  - nested (2025-01-01)\n```md\n- fake (2099-01-01)\n```\n- Last\n'
    current, history = module._compaction_partition(source, recent_bytes=1000)
    assert history == []
    record = next(u for u in current if '- Item' in u['body'])
    assert 'nested' in record['body'] and 'fake' in record['body']
    assert record['date_hint'] == '2026-09-07'


@pytest.mark.parametrize('response,error', [
    (SimpleNamespace(text='text', finish_reason='unknown'), 'invalid_compaction_finish_reason'),
    (SimpleNamespace(text=None, finish_reason='stop'), 'invalid_compaction_completion'),
])
async def test_unknown_delivery_records_remain_terminal(response, error):
    # Deliberately inject broken records beyond the normalized ChatResult type.
    service = make_service()
    service._complete_chat = AsyncMock(return_value=response)
    candidate, details = await service._plan_single_file_compaction('facts.md', _source(), 100, '')
    assert candidate is None and details['error'] == error
    assert service._complete_chat.await_count == 1


@pytest.mark.parametrize('body', [
    '## State\n- Example\n    ```text\n    content\n    ```',
    '## State\n<!-- pending verification -->',
    '## State\nPending verification\n---',
    '## State\n<https://example.com/design>',
    '## State\n ## Indented heading\nPending verification.',
    '## State\n##\nPending verification.',
    '## State\n\u200b## Hidden heading\nPending verification.',
])
async def test_compaction_corrects_bodies_rejected_by_the_next_normal_edit(body):
    # This is the downstream consumer's contract, not a second Markdown parser.
    assert module._normal_h1_topology('# Bank\n' + body) is None
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[
        reply(body), reply('## State\nWork completed; verify the next request.'),
    ])
    candidate, details = await service._plan_single_file_compaction(
        'facts.md', _source(), 100, '',
    )
    assert candidate is not None and details['status'] == 'ok'
    assert service._complete_chat.await_count == 2
    updated, failures, _ = module._normal_edit_candidate(candidate, [{
        'type': 'append_to_section', 'heading': '## State',
        'content': 'The next request is now confirmed.',
        'reason': 'New evidence after compaction.', 'notes': [1],
    }], 0)
    assert failures == []
    assert updated is not None and 'next request is now confirmed' in updated


async def test_incompatible_markdown_exhausts_the_existing_correction_budget():
    service = make_service()
    service._complete_chat = AsyncMock(return_value=reply('State\n<!-- private rejected text -->'))
    candidate, details = await service._plan_single_file_compaction(
        'facts.md', _source(), 100, '',
    )
    assert candidate is None
    assert details['error'] == 'invalid_compaction_replacement_structure'
    assert service._complete_chat.await_count == 2
    assert 'private rejected text' not in json.dumps(service._complete_chat.await_args.args[0])


async def test_incompatible_final_markdown_cannot_add_a_second_correction():
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[
        reply('partial lesson', 'length'), reply('A reusable historical lesson.'),
        reply('Current state\n<!-- unsupported region -->'),
    ])
    candidate, details = await service._plan_single_file_compaction(
        'facts.md', dated_source(), 100, '',
    )
    assert candidate is None
    assert details['error'] == 'invalid_compaction_replacement_structure'
    assert service._complete_chat.await_count == 3
