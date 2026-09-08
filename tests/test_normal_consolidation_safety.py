"""Fail-closed regression coverage for normal consolidation and dedup (#397)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import ConsolidatorService
from tests.test_write_sink import WriteSinkFakeStorage


SPACE = "normal-safety"
NOTE = f"{SPACE}/live/20000101T000000_alice_observation_deadbeef.md"
BANK_KEY = f"{SPACE}/bank/facts.md"
META_KEY = f"{SPACE}/_meta.json"
SYNTHESIS_KEY = f"{SPACE}/_synthesis.md"
FACTS = "# Facts\n\n## Status\n\nold fact\n"


class RecordingStorage(WriteSinkFakeStorage):
    """DirectLocal-shaped fake that exposes persistence order without network I/O."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []
        self.list_and_get_calls: list[tuple[str, bool]] = []

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        self.events.append(("put", key))
        await super().put(key, content, content_type)

    async def put_json(self, key: str, data: dict) -> None:
        self.events.append(("put_json", key))
        await super().put_json(key, data)

    async def get(self, key: str) -> str | None:
        self.events.append(("get", key))
        return await super().get(key)

    async def list_and_get(
        self, prefix: str, exclude_keep: bool = True
    ) -> list[dict[str, str]]:
        self.list_and_get_calls.append((prefix, exclude_keep))
        return [
            {"key": key, "content": content}
            for key, content in sorted(self.objects.items())
            if key.startswith(prefix)
            and (not exclude_keep or not key.endswith(".keep"))
        ]

    async def delete(self, key: str) -> None:
        self.events.append(("delete", key))
        await super().delete(key)

    async def delete_many(self, keys: list[str]) -> int:
        self.events.append(("delete_many", ",".join(keys)))
        return await super().delete_many(keys)


