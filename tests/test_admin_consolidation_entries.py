"""Spaces/Home entry points use the shared launcher and current-session data."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPACES = ROOT / "src/live_mem/static/js/admin/views-spaces.js"
DASHBOARD = ROOT / "src/live_mem/static/js/admin/views-dashboard.js"
RUNTIME = ROOT / "tests/js/admin_consolidation_entries_runtime.mjs"


def _run(spaces: Path = SPACES, dashboard: Path = DASHBOARD) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable")
    return subprocess.run([node, str(RUNTIME), str(spaces), str(dashboard)], cwd=ROOT, text=True, capture_output=True, check=False)


def test_consolidation_entry_points_runtime() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin consolidation entry points runtime: ok" in result.stdout


@pytest.mark.parametrize(("source_path", "old", "new", "failure"), [
    (DASHBOARD, "if (!_current(ctx) || !result", "if (!result", "late callback does not navigate after route exit"),
    (SPACES, " || !spaces.some(space => space.space_id === spaceId)", "", "only a valid loaded row may launch"),
])
def test_consolidation_entry_guards_are_mutation_proven(tmp_path: Path, source_path: Path, old: str, new: str, failure: str) -> None:
    source = source_path.read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutant = tmp_path / source_path.name
    mutant.write_text(source.replace(old, new), encoding="utf-8")
    result = _run(spaces=mutant) if source_path == SPACES else _run(dashboard=mutant)
    assert result.returncode != 0, "The missing guard must fail its behavioral assertion"
    assert failure in result.stdout + result.stderr
