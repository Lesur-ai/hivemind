"""Portal consolidation consumes the real shared refresh controller."""

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


@pytest.mark.parametrize(("old", "new", "failure"), [
    (
        "await loadLanes(epoch, automatic ? laneIds(state.data) : undefined, isCurrent);",
        "await loadLanes(epoch, undefined, isCurrent);",
        "automatic inventory scan",
    ),
    (
        "        if (!current()) return;\n        if (!data",
        "        if (!data",
        "late response paints another session",
    ),
    (
        "            if (id && seen.has(key)) return;\n",
        "",
        "duplicate jobs",
    ),
    (
        "        history.sort((a, b) => Date.parse(b.finished_at) - Date.parse(a.finished_at));",
        "        history.sort((a, b) => Date.parse(a.finished_at) - Date.parse(b.finished_at));",
        "oldest-first history",
    ),
    (
        "        const captured = scopedItems(scan.spaces).map(s => String(s.space_id || '')).filter(Boolean);",
        "        const captured = scan.spaces.map(s => String(s.space_id || '')).filter(Boolean);",
        "scope expansion in stale bulk confirmation",
    ),
    (
        "if (!['running', 'queued', 'succeeded', 'failed', 'not_found'].includes(data && data.status)) {",
        "if (false) {",
        "error overwrites the detail snapshot",
    ),
    (
        "        if (!initialLane) void PortalRefresh.refresh().catch(() => {});",
        "        void PortalRefresh.refresh().catch(() => {});",
        "redundant initial read in embedded view",
    ),
    (
        "        return !!(m && m.style && m.style.display === 'flex');",
        "        return !!m;",
        "closed modal keeps following its job",
    ),
    (
        "                parallelism_model: initialLane.parallelism_model, service_config: initialLane.service_config };",
        "                parallelism_model: undefined, service_config: undefined };",
        "embedded worker configuration is lost",
    ),
    (
        "            document.getElementById(state.data ? 'consolFreshness' : 'consolLanes').innerHTML = unavailable;",
        "            void unavailable;",
        "unavailable session silently stays loading",
    ),
])
def test_consumer_guards_mutation_proven(tmp_path, old, new, failure):
    result = _run(_mutant(tmp_path, old, new))
    assert result.returncode != 0, failure


@pytest.mark.parametrize(
    ("old", "new", "finding"),
    [
        (
            "        const retainedDenials = Array.isArray(liveIds) && state.data\n"
            "            ? scopedItems(state.data.denied_spaces).filter(item => !liveIds.includes(item.space_id)) : [];",
            "        const retainedDenials = [];",
            "F4: unrequested denied spaces disappear during a live tick",
        ),
        (
            "        if (!lanes.length) {\n"
            "            el.innerHTML = panel(stateEmpty({ title: 'No spaces visible', hint: 'This token cannot see any consolidation lanes.' }));\n"
            "            return;\n"
            "        }\n",
            "",
            "F5: missing lane visibility is presented as quiet activity",
        ),
        (
            '                <button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-toggle" aria-expanded="true">Hide notes</button>',
            "",
            "F6: expanded notes panel has no collapse control",
        ),
        (
            "        else _staleGen += 1; // A late scan must not repaint or refill the collapsed panel.\n",
            "",
            "F6: a late scan repopulates the collapsed panel",
        ),
    ],
)
def test_round_one_consolidation_fixes_are_mutation_proven(tmp_path: Path, old: str, new: str, finding: str) -> None:
    completed = _run(_mutant(tmp_path, old, new))
    assert completed.returncode != 0, f"the runtime must reject {finding}"
