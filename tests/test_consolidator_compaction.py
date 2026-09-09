"""Fail-closed regression coverage for strict bank compaction plans (#393)."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from unittest.mock import AsyncMock

import pytest

from hivemind_inference.errors import InferenceError
from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import ConsolidatorService
from live_mem.core.write_sink import DirectLocalWriteSink, StagedWriteNotImplemented
from tests.test_write_sink import WriteSinkFakeStorage


class CompactionStorage(WriteSinkFakeStorage):
    """Current storage shape used by the automatic and manual callers."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        self.events.append(f"put:{key}")
        await super().put(key, content, content_type)

    async def delete(self, key: str) -> None:
        self.events.append(f"delete:{key}")
        await super().delete(key)

    async def list_and_get(self, prefix: str) -> list[dict[str, str]]:
        return [
            {"key": key, "content": content}
            for key, content in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    async def exists(self, key: str) -> bool:
        # Isolated apply tests exercise a frozen bank batch rather than the
        # preceding space-read pipeline.  A real batch can only be formed for
        # an existing space; model that fact without forcing every focused
        # test to add unrelated metadata objects.
        return key == "space-a/_meta.json" or await super().exists(key)


class RecordingChat:
    """Provider-neutral double for the normalized ``_complete_chat`` seam."""

    def __init__(self, content: str = "", finish_reason: str = "stop") -> None:
        self.calls: list[dict] = []
        self.content = content
        self.finish_reason = finish_reason
        self.error: Exception | None = None

    async def __call__(self, messages, output_budget, *, retry_policy="bounded"):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "output_budget": output_budget,
                "retry_policy": retry_policy,
            }
        )
        if self.error is not None:
            raise self.error
        return ChatResult(
            text=self.content,
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason=self.finish_reason,
        )


class OpenAICompatiblePlanChat(RecordingChat):
    """Fake of an OpenAI-compatible adapter after normalization."""


class AnthropicNativePlanChat(RecordingChat):
    """Fake of an Anthropic-native adapter after normalization."""


def make_service(
    *,
    max_size: int = 10_000,
    max_tokens: int = 4096,
    context_window: int = 131_072,
) -> ConsolidatorService:
    """Build only the attributes exercised by isolated compaction tests."""

    service = object.__new__(ConsolidatorService)
    service._bank_file_max_size = max_size
    service._max_tokens = max_tokens
    service._context_window = context_window
    service._context_window_env_name = "INFERENCE_CHAT_CONTEXT_WINDOW"
    service._timeout = 1
    service._model = "test-model"
    # ``consolidate`` first checks that a chat role exists.  Isolated
    # compaction tests do not construct the production inference runtime, but
    # route-first pipeline tests still need to enter the route gate.
    service._chat_profile = object()
    service._complete_chat = RecordingChat()

    async def retain_test_route(
        space_id: str,
        direct_local_sink: DirectLocalWriteSink,
        operation: str,
    ) -> DirectLocalWriteSink:
        """Keep isolated plan/apply tests independent of the global registry."""

        del space_id, operation
        return direct_local_sink

    service._final_direct_local_compaction_sink = retain_test_route
    return service


def completions_for(service: ConsolidatorService) -> RecordingChat:
    return service._complete_chat


def _source(*, eol: str = "\n", final_newline: bool = True) -> str:
    content = (
        f"# Bank{eol}{eol}"
        f"## Keep{eol}"
        f"immutable evidence 😀{eol}{eol}"
        f"## Details{eol}"
        + "obsolete verbose detail " * 35
        + f"{eol}## Tail{eol}unchanged tail"
    )
    return content + eol if final_newline else content


def _plan(filename: str, operations: list[dict]) -> dict:
    return {
        "file_edits": [
            {
                "filename": filename,
                "action": "edit",
                "operations": operations,
            }
        ]
    }


def _replace_details(content: str = "condensed facts") -> dict:
    return {
        "type": "replace_section",
        "heading": "## Details",
        "content": content,
        "reason": "Remove repeated obsolete detail.",
    }


def _plan_json(filename: str, operations: list[dict]) -> str:
    return json.dumps(_plan(filename, operations), ensure_ascii=False)


def _without_preimage_id(result: dict) -> dict:
    """Assert the additive same-second-safe backup diagnostic, then remove it."""

    normalized = dict(result)
    preimage_id = normalized.pop("preimage_id")
    assert type(preimage_id) is str
    space_id, timestamp = preimage_id.split("/", 1)
    assert space_id == "space-a"
    date_part, operation_id = timestamp.rsplit("-", 1)
    assert len(date_part) == len("2026-01-01T00-00-00")
    assert len(operation_id) == 32
    assert set(operation_id) <= set("0123456789abcdef")
    return normalized


@pytest.mark.parametrize(
    "filename",
    [
        "activeContext.md",
        "ActiveContext.md",
        "progress.md",
        "techContext.md",
        "subdir/activeContext.md",
        "custom.md",
    ],
)
def test_compaction_limit_is_independent_of_the_bank_filename(filename: str) -> None:
    service = make_service(max_size=15_360)

    assert service._get_max_size_for_file(filename) == 15_360


# =============================================================================
# #394 — complete logical prepare phase before DirectLocal apply
# =============================================================================


def _prepared_plan_details() -> dict[str, object]:
    return {
        "status": "ok",
        "action": "edit",
        "operation_reasons": ("Remove redundant historical detail.",),
    }


async def test_final_route_fence_resolves_a_fresh_direct_local_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    original_storage = CompactionStorage()
    fresh_storage = CompactionStorage()
    reservation_check = AsyncMock()

    class FreshRegistry:
        async def resolve_sink(self, space_id: str) -> DirectLocalWriteSink:
            assert space_id == "space-a"
            return DirectLocalWriteSink(fresh_storage)

    from live_mem.core import engines

    monkeypatch.setattr(consolidator_module, "assert_space_not_reserved", reservation_check)
    monkeypatch.setattr(engines, "get_engine_registry", lambda: FreshRegistry())

    sink = await ConsolidatorService._final_direct_local_compaction_sink(
        service,
        "space-a",
        DirectLocalWriteSink(original_storage),
        "compact",
    )

    assert isinstance(sink, DirectLocalWriteSink)
    assert sink.storage is fresh_storage
    reservation_check.assert_awaited_once_with("space-a")


async def test_prepared_batch_is_frozen_hash_complete_and_apply_does_not_replan() -> None:
    storage = CompactionStorage()
    source = "é" * 80
    storage.objects = {"space-a/bank/facts.md": source}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("😀" * 20, _prepared_plan_details())
    )

    batch, failures = await service._prepare_compaction_batch(
        "space-a",
        [{"key": "space-a/bank/facts.md", "content": source}],
        "# Rules",
    )

    assert failures == ()
    assert batch is not None
    target = batch.targets[0]
    assert target.source_utf8_bytes == len(source.encode("utf-8"))
    assert target.result_utf8_bytes == len(("😀" * 20).encode("utf-8"))
    assert target.source_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert target.result_sha256 == hashlib.sha256(
        ("😀" * 20).encode("utf-8")
    ).hexdigest()
    assert target.expected_original_sha256 == target.source_sha256
    assert target.expected_result_sha256 == target.result_sha256
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.result = "mutated"  # type: ignore[misc]

    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert applied["status"] == "ok"
    assert service._plan_single_file_compaction.await_count == 1
    assert storage.objects["space-a/bank/facts.md"] == "😀" * 20
    archive_keys = [
        key for key in storage.objects if key.startswith("_backups/space-a/")
    ]
    assert len(archive_keys) == 1
    assert storage.objects[archive_keys[0]] == source


