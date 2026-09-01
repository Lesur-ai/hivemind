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
from pathlib import Path
from unittest.mock import patch

import pytest

from live_mem.core import consolidator as consolidator_module
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
        self.compact_calls.append(
            {
                "space_id": space_id,
                "dry_run": dry_run,
                "direct_local_sink": consolidator_module._bound_direct_local_compaction_sink(
                    space_id
                ),
            }
        )
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
async def test_mid_engine_consolidate_never_binds_a_stale_manual_authority() -> None:
    """Only manual compact_bank may consume the one-route capability.

    A registry-built engine can be retained while a space moves to STAGED or
    REFUSE.  ``consolidate`` must leave the old DirectLocal proof unbound so
    the real service re-resolves the lifecycle route at execution time.
    """

    class ContextRecordingConsolidator:
        def __init__(self) -> None:
            self.bound_sink: DirectLocalWriteSink | None = None

        async def consolidate(self, space_id, **_kwargs) -> dict:
            self.bound_sink = consolidator_module._bound_direct_local_compaction_sink(
                space_id
            )
            return {"status": "ok"}

    storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage)
    fake = ContextRecordingConsolidator()
    engine = MidEngine(
        consolidator=fake,
        queue=FakeQueue(),
        write_sink=sink,
        direct_local_compaction_authority=(
            consolidator_module._issue_direct_local_compaction_authority(
                "space-a", sink
            )
        ),
    )

    result = await engine.consolidate("space-a", enforce_cooldown=False)

    assert result == {"status": "ok"}
    assert fake.bound_sink is None


@pytest.mark.asyncio
async def test_manual_compaction_authority_cannot_escape_to_a_child_task() -> None:
    """A child task must not retain a tool route proof after the tool exits."""

    storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage)
    authority = consolidator_module._issue_direct_local_compaction_authority(
        "space-a", sink
    )
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def child() -> DirectLocalWriteSink | None:
        child_started.set()
        await release_child.wait()
        return consolidator_module._bound_direct_local_compaction_sink("space-a")

    with consolidator_module._direct_local_compaction_authority(authority):
        task = asyncio.create_task(child())
        await child_started.wait()
        assert (
            consolidator_module._bound_direct_local_compaction_sink("space-a")
            is sink
        )

    release_child.set()
    assert await task is None


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
    assert fake.compact_calls == [
        {"space_id": "space-a", "dry_run": True, "direct_local_sink": None}
    ]


@pytest.mark.asyncio
async def test_mid_engine_compact_bank_default_dry_run_true() -> None:
    fake = FakeConsolidator()
    engine = MidEngine(consolidator=fake, queue=FakeQueue())

    await engine.compact_bank("space-a")

    assert fake.compact_calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_mid_engine_compact_bank_forwards_its_resolved_sink_on_apply() -> None:
    fake = FakeConsolidator()
    storage = WriteSinkFakeStorage()
    sink = DirectLocalWriteSink(storage)
    engine = MidEngine(
        consolidator=fake,
        queue=FakeQueue(),
        write_sink=sink,
        direct_local_compaction_authority=(
            consolidator_module._issue_direct_local_compaction_authority(
                "space-a", sink
            )
        ),
    )

    with engine._tool_compaction_authority("space-a"):
        result = await engine.compact_bank("space-a", dry_run=False)

    assert result is fake.compact_result
    assert fake.compact_calls == [
        {"space_id": "space-a", "dry_run": False, "direct_local_sink": sink}
    ]


@pytest.mark.asyncio
async def test_mid_engine_compact_bank_does_not_reuse_a_retained_authority() -> None:
    """A public call on a retained engine must make the service re-resolve."""

    fake = FakeConsolidator()
    sink = DirectLocalWriteSink(WriteSinkFakeStorage())
    engine = MidEngine(
        consolidator=fake,
        queue=FakeQueue(),
        write_sink=sink,
        direct_local_compaction_authority=(
            consolidator_module._issue_direct_local_compaction_authority(
                "space-a", sink
            )
        ),
    )

    result = await engine.compact_bank("space-a", dry_run=False)

    assert result is fake.compact_result
    assert fake.compact_calls == [
        {"space_id": "space-a", "dry_run": False, "direct_local_sink": None}
    ]


