"""Behavioral proof of the shared Portal consolidation launcher."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src/live_mem/static/js/admin-app.js"
RUNTIME = ROOT / "tests/js/admin_consolidation_launcher_runtime.mjs"


def run(path):
    node = shutil.which("node") or shutil.which("nodejs")
    assert node, "Node.js is required for Portal runtime proof"
    return subprocess.run([node, str(RUNTIME), str(path)], cwd=ROOT, capture_output=True, text=True)


def test_shared_launcher():
    result = run(APP)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("old", "new"), [
    ("agent: scope === 'all' ? '' : agent", "agent: ''"),
    ("(scope === 'all' && !canManage)", "(scope === 'all' && false)"),
    ("if (!current() || inFlight) return false;", "if (!current()) return false;"),
    ("        if (!current()) return false;\n        inFlight = false;", "        inFlight = false;"),
    ("&& form && form.isConnected && document.getElementById('portalConsolidationForm') === form", "&& form"),
    ("&& document.getElementById('adminModal')?.style.display === 'flex'", "&& true"),
])
def test_launcher_guards_mutation_proven(tmp_path, old, new):
    source = APP.read_text()
    assert source.count(old) == 1
    mutant = tmp_path / APP.name
    mutant.write_text(source.replace(old, new))
    result = run(mutant)
    assert result.returncode != 0, "launcher runtime must detect weakened guard"
