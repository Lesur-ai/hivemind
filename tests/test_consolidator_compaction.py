"""Collected regression coverage for the manual and automatic bank compaction paths.

This replaces the retired standalone compaction utility.  Its incomplete
``MagicMock`` settings failed before reaching the service.  These tests exercise
the production methods with the current storage and client seams, entirely
offline.

P13-1C (#276): the provider seam is now the shared inference boundary, so the
double records normalized ``ChatRequest`` calls through ``_complete_chat``
instead of an ``AsyncOpenAI`` completions endpoint.  The recorded request also
proves what a per-call override can no longer do: ``ChatRequest`` has no
temperature field, so the compaction path cannot pin its own temperature.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

import pytest

from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import ConsolidatorService
from tests.test_write_sink import WriteSinkFakeStorage


class CompactionStorage(WriteSinkFakeStorage):
    """Current storage shape used by the compaction methods."""

    async def list_and_get(self, prefix: str) -> list[dict[str, str]]:
        return [
            {"key": key, "content": content}
            for key, content in sorted(self.objects.items())
            if key.startswith(prefix)
        ]


class RecordingChat:
    """Double for the shared-boundary ``_complete_chat`` seam."""

    def __init__(self, content: str = "# Compacted\n") -> None:
        self.calls: list[dict] = []
        self.content = content
        self.error: Exception | None = None

    async def __call__(self, messages, output_budget):
        self.calls.append({"messages": messages, "output_budget": output_budget})
        if self.error is not None:
            raise self.error
        return ChatResult(
            text=self.content,
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason="stop",
        )


def make_service(*, max_size: int = 100, max_tokens: int = 100) -> ConsolidatorService:
    """Build only the attributes used by the isolated compaction methods."""

    service = object.__new__(ConsolidatorService)
    service._legacy_french_prompts = False
    service._bank_file_max_size = max_size
    service._max_tokens = max_tokens
    service._compact_threshold = 0.6
    service._model = "test-model"
    service._complete_chat = RecordingChat()
    return service


def completions_for(service: ConsolidatorService) -> RecordingChat:
    return service._complete_chat


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
    service = make_service(max_size=15360)

    assert service._get_max_size_for_file(filename) == 15360


async def test_auto_compaction_skips_a_bank_below_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    service = make_service(max_size=100, max_tokens=100)
    service._compact_single_file = AsyncMock()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    bank_files = [{"key": "space-a/bank/facts.md", "content": "x" * 200}]
    result = await service._compact_bank_if_needed("space-a", bank_files, "# Rules")

    assert result == {
        "compacted": False,
        "files_compacted": 0,
        "size_before": 200,
        "size_after": 200,
    }
    service._compact_single_file.assert_not_awaited()
    assert storage.objects == {}


async def test_auto_compaction_writes_only_a_smaller_over_limit_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    service = make_service(max_size=100, max_tokens=100)
    service._compact_single_file = AsyncMock(return_value="c" * 60)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    bank_files = [
        {"key": "space-a/bank/activeContext.md", "content": "a" * 360},
        {"key": "space-a/bank/facts.md", "content": "f" * 40},
    ]
    result = await service._compact_bank_if_needed("space-a", bank_files, "# Rules")

    assert result == {
        "compacted": True,
        "files_compacted": 1,
        "size_before": 400,
        "size_after": 100,
    }
    service._compact_single_file.assert_awaited_once_with(
        "activeContext.md", "a" * 360, 100, "# Rules"
    )
    assert storage.objects == {"space-a/bank/activeContext.md": "c" * 60}


async def test_single_file_compaction_uses_the_rules_and_cleans_model_wrappers() -> None:
    service = make_service(max_size=100)
    completions = completions_for(service)
    completions.content = "<think>internal</think>\n```markdown\n# Compacted\n```"

    result = await service._compact_single_file(
        "activeContext.md", "x" * 200, 100, "# Reference rules"
    )

    assert result == "# Compacted"
    assert len(completions.calls) == 1
    call = completions.calls[0]
    # The requested output budget still reaches the boundary; the model and
    # temperature no longer can — they come from the resolved profile only
    # (ADR-0027), and ChatRequest has no field to override either.
    assert call["output_budget"] == 4096
    assert "temperature" not in {
        field.name for field in dataclasses.fields(consolidator_module.ChatRequest)
    }
    prompt = call["messages"][0]["content"]
    assert 'bank file named "activeContext.md"' in prompt
    assert "# Reference rules" in prompt
    assert "Merge redundant information" in prompt
    assert "Remove obsolete or overly granular details" in prompt
    assert "one line per milestone" in prompt


async def test_single_file_compaction_returns_none_when_the_provider_fails() -> None:
    service = make_service()
    completions_for(service).error = RuntimeError("provider unavailable")

    assert await service._compact_single_file("facts.md", "content", 100, "# Rules") is None


async def test_manual_compaction_dry_run_reports_limits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    await storage.put_json("space-a/_meta.json", {"created_at": "2026-01-01"})
    await storage.put("space-a/_rules.md", "# Rules")
    await storage.put("space-a/bank/activeContext.md", "a" * 120)
    await storage.put("space-a/bank/facts.md", "f" * 40)
    before = dict(storage.objects)
    service = make_service(max_size=100)
    service._compact_single_file = AsyncMock(return_value="c" * 60)
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)

    result = await service.compact_bank("space-a", dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["files_total"] == 2
    assert result["files_over_limit"] == 1
    assert result["total_size_before"] == result["total_size_after"] == 160
    assert result["files"] == [
        {
            "filename": "activeContext.md",
            "size": 120,
            "max_size": 100,
            "over_limit": True,
            "ratio": 1.2,
        },
        {
            "filename": "facts.md",
            "size": 40,
            "max_size": 100,
            "over_limit": False,
            "ratio": 0.4,
        },
    ]
    service._compact_single_file.assert_not_awaited()
    assert storage.objects == before


async def test_manual_compaction_writes_a_smaller_result_after_reservation_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    await storage.put_json("space-a/_meta.json", {"created_at": "2026-01-01"})
    await storage.put("space-a/_rules.md", "# Rules")
    await storage.put("space-a/bank/facts.md", "f" * 120)
    service = make_service(max_size=100)
    service._compact_single_file = AsyncMock(return_value="c" * 60)
    reservation_check = AsyncMock()
    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        consolidator_module, "assert_space_not_reserved", reservation_check
    )

    result = await service.compact_bank("space-a", dry_run=False)

    assert result["total_size_before"] == 120
    assert result["total_size_after"] == 60
    assert result["files"][0]["compacted_size"] == 60
    assert result["files"][0]["reduction_pct"] == 50
    reservation_check.assert_awaited_once_with("space-a")
    assert storage.objects["space-a/bank/facts.md"] == "c" * 60


async def test_manual_compaction_reports_a_missing_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CompactionStorage()
    service = make_service()

    monkeypatch.setattr(consolidator_module, "get_storage", lambda: storage)
    result = await service.compact_bank("missing")

    assert result == {"status": "error", "message": "Espace 'missing' introuvable"}
