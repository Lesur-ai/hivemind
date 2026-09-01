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
    service._legacy_french_prompts = False
    service._max_tokens = max_tokens
    service._context_window = context_window
    service._context_window_env_name = "INFERENCE_CHAT_CONTEXT_WINDOW"
    service._timeout = 1
    service._complete_chat = completion or Completion('{"file_edits": [], "synthesis": "ok"}')
    return service


def _create(filename: str = "new.md", *, content: str = "# New\n\nbody\n", reason: str = "Required by the notes.") -> dict:
    return {
        "filename": filename,
        "action": "create",
        "content": content,
        "reason": reason,
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
            }
        ],
    }


def _output(*file_edits: dict, synthesis: str = "The notes were integrated.") -> dict:
    return {"file_edits": list(file_edits), "synthesis": synthesis}


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
    """A valid create cannot sneak through ahead of an invalid edit sibling."""

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
                        "heading": "## Missing",
                        "content": "must not matter",
                        "reason": "invalid target",
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
            "reason": "ambiguous_or_missing_normal_target",
            "file_index": 1,
            "operation_index": 0,
            "filename": "facts.md",
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": hashlib.sha256(
                "## Missing".encode("utf-8")
            ).hexdigest(),
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "replace_section",
                "heading": requested_heading,
                "content": "new body",
                "reason": "A verified fact changed.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "replace_section",
                "heading": "## Release - 2026",
                "content": "second updated",
                "reason": "The exact section changed.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": requested,
                "reason": "The section is obsolete.",
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
        "## release — 2026",
        "## Release: 2026",
        "### Release — 2026",
        "## Release —",
    ],
)
def test_normal_target_fallback_does_not_become_fuzzy(requested: str) -> None:
    source = "# Facts\n\n## Release — 2026\n\nold body\n"

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": requested,
                "reason": "The section is obsolete.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "delete_section",
                "heading": "## Release\u200b — 2026",
                "reason": "The section is obsolete.",
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


async def test_missing_normal_after_is_attributable_without_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = "## Missing — Anchor"
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
                        "type": "add_section",
                        "heading": "## Added",
                        "after": requested,
                        "content": "new facts",
                        "reason": "The new section follows its anchor.",
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
            "reason": "ambiguous_or_missing_normal_after",
            "file_index": 0,
            "operation_index": 0,
            "filename": "facts.md",
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": hashlib.sha256(
                requested.encode("utf-8")
            ).hexdigest(),
        }
    ]
    assert storage.snapshot() == before