class Completion:
    """One normalized provider response for the sole inference seam."""

    def __init__(
        self,
        text: str,
        *,
        finish_reason: str = "stop",
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.error = error
        self.calls = 0
        self.output_budgets: list[int] = []
        self.retry_policies: list[str] = []
        self.messages: list[list[dict]] = []

    async def __call__(self, messages, output_budget, *, retry_policy="bounded"):
        self.calls += 1
        self.output_budgets.append(output_budget)
        self.retry_policies.append(retry_policy)
        self.messages.append([dict(message) for message in messages])
        if self.error is not None:
            raise self.error
        return ChatResult(
            text=self.text,
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason=self.finish_reason,
        )


def _service(
    completion: Completion | None = None,
    *,
    max_tokens: int = 4096,
    context_window: int = 131_072,
) -> ConsolidatorService:
    service = object.__new__(ConsolidatorService)
    service._max_tokens = max_tokens
    service._context_window = context_window
    service._context_window_env_name = "INFERENCE_CHAT_CONTEXT_WINDOW"
    service._timeout = 1
    service._complete_chat = completion or Completion('{"file_edits": [], "discarded_notes": [], "synthesis": "ok"}')
    return service


def _create(filename: str = "new.md", *, content: str = "# New\n\nbody\n", reason: str = "Required by the notes.", notes: list[int] | None = None) -> dict:
    return {
        "filename": filename,
        "action": "create",
        "content": content,
        "reason": reason,
        "notes": [1] if notes is None else notes,
    }


def _edit(filename: str = "facts.md", *, operations: list[dict] | None = None) -> dict:
    return {
        "filename": filename,
        "action": "edit",
        "operations": operations
        or [
            {
                "type": "append_to_section",
                "heading": "## Status",
                "content": "- newly verified fact",
                "reason": "The batch adds this fact.",
                "notes": [1],
            }
        ],
    }


def _output(
    *file_edits: dict,
    synthesis: str = "The notes were integrated.",
    discarded: list[dict] | None = None,
) -> dict:
    return {
        "file_edits": list(file_edits),
        "discarded_notes": list(discarded or []),
        "synthesis": synthesis,
    }


def _seed(storage: RecordingStorage, *, facts: str = FACTS) -> list[dict]:
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[BANK_KEY] = facts
    storage.objects[NOTE] = "source note"
    return [{"key": BANK_KEY, "content": facts}]


@pytest.mark.parametrize(
    ("name", "llm_output", "facts"),
    [
        ("create-existing", _output(_create("facts.md")), FACTS),
        ("edit-missing", _output(_edit("missing.md")), FACTS),
        (
            "duplicate-target",
            _output(_create("new.md"), _create("new.md")),
            FACTS,
        ),
        ("keep-sentinel-target", _output(_create("lost.keep")), FACTS),
        ("dangerous-filename", _output(_create("<unsafe>.md")), FACTS),
        ("control-filename", _output(_create("new.md\ninjected.md")), FACTS),
        ("invisible-filename", _output(_create("new\u200b.md")), FACTS),
        ("format-filename", _output(_create("new\u2066.md")), FACTS),
        (
            "unknown-field",
            _output(
                {
                    **_create(),
                    "unexpected": "must not be accepted",
                }
            ),
            FACTS,
        ),
        (
            "unknown-operation",
            _output(
                _edit(
                    operations=[
                        {
                            "type": "invented_operation",
                            "heading": "## Status",
                            "content": "bad",
                            "reason": "bad",
                        }
                    ]
                )
            ),
            FACTS,
        ),
        ("blank-create-content", _output(_create(content="   ")), FACTS),
        ("blank-create-reason", _output(_create(reason="\n")), FACTS),
        ("blank-invisible-content", _output(_create(content="\u200b")), FACTS),
        ("blank-control-reason", _output(_create(reason="\x00")), FACTS),
        (
            "ambiguous-heading",
            _output(_edit()),
            "# Facts\n\n## Status\n\none\n\n## Status\n\ntwo\n",
        ),
        (
            "h1-target",
            _output(
                _edit(
                    operations=[
                        {
                            "type": "replace_section",
                            "heading": "# Facts",
                            "content": "rewritten root body",
                            "reason": "must be refused",
                            "notes": [1],
                        }
                    ]
                )
            ),
            FACTS,
        ),
    ],
)
async def test_invalid_normal_batch_changes_no_durable_object(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    llm_output: dict,
    facts: str,
) -> None:
    """Every syntactic or target-dependent invalidity is all-or-nothing."""

    storage = RecordingStorage()
    bank_files = _seed(storage, facts=facts)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error", name
    assert result["operations_failed"] >= 1, name
    assert result["operation_failures"], name
    assert storage.snapshot() == before, name
    assert NOTE in storage.objects, name


async def test_blank_normal_synthesis_with_a_valid_edit_changes_no_durable_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthesis blankness guard cannot hide behind an empty-edit refusal."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("valid.md"), synthesis="\u200b"),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [{"reason": "blank_normal_synthesis"}]
    assert storage.snapshot() == before


async def test_one_invalid_sibling_prevents_every_other_sibling_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid create cannot sneak through ahead of an invalid edit sibling.

    The invalid sibling is a protected H1 target.  It used to be a missing
    heading, but an absent target is now recovered rather than refused, so that
    example no longer exercises the atomicity contract this test exists for.
    """

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _create("new.md"),
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "# Facts",
                        "content": "must not matter",
                        "reason": "invalid target",
                        "notes": [1],
                    }
                ]
            ),
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "protected_normal_h1_target",
            "file_index": 1,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    ("source_heading", "requested_heading"),
    [
        ("## Release — 2026", "## Release - 2026"),
        ("## Référence", "## Re\u0301fe\u0301rence"),
        ("## Topic   With   Gaps", "## Topic With Gaps"),
    ],
)
def test_normal_target_uses_unique_conservative_fallback_without_rewriting_heading(
    source_heading: str,
    requested_heading: str,
) -> None:
    source = f"# Facts\n\n{source_heading}\n\nold body\n\n## Other\n\nuntouched\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "replace_section",
                "heading": requested_heading,
                "content": "new body",
                "reason": "A verified fact changed.",
                "notes": [1],
            }
        ],
        0,
    )

    assert failures == []
    assert candidate == f"# Facts\n\n{source_heading}\nnew body\n## Other\n\nuntouched\n"


def test_normal_exact_target_wins_over_a_normalized_collision() -> None:
    source = (
        "# Facts\n\n"
        "## Release — 2026\n\nfirst body\n\n"
        "## Release - 2026\n\nsecond body\n"
    )

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "replace_section",
                "heading": "## Release - 2026",
                "content": "second updated",
                "reason": "The exact section changed.",
                "notes": [1],
            }
        ],
        0,
    )

    assert failures == []
    assert candidate == (
        "# Facts\n\n"
        "## Release — 2026\n\nfirst body\n\n"
        "## Release - 2026\nsecond updated\n"
    )


def test_normal_fallback_collision_is_attributable_and_fail_closed() -> None:
    requested = "## Release ‐ 2026"
    source = (
        "# Facts\n\n"
        "## Release — 2026\n\nfirst body\n\n"
        "## Release - 2026\n\nsecond body\n"
    )

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": requested,
                "reason": "The section is obsolete.",
                "notes": [1],
            }
        ],
        2,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "ambiguous_or_missing_normal_target",
            "file_index": 2,
            "operation_index": 0,
            "target_resolution": "ambiguous",
            "target_match_count": 2,
            "target_heading_sha256": hashlib.sha256(
                requested.encode("utf-8")
            ).hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "requested",
    [
        # #457 item 3 — la casse seule n'est plus ici : elle resout desormais,
        # avec unicite obligatoire.  Voir
        # ``test_a_case_only_difference_resolves_instead_of_forking_the_history``.
        "## Release: 2026",   # ponctuation differente
        "### Release — 2026", # niveau ATX different
        "## Release —",       # titre tronque
    ],
)
def test_normal_target_fallback_does_not_become_fuzzy(requested: str) -> None:
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": requested,
                "reason": "The section is obsolete.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "ambiguous_or_missing_normal_target",
            "file_index": 0,
            "operation_index": 0,
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": hashlib.sha256(
                requested.encode("utf-8")
            ).hexdigest(),
        }
    ]


def test_normal_invisible_target_remains_invalid_without_a_content_hash() -> None:
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": "## Release\u200b — 2026",
                "reason": "The section is obsolete.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "invalid_normal_heading",
            "file_index": 0,
            "operation_index": 0,
        }
    ]


async def test_normal_add_after_uses_unique_conservative_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Anchor — 2026\n\nold\n\n## Other\n\nkept\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "add_section",
                        "heading": "## Added",
                        "after": "## Anchor - 2026",
                        "content": "new facts",
                        "reason": "The new section follows its anchor.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "## Anchor — 2026\n\nold\n\n\n## Added\n\nnew facts\n\n## Other" in persisted


async def test_missing_normal_after_is_recovered_as_eof_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = "## Missing — Anchor"
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "add_section",
                        "heading": "## Added",
                        "after": requested,
                        "content": "new facts",
                        "reason": "The new section follows its anchor.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert result["bank_files_updated"] == 1
    assert result["recovered_operations"] == [
        {
            "operation_index": 0,
            "filename": "facts.md",
            "type": "add_section",
            "strategy": "append_missing_after_anchor",
            "after_heading_sha256": hashlib.sha256(
                requested.encode("utf-8")
            ).hexdigest(),
        }
    ]
    assert "## Added\n\nnew facts" in storage.objects[BANK_KEY]


async def test_missing_canonical_bank_file_on_edit_is_autocreated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Current Focus",
                        "content": "Implementing resilient consolidation.",
                        "reason": "Note adds active context.",
                        "notes": [1],
                    }
                ],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert result["bank_files_created"] == 1
    assert result["recovered_operations"] == [
        {
            "file_index": 0,
            "filename": "activeContext.md",
            "type": "file",
            "strategy": "create_missing_bank_file",
        }
    ]
    created_key = f"{SPACE}/bank/activeContext.md"
    assert created_key in storage.objects
    assert storage.objects[created_key] == "# Active Context\n\n## Current Focus\n\nImplementing resilient consolidation.\n"


async def test_missing_canonical_bank_file_on_rewrite_enforces_exact_h1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    # 1. Candidate with no H1 -> fails closed with normal_h1_not_preserved
    no_h1_result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "progress.md",
                "action": "rewrite",
                "content": "plain text without any H1",
                "reason": "missing h1 rewrite",
                "notes": [1],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )
    assert no_h1_result["status"] == "error"
    assert f"{SPACE}/bank/progress.md" not in storage.objects
    assert any(
        f.get("reason") == "normal_h1_not_preserved"
        for f in no_h1_result.get("operation_failures", [])
    )

    # 2. Candidate with wrong H1 -> fails closed
    wrong_h1_result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "progress.md",
                "action": "rewrite",
                "content": "# Wrong Title\n\n## Section\nContent",
                "reason": "wrong h1 rewrite",
                "notes": [1],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )
    assert wrong_h1_result["status"] == "error"
    assert f"{SPACE}/bank/progress.md" not in storage.objects

    # 3. Candidate with correct H1 -> succeeds and records recovery
    valid_rewrite_result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "progress.md",
                "action": "rewrite",
                "content": "# Progress\n\n## Status\n\nAll systems operational.\n",
                "reason": "valid rewrite",
                "notes": [1],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )
    assert valid_rewrite_result["status"] == "ok"
    assert valid_rewrite_result["bank_files_created"] == 1
    assert valid_rewrite_result["recovered_operations"] == [
        {
            "file_index": 0,
            "filename": "progress.md",
            "type": "file",
            "strategy": "create_missing_bank_file",
        }
    ]
    assert storage.objects[f"{SPACE}/bank/progress.md"] == "# Progress\n\n## Status\n\nAll systems operational.\n"


async def test_partial_execution_never_reports_recovery_for_failed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    # File 0: missing activeContext.md with invalid operation (fails closed) -> notes: [1]
    # File 1: valid edit on facts.md -> notes: [2]
    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "delete_section",
                        "heading": "## Nonexistent",
                        "reason": "bad delete",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Extra",
                        "content": "more facts",
                        "reason": "good add",
                        "notes": [2],
                    }
                ],
            },
        ),
        bank_files=bank_files,
        notes_keys=[f"{SPACE}/short/note-1.json", f"{SPACE}/short/note-2.json"],
        notes_count=2,
        usage={},
        skip_meta=False,
    )

    # A refused file refuses the whole batch before any write, so the
    # valid sibling edit is NOT persisted either and no recovery is reported.
    assert result["status"] == "error"
    assert result["reason"] == "invalid_consolidation_batch"
    assert result["bank_files_created"] == 0
    assert result["bank_files_updated"] == 0
    assert result.get("recovered_operations", []) == []
    assert f"{SPACE}/bank/activeContext.md" not in storage.objects
    assert storage.objects[BANK_KEY] == FACTS


async def test_auto_created_edit_aggregates_operation_counts_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "techContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Architecture",
                        "content": "Microservices with FastMCP.",
                        "reason": "Add architecture.",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Stack",
                        "content": "Python 3.12, Docker, Redis.",
                        "reason": "Add stack.",
                        "notes": [1],
                    },
                ],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert result["bank_files_created"] == 1
    assert result["operations_applied"] == 2
    synthesis_key = f"{SPACE}/_synthesis.md"
    assert synthesis_key in storage.objects
    assert "operations_applied: 2" in storage.objects[synthesis_key]


async def test_post_dedup_mutation_on_missing_file_enforces_canonical_h1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    # Mock deduplicator to return altered content with modified H1 for missing file
    original_dedup = service._deduplicate_content

    async def _mock_dedup_corrupt_h1(content: str, filename: str):
        if filename == "activeContext.md":
            return "# Corrupted Title\n\n## Section\nContent", 0, None
        return await original_dedup(content, filename)

    monkeypatch.setattr(service, "_deduplicate_content", _mock_dedup_corrupt_h1)

    result = await service._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Section",
                        "content": "Content",
                        "reason": "Add section.",
                        "notes": [1],
                    }
                ],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert f"{SPACE}/bank/activeContext.md" not in storage.objects
    assert any(
        f.get("reason") == "normal_h1_not_preserved"
        for f in result.get("operation_failures", [])
    )


async def test_persistence_failure_reports_only_reached_write_recoveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingStorage(RecordingStorage):
        def __init__(self, fail_on_put_index: int):
            super().__init__()
            self.fail_on_put_index = fail_on_put_index
            self.put_count = 0

        async def put(self, key: str, content: str | bytes) -> None:
            if key.endswith(".md") and "/bank/" in key:
                if self.put_count == self.fail_on_put_index:
                    raise RuntimeError("injected bank put failure")
                self.put_count += 1
            await super().put(key, content)

    # 1. Failure on 1st bank put: 0 files created, 0 recoveries reported
    failing_storage_0 = FailingStorage(fail_on_put_index=0)
    bank_files_0 = _seed(failing_storage_0)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: failing_storage_0)

    result_0 = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Focus",
                        "content": "Focus content",
                        "reason": "Add focus",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "progress.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Status",
                        "content": "Status content",
                        "reason": "Add status",
                        "notes": [1],
                    }
                ],
            },
        ),
        bank_files=bank_files_0,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result_0["status"] == "partial"
    assert result_0["bank_files_created"] == 0
    assert result_0["operations_applied"] == 0
    assert result_0["recovered_operations"] == []

    # 2. Failure on 2nd bank put: 1 file created, exactly 1 recovery reported
    failing_storage_1 = FailingStorage(fail_on_put_index=1)
    bank_files_1 = _seed(failing_storage_1)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: failing_storage_1)

    result_1 = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Focus",
                        "content": "Focus content",
                        "reason": "Add focus",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "progress.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Status",
                        "content": "Status content",
                        "reason": "Add status",
                        "notes": [1],
                    }
                ],
            },
        ),
        bank_files=bank_files_1,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result_1["status"] == "partial"
    assert result_1["bank_files_created"] == 1
    assert result_1["operations_applied"] == 1
    assert result_1["recovered_operations"] == [
        {
            "file_index": 0,
            "filename": "activeContext.md",
            "type": "file",
            "strategy": "create_missing_bank_file",
        }
    ]


async def test_readback_failure_reports_only_verified_write_recoveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadbackCorruptingStorage(RecordingStorage):
        async def get(self, key: str) -> str | None:
            if key == f"{SPACE}/bank/progress.md":
                return "corrupted content"
            return await super().get(key)

    storage = ReadbackCorruptingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Focus",
                        "content": "Focus content",
                        "reason": "Add focus",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "progress.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Status",
                        "content": "Status content",
                        "reason": "Add status",
                        "notes": [1],
                    }
                ],
            },
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "partial"
    assert result["reason"] == "batch_write_failed"
    # Only activeContext.md was successfully verified; progress.md failed readback!
    assert result["recovered_operations"] == [
        {
            "file_index": 0,
            "filename": "activeContext.md",
            "type": "file",
            "strategy": "create_missing_bank_file",
        }
    ]


async def test_readback_exception_reports_only_verified_write_recoveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadbackThrowingStorage(RecordingStorage):
        def __init__(self, throw_on_key: str):
            super().__init__()
            self.throw_on_key = throw_on_key

        async def get(self, key: str) -> str | None:
            if key == self.throw_on_key:
                raise RuntimeError("simulated S3 readback failure")
            return await super().get(key)

    # 1. Exception on 1st readback -> 0 verified recoveries reported
    storage_0 = ReadbackThrowingStorage(throw_on_key=f"{SPACE}/bank/activeContext.md")
    bank_files_0 = _seed(storage_0)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage_0)

    result_0 = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Focus",
                        "content": "Focus content",
                        "reason": "Add focus",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "progress.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Status",
                        "content": "Status content",
                        "reason": "Add status",
                        "notes": [1],
                    }
                ],
            },
        ),
        bank_files=bank_files_0,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result_0["status"] == "partial"
    assert result_0["recovered_operations"] == []

    # 2. Exception on 2nd readback -> 1st verified recovery reported
    storage_1 = ReadbackThrowingStorage(throw_on_key=f"{SPACE}/bank/progress.md")
    bank_files_1 = _seed(storage_1)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage_1)

    result_1 = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "activeContext.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Focus",
                        "content": "Focus content",
                        "reason": "Add focus",
                        "notes": [1],
                    }
                ],
            },
            {
                "filename": "progress.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "add_section",
                        "heading": "## Status",
                        "content": "Status content",
                        "reason": "Add status",
                        "notes": [1],
                    }
                ],
            },
        ),
        bank_files=bank_files_1,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result_1["status"] == "partial"
    assert result_1["recovered_operations"] == [
        {
            "file_index": 0,
            "filename": "activeContext.md",
            "type": "file",
            "strategy": "create_missing_bank_file",
        }
    ]



def test_normal_after_fallback_collision_reports_exact_cardinality() -> None:
    requested = "## Anchor ‐ 2026"
    source = (
        "# Facts\n\n"
        "## Anchor — 2026\n\nfirst\n\n"
        "## Anchor - 2026\n\nsecond\n"
    )

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Added",
                "after": requested,
                "content": "new facts",
                "reason": "The new section follows its anchor.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "ambiguous_or_missing_normal_after",
            "file_index": 0,
            "operation_index": 0,
            "target_resolution": "ambiguous",
            "target_match_count": 2,
            "target_heading_sha256": hashlib.sha256(
                requested.encode("utf-8")
            ).hexdigest(),
        }
    ]


def test_two_normal_aliases_cannot_target_one_source_section() -> None:
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Release — 2026",
                "content": "first addition",
                "reason": "One fact was added.",
                "notes": [1],
            },
            {
                "type": "prepend_to_section",
                "heading": "## Release - 2026",
                "content": "second addition",
                "reason": "Another fact was added.",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "duplicate_normal_target",
            "file_index": 0,
            "operation_index": 1,
        }
    ]


def test_normal_add_cannot_create_a_conservative_heading_collision() -> None:
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release - 2026",
                "content": "new body",
                "reason": "A new section was requested.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "duplicate_normal_target",
            "file_index": 0,
            "operation_index": 0,
        }
    ]


def test_normal_add_cannot_enter_an_existing_conservative_collision() -> None:
    source = (
        "# Facts\n\n"
        "## Release — 2026\n\nfirst\n\n"
        "## Release - 2026\n\nsecond\n"
    )

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release ‐ 2026",
                "content": "third body",
                "reason": "A new section was requested.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "duplicate_normal_target",
            "file_index": 0,
            "operation_index": 0,
        }
    ]


def test_two_normal_adds_cannot_reuse_one_exact_heading() -> None:
    source = "# Facts\n\n## Anchor A\n\nfirst\n\n## Anchor B\n\nsecond\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Added",
                "after": "## Anchor A",
                "content": "first addition",
                "reason": "The first section was requested.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Added",
                "after": "## Anchor B",
                "content": "second addition",
                "reason": "The second section was requested.",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "duplicate_normal_target",
            "file_index": 0,
            "operation_index": 1,
        }
    ]


def test_two_normal_adds_cannot_create_a_conservative_collision() -> None:
    source = "# Facts\n\n## Anchor A\n\nfirst\n\n## Anchor B\n\nsecond\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release — 2026",
                "after": "## Anchor A",
                "content": "first addition",
                "reason": "The first section was requested.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Release - 2026",
                "after": "## Anchor B",
                "content": "second addition",
                "reason": "The second section was requested.",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert failures == [
        {
            "reason": "duplicate_normal_target",
            "file_index": 0,
            "operation_index": 1,
        }
    ]


async def test_normal_failure_projection_drops_unrecognized_content(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "NORMAL_TARGET_SECRET_MUST_NOT_LEAK"
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        consolidator_module,
        "_normal_edit_candidate",
        lambda *_args, **_kwargs: (
            None,
            [
                {
                    "reason": "ambiguous_or_missing_normal_target",
                    "file_index": 0,
                    "operation_index": 0,
                    "target_resolution": "missing",
                    "target_match_count": 0,
                    "target_heading_sha256": "a" * 64,
                    "heading": secret,
                    "completion": secret,
                    "unexpected": secret,
                }
            ],
            [],
        ),
    )

    with caplog.at_level("WARNING", logger="live_mem.consolidator"):
        result = await _service()._write_results(
            space_id=SPACE,
            llm_output=_output(_edit()),
            bank_files=bank_files,
            notes_keys=[NOTE],
            notes_count=1,
            usage={},
            skip_meta=False,
        )

    assert result["operation_failures"] == [
        {
            "reason": "ambiguous_or_missing_normal_target",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": "a" * 64,
        }
    ]
    assert secret not in repr(result)
    assert secret not in caplog.text
    assert storage.snapshot() == before


def test_unknown_normal_failure_cannot_zero_the_semantic_failure_count() -> None:
    result = ConsolidatorService._normal_preparation_error_result(
        space_id=SPACE,
        bank_files=[],
        notes_count=1,
        usage={},
        failure=consolidator_module._NormalBatchPreparationFailure(
            (
                {
                    "reason": "future_normal_failure_not_allowlisted",
                    "untrusted": "must not escape",
                },
            )
        ),
    )

    assert result["status"] == "error"
    assert result["operations_failed"] == 1
    assert result["operation_failures"] == []
    assert "future_normal_failure_not_allowlisted" not in repr(result)
    assert "must not escape" not in repr(result)


@pytest.mark.parametrize(
    ("text", "finish_reason", "expected_reason"),
    [
        (
            json.dumps(_output(_create("new.md"))),
            "length",
            "normal_consolidation_completion_length",
        ),
        (
            json.dumps(_output(_create("new.md"))),
            "content_rejected",
            "normal_consolidation_completion_content_rejected",
        ),
        ("   \n", "stop", "blank_normal_consolidation_completion"),
        (
            '{"file_edits": [{"filename": "new.md", "action": "create"',
            "stop",
            "invalid_normal_consolidation_json",
        ),
        (
            '{"file_edits": [], "file_edits": [{"filename": "new.md", '
            '"action": "create", "content": "# New", "reason": "grounded"}], '
            '"synthesis": "ok"}',
            "stop",
            "invalid_normal_consolidation_json",
        ),
        (
            '{"file_edits": [{"filename": "new.md", "action": "create", '
            '"content": "# New", "reason": "grounded"}], "discarded_notes": [], "synthesis": NaN}',
            "stop",
            "invalid_normal_consolidation_json",
        ),
        (
            '{"file_edits": [{"filename": "new.md", "action": "create", '
            '"content": "# New\\ud800", "reason": "grounded"}], '
            '"synthesis": "ok"}',
            "stop",
            "invalid_normal_utf8",
        ),
        (
            json.dumps({**_output(_create("new.md")), "unknown": True}),
            "stop",
            "invalid_normal_schema",
        ),
    ],
)
async def test_mutating_completion_gate_refuses_unsafe_responses(
    text: str, finish_reason: str, expected_reason: str
) -> None:
    completion = Completion(text, finish_reason=finish_reason)
    result = await _service(completion)._call_llm([{"role": "user", "content": "x"}])

    assert result["status"] == "error"
    assert result["reason"] == expected_reason
    assert completion.calls == 1


async def test_schema_rejection_of_a_parsed_plan_returns_the_parsed_json_for_the_corrective_turn() -> None:
    """A parsed JSON that fails the closed plan schema is the model's
    own form fault. ``_call_llm`` must hand the parsed JSON back (never the raw
    text) so the corrective completion can replay it as the assistant turn."""
    parsed_but_invalid = {"file_edits": [], "synthesis": "x"}  # discarded_notes missing
    completion = Completion(json.dumps(parsed_but_invalid))

    result = await _service(completion)._call_llm([{"role": "user", "content": "x"}])

    assert result["status"] == "error"
    assert result["reason"] == "invalid_normal_schema"
    assert result["operation_failures"] == [{"reason": "invalid_normal_root_schema"}]
    assert result["data"] == parsed_but_invalid
    assert completion.calls == 1


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "prefix_body_chars",
    [None, 337, 1023],
    ids=["no-prefix", "observed-337-char-prose", "1024-char-prefix-bound"],
)
async def test_normal_completion_accepts_one_bounded_json_fence(
    caplog: pytest.LogCaptureFixture,
    line_ending: str,
    prefix_body_chars: int | None,
) -> None:
    plan = _output(_create("new.md"))
    secret = "FENCED_PREFIX_SECRET_MUST_NOT_LEAK"
    prefix = (
        ""
        if prefix_body_chars is None
        else secret + "p" * (prefix_body_chars - len(secret)) + "\n"
    )
    assert not prefix or len(prefix) in {338, 1024}
    text = (
        prefix
        + f"```json{line_ending}"
        + json.dumps(plan)
        + f"{line_ending}```"
    )
    completion = Completion(text)

    with caplog.at_level("INFO", logger="live_mem.consolidator"):
        result = await _service(completion)._call_llm(
            [{"role": "user", "content": "x"}]
        )

    assert result["status"] == "ok"
    assert result["data"] == plan
    assert completion.calls == 1
    assert "bounded_json_fence" in caplog.text
    assert secret not in caplog.text
    assert secret not in repr(result)


async def test_normal_completion_with_embedded_markdown_code_fences_is_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Markdown code fences inside JSON string content must not break the envelope."""
    content_with_code = (
        "# Script Documentation\n\n"
        "Here is the bash script:\n\n"
        "```bash\n"
        "echo 'hello world'\n"
        "ls -la /tmp\n"
        "```\n\n"
        "And Python snippet:\n\n"
        "```python\n"
        "def main():\n"
        "    return 42\n"
        "```\n"
    )
    plan = _output(_create("script.md", content=content_with_code, notes=[1]))
    fenced_text = (
        "Here is the planned update:\n"
        "```json\n"
        + json.dumps(plan)
        + "\n```"
    )
    completion = Completion(fenced_text)

    with caplog.at_level("INFO", logger="live_mem.consolidator"):
        result = await _service(completion)._call_llm(
            [{"role": "user", "content": "x"}]
        )

    assert result["status"] == "ok"
    assert result["data"] == plan
    assert "bounded_json_fence" in caplog.text


@pytest.mark.parametrize(
    ("invalid_notes", "expected_reason"),
    [
        (None, "invalid_normal_file_edit_schema"),
        ([], "invalid_normal_notes"),
        (["1"], "invalid_normal_notes"),
        ([1.0], "invalid_normal_notes"),
        ([True], "invalid_normal_notes"),
        ([0], "invalid_normal_notes"),
        ([-1], "invalid_normal_notes"),
    ],
)
def test_normal_schema_rejects_missing_empty_or_malformed_notes(
    invalid_notes: object,
    expected_reason: str,
) -> None:
    if invalid_notes is None:
        plan = {
            "file_edits": [
                {
                    "filename": "new.md",
                    "action": "create",
                    "content": "# New\n\nbody\n",
                    "reason": "Required.",
                }
            ],
            "discarded_notes": [],
            "synthesis": "done",
        }
    else:
        plan = {
            "file_edits": [
                {
                    "filename": "new.md",
                    "action": "create",
                    "content": "# New\n\nbody\n",
                    "reason": "Required.",
                    "notes": invalid_notes,
                }
            ],
            "discarded_notes": [],
            "synthesis": "done",
        }
    failures = consolidator_module._normal_output_schema_failures(plan)
    assert failures, f"Expected failures for notes={invalid_notes!r}"
    assert any(f["reason"] == expected_reason for f in failures)


async def test_direct_normal_json_remains_primary_without_recovery_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = _output(_create("new.md"))
    completion = Completion(json.dumps(plan))

    with caplog.at_level("INFO", logger="live_mem.consolidator"):
        result = await _service(completion)._call_llm(
            [{"role": "user", "content": "x"}]
        )

    assert result["status"] == "ok"
    assert result["data"] == plan
    assert completion.calls == 1
    assert "bounded_json_fence" not in caplog.text


async def test_bounded_json_fence_still_requires_the_closed_normal_schema(
    caplog: pytest.LogCaptureFixture,
) -> None:
    completion = Completion(
        "preface\n```json\n"
        + json.dumps({**_output(_create("new.md")), "unknown": True})
        + "\n```"
    )

    with caplog.at_level("INFO", logger="live_mem.consolidator"):
        result = await _service(completion)._call_llm(
            [{"role": "user", "content": "x"}]
        )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_normal_schema"
    assert completion.calls == 1
    assert "bounded_json_fence" not in caplog.text


def test_bounded_normal_parser_rejects_non_text_before_direct_parsing() -> None:
    data, error, recovery = consolidator_module._bounded_normal_json_completion(
        b"{}"  # type: ignore[arg-type]
    )

    assert data is None
    assert error == "invalid_normal_consolidation_json"
    assert recovery is None


async def test_bounded_json_fence_passes_the_existing_prepare_apply_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _output(_edit())
    completion = Completion(
        "I will return the requested object.\n```json\n"
        + json.dumps(plan)
        + "\n```"
    )
    service = _service(completion)
    parsed = await service._call_llm([{"role": "user", "content": "x"}])
    assert parsed["status"] == "ok"
    assert parsed["data"] == plan

    storage = RecordingStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service._write_results(
        space_id=SPACE,
        llm_output=parsed["data"],
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert "- newly verified fact" in storage.objects[BANK_KEY]
    assert NOTE not in storage.objects


@pytest.mark.parametrize(
    "text",
    [
        # The prefix bound is inclusive and counts the physical newline that
        # places the Markdown fence at the start of its line.
        "p" * 1024
        + "\n```json\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```",
        "preface```json\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```",
        "preface\n```JSON\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```",
        "preface\n```\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```",
        "preface\n```json "
        + json.dumps(_output(_create("new.md")))
        + "```",
        "preface\n```json\n"
        + json.dumps(_output(_create("new.md")))
        + "```",
        "preface\n```json\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```\ntrailing prose",
        "preface\n```json\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```\n```json\n{}\n```",
        "preface\n```json\n{\"file_edits\": [\n```",
        "\ud800\n```json\n"
        + json.dumps(_output(_create("new.md")))
        + "\n```",
        "preface\n```json\n"
        '{"file_edits": [], "file_edits": [], "discarded_notes": [], "synthesis": "ok"}'
        "\n```",
        "preface\n```json\n"
        '{"file_edits": [], "discarded_notes": [], "synthesis": NaN}'
        "\n```",
    ],
)
async def test_normal_completion_rejects_every_other_fence_shape(
    text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    completion = Completion(text)

    with caplog.at_level("INFO", logger="live_mem.consolidator"):
        result = await _service(completion)._call_llm(
            [{"role": "user", "content": "x"}]
        )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_normal_consolidation_json"
    assert completion.calls == 1
    assert "preface" not in caplog.text
    assert "file_edits" not in caplog.text
    assert "preface" not in repr(result)
    assert "file_edits" not in repr(result)


async def test_mutating_completion_gate_cannot_extract_or_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*_args, **_kwargs):
        raise AssertionError("normal mutating completion must not salvage output")

    monkeypatch.setattr(consolidator_module, "_extract_json", unreachable)
    monkeypatch.setattr(consolidator_module, "_repair_json", unreachable)
    monkeypatch.setattr(consolidator_module, "_close_json_structure", unreachable)
    result = await _service(
        Completion("preface\n```json\n{\"file_edits\": [\n```")
    )._call_llm(
        [{"role": "user", "content": "x"}]
    )

    assert result["status"] == "error"


@pytest.mark.parametrize(
    "completion",
    [
        Completion("merged", finish_reason="length"),
        Completion("\n\t", finish_reason="stop"),
        Completion("\u200b", finish_reason="stop"),
        Completion("\x00", finish_reason="stop"),
        Completion("ignored", error=RuntimeError("provider failure")),
    ],
)
async def test_failed_deduplication_preserves_every_original_byte(
    completion: Completion,
) -> None:
    source = "# Doc\n\n## Status\n\nolder fact\n\n## Status\n\nnewer fact\n"

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "new.md"
    )

    assert candidate == source
    assert merged_count == 0
    assert error in {"deduplication_merge_failed", "deduplication_iteration_limit"}


async def test_dedup_whitespace_variants_require_a_real_merge() -> None:
    """A hard-break-only difference is not safe for the automatic fast path."""

    source = (
        "# Doc\n\n"
        "## Status\n\n"
        "line with a Markdown hard break  \nnext line\n\n"
        "## Status\n\n"
        "line with a Markdown hard break\nnext line\n"
    )
    completion = Completion("", finish_reason="length")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "whitespace.md"
    )

    assert completion.calls == 1
    assert candidate == source
    assert merged_count == 0
    assert error == "deduplication_merge_failed"


