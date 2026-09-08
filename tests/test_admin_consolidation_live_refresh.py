"""ADMIN_CONSOLE_DESIGN §5.5.1 — bounded live refresh of the Consolidation view.

Owner decision (2026-09-05): while a consolidation job is running or queued the
view must show progress without a manual Refresh. The exception to D8 ("manual
refresh only, no polling") is bounded: one 60 s reload while a lane is active,
nothing otherwise, dropped on a route/session change, no network call while the
tab is hidden, and the same discipline for an open job inspector. The tick
re-reads the painted lanes by explicit ``space_ids`` (an in-memory registry
read); only the full load runs the storage-backed space scan. The runtime is
executed with Node against the real view source; the guards that make the
exception bounded and in-memory are mutation-proven.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tests/js/admin_consolidation_live_refresh_runtime.mjs"
CONSOLIDATION = ROOT / "src/live_mem/static/js/admin/views-consolidation.js"


def _node() -> str:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable")
    return node


def _run(consolidation: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), str(RUNTIME), str(consolidation)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _mutant(tmp_path: Path, old: str, new: str) -> Path:
    text = CONSOLIDATION.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    mutant = tmp_path / CONSOLIDATION.name
    mutant.write_text(text.replace(old, new), encoding="utf-8")
    return mutant


def test_consolidation_view_live_refresh_is_bounded() -> None:
    completed = _run(CONSOLIDATION)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin consolidation live refresh runtime: ok" in completed.stdout


def test_live_refresh_only_while_a_job_is_active_is_mutation_proven(tmp_path: Path) -> None:
    """Unconditional polling (the D8 violation) must be refused by the runtime."""
    mutant = _mutant(tmp_path, "        if (!anyLaneActive(data)) return;\n", "")
    completed = _run(mutant)
    assert completed.returncode != 0, "the runtime must refuse polling idle lanes"


def test_live_refresh_drops_on_epoch_change_is_mutation_proven(tmp_path: Path) -> None:
    """A tick surviving navigation would repaint a foreign route (§3.3.2 rule 3)."""
    mutant = _mutant(
        tmp_path,
        "            if (AdminRouter.epoch !== epoch || !sessionActive()) return;\n            if (document.hidden) { scheduleLive(epoch, data); return; }",
        "            if (!sessionActive()) return;\n            if (document.hidden) { scheduleLive(epoch, data); return; }",
    )
    completed = _run(mutant)
    assert completed.returncode != 0, "the runtime must refuse a tick that ignores the route epoch"


def test_live_tick_never_runs_the_storage_scan_is_mutation_proven(tmp_path: Path) -> None:
    """A tick passing ``space_ids=""`` would re-run ``list_spaces()`` (root S3 LIST +
    three storage calls per space) every minute for the whole job (§5.5.1 Cost)."""
    mutant = _mutant(
        tmp_path,
        "            loadLanes(epoch, laneIds(data));\n",
        "            loadLanes(epoch);\n",
    )
    completed = _run(mutant)
    assert completed.returncode != 0, "the runtime must refuse a tick that re-runs the storage-backed space scan"


def test_job_inspector_keeps_snapshot_on_typed_error_is_mutation_proven(tmp_path: Path) -> None:
    """Repainting any payload would replace a running job by an error state."""
    mutant = _mutant(
        tmp_path,
        "            if (!['running', 'queued', 'succeeded', 'failed', 'not_found'].includes(nextStatus)) {\n                scheduleJobLive(jobId, op, epoch, data);\n                return;\n            }\n",
        "",
    )
    completed = _run(mutant)
    assert completed.returncode != 0, "the runtime must refuse repainting a typed error over a good snapshot"


def test_job_inspector_stops_on_closed_modal_is_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        "        return !!(m && m.style && m.style.display === 'flex');",
        "        return !!m;",
    )
    completed = _run(mutant)
    assert completed.returncode != 0, "the runtime must refuse re-reading a job behind a closed modal"
