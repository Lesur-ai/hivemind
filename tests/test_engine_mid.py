# -*- coding: utf-8 -*-
"""
Tests for P3-6 (issue #55) — MidEngine adapter over the consolidation surface
(ConsolidatorService + ConsolidationQueueService).

Deterministic and offline: delegation is verified with fakes (no real
ConsolidatorService — its __init__ builds AsyncOpenAI + httpx); the queue
semantics test uses the REAL ConsolidationQueueService with a gating
FakeConsolidator (the established test_consolidation_queue.py idiom). No real S3
/ network / LLM.

What is verified (Wave-2 WRAP-DON'T-REWRITE contract):
- consolidate / enqueue_consolidation / get_job / get_space_summary /
  compact_bank delegate VERBATIM (identical args + identical returned object).
- consolidate forwards progress_callback and keeps enforce_cooldown=True default.
- enqueue_consolidation forwards (space_id, agent, requested_by) unchanged so the
  queue's agent-coalescing branch is preserved (agent='' distinct).
- default write_sink is DirectLocalWriteSink.
- per-space single-writer (one worker per space, FIFO, in_memory_best_effort) is
  preserved by hand-off to the queue singleton (MidEngine adds no worker/lock).
- the durable-mutation call-site enumeration (the #8/#9 deliverable) lists the
  full consolidator + bank-tool set, anchored semantically.

The injected WriteSink is HELD but NOT consumed in Wave-2.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from live_mem.core.consolidation_queue import (
    ConsolidationQueueService,
    QUEUE_GUARANTEE,
    reset_consolidation_queue_for_tests,
)
from live_mem.core.consolidator import ConsolidatorService
from live_mem.core.engines.mid import (
    WRITE_SINK_MUTATION_CALL_SITES,
    MidEngine,
)
from live_mem.core.write_sink import DirectLocalWriteSink, WriteSink
from tests.test_write_sink import WriteSinkFakeStorage


@pytest.fixture(autouse=True)
def _no_real_s3_for_default_sink():
    """Guarantee the default ``DirectLocalWriteSink()`` (constructed whenever a
    MidEngine is built without an explicit sink) never builds a real boto3 client.

    ``DirectLocalWriteSink.__init__`` resolves ``get_storage()`` when no storage
    is injected; offline that constructs a real ``StorageService`` and raises on
    the empty S3 endpoint. Patching the write_sink namespace keeps the
    default-sink path deterministic and offline. The consolidator / queue are
    injected as fakes, so no real LLM/queue singleton is ever built either.
    """
    with patch(
        "live_mem.core.write_sink.get_storage", return_value=WriteSinkFakeStorage()
    ):
        yield


# =============================================================================
# Fakes — record exact delegation without building a real LLM client.
# =============================================================================


class FakeConsolidator:
    """Records consolidate / compact_bank calls verbatim and returns a sentinel
    result object (identity-checked to prove pass-through, no transformation)."""

    def __init__(self) -> None:
        self.consolidate_calls: list[dict] = []
        self.compact_calls: list[dict] = []
        self.consolidate_result = {"status": "ok", "notes_processed": 3, "_sentinel": object()}
        self.compact_result = {"status": "ok", "_sentinel": object()}

    async def consolidate(
        self,
        space_id,
        agent="",
        enforce_cooldown=True,
        progress_callback=None,
        note_keys=None,
    ) -> dict:
        self.consolidate_calls.append(
            {
                "space_id": space_id,
                "agent": agent,
                "enforce_cooldown": enforce_cooldown,
                "progress_callback": progress_callback,
                "note_keys": note_keys,
            }
        )
        if progress_callback is not None:
            maybe = progress_callback({"phase": "batch_running"})
            if inspect.isawaitable(maybe):
                await maybe
        return self.consolidate_result

    async def compact_bank(self, space_id, dry_run=True) -> dict:
        self.compact_calls.append({"space_id": space_id, "dry_run": dry_run})
        return self.compact_result


class FakeQueue:
    """Records queue delegation verbatim and returns sentinel objects."""

    def __init__(self) -> None:
        self.enqueue_calls: list[tuple] = []
        self.get_job_calls: list[str] = []
        self.summary_calls: list[str] = []
        self.enqueue_result = {"status": "running", "job_id": "consol_x", "_s": object()}
        self.get_job_result = {"status": "running", "job_id": "consol_x"}
        self.summary_result = {"space_id": "s", "lane_state": "running"}

    async def enqueue(self, space_id, agent, requested_by) -> dict:
        self.enqueue_calls.append((space_id, agent, requested_by))
        return self.enqueue_result

    async def get_job(self, job_id) -> dict:
        self.get_job_calls.append(job_id)
        return self.get_job_result

    async def get_space_summary(self, space_id) -> dict:
        self.summary_calls.append(space_id)
        return self.summary_result


class LegacySignatureConsolidator:
    """Injected pre-#163 consolidator without the optional note_keys kwarg."""

    async def consolidate(
        self,
        space_id,
        agent="",
        enforce_cooldown=True,
        progress_callback=None,
    ) -> dict:
        return {
            "status": "ok",
            "space_id": space_id,
            "agent": agent,
            "enforce_cooldown": enforce_cooldown,
            "progress_callback": progress_callback,
        }

    async def compact_bank(self, space_id, dry_run=True) -> dict:
        return {"status": "ok", "space_id": space_id, "dry_run": dry_run}