async def test_dedup_merge_uses_reasoning_inclusive_profile_budget() -> None:
    completion = Completion("merged direct body")
    service = _service(
        completion,
        max_tokens=200_000,
        context_window=1_000_000,
    )

    merged = await service._merge_sections_via_llm(
        "## Status", ["older direct body", "newer direct body"]
    )

    assert merged == "merged direct body"
    assert completion.calls == 1
    assert completion.output_budgets == [200_000]
    assert completion.retry_policies == ["bounded"]


def test_dedup_merge_budget_respects_profile_and_remaining_context() -> None:
    messages = [{"role": "user", "content": "é" * 30}]
    estimated_input = (
        consolidator_module._strict_compaction_input_tokens(messages[0]["content"])
        + 16
    )

    profile_limited = _service(max_tokens=2048, context_window=1_000_000)
    assert profile_limited._dedup_merge_output_budget(messages) == 2048

    context_limited = _service(
        max_tokens=200_000,
        context_window=estimated_input + 12_345,
    )
    assert context_limited._dedup_merge_output_budget(messages) == 12_345


def test_dedup_merge_budget_requires_visible_body_reservation() -> None:
    messages = [{"role": "user", "content": "input"}]
    estimated_input = (
        consolidator_module._strict_compaction_input_tokens(messages[0]["content"])
        + 16
    )
    service = _service(
        max_tokens=200_000,
        context_window=estimated_input + 4096,
    )

    assert service._dedup_merge_output_budget(messages) == 4096

    service._context_window -= 1
    assert service._dedup_merge_output_budget(messages) is None


async def test_dedup_merge_context_refusal_has_no_provider_egress() -> None:
    heading = "## Status"
    versions = ["older direct body", "newer direct body"]
    probe = Completion("merged direct body")
    probe_service = _service(
        probe,
        max_tokens=200_000,
        context_window=1_000_000,
    )
    assert await probe_service._merge_sections_via_llm(heading, versions) is not None
    estimated_input = sum(
        consolidator_module._strict_compaction_input_tokens(message["content"])
        for message in probe.messages[0]
    ) + 16 * len(probe.messages[0])

    completion = Completion("must not be called")
    service = _service(
        completion,
        max_tokens=200_000,
        context_window=estimated_input + 4095,
    )

    merged = await service._merge_sections_via_llm(heading, versions)

    assert merged is None
    assert completion.calls == 0


async def test_dedup_hierarchy_key_cannot_collide_on_literal_separator_text() -> None:
    """Distinct ancestor paths containing `` > `` are never merged together."""

    source = (
        "# Root\n\n"
        "## A > ### B\n\n"
        "#### Target\n\n"
        "first\n\n"
        "## A\n\n"
        "### B\n\n"
        "#### Target\n\n"
        "second\n"
    )
    completion = Completion("must not be called")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "paths.md"
    )

    assert candidate == source
    assert merged_count == 0
    assert error is None
    assert completion.calls == 0


async def test_verified_bank_write_precedes_synthesis_metadata_and_note_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[NOTE] = "source note"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("new.md")),
        bank_files=[],
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    bank_put = storage.events.index(("put", f"{SPACE}/bank/new.md"))
    bank_readback = storage.events.index(("get", f"{SPACE}/bank/new.md"))
    synthesis_put = storage.events.index(("put", SYNTHESIS_KEY))
    metadata_put = storage.events.index(("put_json", META_KEY))
    note_delete = next(
        index
        for index, event in enumerate(storage.events)
        if event[0] == "delete"
    )
    assert bank_put < bank_readback < synthesis_put < metadata_put < note_delete
    assert NOTE not in storage.objects


