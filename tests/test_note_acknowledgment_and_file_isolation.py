"""Focused tests for normal consolidation note acknowledgment
and the two-disposition contract for consumed notes.

A file refused during validation rejects its whole batch before any durable
write. Every note of an accepted batch is either integrated or declared
useless — both consumed. Nothing is ever retained "just in case".
"""

from __future__ import annotations

import logging

import pytest

from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import (
    ConsolidatorService,
    _NormalBatchPreparationFailure,
    _PreparedNormalBatch,
    _normal_output_schema_failures,
)
from tests.test_write_sink import WriteSinkFakeStorage


SPACE = "note-ack-space"


class RecordingStorage(WriteSinkFakeStorage):
    """Storage test double tracking operations faithfully."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted_keys: list[str] = []

    async def delete(self, key: str) -> None:
        # Consumed notes are deleted one key at a time.
        self.deleted_keys.append(key)
        await super().delete(key)

    async def list_and_get(
        self, prefix: str, exclude_keep: bool = True
    ) -> list[dict[str, str]]:
        return [
            {"key": key, "content": content}
            for key, content in sorted(self.objects.items())
            if key.startswith(prefix)
            and (not exclude_keep or not key.endswith(".keep"))
        ]


def _service() -> ConsolidatorService:
    service = object.__new__(ConsolidatorService)
    service._bank_file_max_size = 10_000
    service._max_tokens = 4096
    service._max_notes = 100
    service._batch_size = 20
    service._context_window = 131_072
    service._context_window_env_name = "INFERENCE_CHAT_CONTEXT_WINDOW"
    service._timeout = 1
    service._model = "test-model"
    service._chat_profile = object()
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._cooldown_seconds = 0
    return service


def _seed_bank(storage: RecordingStorage) -> list[dict]:
    storage.objects[f"{SPACE}/_rules.md"] = "# Rules\nKeep facts organized."
    storage.objects[f"{SPACE}/_meta.json"] = (
        '{"schema_version": 1, "consolidation_count": 0, "total_notes_processed": 0}'
    )
    storage.objects[f"{SPACE}/_synthesis.md"] = (
        "---\nconsolidated_at: '2026-01-01T00:00:00Z'\nnotes_processed: 0\n"
        "mode: surgical_edit\noperations_applied: 0\noperations_failed: 0\n---\n\n"
        "Initial synthesis."
    )
    storage.objects[f"{SPACE}/bank/facts.md"] = (
        "# Facts\n\n## Section 1\nFact A\n\n## Section 2\nFact B\n"
    )
    return [
        {
            "key": f"{SPACE}/bank/facts.md",
            "content": "# Facts\n\n## Section 1\nFact A\n\n## Section 2\nFact B\n",
        }
    ]


def _seed_notes(storage: RecordingStorage, count: int) -> list[str]:
    keys = []
    for i in range(1, count + 1):
        key = f"{SPACE}/live/20260901T120000_agent_fact_note{i}.md"
        content = f"---\nagent: test\ncategory: fact\n---\nNote {i} detail"
        storage.objects[key] = content
        keys.append(key)
    return keys


# ── 1. Schema Validation Tests ───────────────────────────────────────────────


def test_schema_accepts_valid_notes_in_file_edits_and_operations() -> None:
    data = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "New content",
                        "reason": "From note 1 and 2",
                        "notes": [1, 2],
                    }
                ],
            },
            {
                "filename": "new.md",
                "action": "create",
                "content": "# New\n\nBody",
                "reason": "From note 3",
                "notes": [3],
            },
            {
                "filename": "rewritten.md",
                "action": "rewrite",
                "content": "# Rewritten\n\nBody",
                "reason": "Major overhaul from note 4",
                "notes": [4],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary of notes 1 to 4",
    }
    failures = _normal_output_schema_failures(data)
    assert failures == []


@pytest.mark.parametrize(
    "invalid_notes",
    [
        "1",  # string instead of list
        123,  # integer instead of list
        [0],  # 0 is not >= 1
        [-1],  # negative int
        [True],  # bool is not allowed
        [1.5],  # float
        ["1"],  # list of string
    ],
)
def test_schema_rejects_malformed_notes_field(invalid_notes: object) -> None:
    data = {
        "file_edits": [
            {
                "filename": "new.md",
                "action": "create",
                "content": "# New\n\nBody",
                "reason": "From note",
                "notes": invalid_notes,
            }
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }
    failures = _normal_output_schema_failures(data)
    assert any(f.get("reason") == "invalid_normal_notes" for f in failures)


# ── 2. Bounds & Preparation Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_batch_rejects_out_of_bounds_notes() -> None:
    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "new.md",
                "action": "create",
                "content": "# New\n\nBody",
                "reason": "From note 5",
                "notes": [5],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }
    result = await service._prepare_normal_batch(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=[],
        notes_count=2,  # Only 2 notes available, note 5 is out of bounds!
    )
    assert isinstance(result, _NormalBatchPreparationFailure)
    assert result.operation_failures == (
        {
            "reason": "invalid_normal_notes_out_of_bounds",
            "file_index": 0,
            "filename": "new.md",
        },
    )


# ── 3. Full Success with Explicit Notes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_full_batch_with_explicit_notes_deletes_acknowledged_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Added fact from note 1",
                        "reason": "update",
                        "notes": [1],
                    },
                    {
                        "type": "append_to_section",
                        "heading": "## Section 2",
                        "content": "- Added fact from note 2",
                        "reason": "update",
                        "notes": [2],
                    },
                ],
            },
            {
                "filename": "extra.md",
                "action": "create",
                "content": "# Extra\n\nNew info from note 3",
                "reason": "new file",
                "notes": [3],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Processed notes 1, 2, 3",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=3,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "ok"
    assert result["notes_processed"] == 3
    assert result["notes_deleted"] == 3
    assert result["notes_delete_failed"] == 0
    assert result["operations_applied"] == 2
    assert result["operations_failed"] == 0

    # Verify all 3 notes were deleted from storage
    for key in note_keys:
        assert key not in storage.objects

    # Verify bank writes succeeded
    assert f"{SPACE}/bank/extra.md" in storage.objects
    assert "- Added fact from note 1" in storage.objects[f"{SPACE}/bank/facts.md"]


# ── 4. File-Level Isolation & Partial Execution (Items 4 & 5) ─────────────────


@pytest.mark.asyncio
async def test_refused_sibling_file_refuses_the_whole_batch_and_consumes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File 1 valid (notes 1, 2), File 2 refused (note 3, duplicate target).

    The batch is all-or-nothing again: nothing is written, no note is consumed,
    and no note is left behind "just in case".
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Valid fact from notes 1 & 2",
                        "reason": "update",
                        "notes": [1, 2],
                    }
                ],
            },
            {
                "filename": "facts.md",  # Duplicate target -> validation error on File 2
                "action": "create",
                "content": "# Invalid duplicate create",
                "reason": "bad edit",
                "notes": [3],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=3,
        usage={"total_tokens": 120},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_consolidation_batch"
    assert result["preflight_failed"] is True
    assert [f["reason"] for f in result["operation_failures"]] == ["duplicate_normal_target"]
    assert result["operations_applied"] == 0
    assert result["notes_deleted"] == 0
    assert result["notes_retained"] == []
    assert all(key in storage.objects for key in note_keys)
    assert storage.deleted_keys == []
    # The valid sibling edit was NOT persisted either.
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank


# ── 5. Fail-Closed on Sibling Failure without Explicit Attribution ───────────


@pytest.mark.asyncio
async def test_sibling_failure_without_explicit_notes_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When notes attribution is omitted (legacy format) and one file fails,

    the batch cannot know which notes belong to which file. It must fail closed
    without writing any file or deleting any note.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before_bank = dict(storage.objects)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Sibling edit without notes attribution",
                        "reason": "update",
                    }
                ],
            },
            {
                "filename": "facts.md",  # duplicate target
                "action": "create",
                "content": "# Invalid",
                "reason": "create",
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 50},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_consolidation_batch"
    assert result["notes_deleted"] == 0

    # Storage is completely untouched
    for key in note_keys:
        assert key in storage.objects
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank[f"{SPACE}/bank/facts.md"]


# ── 6. Unattributed Notes Retained Safely ────────────────────────────────────


@pytest.mark.asyncio
async def test_note_without_disposition_refuses_the_batch_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note neither integrated nor declared useless refuses the batch.

    Direct callers of ``_write_results`` get no corrective completion: the
    refusal names the missing note and nothing durable happens.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = _service()

    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Fact from note 1 only",
                        "reason": "update",
                        "notes": [1],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Summary for note 1",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 50},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {"reason": "normal_notes_unclassified", "missing_notes": [2]}
    ]
    assert result["notes_deleted"] == 0
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank


# ── 7. Multi-Batch Consolidation with Note Acknowledgment ────────────────────


@pytest.mark.asyncio
async def test_multibatch_consolidation_with_note_acknowledgment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full consolidate() path: note 1 integrated, note 2 declared useless.

    Both dispositions consume the note; the discarded one is logged with its key
    and closed reason only after its own delete, never with its content.
    """
    caplog.set_level(logging.INFO, logger="live_mem.consolidator")
    from live_mem.core import engines
    from live_mem.core.write_sink import DirectLocalWriteSink

    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)

    class DirectRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            return DirectLocalWriteSink(storage)

    monkeypatch.setattr(engines, "get_engine_registry", lambda: DirectRegistry())
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    # Batch 1 LLM response: Note 1 applied to facts.md, Note 2 omitted
    batch1_response = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Batch 1 fact from note 1",
                        "reason": "batch 1 update",
                        "notes": [1],
                    }
                ],
            }
        ],
        "discarded_notes": [{"note": 2, "reason": "already_in_bank"}],
        "synthesis": "Synthesis after batch 1",
    }

    # Mock _call_llm
    async def mock_call_llm(messages: list[dict]) -> dict:
        return {
            "status": "ok",
            "data": batch1_response,
            "usage": {"total_tokens": 80, "prompt_tokens": 50, "completion_tokens": 30},
        }

    monkeypatch.setattr(service, "_call_llm", mock_call_llm)

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert result["notes_deleted"] == 2
    assert result["notes_applied"] == [1]
    assert result["notes_discarded"] == [2]
    assert result["notes_discarded_count"] == 1
    assert result["discarded"] == [{"note": 2, "reason": "already_in_bank"}]
    assert result["notes_retained"] == []
    assert note_keys[0] not in storage.objects
    assert note_keys[1] not in storage.objects

    # Both dispositions count as processed.
    meta = await storage.get_json(f"{SPACE}/_meta.json")
    assert meta["total_notes_processed"] == 2
    assert meta["consolidation_count"] == 1

    # The discarded note is logged once, after its delete, by key and closed
    # reason — never its content.
    discard_logs = [
        r for r in caplog.records if r.getMessage().startswith("Consolidation discarded note")
    ]
    assert len(discard_logs) == 1
    message = discard_logs[0].getMessage()
    assert note_keys[1].rsplit("/", 1)[-1] in message
    assert "reason=already_in_bank" in message
    assert "category=fact" in message
    assert "Note 2 detail" not in message
    assert note_keys[1] in storage.deleted_keys


