"""Focused coverage for the existing backup primitive used by #395."""

from __future__ import annotations

import pytest

from live_mem.core import backup as backup_module
from live_mem.core.backup import BackupService
from live_mem.core.storage import StorageInventoryMetadataError
from live_mem.tools.backup import _parse_backup_id
from tests.test_write_sink import WriteSinkFakeStorage


async def test_backup_operation_id_creates_a_distinct_same_second_preimage() -> None:
    storage = WriteSinkFakeStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/bank/facts.md": "exact source bytes",
    }

    result = await BackupService().create(
        "space-a",
        operation_id="a" * 32,
        storage=storage,
    )
    second = await BackupService().create(
        "space-a",
        operation_id="b" * 32,
        storage=storage,
    )

    assert result["status"] == "created"
    assert result["backup_id"].endswith("-" + "a" * 32)
    assert second["backup_id"].endswith("-" + "b" * 32)
    assert second["backup_id"] != result["backup_id"]
    assert (
        storage.objects[
            f"_backups/{result['backup_id']}/bank/facts.md"
        ]
        == "exact source bytes"
    )
    assert (
        storage.objects[
            f"_backups/{second['backup_id']}/bank/facts.md"
        ]
        == "exact source bytes"
    )
    space_id, timestamp, error = _parse_backup_id(result["backup_id"])
    assert error is None
    assert space_id == "space-a"
    assert timestamp == result["backup_id"].split("/", 1)[1]


@pytest.mark.parametrize("metadata", [None, "not-json", "{}"])
async def test_old_compaction_preimage_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch, metadata: str | None,
) -> None:
    storage = WriteSinkFakeStorage()
    backup_id = "space-a/2026-08-19T00-00-00-" + "a" * 32
    prefix = f"_backups/{backup_id}/"
    storage.objects = {prefix + "bank/facts.md": "retained evidence 😀"}
    if metadata is not None:
        storage.objects[prefix + "_meta.json"] = metadata
    before = dict(storage.objects)
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    result = await BackupService().delete(backup_id)

    assert result["status"] == "error"
    assert result["error"] == "compaction_preimage_protected"
    assert storage.objects == before


async def test_compaction_preimage_descendants_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = WriteSinkFakeStorage()
    backup_id = "space-a/2026-08-19T00-00-00-" + "b" * 32
    key = f"_backups/{backup_id}/bank/facts.md"
    storage.objects[key] = "retained evidence"
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    result = await BackupService().delete(backup_id + "/bank")

    assert result["status"] == "error"
    assert result["error"] == "compaction_preimage_protected"
    assert storage.objects == {key: "retained evidence"}


async def test_compaction_preimage_refusal_requires_no_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_storage():
        raise AssertionError("deletion must refuse from the reserved identity")

    monkeypatch.setattr(backup_module, "get_storage", unexpected_storage)

    result = await BackupService().delete(
        "space-a/2026-08-19T00-00-00-" + "c" * 32
    )

    assert result["status"] == "error"
    assert result["error"] == "compaction_preimage_protected"


async def test_compaction_identity_is_protected_before_first_backup_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = WriteSinkFakeStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/bank/facts.md": "exact source bytes",
    }
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)
    copy_object = storage.copy_object
    deletion_attempts = []

    async def copy_with_concurrent_delete(source_key: str, dest_key: str) -> None:
        backup_id = "/".join(dest_key.split("/")[1:3])
        deletion_attempts.append(await BackupService().delete(backup_id))
        await copy_object(source_key, dest_key)

    monkeypatch.setattr(storage, "copy_object", copy_with_concurrent_delete)

    result = await BackupService().create(
        "space-a", operation_id="d" * 32, storage=storage,
    )

    assert result["status"] == "created"
    assert len(deletion_attempts) == 2
    assert all(item.get("error") == "compaction_preimage_protected"
               for item in deletion_attempts)
    assert storage.objects[f"_backups/{result['backup_id']}/_meta.json"] == "{}"
    assert storage.objects[f"_backups/{result['backup_id']}/bank/facts.md"] == "exact source bytes"


async def test_ordinary_backup_still_deletes_without_affecting_preimage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = WriteSinkFakeStorage()
    backup_id = "space-a/2026-08-19T00-00-00"
    protected_key = f"_backups/{backup_id}-{'e' * 32}/bank/facts.md"
    storage.objects = {
        f"_backups/{backup_id}/_meta.json": "{}",
        f"_backups/{backup_id}/bank/facts.md": "ordinary copy",
        protected_key: "retained evidence",
    }
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    result = await BackupService().delete(backup_id)

    assert result == {"status": "deleted", "backup_id": backup_id, "files_deleted": 2}
    assert storage.objects == {protected_key: "retained evidence"}


async def test_compaction_preimage_remains_listable_and_restorable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = WriteSinkFakeStorage()
    backup_id = "space-a/2026-08-19T00-00-00-" + "f" * 32
    prefix = f"_backups/{backup_id}/"
    storage.objects = {
        prefix + "_meta.json": '{"space_id":"space-a"}',
        prefix + "bank/facts.md": "retained evidence 😀",
    }
    before = dict(storage.objects)
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    listing = await BackupService().list_backups("space-a")
    restored = await BackupService().restore(backup_id)

    assert listing["status"] == "ok"
    assert listing["total"] == 1
    assert listing["backups"][0]["backup_id"] == backup_id
    assert restored == {
        "status": "ok", "backup_id": backup_id,
        "space_id": "space-a", "files_restored": 2,
    }
    assert {key: storage.objects[key] for key in before} == before
    assert storage.objects["space-a/bank/facts.md"] == "retained evidence 😀"