# =============================================================================
# Delegation — consolidate
# =============================================================================


@pytest.mark.asyncio
async def test_mid_engine_consolidate_delegates_verbatim() -> None:
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    result = await engine.consolidate("space-a", agent="agent-x", enforce_cooldown=False)

    assert result is fake.consolidate_result  # identity: no transformation
    assert fake.consolidate_calls == [
        {
            "space_id": "space-a",
            "agent": "agent-x",
            "enforce_cooldown": False,
            "progress_callback": None,
            "note_keys": None,
        }
    ]


@pytest.mark.asyncio
async def test_mid_engine_consolidate_default_enforce_cooldown_true() -> None:
    """Default enforce_cooldown stays True (matches ConsolidatorService)."""
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    await engine.consolidate("space-a")

    assert fake.consolidate_calls[0]["enforce_cooldown"] is True
    assert fake.consolidate_calls[0]["agent"] == ""


@pytest.mark.asyncio
async def test_mid_engine_progress_callback_forwarded() -> None:
    """A progress_callback passed to the engine is forwarded and invoked by the
    wrapped consolidate."""
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    seen: list[dict] = []

    async def cb(progress: dict) -> None:
        seen.append(progress)

    await engine.consolidate("space-a", progress_callback=cb)

    assert fake.consolidate_calls[0]["progress_callback"] is cb
    assert seen == [{"phase": "batch_running"}]


@pytest.mark.asyncio
async def test_mid_engine_exact_note_allowlist_forwarded() -> None:
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())
    selected = ["space-a/live/old.md"]

    await engine.consolidate("space-a", note_keys=selected)

    assert fake.consolidate_calls[0]["note_keys"] is selected


@pytest.mark.asyncio
async def test_mid_engine_omits_new_kwarg_for_legacy_injected_consolidator() -> None:
    engine = MidEngine(consolidator=LegacySignatureConsolidator(), queue=FakeQueue())

    result = await engine.consolidate("space-a")

    assert result["status"] == "ok"
    assert result["space_id"] == "space-a"


# =============================================================================
# Delegation — queue (enqueue / get_job / get_space_summary)
# =============================================================================


@pytest.mark.asyncio
async def test_mid_engine_enqueue_delegates_to_queue() -> None:
    fakeq = FakeQueue()
    engine = MidEngine(consolidator=FakeConsolidator(), queue=fakeq)

    result = await engine.enqueue_consolidation("space-a", "agent-x", "requester")

    assert result is fakeq.enqueue_result
    assert fakeq.enqueue_calls == [("space-a", "agent-x", "requester")]


@pytest.mark.asyncio
async def test_mid_engine_enqueue_forwards_empty_agent_unchanged() -> None:
    """agent='' must reach queue.enqueue UNCHANGED (no coercion) so the queue's
    coalescing branch keeps agent='' distinct from a named agent."""
    fakeq = FakeQueue()
    engine = MidEngine(consolidator=FakeConsolidator(), queue=fakeq)

    await engine.enqueue_consolidation("space-a", "", "maintainer")
    await engine.enqueue_consolidation("space-a", "agent-a", "agent-a")

    assert fakeq.enqueue_calls == [
        ("space-a", "", "maintainer"),
        ("space-a", "agent-a", "agent-a"),
    ]