# ── 8. Lexer Structural Failure Isolated to File (Item 4) ────────────────────


@pytest.mark.asyncio
async def test_structural_failure_in_one_file_refuses_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File B fails structural validation (H1 not preserved on rewrite),
    so the healthy edit on File A is NOT persisted either and no note is consumed.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    # A second existing bank file so File B can be a structurally invalid
    # rewrite (H1 destroyed) without colliding with File A's target.
    notes_md = "# Notes\n\n## Log\nentry\n"
    storage.objects[f"{SPACE}/bank/notes.md"] = notes_md
    bank_files.append({"key": f"{SPACE}/bank/notes.md", "content": notes_md})
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Healthy edit for note 1",
                        "reason": "valid op",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "notes.md",
                "action": "rewrite",
                "content": "No H1 heading here at all — destroys H1!",
                "reason": "bad rewrite",
                "notes": [2],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["operations_applied"] == 0
    assert result["operations_failed"] == 1
    assert [f["reason"] for f in result["operation_failures"]] == ["normal_h1_not_preserved"]
    assert result["notes_deleted"] == 0
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects
    assert "- Healthy edit for note 1" not in storage.objects[f"{SPACE}/bank/facts.md"]
    assert storage.objects[f"{SPACE}/bank/notes.md"] == notes_md


