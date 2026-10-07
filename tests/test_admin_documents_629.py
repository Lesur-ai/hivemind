"""#629: exercise safe document page navigation, including discarded mutants."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "src/live_mem/static/js/admin/views-space-detail.js"


def run_contract(source):
    start = source.index("    function documentStatusLabel(")
    end = source.index("    function documentFirstPageAvailable(", start) if "    function documentFirstPageAvailable(" in source else source.index("    function documentWarnings(", start)
    program = "const assert = require('node:assert/strict');\n" + source[start:end] + """
const good = { offset: 0, count: 50, limit: 50, total_count: 51, documents: Array(50).fill({}) };
assert.equal(documentPageUsable(good), true);
assert.equal(documentPageUsable({ ...good, offset: 50, count: 1, documents: [{}] }), true);
for (const change of [
    { offset: '0' }, { offset: '<img src=x>' }, { offset: -1 }, { offset: .5 },
    { offset: Number.MAX_SAFE_INTEGER }, { count: true }, { count: 0 }, { count: 51, documents: Array(51).fill({}), total_count: 100 },
    { limit: undefined }, { limit: 100 }, { total_count: '51' }, { total_count: 51.5 }, { total_count: 49 },
    { count: 49 }, { documents: null },
]) assert.equal(documentPageUsable({ ...good, ...change }), false, JSON.stringify(change));
assert.equal(documentPageUsable({ offset: 0, count: 0, limit: 50, total_count: 0, documents: [] }), false);
assert.equal(documentStatusLabel('succeeded'), 'Succeeded');
assert.equal(documentStatusLabel('toString'), 'toString');
assert.equal(documentStatusLabel('__proto__'), '__proto__');
assert.equal(documentStatusLabel('constructor'), 'constructor');
assert.equal(documentStatusLabel('<unknown status>'), '<unknown status>');
"""
    return subprocess.run([shutil.which("node"), "-e", program], cwd=ROOT, text=True, capture_output=True)


def test_document_page_contract():
    result = run_contract(VIEW.read_text())
    assert result.returncode == 0, result.stdout + result.stderr


def test_unknown_document_status_is_not_an_inherited_label():
    source = VIEW.read_text()
    start = source.index("    function documentStatusLabel(")
    end = source.index("    function documentStatus(", start)
    contract = source[start:end]
    assert contract.count('Object.hasOwn(labels, status)') == 1
    mutant = source[:start] + contract.replace('Object.hasOwn(labels, status)', 'status in labels', 1) + source[end:]
    result = run_contract(mutant)
    assert result.returncode != 0 and 'AssertionError' in result.stderr, result.stdout + result.stderr


@pytest.mark.parametrize("guard", [
    "Number.isSafeInteger(data.offset)", "data.count > 0", "data.count <= 50",
    "data.limit === 50", "data.documents.length === data.count",
    "data.total_count >= data.offset + data.count", "Number.isSafeInteger(data.total_count)",
])
def test_document_page_guards_are_mutation_proven(guard):
    source = VIEW.read_text()
    start = source.index("    function documentPageUsable(")
    end = source.index("    function documentFirstPageAvailable(", start)
    contract = source[start:end]
    assert contract.count(guard) == 1
    mutant = source.replace(contract, contract.replace(guard, "true", 1), 1)
    if guard == "Number.isSafeInteger(data.offset)":
        # The sum checks also reject strings/fractional offsets. Mutate the
        # equivalent validation family together instead of claiming a redundant
        # check can independently admit an unsafe offset.
        mutant = mutant.replace("Number.isSafeInteger(data.offset + data.count)", "true", 1)
        mutant = mutant.replace("Number.isSafeInteger(data.offset + 50)", "true", 1)
    result = run_contract(mutant)
    assert result.returncode != 0, f"Document pagination mutant survived: {guard}"
    assert "AssertionError" in result.stderr, result.stdout + result.stderr


def run_recovery_contract(source):
    start = source.index("    function documentStatusLabel(")
    end = source.index("    function documentWarnings(", start)
    app = (ROOT / "src/live_mem/static/js/admin-app.js").read_text()
    formatter = app[app.index("function fmtSize("):app.index("function _pad2(")]
    program = "const assert = require('node:assert/strict');\n" + formatter + source[start:end] + """