async def test_apply_persists_every_verified_preimage_before_the_first_bank_put() -> None:
    storage = CompactionStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert applied["status"] == "ok"
    archive_put_indexes = [
        index
        for index, event in enumerate(storage.events)
        if event.startswith("put:_backups/space-a/")
    ]
    bank_put_indexes = [
        index
        for index, event in enumerate(storage.events)
        if event.startswith("put:space-a/bank/")
    ]
    assert len(archive_put_indexes) == len(bank_put_indexes) == 2
    assert max(archive_put_indexes) < min(bank_put_indexes)
    archive_values = {
        value
        for key, value in storage.objects.items()
        if key.startswith("_backups/space-a/")
    }
    assert archive_values == {"a" * 120, "b" * 120}


async def test_apply_prewrite_drift_restores_earlier_owned_results_only() -> None:
    class DriftBeforeSecondApplyStorage(CompactionStorage):
        def __init__(self) -> None:
            super().__init__()
            self.b_reads = 0

        async def get(self, key: str):
            if key == "space-a/bank/b.md":
                self.b_reads += 1
                # A pre-backup drift check, full-space backup copy, and
                # post-copy source verification all happen before the apply
                # boundary. The backup readback uses its `_backups/` key, not
                # this live bank key. Drift only at the final prewrite
                # recheck, after ``a`` was successfully applied, to exercise
                # bounded rollback.
                if self.b_reads == 4:
                    self.objects[key] = "operator-owned"
            return await super().get(key)

    storage = DriftBeforeSecondApplyStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "error",
        "failure_reason": "compaction_apply_reverted",
        "failures": [
            {"filename": "b.md", "error": "compaction_prewrite_drift"}
        ],
    }
    assert storage.objects["space-a/bank/a.md"] == "a" * 120
    assert storage.objects["space-a/bank/b.md"] == "operator-owned"
    assert "put:space-a/bank/b.md" not in storage.events


async def test_apply_rechecks_input_hash_immediately_before_mutation() -> None:
    class DriftAtFinalPrewriteStorage(CompactionStorage):
        def __init__(self) -> None:
            super().__init__()
            self.facts_reads = 0

        async def get(self, key: str):
            if key == "space-a/bank/facts.md":
                self.facts_reads += 1
                if self.facts_reads == 4:
                    # Pre-backup verification, backup copy, and post-copy
                    # source verification have already succeeded. This is
                    # the final comparison immediately preceding the first
                    # bank put.
                    self.objects[key] = "operator-owned-after-final-check"
            return await super().get(key)

    storage = DriftAtFinalPrewriteStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "error",
        "failure_reason": "compaction_prewrite_drift",
        "failures": [
            {"filename": "facts.md", "error": "compaction_prewrite_drift"}
        ],
    }
    assert storage.objects["space-a/bank/facts.md"] == "operator-owned-after-final-check"
    assert "put:space-a/bank/facts.md" not in storage.events


async def test_apply_preserves_a_later_unknown_value_during_bounded_rollback() -> None:
    class PersistThenFailSecondPutStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            if key == "space-a/bank/b.md" and content == "b" * 60:
                self.events.append(f"put:{key}")
                self.objects[key] = "operator-owned"
                raise RuntimeError("write failed after a competing update")
            await super().put(key, content, content_type)

    storage = PersistThenFailSecondPutStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "partial",
        "failure_reason": "compaction_apply_recovery_unverified",
        "files_applied_before_failure": 1,
        "apply_may_have_mutated": True,
        "recovery_required": True,
        "failures": [
            {"filename": "b.md", "error": "compaction_apply_failed"},
            {
                "filename": "b.md",
                "error": "compaction_rollback_ownership_unverified",
            },
        ],
    }
    assert storage.objects["space-a/bank/a.md"] == "a" * 120
    assert storage.objects["space-a/bank/b.md"] == "operator-owned"


async def test_apply_restores_an_owned_first_result_after_second_write_failure() -> None:
    class FailSecondPutStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            if key == "space-a/bank/b.md" and content == "b" * 60:
                self.events.append(f"put:{key}")
                raise RuntimeError("injected second put failure")
            await super().put(key, content, content_type)

    storage = FailSecondPutStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "error",
        "failure_reason": "compaction_apply_reverted",
        "failures": [
            {"filename": "b.md", "error": "compaction_apply_failed"},
        ],
    }
    assert storage.objects["space-a/bank/a.md"] == "a" * 120
    assert storage.objects["space-a/bank/b.md"] == "b" * 120


async def test_apply_reports_partial_when_postwrite_readback_is_a_third_value() -> None:
    class CorruptPostwriteReadbackStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key == "space-a/bank/facts.md" and content == "c" * 60:
                self.objects[key] = "operator-owned"

    storage = CorruptPostwriteReadbackStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "partial",
        "failure_reason": "compaction_apply_recovery_unverified",
        "files_applied_before_failure": 0,
        "apply_may_have_mutated": True,
        "recovery_required": True,
        "failures": [
            {"filename": "facts.md", "error": "compaction_apply_readback_unverified"},
            {
                "filename": "facts.md",
                "error": "compaction_rollback_ownership_unverified",
            },
        ],
    }
    assert storage.objects["space-a/bank/facts.md"] == "operator-owned"


async def test_apply_restores_after_a_transient_postwrite_readback_failure() -> None:
    class TransientPostwriteReadStorage(CompactionStorage):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_result_read = False

        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key == "space-a/bank/facts.md" and content == "c" * 60:
                self.fail_next_result_read = True

        async def get(self, key: str):
            if key == "space-a/bank/facts.md" and self.fail_next_result_read:
                self.fail_next_result_read = False
                raise RuntimeError("transient result read failure")
            return await super().get(key)

    storage = TransientPostwriteReadStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "error",
        "failure_reason": "compaction_apply_reverted",
        "failures": [
            {"filename": "facts.md", "error": "compaction_apply_readback_failed"}
        ],
    }
    assert storage.objects["space-a/bank/facts.md"] == "f" * 120


async def test_apply_reports_partial_when_rollback_readback_is_unverified() -> None:
    class CorruptRollbackReadbackStorage(CompactionStorage):
        def __init__(self) -> None:
            super().__init__()
            self.a_result_written = False

        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            if key == "space-a/bank/a.md" and content == "a" * 120 and self.a_result_written:
                self.events.append(f"put:{key}")
                self.objects[key] = "rollback-mismatch"
                return
            if key == "space-a/bank/b.md" and content == "b" * 60:
                self.events.append(f"put:{key}")
                raise RuntimeError("injected second put failure")
            await super().put(key, content, content_type)
            if key == "space-a/bank/a.md" and content == "a" * 60:
                self.a_result_written = True

    storage = CorruptRollbackReadbackStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "partial",
        "failure_reason": "compaction_apply_recovery_unverified",
        "files_applied_before_failure": 1,
        "apply_may_have_mutated": True,
        "recovery_required": True,
        "failures": [
            {"filename": "b.md", "error": "compaction_apply_failed"},
            {
                "filename": "a.md",
                "error": "compaction_rollback_readback_unverified",
            },
        ],
    }
    assert storage.objects["space-a/bank/a.md"] == "rollback-mismatch"
    assert storage.objects["space-a/bank/b.md"] == "b" * 120