# ── 9. Additional Adversarial Cases (Round 2 Coverage) ───────────────────────


@pytest.mark.asyncio
async def test_empty_attribution_deletes_zero_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When explicit notes: [] is supplied, it is rejected by schema as invalid_normal_notes and zero notes are deleted."""
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- General update without note attribution",
                        "reason": "background update",
                        "notes": [],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    # Direct schema check fails with invalid_normal_notes
    failures = _normal_output_schema_failures(llm_output)
    assert any(f.get("reason") == "invalid_normal_notes" for f in failures)

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["notes_deleted"] == 0
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects


@pytest.mark.asyncio
async def test_attributed_noop_rewrite_consumes_its_note_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attributed rewrite that changes nothing means the fact was
    already in the bank; note 1 is consumed, note 2 is declared useless, and the
    batch writes neither bank file nor synthesis."""
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]
    before_synthesis = storage.objects[f"{SPACE}/_synthesis.md"]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = _service()

    # Content is identical to existing content in facts.md
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": bank_files[0]["content"],
                "reason": "noop rewrite",
                "notes": [1],
            }
        ],
        "discarded_notes": [{"note": 2, "reason": "no_bank_value"}],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "ok"
    assert result["notes_deleted"] == 2
    assert result["notes_applied"] == [1]
    assert result["notes_discarded"] == [2]
    assert result["notes_retained"] == []
    assert result["synthesis_written"] is False
    assert result["bank_files_updated"] == 0
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank
    assert storage.objects[f"{SPACE}/_synthesis.md"] == before_synthesis
    assert note_keys[0] not in storage.objects
    assert note_keys[1] not in storage.objects
    assert storage.deleted_keys == note_keys


