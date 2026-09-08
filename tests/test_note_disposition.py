"""Two dispositions per note, one corrective completion.

Every note of a consolidation batch ends the batch either attributed to a bank
write or listed in ``discarded_notes`` with a closed reason; both dispositions
consume the note.  A batch refused only for the model's own form faults — an
incomplete plan, any plan grammar / disposition / edit-structure fault, or a
complete terminal completion whose content is unusable — gets exactly one
corrective completion; environment faults, unrecoverable delivery faults and a
second fault fail before any durable write.
The recovery tests cover chat invalid_response and length/other using
that same correction.  Consumed notes are deleted one key at a time and a discarded note is
logged only after its own delete returned.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import (
    _NORMAL_MODEL_COMPLETION_FAULT_REASONS,
    _NORMAL_MODEL_FORM_FAULT_REASONS,
    _NORMAL_OPERATION_FAILURE_REASONS,
    _bounded_normal_json_completion,
    _mutating_completion_text,
    _normal_model_body_fault,
    _normal_model_form_faults_only,
    _normal_note_dispositions,
    _normal_output_schema_failures,
    _sanitize_normal_operation_failure_payloads,
)
from tests.test_note_acknowledgment_and_file_isolation import (
    SPACE,
    RecordingStorage,
    _seed_bank,
    _seed_notes,
    _service,
)


def _plan(
    file_edits: list[dict] | None = None,
    discarded: list[dict] | None = None,
    synthesis: str = "Summary",
) -> dict:
    return {
        "file_edits": list(file_edits or []),
        "discarded_notes": list(discarded or []),
        "synthesis": synthesis,
    }


def _edit(
    notes: list[int],
    content: str = "- new fact",
    heading: str = "## Section 1",
    filename: str = "facts.md",
) -> dict:
    return {
        "filename": filename,
        "action": "edit",
        "operations": [
            {
                "type": "append_to_section",
                "heading": heading,
                "content": content,
                "reason": "update",
                "notes": list(notes),
            }
        ],
    }


def _discard(note: int, reason: str = "already_in_bank") -> dict:
    return {"note": note, "reason": reason}


def _bind_direct_local(monkeypatch: pytest.MonkeyPatch, storage: RecordingStorage) -> None:
    from live_mem.core import engines
    from live_mem.core.write_sink import DirectLocalWriteSink

    class DirectRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            return DirectLocalWriteSink(storage)

    monkeypatch.setattr(engines, "get_engine_registry", lambda: DirectRegistry())
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)


def _ok(plan: dict, total: int = 10) -> dict:
    return {
        "status": "ok",
        "data": plan,
        "usage": {"total_tokens": total, "prompt_tokens": total - 1, "completion_tokens": 1},
    }


# ── T1 — closed schema of discarded_notes ────────────────────────────────────


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (
            {"file_edits": [], "synthesis": "x"},
            [{"reason": "invalid_normal_root_schema"}],
        ),
        (
            {"file_edits": [], "discarded_notes": {}, "synthesis": "x"},
            [{"reason": "invalid_normal_discard"}],
        ),
        (
            _plan(discarded=[{"note": 1, "reason": "obsolete", "why": "x"}]),
            [{"reason": "invalid_normal_discard", "discard_index": 0}],
        ),
        (
            _plan(discarded=[{"note": True, "reason": "obsolete"}]),
            [{"reason": "invalid_normal_discard", "discard_index": 0}],
        ),
        (
            _plan(discarded=[{"note": 0, "reason": "obsolete"}]),
            [{"reason": "invalid_normal_discard", "discard_index": 0}],
        ),
        (
            _plan(discarded=[{"note": 3, "reason": "obsolete"}]),
            [{"reason": "invalid_normal_discard", "discard_index": 0}],
        ),
        (
            _plan(discarded=[_discard(1), _discard(1, "obsolete")]),
            [{"reason": "invalid_normal_discard", "note": 1, "discard_index": 1}],
        ),
        (
            _plan(discarded=[{"note": 1, "reason": "meh"}]),
            [{"reason": "invalid_normal_discard", "note": 1, "discard_index": 0}],
        ),
        (_plan(discarded=[_discard(1), _discard(2, "no_bank_value")]), []),
    ],
    ids=[
        "missing-root-key",
        "not-a-list",
        "extra-key",
        "bool-note",
        "zero-note",
        "out-of-range",
        "duplicate-note",
        "unknown-code",
        "valid",
    ],
)
def test_discard_schema_is_closed(plan: dict, expected: list[dict]) -> None:
    assert _normal_output_schema_failures(plan, notes_count=2) == expected


def test_dispositions_completeness_exclusivity_and_bounds() -> None:
    unclassified = _normal_note_dispositions(_plan(), 1)
    assert unclassified[2] == [{"reason": "normal_notes_unclassified", "missing_notes": [1]}]

    applied, discarded, failures = _normal_note_dispositions(
        _plan(discarded=[_discard(1), _discard(2, "superseded")]), 2
    )
    assert (applied, discarded, failures) == (frozenset(), {1: "already_in_bank", 2: "superseded"}, [])

    applied, discarded, failures = _normal_note_dispositions(
        _plan([_edit([1])], discarded=[_discard(1)]), 1
    )
    assert failures == [{"reason": "normal_note_disposition_overlap", "note": 1}]

    # An out-of-bounds attribution is a form fault reported alone (no missing
    # indexes): eligible for the corrective completion like every model form
    # fault, but relayed without inventing a note list.
    applied, discarded, failures = _normal_note_dispositions(_plan([_edit([3])]), 2)
    assert failures == [
        {
            "reason": "invalid_normal_notes_out_of_bounds",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]


# ── T6 — overlap refuses the batch before any write ──────────────────────────


async def test_overlap_between_dispositions_refuses_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_plan([_edit([1, 2])], discarded=[_discard(2)]),
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {"reason": "normal_note_disposition_overlap", "note": 2}
    ]
    assert result["notes_deleted"] == 0
    assert storage.objects == before
    assert storage.deleted_keys == []


# ── T3 — one corrective completion, never a third ────────────────────────────


async def test_corrective_completion_replaces_the_incomplete_plan_in_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    first = _plan([_edit([1], content="- first attempt")])
    second = _plan([_edit([1, 2], content="- second attempt")])
    service._call_llm = AsyncMock(side_effect=[_ok(first, total=10), _ok(second, total=20)])

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert service._call_llm.await_count == 2
    # The corrective operation replays the validated JSON and names the missing note.
    corrective_messages = service._call_llm.await_args_list[1].args[0]
    assert corrective_messages[-2] == {
        "role": "assistant",
        "content": json.dumps(first, ensure_ascii=False),
    }
    assert corrective_messages[-1]["role"] == "user"
    assert "Notes without disposition: 2" in corrective_messages[-1]["content"]
    # The second plan replaced the first one entirely.
    bank = storage.objects[f"{SPACE}/bank/facts.md"]
    assert "- second attempt" in bank
    assert "- first attempt" not in bank
    # Both completions are paid and counted exactly once.
    assert result["llm_tokens_used"] == 30
    assert result["notes_deleted"] == 2
    assert all(key not in storage.objects for key in note_keys)


async def test_second_incomplete_plan_fails_the_batch_and_never_requests_a_third(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    incomplete = _plan([_edit([1])])
    complete = _plan([_edit([1, 2])])
    service._call_llm = AsyncMock(
        side_effect=[_ok(incomplete), _ok(incomplete), _ok(complete)]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    # Two application completions at most: the valid third answer is never asked for.
    assert service._call_llm.await_count == 2
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_write_failed"
    assert result["failed_batch"] == 1
    assert {"reason": "normal_notes_unclassified", "missing_notes": [2]} in result[
        "operation_failures"
    ]
    assert result["notes_deleted"] == 0
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank
    assert all(key in storage.objects for key in note_keys)


async def test_rejected_corrective_completion_still_counts_its_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    service._call_llm = AsyncMock(
        side_effect=[
            _ok(_plan([_edit([1])]), total=10),
            {
                "status": "error",
                "reason": "invalid_normal_schema",
                "message": "LLM returned an invalid consolidation plan",
                "operation_failures": [{"reason": "invalid_normal_root_schema"}],
                "usage": {"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2},
            },
        ]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["llm_tokens_used"] == 17
    assert result["llm_prompt_tokens"] == 14
    assert result["llm_completion_tokens"] == 3


async def test_disposition_overlap_gets_the_single_corrective_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlap is the model's own form
    fault, so it earns the one corrective completion instead of failing the run."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    overlap = _plan([_edit([1, 2])], discarded=[_discard(2)])
    service._call_llm = AsyncMock(side_effect=[_ok(overlap), _ok(_plan([_edit([1, 2])]))])

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 2
    assert result["status"] == "ok"
    user_turn = service._call_llm.await_args_list[1].args[0][-1]["content"]
    assert '"reason": "normal_note_disposition_overlap"' in user_turn
    assert '"note": 2' in user_turn
    assert "exactly once" in user_turn
    assert all(key not in storage.objects for key in note_keys)


# ── T4 — direct path: all discarded, one deletion fails, delete before log ───


class _OrderedStorage(RecordingStorage):
    def __init__(self, fail_key: str) -> None:
        super().__init__()
        self.fail_key = fail_key
        self.events: list[tuple[str, str]] = []

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.events.append(("put", key))
        await super().put(key, content, content_type)

    async def delete(self, key: str) -> None:
        if key == self.fail_key:
            raise RuntimeError("injected delete failure")
        self.events.append(("delete", key))
        await super().delete(key)


class _EventLogHandler(logging.Handler):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__(level=logging.INFO)
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Consolidation discarded note"):
            note = message.split("note=", 1)[1].split(" ", 1)[0]
            self.events.append(("log", note))


async def test_all_discarded_direct_path_deletes_key_by_key_and_logs_after_each_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note_keys_preview = _seed_notes(RecordingStorage(), 3)
    storage = _OrderedStorage(fail_key=note_keys_preview[1])
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]
    before_synthesis = storage.objects[f"{SPACE}/_synthesis.md"]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    handler = _EventLogHandler(storage.events)
    logger = logging.getLogger("live_mem.consolidator")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = await _service()._write_results(
            space_id=SPACE,
            llm_output=_plan(
                discarded=[_discard(1), _discard(2, "obsolete"), _discard(3, "obsolete")]
            ),
            bank_files=bank_files,
            notes_keys=note_keys,
            notes_count=3,
            usage={},
            skip_meta=False,
            storage=storage,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert result["status"] == "partial"
    assert result["reason"] == "partial_delete"
    assert result["notes_deleted"] == 2
    assert result["notes_delete_failed"] == 1
    assert result["notes_discarded"] == [1, 2, 3]
    assert result["synthesis_written"] is False
    # Nothing durable besides the deletions and the direct-path metadata update:
    # no bank put, no synthesis put.
    puts = [event[1] for event in storage.events if event[0] == "put"]
    assert puts == [f"{SPACE}/_meta.json"]
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank
    assert storage.objects[f"{SPACE}/_synthesis.md"] == before_synthesis
    # Each surviving deletion is logged right after its own delete, the failed
    # key is neither deleted nor logged, and the loop continued past it.
    stems = [key.rsplit("/", 1)[-1] for key in note_keys]
    assert [event for event in storage.events if event[0] != "put"] == [
        ("delete", note_keys[0]),
        ("log", stems[0]),
        ("delete", note_keys[2]),
        ("log", stems[2]),
    ]
    assert note_keys[1] in storage.objects
    assert note_keys[0] not in storage.objects and note_keys[2] not in storage.objects


async def test_discard_log_carries_the_reason_of_its_own_note(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_plan(discarded=[_discard(1, "superseded"), _discard(2, "no_bank_value")]),
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={},
        storage=storage,
    )

    assert result["status"] == "ok"
    messages = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]
    assert len(messages) == 2
    assert f"note={note_keys[0].rsplit('/', 1)[-1]}" in messages[0]
    assert "reason=superseded" in messages[0]
    assert f"note={note_keys[1].rsplit('/', 1)[-1]}" in messages[1]
    assert "reason=no_bank_value" in messages[1]
    assert all("Note 1 detail" not in m and "Note 2 detail" not in m for m in messages)


# ── T4b — an all-discarded batch keeps the last written synthesis size ───────


async def test_all_discarded_batch_keeps_the_previously_written_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    service._call_llm = AsyncMock(
        side_effect=[
            _ok(_plan([_edit([1])], synthesis="Synthesis one")),
            _ok(_plan(discarded=[_discard(1)]), total=3),
        ]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert result["batches_completed"] == 2
    assert result["synthesis_size"] == len("Synthesis one")
    assert "Synthesis one" in storage.objects[f"{SPACE}/_synthesis.md"]
    assert result["notes_applied"] == [1]
    assert result["notes_discarded"] == [2]
    assert result["notes_deleted"] == 2
    assert all(key not in storage.objects for key in note_keys)
    # Run level: the first batch wrote the synthesis, the all-discarded one did not.
    assert result["synthesis_written"] is True
    _assert_run_invariants(result)


# ── T5a — a pre-write failure at a later batch finalizes the completed prefix ─


async def test_pre_write_failure_finalizes_the_completed_prefix_and_names_the_stop(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 5)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 3
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    service._call_llm = AsyncMock(
        side_effect=[
            _ok(_plan([_edit([1, 2])], discarded=[_discard(3, "superseded")])),
            {"status": "error", "message": "injected second-batch failure", "usage": {}},
        ]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["failed_batch"] == 2
    assert result["batches_completed"] == 1
    assert result["notes_total"] == 5
    assert result["notes_applied"] == [1, 2]
    assert result["notes_discarded"] == [3]
    assert result["discarded"] == [{"note": 3, "reason": "superseded"}]
    assert result["notes_deleted"] == 3
    assert result["notes_remaining"] == 2
    assert result["message"] == (
        "Consolidation stopped at batch 2/2 (batch_llm_failed): 1/2 batches "
        "completed, 2 notes integrated, 1 declared useless, 3 deleted; 2 notes "
        "remain in order and are retried first on the next run."
    )
    assert result["synthesis_written"] is True
    _assert_run_invariants(result)
    assert all(key not in storage.objects for key in note_keys[:3])
    assert all(key in storage.objects for key in note_keys[3:])
    discard_logs = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]
    assert len(discard_logs) == 1 and "reason=superseded" in discard_logs[0]


# ── T15 — a read failure inside a later all-discarded batch keeps the prefix safe ──


async def test_count_failure_in_a_later_all_discarded_batch_still_finalizes_all_notes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The count is informative even after an all-discarded batch. Both
    verified dispositions finalize; a count outage cannot strand any note."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 5)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 3
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    armed = {"fail_listing": False}
    real_list_objects = storage.list_objects

    async def flaky_list_objects(prefix: str):
        if armed["fail_listing"] and prefix.endswith("/bank/"):
            raise RuntimeError("injected bank listing outage")
        return await real_list_objects(prefix)

    storage.list_objects = flaky_list_objects  # type: ignore[method-assign]

    plans = iter(
        [
            _ok(_plan([_edit([1, 2])], discarded=[_discard(3, "superseded")])),
            _ok(_plan(discarded=[_discard(1, "already_in_bank"), _discard(2, "obsolete")])),
        ]
    )

    async def call_llm(_messages):
        plan = next(plans)
        if not plan["data"]["file_edits"]:
            armed["fail_listing"] = True  # the second, all-discarded batch
        return plan

    service._call_llm = call_llm

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert "failure_reason" not in result
    assert result["batches_completed"] == 2
    assert result["notes_applied"] == [1, 2]
    assert result["notes_discarded"] == [3, 4, 5]
    assert result["notes_deleted"] == 5
    assert result["notes_remaining"] == 0
    assert all(key not in storage.objects for key in note_keys)
    discard_logs = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]
    assert len(discard_logs) == 3
    assert "reason=superseded" in discard_logs[0]
    assert "reason=already_in_bank" in discard_logs[1]
    assert "reason=obsolete" in discard_logs[2]
    assert "file-count refresh failed" in caplog.text


# ── T16/T17/T18 — the deferred finalization proof is exact, and only a writable batch ──
# ── can make the completed prefix unsafe ──────────────────────────────────────────────


def _assert_run_invariants(result: dict) -> None:
    """notes_processed = integrated + discarded on every terminal result."""
    assert result["notes_processed"] == len(result["notes_applied"]) + len(result["notes_discarded"])
    assert result["notes_discarded_count"] == len(result["notes_discarded"])
    assert result["notes_retained"] == []
    assert isinstance(result["synthesis_written"], bool)


_ALL_DISCARDED_SECOND_PLAN = _plan(
    discarded=[_discard(1, "already_in_bank"), _discard(2, "obsolete")]
)


async def _run_prefix_then_tampered_all_discarded_batch(
    monkeypatch, caplog, tamper, second_plan: dict | None = None
):
    """Batch 1 integrates and discards; batch 2 (all-discarded by default) has its
    deferred proof tampered by ``tamper(result_dict)`` before the loop reads it."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 5)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 3
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        side_effect=[
            _ok(_plan([_edit([1, 2])], discarded=[_discard(3, "superseded")])),
            _ok(second_plan if second_plan is not None else _ALL_DISCARDED_SECOND_PLAN),
        ]
    )
    real_write_results = service._write_results
    calls = {"n": 0}

    async def tampered_write_results(**kwargs):
        result = await real_write_results(**kwargs)
        calls["n"] += 1
        if calls["n"] == 2:
            tamper(result)
        return result

    service._write_results = tampered_write_results  # type: ignore[method-assign]
    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)
    return result, storage, note_keys, caplog


