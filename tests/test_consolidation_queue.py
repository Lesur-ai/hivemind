# -*- coding: utf-8 -*-
"""
Tests for issue #20 — asynchronous in-memory consolidation queue.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.core.consolidation_queue import (
    ConsolidationQueueService,
    QUEUE_GUARANTEE,
    reset_consolidation_queue_for_tests,
)
from live_mem.tools.bank import register as register_bank_tools
from tests.test_write_sink import WriteSinkFakeStorage


class _NonHiveStorageGate:
    """Context manager pointing the two get_storage seams the P3-7 route-first
    gate touches at one empty offline fake.

    bank_consolidate now resolves the per-space WriteSink route BEFORE enqueue
    (fail-closed-routing gate). An empty store has no _hivemind/ state, so
    resolve_write_route -> DIRECT_LOCAL and the enqueue proceeds exactly as
    before. The gate reads storage via (a) live_mem.core.engines.get_storage
    (the registry's _storage_dep, for the route check) and, for DIRECT_LOCAL with
    no DI-injected storage, builds DirectLocalWriteSink() which resolves
    (b) live_mem.core.write_sink.get_storage. Both must be patched to keep the
    check deterministic and offline (no boto3)."""

    def __init__(self) -> None:
        self.fake = WriteSinkFakeStorage()
        self._patches = [
            patch("live_mem.core.engines.get_storage", return_value=self.fake),
            patch("live_mem.core.write_sink.get_storage", return_value=self.fake),
        ]

    def __enter__(self) -> "_NonHiveStorageGate":
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> bool:
        for p in reversed(self._patches):
            p.stop()
        return False


class FakeConsolidator:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def consolidate(
        self,
        space_id: str,
        agent: str = "",
        enforce_cooldown: bool = True,
        progress_callback=None,
    ) -> dict:
        self.calls.append(
            {
                "space_id": space_id,
                "agent": agent,
                "enforce_cooldown": enforce_cooldown,
            }
        )
        self.started.set()
        if progress_callback:
            await progress_callback(
                {
                    "phase": "batch_running",
                    "batch_size": 2,
                    "notes_total": 4,
                    "notes_done": 2,
                    "batches_total": 2,
                    "batches_done": 1,
                    "current_batch": 2,
                }
            )
        await self.release.wait()
        return {
            "status": "ok",
            "space_id": space_id,
            "notes_processed": 1,
            "batch_size": 2,
            "batches_total": 2,
            "batches_completed": 2,
        }


def _token(name: str, permissions: list[str]) -> dict:
    return {
        "client_name": name,
        "permissions": permissions,
        "allowed_resources": ["project"],
    }


def _bank_tool(name: str):
    mcp = FastMCP(name="test")
    register_bank_tools(mcp)
    tool = mcp._tool_manager._tools[name]
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    raise AssertionError(f"Tool {name} has no callable")


def test_bank_consolidate_schema_distinguishes_null_default_from_blank_global():
    mcp = FastMCP(name="schema")
    register_bank_tools(mcp)
    agent_schema = mcp._tool_manager._tools["bank_consolidate"].parameters[
        "properties"
    ]["agent"]

    assert agent_schema["default"] is None
    assert {variant["type"] for variant in agent_schema["anyOf"]} == {
        "string",
        "null",
    }
    assert "explicit empty string" in agent_schema["description"]


def _assert_no_auto_polling_contract(payload: dict) -> None:
    assert payload["next_action"] == "return_to_user_without_polling"
    assert payload["polling"]["recommended"] is False
    assert payload["polling"]["mode"] == "manual_only"
    assert payload["polling"]["status_tool"] == "bank_consolidation_status"


@pytest.fixture(autouse=True)
def reset_queue():
    reset_consolidation_queue_for_tests()
    yield
    reset_consolidation_queue_for_tests()


@pytest.mark.asyncio
async def test_enqueue_first_job_returns_running_and_processes_without_cooldown():
    fake = FakeConsolidator()
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=fake,
    ):
        result = await queue.enqueue("project", "agent-a", "agent-a")
        assert result["status"] == "running"
        assert result["queue_position"] == 1
        assert result["guarantee"] == QUEUE_GUARANTEE
        _assert_no_auto_polling_contract(result)

        await asyncio.wait_for(fake.started.wait(), timeout=1)
        fake.release.set()
        for _ in range(20):
            status = await queue.get_job(result["job_id"])
            if status["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)

    assert status["status"] == "succeeded"
    assert status["scope"] == "agent"
    assert status["scope_label"] == "Agent: agent-a"
    assert status["progress"]["batch_size"] == 2
    assert status["progress"]["batches_done"] == 2
    assert fake.calls == [
        {
            "space_id": "project",
            "agent": "agent-a",
            "enforce_cooldown": False,
        }
    ]


@pytest.mark.asyncio
async def test_same_space_second_request_is_queued_not_conflict_and_fifo():
    fake = FakeConsolidator()
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=fake,
    ):
        first = await queue.enqueue("project", "agent-a", "agent-a")
        await asyncio.wait_for(fake.started.wait(), timeout=1)
        second = await queue.enqueue("project", "agent-b", "agent-b")

        assert first["status"] == "running"
        assert second["status"] == "queued"
        assert second["queue_position"] == 2
        _assert_no_auto_polling_contract(first)
        _assert_no_auto_polling_contract(second)

        fake.release.set()
        await asyncio.sleep(0.05)
        first_status = await queue.get_job(first["job_id"])
        second_status = await queue.get_job(second["job_id"])

    assert first_status["status"] == "succeeded"
    assert second_status["status"] == "succeeded"
    assert [call["agent"] for call in fake.calls] == ["agent-a", "agent-b"]


@pytest.mark.asyncio
async def test_different_spaces_start_independently():
    calls = []
    release = asyncio.Event()

    class ParallelFake:
        async def consolidate(
            self, space_id, agent="", enforce_cooldown=True, progress_callback=None
        ):
            calls.append(space_id)
            await release.wait()
            return {"status": "ok", "space_id": space_id}

    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=ParallelFake(),
    ):
        await queue.enqueue("space-a", "agent-a", "agent-a")
        await queue.enqueue("space-b", "agent-b", "agent-b")

        for _ in range(20):
            if set(calls) == {"space-a", "space-b"}:
                break
            await asyncio.sleep(0.01)
        release.set()
        await asyncio.sleep(0)

    assert set(calls) == {"space-a", "space-b"}


@pytest.mark.asyncio
async def test_space_summary_exposes_lane_model_and_queued_jobs():
    fake = FakeConsolidator()
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=fake,
    ):
        first = await queue.enqueue("project", "", "maintainer")
        await asyncio.wait_for(fake.started.wait(), timeout=1)
        second = await queue.enqueue("project", "agent-a", "agent-a")
        summary = await queue.get_space_summary("project")
        fake.release.set()

    assert summary["space_id"] == "project"
    assert summary["lane_state"] == "running"
    assert summary["parallelism_model"] == "one_worker_per_space"
    assert summary["running_job"]["job_id"] == first["job_id"]
    assert summary["running_job"]["scope"] == "all_agents"
    assert summary["queued_count"] == 1
    assert summary["queued_jobs"][0]["job_id"] == second["job_id"]
    assert summary["queued_jobs"][0]["scope_label"] == "Agent: agent-a"


@pytest.mark.asyncio
async def test_pending_same_agent_job_is_coalesced():
    fake = FakeConsolidator()
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=fake,
    ):
        await queue.enqueue("project", "agent-a", "agent-a")
        await asyncio.wait_for(fake.started.wait(), timeout=1)

        second = await queue.enqueue("project", "agent-a", "agent-a")
        third = await queue.enqueue("project", "agent-a", "agent-a")

        fake.release.set()
        for _ in range(20):
            second_status = await queue.get_job(second["job_id"])
            if second_status["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)

    assert second["status"] == "queued"
    assert third["job_id"] == second["job_id"]
    assert second_status["status"] == "succeeded"


@pytest.mark.asyncio
async def test_failed_job_status_exposes_error():
    class FailingConsolidator:
        async def consolidate(
            self, space_id, agent="", enforce_cooldown=True, progress_callback=None
        ):
            return {"status": "error", "message": "LLM unavailable"}

    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=FailingConsolidator(),
    ):
        result = await queue.enqueue("project", "agent-a", "agent-a")
        for _ in range(20):
            status = await queue.get_job(result["job_id"])
            if status["status"] == "failed":
                break
            await asyncio.sleep(0.01)

    assert status["status"] == "failed"
    assert status["error"] == "LLM unavailable"
    assert status["result"]["status"] == "error"


@pytest.mark.asyncio
async def test_bank_consolidate_rejects_read_token_before_enqueue():
    tok = current_token_info.set(_token("reader", ["read"]))
    try:
        with patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(space_id="project")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error"
    assert "write" in result["message"]
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_write_token_auto_scopes_omitted_agent_to_caller():
    tok = current_token_info.set(_token("alice", ["read", "write"]))
    try:
        with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(return_value={"status": "running", "job_id": "j1"}),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(space_id="project")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "running"
    # The route-first gate (DIRECT_LOCAL) does NOT change the enqueue call: same
    # kwargs as before P3-7.
    enqueue.assert_awaited_once_with(
        space_id="project",
        agent="alice",
        requested_by="alice",
    )


@pytest.mark.asyncio
async def test_write_token_with_empty_identity_fails_closed_before_enqueue():
    tok = current_token_info.set(_token("", ["read", "write"]))
    try:
        with patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(space_id="project")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error"
    assert "client_name" in result["message"]
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_write_token_explicit_self_scope_is_allowed():
    tok = current_token_info.set(_token("alice", ["read", "write"]))
    try:
        with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(return_value={"status": "running", "job_id": "j1"}),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(
                space_id="project",
                agent="alice",
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "running"
    enqueue.assert_awaited_once_with(
        space_id="project",
        agent="alice",
        requested_by="alice",
    )


@pytest.mark.asyncio
async def test_write_token_cannot_enqueue_other_agent_scope():
    tok = current_token_info.set(_token("alice", ["read", "write"]))
    try:
        result = await _bank_tool("bank_consolidate")(
            space_id="project",
            agent="bob",
        )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error"
    assert "manage" in result["message"]


@pytest.mark.asyncio
async def test_write_token_cannot_enqueue_explicit_global_scope():
    tok = current_token_info.set(_token("alice", ["read", "write"]))
    try:
        with patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(
                space_id="project",
                agent="",
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error"
    assert "manage" in result["message"]
    enqueue.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        ["read", "write", "manage"],
        ["read", "write", "manage", "admin"],
    ],
    ids=["manage", "admin"],
)
async def test_privileged_token_omitted_agent_still_scopes_to_caller(permissions):
    tok = current_token_info.set(_token("maintainer", permissions))
    try:
        with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(return_value={"status": "running", "job_id": "j1"}),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(space_id="project")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "running"
    enqueue.assert_awaited_once_with(
        space_id="project",
        agent="maintainer",
        requested_by="maintainer",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        ["read", "write", "manage"],
        ["read", "write", "manage", "admin"],
    ],
    ids=["manage", "admin"],
)
async def test_privileged_token_explicit_blank_agent_enqueues_global_scope(permissions):
    tok = current_token_info.set(_token("maintainer", permissions))
    try:
        with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(return_value={"status": "running", "job_id": "j1"}),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(
                space_id="project",
                agent="",
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "running"
    enqueue.assert_awaited_once_with(
        space_id="project",
        agent="",
        requested_by="maintainer",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        ["read", "write", "manage"],
        ["read", "write", "manage", "admin"],
    ],
    ids=["manage", "admin"],
)
async def test_privileged_token_explicit_other_agent_enqueues_that_scope(permissions):
    tok = current_token_info.set(_token("maintainer", permissions))
    try:
        with _NonHiveStorageGate(), patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.enqueue",
            new=AsyncMock(return_value={"status": "running", "job_id": "j1"}),
        ) as enqueue:
            result = await _bank_tool("bank_consolidate")(
                space_id="project",
                agent="alice",
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "running"
    enqueue.assert_awaited_once_with(
        space_id="project",
        agent="alice",
        requested_by="maintainer",
    )


@pytest.mark.asyncio
async def test_bank_consolidation_status_requires_space_read_access():
    tok = current_token_info.set(_token("reader", ["read"]))
    try:
        with patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.get_job",
            new=AsyncMock(
                return_value={
                    "status": "queued",
                    "job_id": "consol_1",
                    "space_id": "other-space",
                }
            ),
        ):
            result = await _bank_tool("bank_consolidation_status")(job_id="consol_1")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error"
    assert "Access denied" in result["message"]


@pytest.mark.asyncio
async def test_bank_consolidation_queues_returns_accessible_lane_summaries():
    tok = current_token_info.set(_token("reader", ["read"]))
    try:
        with patch(
            "live_mem.core.consolidation_queue.ConsolidationQueueService.get_space_summary",
            new=AsyncMock(
                return_value={
                    "space_id": "project",
                    "lane_state": "idle",
                    "parallelism_model": "one_worker_per_space",
                    "running_job": None,
                    "queued_count": 0,
                    "queued_jobs": [],
                    "latest_jobs": [],
                    "service_config": {"batch_size": 5},
                }
            ),
        ) as summary:
            result = await _bank_tool("bank_consolidation_queues")(
                space_ids="project,other-space"
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "ok"
    assert result["total_spaces"] == 1
    assert result["lanes"][0]["space_id"] == "project"
    assert result["parallelism_model"] == "one_worker_per_space"
    assert result["denied_spaces"][0]["space_id"] == "other-space"
    summary.assert_awaited_once_with("project")


# ─────────────────────────────────────────────────────────────
# P12-1 — Terminal queue status and progress phase honesty
# ─────────────────────────────────────────────────────────────


class _OutcomeConsolidator:
    """Returns (or raises) a fixed consolidation outcome."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def consolidate(
        self, space_id, agent="", enforce_cooldown=True, progress_callback=None
    ):
        if progress_callback:
            await progress_callback(
                {
                    "phase": "batch_running",
                    "batch_size": 1,
                    "notes_total": 2,
                    "notes_done": 1,
                    "batches_total": 2,
                    "batches_done": 1,
                    "current_batch": 2,
                }
            )
        if self._error is not None:
            raise self._error
        return dict(self._result)


