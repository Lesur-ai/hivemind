"""Manual work is observable without changing compaction execution or authority."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_mem.auth.context import current_token_info
from live_mem.core.consolidation_queue import (
    ConsolidationQueueService, get_consolidation_queue,
    reset_consolidation_queue_for_tests,
)
from live_mem.core.locks import get_lock_manager
from live_mem.core.write_sink import DirectLocalWriteSink
from tests.test_consolidation_queue import _bank_tool, _token
from tests.test_write_sink import WriteSinkFakeStorage


@pytest.mark.asyncio
async def test_manual_running_then_completed_is_independent_of_consolidation():
    queue = ConsolidationQueueService()
    started, release = asyncio.Event(), asyncio.Event()
    report = {"status": "ok", "files_total": 6, "files_over_limit": 1,
              "total_size_before": 51000, "total_size_after": 20000,
              "preimage_id": "project/capture", "content": "PRIVATE BANK TEXT"}

    async def compact():
        started.set()
        await release.wait()
        return report

    task = asyncio.create_task(queue.observe_manual_compaction("project", "operator", compact))
    try:
        await asyncio.wait_for(started.wait(), 1)
        lane = await queue.get_space_summary("project")
        job = lane["manual_compaction"]
        assert job["status"] == "running" and job["kind"] == "manual_compaction"
        assert job["started_at"] and job["finished_at"] is None
        assert job["requested_by"] == "operator"
        assert job["progress"] == {"phase": "compacting"}
        assert job["scope_label"] == "Manual compaction"
        assert "Async consolidation" not in job["message"]
        assert lane["running_job"] is None and lane["latest_jobs"] == []
        assert lane["lane_state"] == "idle"
        assert await queue.get_space_readiness_summary("project") == {"running_job_id": "", "queued_count": 0}
        assert (await queue.get_job(job["job_id"]))["status"] == "running"
        release.set()
        assert await task is report  # the synchronous command's report is unchanged
        finished = (await queue.get_space_summary("project"))["manual_compaction"]
        assert finished["job_id"] == job["job_id"]
        assert finished["status"] == "succeeded" and finished["finished_at"]
        assert finished["result"]["total_size_after"] == 20000
        assert finished["result"]["files_over_limit"] == 1
        assert "PRIVATE BANK TEXT" not in str(finished)
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "error", "conflict"])
async def test_non_ok_report_is_terminal_failure(status):
    queue = ConsolidationQueueService()
    report = {"status": status, "recovery_required": True, "failure_reason": "apply_failed"}
    assert await queue.observe_manual_compaction("project", "operator", AsyncMock(return_value=report)) is report
    job = (await queue.get_space_summary("project"))["manual_compaction"]
    assert job["status"] == "failed" and job["finished_at"]
    assert job["result"]["recovery_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE PROMPT TOKEN"), asyncio.CancelledError("PRIVATE PROMPT TOKEN")])
async def test_exception_or_cancellation_cannot_leave_running_or_leak(failure):
    queue = ConsolidationQueueService()
    with pytest.raises(type(failure)):
        await queue.observe_manual_compaction("project", "operator", AsyncMock(side_effect=failure))
    job = (await queue.get_space_summary("project"))["manual_compaction"]
    assert job["status"] == "failed" and job["finished_at"]
    assert job["result"]["recovery_required"] is True
    assert "PRIVATE PROMPT TOKEN" not in str(job)


@pytest.mark.asyncio
async def test_history_is_bounded_and_other_spaces_are_isolated():
    queue = ConsolidationQueueService(max_history=2)
    operation = AsyncMock(return_value={"status": "ok"})
    await queue.observe_manual_compaction("project", "operator", operation)
    first = (await queue.get_space_summary("project"))["manual_compaction"]["job_id"]
    await queue.observe_manual_compaction("other", "operator", operation)
    await queue.observe_manual_compaction("project", "operator", operation)
    assert len(queue._jobs) == 2
    assert (await queue.get_job(first))["status"] == "not_found"
    assert (await queue.get_space_summary("unknown"))["manual_compaction"] is None
    assert (await queue.get_space_summary("other"))["manual_compaction"]["space_id"] == "other"


@pytest.mark.asyncio
async def test_actual_task_cancellation_terminates_the_record():
    queue = ConsolidationQueueService()
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(queue.observe_manual_compaction("project", "operator", operation))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = (await queue.get_space_summary("project"))["manual_compaction"]
    assert job["status"] == "failed" and job["finished_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_terminal_transition_cannot_wait_on_a_contended_state_lock(raises):
    queue = ConsolidationQueueService()
    started, release = asyncio.Event(), asyncio.Event()
    report = {"status": "ok", "files_total": 1, "files_over_limit": 1}
    failure = RuntimeError("PRIVATE PROMPT")

    async def operation():
        started.set()
        await release.wait()
        if raises:
            raise failure
        return report

    task = asyncio.create_task(queue.observe_manual_compaction("project", "operator", operation))
    await asyncio.wait_for(started.wait(), 1)
    await queue._state_lock.acquire()
    try:
        release.set()
        await asyncio.sleep(0)
        terminal_without_wait = task.done()
        if not terminal_without_wait:
            task.cancel()  # exercise the old finalization cancellation window
        outcome, = await asyncio.gather(task, return_exceptions=True)
    finally:
        queue._state_lock.release()
    assert terminal_without_wait
    assert outcome is (failure if raises else report)
    job = (await queue.get_space_summary("project"))["manual_compaction"]
    assert job["status"] == ("failed" if raises else "succeeded")
    assert job["finished_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,rollback,status", [
    ("compaction_prepare_failed", "not_needed", "error"),
    ("compaction_apply_reverted", "verified", "error"),
    ("compaction_recovery_unverified", "unverified", "partial"),
])
async def test_real_compactor_recovery_report_keeps_safe_diagnostics(reason, rollback, status):
    queue = ConsolidationQueueService()
    report = {"status": status, "space_id": "project", "dry_run": False,
              "files_total": 6, "files_over_limit": 1, "total_size_before": 51000,
              "total_size_after": None, "failure_reason": reason,
              "failed_phase": "prepare" if rollback == "not_needed" else "apply",
              "rollback_outcome": rollback,
              "files": [{"filename": "PRIVATE BANK", "error": "PRIVATE PROMPT"}],
              "failures": [{"error": "PRIVATE PROMPT"}]}
    if status == "partial":
        report.update(apply_may_have_mutated=True, files_applied_before_failure=1,
                      recovery_required=True, preimage_id="project/capture")
    assert await queue.observe_manual_compaction("project", "operator", AsyncMock(return_value=report)) is report
    result = (await queue.get_space_summary("project"))["manual_compaction"]["result"]
    for key in ("failed_phase", "rollback_outcome", "apply_may_have_mutated", "files_applied_before_failure"):
        if key in report:
            assert result[key] == report[key]
    assert "PRIVATE" not in str(result)
    assert "phase" not in result and "files_compacted" not in result


@pytest.mark.asyncio
async def test_observation_does_not_run_or_block_consolidation_worker(monkeypatch):
    queue = ConsolidationQueueService()
    manual_started, release, consolidation_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def consolidate(*args, **kwargs):
        consolidation_started.set()
        return {"status": "ok", "notes_processed": 0}

    monkeypatch.setattr("live_mem.core.consolidation_queue.get_consolidator", lambda: SimpleNamespace(consolidate=consolidate))
    monkeypatch.setattr("live_mem.core.consolidation_queue.assert_space_not_reserved", AsyncMock())

    async def compact():
        manual_started.set()
        await release.wait()
        return {"status": "ok"}

    async def manual_request():
        async with get_lock_manager().consolidation("coexist"):
            return await queue.observe_manual_compaction("coexist", "operator", compact)

    task = asyncio.create_task(manual_request())
    try:
        await asyncio.wait_for(manual_started.wait(), 1)
        queued = await queue.enqueue("coexist", "agent", "agent")
        lane = await queue.get_space_summary("coexist")
        assert lane["running_job"]["job_id"] == queued["job_id"]
        assert lane["manual_compaction"]["status"] == "running"
        assert not consolidation_started.is_set()
        release.set()
        await task
        await asyncio.wait_for(consolidation_started.wait(), 1)
        worker = queue._workers.get("coexist")
        if worker:
            await worker
        assert (await queue.get_job(queued["job_id"]))["status"] == "succeeded"
        assert (await queue.get_space_summary("coexist"))["manual_compaction"]["status"] == "succeeded"
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_invalid_compaction_result_cannot_leave_running():
    queue = ConsolidationQueueService()
    with pytest.raises(TypeError):
        await queue.observe_manual_compaction("project", "operator", AsyncMock(return_value=None))
    assert (await queue.get_space_summary("project"))["manual_compaction"]["status"] == "failed"


@pytest.fixture
def tool_context(monkeypatch):
    reset_consolidation_queue_for_tests()
    compact = AsyncMock(return_value={"status": "ok", "space_id": "project"})
    engine = SimpleNamespace(write_sink=DirectLocalWriteSink(storage=WriteSinkFakeStorage()),
                             compact_bank=compact, _tool_compaction_authority=lambda _: nullcontext())
    registry = SimpleNamespace(mid_engine=AsyncMock(return_value=engine))
    monkeypatch.setattr("live_mem.core.engines.get_engine_registry", lambda: registry)
    token = current_token_info.set(_token("operator", ["read", "manage"]))
    yield compact, registry
    current_token_info.reset(token)
    reset_consolidation_queue_for_tests()


@pytest.mark.asyncio
async def test_bank_compact_publishes_running_from_the_actual_locked_request(tool_context):
    compact, _ = tool_context
    started, release = asyncio.Event(), asyncio.Event()

    async def operation(*args, **kwargs):
        assert get_lock_manager().consolidation("project").locked()
        started.set()
        await release.wait()
        return {"status": "ok", "space_id": "project"}

    compact.side_effect = operation
    task = asyncio.create_task(_bank_tool("bank_compact")("project", dry_run=False))
    try:
        await asyncio.wait_for(started.wait(), 1)
        job = (await get_consolidation_queue().get_space_summary("project"))["manual_compaction"]
        assert job["status"] == "running"
        status = await _bank_tool("bank_consolidation_status")(job["job_id"])
        assert status["requested_by"] == "operator"
        release.set()
        assert (await task)["status"] == "ok"
        assert (await get_consolidation_queue().get_job(job["job_id"]))["status"] == "succeeded"
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_dry_run_and_lock_conflict_do_not_create_work(tool_context, monkeypatch):
    compact, _ = tool_context
    scan = AsyncMock(return_value={"status": "ok", "dry_run": True})
    monkeypatch.setattr("live_mem.core.consolidator.get_consolidator", lambda: SimpleNamespace(compact_bank=scan))
    assert (await _bank_tool("bank_compact")("project", dry_run=True))["status"] == "ok"
    async with get_lock_manager().consolidation("project"):
        assert (await _bank_tool("bank_compact")("project", dry_run=False))["status"] == "conflict"
    assert (await get_consolidation_queue().get_space_summary("project"))["manual_compaction"] is None
    compact.assert_not_called()


@pytest.mark.asyncio
async def test_manage_and_route_refusals_create_no_work(tool_context):
    compact, registry = tool_context
    token = current_token_info.set(_token("reader", ["read"]))
    try:
        assert (await _bank_tool("bank_compact")("project", dry_run=False))["status"] == "error"
    finally:
        current_token_info.reset(token)
    registry.mid_engine.return_value.write_sink = object()
    assert (await _bank_tool("bank_compact")("project", dry_run=False))["status"] != "ok"
    assert (await get_consolidation_queue().get_space_summary("project"))["manual_compaction"] is None
    compact.assert_not_called()


@pytest.mark.asyncio
async def test_manual_job_status_keeps_space_access_gate(tool_context):
    await _bank_tool("bank_compact")("project", dry_run=False)
    job = (await get_consolidation_queue().get_space_summary("project"))["manual_compaction"]
    token = current_token_info.set({**_token("outsider", ["read"]), "allowed_resources": ["other"]})
    try:
        result = await _bank_tool("bank_consolidation_status")(job["job_id"])
        assert result["status"] == "error" and "job_id" not in result
    finally:
        current_token_info.reset(token)