def _assert_prefix_finalized_and_faulty_batch_retained(result, storage, note_keys, caplog) -> None:
    assert result["status"] == "partial"
    assert result["failure_reason"] == "batch_finalization_failed"
    assert result["failed_batch"] == 2
    assert result["batches_completed"] == 1
    assert result["notes_applied"] == [1, 2]
    assert result["notes_discarded"] == [3]
    assert result["notes_deleted"] == 3
    assert result["notes_remaining"] == 2
    assert result["synthesis_written"] is True
    _assert_run_invariants(result)
    assert all(key not in storage.objects for key in note_keys[:3])
    assert all(key in storage.objects for key in note_keys[3:])
    discard_logs = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]
    assert len(discard_logs) == 1 and "reason=superseded" in discard_logs[0]


async def test_missing_disposition_proof_refuses_finalization_of_that_batch_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without an exact disposition proof, the batch's notes must not be deleted
    (they would lose their mandatory reason log); the safe prefix is still finalized."""
    out = await _run_prefix_then_tampered_all_discarded_batch(
        monkeypatch, caplog, lambda r: r.pop("_deferred_dispositions", None)
    )
    _assert_prefix_finalized_and_faulty_batch_retained(*out)


async def test_malformed_disposition_reason_refuses_finalization_of_that_batch_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def tamper(r):
        r["_deferred_dispositions"] = tuple(
            (key, "because") for key, _reason in r["_deferred_dispositions"]
        )

    out = await _run_prefix_then_tampered_all_discarded_batch(monkeypatch, caplog, tamper)
    _assert_prefix_finalized_and_faulty_batch_retained(*out)


async def test_missing_key_proof_in_a_zero_write_batch_keeps_the_prefix_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    out = await _run_prefix_then_tampered_all_discarded_batch(
        monkeypatch, caplog, lambda r: r.pop("_deferred_note_keys", None)
    )
    _assert_prefix_finalized_and_faulty_batch_retained(*out)


async def test_tampered_proof_on_a_writable_batch_keeps_the_prefix_unfinalized(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The complementary branch: a batch WITH bank writes whose proof is invalid may
    have interleaved its writes with the prefix, so the prefix is not finalized and
    every note stays durable; the counters say nothing was consumed."""
    result, storage, note_keys, caplog = await _run_prefix_then_tampered_all_discarded_batch(
        monkeypatch,
        caplog,
        lambda r: r.pop("_deferred_dispositions", None),
        second_plan=_plan([_edit([1])], discarded=[_discard(2, "obsolete")]),
    )

    assert result["status"] == "partial"
    assert result["failure_reason"] == "batch_finalization_failed"
    assert result["failed_batch"] == 2
    assert result["batches_completed"] == 1
    assert result["notes_applied"] == []
    assert result["notes_discarded"] == []
    assert result["notes_processed"] == 0
    assert result["notes_deleted"] == 0
    assert result["notes_remaining"] == 5
    assert result["synthesis_written"] is True
    _assert_run_invariants(result)
    assert all(key in storage.objects for key in note_keys)
    assert not [
        r for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]


