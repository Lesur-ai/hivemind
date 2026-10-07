// Runtime proof for the shared admin renderer of a consolidation
// result: a run that stopped at its first batch (notes_total > 0,
// notes_processed = 0) must render its counters, never "nothing to
// consolidate", and a failed job must still show its result block.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const consolidationPath = process.argv[2];
assert.ok(consolidationPath, 'consolidation view path is required');

const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

function stateStub(kind) {
    return options => {
        const o = typeof options === 'string' ? { title: options } : (options || {});
        return `<div class="state-${kind}">${escapeHtml(o.title || '')} ${escapeHtml(o.message || o.hint || '')}</div>`;
    };
}

function context() {
    const ctx = {
        console,
        esc: escapeHtml,
        icon: () => '',
        serverMessage: message => `<p class="server-message">${escapeHtml(message)}</p>`,
        statusDot: (severity, text) => `<span class="dot ${escapeHtml(severity)}">${escapeHtml(text)}</span>`,
        dataTable: (headers, rows) => `<table>${Array.isArray(rows) ? rows.join('') : String(rows ?? '')}</table>`,
        renderTimestamp: value => `<time>${escapeHtml(value)}</time>`,
        copyable: value => `<code>${escapeHtml(value)}</code>`,
        monoBlock: value => `<pre>${escapeHtml(value)}</pre>`,
        stateError: stateStub('error'),
        stateEmpty: stateStub('empty'),
        stateLoading: stateStub('loading'),
        stateUnavailable: stateStub('unavailable'),
        registerAction() {},
        showToast() {},
        showDestructiveModal() {},
        showModal() {},
        closeModal() {},
        callTool: async () => ({}),
        AdminViews: { register() {} },
        AdminRouter: { epoch: 1, go() {}, current: () => ({}) },
        document: {
            addEventListener() {},
            querySelector() { return null; },
            querySelectorAll() { return []; },
            getElementById() { return null; },
        },
        window: {},
        setTimeout,
        clearTimeout,
    };
    vm.createContext(ctx);
    // Load the actual shared renderer, as the browser shell does. Keep this
    // seam present even in fixtures with no automatic-maintenance outcome.
    const appSource = fs.readFileSync(new URL('../../src/live_mem/static/js/admin-app.js', import.meta.url), 'utf8');
    const helperStart = appSource.indexOf('function renderAutoCompaction(');
    const helperEnd = appSource.indexOf('\nfunction ', helperStart + 1);
    assert.ok(helperStart >= 0 && helperEnd > helperStart, 'shared maintenance renderer is missing');
    vm.runInContext(appSource.slice(helperStart, helperEnd), ctx);

    return ctx;
}

// ── consolidation view ───────────────────────────────────────────────────────
const cv = context();
const cvSource = fs.readFileSync(consolidationPath, 'utf8');
const cvInstrumented = cvSource.replace(
    "AdminViews.register('consolidation', render);",
    'globalThis.__consolidation = { renderResultMetrics, renderJob };',
);
assert.notEqual(cvInstrumented, cvSource, 'consolidation instrumentation anchor missing');
vm.runInContext(cvInstrumented, cv, { filename: consolidationPath });
assert.ok(cv.__consolidation, 'consolidation instrumentation failed');

const metrics = cv.__consolidation.renderResultMetrics({
    status: 'error',
    notes_total: 3,
    notes_processed: 0,
    notes_discarded_count: 0,
    notes_deleted: 0,
    notes_remaining: 3,
    failed_batch: 1,
    failure_reason: 'batch_llm_failed',
    message: 'Consolidation stopped at batch 1/2 (batch_llm_failed)',
});
assert.equal(metrics.includes('Nothing to do'), false, 'a stopped run is not "nothing to do"');
assert.ok(metrics.includes('Notes total'));
assert.ok(metrics.includes('Notes remaining'));
assert.ok(metrics.includes('batch_llm_failed'));

const nothingToDo = cv.__consolidation.renderResultMetrics({
    status: 'ok', notes_total: 0, notes_processed: 0, message: 'No new notes to consolidate',
});
assert.ok(nothingToDo.includes('Nothing to do'));

