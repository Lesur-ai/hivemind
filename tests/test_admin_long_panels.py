"""Exercise LONG's five panels and bounded reads with the shared controller."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / 'src/live_mem/static/js/admin/views-space-detail.js'
RUNTIME = ROOT / 'tests/js/admin_long_panels_runtime.mjs'


def run_runtime(view):
    return subprocess.run(
        [shutil.which('node'), str(RUNTIME), str(view)],
        cwd=ROOT, capture_output=True, text=True,
    )


def test_long_panels_runtime():
    result = run_runtime(VIEW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'admin LONG panels runtime: ok' in result.stdout


@pytest.mark.parametrize('old,new', [
    ('if (state.error || !ingestPage(state.list, true)) return;', 'if (state.error) return;'),
    ('data.count === 0 && data.offset > 0 && data.offset >= data.total', 'data.count === 0 && data.offset >= data.total'),
    ('result?.job_id === feedback.job_id &&', 'true &&'),
    ("['queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped'].includes(result.status)", 'true'),
    ('!cancelStatusRead && current() && state.selected', 'current() && state.selected'),
    ('!isCurrent() || state.cancelResult !== feedback', 'state.cancelResult !== feedback'),

    ('void loadLong(view, view.longPanel === \'graph\');', 'void loadLong(view, true);'),
    ('async function loadDocument(view, includeContent = false)', 'async function loadDocument(view, includeContent = true)'),
    ('node.longMarkup === html', 'false'),
    ('canAuto: () => ingestActive(view.ingestions.detail) || !!view.ingestions.list?.jobs.some(ingestActive)',
     'canAuto: () => view.ingestions.list?.polling?.recommended !== false && (ingestActive(view.ingestions.detail) || !!view.ingestions.list?.jobs.some(ingestActive))'),
    ('if (!longOwned(view) || seq !== state.detailSeq || state.selected !== selected) return;',
     'if (!longOwned(view)) return;'),
    ("if (!hasPermission(view, 'write') || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return;",
     'if (!ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return;'),
    ("showModal('Cancel ingestion',", "void callTool('long_ingest_cancel', { space_id: view.spaceId, job_id: jobId }); showModal('Cancel ingestion',"),
    ("callTool('long_ingest_cancel', { space_id: view.spaceId, job_id: jobId })",
     "callTool('long_ingest_cancel', { space_id: 'another-space', job_id: jobId })"),
    ("if (!longOwned(view) || !hasPermission(view, 'write') || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return false;",
     "if (!longOwned(view) || !hasPermission(view, 'write') || state.cancelBusy) return false;"),
    ("if (!longOwned(view) || !hasPermission(view, 'write') || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return false;",
     "if (!longOwned(view) || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return false;"),
    ('if (!current()) return { skipped: true };', 'if (!current()) return;'),
    ('return current() ? { follow: true } : { skipped: true };', 'return { follow: true };'),
    ('longPagination(data.offset, data.count, data.total_count)',
     'longPagination(state.offset, data.count, data.total_count)'),
    ('!Number.isSafeInteger(offset)', 'false'),
    ('offset < 0', 'false'),
    ('!Number.isSafeInteger(count)', 'false'),
    ('count < 0', 'false'),
    ('!Number.isSafeInteger(offset + count)', 'false'),
    ('if (offset < 0 || offset >= state.list.total) return;', 'if (offset >= state.list.total) return;'),
    ('if (offset < 0 || offset >= state.list.total) return;', 'if (offset < 0) return;'),
    ("if (state.error || !ingestPage(state.list) || state.list.count === 0 || !['-1', '1'].includes(data.step)) return;",
     "if (!ingestPage(state.list) || state.list.count === 0 || !['-1', '1'].includes(data.step)) return;"),
    ("if (state.error || !ingestPage(state.list) || state.list.count === 0 || !['-1', '1'].includes(data.step)) return;",
     "if (state.error || state.list.count === 0 || !['-1', '1'].includes(data.step)) return;"),
    ("data.status === 'ok' && !data.partial && Array.isArray(data.jobs)",
     "data.status === 'ok' && Array.isArray(data.jobs)"),
    ('if (ingestPage(result) && result.count === 0 && result.total === 0 && result.offset === 0)',
     'if (result.count === 0 && result.total === 0 && result.offset === 0)'),
    ('if (ingestPage(result) && result.count === 0 && result.total === 0 && result.offset === 0)',
     'if (ingestPage(result) && result.count === 0 && result.total >= 0 && result.offset === 0)'),
    ('state.selected = state.detail = null;\n            state.checkDetail = false;',
     'state.detail = null;\n            state.checkDetail = false;'),
    ('state.revision++; state.readRevision = state.revision;', 'state.readRevision = state.revision;'),
    ("view.longFailure ? renderLongConnection(view, view.longFailure) : stateLoading('Loading long memory…')",
     "view.longFailure ? unavailableOrError({ message: view.longError }, 'sd-long-refresh') : stateLoading('Loading long memory…')"),
    ("!view.longData ? (view.longFailure ? renderLongConnection(view, view.longFailure) : stateLoading('Loading long status…')) :",
     "!view.longData ? 'Configured ontology is not reported.' :"),
])
def test_long_panel_guards_are_mutation_proven(tmp_path, old, new):
    source = VIEW.read_text()
    if old in ("!Number.isSafeInteger(offset)", "offset < 0", "!Number.isSafeInteger(count)", "count < 0", "!Number.isSafeInteger(offset + count)"):
        start = source.index("    function longPagination(")
        end = source.index("    function paintDocuments(", start)
        target = source[start:end]
    elif "current()" in old:
        start = source.index("    async function refreshIngestions(")
        end = source.index("    function requestIngestions(", start)
        target = source[start:end]
    else:
        target = source
    assert target.count(old) == 1
    mutant = tmp_path / VIEW.name
    mutant.write_text(source.replace(target, target.replace(old, new, 1), 1))
    result = run_runtime(mutant)
    assert result.returncode != 0, 'LONG mutation survived: ' + old
    assert 'AssertionError' in result.stderr, result.stdout + result.stderr


@pytest.mark.parametrize('function,old,new,oracle', [
    ('function renderLongHealth', "attentionBanner('Waiting for the first ingestion',", "attentionBanner('Unbound',", 'R1 F1 unbound attention banner'),
    ('function renderLongHealth', "failClosedBanner('Long runtime unavailable', 'The required embedded long runtime is not configured for this space.')", "'<p>Disconnected</p>'", 'R1 F2 unavailable runtime alert'),
    ('function graphStatsSection', 'reachable !== true || !isRecord', 'false || !isRecord', 'R1 F3 no metrics outside contract'),
    ('function graphStatsSection', 'reachable !== true || !isRecord', 'reachable !== true || false', 'R1 F3 no metrics outside contract'),
    ('async function loadLong', "(!view.longData || view.longData.status !== 'ok')", 'true', 'R1 F4 keep successful LONG snapshot'),
    ('function paintLong', "view.longData.status !== 'ok' ? renderLongConnection(view, view.longData) :", "false ? '' :", 'ontology retains read_only'),
    ('function safeLongEndpoint', "endpoint.searchParams.set(key, '[redacted]')", 'endpoint.searchParams.set(key, endpoint.searchParams.get(key))', 'R1 F6 query credential redaction'),
    ('function safeLongEndpoint', ".replace(/\\?[^#]*/, '?[redacted]')", '.replace(/\\?[^#]*/, match => match)', 'R1 F6 malformed URL query redaction'),
    ('function graphStatsSection', '${safe(name)}', '${name}', 'R1 escaping of entity types'),
    ('function renderLongHealth', 'safe(data.config.ontology)', 'data.config.ontology', 'R1 escaping of configured ontology'),
    ('function renderLongHealth', ': safe(value)', ': String(value)', 'R1 escaping of memory ID'),
])
def test_long_r1_findings_are_mutation_proven(tmp_path, function, old, new, oracle):
    source = VIEW.read_text()
    start = source.index('    ' + function + '(')
    boundaries = [source.find(marker, start + 1) for marker in ('\n    function ', '\n    async function ')]
    end = min(position for position in boundaries if position >= 0)
    target = source[start:end]
    assert target.count(old) == 1, 'mutation must address exactly one finding guard'
    mutant = tmp_path / VIEW.name
    mutant.write_text(source[:start] + target.replace(old, new, 1) + source[end:])
    result = run_runtime(mutant)
    assert result.returncode != 0, 'R1 finding mutation survived'
    assert 'AssertionError' in result.stderr, result.stdout + result.stderr
    assert oracle in result.stderr, 'mutation did not hit its expected finding oracle: ' + result.stderr