@pytest.mark.parametrize("path", ["no_provider", "cooldown", "inputs_conflict"])
async def test_every_early_terminal_result_carries_synthesis_written(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The run-level contract holds on the terminal results returned before any
    batch: no chat provider, active cooldown, input collection error/conflict."""
    storage = RecordingStorage()
    _seed_bank(storage)
    _seed_notes(storage, 1)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._call_llm = AsyncMock(side_effect=AssertionError("no LLM call expected"))
    enforce_cooldown = False
    if path == "no_provider":
        service._chat_profile = None
    elif path == "cooldown":
        import time

        service._cooldown_seconds = 3600
        monkeypatch.setitem(
            consolidator_module._last_consolidation_started, SPACE, time.monotonic()
        )
        enforce_cooldown = True
    else:
        service._collect_inputs = AsyncMock(
            return_value={"status": "conflict", "reason": "lock_held", "message": "busy"}
        )

    before = dict(storage.objects)
    result = await service.consolidate(space_id=SPACE, enforce_cooldown=enforce_cooldown)

    assert result["status"] in {"error", "conflict"}
    assert result["synthesis_written"] is False
    assert storage.objects == before  # nothing durable was touched


async def test_zero_note_run_reports_the_full_result_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._call_llm = AsyncMock(side_effect=AssertionError("no LLM call expected"))

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert result["notes_total"] == 0
    assert result["notes_processed"] == 0
    assert result["synthesis_written"] is False


# ── T14 — P12-1: an all-discarded first batch cannot fake a durable write ────


async def test_failure_before_any_mutation_on_an_all_discarded_first_batch_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    _seed_notes(storage, 1)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(return_value=_ok(_plan(discarded=[_discard(1)])))
    service._write_results = AsyncMock(side_effect=RuntimeError("injected before mutation"))

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_write_failed"


async def test_failure_after_a_prepared_write_stays_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    _seed_notes(storage, 1)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(return_value=_ok(_plan([_edit([1])])))
    service._write_results = AsyncMock(side_effect=RuntimeError("injected during write"))

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["failure_reason"] == "batch_write_failed"


# ── deleted_note_keys: exact selection only ──────────────────────────────────


async def test_deleted_note_keys_are_reported_only_for_an_exact_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 3
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        return_value=_ok(_plan(discarded=[_discard(1), _discard(2), _discard(3)]))
    )

    ordinary = await service.consolidate(space_id=SPACE, enforce_cooldown=False)
    assert ordinary["status"] == "ok"
    assert "deleted_note_keys" not in ordinary

    note_keys = _seed_notes(storage, 3)
    service._call_llm = AsyncMock(
        return_value=_ok(_plan(discarded=[_discard(1), _discard(2)]))
    )
    exact = await service.consolidate(
        space_id=SPACE, enforce_cooldown=False, note_keys=note_keys[:2]
    )
    assert exact["status"] == "ok"
    assert exact["deleted_note_keys"] == note_keys[:2]
    assert note_keys[2] in storage.objects


# ── T12 — public relay of note indexes is bounded ────────────────────────────


def test_sanitizer_relays_note_indexes_only_within_bounds() -> None:
    failures = [
        {"reason": "normal_notes_unclassified", "missing_notes": [2, 9, True, "x", 0, 2]},
        {"reason": "normal_note_disposition_overlap", "note": 5},
        {"reason": "normal_note_disposition_overlap", "note": 1},
        {"reason": "invalid_normal_discard", "discard_index": 4, "note": 2},
    ]
    assert _sanitize_normal_operation_failure_payloads(failures, notes_count=3) == [
        {"reason": "normal_notes_unclassified", "missing_notes": [2]},
        {"reason": "normal_note_disposition_overlap"},
        {"reason": "normal_note_disposition_overlap", "note": 1},
        {"reason": "invalid_normal_discard", "discard_index": 4, "note": 2},
    ]
    # Without a known bound, positive integers are kept; junk never is.
    assert _sanitize_normal_operation_failure_payloads(failures)[0] == {
        "reason": "normal_notes_unclassified",
        "missing_notes": [2, 9],
    }


async def test_refusal_relay_uses_the_batch_size_as_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_plan([_edit([1])]),
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={},
        storage=storage,
    )

    assert result["operation_failures"] == [
        {"reason": "normal_notes_unclassified", "missing_notes": [2]}
    ]


# ── run report: each discarded note carries its own reason ───────────────────


async def test_run_report_binds_each_reason_to_its_own_note(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two notes declared useless in one batch, for two different reasons: the run
    report and the logs must bind each reason to its own note (M11)."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 3
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        return_value=_ok(
            _plan([_edit([2])], discarded=[_discard(1, "obsolete"), _discard(3, "no_bank_value")])
        )
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert result["notes_applied"] == [2]
    assert result["notes_discarded"] == [1, 3]
    assert result["discarded"] == [
        {"note": 1, "reason": "obsolete"},
        {"note": 3, "reason": "no_bank_value"},
    ]
    logs = {
        r.getMessage().split("note=", 1)[1].split(" ", 1)[0]: r.getMessage().rsplit("reason=", 1)[1]
        for r in caplog.records
        if r.getMessage().startswith("Consolidation discarded note")
    }
    assert logs == {
        note_keys[0].rsplit("/", 1)[-1]: "obsolete",
        note_keys[2].rsplit("/", 1)[-1]: "no_bank_value",
    }
    assert all(key not in storage.objects for key in note_keys)


# ── T20+ — model form faults earn the single corrective completion ──
# A model form fault gets one corrective completion before stopping the run.
# The correction is bounded, and environment faults still refuse at once.
# Diagnostics are closed and content-free; transport retries are separate.

_SETEXT_BODY = "updated fact\n\nIndependent\n---\n\ninjected hierarchy"

_ENVIRONMENT_REASONS = frozenset(
    {
        "invalid_normal_bank_snapshot",
        "invalid_normal_batch_input",
        "invalid_normal_source_structure",
        "unsupported_normal_markdown_structure",
        "ambiguous_normalized_bank_target",
        "invalid_normal_utf8",
        "invalid_normal_completion",
        "normal_bank_readback_failed",
        "normal_synthesis_readback_failed",
        "normal_metadata_readback_failed",
        "normal_persistence_failure",
    }
)


def test_model_form_fault_allowlist_is_closed_and_excludes_environment_faults() -> None:
    assert _NORMAL_MODEL_FORM_FAULT_REASONS <= _NORMAL_OPERATION_FAILURE_REASONS
    assert not (_NORMAL_MODEL_FORM_FAULT_REASONS & _ENVIRONMENT_REASONS)
    assert _ENVIRONMENT_REASONS <= _NORMAL_OPERATION_FAILURE_REASONS
    assert not _normal_model_form_faults_only([])
    assert not _normal_model_form_faults_only("not-a-list")
    # Exact list only: a tuple would pass eligibility while the relay copy
    # accepts lists alone — fail closed on the container shape.
    assert not _normal_model_form_faults_only(({"reason": "invalid_normal_root_schema"},))
    assert not _normal_model_form_faults_only([{"reason": "invalid_normal_bank_snapshot"}])
    assert not _normal_model_form_faults_only(
        [
            {"reason": "invalid_normal_replacement_structure"},
            {"reason": "invalid_normal_source_structure"},
        ]
    )
    assert _normal_model_form_faults_only(
        [
            {"reason": "invalid_normal_replacement_structure", "detail": "setext_heading"},
            {"reason": "normal_notes_unclassified", "missing_notes": [2]},
        ]
    )


@pytest.mark.parametrize(
    ("body", "owner_level", "expected"),
    [
        ("- plain item\n\nparagraph", 2, None),
        (_SETEXT_BODY, 2, "setext_heading"),
        ("text\n```python\nopen fence", 2, "unbalanced_fence"),
        ("intro\n\n## Same level as target\n\nbody", 2, "heading_not_deeper_than_target"),
        ("intro\n\n### Deeper is fine\n\nbody", 2, None),
    ],
    ids=["safe", "setext", "fence", "same-level", "deeper"],
)
def test_body_fault_names_the_broken_rule(
    body: str, owner_level: int, expected: str | None
) -> None:
    assert _normal_model_body_fault(body, owner_level=owner_level) == expected


def test_relay_keeps_only_closed_structure_details() -> None:
    relayed = _sanitize_normal_operation_failure_payloads(
        [
            {
                "reason": "invalid_normal_replacement_structure",
                "file_index": 0,
                "operation_index": 1,
                "detail": "setext_heading",
            },
            {
                "reason": "invalid_normal_replacement_structure",
                "file_index": 0,
                "operation_index": 2,
                "detail": "<script>free text</script>",
            },
            {"reason": "duplicate_normal_target", "file_index": 0, "detail": "setext_heading"},
        ]
    )
    assert relayed == [
        {
            "reason": "invalid_normal_replacement_structure",
            "file_index": 0,
            "operation_index": 1,
            "detail": "setext_heading",
        },
        {"reason": "invalid_normal_replacement_structure", "file_index": 0, "operation_index": 2},
        {"reason": "duplicate_normal_target", "file_index": 0},
    ]


async def test_structure_fault_gets_one_corrective_completion_with_closed_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.WARNING, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    faulty = _plan([_edit([1, 2], content=_SETEXT_BODY)])
    fixed = _plan([_edit([1, 2], content="- corrected fact")])
    service._call_llm = AsyncMock(side_effect=[_ok(faulty, total=10), _ok(fixed, total=20)])

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert service._call_llm.await_count == 2
    corrective = service._call_llm.await_args_list[1].args[0]
    # The assistant turn replays the model's own parsed JSON, never raw text.
    assert corrective[-2] == {
        "role": "assistant",
        "content": json.dumps(faulty, ensure_ascii=False),
    }
    user_turn = corrective[-1]["content"]
    assert corrective[-1]["role"] == "user"
    assert '"reason": "invalid_normal_replacement_structure"' in user_turn
    assert '"detail": "setext_heading"' in user_turn
    assert '"filename": "facts.md"' in user_turn
    assert "no line made only of --- or ===" in user_turn
    assert "Notes without disposition" not in user_turn
    assert "replaces the previous plan entirely" in user_turn
    # The second plan replaced the first in full; the bank never saw the fault.
    bank = storage.objects[f"{SPACE}/bank/facts.md"]
    assert "- corrected fact" in bank
    assert "injected hierarchy" not in bank
    assert result["llm_tokens_used"] == 30
    assert result["notes_deleted"] == 2
    assert all(key not in storage.objects for key in note_keys)
    # The retry is observable, with closed diagnostics and no model prose.
    warning = next(
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "corrective completion" in r.getMessage()
    )
    assert "refused once for 1 model form fault(s)" in warning
    assert '"reason":"invalid_normal_replacement_structure"' in warning
    assert '"detail":"setext_heading"' in warning
    assert "injected hierarchy" not in warning


async def test_schema_fault_of_a_parsed_plan_gets_one_corrective_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_call_llm`` rejects a parsed JSON that breaks the closed grammar: the
    model gets one corrective completion replaying that JSON.
    Malformed JSON and provider errors are NOT covered (see the next test)."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    parsed_but_invalid = {"file_edits": [], "synthesis": "x"}  # discarded_notes missing
    rejected = {
        "status": "error",
        "message": "LLM returned an invalid consolidation plan",
        "reason": "invalid_normal_schema",
        "operation_failures": [{"reason": "invalid_normal_root_schema"}],
        "data": parsed_but_invalid,
        "usage": {"total_tokens": 7, "prompt_tokens": 6, "completion_tokens": 1},
    }
    service._call_llm = AsyncMock(
        side_effect=[rejected, _ok(_plan([_edit([1, 2])]), total=20)]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert service._call_llm.await_count == 2
    corrective = service._call_llm.await_args_list[1].args[0]
    assert corrective[-2] == {
        "role": "assistant",
        "content": json.dumps(parsed_but_invalid, ensure_ascii=False),
    }
    assert '"reason": "invalid_normal_root_schema"' in corrective[-1]["content"]
    assert "closed root keys" in corrective[-1]["content"]
    assert result["llm_tokens_used"] == 27
    assert all(key not in storage.objects for key in note_keys)


@pytest.mark.parametrize(
    "operation_failures",
    [
        [{"reason": "invalid_normal_root_schema"}, None],
        [{"reason": "invalid_normal_root_schema"}, "invalid_normal_discard"],
        [],
        "not-a-list",
        ({"reason": "invalid_normal_root_schema"},),
    ],
    ids=["none-entry", "string-entry", "empty", "not-a-list", "tuple-container"],
)
async def test_malformed_schema_failure_list_never_authorizes_the_corrective_completion(
    monkeypatch: pytest.MonkeyPatch, operation_failures: object
) -> None:
    """Eligibility is decided on the RAW failure list. A
    malformed entry next to an allowed reason must fail closed — no second paid
    completion — even though the relay later drops the malformed entry."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    rejected = {
        "status": "error",
        "message": "LLM returned an invalid consolidation plan",
        "reason": "invalid_normal_schema",
        "operation_failures": operation_failures,
        "data": {"file_edits": [], "synthesis": "x"},
        "usage": {"total_tokens": 7, "prompt_tokens": 6, "completion_tokens": 1},
    }
    service._call_llm = AsyncMock(side_effect=[rejected, _ok(_plan([_edit([1, 2])]))])

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 1
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["llm_tokens_used"] == 7
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)


