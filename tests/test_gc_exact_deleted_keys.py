"""GC counts exactly the keys the consolidator deleted.

An exact selection used to be projected from a count onto a key prefix, which
is wrong as soon as one deletion fails in the middle.  The consolidator now
reports ``deleted_note_keys`` for an exact selection; GC uses it when present
and valid, keeps the historical prefix projection only for a legacy result that
lacks the field, and fails closed on a present-but-invalid field.
"""

from __future__ import annotations

from typing import Any

import pytest

from live_mem.core import consolidator as consolidator_module
from live_mem.core import gc as gc_module
from tests.test_gc_safety import GCStorage, _bind_runtime, _old_key, _seed_space


class _ExactKeysConsolidator:
    """Deletes what it is told to, reports exactly the surviving keys."""

    def __init__(self, *, fail: set[str] | None = None, remaining: int = 0, override: Any = None):
        self.fail = fail or set()
        self.remaining = remaining
        self.override = override
        self.calls: list[dict[str, Any]] = []
        self.storage: GCStorage | None = None

    async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
        self.calls.append({"space_id": space_id, **kwargs})
        selected = list(kwargs.get("note_keys", []))
        deleted: list[str] = []
        for key in selected:
            if key in self.fail:
                continue
            if self.storage is not None:
                await self.storage.delete(key)
            deleted.append(key)
        result: dict[str, Any] = {
            "status": "ok" if not self.fail and self.remaining == 0 else "partial",
            "notes_processed": len(deleted),
            "notes_deleted": len(deleted),
            "notes_delete_failed": len(self.fail),
            "notes_remaining": self.remaining,
            "bank_files_created": 0,
            "bank_files_updated": 0,
            "deleted_note_keys": deleted,
        }
        if self.override is not None:
            result["deleted_note_keys"] = self.override
        return result


async def _run(monkeypatch: pytest.MonkeyPatch, sid: str, consolidator, *old_suffixes: str):
    storage = GCStorage()
    keys = [_old_key(sid, suffix) for suffix in old_suffixes]
    _seed_space(storage, sid, *keys)
    _bind_runtime(monkeypatch, storage)
    consolidator.storage = storage
    monkeypatch.setattr(consolidator_module, "get_consolidator", lambda: consolidator)
    result = await gc_module.GCService().consolidate_old_notes(sid, 7)
    return result, keys, consolidator.calls[0]["note_keys"]


async def test_gc_counts_only_the_keys_actually_deleted_when_the_notice_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "gc-exact-notice-failed"

    class NoticeFails(_ExactKeysConsolidator):
        # The notice is always first in the selection (gc.py); the consolidator
        # only learns its key at call time, so the failure is bound there.
        async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
            self.fail = {kwargs["note_keys"][0]}
            return await super().consolidate(space_id, **kwargs)

    result, keys, selected = await _run(monkeypatch, sid, NoticeFails(), "a", "b")
    assert selected[0] not in keys  # the notice comes first, old notes after
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["notice_processed"] is False
    assert detail["notes_processed"] == 2
    assert detail["status"] == "partial"


async def test_gc_excludes_an_old_note_whose_deletion_failed_after_the_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "gc-exact-old-failed"

    class SecondOldFails(_ExactKeysConsolidator):
        async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
            # note_keys = [notice, *sorted(old)] -> fail the second old note.
            self.fail = {kwargs["note_keys"][2]}
            return await super().consolidate(space_id, **kwargs)

    result, keys, selected = await _run(monkeypatch, sid, SecondOldFails(), "a", "b", "c")
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["notice_processed"] is True
    assert detail["notes_processed"] == 2
    assert detail["status"] == "partial"


async def test_gc_counts_only_deleted_keys_when_the_exact_selection_was_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "gc-exact-truncated"

    class Truncating(_ExactKeysConsolidator):
        async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
            # The cap left the last selected key unprocessed.
            kwargs["note_keys"] = list(kwargs["note_keys"])[:-1]
            self.remaining = 1
            return await super().consolidate(space_id, **kwargs)

    result, keys, selected = await _run(monkeypatch, sid, Truncating(), "a", "b", "c")
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["notes_processed"] == 2
    assert detail["notice_processed"] is True
    assert detail["status"] == "partial"


async def test_gc_keeps_the_prefix_projection_for_a_legacy_result_without_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "gc-legacy-prefix"

    class Legacy(_ExactKeysConsolidator):
        async def consolidate(self, space_id: str, **kwargs: Any) -> dict:
            result = await super().consolidate(space_id, **kwargs)
            del result["deleted_note_keys"]
            return result

    result, keys, selected = await _run(monkeypatch, sid, Legacy(), "a", "b")
    detail = result["consolidation_details"][sid]["alice"]
    assert detail["status"] == "ok"
    assert detail["notes_processed"] == 2
    assert detail["notice_processed"] is True


@pytest.mark.parametrize(
    "override",
    [
        "not-a-list",
        ["dup", "dup"],
        ["foreign/live/key.md"],
        [42],
    ],
    ids=["not-a-list", "duplicate", "outside-selection", "not-a-string"],
)
async def test_gc_fails_closed_on_a_present_but_invalid_deleted_note_keys(
    monkeypatch: pytest.MonkeyPatch, override: Any
) -> None:
    sid = f"gc-exact-invalid-{abs(hash(str(override))) % 1000}"
    result, keys, selected = await _run(
        monkeypatch, sid, _ExactKeysConsolidator(override=override), "a", "b"
    )
    detail = result["consolidation_details"][sid]["alice"]
    # Zero extrapolation: nothing is counted as processed, the notice is not
    # considered processed, and the reason is stable.
    assert detail["status"] == "partial"
    assert detail["reason"] == "invalid_deleted_note_keys"
    assert detail["notes_processed"] == 0
    assert detail["notice_processed"] is False
