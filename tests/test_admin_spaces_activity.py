"""Behavioral proof for the 1.5.2 Spaces inventory/activity amendment."""
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/live_mem/static/js/admin/views-spaces.js"
RUNTIME = ROOT / "tests/js/admin_spaces_activity_runtime.mjs"


def _run(source: Path) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable")
    return subprocess.run([node, str(RUNTIME), str(source)], cwd=ROOT, text=True, capture_output=True, check=False)


def test_spaces_activity_runtime() -> None:
    result = _run(SOURCE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin spaces activity runtime: ok" in result.stdout


@pytest.mark.parametrize(("old", "new", "reason"), [
    ("AdminRouter.epoch === epoch && sessionGenerationIsCurrent(generation)", "sessionGenerationIsCurrent(generation)", "route exit must stop polling"),
    ("AdminRouter.epoch === epoch && sessionGenerationIsCurrent(generation)", "AdminRouter.epoch === epoch", "session ownership must stop late reads"),
    ("            if (document.hidden) { _scheduleLive(epoch); return; }", "", "hidden tabs must not read"),
    ("{ space_ids: ids.join(',') }", "{ space_ids: '' }", "live refresh must use explicit painted IDs"),
    ("if (_pending || !_current(epochAtCall) || document.hidden) return;", "if (!_current(epochAtCall) || document.hidden) return;", "live requests must not overlap"),
    ("        if (_liveStopped) { _clearLive(); return; }", "", "typed unavailable responses must stop automatic refresh"),
    ("? _queueDenied.filter(d => !requestedIds.includes(d.space_id)) : [];", "? [] : [];", "a scoped read must retain warnings for unrequested spaces"),
    ("        if (_liveTimer !== null) return;", "        _clearLive();", "search cannot postpone an existing refresh deadline"),
    ("        if (!_current(epochAtCall, generation)) return;", "", "a late response cannot repaint another session"),
])
def test_spaces_activity_guards_are_mutation_proven(tmp_path: Path, old: str, new: str, reason: str) -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    mutant = tmp_path / SOURCE.name
    mutant.write_text(source.replace(old, new), encoding="utf-8")
    result = _run(mutant)
    assert result.returncode != 0, reason