async def test_apply_fails_before_bank_mutation_when_a_preimage_cannot_be_verified() -> None:
    class FailPreimageStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            if key.startswith("_backups/space-a/"):
                self.events.append(f"put:{key}")
                raise RuntimeError("preimage storage unavailable")
            await super().put(key, content, content_type)

    storage = FailPreimageStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert applied == {
        "status": "error",
        "failure_reason": "compaction_preimage_backup_failed",
        "failures": [
            {"filename": "", "error": "compaction_preimage_backup_failed"}
        ],
    }
    assert storage.objects == {"space-a/bank/facts.md": "f" * 120}
    assert not any(event.startswith("put:space-a/bank/") for event in storage.events)


async def test_apply_rejects_a_corrupt_preimage_readback_before_bank_mutation() -> None:
    class CorruptBackupStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key.startswith("_backups/space-a/") and key.endswith("/bank/facts.md"):
                self.objects[key] = "corrupt backup bytes"

    storage = CorruptBackupStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert _without_preimage_id(applied) == {
        "status": "error",
        "failure_reason": "compaction_preimage_backup_unverified",
        "failures": [
            {"filename": "facts.md", "error": "compaction_preimage_backup_unverified"}
        ],
    }
    assert storage.objects["space-a/bank/facts.md"] == "f" * 120
    assert not any(event.startswith("put:space-a/bank/") for event in storage.events)


async def test_apply_input_drift_creates_no_preimage_or_bank_mutation() -> None:
    storage = CompactionStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    storage.objects["space-a/bank/facts.md"] = "operator-owned"
    applied = await service._apply_prepared_compaction_batch(
        "space-a", batch, DirectLocalWriteSink(storage)
    )

    assert applied == {
        "status": "error",
        "failure_reason": "compaction_preimage_source_drift",
        "failures": [
            {"filename": "facts.md", "error": "compaction_preimage_source_drift"}
        ],
    }
    assert storage.objects == {"space-a/bank/facts.md": "operator-owned"}
    assert storage.events == []


async def test_apply_cancellation_restores_an_ambiguously_persisted_result() -> None:
    class PersistThenCancelStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key == "space-a/bank/facts.md" and content == "c" * 60:
                raise asyncio.CancelledError()

    storage = PersistThenCancelStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await service._apply_prepared_compaction_batch(
            "space-a", batch, DirectLocalWriteSink(storage)
        )
    assert getattr(cancelled.value, "compaction_rollback_failures") == ()
    assert getattr(cancelled.value, "compaction_preimage_id").startswith("space-a/")
    assert storage.objects["space-a/bank/facts.md"] == "f" * 120


async def test_apply_cancellation_attaches_unverified_rollback_diagnostics() -> None:
    class PersistThirdValueThenCancelStorage(CompactionStorage):
        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            await super().put(key, content, content_type)
            if key == "space-a/bank/facts.md" and content == "c" * 60:
                self.objects[key] = "operator-owned"
                raise asyncio.CancelledError()

    storage = PersistThirdValueThenCancelStorage()
    storage.objects = {"space-a/bank/facts.md": "f" * 120}
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await service._apply_prepared_compaction_batch(
            "space-a", batch, DirectLocalWriteSink(storage)
        )

    assert getattr(cancelled.value, "compaction_rollback_failures") == (
        {
            "filename": "facts.md",
            "error": "compaction_rollback_ownership_unverified",
        },
    )
    assert getattr(cancelled.value, "compaction_preimage_id").startswith("space-a/")
    assert storage.objects["space-a/bank/facts.md"] == "operator-owned"


async def test_apply_failure_cancellation_marks_rollback_as_unverified() -> None:
    class FailThenCancelRollbackStorage(CompactionStorage):
        def __init__(self) -> None:
            super().__init__()
            self.a_result_written = False

        async def put(
            self, key: str, content: str, content_type: str = "text/plain"
        ) -> None:
            if key == "space-a/bank/b.md" and content == "b" * 60:
                self.events.append(f"put:{key}")
                raise RuntimeError("injected second write failure")
            if (
                key == "space-a/bank/a.md"
                and content == "a" * 120
                and self.a_result_written
            ):
                raise asyncio.CancelledError()
            await super().put(key, content, content_type)
            if key == "space-a/bank/a.md" and content == "a" * 60:
                self.a_result_written = True

    storage = FailThenCancelRollbackStorage()
    storage.objects = {
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    batch, failures = await service._prepare_compaction_batch(
        "space-a", await storage.list_and_get("space-a/bank/"), "# Rules"
    )

    assert failures == ()
    assert batch is not None
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await service._apply_prepared_compaction_batch(
            "space-a", batch, DirectLocalWriteSink(storage)
        )

    assert getattr(cancelled.value, "compaction_rollback_failures") == (
        {"filename": "", "error": "compaction_rollback_cancelled"},
    )
    assert getattr(cancelled.value, "compaction_preimage_id").startswith("space-a/")


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        (lambda target: dataclasses.replace(target, action="unknown"), "unknown_compaction_action"),
        (
            lambda target: dataclasses.replace(target, action="create"),
            "create_existing_compaction_target",
        ),
        (
            lambda target: dataclasses.replace(target, expected_original_exists=False),
            "missing_compaction_target",
        ),
        (
            lambda target: dataclasses.replace(
                target, action="rewrite", expected_original_exists=False
            ),
            "missing_compaction_target",
        ),
        (
            lambda target: dataclasses.replace(target, reasons=()),
            "missing_compaction_operation_reason",
        ),
        (
            lambda target: dataclasses.replace(target, expected_result_sha256="0" * 64),
            "invalid_compaction_postcondition",
        ),
        (
            lambda target: dataclasses.replace(target, expected_result_exists="yes"),
            "invalid_compaction_postcondition",
        ),
        (
            lambda target: dataclasses.replace(target, source_key=7, target_key=7),
            "invalid_compaction_postcondition",
        ),
    ],
)
async def test_apply_rejects_illegal_prepared_transition_before_any_mutation(
    change, expected_error
) -> None:
    storage = CompactionStorage()
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=("c" * 60, _prepared_plan_details())
    )
    batch, failures = await service._prepare_compaction_batch(
        "space-a",
        [{"key": "space-a/bank/facts.md", "content": "f" * 120}],
        "# Rules",
    )
    assert failures == ()
    assert batch is not None
    invalid = dataclasses.replace(batch, targets=(change(batch.targets[0]),))

    applied = await service._apply_prepared_compaction_batch(
        "space-a", invalid, DirectLocalWriteSink(storage)
    )

    assert applied["status"] == "error"
    assert applied["failures"] == [
        {"filename": "facts.md", "error": expected_error}
    ]
    assert storage.events == []
    assert storage.objects == {}


async def test_prepare_rejects_normalized_target_collisions_before_provider() -> None:
    storage = CompactionStorage()
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock()
    bank_files = [
        {"key": "space-a/bank/facts.md", "content": "f" * 120},
        {"key": "space-a/bank/facts\u200b.md", "content": "g" * 120},
    ]

    batch, failures = await service._prepare_compaction_batch(
        "space-a", bank_files, "# Rules"
    )

    assert batch is None
    assert {failure.error for failure in failures} == {"duplicate_compaction_target"}
    service._plan_single_file_compaction.assert_not_awaited()
    assert storage.events == []