@pytest.mark.asyncio
async def test_overlapping_attribution_gives_retention_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If File A (succeeds) references [1] and File B (fails) references [1, 2],

    Note 1 must NOT be deleted because it is tied to failed File B.
    Since no notes remain applied, the batch fails closed.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Update from note 1",
                        "reason": "valid",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1",
                "reason": "bad rewrite",
                "notes": [1, 2],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 100},
        storage=storage,
    )

    # Batch failed closed because Note 1 is withheld due to File B failure
    assert result["status"] == "error"
    assert result["notes_deleted"] == 0
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects


@pytest.mark.asyncio
async def test_mixed_omission_fails_closed_on_file_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If File A has notes: [1] but File B omits notes, a failure on File B must fail closed."""
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Valid edit",
                        "reason": "valid",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1",
                "reason": "bad rewrite",
                # no 'notes' specified
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=2,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["notes_deleted"] == 0
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects


@pytest.mark.asyncio
async def test_consolidate_full_flow_refuses_the_batch_and_leaves_the_queue_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused file stops the run at its batch; nothing is written,
    nothing is consumed, and the message names the batch and the waiting notes."""
    from live_mem.core import engines
    from live_mem.core.write_sink import DirectLocalWriteSink

    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 2)
    before_bank = storage.objects[f"{SPACE}/bank/facts.md"]

    class DirectRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            return DirectLocalWriteSink(storage)

    monkeypatch.setattr(engines, "get_engine_registry", lambda: DirectRegistry())
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    # LLM response: File 1 (healthy edit, note 1), File 2 (bad rewrite, note 2)
    llm_response = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Successful fact from note 1",
                        "reason": "valid",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1 without heading",
                "reason": "bad rewrite",
                "notes": [2],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Synthesis summary",
    }

    async def mock_call_llm(messages: list[dict]) -> dict:
        return {
            "status": "ok",
            "data": llm_response,
            "usage": {"total_tokens": 80, "prompt_tokens": 50, "completion_tokens": 30},
        }

    monkeypatch.setattr(service, "_call_llm", mock_call_llm)

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_write_failed"
    assert result["failed_batch"] == 1
    assert result["batches_completed"] == 0
    assert result["notes_total"] == 2
    assert result["notes_processed"] == 0
    assert result["notes_deleted"] == 0
    assert result["notes_remaining"] == 2
    assert result["message"].startswith(
        "Consolidation stopped at batch 1/1 (batch_write_failed)"
    )
    assert "2 notes remain in order" in result["message"]
    assert note_keys[0] in storage.objects
    assert note_keys[1] in storage.objects
    assert storage.objects[f"{SPACE}/bank/facts.md"] == before_bank