async def test_bank_readback_failure_retains_sources_and_never_publishes_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MismatchStorage(RecordingStorage):
        async def get(self, key: str) -> str | None:
            value = await super().get(key)
            if key == f"{SPACE}/bank/new.md":
                return "unexpected readback"
            return value

    storage = MismatchStorage()
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[NOTE] = "source note"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("new.md")),
        bank_files=[],
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "partial"
    assert result["operation_failures"] == [
        {"reason": "normal_bank_readback_failed"}
    ]
    assert NOTE in storage.objects
    assert SYNTHESIS_KEY not in storage.objects
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_synthesis_readback_failure_retains_sources_and_never_publishes_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A divergent synthesis readback must stop before metadata or note deletion."""

    class MismatchStorage(RecordingStorage):
        async def get(self, key: str) -> str | None:
            value = await super().get(key)
            if key == SYNTHESIS_KEY:
                return "unexpected synthesis readback"
            return value

    storage = MismatchStorage()
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[NOTE] = "source note"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("new.md")),
        bank_files=[],
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "partial"
    assert result["operation_failures"] == [
        {"reason": "normal_synthesis_readback_failed"}
    ]
    assert NOTE in storage.objects
    assert all(event[0] != "put_json" for event in storage.events)
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_metadata_readback_failure_retains_sources_in_direct_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct writer must verify metadata before it consumes its source note."""

    class MismatchStorage(RecordingStorage):
        def __init__(self) -> None:
            super().__init__()
            self.metadata_written = False

        async def put_json(self, key: str, data: dict) -> None:
            await super().put_json(key, data)
            if key == META_KEY:
                self.metadata_written = True

        async def get_json(self, key: str) -> dict | None:
            value = await super().get_json(key)
            if key == META_KEY and self.metadata_written:
                return {"unexpected": "metadata readback"}
            return value

    storage = MismatchStorage()
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[NOTE] = "source note"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("new.md")),
        bank_files=[],
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "partial"
    assert result["operation_failures"] == [
        {"reason": "normal_metadata_readback_failed"}
    ]
    assert NOTE in storage.objects
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_legacy_unicode_alias_cleanup_follows_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unambiguous legacy alias is removed only after canonical durability."""

    storage = RecordingStorage()
    legacy_key = f"{SPACE}/bank/facts\u200b.md"
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[NOTE] = "source note"
    storage.objects[legacy_key] = FACTS
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit("facts.md")),
        bank_files=[{"key": legacy_key, "content": FACTS}],
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    canonical_put = storage.events.index(("put", BANK_KEY))
    canonical_readback = storage.events.index(("get", BANK_KEY))
    legacy_delete = storage.events.index(("delete", legacy_key))
    assert canonical_put < canonical_readback < legacy_delete
    assert BANK_KEY in storage.objects
    assert legacy_key not in storage.objects


async def test_collect_inputs_excludes_keep_sentinels_with_real_storage_contract() -> None:
    """Normal collection requests the default filtered listing for both inputs."""

    storage = RecordingStorage()
    live_key = f"{SPACE}/live/20000101T000000_alice_observation_keepcheck.md"
    live_keep_key = f"{SPACE}/live/.keep"
    bank_keep_key = f"{SPACE}/bank/.keep"
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[live_key] = "live note"
    storage.objects[live_keep_key] = "live sentinel"
    storage.objects[BANK_KEY] = FACTS
    storage.objects[bank_keep_key] = "bank sentinel"
    service = _service()
    service._max_notes = 10

    inputs = await service._collect_inputs(SPACE, storage=storage)

    assert inputs["notes_keys"] == [live_key]
    assert inputs["bank_files"] == [{"key": BANK_KEY, "content": FACTS}]
    assert storage.list_and_get_calls == [
        (f"{SPACE}/live/", True),
        (f"{SPACE}/bank/", True),
    ]
    assert [item["key"] for item in await storage.list_and_get(
        f"{SPACE}/live/", exclude_keep=False
    )] == [live_keep_key, live_key]
    assert [item["key"] for item in await storage.list_and_get(
        f"{SPACE}/bank/", exclude_keep=False
    )] == [bank_keep_key, BANK_KEY]


async def test_normal_edit_uses_the_raw_fence_aware_heading_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fenced example and trailing spaces must not retarget an edit."""

    source = (
        "# Facts\n\n"
        "```md\n"
        "## Status\n"
        "fenced example\n"
        "```\n\n"
        "## Status  \n\n"
        "real status\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Status  ",
                        "content": "- exact physical target",
                        "reason": "The real status gained a fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "```md\n## Status\nfenced example\n```" in persisted
    assert "## Status  \n\nreal status\n\n- exact physical target\n" in persisted


async def test_normal_edit_refuses_when_compaction_fence_spans_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab-permissive compaction closer cannot expose normal-editor code."""

    source = (
        "# Root\n\n"
        "~~~\n"
        "\t~~~\n"
        "## Ghost\n"
        "SECRET-CODE-BYTES\n"
        "## Still-Code\n"
        "\t~~~\n"
        "~~~\n\n"
        "## Real\n\n"
        "keep\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "delete_section",
                        "heading": "## Ghost",
                        "reason": "This apparent section is obsolete.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "unsupported_normal_markdown_structure",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before
    assert "SECRET-CODE-BYTES" in storage.objects[BANK_KEY]


async def test_first_edit_cannot_redirect_later_target_with_injected_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All operation spans are resolved from the immutable source snapshot."""

    source = (
        "# Facts\n\n"
        "## First\n\n"
        "old first\n\n"
        "## Second\n\n"
        "### Target\n\n"
        "original target\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## First",
                        "content": (
                            "new first\n\n"
                            "### Target\n\n"
                            "injected sibling-looking target"
                        ),
                        "reason": "The first section was updated.",
                        "notes": [1],
                    },
                    {
                        "type": "append_to_section",
                        "heading": "### Target",
                        "content": "- appended to the original target",
                        "reason": "The original target gained a fact.",
                        "notes": [1],
                    },
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "### Target\n\ninjected sibling-looking target" in persisted
    assert "### Target\n\noriginal target\n\n- appended to the original target" in persisted


async def test_add_after_a_deleted_anchor_is_refused_without_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Anchor\n\nold\n\n## Other\n\nkept\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "delete_section",
                        "heading": "## Anchor",
                        "reason": "The obsolete section is removed.",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Replacement",
                        "after": "## Anchor",
                        "content": "replacement facts",
                        "reason": "The replacement follows the anchor.",
                        "notes": [1],
                    },
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_after_anchor_modified",
            "file_index": 0,
            "operation_index": 1,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_add_after_an_appended_anchor_is_accepted_and_ordered_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Travaux en cours\n\n- Tache A\n\n## Autre\n\n- Tache Z\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                filename="facts.md",
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Travaux en cours",
                        "content": "- Tache B ajoutee",
                        "reason": "Ajout de la tache B",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Decisions recentes",
                        "after": "## Travaux en cours",
                        "content": "- Decision GPU actee",
                        "reason": "Insertion de la decision",
                        "notes": [1],
                    },
                ],
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "- Tache A" in persisted
    assert "- Tache B ajoutee" in persisted
    assert "## Decisions recentes\n\n- Decision GPU actee" in persisted
    assert "## Autre\n\n- Tache Z" in persisted
    assert persisted.index("## Travaux en cours") < persisted.index("## Decisions recentes") < persisted.index("## Autre")


async def test_add_after_a_prepended_anchor_is_accepted_and_ordered_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Travaux en cours\n\n- Tache A\n\n## Autre\n\n- Tache Z\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                filename="facts.md",
                operations=[
                    {
                        "type": "prepend_to_section",
                        "heading": "## Travaux en cours",
                        "content": "- Tache 0 prioritaire",
                        "reason": "Ajout prioritaire",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Decisions recentes",
                        "after": "## Travaux en cours",
                        "content": "- Decision GPU actee",
                        "reason": "Insertion de la decision",
                        "notes": [1],
                    },
                ],
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "- Tache 0 prioritaire" in persisted
    assert "- Tache A" in persisted
    assert "## Decisions recentes\n\n- Decision GPU actee" in persisted
    assert "## Autre\n\n- Tache Z" in persisted
    assert persisted.index("## Travaux en cours") < persisted.index("## Decisions recentes") < persisted.index("## Autre")
    assert persisted.index("- Tache 0 prioritaire") < persisted.index("- Tache A")


async def test_add_after_an_anchor_with_multiple_appends_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Travaux en cours\n\n- Tache A\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                filename="facts.md",
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Travaux en cours",
                        "content": "- Tache B",
                        "reason": "Append 1",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Next",
                        "after": "## Travaux en cours",
                        "content": "new section content",
                        "reason": "Add section",
                        "notes": [1],
                    },
                    {
                        "type": "append_to_section",
                        "heading": "## Travaux en cours",
                        "content": "- Tache C",
                        "reason": "Append 2",
                        "notes": [1],
                    },
                ],
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "- Tache A\n\n- Tache B\n\n- Tache C" in persisted
    assert "## Next\n\nnew section content" in persisted


async def test_add_after_a_replaced_anchor_is_accepted_and_ordered_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Section A\n\nold content A\n\n## Section B\n\nold content B\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                filename="facts.md",
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Section A",
                        "content": "replaced content A",
                        "reason": "Update section A",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Section Inserted",
                        "after": "## Section A",
                        "content": "newly inserted section",
                        "reason": "Insert after replaced section A",
                        "notes": [1],
                    },
                ],
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert "replaced content A" in persisted
    assert "old content A" not in persisted
    assert "## Section Inserted\n\nnewly inserted section" in persisted
    assert persisted.index("## Section A") < persisted.index("## Section Inserted") < persisted.index("## Section B")


async def test_add_after_a_deleted_parent_anchor_fails_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Parent\n\n### Child\n\nchild fact\n\n## Other\n\nkept\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "delete_section",
                        "heading": "## Parent",
                        "reason": "The entire parent is removed.",
                        "notes": [1],
                    },
                    {
                        "type": "add_section",
                        "heading": "## Injected",
                        "after": "### Child",
                        "content": "should fail because parent was deleted",
                        "reason": "After deleted child",
                        "notes": [1],
                    },
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_after_anchor_modified",
            "file_index": 0,
            "operation_index": 1,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_rewrite_with_an_extra_h1_is_refused_without_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "# Facts\n\n## Status\n\nold fact\n\n# Injected\n",
                "reason": "A second root must never be accepted.",
                "notes": [1],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_h1_not_preserved",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "source",
    [
        "Facts\n=====\n\n## Status\n\nold fact\n",
        "# Facts\n\nIndependent\n-----------\n\n## Status\n\nold fact\n",
    ],
    ids=["setext-h1", "setext-h2"],
)
async def test_setext_heading_source_fails_closed_before_any_edit(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "unsupported_normal_markdown_structure",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_model_body_cannot_inject_a_setext_h2_with_dashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``---`` after prose is a Setext H2, never a harmless body fragment."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": (
                            "updated status\n\n"
                            "Independent\n"
                            "---\n\n"
                            "injected hierarchy"
                        ),
                        "reason": "The status was updated.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "invalid_normal_replacement_structure",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
            "detail": "setext_heading",
        }
    ]
    assert storage.snapshot() == before


@pytest.mark.parametrize("indent", [" ", "  ", "   "], ids=["one", "two", "three"])
async def test_indented_atx_heading_source_fails_closed_before_any_edit(
    monkeypatch: pytest.MonkeyPatch,
    indent: str,
) -> None:
    """CommonMark ATX headings indented up to three spaces are not prose."""

    source = (
        "# Facts\n\n"
        f"{indent}## Hidden sibling\n\n"
        "hidden content\n\n"
        "## Status\n\n"
        "old fact\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "unsupported_normal_markdown_structure",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


@pytest.mark.parametrize("indent", [" ", "  ", "   "], ids=["one", "two", "three"])
async def test_model_body_cannot_inject_an_indented_atx_heading(
    monkeypatch: pytest.MonkeyPatch,
    indent: str,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": (
                            "updated status\n\n"
                            f"{indent}## Injected sibling\n\n"
                            "injected hierarchy"
                        ),
                        "reason": "The status was updated.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "invalid_normal_replacement_structure",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
            "detail": "unsupported_atx_heading",
        }
    ]
    assert storage.snapshot() == before