@pytest.mark.asyncio
async def test_mid_engine_bare_direct_sink_is_not_route_authority() -> None:
    """Only EngineRegistry may grant the context proof consumed by apply."""

    fake = FakeConsolidator()
    sink = DirectLocalWriteSink(WriteSinkFakeStorage())
    engine = MidEngine(consolidator=fake, queue=FakeQueue(), write_sink=sink)

    await engine.compact_bank("space-a", dry_run=False)

    assert fake.compact_calls == [
        {"space_id": "space-a", "dry_run": False, "direct_local_sink": None}
    ]


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
    descriptions so #8/#9 inherit the authoritative list. The normal
    consolidation source anchors are checked separately below; this test keeps
    the primary inventory assertions semantic."""
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
    methods = {s["method"] for s in consolidator_sites}

    normal_apply = "ConsolidatorService._apply_prepared_normal_batch"
    consolidate = "ConsolidatorService.consolidate"

    # ``_write_results`` remains callable for compatibility, but only delegates
    # validation/application and must never be presented as a durable sink.
    assert callable(getattr(ConsolidatorService, "_write_results"))
    assert "ConsolidatorService._write_results" not in methods

    # The single prepared-batch bank PUT covers all create/edit/rewrite candidates.
    prepared_bank_puts = [
        s
        for s in consolidator_sites
        if s["method"] == normal_apply
        and s["op"] == "put"
        and s["key_pattern"] == "{space_id}/bank/{filename}"
    ]
    assert len(prepared_bank_puts) == 1
    assert "create, edit, or rewrite" in prepared_bank_puts[0]["description"].lower()
    # _synthesis.md PUT is part of that same prepared batch.
    synthesis_puts = [
        s for s in consolidator_sites if s["key_pattern"].endswith("_synthesis.md")
    ]
    assert {(s["method"], s["op"]) for s in synthesis_puts} == {(normal_apply, "put")}
    # _meta.json has the direct/private application path and the run-level path.
    meta_json = [s for s in consolidator_sites if s["key_pattern"].endswith("_meta.json")]
    assert {(s["method"], s["op"]) for s in meta_json} == {
        (normal_apply, "put_json"),
        (consolidate, "put_json"),
    }
    assert any("private direct-application" in s["description"].lower() for s in meta_json)
    assert any("run-level" in s["description"].lower() for s in meta_json)
    # Unicode cleanup is a prepared-batch mutation after bank write readback.
    unicode_deletes = [
        s
        for s in consolidator_sites
        if s["op"] == "delete" and "unicode" in s["description"].lower()
    ]
    assert {s["method"] for s in unicode_deletes} == {normal_apply}
    assert "readback" in unicode_deletes[0]["description"].lower()
    # Direct callers retain their compatible finalization; normal consolidate()
    # defers its one delete_many until after the run-level metadata readback.
    note_deletes = [
        s
        for s in consolidator_sites
        if s["op"] == "delete_many" and "consumed notes" in s["key_pattern"]
    ]
    assert {s["method"] for s in note_deletes} == {normal_apply, consolidate}
    direct_note_delete = next(s for s in note_deletes if s["method"] == normal_apply)
    deferred_note_delete = next(s for s in note_deletes if s["method"] == consolidate)
    assert "defer_note_finalization" in direct_note_delete["description"]
    assert "deferred" in deferred_note_delete["description"].lower()
    assert "metadata write/readback" in deferred_note_delete["description"].lower()
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


def test_mid_engine_normal_writesink_line_hints_resolve_to_live_mutations() -> None:
    """Pin the normal-consolidation source anchors to their real storage calls.

    These seven inventory entries are used as precise handoff pointers for the
    normal batch's durable-write ordering.  A source delta must therefore
    update the corresponding ``line_hint`` rather than silently leaving the
    inventory stale.
    """
    source_lines = Path(consolidator_module.__file__).read_text(
        encoding="utf-8"
    ).splitlines()
    normal_apply = "ConsolidatorService._apply_prepared_normal_batch"
    consolidate = "ConsolidatorService.consolidate"
    expected_calls = {
        (normal_apply, "delete", "{space_id}/bank/<unicode-dup>"):
            "await storage.delete(raw_key)",
        (normal_apply, "put", "{space_id}/bank/{filename}"):
            'await storage.put(f"{space_id}/bank/{write.filename}", write.content)',
        (normal_apply, "put", "{space_id}/_synthesis.md"):
            'await storage.put(f"{space_id}/_synthesis.md", synthesis_md)',
        (normal_apply, "put_json", "{space_id}/_meta.json"):
            'await storage.put_json(f"{space_id}/_meta.json", meta)',
        (normal_apply, "delete_many", "{space_id}/live/* (consumed notes)"):
            "notes_deleted = await storage.delete_many(notes_keys)",
        (consolidate, "put_json", "{space_id}/_meta.json"):
            'await storage.put_json(f"{space_id}/_meta.json", meta)',
        (consolidate, "delete_many", "{space_id}/live/* (consumed notes)"):
            "notes_deleted = await storage.delete_many(pending_note_keys)",
    }
    sites_by_identity = {
        (site["method"], site["op"], site["key_pattern"]): site
        for site in WRITE_SINK_MUTATION_CALL_SITES
        if site["branch"] == "consolidator"
    }

    assert expected_calls.keys() <= sites_by_identity.keys()
    for identity, expected_call in expected_calls.items():
        line_hint = sites_by_identity[identity]["line_hint"]
        assert source_lines[line_hint - 1].strip() == expected_call