assert.equal(typeof documentFirstPageAvailable, 'function');
assert.equal(typeof documentSize, 'function');
const empty = { offset: 50, count: 0, limit: 50, total_count: 50, documents: [] };
assert.equal(documentFirstPageAvailable(empty), true);
assert.equal(documentFirstPageAvailable({ ...empty, total_count: 0, partial: true }), true);
for (const change of [
    { offset: 0 }, { offset: 0, total_count: 0 }, { offset: -50 }, { offset: '50' }, { offset: 50.5 }, { offset: Infinity },
    { count: 1 }, { count: '0' }, { limit: 100 }, { limit: '50' },
    { documents: [{}] }, { documents: null }, { total_count: '50' },
    { total_count: 50.5 }, { total_count: -1 }, { total_count: 51 },
]) assert.equal(documentFirstPageAvailable({ ...empty, ...change }), false, JSON.stringify(change));
for (const bad of ['abc', '0', '', null, undefined, false, NaN, Infinity, -Infinity, -1, {}, []]) {
    assert.equal(documentSize(bad, true), 'Size unavailable');
    assert.equal(documentSize(bad), 'Size unavailable');
}
assert.equal(documentSize(0, true), '0 B');
assert.equal(documentSize(0), 0);
assert.equal(documentSize(1024, true), '1.0 KB');
assert.equal(documentStatusLabel('cleanup_pending'), 'Cleanup pending');
assert.equal(documentStatusLabel('unknown_future_status'), 'unknown_future_status');
"""
    return subprocess.run([shutil.which("node"), "-e", program], cwd=ROOT, text=True, capture_output=True)


def test_document_recovery_size_and_unknown_contract():
    result = run_recovery_contract(VIEW.read_text())
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("guard", [
    "Number.isSafeInteger(data.offset)", "data.offset > 0", "data.count === 0", "data.limit === 50",
    "Array.isArray(data.documents)", "data.documents.length === 0", "Number.isSafeInteger(data.total_count)",
    "data.total_count >= 0", "data.total_count <= data.offset",
])
def test_first_page_recovery_guards_are_mutation_proven(guard):
    source = VIEW.read_text()
    start = source.index("    function documentFirstPageAvailable(")
    end = source.index("    function documentSize(", start)
    contract = source[start:end]
    assert contract.count(guard) == 1
    mutant = source.replace(contract, contract.replace(guard, "true", 1), 1)
    result = run_recovery_contract(mutant)
    assert result.returncode != 0, f"Recovery mutant survived: {guard}"
    # Removing the Array guard throws on null before permitting unsafe recovery.
    assert "AssertionError" in result.stderr or guard == "Array.isArray(data.documents)" and "TypeError" in result.stderr


@pytest.mark.parametrize("guard", ["Number.isFinite(value)", "value >= 0"])
def test_document_size_guards_are_mutation_proven(guard):
    source = VIEW.read_text()
    start = source.index("    function documentSize(")
    end = source.index("    function documentInspectLabel(", start)
    contract = source[start:end]
    assert contract.count(guard) == 1
    result = run_recovery_contract(source.replace(contract, contract.replace(guard, "true", 1), 1))
    assert result.returncode != 0 and "AssertionError" in result.stderr, result.stdout + result.stderr


def test_document_filter_vocabulary_is_persisted_not_read_defaults():
    # Backend equality filters persisted values, not read-side Unknown defaults
    # or job failure statuses. Browser tests exercise all of these UI options.
    import ast
    pipeline = ast.parse((ROOT / "services/graph-memory/src/mcp_memory/core/ingest_pipeline.py").read_text())
    writes = {kw.value.value for call in ast.walk(pipeline) if isinstance(call, ast.Call)
              for kw in call.keywords if kw.arg == "ingestion_status" and isinstance(kw.value, ast.Constant)}
    graph = (ROOT / "services/graph-memory/src/mcp_memory/core/graph.py").read_text()
    writes.update(re.findall(r"\w+\.ingestion_status\s*=\s*'([^']+)'", graph))
    assert writes == {"running", "succeeded", "deprecated", "cleanup_pending"}
    offered = re.search(r"\$\{(\[[^\]]+\])\.map\(status => `<option value=", VIEW.read_text()).group(1)
    assert set(ast.literal_eval(offered)) == writes


@pytest.mark.parametrize("old,new", [
    ("state.loading || !documentFirstPageAvailable(state.list)", "!documentFirstPageAvailable(state.list)"),
    ("state.loading || !documentFirstPageAvailable(state.list)", "state.loading"),
    ("!longOwned(view)", "false"),
    ("view?.longPanel !== 'documents'", "false"),
])
def test_document_recovery_dispatch_guards_are_mutation_proven(tmp_path, old, new):
    source = VIEW.read_text()
    start = source.index("    registerAction('sd-documents-first',")
    end = source.index("    registerAction('sd-document-inspect',", start)
    action = source[start:end]
    assert action.count(old) == 1
    mutant = tmp_path / VIEW.name
    mutant.write_text(source.replace(action, action.replace(old, new, 1), 1))
    result = subprocess.run([shutil.which('node'), str(ROOT / 'tests/js/admin_long_panels_runtime.mjs'), str(mutant)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0 and 'AssertionError' in result.stderr, result.stdout + result.stderr