async def test_empty_atx_heading_source_fails_closed_before_any_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty H1 is still a structural boundary, not editable prose."""

    source = "# Facts\n\n## Status\n\nold fact\n\n#\n\nprotected root\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] in {
        "unsupported_normal_markdown_structure",
        "invalid_normal_source_structure",
    }
    assert storage.snapshot() == before


async def test_model_body_cannot_inject_an_empty_atx_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": "updated status\n\n#\n\ninjected root",
                        "reason": "The status was updated.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "invalid_normal_replacement_structure"
    )
    assert storage.snapshot() == before


async def test_add_after_cannot_reparent_the_next_source_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shallower insertion before a sibling would silently change ownership."""

    source = (
        "# Facts\n\n"
        "## Parent\n\n"
        "### Anchor\n\n"
        "anchor facts\n\n"
        "### Later\n\n"
        "must stay a child of Parent\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "add_section",
                        "heading": "## New parent",
                        "after": "### Anchor",
                        "content": "would reparent Later",
                        "reason": "This hierarchy change is unsafe.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_add_reparents_source",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_add_after_may_insert_a_deeper_child_without_reparenting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# Facts\n\n"
        "## Parent\n\n"
        "### Anchor\n\n"
        "anchor facts\n\n"
        "### Later\n\n"
        "still a child of Parent\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "add_section",
                        "heading": "#### New child",
                        "after": "### Anchor",
                        "content": "nested facts",
                        "reason": "The anchor gained a child.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert persisted.index("#### New child") < persisted.index("### Later")
    assert "### Later\n\nstill a child of Parent\n" in persisted


async def test_prepend_cannot_reparent_an_existing_source_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# Facts\n\n"
        "## Parent\n\n"
        "#### Existing child\n\n"
        "must keep Parent as its owner\n\n"
        "## Later\n\n"
        "unrelated\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "prepend_to_section",
                        "heading": "## Parent",
                        "content": "### Inserted parent\n\nwould adopt Existing child",
                        "reason": "This would change source ownership.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_prepend_reparents_source",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_raw_html_block_source_fails_closed_before_any_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heading-looking line inside raw HTML is data, never an edit target."""

    source = (
        "# Facts\n\n"
        "<script>\n"
        "## Internal config\n"
        "secret = true\n"
        "</script>\n\n"
        "## Status\n\n"
        "old fact\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "delete_section",
                        "heading": "## Internal config",
                        "reason": "Must never target raw HTML data.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "unsupported_normal_markdown_structure"
    )
    assert storage.snapshot() == before


async def test_model_body_cannot_introduce_a_raw_html_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": "updated fact\n\n<!--\n## hidden data\n-->",
                        "reason": "Must never add an opaque block.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "invalid_normal_replacement_structure"
    )
    assert storage.snapshot() == before


@pytest.mark.parametrize("prefix", ["", "\ufeff"], ids=["plain", "bom-prefixed"])
async def test_yaml_front_matter_source_fails_closed_before_any_edit(
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    source = (
        f"{prefix}---\n"
        "## private config\n"
        "...\n\n"
        "# Facts\n\n"
        "## Status\n\n"
        "old fact\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "unsupported_normal_markdown_structure"
    )
    assert storage.snapshot() == before


async def test_hidden_bom_prefixed_h1_cannot_be_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "\ufeff# Facts\n\n## Status\n\nold fact\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "\ufeff# Changed\n\n## Status\n\nnew fact\n",
                "reason": "Must not change a hidden root heading.",
                "notes": [1],
            }
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == "normal_h1_not_preserved"
    assert storage.snapshot() == before


async def test_model_body_cannot_introduce_a_hidden_atx_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": "updated fact\n\n\ufeff# injected root",
                        "reason": "Must not add a hidden root heading.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "invalid_normal_replacement_structure"
    )
    assert storage.snapshot() == before


async def test_dedup_failure_keeps_duplicates_and_still_persists_the_unrelated_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused merge retains the duplicates; it no longer discards the batch.

    The seeded bank already carries a duplicate ``## Status``.  Refusing the
    whole batch for it meant this space could never consolidate again until a
    human edited the file by hand: every later run would re-attempt the same
    merge and fail the same way.  Deduplication is a defensive post-pass, not
    the work the caller asked for, so a refused merge now retains the
    duplicates, is counted, and lets the unrelated edit reach storage.

    What must not change: the refused merge never reaches storage, and the
    batch stays all-or-nothing.
    """

    source = (
        "# Facts\n\n"
        "## Status\n\nolder version\n\n"
        "## Status\n\nnewer version\n\n"
        "## Other\n\nunchanged\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    completion = Completion("not terminal", finish_reason="length")
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service(completion)._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Other",
                        "content": "- kept because dedup is only a post-pass",
                        "reason": "The unrelated section gained a fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert "operation_failures" not in result
    assert result["dedup_failures_count"] == 1
    # Aucun retour au modèle : un refus reste un refus, pas une relance.
    assert completion.calls == 1

    persisted = storage.objects[BANK_KEY]
    # Le travail valide est conservé au lieu d'être jeté avec le doublon.
    assert "- kept because dedup is only a post-pass" in persisted
    # Les deux occurrences survivent byte-a-byte : rien n'a ete fusionne.
    assert persisted.count("## Status") == 2
    assert "older version" in persisted and "newer version" in persisted


async def test_noop_edit_reports_zero_operations_in_result_and_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Facts\n\n## Status\n\nold fact\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": "\nold fact",
                        "reason": "The source already contains the exact fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    # An attributed edit that changes nothing means the fact was
    # already in the bank by the model's own account; the note is consumed and
    # nothing durable is written, not even the synthesis.
    assert result["status"] == "ok"
    assert result["notes_deleted"] == 1
    assert result["operations_applied"] == 0
    assert result["synthesis_written"] is False
    assert SYNTHESIS_KEY not in storage.objects
    assert NOTE not in storage.objects


async def test_metadata_readback_failure_keeps_deferred_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run-level metadata must verify before the one deferred deletion."""

    class MetadataReadbackMismatchStorage(RecordingStorage):
        metadata_written = False

        async def put_json(self, key: str, data: dict) -> None:
            await super().put_json(key, data)
            if key == META_KEY:
                self.metadata_written = True

        async def get_json(self, key: str) -> dict | None:
            value = await super().get_json(key)
            if key == META_KEY and self.metadata_written:
                return {"unexpected": "metadata readback"}
            return value

    storage = MetadataReadbackMismatchStorage()
    bank_files = _seed(storage)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = _service()
    service._batch_size = 10
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "discarded_notes": [],
            "synthesis": "",
        }
    )
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={
            "status": "ok",
            "data": _output(_create("metadata.md")),
            "usage": {},
        }
    )

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["metadata_update_failed"] is True
    assert result["failure_reason"] == "metadata_update_failed"
    assert result["notes_deleted"] == 0
    assert result["notes_remaining"] == 1
    assert NOTE in storage.objects
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_overlong_filename_refuses_the_whole_sibling_batch_before_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend key bound is part of normal batch preflight, not apply."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("good.md"), _create("é" * 600 + ".md")),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {"reason": "invalid_normal_filename", "file_index": 1}
    ]
    assert storage.snapshot() == before


async def test_lone_surrogate_refuses_every_sibling_before_storage_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict JSON must not defer a UnicodeEncodeError until after a put."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _create("good.md"),
            _create("bad.md", content="body" + chr(0xD800)),
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [{"reason": "invalid_normal_utf8"}]
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "fence_fragment",
    [
        "``` invalid ` info\n## Protected\nsecret\n```",
        "\t```\n## Protected\nsecret\n\t```",
    ],
    ids=["backtick-in-info", "tab-indented"],
)
async def test_unsupported_fence_lookalikes_in_source_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    fence_fragment: str,
) -> None:
    storage = RecordingStorage()
    source = f"# Facts\n\n## Status\n\nold fact\n\n{fence_fragment}\n"
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] in {
        "unsupported_normal_markdown_structure",
        "invalid_normal_source_structure",
    }
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "fence_fragment",
    [
        "``` invalid ` info\n## Protected\nsecret\n```",
        "\t```\n## Protected\nsecret\n\t```",
    ],
    ids=["backtick-in-info", "tab-indented"],
)
async def test_model_body_cannot_introduce_unsupported_fence_lookalikes(
    monkeypatch: pytest.MonkeyPatch,
    fence_fragment: str,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": f"updated fact\n\n{fence_fragment}",
                        "reason": "The model body must retain a complete grammar.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "invalid_normal_replacement_structure"
    )
    assert storage.snapshot() == before


async def test_commented_yaml_document_markers_are_an_opaque_source_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    source = (
        "--- # generated metadata\n"
        "## private config\n"
        "flag: true\n"
        "... # end metadata\n\n"
        "# Facts\n\n## Status\n\nold fact\n"
    )
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "unsupported_normal_markdown_structure"
    )
    assert storage.snapshot() == before


async def test_invisible_heading_text_is_never_an_ambiguous_normal_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    source = "# Facts\n\n## Sta\u200btus\n\nhidden\n\n## Status\n\nold fact\n"
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit()),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "unsupported_normal_markdown_structure"
    )
    assert storage.snapshot() == before


async def test_model_body_cannot_introduce_an_invisible_heading_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "replace_section",
                        "heading": "## Status",
                        "content": "updated\n\n## Sta\u200btus\n\nhidden",
                        "reason": "Visual target aliases are not safe.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "invalid_normal_replacement_structure"
    )
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"consolidation_count": True},
        {"consolidation_count": -1},
        {"total_notes_processed": 1.5},
    ],
    ids=["wrong-type", "boolean-counter", "negative-counter", "float-counter"],
)
async def test_invalid_metadata_is_not_overwritten_or_finalized(
    monkeypatch: pytest.MonkeyPatch,
    metadata: object,
) -> None:
    """Corrupt metadata must not advance counters or consume source notes."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    storage.objects[META_KEY] = json.dumps(metadata)
    original_meta = storage.objects[META_KEY]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("new.md")),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "partial"
    assert result["operation_failures"] == [{"reason": "normal_persistence_failure"}]
    assert storage.objects[META_KEY] == original_meta
    assert NOTE in storage.objects
    assert all(event[0] != "put_json" for event in storage.events)
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_surrogate_dedup_merge_is_counted_and_never_reaches_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTANT — an invalid-UTF-8 merge must be dropped, not persisted.

    This is the assertion that must never weaken: tolerating a refused merge
    means the pre-dedup candidate is written, so nothing may leak from the
    rejected merge output itself.
    """

    source = (
        "# Facts\n\n## Status\n\nolder version\n\n"
        "## Status\n\nnewer version\n\n"
        "## Other\n\nunchanged\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service(Completion("merged" + chr(0xD800)))._write_results(
        space_id=SPACE,
        llm_output=_output(
            _create("good.md"),
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Other",
                        "content": "- valid sibling edit",
                        "reason": "The unrelated section gained a fact.",
                        "notes": [1],
                    }
                ]
            ),
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert result["dedup_failures_count"] == 1
    # Le lot valide aboutit : creation et edition frere sont persistees.
    assert f"{SPACE}/bank/good.md" in storage.objects
    persisted = storage.objects[BANK_KEY]
    assert "- valid sibling edit" in persisted
    assert persisted.count("## Status") == 2
    # Rien de la fusion rejetee n'atteint le stockage, sous aucune forme.
    for content in storage.objects.values():
        assert chr(0xD800) not in content
        assert "merged" not in content


@pytest.mark.parametrize(
    ("operation_type", "content"),
    [
        ("replace_section", "replacement direct body"),
        ("append_to_section", "appended direct body"),
        ("prepend_to_section", "prepended direct body"),
        ("delete_section", None),
    ],
)
async def test_normal_parent_operations_preserve_descendant_bytes(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    content: str | None,
) -> None:
    """Normal editing keeps nested sections out of a parent's direct body."""

    source = (
        "# Facts\n\n"
        "## Parent\n\n"
        "old direct body\n\n"
        "### Child\n\n"
        "child evidence must remain byte-for-byte\n\n"
        "## Later\n\n"
        "unrelated evidence\n"
    )
    child_suffix = source[source.index("### Child") :]
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    operation = {
        "type": operation_type,
        "heading": "## Parent",
        "reason": "Apply only the parent direct body.",
        "notes": [1],
    }
    if content is not None:
        operation["content"] = content

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_edit(operations=[operation])),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    persisted = storage.objects[BANK_KEY]
    assert persisted[persisted.index("### Child") :] == child_suffix
    if operation_type == "delete_section":
        assert "## Parent" not in persisted
    else:
        assert "## Parent" in persisted


@pytest.mark.parametrize(
    ("operation_type", "expected_reason"),
    [
        ("replace_section", "normal_replace_reparents_source"),
        ("append_to_section", "normal_append_reparents_source"),
        ("prepend_to_section", "normal_prepend_reparents_source"),
    ],
)
async def test_normal_parent_body_cannot_reparent_a_descendant(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    expected_reason: str,
) -> None:
    """A generated shallower heading cannot adopt an existing source child."""

    source = (
        "# Facts\n\n"
        "## Parent\n\n"
        "old direct body\n\n"
        "#### Existing child\n\n"
        "must remain under Parent\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": operation_type,
                        "heading": "## Parent",
                        "content": "### Generated child\n\nwould adopt Existing child",
                        "reason": "Changing the existing hierarchy is unsafe.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": expected_reason,
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_successful_dedup_preserves_distinct_descendant_sections() -> None:
    """Dedup merges direct bodies only; children never become disposable spans."""

    first_child = "### Older child\n\nolder child evidence\n\n"
    second_child = "### Newer child\n\nnewer child evidence\n"
    source = (
        "# Doc\n\n"
        "## Status\n\n"
        "older direct body\n\n"
        f"{first_child}"
        "## Status\n\n"
        "newer direct body\n\n"
        f"{second_child}"
    )
    completion = Completion("merged direct body")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "facts.md"
    )

    assert error is None
    assert merged_count == 1
    assert completion.calls == 1
    assert candidate.count("## Status") == 1
    assert "merged direct body" in candidate
    assert first_child in candidate
    assert second_child in candidate


async def test_dedup_merge_refuses_obvious_net_expansion() -> None:
    source = (
        "# Doc\n\n"
        "## Status\n\n"
        "older direct body\n\n"
        "## Status\n\n"
        "newer direct body\n"
    )
    completion = Completion("expanded " * 100)

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "facts.md"
    )

    assert error == "deduplication_merge_expansion_refused"
    assert merged_count == 0
    assert completion.calls == 1
    assert candidate == source


async def test_dedup_merge_checks_rendered_crlf_candidate_size() -> None:
    body_1 = "\r\n" + ("a" * 20 + "\r\n") * 20
    body_2 = "\r\n" + ("b" * 20 + "\r\n") * 20
    source = (
        "# Doc\r\n\r\n"
        "## Status\r\n"
        f"{body_1}"
        "## Status\r\n"
        f"{body_2}"
    )
    source_body_bytes = len(body_1.encode()) + len(body_2.encode())
    newline_count = 60
    merged = ("x\n" * newline_count) + (
        "y" * (source_body_bytes - 1 - (2 * newline_count))
    )
    assert len(merged.encode()) == source_body_bytes - 1

    candidate, merged_count, error = await _service(
        Completion(merged)
    )._deduplicate_content(source, "crlf.md")

    assert error == "deduplication_merge_expansion_refused"
    assert merged_count == 0
    assert candidate == source


async def test_dedup_merge_allows_lossless_net_reduction() -> None:
    body_1 = "\r\nalpha evidence\r\n"
    body_2 = "\r\nbeta evidence\r\n"
    source = (
        "# Doc\r\n\r\n"
        "## Status\r\n"
        f"{body_1}"
        "## Status\r\n"
        f"{body_2}"
    )
    merged = (body_1 + body_2).replace("\r\n", "\n")

    candidate, merged_count, error = await _service(
        Completion(merged)
    )._deduplicate_content(source, "lossless.md")

    assert error is None
    assert merged_count == 1
    assert len(candidate.encode()) <= len(source.encode())
    assert "alpha evidence" in candidate
    assert "beta evidence" in candidate


async def test_dedup_does_not_merge_children_newly_reparented_by_parent_removal() -> None:
    """A synthetic group rolls back instead of widening the merge scope.

    Removing the first duplicate parent's heading/direct body leaves its child
    bytes in place.  That can reparent the child under an earlier sibling with
    the same child heading, but this synthetic relationship was not a duplicate
    in the source and must never trigger a second LLM merge in the same pass.
    The complete in-memory pass therefore refuses the candidate and preserves
    every original byte rather than persisting a partial deduplication.
    """

    source = (
        "# Root\n\n"
        "## A\n\n"
        "### C\n\n"
        "ALPHA-UNIQUE\n\n"
        "## Dup\n\n"
        "first duplicate body\n\n"
        "### C\n\n"
        "GAMMA-UNIQUE\n\n"
        "## Dup\n\n"
        "second duplicate body\n"
    )
    completion = Completion("merged duplicate parent body")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "cascade.md"
    )

    assert error == "deduplication_unresolved_duplicate_groups"
    assert merged_count == 0
    assert completion.calls == 1
    assert candidate == source