async def test_manual_prepare_failure_is_global_not_partial_success() -> None:
    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    before = storage.snapshot()
    service = make_service(max_size=100)
    planned: list[str] = []

    async def planner(filename, content, max_size, rules):
        planned.append(filename)
        if filename == "a.md":
            return "a" * 60, _prepared_plan_details()
        return None, {"status": "error", "error": "invalid_compaction_json"}

    service._plan_single_file_compaction = planner
    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority(
            "space-a", DirectLocalWriteSink(storage)
        )
    ):
        result = await service.compact_bank("space-a", dry_run=False)

    assert result["status"] == "error"
    assert result["failure_reason"] == "compaction_prepare_failed"
    assert planned == ["a.md", "b.md"]
    assert result["files"][0]["error"] == "batch_preparation_failed"
    assert result["files"][1]["error"] == "invalid_compaction_json"
    assert storage.events == []
    assert storage.objects == before


async def test_manual_dry_run_reports_snapshot_collision_without_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan must surface the same collision that blocks a later apply."""

    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/facts.md": "# Facts\n\n## Detail\n" + "f" * 120,
        "space-a/bank/facts\u200b.md": "# Facts\n\n## Detail\n" + "g" * 120,
    }
    before = storage.snapshot()
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service.compact_bank("space-a", dry_run=True)

    assert result["status"] == "error"
    assert result["dry_run"] is True
    assert result["failure_reason"] == "compaction_prepare_failed"
    assert result["failures"] == [
        {"filename": "facts.md", "error": "duplicate_compaction_target"},
        {"filename": "facts.md", "error": "duplicate_compaction_target"},
    ]
    assert "bank_repair" in result["remediation"]
    service._plan_single_file_compaction.assert_not_awaited()
    assert storage.events == []
    assert storage.objects == before


async def test_manual_dry_run_reports_all_provider_free_preflight_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan catches every deterministic planner refusal without egress."""

    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/a.md": "## No level-one heading\n\n" + "a" * 120,
        "space-a/bank/b.md": "# Broken\n\n```\n" + "b" * 120,
        "space-a/bank/c.md": "# Facts\n\n## Detail\n" + "c" * 120,
    }
    before = storage.snapshot()
    service = make_service(max_size=100, context_window=1)
    service._plan_single_file_compaction = AsyncMock(
        side_effect=AssertionError("dry-run must not invoke the planner")
    )
    service._complete_chat = AsyncMock(
        side_effect=AssertionError("dry-run must not contact the provider")
    )
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service.compact_bank("space-a", dry_run=True)

    assert result["status"] == "error"
    assert result["dry_run"] is True
    assert result["failure_reason"] == "compaction_prepare_failed"
    assert result["failures"] == [
        {"filename": "a.md", "error": "invalid_compaction_source_structure"},
        {"filename": "b.md", "error": "invalid_compaction_source_structure"},
        {"filename": "c.md", "error": "compaction_context_exhausted"},
    ]
    assert [report["error"] for report in result["files"]] == [
        "invalid_compaction_source_structure",
        "invalid_compaction_source_structure",
        "compaction_context_exhausted",
    ]
    service._plan_single_file_compaction.assert_not_awaited()
    service._complete_chat.assert_not_awaited()
    assert storage.events == []
    assert storage.objects == before


async def test_manual_dry_run_keeps_provider_free_preflights_after_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One collision must not hide another target's no-egress repair need."""

    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/a.md": "# Facts\n\n## Detail\n" + "a" * 120,
        "space-a/bank/a\u200b.md": "# Facts\n\n## Detail\n" + "b" * 120,
        "space-a/bank/b.md": "## No level-one heading\n\n" + "c" * 120,
    }
    before = storage.snapshot()
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        side_effect=AssertionError("dry-run must not invoke the planner")
    )
    service._complete_chat = AsyncMock(
        side_effect=AssertionError("dry-run must not contact the provider")
    )
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service.compact_bank("space-a", dry_run=True)

    assert result["status"] == "error"
    assert result["failure_reason"] == "compaction_prepare_failed"
    assert result["failures"] == [
        {"filename": "a.md", "error": "duplicate_compaction_target"},
        {"filename": "a.md", "error": "duplicate_compaction_target"},
        {"filename": "b.md", "error": "invalid_compaction_source_structure"},
    ]
    assert [report["error"] for report in result["files"]] == [
        "duplicate_compaction_target",
        "duplicate_compaction_target",
        "invalid_compaction_source_structure",
    ]
    service._plan_single_file_compaction.assert_not_awaited()
    service._complete_chat.assert_not_awaited()
    assert storage.events == []
    assert storage.objects == before


async def test_manual_apply_resolves_route_before_storage_when_no_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(max_size=100)

    def forbidden_storage():
        raise AssertionError("unrouted compaction must not resolve storage")

    class RefusingRegistry:
        async def resolve_sink(self, space_id: str):
            assert space_id == "space-a"
            raise RuntimeError("route refused")

    from live_mem.core import engines

    monkeypatch.setattr(consolidator_module, "get_storage", forbidden_storage)
    monkeypatch.setattr(engines, "get_engine_registry", lambda: RefusingRegistry())
    with pytest.raises(RuntimeError, match="route refused"):
        await service.compact_bank("space-a", dry_run=False)


async def test_consolidate_rechecks_route_before_collecting_inputs_or_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue worker's time-of-use route gate precedes every legacy seam."""

    service = make_service(max_size=100)
    service._collect_inputs = AsyncMock()
    service._call_llm = AsyncMock()

    class RefusingRegistry:
        async def resolve_sink(self, space_id: str):
            assert space_id == "space-a"
            raise RuntimeError("route became non-direct")

    from live_mem.core import engines

    monkeypatch.setattr(engines, "get_engine_registry", lambda: RefusingRegistry())
    with pytest.raises(RuntimeError, match="route became non-direct"):
        await service.consolidate("space-a", enforce_cooldown=False)

    service._collect_inputs.assert_not_awaited()
    service._call_llm.assert_not_awaited()