const failedJob = cv.__consolidation.renderJob({
    status: 'failed',
    space_id: 'demo',
    scope_label: 'All agents',
    job_id: 'j2',
    error: 'Consolidation stopped at batch 1/2 (batch_llm_failed)',
    progress: { phase: 'failed', batch_size: 2, notes_total: 3, notes_done: 0, batches_total: 2, batches_done: 0, current_batch: 1 },
    result: {
        status: 'error', notes_total: 3, notes_processed: 0, notes_discarded_count: 0,
        notes_deleted: 0, notes_remaining: 3, failed_batch: 1, failure_reason: 'batch_llm_failed',
        message: 'stopped',
    },
});
assert.ok(failedJob.includes('Failed'));
assert.ok(failedJob.includes('Notes remaining'), 'a failed job must render its counters in the consolidation view');


// ── Files above the threshold before consolidation (indicator only), shared inspector ───────────
{
    const hostile = '<img src=x onerror=alert(1)>.md';
    const advisoryResult = {
        status: 'ok', notes_total: 2, notes_processed: 2, notes_discarded_count: 0, notes_deleted: 2, notes_remaining: 0,
        bank_files_updated: 2, bank_files_created: 0, bank_files_unchanged: 4, operations_applied: 3, operations_failed: 0,
        synthesis_size: 900, llm_tokens_used: 70000, batches_total: 1, batches_completed: 1, duration_seconds: 60,
        bank_size_advisory: [
            { filename: 'progress.md', utf8_bytes: 42553, max_size: 35000 },
            { filename: hostile, utf8_bytes: 99999, max_size: 35000 },
            { filename: 'malformed.md', utf8_bytes: 'big', max_size: 35000 },   // skipped: not an integer
            'garbage',
        ],
    };
    const okJob = cv.__consolidation.renderJob({ job_id: 'j-adv', status: 'succeeded', queue_position: 0, result: advisoryResult });
    assert.ok(okJob.includes('Files above the threshold before consolidation'), 'consolidation inspector must show the size advisory');
    assert.ok(okJob.includes('automatic compaction result'), 'the advisory distinguishes the follow-up outcome');
    assert.ok(okJob.includes('progress.md') && okJob.includes('42553') && okJob.includes('35000'));
    assert.ok(okJob.includes('&lt;img src=x onerror=alert(1)&gt;.md'), 'the filename is escaped');
    assert.equal(okJob.includes(hostile), false, 'no raw markup from a server-provided filename');
    assert.equal(okJob.includes('malformed.md'), false, 'a malformed item is skipped, not rendered');
    assert.equal(okJob.includes('Compaction refused'), false, 'no compaction envelope exists any more');
    const failedAdvisoryJob = cv.__consolidation.renderJob({ job_id: 'j-adv-f', status: 'failed', queue_position: 0, error: 'stopped', result: { ...advisoryResult, status: 'error', failure_reason: 'batch_llm_failed', failed_batch: 1 } });
    assert.ok(failedAdvisoryJob.includes('Files above the threshold before consolidation'), 'a failed job still shows the advisory');
    const silent = cv.__consolidation.renderJob({ job_id: 'j-quiet', status: 'succeeded', queue_position: 0, result: { ...advisoryResult, bank_size_advisory: undefined } });
    assert.equal(silent.includes('Files above the threshold before consolidation'), false, 'no advisory → nothing rendered');


}


// The shared renderer is exercised, not replaced with a no-op test stub.
const maintenanceResult = { status: 'ok', notes_total: 2, notes_processed: 2,
    auto_compaction: { status: 'partial', recovery_required: true,
        preimage_id: '<capture>', started_at: '2026-09-24T08:00:00Z' } };
for (const status of ['succeeded', 'failed']) {
    const html = cv.__consolidation.renderJob({ job_id: 'maintenance', status, result: maintenanceResult });
    assert.ok(html.includes('Compaction incomplete'));
    assert.ok(html.includes('Recovery must be checked before retrying.'));
    assert.ok(html.includes('&lt;capture&gt;'));
    assert.equal(html.includes('<capture>'), false);
    assert.ok(html.includes('Originals are retained before compaction.'));
}
assert.equal(cv.renderAutoCompaction({ auto_compaction: { status: 'disabled' } })
    .includes('Originals are retained before compaction.'), false);

console.log('admin consolidation result runtime: ok');
