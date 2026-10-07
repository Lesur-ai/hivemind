"""Home read budget is exercised with its real shared refresh controller."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / 'src/live_mem/static/js/admin/views-dashboard.js'
RUNTIME = ROOT / 'tests/js/admin_home_runtime.mjs'


def test_home_runtime():
    result = subprocess.run([shutil.which('node'), str(RUNTIME), str(VIEW)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'admin Home runtime: ok' in result.stdout


@pytest.mark.parametrize("old,new", [
    ("Math.min(12, columns * rows)", "columns * rows + 20"),
    ("{ include_counts: false }", "{ include_counts: true }"),
    ("Number.isFinite(value) && value > 0", "false"),
    ("_consolidationTime(b.last_consolidation) - _consolidationTime(a.last_consolidation)", "_consolidationTime(a.last_consolidation) - _consolidationTime(b.last_consolidation)"),
    ("{ space_ids: ids.join(',') }", "{ space_ids: '' }"),
    ("if (!_canChangeScope() || _selectedSpaces.length >= 3) return;", "if (!_canChangeScope()) return;"),
    ("return { follow: true };", "return { follow: !!view.data?.lanes.some(lane => lane.running_job) };"),
    ("const current = () => _home === view && _current(view.ctx) && isCurrent();", "const current = () => true;"),
    ("data.notes.slice(0, 20).forEach", "data.notes.forEach"),
    ("if (!PortalRefresh.state().available) {", "if (false) {"),
    ("if (content.textContent !== text) content.textContent = text;", "content.textContent = text;"),
    ("if (!view.inventorySuccess) {", "if (true) {"),
    ("height >= 760 ? 2 : 1", "1"),
    ("data.lanes.filter(lane => lane && ids.includes(lane.space_id))", "data.lanes"),
    ("compaction.status === 'error' || compaction.recovery_required === true", "compaction.status === 'error'"),
    ("if (partial) {", "if (false) {"),
    ("if (partial) {", "if (partial && job.status === 'succeeded') {"),
    ("job.result.status !== 'error'", "true"),
    ("Number.isSafeInteger(result.batches_completed) && Number.isSafeInteger(result.batches_total)", "true"),
    ("result.batches_completed >= 0 && result.batches_total >= result.batches_completed", "true"),
    ('${job.status === \'failed\' ? \' state-error\' : \'\'}">', '${job.status === \'failed\' ? \' state-error\' : \'\'}" role="alert">'),
])
def test_home_budget_and_ownership_guards_are_mutation_proven(tmp_path, old, new):
    source = VIEW.read_text()
    assert source.count(old) == 1
    mutant = tmp_path / VIEW.name
    mutant.write_text(source.replace(old, new))
    result = subprocess.run([shutil.which('node'), str(RUNTIME), str(mutant)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0, 'Home mutation survived: ' + old
    assert 'AssertionError' in result.stderr, result.stdout + result.stderr


def test_home_public_metadata_contract_and_timestamp_selector():
    spec = (ROOT / 'docs/MCP_TOOLS_SPEC.md').read_text().split('### `space_list`', 1)[1].split('### `space_info`', 1)[0]
    assert 'include_counts: bool = True' in spec
    for field in ['last_consolidation', 'consolidation_count', 'total_notes_processed']:
        assert field in spec
    assert 'include_counts' in (ROOT / 'README.fr.md').read_text().split('| `space_list`', 1)[1].split('\n', 1)[0]
    css = (ROOT / 'src/live_mem/static/css/admin.css').read_text()
    assert '.dash-latest-signal .mono-data' in css
    assert '.dash-latest-signal .timestamp' not in css