async def test_consolidate_staged_route_reports_the_requested_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service-level refusal must not mislabel consolidation as compact."""

    service = make_service(max_size=100)
    service._collect_inputs = AsyncMock()
    service._call_llm = AsyncMock()

    class StagedRegistry:
        async def resolve_sink(self, space_id: str) -> object:
            assert space_id == "space-a"
            return object()

    from live_mem.core import engines

    monkeypatch.setattr(engines, "get_engine_registry", lambda: StagedRegistry())
    with pytest.raises(StagedWriteNotImplemented, match=r"op=consolidate"):
        await service.consolidate("space-a", enforce_cooldown=False)

    service._collect_inputs.assert_not_awaited()
    service._call_llm.assert_not_awaited()


async def test_manual_apply_failure_restores_verified_preimages() -> None:
    class FailSecondPutStorage(CompactionStorage):
        async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
            if key.endswith("/b.md") and content == "b" * 60:
                self.events.append(f"put:{key}")
                raise RuntimeError("injected second put failure")
            await super().put(key, content, content_type)

    storage = FailSecondPutStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/a.md": "a" * 120,
        "space-a/bank/b.md": "b" * 120,
    }
    storage.events.clear()
    service = make_service(max_size=100)

    async def planner(filename, content, max_size, rules):
        return filename[0] * 60, _prepared_plan_details()

    service._plan_single_file_compaction = planner
    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority(
            "space-a", DirectLocalWriteSink(storage)
        )
    ):
        result = await service.compact_bank("space-a", dry_run=False)

    assert result["status"] == "error"
    assert result["failure_reason"] == "compaction_apply_reverted"
    assert result["total_size_after"] is None
    assert result["failed_phase"] == "apply"
    assert result["rollback_outcome"] == "verified"
    assert result["failures"] == [
        {"filename": "b.md", "error": "compaction_apply_failed"}
    ]
    assert storage.objects["space-a/bank/a.md"] == "a" * 120
    assert storage.objects["space-a/bank/b.md"] == "b" * 120


@pytest.mark.parametrize(
    "chat_type",
    [OpenAICompatiblePlanChat, AnthropicNativePlanChat],
)
async def test_strict_planner_uses_normalized_provider_once_and_full_rules(
    chat_type: type[RecordingChat],
) -> None:
    source = _source()
    rules = "# Rules\n" + "R" * 4_096 + "\nEND-OF-RULES"
    service = make_service()
    chat = chat_type("condensed facts")
    service._complete_chat = chat

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, rules
    )

    assert candidate is not None
    assert details["status"] == "ok"
    assert details["source_bytes"] == len(source.encode("utf-8"))
    assert details["candidate_bytes"] == len(candidate.encode("utf-8"))
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["retry_policy"] == "none"
    assert call["output_budget"] == 4096
    prompt = "\n".join(message["content"] for message in call["messages"])
    assert "END-OF-RULES" in prompt
    data = json.loads(call["messages"][1]["content"])
    assert data["reference_rules"] == rules
    sections = consolidator_module._strict_compaction_sections(source)
    expected_bodies = []
    for i, heading in enumerate(sections):
        end = sections[i + 1].start if i + 1 < len(sections) else len(source)
        body = source[heading.heading_end:end]
        if body.strip():
            expected_bodies.append(body)
    assert "".join(item["body"] for item in data["undated_context"]) == "".join(expected_bodies)
    assert "untrusted data" in prompt
    assert "temperature" not in {
        field.name for field in dataclasses.fields(consolidator_module.ChatRequest)
    }


async def test_strict_planner_preserves_reasoning_profile_generation_budget() -> None:
    source = _source()
    service = make_service(max_tokens=200_000, context_window=1_000_000)
    chat = RecordingChat("condensed facts")
    service._complete_chat = chat

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 15_360, "# Rules"
    )

    assert candidate is not None
    assert details["status"] == "ok"
    assert len(chat.calls) == 1
    assert chat.calls[0]["output_budget"] == 200_000
    assert chat.calls[0]["retry_policy"] == "none"


@pytest.mark.parametrize(
    "provider_shape",
    ["openai-compatible", "anthropic-native"],
)
async def test_strict_planner_reaches_both_provider_shapes_only_through_chatrequest(
    provider_shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    service = make_service()
    del service._complete_chat
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            return ChatResult(
                text="condensed facts",
                configured_model=f"{provider_shape}-test-model",
                model_evidence="configured_only",
                finish_reason="stop",
            )

    class Runtime:
        def chat_provider(self):
            return Provider()

    from live_mem.core import inference_runtime

    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: Runtime())
    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, "# Rules"
    )

    assert candidate is not None
    assert details["status"] == "ok"
    assert len(requests) == 1
    assert requests[0].retry_policy == "none"
    assert requests[0].max_output_tokens == 4096


@pytest.mark.parametrize(
    "provider_shape",
    ["openai-compatible", "anthropic-native"],
)
async def test_strict_planner_preserves_reasoning_budget_through_chatrequest(
    provider_shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    service = make_service(max_tokens=200_000, context_window=1_000_000)
    del service._complete_chat
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            return ChatResult(
                text="condensed facts",
                configured_model=f"{provider_shape}-test-model",
                model_evidence="configured_only",
                finish_reason="stop",
            )

    class Runtime:
        def chat_provider(self):
            return Provider()

    from live_mem.core import inference_runtime

    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: Runtime())
    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 15_360, "# Rules"
    )

    assert candidate is not None
    assert details["status"] == "ok"
    assert len(requests) == 1
    assert requests[0].retry_policy == "none"
    assert requests[0].max_output_tokens == 200_000


@pytest.mark.parametrize(
    ("finish_reason", "expected_error"),
    [
        ("length", "compaction_completion_length"),
        ("content_rejected", "compaction_completion_content_rejected"),
        ("other", "compaction_completion_other"),
    ],
)
async def test_nonterminal_completion_is_rejected_before_plan_parse(
    finish_reason: str, expected_error: str
) -> None:
    service = make_service()
    chat = RecordingChat("condensed facts", finish_reason)
    service._complete_chat = chat

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", _source(), 10_000, "# Rules"
    )

    assert candidate is None
    assert details == {"status": "error", "error": expected_error}
    assert len(chat.calls) == (1 if finish_reason == "content_rejected" else 2)


async def test_nonterminal_completion_cannot_reach_json_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    completions_for(service).finish_reason = "length"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("non-terminal completion reached JSON parsing")

    monkeypatch.setattr(consolidator_module.json, "loads", forbidden)
    candidate, details = await service._plan_single_file_compaction(
        "facts.md", _source(), 10_000, "# Rules"
    )

    assert candidate is None
    assert details["error"] == "compaction_completion_length"


async def test_blank_stop_completion_and_provider_failure_are_safe_no_plans() -> None:
    source = _source()
    service = make_service()
    completions_for(service).content = " \n\t "

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, "# Rules"
    )
    assert candidate is None
    assert details["error"] == "blank_compaction_completion"

    completions_for(service).error = RuntimeError("provider completion must not leak")
    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, "# Rules"
    )
    assert candidate is None
    assert details == {"status": "error", "error": "compaction_provider_failure"}


@pytest.mark.parametrize(
    ("provider_id", "adapter_id"),
    [("openai", "openai-compatible"), ("anthropic", "anthropic")],
)
async def test_normalized_adapter_refusal_is_a_safe_no_plan(
    provider_id: str, adapter_id: str
) -> None:
    service = make_service()
    completions_for(service).error = InferenceError(
        category="content_rejected",
        role="chat",
        provider_id=provider_id,
        adapter_id=adapter_id,
        retryable=False,
        correlation_id="compaction-refusal",
    )

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", _source(), 10_000, "# Rules"
    )

    assert candidate is None
    assert details == {"status": "error", "error": "compaction_provider_failure"}


async def test_valid_plan_does_not_enter_generic_json_or_storage_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    service = make_service()
    completions_for(service).content = "condensed facts"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("compaction planner used a forbidden generic path")

    monkeypatch.setattr(consolidator_module, "_extract_json", forbidden)
    monkeypatch.setattr(consolidator_module, "_repair_json", forbidden)
    monkeypatch.setattr(consolidator_module, "get_storage", forbidden)
    service._call_llm = forbidden

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, "# Rules"
    )

    assert candidate is not None
    assert details["status"] == "ok"


async def test_compaction_rejects_a_lone_surrogate_before_hashing():
    service = make_service()
    completions_for(service).content = "bad \ud800"
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 100, "")
    assert candidate is None
    assert details == {"status": "error", "error": "invalid_compaction_utf8"}
    assert len(completions_for(service).calls) == 2


def test_compaction_failure_serializer_drops_malformed_or_unknown_target_fields() -> None:
    marker = "SERIALIZER_RAW_COMPLETION_SECRET_4f27"
    expected_hash = hashlib.sha256(b"## requested target").hexdigest()

    safe = consolidator_module._sanitize_compaction_failure_payloads(
        [
            {
                "filename": "facts.md",
                "error": "ambiguous_or_missing_compaction_target",
                "operation_index": 0,
                "target_resolution": "missing",
                "target_match_count": 0,
                "target_heading_sha256": expected_hash,
                "heading": marker,
                "reason": marker,
                "prompt": marker,
                "completion": marker,
            },
            {
                "filename": "other.md",
                "error": "ambiguous_or_missing_compaction_target",
                "operation_index": 1,
                "target_resolution": "missing",
                "target_match_count": 1,
                "target_heading_sha256": "not-a-hash",
                "heading": marker,
            },
        ]
    )

    assert safe == [
        {
            "filename": "facts.md",
            "error": "ambiguous_or_missing_compaction_target",
            "operation_index": 0,
            "target_resolution": "missing",
            "target_match_count": 0,
            "target_heading_sha256": expected_hash,
        },
        {
            "filename": "other.md",
            "error": "ambiguous_or_missing_compaction_target",
        },
    ]
    assert marker not in json.dumps(safe)


def test_strict_headings_ignore_unicode_and_nonclosing_fence_pseudo_lines() -> None:
    source = (
        "# Bank\n\n"
        "## Details\n"
        "text\u2028## Not A Physical Heading\n"
        "```python\n"
        "```not-a-closing-fence\n"
        "## Also Code\n"
        "```\n"
        "## Tail\n"
        "tail\n"
    )

    assert [
        section.heading
        for section in consolidator_module._strict_compaction_sections(source)
    ] == ["# Bank", "## Details", "## Tail"]


def test_legacy_tab_fence_keeps_hidden_heading_out_of_compaction_roots():
    source = "# Bank\n\n\t```\n# Hidden Heading\nprotected evidence\n\t```\n## Tail\ntail"
    assert consolidator_module._strict_compaction_fences_balanced(source)
    assert [h.heading for h in consolidator_module._strict_compaction_sections(source)] == ["# Bank", "## Tail"]
    recent, history = consolidator_module._compaction_partition(source, 100)
    assert not history
    assert any("# Hidden Heading" in unit["body"] for unit in recent)


async def test_unbalanced_source_fence_refuses_before_provider_egress():
    service = make_service()
    source = "# Bank\n\n## Details\n```python\n``` not-a-close\n## Swallowed\n"
    candidate, details = await service._plan_single_file_compaction("facts.md", source, 100, "")
    assert candidate is None
    assert details == {"status": "error", "error": "invalid_compaction_source_structure"}
    assert completions_for(service).calls == []


async def test_balanced_source_fence_with_a_pseudo_close_remains_valid():
    source = "# Bank\n\n## Details\n```python\n``` not-a-close\nreal code\n```\n" + "x" * 240
    service = make_service()
    completions_for(service).content = "Keep the relevant lesson."
    candidate, details = await service._plan_single_file_compaction("facts.md", source, 100, "")
    assert candidate is not None and details["status"] == "ok"


@pytest.mark.parametrize(
    ("source", "max_size", "replacement", "reduction_label"),
    [
        # 1 % de réduction : jetée avant par le plancher de 5 %.
        ("# Bank\n\n## Details\n" + "x" * 1_000, 10_000, "y" * 990, "1 %"),
        # Réduction réelle mais résultat encore TRÈS au-dessus de la limite :
        # jetée avant par la cible à 75 % puis par la limite dure.  C'est le cas
        # de `progress.md`, où aucun résultat réaliste ne passait, donc où le
        # fichier ne pouvait jamais être amélioré.
        ("# Bank\n\n## Details\n" + "x" * 20_000, 1_000, "y" * 8_000, "60 %"),
        ("# Bank\n\n## Details\n" + "x" * 2_000, 1_000, "y" * 1_800, "10 %"),
    ],
)
async def test_a_real_reduction_is_accepted_even_when_the_ideal_is_out_of_reach(
    source: str, max_size: int, replacement: str, reduction_label: str
) -> None:
    """#457 item 2 — la limite de taille est un idéal, plus un veto.

    Chacun de ces candidats réduit réellement le fichier et était pourtant
    refusé : par le plancher de 5 %, par la cible à 75 %, ou par la limite dure.
    Les refuser laissait le fichier hors limite pour toujours, puisque la
    perfection était inatteignable en une passe.
    """
    service = make_service()
    completions_for(service).content = replacement

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, max_size, "# Rules"
    )

    assert details.get("error") is None, reduction_label
    assert candidate is not None
    # Strictement plus petit, et le H1 est préservé.
    assert len(candidate.encode("utf-8")) < len(source.encode("utf-8"))
    assert candidate.startswith("# Bank")


async def test_context_exhaustion_refuses_before_provider_egress() -> None:
    service = make_service(max_tokens=4096, context_window=512)
    source = _source()

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 10_000, "# Rules\n" + "x" * 4_096
    )

    assert candidate is None
    assert details == {"status": "error", "error": "compaction_context_exhausted"}
    assert completions_for(service).calls == []


async def test_reasoning_profile_refuses_before_egress_below_visible_plan_floor() -> None:
    source = _source()
    rules = "# Rules"
    service = make_service(max_tokens=200_000, context_window=1_000_000)
    messages = service._build_compaction_plan_messages("facts.md", source, 15_360, rules)
    input_tokens = sum(
        consolidator_module._strict_compaction_input_tokens(message["content"])
        for message in messages
    ) + 16 * len(messages)
    visible_plan_reservation = max(4096, (15_360 * 75 // 100) // 3 + 1024)
    service._context_window = input_tokens + visible_plan_reservation - 1

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 15_360, rules
    )

    assert candidate is None
    assert details == {"status": "error", "error": "compaction_context_exhausted"}
    assert completions_for(service).calls == []


async def test_calibrated_context_admission_keeps_a_standard_profile_usable() -> None:
    source = "# Bank\n\n## Details\n" + "x" * 175_000
    rules = "# Rules\n" + "r" * 6_800
    service = make_service(max_size=300_000, context_window=131_072)
    completions_for(service).content = "y" * 9_000

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", source, 300_000, rules
    )

    assert consolidator_module._strict_compaction_input_tokens("a") == 1
    assert consolidator_module._strict_compaction_input_tokens("é") == 1
    assert consolidator_module._strict_compaction_input_tokens("€") == 1
    assert consolidator_module._strict_compaction_input_tokens("😀") == 2
    assert candidate is not None
    assert details["status"] == "ok"
    assert len(completions_for(service).calls) == 1


def test_context_admission_boundary_is_exact_and_deterministic() -> None:
    messages = [
        {"role": "system", "content": "é"},
        {"role": "user", "content": "abc"},
    ]
    # ceil(2 / 3) + ceil(3 / 3) + (2 * 16) + 4096 output tokens.
    required_context = 1 + 1 + 32 + 4_096

    fitting_service = make_service(context_window=required_context)
    exhausted_service = make_service(context_window=required_context - 1)

    assert fitting_service._compaction_output_budget(messages, 100) == 4_096
    assert exhausted_service._compaction_output_budget(messages, 100) is None


def test_reasoning_budget_uses_profile_then_remaining_context_not_file_target() -> None:
    messages = [
        {"role": "system", "content": "é"},
        {"role": "user", "content": "abc"},
    ]
    input_tokens = 1 + 1 + 32
    profile_budget = 200_000
    visible_plan_reservation = max(4096, (15_360 * 75 // 100) // 3 + 1024)

    full_profile = make_service(
        max_tokens=profile_budget,
        context_window=input_tokens + profile_budget,
    )
    one_token_short = make_service(
        max_tokens=profile_budget,
        context_window=input_tokens + profile_budget - 1,
    )
    visible_floor = make_service(
        max_tokens=profile_budget,
        context_window=input_tokens + visible_plan_reservation,
    )
    exhausted = make_service(
        max_tokens=profile_budget,
        context_window=input_tokens + visible_plan_reservation - 1,
    )

    assert full_profile._compaction_output_budget(messages, 15_360) == profile_budget
    assert full_profile._compaction_output_budget(messages, 60_000) == profile_budget
    assert one_token_short._compaction_output_budget(messages, 15_360) == profile_budget - 1
    assert visible_floor._compaction_output_budget(messages, 15_360) == visible_plan_reservation
    assert exhausted._compaction_output_budget(messages, 15_360) is None


async def test_invalid_source_structure_refuses_before_provider_egress() -> None:
    service = make_service()

    candidate, details = await service._plan_single_file_compaction(
        "facts.md", "plain text only", 10_000, "# Rules"
    )

    assert candidate is None
    assert details == {"status": "error", "error": "invalid_compaction_source_structure"}
    assert completions_for(service).calls == []


async def test_manual_compaction_dry_run_reports_utf8_limits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    await storage.put_json("space-a/_meta.json", {"created_at": "2026-01-01"})
    await storage.put("space-a/_rules.md", "# Rules")
    oversized = "# Facts\n\n## Detail\n" + "é" * 120
    await storage.put("space-a/bank/activeContext.md", oversized)
    await storage.put("space-a/bank/facts.md", "f" * 40)
    before = dict(storage.objects)
    service = make_service(max_size=100)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service.compact_bank("space-a", dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["files_total"] == 2
    assert result["files_over_limit"] == 1
    assert result["total_size_before"] == result["total_size_after"] == 299
    assert result["files"][0] == {
        "filename": "activeContext.md",
        "size": 259,
        "max_size": 100,
        "source_sha256": hashlib.sha256(oversized.encode("utf-8")).hexdigest(),
        "over_limit": True,
        "ratio": 2.59,
    }
    assert completions_for(service).calls == []
    assert storage.objects == before


async def test_manual_compaction_writes_a_validated_smaller_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    await storage.put_json("space-a/_meta.json", {"created_at": "2026-01-01"})
    await storage.put("space-a/_rules.md", "# Rules")
    await storage.put("space-a/bank/facts.md", "f" * 120)
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=(
            "c" * 60,
            {
                "status": "ok",
                "action": "edit",
                "operation_reasons": ("Remove repetition.",),
            },
        )
    )
    reservation_check = AsyncMock()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        consolidator_module, "assert_space_not_reserved", reservation_check
    )

    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority(
            "space-a", DirectLocalWriteSink(storage)
        )
    ):
        result = await service.compact_bank("space-a", dry_run=False)

    assert result["total_size_before"] == 120
    assert result["total_size_after"] == 60
    assert result["files"][0]["compacted_size"] == 60
    assert result["files"][0]["reduction_pct"] == 50
    assert result["files"][0]["source_sha256"] == hashlib.sha256(
        ("f" * 120).encode("utf-8")
    ).hexdigest()
    assert result["files"][0]["result_sha256"] == hashlib.sha256(
        ("c" * 60).encode("utf-8")
    ).hexdigest()
    reservation_check.assert_awaited_once_with("space-a")
    assert storage.objects["space-a/bank/facts.md"] == "c" * 60


async def test_manual_compaction_preserves_content_and_safe_plan_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    await storage.put_json("space-a/_meta.json", {"created_at": "2026-01-01"})
    await storage.put("space-a/_rules.md", "# Rules")
    await storage.put("space-a/bank/facts.md", "f" * 120)
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(
        return_value=(None, {"status": "error", "error": "invalid_compaction_json"})
    )
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        consolidator_module, "assert_space_not_reserved", AsyncMock()
    )

    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority(
            "space-a", DirectLocalWriteSink(storage)
        )
    ):
        result = await service.compact_bank("space-a", dry_run=False)

    assert result["status"] == "error"
    assert result["failed_phase"] == "prepare"
    assert result["rollback_outcome"] == "not_needed"
    assert result["files"][0]["error"] == "invalid_compaction_json"
    assert storage.objects["space-a/bank/facts.md"] == "f" * 120


async def test_manual_compaction_rejects_legacy_plan_without_leaking_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected model output never enters public diagnostics or storage."""

    marker_heading = "## MANUAL_COMPLETION_HEADING_SECRET_4f27"
    source = "# Bank\n\n## Details\n" + "obsolete detail " * 200
    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/_rules.md": "# Rules",
        "space-a/bank/facts.md": source,
    }
    before = storage.snapshot()
    service = make_service(max_size=1_000)
    completions_for(service).content = _plan_json(
        "facts.md",
        [
            _replace_details("condensed evidence " * 30),
            {
                "type": "delete_section",
                "heading": marker_heading,
                "reason": "MANUAL_COMPLETION_REASON_SECRET_9a31",
            },
        ],
    )
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority(
            "space-a", DirectLocalWriteSink(storage)
        )
    ):
        result = await service.compact_bank("space-a", dry_run=False)

    assert result["status"] == "error"
    assert result["failed_phase"] == "prepare"
    assert result["rollback_outcome"] == "not_needed"
    assert result["failures"] == [{"filename": "facts.md", "error": "invalid_compaction_summary_format"}]
    assert marker_heading not in json.dumps(result)
    assert storage.objects == before