async def _terminal_status(queue, job_id, expected):
    for _ in range(50):
        status = await queue.get_job(job_id)
        if status["status"] == expected:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"job {job_id} never reached {expected}: {status['status']}"
    )


@pytest.mark.asyncio
async def test_partial_result_marks_job_failed_with_terminal_failed_phase():
    consolidator = _OutcomeConsolidator(
        result={
            "status": "partial",
            "message": "Consolidation partielle",
            "failure_reason": "batch_llm_failed",
            "failed_batch": 2,
            "notes_processed": 1,
            "batch_size": 1,
            "batches_total": 2,
            "batches_completed": 1,
        }
    )
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=consolidator,
    ):
        accepted = await queue.enqueue("project", "agent-a", "agent-a")
        status = await _terminal_status(queue, accepted["job_id"], "failed")

    assert status["status"] == "failed"
    assert status["progress"]["phase"] == "failed"
    assert status["result"]["status"] == "partial"
    assert status["result"]["failed_batch"] == 2
    assert status["result"]["failure_reason"] == "batch_llm_failed"


@pytest.mark.asyncio
async def test_error_result_marks_job_failed_with_terminal_failed_phase():
    consolidator = _OutcomeConsolidator(
        result={
            "status": "error",
            "message": "La consolidation a échoué avant toute écriture durable",
            "failure_reason": "batch_llm_failed",
            "failed_batch": 1,
            "notes_processed": 0,
            "batch_size": 1,
            "batches_total": 2,
            "batches_completed": 0,
        }
    )
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=consolidator,
    ):
        accepted = await queue.enqueue("project", "agent-a", "agent-a")
        status = await _terminal_status(queue, accepted["job_id"], "failed")

    assert status["status"] == "failed"
    assert status["progress"]["phase"] == "failed"
    assert status["result"]["failed_batch"] == 1


