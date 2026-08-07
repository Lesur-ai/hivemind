# -*- coding: utf-8 -*-
"""Backup-restore storage fake shared by the Hivemind recovery suites."""

from __future__ import annotations

from tests.test_hivemind_state import FakeStorage


class CopyFakeStorage(FakeStorage):
    """``FakeStorage`` with the copy primitive used by ``BackupService`` tests."""

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        self.put_calls += 1
        self.objects[dest_key] = self.objects[source_key]


def patch_backup_storage(monkeypatch, storage: FakeStorage) -> None:
    """Route ``BackupService``'s locally bound storage factory to ``storage``."""
    monkeypatch.setattr("live_mem.core.backup.get_storage", lambda: storage)