async def test_manual_compaction_reports_a_missing_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    service = make_service()

    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    result = await service.compact_bank("missing")

    assert result == {"status": "error", "message": "Space 'missing' not found"}


async def test_complete_chat_forwards_explicit_retry_policy_to_chat_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(max_tokens=99)
    captured: dict[str, object] = {}

    class Provider:
        async def complete(self, request):
            captured["request"] = request
            return ChatResult(
                text="{}",
                configured_model="test-model",
                model_evidence="configured_only",
                finish_reason="stop",
            )

    class Runtime:
        def chat_provider(self):
            return Provider()

    from live_mem.core import inference_runtime

    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: Runtime())
    await ConsolidatorService._complete_chat(
        service,
        [{"role": "user", "content": "test"}],
        200,
        retry_policy="none",
    )

    request = captured["request"]
    assert request.max_output_tokens == 99
    assert request.retry_policy == "none"


# ---------------------------------------------------------------------------
# Les DEUX frontieres de validation doivent avoir une garde durable,
# pas seulement le planificateur.
# ---------------------------------------------------------------------------


def test_a_result_above_the_limit_survives_preparation_and_apply_revalidation():
    """`max_size < result < source` doit passer les deux frontieres.

    ``compaction_result_exceeds_max_size`` ne doit être imposé ni à la
    préparation de cible ni à la revalidation d'apply. Un test qui s'arrête au
    planificateur ne garde ni l'un ni l'autre. Les deux doivent rester
    cohérents, sinon l'apply refuserait ce que
    la préparation vient d'accepter.
    """
    source = "# Bank\n\n## Details\n" + "x" * 300
    result = "# Bank\n\n## Details\n" + "y" * 150
    max_size = 100
    assert max_size < len(result.encode("utf-8")) < len(source.encode("utf-8"))

    target, error = consolidator_module._materialize_prepared_compaction_target(
        space_id="s",
        source_key="s/bank/facts.md",
        filename="facts.md",
        source=source,
        max_size=max_size,
        action="edit",
        result=result,
        reasons=("Condense the details.",),
    )

    assert error is None, error
    assert target is not None

    batch = consolidator_module._PreparedCompactionBatch(
        space_id="s",
        targets=(target,),
        total_source_utf8_bytes=len(source.encode("utf-8")),
        total_result_utf8_bytes=len(result.encode("utf-8")),
    )
    # La revalidation d'apply doit accepter exactement ce que la preparation a gele.
    assert consolidator_module._prepared_compaction_batch_error(batch, "s") == ()