# ── 10. Additional Adversarial Cases (Round 3 Coverage) ──────────────────────


@pytest.mark.asyncio
async def test_edit_with_partial_operation_attribution_fails_closed_on_file_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an edit file has 2 operations and only 1 has notes, it must fail closed if another file fails."""
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Op 1 attributed",
                        "reason": "valid",
                        "notes": [1],
                    },
                    {
                        "type": "append_to_section",
                        "heading": "## Section 2",
                        "content": "- Op 2 unattributed",
                        "reason": "valid",
                        # 'notes' omitted on purpose
                    },
                ],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1",
                "reason": "bad rewrite",
                "notes": [2],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=3,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["notes_deleted"] == 0
    assert all(k in storage.objects for k in note_keys)


@pytest.mark.asyncio
async def test_overlapping_write_is_withheld_from_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a successful candidate references [1, 3] and a failing candidate references [1, 2],

    the successful candidate MUST NOT be persisted because note 1 is shared with the failed file.
    Since no non-overlapping writes remain, the batch fails closed.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "new_file.md",
                "action": "create",
                "content": "# New File\n\nContent for note 1 and 3\n",
                "reason": "valid create",
                "notes": [1, 3],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1",
                "reason": "bad rewrite",
                "notes": [1, 2],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=3,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert f"{SPACE}/bank/new_file.md" not in storage.objects
    assert result["notes_deleted"] == 0
    assert all(k in storage.objects for k in note_keys)


@pytest.mark.asyncio
async def test_valid_sibling_files_are_not_persisted_when_one_file_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File A [3] and File B [1, 2] are valid, File C [1] fails.
    The whole batch is refused: neither A nor B is persisted, no note is consumed.
    """
    storage = RecordingStorage()
    bank_files = _seed_bank(storage)
    note_keys = _seed_notes(storage, 3)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = _service()

    llm_output = {
        "file_edits": [
            {
                "filename": "file_a.md",
                "action": "create",
                "content": "# File A\n\nIndependent fact from note 3\n",
                "reason": "valid create",
                "notes": [3],
            },
            {
                "filename": "file_b.md",
                "action": "create",
                "content": "# File B\n\nOverlapping fact from note 1 and 2\n",
                "reason": "overlapping create",
                "notes": [1, 2],
            },
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "Destroyed H1",
                "reason": "bad rewrite",
                "notes": [1],
            },
        ],
        "discarded_notes": [],
        "synthesis": "Summary",
    }

    result = await service._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=note_keys,
        notes_count=3,
        usage={"total_tokens": 100},
        storage=storage,
    )

    assert result["status"] == "error"
    assert result["notes_deleted"] == 0
    assert result["notes_applied"] == []
    assert result["notes_retained"] == []
    assert f"{SPACE}/bank/file_a.md" not in storage.objects
    assert f"{SPACE}/bank/file_b.md" not in storage.objects
    assert all(key in storage.objects for key in note_keys)


