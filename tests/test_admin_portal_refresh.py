"""Executable contract for the Portal's one active read-refresh controller."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/live_mem/static/js/admin/portal-refresh.js"
RUNTIME = ROOT / "tests/js/admin_portal_refresh_runtime.mjs"


def run(source):
    node = shutil.which("node") or shutil.which("nodejs")
    assert node, "Node.js is required for Portal controller evidence"
    return subprocess.run([node, str(RUNTIME), str(source)], cwd=ROOT, capture_output=True, text=True, check=False)


def test_portal_refresh_runtime():
    completed = run(SOURCE)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "terminal follow: ok" in completed.stdout


@pytest.mark.parametrize("old,new", [
    ("AdminRouter.epoch === owner.epoch", "true"),
    ("sessionGenerationIsCurrent(owner.sessionGeneration)", "true"),
    ("if (owner.flight) return owner.flight;", "// mutant: allow concurrent reads"),
    ("if (document.hidden) {\n            if (!automatic) owner.manualPending = true;\n            return Promise.resolve();\n        }", ""),
    ("if (!automatic) owner.manualPending = true;\n            return Promise.resolve();", "return Promise.resolve();"),
    ("owned(owner) && owner.manualPending && !document.hidden", "false"),
    ("|| result?.skipped === true", ""),
    ("Object.keys(value).length === 2", "true"),
    ("owner.automaticGeneration === automaticGeneration", "true"),
])
def test_portal_refresh_guards_are_mutation_proven(tmp_path, old, new):
    source = SOURCE.read_text()
    assert source.count(old) == 1, old
    mutant = tmp_path / "portal-refresh.js"
    mutant.write_text(source.replace(old, new, 1))
    completed = run(mutant)
    assert completed.returncode != 0, "runtime failed to detect removal of " + old