# ── An unusable completion is a model fault too ────────────────────────────
# Model text that is not the required JSON earns one corrective completion.
# Delivery faults use their own reason allowlist; the tests below separate
# correctable content from terminal provider, environment, or mixed faults.
# Transient transport retries are covered separately from this correction.


class _FakeCompletion:
    """Minimal stand-in for the provider result read by the terminality gate."""

    def __init__(self, finish_reason: object, text: object) -> None:
        self.finish_reason = finish_reason
        self.text = text


def _llm_error(reason: str | None, total: int = 7) -> dict:
    payload: dict = {
        "status": "error",
        "message": "LLM returned an unusable completion",
        "usage": {"total_tokens": total, "prompt_tokens": total - 1, "completion_tokens": 1},
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def test_completion_fault_reasons_are_the_ones_the_real_gates_emit() -> None:
    """Pin every token of the closed set to the code that actually produces it:
    a rename in the gates must break this test, never silently disable the retry."""
    op = "normal_consolidation"
    blank = _mutating_completion_text(_FakeCompletion("stop", "   "), operation=op)[1]
    _, json_error, _ = _bounded_normal_json_completion("this is prose, not JSON")
    assert blank in _NORMAL_MODEL_COMPLETION_FAULT_REASONS
    assert json_error in _NORMAL_MODEL_COMPLETION_FAULT_REASONS
    assert "invalid_normal_utf8" in _NORMAL_MODEL_COMPLETION_FAULT_REASONS
    # Delivery faults, produced by the same gate, must stay out of the set.
    for finish_reason in ("length", "content_rejected", "other", "unexpected"):
        delivery = _mutating_completion_text(
            _FakeCompletion(finish_reason, "x"), operation=op
        )[1]
        assert delivery not in _NORMAL_MODEL_COMPLETION_FAULT_REASONS
    no_text = _mutating_completion_text(_FakeCompletion("stop", None), operation=op)[1]
    assert no_text not in _NORMAL_MODEL_COMPLETION_FAULT_REASONS
    assert len(_NORMAL_MODEL_COMPLETION_FAULT_REASONS) == 3


@pytest.mark.parametrize(
    "reason",
    sorted(_NORMAL_MODEL_COMPLETION_FAULT_REASONS),
)
async def test_unusable_completion_gets_the_single_corrective_completion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reason: str
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.WARNING, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        side_effect=[_llm_error(reason, total=7), _ok(_plan([_edit([1, 2])]), total=20)]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert service._call_llm.await_count == 2
    corrective = service._call_llm.await_args_list[1].args[0]
    # No JSON survived, so no assistant turn is replayed: exactly one user turn
    # is appended, and it never carries the unusable completion itself.
    assert len(corrective) == len(service._call_llm.await_args_list[0].args[0]) + 1
    assert corrective[-1]["role"] == "user"
    assert reason in corrective[-1]["content"]
    assert "nothing else" in corrective[-1]["content"]
    assert "file_edits, discarded_notes and synthesis" in corrective[-1]["content"]
    # Both paid completions are counted exactly once.
    assert result["llm_tokens_used"] == 27
    assert result["notes_deleted"] == 2
    assert all(key not in storage.objects for key in note_keys)
    warning = next(
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "corrective completion" in r.getMessage()
    )
    assert "unusable completion" in warning
    assert f"reason={reason}" in warning


async def test_second_unusable_completion_refuses_the_batch_and_never_requests_a_third(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    unusable = _llm_error("invalid_normal_consolidation_json")
    service._call_llm = AsyncMock(
        side_effect=[unusable, unusable, _ok(_plan([_edit([1, 2])]))]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 2
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["failed_batch"] == 1
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)


@pytest.mark.parametrize(
    "reason",
    [
        "normal_consolidation_completion_content_rejected",
        "invalid_normal_consolidation_finish_reason",
        "invalid_normal_consolidation_completion",
        "provider_unavailable",
        None,
    ],
    ids=[
        "content-filter",
        "unexpected-finish-reason",
        "no-text",
        "provider-error",
        "no-reason-at-all",
    ],
)
async def test_delivery_faults_and_provider_errors_stay_terminal(
    monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    """Refusals, invalid result objects and unclassified faults remain terminal.
    Normalized length/other responses now have separate recovery coverage.
    ``None`` still covers exhausted windows and generic exceptions."""
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        side_effect=[_llm_error(reason), _ok(_plan([_edit([1, 2])]))]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 1
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)


async def test_second_form_fault_refuses_the_batch_with_closed_diagnostics_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    caplog.set_level(logging.WARNING, logger="live_mem.consolidator")
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    faulty = _plan([_edit([1, 2], content=_SETEXT_BODY)])
    valid_but_never_requested = _plan([_edit([1, 2])])
    service._call_llm = AsyncMock(
        side_effect=[_ok(faulty), _ok(faulty), _ok(valid_but_never_requested)]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    # Absolute bound: two completions, the valid third answer is never asked for.
    assert service._call_llm.await_count == 2
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_write_failed"
    assert result["failed_batch"] == 1
    assert {
        "reason": "invalid_normal_replacement_structure",
        "file_index": 0,
        "operation_index": 0,
        "filename": "facts.md",
        "detail": "setext_heading",
    } in result["operation_failures"]
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)
    error = next(
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "refused before storage mutation" in r.getMessage()
    )
    # The refusal names the batch, the attempt and the closed diagnostics — the
    # gap that required an admin-only job lookup is closed.
    assert "(1 failure(s), attempt 2/2)" in error
    assert '"reason":"invalid_normal_replacement_structure"' in error
    assert '"detail":"setext_heading"' in error
    assert '"filename":"facts.md"' in error
    assert "injected hierarchy" not in error


async def test_environment_fault_refuses_at_once_without_a_corrective_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model cannot repair the bank: an unbalanced fence in the SOURCE file
    is an environment fault and refuses on the first completion."""
    storage = RecordingStorage()
    _seed_bank(storage)
    storage.objects[f"{SPACE}/bank/facts.md"] = (
        "# Facts\n\n## Section 1\nFact A\n```\nopen fence\n\n## Section 2\nFact B\n"
    )
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._call_llm = AsyncMock(
        side_effect=[_ok(_plan([_edit([1, 2])])), _ok(_plan([_edit([1, 2])]))]
    )

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 1
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_write_failed"
    assert {
        "reason": "invalid_normal_source_structure",
        "file_index": 0,
        "operation_index": 0,
        "filename": "facts.md",
    } in result["operation_failures"]
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)


async def test_mixed_model_and_environment_faults_refuse_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One form fault plus one environment fault in the same plan: no retry,
    because the corrective completion could not fix the environment half."""
    storage = RecordingStorage()
    _seed_bank(storage)
    storage.objects[f"{SPACE}/bank/broken.md"] = "# Broken\n\n## Part\n```\nopen fence\n"
    note_keys = _seed_notes(storage, 2)
    before = dict(storage.objects)
    _bind_direct_local(monkeypatch, storage)
    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    mixed = _plan(
        [
            _edit([1], content=_SETEXT_BODY),
            _edit([2], heading="## Part", filename="broken.md"),
        ]
    )
    service._call_llm = AsyncMock(side_effect=[_ok(mixed), _ok(_plan([_edit([1, 2])]))])

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert service._call_llm.await_count == 1
    assert result["status"] == "error"
    reasons = {failure["reason"] for failure in result["operation_failures"]}
    assert {"invalid_normal_replacement_structure", "invalid_normal_source_structure"} <= reasons
    assert storage.objects == before
    assert all(key in storage.objects for key in note_keys)