async def test_dedup_rolls_back_when_a_synthetic_child_joins_an_original_group() -> None:
    """A reparented child may not be re-merged into an existing source path."""

    source = (
        "# Root\n\n"
        "## A\n\n"
        "### C\n\n"
        "ALPHA-FIRST\n\n"
        "### C\n\n"
        "ALPHA-SECOND\n\n"
        "## Dup\n\n"
        "first duplicate body\n\n"
        "### C\n\n"
        "GAMMA-UNIQUE\n\n"
        "## Dup\n\n"
        "second duplicate body\n"
    )
    completion = Completion("merged snapshot group")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "cascade-existing-path.md"
    )

    assert error == "deduplication_unresolved_duplicate_groups"
    assert merged_count == 0
    # The two source groups are prepared exactly once.  The reparented third
    # child is never submitted as a newly manufactured third merge request.
    assert completion.calls == 2
    assert candidate == source


async def test_dedup_refuses_more_than_fifty_source_duplicate_groups() -> None:
    """Freezing groups preserves the old bounded provider-call contract."""

    source = "# Root\n\n" + "".join(
        f"## Group {index}\n\nfirst\n\n## Group {index}\n\nsecond\n\n"
        for index in range(51)
    )
    completion = Completion("must not be called")

    candidate, merged_count, error = await _service(completion)._deduplicate_content(
        source, "many-groups.md"
    )

    assert candidate == source
    assert merged_count == 0
    assert error == "deduplication_iteration_limit"
    assert completion.calls == 0


async def test_post_dedup_reduction_guard_refuses_a_lossy_edit_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reduction floor runs again after successful duplicate collapse."""

    duplicate = "## Status\n\n" + ("repeated durable evidence " * 30) + "\n\n"
    source = "# Facts\n\n" + duplicate * 5 + "## Other\n\nkeep\n"
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Other",
                        "content": "- a valid direct fact",
                        "reason": "The other section gained one fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "normal_edit_reduction_refused",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_normalized_collision_only_blocks_its_own_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy aliases neither self-heal nor block an unrelated safe create."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    legacy_key = f"{SPACE}/bank/facts\u200b.md"
    legacy_content = "# Legacy\n\nseparate historical object\n"
    storage.objects[legacy_key] = legacy_content
    bank_files.append({"key": legacy_key, "content": legacy_content})
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("unrelated.md")),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "ok"
    assert storage.objects[BANK_KEY] == FACTS
    assert storage.objects[legacy_key] == legacy_content
    assert f"{SPACE}/bank/unrelated.md" in storage.objects


async def test_normalized_collision_target_refuses_every_sibling_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targeting an ambiguous legacy alias remains a complete-batch refusal."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    legacy_key = f"{SPACE}/bank/facts\u200b.md"
    storage.objects[legacy_key] = "# Legacy\n\nseparate historical object\n"
    bank_files.append({"key": legacy_key, "content": storage.objects[legacy_key]})
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(_create("unrelated.md"), _edit("facts.md")),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "ambiguous_normalized_bank_target",
            "file_index": 1,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


async def test_empty_normal_file_edits_refuse_before_any_durable_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop completion with no operation is not authorization to consume notes.

    the refusal is no longer the empty list itself but the note left
    without disposition.  The same empty ``file_edits`` with every note declared
    useless is a valid plan that consumes the notes without touching the bank.
    """

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {"reason": "normal_notes_unclassified", "missing_notes": [1]}
    ]
    assert storage.snapshot() == before

    # Contrast: the complete disposition passes and consumes the note only.
    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(discarded=[{"note": 1, "reason": "already_in_bank"}]),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )
    assert result["status"] == "ok"
    assert result["notes_deleted"] == 1
    assert result["notes_discarded"] == [1]
    assert result["synthesis_written"] is False
    assert NOTE not in storage.objects
    after = storage.snapshot()
    assert after[BANK_KEY] == before[BANK_KEY]
    assert f"{SPACE}/_synthesis.md" not in {k for k in after if k not in before}


async def test_direct_consolidate_projects_normal_failures_without_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "DIRECT_NORMAL_FAILURE_SECRET_MUST_NOT_LEAK"
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "discarded_notes": [],
            "synthesis": "",
        }
    )
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        return_value={
            "status": "error",
            "message": "plan refused",
            "operation_failures": [
                {
                    "reason": "ambiguous_or_missing_normal_target",
                    "file_index": 0,
                    "operation_index": 0,
                    "filename": "facts.md",
                    "target_resolution": "missing",
                    "target_match_count": 0,
                    "target_heading_sha256": "c" * 64,
                    "heading": secret,
                    "completion": secret,
                }
            ],
        }
    )

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert result["status"] == "error"
    assert result["operations_failed"] == 1
    assert result["operation_failures"] == [
        {
            "reason": "ambiguous_or_missing_normal_target",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": "c" * 64,
        }
    ]
    assert secret not in repr(result)
    assert storage.snapshot() == before


async def test_later_batch_failure_finalizes_only_the_verified_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real normal writer consumes only notes covered by verified batches."""

    second_note = f"{SPACE}/live/20000101T000001_alice_observation_cafebabe.md"
    storage = RecordingStorage()
    bank_files = _seed(storage)
    storage.objects[second_note] = "second source note"
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [
                {"key": NOTE, "content": "first source note"},
                {"key": second_note, "content": "second source note"},
            ],
            "notes_keys": [NOTE, second_note],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "discarded_notes": [],
            "synthesis": "",
        }
    )
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        side_effect=[
            {
                "status": "ok",
                "data": _output(_create("first-batch.md")),
                "usage": {},
            },
            {
                "status": "error",
                "message": "second batch refused",
                "operation_failures": [{"reason": "invalid_normal_completion"}],
            },
        ]
    )

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["failed_batch"] == 2
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["batches_completed"] == 1
    assert result["notes_processed"] == 1
    assert result["notes_deleted"] == 1
    assert result["notes_remaining"] == 1
    assert NOTE not in storage.objects
    assert second_note in storage.objects
    assert f"{SPACE}/bank/first-batch.md" in storage.objects
    metadata = await storage.get_json(META_KEY)
    assert metadata["consolidation_count"] == 1
    assert metadata["total_notes_processed"] == 1


async def test_later_persistence_failure_retains_the_verified_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed later write can invalidate the earlier batch's deletion proof."""

    second_note = f"{SPACE}/live/20000101T000001_alice_observation_cafebabe.md"
    shared_key = f"{SPACE}/bank/shared.md"

    class LaterWriteMismatchStorage(RecordingStorage):
        def __init__(self) -> None:
            super().__init__()
            self.shared_writes = 0

        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key == shared_key:
                self.shared_writes += 1
                if self.shared_writes == 2:
                    # Model a PUT that reached durable storage but whose
                    # readback proves it did not retain the intended bytes.
                    self.objects[key] = "TRUNCATED BY FAILED SECOND BATCH"

    storage = LaterWriteMismatchStorage()
    bank_files = _seed(storage)
    storage.objects[second_note] = "second source note"
    initial_metadata = storage.objects[META_KEY]
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [
                {"key": NOTE, "content": "first source note"},
                {"key": second_note, "content": "second source note"},
            ],
            "notes_keys": [NOTE, second_note],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "discarded_notes": [],
            "synthesis": "",
        }
    )
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []
    service._call_llm = AsyncMock(
        side_effect=[
            {
                "status": "ok",
                "data": _output(
                    _create(
                        "shared.md",
                        content="# Shared\n\n## Status\n\nfirst\n",
                    )
                ),
                "usage": {},
            },
            {
                "status": "ok",
                "data": _output(
                    _edit(
                        "shared.md",
                        operations=[
                            {
                                "type": "replace_section",
                                "heading": "## Status",
                                "content": "second",
                                "reason": "The second note supersedes it.",
                                "notes": [1],
                            }
                        ],
                    )
                ),
                "usage": {},
            },
        ]
    )

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert result["status"] == "partial"
    assert result["failure_reason"] == "batch_write_failed"
    assert result["failed_batch"] == 2
    assert result["batches_completed"] == 1
    # Nothing was consumed: the prefix was not finalized.
    assert result["notes_processed"] == 0
    assert result["notes_processed"] == len(result["notes_applied"]) + len(result["notes_discarded"])
    assert result["notes_applied"] == []
    assert result["notes_discarded"] == []
    assert result["notes_retained"] == []
    assert result["notes_deleted"] == 0
    assert result["notes_remaining"] == 2
    assert NOTE in storage.objects
    assert second_note in storage.objects
    assert storage.objects[shared_key] == "TRUNCATED BY FAILED SECOND BATCH"
    assert storage.objects[META_KEY] == initial_metadata
    assert all(event[0] != "put_json" for event in storage.events)
    assert all(event[0] not in {"delete", "delete_many"} for event in storage.events)


async def test_nonterminal_completion_in_full_consolidation_keeps_storage_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real pipeline rejects both truncated completions before any write."""

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    completion = Completion(
        json.dumps(_output(_create("new.md"))), finish_reason="length"
    )
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service(completion)
    service._batch_size = 1
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "discarded_notes": [],
            "synthesis": "",
        }
    )
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert completion.calls == 2
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["failed_batch"] == 1
    assert result["notes_processed"] == 0
    assert result["notes_deleted"] == 0
    assert storage.snapshot() == before


async def test_unreadable_candidate_still_refuses_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTANT — the one dedup refusal that must keep failing closed.

    ``deduplication_invalid_structure`` can mean the candidate itself is
    unreadable by the strict lexer, not merely that a merge failed.  Persisting
    such a document would poison the file: every later consolidation would fail
    closed on it, turning a recoverable batch refusal into a permanently stuck
    space.

    Upstream has no equivalent guard — its splitter is a permissive line regex
    that cannot refuse a document — so this refusal is Hivemind-specific and is
    deliberately not relaxed by the tolerance ported for the other refusals.
    """

    source = (
        "# Facts\n\n"
        "## Status\n\nolder version\n\n"
        "## Status\n\nnewer version\n\n"
        "## Other\n\nunchanged\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    # Le lexer strict declare le candidat illisible.
    monkeypatch.setattr(
        consolidator_module, "_strict_normal_duplicates", lambda content: None
    )

    result = await _service(Completion("merged"))._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Other",
                        "content": "- must not be persisted into an unreadable file",
                        "reason": "The unrelated section gained a fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "deduplication_invalid_structure",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]
    # Fail-closed integral : aucune ecriture, la note source survit.
    assert storage.snapshot() == before
    assert NOTE in storage.objects


# ── #452 lot 1.1 — récupération d'une section absorbée ────────────────────
#
# Porté de live-mem 2.9.1 (amont #62/#63). Une opération visant un chapitre
# qu'une compaction antérieure a absorbé ne refuse plus le lot : le chapitre est
# recréé en fin de fichier, via le chemin de rendu d'``add_section``, donc sous
# les mêmes protections (H1 protégé, corps validé, anti-doublon).
#
# Compromis produit accepté : cela recrée un chapitre que le
# modèle a nommé sans prouver qu'il ait existé. Les tests marqués RISQUE ACCEPTÉ
# documentent précisément ce qui passe à travers.

_RECOVERY_SOURCE = "# Facts\n\n## Status\n\nbody\n"
_ABSORBED = "## Historique 2026-07"


@pytest.mark.parametrize(
    "operation_type",
    ["replace_section", "append_to_section", "prepend_to_section"],
)
def test_absorbed_heading_is_recreated_instead_of_refusing_the_batch(
    operation_type: str,
) -> None:
    """RED avant 1.1 : le lot entier était refusé, les notes restaient bloquées."""

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": operation_type,
                "heading": _ABSORBED,
                "content": "- entrée que la compaction avait absorbée",
                "reason": "The chapter was absorbed by an earlier compaction.",
            }
        ],
        0,
    )

    assert failures == []
    assert candidate is not None
    # Recréé en fin de fichier, sans parent inféré ni span existant touché.
    assert candidate.startswith(_RECOVERY_SOURCE)
    assert _ABSORBED in candidate
    assert "- entrée que la compaction avait absorbée" in candidate
    # Le contenu d'origine survit intact.
    assert "## Status" in candidate and "body" in candidate

    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery["type"] == operation_type
    assert recovery["strategy"] == "append_missing_section"
    assert recovery["operation_index"] == 0
    # Diagnostic sans contenu : empreinte du titre, jamais le titre en clair.
    assert _ABSORBED not in str(recovery)
    assert len(recovery["heading_sha256"]) == 64


def test_recovery_never_fires_on_an_ambiguous_target() -> None:
    """MUTANT — la garde d'ambiguïté de #443 prime sur la règle d'absence.

    Un titre dupliqué reste irrésolvable. Le récupérer reviendrait à créer une
    troisième occurrence d'un chapitre déjà ambigu.
    """

    source = "# Facts\n\n## Notes\n\nfirst\n\n## Notes\n\nsecond\n"
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Notes",
                "content": "- addition",
                "reason": "ambiguous target",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert recoveries == []
    assert failures[0]["reason"] == "ambiguous_or_missing_normal_target"
    assert failures[0]["target_resolution"] == "ambiguous"
    assert failures[0]["target_match_count"] == 2


def test_delete_of_an_absent_heading_still_refuses() -> None:
    """MUTANT — divergence délibérée avec l'amont, qui rend ce cas idempotent.

    L'amont n'a pas de résolution conservative, donc « absent » y signifie
    seulement « réellement absent ». Ici cela couvre aussi « tu as nommé
    quelque chose de très proche d'un chapitre existant, mais pas assez pour
    résoudre ». Répondre « déjà supprimé » dirait au modèle que sa suppression
    a eu lieu alors que le chapitre qu'il visait survit intact, définitivement.
    """

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "delete_section",
                "heading": _ABSORBED,
                "reason": "already absorbed",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert recoveries == []
    assert failures[0]["reason"] == "ambiguous_or_missing_normal_target"