@pytest.mark.parametrize(
    ("source_body", "result_body", "expected_error"),
    [
        # Egal en taille, puis plus gros : la reduction doit rester STRICTE.
        ("x" * 300, "x" * 300, "compaction_not_smaller"),
        ("x" * 300, "y" * 400, "compaction_not_smaller"),
        # An actually empty result is still rejected before durable writes.
        ("x" * 2_000, "", "empty_compaction_candidate"),
    ],
)
def test_preparation_still_refuses_what_the_envelope_forbids(
    source_body: str, result_body: str, expected_error: str
):
    """Empty or non-reducing output cannot cross the preparation boundary."""
    source = "# Bank\n\n## Details\n" + source_body
    result = "# Bank\n\n## Details\n" + result_body
    if expected_error == "empty_compaction_candidate":
        result = ""
    target, error = consolidator_module._materialize_prepared_compaction_target(
        space_id="s",
        source_key="s/bank/facts.md",
        filename="facts.md",
        source=source,
        max_size=100,
        action="edit",
        result=result,
        reasons=("Condense the details.",),
    )
    assert target is None
    assert error == expected_error


def _recovery_response(text: str, finish: str = "stop") -> ChatResult:
    return ChatResult(
        text=text, configured_model="test-model",
        model_evidence="configured_only", finish_reason=finish,
    )