@pytest.mark.asyncio
async def test_mid_engine_get_job_and_space_summary_passthrough() -> None:
    fakeq = FakeQueue()
    engine = MidEngine(consolidator=FakeConsolidator(), queue=fakeq)

    job = await engine.get_job("consol_x")
    summary = await engine.get_space_summary("space-a")

    assert job is fakeq.get_job_result
    assert summary is fakeq.summary_result
    assert fakeq.get_job_calls == ["consol_x"]
    assert fakeq.summary_calls == ["space-a"]


# =============================================================================
# Delegation — compact_bank
# =============================================================================


@pytest.mark.asyncio
async def test_mid_engine_compact_bank_delegates() -> None:
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    result = await engine.compact_bank("space-a", dry_run=True)

    assert result is fake.compact_result
    assert fake.compact_calls == [{"space_id": "space-a", "dry_run": True}]


@pytest.mark.asyncio
async def test_mid_engine_compact_bank_default_dry_run_true() -> None:
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    await engine.compact_bank("space-a")

    assert fake.compact_calls[0]["dry_run"] is True


# =============================================================================
# Constructor DI — default sink + injected held verbatim
# =============================================================================


def test_mid_engine_default_write_sink_is_direct_local() -> None:
    """MidEngine() default sink is DirectLocalWriteSink (lazy: no real S3/LLM at
    construction; consolidator/queue defaults are patched to avoid singletons)."""
    storage = WriteSinkFakeStorage()
    with patch(
        "live_mem.core.engines.mid.get_consolidator", return_value=FakeConsolidator()
    ), patch(
        "live_mem.core.engines.mid.get_consolidation_queue", return_value=FakeQueue()
    ), patch(
        "live_mem.core.write_sink.get_storage", return_value=storage
    ):
        engine = MidEngine()
    assert isinstance(engine.write_sink, DirectLocalWriteSink)
    assert isinstance(engine.write_sink, WriteSink)


def test_mid_engine_accepts_injected_dependencies() -> None:
    fake = FakeConsolidator()
    fakeq = FakeQueue()
    storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage=storage)

    engine = MidEngine(consolidator=fake, queue=fakeq, write_sink=sink)

    assert engine._consolidator is fake
    assert engine._queue is fakeq
    assert engine.write_sink is sink


# =============================================================================
# Signature parity + async contract
# =============================================================================


def test_mid_engine_consolidate_signature_matches_consolidator() -> None:
    eng = inspect.signature(MidEngine.consolidate).parameters
    leg = inspect.signature(ConsolidatorService.consolidate).parameters
    assert list(eng) == list(leg)
    assert eng["agent"].default == ""
    assert eng["enforce_cooldown"].default is True
    assert eng["progress_callback"].default is None


def test_mid_engine_compact_bank_signature_matches_consolidator() -> None:
    eng = inspect.signature(MidEngine.compact_bank).parameters
    leg = inspect.signature(ConsolidatorService.compact_bank).parameters
    assert list(eng) == list(leg)
    assert eng["dry_run"].default is True


def test_mid_engine_methods_are_async() -> None:
    for name in (
        "consolidate",
        "enqueue_consolidation",
        "get_job",
        "get_space_summary",
        "compact_bank",
    ):
        assert inspect.iscoroutinefunction(getattr(MidEngine, name)), name


# =============================================================================
# Queue semantics unchanged — one-worker-per-space FIFO via the REAL queue
# =============================================================================