@pytest.mark.asyncio
async def test_multibatch_run_level_note_indices_are_offset_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-batch consolidate() returns accurate 1-indexed run-level
    notes_applied and notes_discarded (global indexes, reason bound to the right note)."""
    from live_mem.core import engines
    from live_mem.core.write_sink import DirectLocalWriteSink

    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 4)

    class DirectRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            return DirectLocalWriteSink(storage)

    monkeypatch.setattr(engines, "get_engine_registry", lambda: DirectRegistry())
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 2
    service._validation_enabled = False
    service._bank_file_max_size = 35000

    # Batch 1 (notes 1 & 2): applies notes 1 & 2 (batch-local [1, 2] -> run-level [1, 2])
    # Batch 2 (notes 3 & 4): applies note 1 (batch-local 1 -> run-level 3) and
    # declares note 2 useless (batch-local 2 -> run-level 4, reason "obsolete")
    batch1_response = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Fact from note 1 and 2",
                        "reason": "batch 1 op",
                        "notes": [1, 2],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Synthesis 1",
    }
    batch2_response = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 2",
                        "content": "- Fact from note 3",
                        "reason": "batch 2 op",
                        "notes": [1],
                    }
                ],
            }
        ],
        "discarded_notes": [{"note": 2, "reason": "obsolete"}],
        "synthesis": "Synthesis 2",
    }
    responses = [batch1_response, batch2_response]

    async def mock_call_llm(messages: list[dict]) -> dict:
        return {
            "status": "ok",
            "data": responses.pop(0),
            "usage": {"total_tokens": 50, "prompt_tokens": 30, "completion_tokens": 20},
        }

    monkeypatch.setattr(service, "_call_llm", mock_call_llm)

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)

    assert result["status"] == "ok"
    assert result["batches_completed"] == 2
    assert result["notes_deleted"] == 4
    assert result["notes_applied"] == [1, 2, 3]
    assert result["notes_discarded"] == [4]
    assert result["discarded"] == [{"note": 4, "reason": "obsolete"}]
    assert result["notes_retained"] == []
    for key in note_keys:
        assert key not in storage.objects


@pytest.mark.asyncio
async def test_consolidate_validation_pass_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consolidate with validation_enabled=True populates bank_after_batch and runs validation without crashing."""
    from live_mem.core import engines
    from live_mem.core.write_sink import DirectLocalWriteSink

    storage = RecordingStorage()
    _seed_bank(storage)
    note_keys = _seed_notes(storage, 1)

    class DirectRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            return DirectLocalWriteSink(storage)

    monkeypatch.setattr(engines, "get_engine_registry", lambda: DirectRegistry())
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 1
    service._validation_enabled = True
    service._validation_max_examples = 10

    llm_response = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Section 1",
                        "content": "- Validated claim [inferred]",
                        "reason": "op with claim",
                        "notes": [1],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Synthesis",
    }

    async def mock_call_llm(messages: list[dict]) -> dict:
        return {
            "status": "ok",
            "data": llm_response,
            "usage": {"total_tokens": 50, "prompt_tokens": 30, "completion_tokens": 20},
        }

    monkeypatch.setattr(service, "_call_llm", mock_call_llm)

    result = await service.consolidate(space_id=SPACE, enforce_cooldown=False)
    assert result["status"] == "ok"
    assert result["notes_deleted"] == 1
    assert "validation" in result
    assert result["validation"]["enabled"] is True
    assert result["validation"]["lines_scanned"] > 0
    assert result["validation"]["inferred_claims_count"] == 1