def _recovery_provider_error(category="invalid_response", role="chat"):
    return InferenceError(
        category=category, role=role, provider_id="openai-compatible",
        adapter_id="openai-compatible", retryable=False,
        correlation_id="compaction-recovery-test",
    )


@pytest.mark.parametrize("category,role", [
    ("auth", "chat"), ("rate_limited", "chat"),
    ("timeout", "chat"), ("content_rejected", "chat"),
    ("unavailable", "chat"), ("quota_exhausted", "chat"), ("unsupported", "chat"), ("invalid_request", "chat"),
    ("invalid_response", "embedding"),
])
async def test_compaction_recovery_keeps_other_provider_errors_terminal(category, role):
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=_recovery_provider_error(category, role))
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert candidate is None
    assert details == {"status": "error", "error": "compaction_provider_failure"}
    assert service._complete_chat.await_count == 1


@pytest.mark.parametrize("recover,invalid_response", [(False, False), (False, True), (True, False)])
async def test_manual_compaction_recovery_preserves_transaction_boundary(recover, invalid_response, monkeypatch):
    storage = CompactionStorage()
    storage.objects = {
        "space-a/_meta.json": "{}", "space-a/_rules.md": "# Rules",
        "space-a/bank/facts.md": _source(), "space-a/live/untouched.md": "pending note",
    }
    before = dict(storage.objects)
    service = make_service(max_size=100)
    valid = "condensed facts"
    second = (_recovery_provider_error() if invalid_response else
              _recovery_response(valid if recover else '{"PRIVATE_REJECTED_OUTPUT":true}'))
    service._complete_chat = AsyncMock(side_effect=[_recovery_provider_error(), second])
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(consolidator_module, "assert_space_not_reserved", AsyncMock())
    with consolidator_module._direct_local_compaction_authority(
        consolidator_module._issue_direct_local_compaction_authority("space-a", DirectLocalWriteSink(storage))
    ):
        result = await service.compact_bank("space-a", dry_run=False)
    assert service._complete_chat.await_count == 2
    if not recover:
        assert result["status"] == "error"
        assert result["failed_phase"] == "prepare"
        assert result["files"][0]["error"] == (
            "compaction_provider_invalid_response" if invalid_response else "invalid_compaction_summary_format"
        )
        if invalid_response:
            assert "bank_write" not in result["remediation"]
            assert "provider" in result["remediation"]
        assert storage.objects == before
        assert storage.events == []
    else:
        assert result["status"] == "ok"
        assert result["preimage_id"].startswith("space-a/")
        expected = "# Bank\n\n" + valid + "\n"
        assert storage.objects["space-a/bank/facts.md"] == expected
        assert result["files"][0]["result_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert storage.objects["space-a/live/untouched.md"] == "pending note"
        assert any(key.startswith("_backups/") and value == _source() for key, value in storage.objects.items())


async def test_compaction_correction_context_is_rechecked_before_egress():
    service = make_service()
    messages = service._build_compaction_plan_messages("facts.md", _source(), 10_000, "")
    service._context_window = sum(consolidator_module._strict_compaction_input_tokens(m["content"]) for m in messages) + 16 * len(messages) + service._max_tokens
    service._complete_chat = AsyncMock(side_effect=_recovery_provider_error())
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert candidate is None
    assert details["error"] == "compaction_context_exhausted"
    assert service._complete_chat.await_count == 1


async def test_compaction_cancellation_during_correction_propagates():
    service = make_service()
    service._complete_chat = AsyncMock(side_effect=[_recovery_provider_error(), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert service._complete_chat.await_count == 2


@pytest.mark.parametrize("second", [
    _recovery_provider_error(),
    _recovery_response("PRIVATE_REJECTED_OUTPUT", "length"),
    _recovery_response("PRIVATE_REJECTED_OUTPUT", "other"),
    _recovery_response('{"file_edits":[]}'),
])
async def test_compaction_mixed_faults_never_buy_a_third_operation(second):
    service = make_service()
    valid = _recovery_response("condensed facts")
    service._complete_chat = AsyncMock(side_effect=[_recovery_provider_error(), second, valid])
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert candidate is None
    assert details["status"] == "error"
    assert service._complete_chat.await_count == 2


async def test_compaction_unknown_candidate_failure_is_not_correctable(monkeypatch):
    service = make_service()
    service._complete_chat = AsyncMock(return_value=_recovery_response("{}"))
    monkeypatch.setattr(consolidator_module, "_compaction_summary_text", lambda text: (None, "future_storage_or_authority_failure"))
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert candidate is None
    assert details["error"] == "future_storage_or_authority_failure"
    assert service._complete_chat.await_count == 1


async def test_compaction_correction_reaches_real_chatrequest_boundary(monkeypatch):
    from live_mem.core import inference_runtime

    service = make_service(max_tokens=200_000, context_window=500_000)
    del service._complete_chat
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise _recovery_provider_error()
            return _recovery_response("condensed facts")

    class Runtime:
        def chat_provider(self):
            return Provider()

    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: Runtime())
    candidate, details = await service._plan_single_file_compaction("facts.md", _source(), 10_000, "")
    assert candidate is not None
    assert details["status"] == "ok"
    assert len(requests) == 2
    assert all(r.retry_policy == "none" and r.max_output_tokens == 200_000 and r.timeout_seconds == 1 for r in requests)