@pytest.mark.asyncio
async def test_ok_result_keeps_terminal_done_phase():
    consolidator = _OutcomeConsolidator(
        result={
            "status": "ok",
            "notes_processed": 2,
            "batch_size": 1,
            "batches_total": 2,
            "batches_completed": 2,
        }
    )
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=consolidator,
    ):
        accepted = await queue.enqueue("project", "agent-a", "agent-a")
        status = await _terminal_status(queue, accepted["job_id"], "succeeded")

    assert status["status"] == "succeeded"
    assert status["progress"]["phase"] == "done"


@pytest.mark.asyncio
async def test_worker_crash_marks_job_failed_with_terminal_failed_phase():
    # The injected detail mimics provider/storage diagnostics that must
    # NEVER reach a read-authorized client through bank_consolidation_status.
    secret_detail = (
        "boto3 EndpointConnectionError https://sk-SECRET:token@s3.internal:9000"
    )
    consolidator = _OutcomeConsolidator(error=RuntimeError(secret_detail))
    queue = ConsolidationQueueService()

    with patch(
        "live_mem.core.consolidation_queue.get_consolidator",
        return_value=consolidator,
    ):
        accepted = await queue.enqueue("project", "agent-a", "agent-a")
        status = await _terminal_status(queue, accepted["job_id"], "failed")

    assert status["status"] == "failed"
    assert status["progress"]["phase"] == "failed"
    assert status["result"]["status"] == "error"
    # P12-1 (Codex review): raw exception text stays server-side. The client
    # payload carries only a generic message and a stable failure reason.
    import json as _json

    assert "sk-SECRET" not in _json.dumps(status)
    assert "s3.internal" not in _json.dumps(status)
    assert status["result"]["failure_reason"] == "consolidation_crashed"
    assert "server logs" in status["error"]