class _MissingSizeBackupStorage(WriteSinkFakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.copy_calls: list[tuple[str, str]] = []

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        objects = await super().list_objects(prefix, max_keys=max_keys)
        for obj in objects:
            if obj["Key"].endswith("/bank/missing-size.md"):
                obj.pop("Size")
        return objects

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        self.copy_calls.append((source_key, dest_key))
        await super().copy_object(source_key, dest_key)


async def test_backup_create_rejects_missing_size_before_copying_any_prefix() -> None:
    storage = _MissingSizeBackupStorage()
    storage.objects = {
        "space-a/_meta.json": "{}",
        "space-a/bank/first.md": "first",
        "space-a/bank/missing-size.md": "second",
    }

    with pytest.raises(
        StorageInventoryMetadataError,
        match="object inventory size metadata is invalid",
    ):
        await BackupService().create(
            "space-a",
            operation_id="c" * 32,
            storage=storage,
        )

    assert storage.copy_calls == []
    assert not any(key.startswith("_backups/") for key in storage.objects)


async def test_backup_restore_rejects_missing_size_before_target_or_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _MissingSizeBackupStorage()
    storage.objects = {
        "_backups/space-a/2026-08-19T00-00-00/_meta.json": '{"x":1}',
        "_backups/space-a/2026-08-19T00-00-00/bank/missing-size.md": "body",
    }

    async def allow_unreserved(_space_id: str) -> None:
        return None

    async def unexpected_classification(*_args) -> str:
        raise AssertionError("target classification ran before inventory preflight")

    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        backup_module, "assert_space_not_reserved", allow_unreserved
    )
    monkeypatch.setattr(backup_module, "hive_status_label", unexpected_classification)

    with pytest.raises(StorageInventoryMetadataError):
        await BackupService().restore("space-a/2026-08-19T00-00-00")

    assert storage.copy_calls == []
    assert not any(key.startswith("space-a/") for key in storage.objects)


async def test_backup_listing_normalizes_missing_size_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _MissingSizeBackupStorage()
    storage.objects = {
        "_backups/space-a/2026-08-19T00-00-00/_meta.json": '{"x":1}',
        "_backups/space-a/2026-08-19T00-00-00/bank/first.md": "first",
        "_backups/space-a/2026-08-19T00-00-00/bank/missing-size.md": "second",
    }
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    result = await BackupService().list_backups("space-a")

    assert result["status"] == "ok"
    assert result["backups"] == [
        {
            "backup_id": "space-a/2026-08-19T00-00-00",
            "space_id": "space-a",
            "timestamp": "2026-08-19T00-00-00",
            "files_count": 3,
            "total_size": 12,
        }
    ]


async def test_backup_listing_omits_invalid_size_enrichment_and_keeps_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _MissingSizeBackupStorage()
    storage.objects = {
        "_backups/space-a/2026-08-19T00-00-00/_meta.json": (
            '{"backup_description":"malformed inventory"}'
        ),
        "_backups/space-a/2026-08-19T00-00-00/bank/invalid.md": "bad",
        "_backups/space-a/2026-08-19T01-00-00/_meta.json": (
            '{"backup_description":"valid inventory"}'
        ),
        "_backups/space-a/2026-08-19T01-00-00/bank/valid.md": "good",
    }
    original_list_objects = storage.list_objects

    async def list_with_invalid_size(prefix: str, max_keys: int = 0) -> list[dict]:
        objects = await original_list_objects(prefix, max_keys=max_keys)
        for obj in objects:
            if obj["Key"].endswith("/bank/invalid.md"):
                obj["Size"] = False
        return objects

    monkeypatch.setattr(storage, "list_objects", list_with_invalid_size)
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)

    result = await BackupService().list_backups("space-a")

    assert result["status"] == "ok"
    assert result["total"] == 2
    by_id = {entry["backup_id"]: entry for entry in result["backups"]}
    malformed = by_id["space-a/2026-08-19T00-00-00"]
    assert malformed == {
        "backup_id": "space-a/2026-08-19T00-00-00",
        "space_id": "space-a",
        "timestamp": "2026-08-19T00-00-00",
        "description": "malformed inventory",
        "files_count": 2,
    }
    assert "total_size" not in malformed

    valid = by_id["space-a/2026-08-19T01-00-00"]
    assert valid == {
        "backup_id": "space-a/2026-08-19T01-00-00",
        "space_id": "space-a",
        "timestamp": "2026-08-19T01-00-00",
        "description": "valid inventory",
        "files_count": 2,
        "total_size": len(
            storage.objects[
                "_backups/space-a/2026-08-19T01-00-00/_meta.json"
            ]
        )
        + len(storage.objects["_backups/space-a/2026-08-19T01-00-00/bank/valid.md"]),
    }


@pytest.mark.parametrize(
    "operation_id",
    ["A" * 32, "a" * 31, "a" * 33, "not-hex".ljust(32, "x")],
)
async def test_backup_operation_id_rejects_malformed_values(operation_id: str) -> None:
    storage = WriteSinkFakeStorage()
    storage.objects = {"space-a/_meta.json": "{}"}

    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        await BackupService().create(
            "space-a", operation_id=operation_id, storage=storage
        )