def test_normal_after_fallback_collision_reports_exact_cardinality() -> None:
    requested = "## Anchor ‐ 2026"
    source = (
        "# Facts\n\n"
        "## Anchor — 2026\n\nfirst\n\n"
        "## Anchor - 2026\n\nsecond\n"
    )

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Added",
                "after": requested,
                "content": "new facts",
                "reason": "The new section follows its anchor.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "append_to_section",
                "heading": "## Release — 2026",
                "content": "first addition",
                "reason": "One fact was added.",
            },
            {
                "type": "prepend_to_section",
                "heading": "## Release - 2026",
                "content": "second addition",
                "reason": "Another fact was added.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release - 2026",
                "content": "new body",
                "reason": "A new section was requested.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release ‐ 2026",
                "content": "third body",
                "reason": "A new section was requested.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Added",
                "after": "## Anchor A",
                "content": "first addition",
                "reason": "The first section was requested.",
            },
            {
                "type": "add_section",
                "heading": "## Added",
                "after": "## Anchor B",
                "content": "second addition",
                "reason": "The second section was requested.",
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

    candidate, failures = consolidator_module._normal_edit_candidate(
        source,
        [
            {
                "type": "add_section",
                "heading": "## Release — 2026",
                "after": "## Anchor A",
                "content": "first addition",
                "reason": "The first section was requested.",
            },
            {
                "type": "add_section",
                "heading": "## Release - 2026",
                "after": "## Anchor B",
                "content": "second addition",
                "reason": "The second section was requested.",
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
            '"content": "# New", "reason": "grounded"}], "synthesis": NaN}',
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
        (
            json.dumps(_output()),
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
        '{"file_edits": [], "file_edits": [], "synthesis": "ok"}'
        "\n```",
        "preface\n```json\n"
        '{"file_edits": [], "synthesis": NaN}'
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
        if event[0] == "delete_many"
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
                    },
                    {
                        "type": "append_to_section",
                        "heading": "### Target",
                        "content": "- appended to the original target",
                        "reason": "The original target gained a fact.",
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
                    },
                    {
                        "type": "add_section",
                        "heading": "## Replacement",
                        "after": "## Anchor",
                        "content": "replacement facts",
                        "reason": "The replacement follows the anchor.",
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


async def test_dedup_failure_after_an_edit_keeps_the_durable_snapshot_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed duplicate merge rolls back the in-memory edit before storage."""

    source = (
        "# Facts\n\n"
        "## Status\n\nolder version\n\n"
        "## Status\n\nnewer version\n\n"
        "## Other\n\nunchanged\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
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
                        "content": "- would be lost without the preflight",
                        "reason": "The unrelated section gained a fact.",
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
            "reason": "deduplication_merge_failed",
            "file_index": 0,
            "filename": "facts.md",
        }
    ]
    assert completion.calls == 1
    assert storage.snapshot() == before


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
    assert result["operations_applied"] == 0
    assert "operations_applied: 0\n" in storage.objects[SYNTHESIS_KEY]


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
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
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


async def test_surrogate_dedup_merge_refuses_the_whole_prepared_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# Facts\n\n## Status\n\nolder version\n\n"
        "## Status\n\nnewer version\n\n"
        "## Other\n\nunchanged\n"
    )
    storage = RecordingStorage()
    bank_files = _seed(storage, facts=source)
    before = storage.snapshot()
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
            "reason": "deduplication_merge_failed",
            "file_index": 1,
            "filename": "facts.md",
        }
    ]
    assert storage.snapshot() == before


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
    """A stop completion with no operation is not authorization to consume notes."""

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
    assert result["operation_failures"] == [{"reason": "empty_normal_file_edits"}]
    assert storage.snapshot() == before


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
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
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
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
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
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
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
    assert result["notes_processed"] == 1
    assert result["notes_deleted"] == 0
    assert result["notes_remaining"] == 2
    assert NOTE in storage.objects
    assert second_note in storage.objects
    assert storage.objects[shared_key] == "TRUNCATED BY FAILED SECOND BATCH"
    assert storage.objects[META_KEY] == initial_metadata
    assert all(event[0] != "put_json" for event in storage.events)
    assert all(event[0] != "delete_many" for event in storage.events)


async def test_nonterminal_completion_in_full_consolidation_keeps_storage_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real pipeline rejects a truncated completion before any write."""

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
    service._collect_inputs = AsyncMock(
        return_value={
            "notes": [{"key": NOTE, "content": "source note"}],
            "notes_keys": [NOTE],
            "notes_remaining": 0,
            "bank_files": bank_files,
            "rules": "",
            "synthesis": "",
        }
    )
    service._compact_bank_if_needed = AsyncMock(return_value={"compacted": False})
    service._resolve_direct_local_compaction_sink = AsyncMock(
        return_value=SimpleNamespace(storage=storage)
    )
    service._build_prompt = lambda **_kwargs: []

    result = await service.consolidate(SPACE, enforce_cooldown=False)

    assert completion.calls == 1
    assert result["status"] == "error"
    assert result["failure_reason"] == "batch_llm_failed"
    assert result["failed_batch"] == 1
    assert result["notes_processed"] == 0
    assert result["notes_deleted"] == 0
    assert storage.snapshot() == before