def test_recovery_refuses_to_recreate_a_protected_h1() -> None:
    """MUTANT — la topologie H1 reste immuable, y compris par récupération."""

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "append_to_section",
                "heading": "# Autre racine",
                "content": "- contenu",
                "reason": "absent H1",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert recoveries == []
    assert failures[0]["reason"] == "protected_normal_h1_target"


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_recovery_refuses_an_empty_body(body: str) -> None:
    """MUTANT — recréer un chapitre vide ajouterait du bruit, pas du contenu."""

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "append_to_section",
                "heading": _ABSORBED,
                "content": body,
                "reason": "empty body",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert recoveries == []
    assert failures[0]["reason"] == "invalid_normal_replacement_structure"


def test_two_operations_on_one_absent_heading_are_refused() -> None:
    """MUTANT — garde-fou propre à Hivemind, absent de l'amont.

    L'amont évalue séquentiellement, donc son second appel voit le chapitre
    recréé par le premier. Hivemind résout toutes les plages contre un
    instantané immuable, précisément pour que le contenu du modèle ne puisse
    pas rediriger une opération ultérieure. Sans ce refus, deux opérations
    créeraient deux fois le même chapitre.
    """

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "append_to_section",
                "heading": _ABSORBED,
                "content": "- première",
                "reason": "first",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": _ABSORBED,
                "content": "- seconde",
                "reason": "second",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert failures[0]["reason"] == "duplicate_normal_target"
    assert failures[0]["operation_index"] == 1


def test_recovery_cannot_collide_with_an_explicit_add_section() -> None:
    """MUTANT — récupération et création partagent le même registre anti-doublon."""

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "add_section",
                "heading": _ABSORBED,
                "content": "- créé explicitement",
                "reason": "explicit add",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": _ABSORBED,
                "content": "- récupéré",
                "reason": "recovery attempt",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert failures[0]["reason"] == "duplicate_normal_target"


@pytest.mark.parametrize(
    ("requested", "label"),
    [
        ("## Statuss", "faute de frappe"),
        ("### Status", "niveau différent"),
        # #457 item 3 — « casse différente » a QUITTÉ cette liste : le troisième
        # étage de résolution la fait désormais atterrir dans le chapitre
        # existant.  C'est la bascule que le docstring ci-dessous annonçait.
        ("## Chapitre totalement inventé", "titre jamais vu"),
    ],
)
def test_recovery_creates_a_chapter_the_model_merely_named(
    requested: str, label: str
) -> None:
    """RISQUE ACCEPTÉ — documente précisément ce qui passe à travers.

    Le code n'intercepte que « cible absente » : il ne prouve pas que le titre
    ait existé. Une faute de frappe, un mauvais niveau ou un titre purement
    inventé produit donc un nouveau chapitre à côté de celui que le modèle
    visait sans doute. Tolérance arbitrée le 2026-08-28, alignée sur l'amont,
    atténuée par le compteur de récupérations.

    Ce test n'approuve pas le comportement : il le rend visible et mesurable.
    Si un durcissement est décidé plus tard, c'est ce test qui devra basculer.

    **Il a basculé une première fois.** #457 item 3 a retiré « casse
    différente » de cette liste : un écart de casse résout maintenant vers le
    chapitre existant au lieu d'en créer un second. Le reste du risque demeure.
    """

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        _RECOVERY_SOURCE,
        [
            {
                "type": "append_to_section",
                "heading": requested,
                "content": f"- {label}",
                "reason": "near miss",
                "notes": [1],
            }
        ],
        0,
    )

    assert failures == []
    assert candidate is not None
    assert len(recoveries) == 1
    # Le chapitre visé survit intact ; un second, distinct, apparait.
    assert "## Status\n\nbody" in candidate
    assert requested in candidate


# ── #452 lot 1.2 — la frontière de tolérance est fail-closed ──────────────
#
# Tolérer un refus sans vérifier que le helper a bien rendu le candidat intact
# laisserait une version future ou mutée
# de ``_deduplicate_content`` persister du contenu non vérifié, puis supprimer
# la seule copie des notes sources. La condition initiale était aussi
# fail-OPEN : tout jeton nouveau ou mal orthographié passait par omission.

_TOLERATED = sorted(consolidator_module._TOLERATED_DEDUP_REFUSALS)


def _dedup_stub(returned_content, reason):
    async def stub(content, filename):
        return (content if returned_content is None else returned_content), 0, reason

    return stub


async def _run_with_dedup_stub(monkeypatch, stub):
    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    service = _service()
    monkeypatch.setattr(service, "_deduplicate_content", stub)
    result = await service._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Status",
                        "content": "- addition",
                        "reason": "The section gained a fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )
    return result, storage, before


