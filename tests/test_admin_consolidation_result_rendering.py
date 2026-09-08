"""Admin consumers of a consolidation result, proven by execution.

A run that stopped at its first batch has ``notes_total > 0`` and
``notes_processed == 0``: both admin views must render its counters instead of
"nothing to consolidate", and a failed job must still show its result block.
The runtime is executed with Node against the real view sources, and each
regression is mutation-proven: rewiring the "nothing" branch on
``notes_processed`` or hiding the result block on failed jobs must make the
runtime fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tests/js/admin_consolidation_result_runtime.mjs"
SPACE_DETAIL = ROOT / "src/live_mem/static/js/admin/views-space-detail.js"
CONSOLIDATION = ROOT / "src/live_mem/static/js/admin/views-consolidation.js"


def _node() -> str:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable")
    return node


def _run(space_detail: Path, consolidation: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), str(RUNTIME), str(space_detail), str(consolidation)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_consolidation_result_consumers_render_counters_for_stopped_runs() -> None:
    completed = _run(SPACE_DETAIL, CONSOLIDATION)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin consolidation result runtime: ok" in completed.stdout


def _mutant(tmp_path: Path, source: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    mutant = tmp_path / source.name
    mutant.write_text(text.replace(old, new), encoding="utf-8")
    return mutant


def test_space_detail_nothing_branch_is_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        SPACE_DETAIL,
        "if (Number(result.notes_total) === 0) {",
        "if (result.notes_processed === 0) {",
    )
    completed = _run(mutant, CONSOLIDATION)
    assert completed.returncode != 0, "the runtime must refuse the notes_processed regression"


def test_space_detail_failed_job_result_block_is_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        SPACE_DETAIL,
        "${['succeeded', 'failed'].includes(job.status) ? renderJobResult(job) : ''}",
        "${job.status === 'succeeded' ? renderJobResult(job) : ''}",
    )
    completed = _run(mutant, CONSOLIDATION)
    assert completed.returncode != 0, "the runtime must refuse hiding the result of a failed job"


def test_consolidation_view_nothing_branch_is_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        CONSOLIDATION,
        "if (Number(result.notes_total) === 0 && result.message) {",
        "if (Number(result.notes_processed) === 0 && result.message) {",
    )
    completed = _run(SPACE_DETAIL, mutant)
    assert completed.returncode != 0, "the runtime must refuse the notes_processed regression"


def test_consolidation_view_failed_job_metrics_are_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        CONSOLIDATION,
        "${renderResultMetrics(job.result)}${renderBankSizeAdvisory(job.result)}`;",
        "${renderBankSizeAdvisory(job.result)}`;",
    )
    completed = _run(SPACE_DETAIL, mutant)
    assert completed.returncode != 0, "the runtime must refuse hiding a failed job's metrics"


def test_bank_size_advisory_rendering_is_mutation_proven(tmp_path: Path) -> None:
    """An inspector that silently drops the size advisory would hide the
    only signal the operator has that a human compaction decision is due."""
    mutant = _mutant(
        tmp_path,
        CONSOLIDATION,
        "        const items = result && Array.isArray(result.bank_size_advisory) ? result.bank_size_advisory : [];\n",
        "        const items = [];\n",
    )
    completed = _run(SPACE_DETAIL, mutant)
    assert completed.returncode != 0, "the runtime must refuse an inspector that hides the size advisory"


def test_space_detail_bank_size_advisory_rendering_is_mutation_proven(tmp_path: Path) -> None:
    mutant = _mutant(
        tmp_path,
        SPACE_DETAIL,
        "        const items = result && Array.isArray(result.bank_size_advisory) ? result.bank_size_advisory : [];\n",
        "        const items = [];\n",
    )
    completed = _run(mutant, CONSOLIDATION)
    assert completed.returncode != 0, "the runtime must refuse a space-detail inspector that hides the size advisory"