@pytest.mark.asyncio
async def test_mid_engine_queue_semantics_unchanged() -> None:
    """Routing enqueue through MidEngine over the REAL ConsolidationQueueService
    preserves: one worker per space, FIFO ordering, in_memory_best_effort
    guarantee — MidEngine reimplements none of it (pure hand-off)."""
    reset_consolidation_queue_for_tests()

    class GatingFake:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def consolidate(
            self, space_id, agent="", enforce_cooldown=True, progress_callback=None
        ) -> dict:
            self.calls.append(agent)
            self.started.set()
            await self.release.wait()
            return {"status": "ok", "space_id": space_id}

    gate = GatingFake()
    queue = ConsolidationQueueService()
    # MidEngine holds the SAME queue instance; the worker uses get_consolidator()
    # (the queue's own seam), patched to the gating fake.
    engine = MidEngine(consolidator=FakeConsolidator(), queue=queue)

    try:
        with patch(
            "live_mem.core.consolidation_queue.get_consolidator", return_value=gate
        ):
            first = await engine.enqueue_consolidation("project", "agent-a", "agent-a")
            await asyncio.wait_for(gate.started.wait(), timeout=1)
            second = await engine.enqueue_consolidation("project", "agent-b", "agent-b")

            # Single-writer-per-space: first runs, second queued (not parallel).
            assert first["status"] == "running"
            assert first["guarantee"] == QUEUE_GUARANTEE
            assert second["status"] == "queued"
            assert second["queue_position"] == 2
            assert second["guarantee"] == QUEUE_GUARANTEE

            gate.release.set()
            for _ in range(50):
                s1 = await engine.get_job(first["job_id"])
                s2 = await engine.get_job(second["job_id"])
                if s1["status"] == "succeeded" and s2["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)

            summary = await engine.get_space_summary("project")
    finally:
        reset_consolidation_queue_for_tests()

    # FIFO: agent-a processed before agent-b.
    assert gate.calls == ["agent-a", "agent-b"]
    assert summary["parallelism_model"] == "one_worker_per_space"
    assert summary["guarantee"] == QUEUE_GUARANTEE


# =============================================================================
# WriteSink call-site enumeration — the #8/#9 deliverable
# =============================================================================


def test_mid_engine_documents_writesink_mutation_call_sites() -> None:
    """Executable enumeration: MidEngine documents the FULL eventual WriteSink
    mutation set (consolidator + bank-tool branches), anchored on SEMANTIC
    descriptions so #8/#9 inherit the authoritative list. Line numbers are
    hints; we assert membership semantically, not on raw integers."""
    sites = WRITE_SINK_MUTATION_CALL_SITES
    assert len(sites) >= 10

    # Two branches present.
    branches = {s["branch"] for s in sites}
    assert branches == {"consolidator", "bank_tool"}

    # Only the four durable-mutation ops appear (mirrors the WriteSink ABC).
    ops = {s["op"] for s in sites}
    assert ops <= {"put", "put_json", "delete", "delete_many"}

    # ---- CONSOLIDATOR branch coverage (semantic, not line-pinned) -----------
    consolidator_sites = [s for s in sites if s["branch"] == "consolidator"]
    descs = " | ".join(s["description"].lower() for s in consolidator_sites)
    keys = " | ".join(s["key_pattern"] for s in consolidator_sites)
    methods = {s["method"] for s in consolidator_sites}

    # bank/* PUT (create/replace/merge) in _write_results
    assert "{space_id}/bank/{filename}" in keys
    # _synthesis.md PUT
    assert any("_synthesis.md" in s["key_pattern"] for s in consolidator_sites)
    # _meta.json put_json (both _write_results AND consolidate epilogue)
    meta_json = [s for s in consolidator_sites if s["key_pattern"].endswith("_meta.json")]
    assert {s["method"] for s in meta_json} == {
        "ConsolidatorService._write_results",
        "ConsolidatorService.consolidate",
    }
    assert all(s["op"] == "put_json" for s in meta_json)
    # unicode-dup delete + notes delete_many (notes deleted LAST for atomicity)
    assert "unicode" in descs
    assert any(s["op"] == "delete_many" for s in consolidator_sites)
    assert "atomicity" in descs
    # compact_bank bank PUT
    assert "ConsolidatorService.compact_bank" in methods

    # ---- BANK-TOOL branch coverage ------------------------------------------
    bank_sites = [s for s in sites if s["branch"] == "bank_tool"]
    bank_methods = {s["method"] for s in bank_sites}
    assert bank_methods == {"bank_repair", "bank_write", "bank_delete"}
    assert all(s["module"] == "live_mem.tools.bank" for s in bank_sites)
    # bank_delete is the destructive direct multi-key delete path (delete_many).
    assert any(
        s["method"] == "bank_delete" and s["op"] == "delete_many" for s in bank_sites
    )
    # bank_compact must NOT be recorded as a direct bank-tool delete: it delegates
    # to ConsolidatorService.compact_bank (enumerated in the consolidator branch).
    assert "bank_compact" not in bank_methods

    # long_* / graph_push is NEVER a WriteSink write (downstream-derived).
    all_text = " ".join(
        f"{s['module']} {s['method']} {s['description']}" for s in sites
    ).lower()
    assert "graph_push" not in all_text
    assert "long_" not in all_text