@pytest.mark.parametrize("reason", _TOLERATED)
async def test_each_tolerated_refusal_continues_when_content_is_untouched(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les six motifs sûrs, un par un — aucun ne repose sur une omission."""

    result, _storage, _before = await _run_with_dedup_stub(
        monkeypatch, _dedup_stub(None, reason)
    )

    assert result["status"] == "ok"
    assert result["dedup_failures_count"] == 1
    assert "operation_failures" not in result


@pytest.mark.parametrize("reason", _TOLERATED)
async def test_altered_content_on_a_tolerated_refusal_fails_closed(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTANT — un refus ne doit jamais faire confiance aux octets rendus.

    Si le helper signale un refus tout en rendant un contenu différent, il viole
    son contrat. Persister ces octets puis supprimer les notes sources
    transformerait un bug de fusion en corruption silencieuse irrécupérable.
    """

    result, storage, before = await _run_with_dedup_stub(
        monkeypatch, _dedup_stub("# Facts\n\ncontenu altere\n", reason)
    )

    assert result["status"] == "error"
    assert result["operation_failures"] == [
        {
            "reason": "deduplication_contract_violation",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]
    # Aucune ecriture, et la note source survit.
    assert storage.snapshot() == before
    assert NOTE in storage.objects
    assert "contenu altere" not in storage.objects[BANK_KEY]


@pytest.mark.parametrize(
    "reason",
    [
        "deduplication_merge_faild",  # faute de frappe
        "deduplication_some_future_reason",  # jeton ajoute plus tard
        "totally_unrelated_token",
    ],
)
async def test_unknown_dedup_reason_fails_closed(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTANT — la tolérance est une allowlist positive, pas un défaut.

    Un jeton renommé, mal orthographié ou introduit plus tard doit refuser le
    lot. Tolérer par omission ferait silencieusement passer un refus dont
    personne n'a établi qu'il est sûr.
    """

    result, storage, before = await _run_with_dedup_stub(
        monkeypatch, _dedup_stub(None, reason)
    )

    assert result["status"] == "error"
    assert storage.snapshot() == before
    assert NOTE in storage.objects
    # Le motif inconnu est filtre par l'allowlist gelee des diagnostics, donc
    # la charge utile est vide : le lot echoue quand meme, ce qui est l'essentiel.
    assert result["operations_failed"] >= 1


async def test_malformed_dedup_signal_produces_a_structured_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTANT — un signal non hachable doit refuser, pas lever TypeError.

    Le test d'appartenance à l'allowlist suppose un signal hachable ;
    un helper rendant une liste produisait un TypeError au
    lieu du refus structuré. L'échec restait sûr (aucune écriture, note
    conservée) mais privait l'opérateur du diagnostic.
    """

    result, storage, before = await _run_with_dedup_stub(
        monkeypatch, _dedup_stub(None, ["deduplication_merge_failed"])
    )

    assert result["status"] == "error"
    assert storage.snapshot() == before
    assert NOTE in storage.objects
    assert result["operations_failed"] >= 1


async def test_a_recovery_is_never_consumed_when_the_edit_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTANT — ne pas lire un retour après un échec.

    Les récupérations étaient consommées avant la
    barrière de refus. Les chemins actuels rendent toujours une liste vide avec
    un échec, mais rien ne l'impose : une récupération malformée levait un
    KeyError à la place du refus structuré.
    """

    storage = RecordingStorage()
    bank_files = _seed(storage)
    before = storage.snapshot()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        consolidator_module,
        "_normal_edit_candidate",
        lambda *_a, **_k: (
            None,
            [{"reason": "ambiguous_or_missing_normal_target", "file_index": 0,
              "operation_index": 0, "target_resolution": "missing",
              "target_match_count": 0, "target_heading_sha256": "a" * 64}],
            [{"malformed": "no type, no strategy"}],
        ),
    )

    result = await _service()._write_results(
        space_id=SPACE,
        llm_output=_output(
            _edit(
                operations=[
                    {
                        "type": "append_to_section",
                        "heading": "## Status",
                        "content": "- addition",
                        "reason": "The section gained a fact.",
                        "notes": [1],
                    }
                ]
            )
        ),
        bank_files=bank_files,
        notes_keys=[NOTE],
        notes_count=1,
        usage={},
        skip_meta=False,
    )

    assert result["status"] == "error"
    assert result["operation_failures"][0]["reason"] == (
        "ambiguous_or_missing_normal_target"
    )
    # La recuperation malformee n'a jamais ete lue.
    assert "recovered_operations" not in result
    assert storage.snapshot() == before
    assert NOTE in storage.objects


# ---------------------------------------------------------------------------
# #457 item 3 — un ecart de casse ne doit plus forker l'historique.
# ---------------------------------------------------------------------------


def test_a_case_only_difference_resolves_instead_of_forking_the_history() -> None:
    """La casse seule résout, et n'invente donc plus de second chapitre.

    Avant #452 un écart de casse REFUSAIT le lot : bruyant, mais inoffensif.
    Depuis #452 une cible non résolue est RÉCUPÉRÉE, donc le même écart créait
    un chapitre en double et forkait l'historique en silence. Ce test prouve
    que l'édition atterrit bien dans le chapitre existant.
    """
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## release — 2026",
                "content": "appended line",
                "reason": "The note extends the release chapter.",
                "notes": [1],
            }
        ],
        0,
    )

    assert failures == []
    # Aucune récupération : le chapitre existant a été trouvé, pas recréé.
    assert recoveries == []
    assert candidate is not None
    assert candidate.count("Release — 2026") == 1
    assert "release — 2026" not in candidate.replace("Release — 2026", "")
    assert "appended line" in candidate


def test_a_case_folded_match_still_refuses_when_it_is_not_unique() -> None:
    """L'unicité reste obligatoire au troisième étage, comme aux deux premiers.

    Deux chapitres qui ne diffèrent que par la casse sont indiscernables une
    fois repliés : avancer choisirait arbitrairement, ce que la garde
    d'ambiguïté de RC6 (#443) existe précisément pour empêcher.
    """
    source = "# Facts\n\n## Release\n\nfirst\n\n## RELEASE\n\nsecond\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## release",
                "content": "appended",
                "reason": "The note extends the release chapter.",
                "notes": [1],
            }
        ],
        0,
    )

    assert candidate is None
    assert failures[0]["reason"] == "ambiguous_or_missing_normal_target"
    assert failures[0]["target_resolution"] == "ambiguous"
    assert failures[0]["target_match_count"] == 2


def test_a_case_folded_match_never_crosses_atx_levels() -> None:
    """Un niveau différent est un autre endroit du document, pas une dérive.

    Replier la casse ne doit jamais autoriser une H3 à capturer une H2 : ce
    serait déplacer une écriture mutante d'un chapitre vers un autre. La cible
    reste donc ``missing``, ce qui déclenche la récupération de #452 — un
    nouveau chapitre est créé et le ``## Release`` existant n'est pas touché.
    """
    source = "# Facts\n\n## Release\n\nbody\n"

    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "### release",
                "content": "appended",
                "reason": "The note extends the release chapter.",
                "notes": [1],
            }
        ],
        0,
    )

    assert failures == []
    # La H2 existante n'a pas été capturée : c'est une création, pas une édition.
    assert len(recoveries) == 1
    assert recoveries[0]["strategy"] == "append_missing_section"
    assert candidate is not None
    assert "## Release\n\nbody\n" in candidate
    assert "### release" in candidate


def test_a_case_only_variant_cannot_fork_the_history_inside_one_batch() -> None:
    """Le registre intra-lot doit replier la casse.

    Le troisième étage de résolution ne couvre que les titres présents dans le
    SOURCE. Sans repli dans le registre anti-doublon, un ``add_section``
    explicite de ``## STATUS`` suivi de la récupération de ``## Status``
    passait, et les DEUX titres étaient persistés — exactement le fork que cet
    item existe pour empêcher, survivant dans le chemin même-lot.
    """
    source = "# Facts\n\nbody\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## STATUS",
                "content": "first",
                "reason": "The note opens a status chapter.",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Status",
                "content": "second",
                "reason": "The note extends the status chapter.",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert [f["reason"] for f in failures] == ["duplicate_normal_target"]


def test_two_case_only_recoveries_in_one_batch_are_refused() -> None:
    """Même garde sur deux récupérations, pas seulement add puis récupération."""
    source = "# Facts\n\nbody\n"

    candidate, failures, _recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Release Notes",
                "content": "first",
                "reason": "The note opens a release chapter.",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## RELEASE NOTES",
                "content": "second",
                "reason": "The note extends the release chapter.",
                "notes": [1],
            },
        ],
        0,
    )

    assert candidate is None
    assert [f["reason"] for f in failures] == ["duplicate_normal_target"]


def test_two_distinct_add_sections_at_eof_are_sequentially_appended() -> None:
    """Multiple add_section operations in the same batch at EOF must chain cleanly."""
    source = "# Progress\n\nInitial content.\n"
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Section Alpha",
                "content": "Body of Alpha",
                "reason": "First note adds Alpha.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Section Beta",
                "content": "Body of Beta",
                "reason": "Second note adds Beta.",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert (
        candidate
        == "# Progress\n\nInitial content.\n\n## Section Alpha\n\nBody of Alpha\n\n## Section Beta\n\nBody of Beta\n"
    )


def test_two_distinct_missing_sections_recovered_at_eof_are_sequentially_appended() -> None:
    """Multiple missing section recoveries targeting EOF must not abort as conflicting insertions."""
    source = "# Progress\n\nInitial content.\n"
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Missing Alpha",
                "content": "Body Alpha",
                "reason": "Note 1 extends Alpha.",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Missing Beta",
                "content": "Body Beta",
                "reason": "Note 2 extends Beta.",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert len(recoveries) == 2
    assert [r["strategy"] for r in recoveries] == [
        "append_missing_section",
        "append_missing_section",
    ]
    assert candidate is not None
    assert (
        candidate
        == "# Progress\n\nInitial content.\n\n## Missing Alpha\n\nBody Alpha\n\n## Missing Beta\n\nBody Beta\n"
    )


def test_two_distinct_add_sections_after_same_anchor_are_sequentially_appended() -> None:
    """Multiple add_section operations after the same existing anchor preserve order between existing chapters."""
    source = "# Progress\n\n## Chapter 1\n\nBody 1\n\n## Chapter 2\n\nBody 2\n"
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Sub 1A",
                "after": "## Chapter 1",
                "content": "Body 1A",
                "reason": "First insert.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Sub 1B",
                "after": "## Chapter 1",
                "content": "Body 1B",
                "reason": "Second insert.",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert (
        candidate
        == "# Progress\n\n## Chapter 1\n\nBody 1\n\n\n## Sub 1A\n\nBody 1A\n\n## Sub 1B\n\nBody 1B\n\n## Chapter 2\n\nBody 2\n"
    )


def test_two_distinct_add_sections_at_eof_preserve_crlf_line_endings() -> None:
    """Multiple add_section operations in a CRLF document preserve CRLF throughout chained insertions."""
    source = "# Progress\r\n\r\nInitial content.\r\n"
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Section Alpha",
                "content": "Body of Alpha",
                "reason": "First note adds Alpha.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Section Beta",
                "content": "Body of Beta",
                "reason": "Second note adds Beta.",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert (
        candidate
        == "# Progress\r\n\r\nInitial content.\r\n\r\n## Section Alpha\r\n\r\nBody of Alpha\r\n\r\n## Section Beta\r\n\r\nBody of Beta\r\n"
    )


def test_two_distinct_add_sections_at_eof_without_terminal_newline() -> None:
    """Multiple add_section operations on content without trailing newline format cleanly."""
    source = "# Progress\n\nInitial content."
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Section Alpha",
                "content": "Body of Alpha",
                "reason": "First note adds Alpha.",
                "notes": [1],
            },
            {
                "type": "add_section",
                "heading": "## Section Beta",
                "content": "Body of Beta",
                "reason": "Second note adds Beta.",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert (
        candidate
        == "# Progress\n\nInitial content.\n\n## Section Alpha\n\nBody of Alpha\n\n## Section Beta\n\nBody of Beta"
    )


def test_two_appends_to_same_section_are_sequentially_chained() -> None:
    """Multiple append_to_section operations on the same section must chain cleanly."""
    source = (
        "# Progress\n\n"
        "## Recent Work\n\n"
        "- Initial item 1\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Recent Work",
                "content": "- Second item 2",
                "reason": "Note 1 adds item 2",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Recent Work",
                "content": "- Third item 3",
                "reason": "Note 2 adds item 3",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert candidate == (
        "# Progress\n\n"
        "## Recent Work\n\n"
        "- Initial item 1\n\n"
        "- Second item 2\n\n"
        "- Third item 3\n"
    )


def test_three_appends_to_same_section_with_crlf_are_sequentially_chained() -> None:
    """Multiple appends preserve CRLF line endings across chained additions."""
    source = (
        "# Progress\r\n\r\n"
        "## Focus\r\n\r\n"
        "- Task A\r\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Focus",
                "content": "- Task B",
                "reason": "Note 1 adds task B",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Focus",
                "content": "- Task C",
                "reason": "Note 2 adds task C",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Focus",
                "content": "- Task D",
                "reason": "Note 3 adds task D",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert candidate == (
        "# Progress\r\n\r\n"
        "## Focus\r\n\r\n"
        "- Task A\r\n\r\n"
        "- Task B\r\n\r\n"
        "- Task C\r\n\r\n"
        "- Task D\r\n"
    )


def test_two_prepends_to_same_section_are_sequentially_chained() -> None:
    """Multiple prepend_to_section operations on the same section must chain cleanly."""
    source = (
        "# Progress\n\n"
        "## Feed\n\n"
        "- Oldest entry\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "prepend_to_section",
                "heading": "## Feed",
                "content": "- Middle entry",
                "reason": "Note 1 prepends middle entry",
                "notes": [1],
            },
            {
                "type": "prepend_to_section",
                "heading": "## Feed",
                "content": "- Newest entry",
                "reason": "Note 2 prepends newest entry",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert recoveries == []
    assert candidate is not None
    assert candidate == (
        "# Progress\n\n"
        "## Feed\n"
        "- Newest entry\n\n"
        "- Middle entry\n\n"
        "- Oldest entry\n"
    )


def test_add_section_on_exact_existing_heading_recovers_as_append() -> None:
    """add_section targeting an already existing exact heading is recovered as append."""
    source = (
        "# Progress\n\n"
        "## Status\n\n"
        "Current status content.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Status",
                "content": "Additional status info.",
                "reason": "Model issued add_section instead of append_to_section",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Status",
                "content": "More status details.",
                "reason": "Note 2 extends status",
                "notes": [1],
            },
        ],
        0,
    )
    assert failures == []
    assert len(recoveries) == 1
    assert recoveries[0]["strategy"] == "append_existing_section"
    assert candidate is not None
    assert candidate == (
        "# Progress\n\n"
        "## Status\n\n"
        "Current status content.\n\n"
        "Additional status info.\n\n"
        "More status details.\n"
    )


def test_conflicting_operations_on_same_section_are_refused() -> None:
    """replace_section and append_to_section on same section must fail closed."""
    source = (
        "# Progress\n\n"
        "## Status\n\n"
        "Current status content.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "replace_section",
                "heading": "## Status",
                "content": "Replaced content.",
                "reason": "Replace section",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Status",
                "content": "Appended content.",
                "reason": "Append section",
                "notes": [1],
            },
        ],
        0,
    )
    assert candidate is None
    assert len(failures) == 1
    assert failures[0]["reason"] == "duplicate_normal_target"
    assert failures[0]["operation_index"] == 1


def test_two_appends_with_different_aliases_are_refused() -> None:
    """Two appends using different aliases/casing for the same section must fail closed."""
    source = (
        "# Progress\n\n"
        "## Release — 2026\n\n"
        "Existing notes.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Release — 2026",
                "content": "First note.",
                "reason": "Note 1",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Release - 2026",
                "content": "Second note.",
                "reason": "Note 2 with normalized alias",
                "notes": [1],
            },
        ],
        0,
    )
    assert candidate is None
    assert len(failures) == 1
    assert failures[0]["reason"] == "duplicate_normal_target"
    assert failures[0]["operation_index"] == 1


def test_append_with_child_heading_followed_by_append_is_refused() -> None:
    """Chaining an append after an append with child headings would reparent direct body and is refused."""
    source = (
        "# Progress\n\n"
        "## Section\n\n"
        "Original body.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Section",
                "content": "First note.\n\n### Child Heading\n\nChild body.",
                "reason": "Note 1 introduces child heading",
                "notes": [1],
            },
            {
                "type": "append_to_section",
                "heading": "## Section",
                "content": "Second note direct body.",
                "reason": "Note 2 intended for parent direct body",
                "notes": [1],
            },
        ],
        0,
    )
    assert candidate is None
    assert len(failures) == 1
    assert failures[0]["reason"] == "normal_append_reparents_source"
    assert failures[0]["operation_index"] == 1


def test_prepend_with_child_heading_is_refused_when_chained() -> None:
    """Prepending a block with child headings would adopt subsequent prepends and existing body and is refused."""
    source = (
        "# Progress\n\n"
        "## Section\n\n"
        "Original body.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "prepend_to_section",
                "heading": "## Section",
                "content": "First note.",
                "reason": "Note 1",
                "notes": [1],
            },
            {
                "type": "prepend_to_section",
                "heading": "## Section",
                "content": "Second note.\n\n### Child Heading\n\nChild body.",
                "reason": "Note 2 with child heading",
                "notes": [1],
            },
        ],
        0,
    )
    assert candidate is None
    assert len(failures) == 1
    assert failures[0]["reason"] == "normal_prepend_reparents_source"
    assert failures[0]["operation_index"] == 1


def test_prepend_with_child_heading_first_followed_by_plain_prepend_is_refused() -> None:
    """Prepending a block with child heading followed by a plain prepend would adopt the existing body and is refused."""
    source = (
        "# Progress\n\n"
        "## Section\n\n"
        "Original body.\n"
    )
    candidate, failures, recoveries = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "prepend_to_section",
                "heading": "## Section",
                "content": "First note.\n\n### Child Heading\n\nChild body.",
                "reason": "Note 1 with child heading",
                "notes": [1],
            },
            {
                "type": "prepend_to_section",
                "heading": "## Section",
                "content": "Second note plain text.",
                "reason": "Note 2 plain prepend",
                "notes": [1],
            },
        ],
        0,
    )
    assert candidate is None
    assert len(failures) == 1
    assert failures[0]["reason"] == "normal_prepend_reparents_source"
    assert failures[0]["operation_index"] == 1


async def test_multibatch_corrective_completion_resolves_unclassified_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid plan that leaves a note without disposition gets exactly
    one corrective completion; the second response replaces the first in full and
    the run continues.  Nothing is ever retained."""
    storage = RecordingStorage()
    storage.objects[META_KEY] = json.dumps({"consolidation_count": 0})
    storage.objects[BANK_KEY] = "# Facts\n\n## Status\n\ninitial\n"

    note1 = f"{SPACE}/live/20000101T000001_alice_observation_1.md"
    note2 = f"{SPACE}/live/20000101T000002_alice_observation_2.md"
    note3 = f"{SPACE}/live/20000101T000003_alice_observation_3.md"
    note4 = f"{SPACE}/live/20000101T000004_alice_observation_4.md"
    for n in (note1, note2, note3, note4):
        storage.objects[n] = "note content"

    # Batch 1, first response: only note 2 has a disposition -> note 1 is
    # unclassified -> one corrective completion (call 2) returns b2_out, which
    # classifies both notes of the batch.
    b1_out = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Status",
                        "content": "- fact from note 2",
                        "reason": "Note 2 fact",
                        "notes": [2],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Batch 1 synthesis",
    }
    # Batch 2 response: 2 notes (note3, note4). Both notes applied ("notes": [1, 2] -> 1-based relative to batch 2).
    b2_out = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "edit",
                "operations": [
                    {
                        "type": "append_to_section",
                        "heading": "## Status",
                        "content": "- fact from note 3 and 4",
                        "reason": "Note 3 and 4 facts",
                        "notes": [1, 2],
                    }
                ],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Batch 2 synthesis",
    }

    call_count = 0

    async def mock_complete(messages, output_budget, *, retry_policy="bounded"):
        nonlocal call_count
        call_count += 1
        resp = b1_out if call_count == 1 else b2_out
        return ChatResult(
            text=json.dumps(resp),
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason="stop",
        )

    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    service = _service()
    service._batch_size = 2
    service._max_notes = 100
    service._validation_enabled = False
    service._bank_file_max_size = 35000
    service._complete_chat = mock_complete
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    # 3 completions: batch 1 (incomplete), its single corrective completion,
    # batch 2.
    assert call_count == 3
    assert result["status"] == "ok"
    assert result["batches_completed"] == 2
    assert result["batches_total"] == 2
    assert result["notes_processed"] == 4
    assert result["notes_applied"] == [1, 2, 3, 4]
    assert result["notes_discarded"] == []
    assert result["notes_retained"] == []
    assert result["notes_deleted"] == 4
    assert result["notes_remaining"] == 0
    # The corrective response replaced the first one in full.
    assert "- fact from note 2" not in storage.objects[BANK_KEY]
    assert storage.objects[BANK_KEY].count("- fact from note 3 and 4") == 2
    for note in (note1, note2, note3, note4):
        assert note not in storage.objects


@pytest.mark.asyncio
async def test_normal_notes_out_of_bounds_when_notes_count_zero() -> None:
    """When notes_count is 0, any referenced note (e.g. 1) is immediately rejected as out of bounds."""
    service = _service()
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "create",
                "content": "# Facts\n\nNew fact",
                "reason": "Test",
                "notes": [1],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Test synthesis",
    }
    prepared = await service._prepare_normal_batch(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=[],
        notes_count=0,
    )
    assert isinstance(prepared, consolidator_module._NormalBatchPreparationFailure)
    assert any(f.get("reason") == "invalid_normal_notes_out_of_bounds" for f in prepared.operation_failures)


@pytest.mark.asyncio
async def test_normal_multibyte_utf8_reduction_boundary() -> None:
    """UTF-8 byte length (not character count) determines the 30% reduction threshold."""
    service = _service()
    # Source contains 200 emoji (4 bytes each -> 800 bytes)
    emoji_block = "🦀" * 200
    source = f"# Facts\n\n## Section\n\n{emoji_block}\n"
    source_bytes = len(source.encode("utf-8"))
    assert source_bytes >= 800

    # Replacement has 50 ASCII characters (50 bytes), which is < 30% of 800 bytes
    llm_output = {
        "file_edits": [
            {
                "filename": "facts.md",
                "action": "rewrite",
                "content": "# Facts\n\n## Section\n\nshort replacement\n",
                "reason": "Shrink emojis",
                "notes": [1],
            }
        ],
        "discarded_notes": [],
        "synthesis": "Test",
    }
    prepared = await service._prepare_normal_batch(
        space_id=SPACE,
        llm_output=llm_output,
        bank_files=[{"key": f"{SPACE}/bank/facts.md", "content": source}],
        notes_count=1,
    )
    assert isinstance(prepared, consolidator_module._NormalBatchPreparationFailure)
    assert any(f.get("reason") == "normal_rewrite_reduction_refused" for f in prepared.operation_failures)
