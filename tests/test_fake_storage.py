# -*- coding: utf-8 -*-
"""Contract tests for public shared storage fakes."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from live_mem.core import backup
from tests.fakes import GraphLongFakeStorage
from tests.fakes.backup_storage import CopyFakeStorage, patch_backup_storage


_ROOT = Path(__file__).resolve().parents[1]
_BACKUP_TEST_STATE = "tests.test_hivemind_state"


async def test_graph_long_fake_storage_preserves_common_suite_semantics() -> None:
    storage = GraphLongFakeStorage()

    assert await storage.get("space/missing") is None
    assert await storage.get_json("space/missing.json") is None
    await storage.put_json("space/_meta.json", {"space_id": "éspace"})
    await storage.put("space/bank/b.md", "B")
    await storage.put("space/bank/.keep", "")
    await storage.put("space/bank/a.md", "A")

    assert storage.objects["space/_meta.json"] == '{\n  "space_id": "éspace"\n}'
    assert await storage.get_json("space/_meta.json") == {"space_id": "éspace"}
    assert [item["Key"] for item in await storage.list_objects("space/bank/", max_keys=1)] == [
        "space/bank/.keep",
        "space/bank/a.md",
        "space/bank/b.md",
    ]
    assert await storage.list_and_get("space/bank/") == [
        {"key": "space/bank/a.md", "content": "A", "size": 1, "last_modified": ""},
        {"key": "space/bank/b.md", "content": "B", "size": 1, "last_modified": ""},
    ]
    assert [item["key"] for item in await storage.list_and_get("space/bank/", False)] == [
        "space/bank/.keep",
        "space/bank/a.md",
        "space/bank/b.md",
    ]

    snapshot = storage.snapshot()
    await storage.put("space/bank/c.md", "C")
    assert "space/bank/c.md" not in snapshot


async def test_copy_fake_storage_copies_and_patches_backup_factory(monkeypatch) -> None:
    storage = CopyFakeStorage()
    storage.objects["source"] = "payload"
    put_calls_before = storage.put_calls

    await storage.copy_object("source", "destination")
    patch_backup_storage(monkeypatch, storage)

    assert storage.objects["destination"] == "payload"
    assert storage.put_calls == put_calls_before + 1
    assert backup.get_storage() is storage


def _probe_general_fakes_import(*, backup_test_state_loaded: bool) -> subprocess.CompletedProcess[str]:
    injected_state = (
        f"sys.modules[{_BACKUP_TEST_STATE!r}] = object(); "
        if backup_test_state_loaded
        else ""
    )
    return subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "import sys; import tests.fakes; "
            f"{injected_state}"
            "sys.exit('tests.fakes not imported') "
            "if 'tests.fakes' not in sys.modules else None; "
            f"sys.exit('{_BACKUP_TEST_STATE} imported') "
            f"if {_BACKUP_TEST_STATE!r} in sys.modules else None",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_general_fakes_package_does_not_import_backup_test_state() -> None:
    result = _probe_general_fakes_import(backup_test_state_loaded=False)

    assert result.returncode == 0, result.stderr


def test_general_fakes_cycle_guard_fails_under_optimized_python() -> None:
    result = _probe_general_fakes_import(backup_test_state_loaded=True)

    assert result.returncode == 1
    assert f"{_BACKUP_TEST_STATE} imported" in result.stderr
