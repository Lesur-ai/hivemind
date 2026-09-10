import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'space-detail view path is required');
// Use the shipped string-only contract, like the Maintenance harness.
const shellSource = fs.readFileSync(path.join(path.dirname(viewPath), '../admin-app.js'), 'utf8');
const escDeclaration = shellSource.match(/^const esc = .*;$/m);
assert.ok(escDeclaration, 'the shared esc declaration must be available');
const shellEsc = vm.runInNewContext(`${escDeclaration[0]}\nesc;`);

const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

function node(value = '') {
    return {
        attributes: {},
        innerHTML: '',
        value,
        setAttribute(name, attributeValue) { this.attributes[name] = String(attributeValue); },
    };
}

const elements = {
    sdTierPanel: node(),
    sdAuxiliary: node(),
    sdRulesPanel: node(),
    sdAccessPanel: node(),
    sdBackupsPanel: node(),
    sdShortLimit: node('50'),
    sdShortCategory: node(''),
    sdShortAgent: node(''),
    sdShortSince: node(''),
};
const calls = [];
const toasts = [];
let modal = null;
let compactMode = 'ok';
let pendingCompactResolve = null;

const spaceInfo = {
    status: 'ok',
    space_id: 'demo-space',
    description: 'Compaction runtime-proof space',
    hive_status_label: 'local_only',
    live: { notes_count: 1, total_size: 128 },
    bank: { files_count: 1, total_size: 256 },
    consolidation_count: 0,
    synthesis_exists: false,
    consolidation_queue: { lane_state: 'idle', latest_jobs: [], queued_job_ids: [] },
};

function compactionResult(dryRun) {
    if (compactMode === 'pending') {
        return new Promise(resolve => { pendingCompactResolve = resolve; });
    }
    if (compactMode === 'conflict') {
        return { status: 'conflict', message: '<img src=x onerror=window.__xss=1>' };
    }
    if (compactMode === 'error') {
        return {
            status: 'error',
            message: '<img src=x onerror=window.__xss=1>',
            failure_reason: 'compaction_prepare_failed',
            failed_phase: 'prepare',
            rollback_outcome: 'not_started',
            remediation: 'Retry after correcting the bank file.',
            recovery_required: false,
            failures: [{ filename: '<bad>.md', error: '<script>failure</script>' }],
        };
    }
    if (compactMode === 'malformed') {
        return {
            status: 7, space_id: 'demo-space', dry_run: true,
            recovery_required: true, apply_may_have_mutated: true,
            failure_reason: 42, failed_phase: ['<apply>'], rollback_outcome: true,
            remediation: { diagnostic: 'unexpected' }, total_size_after: null,
            files: [{ filename: 23, error: ['<file-error>'], ratio: 2 }],
        };
    }
    if (compactMode === 'partial') {
        return {
            status: 'partial',
            space_id: 'demo-space',
            dry_run: false,
            files_total: 1,
            files_over_limit: 1,
            total_size_before: 220,
            total_size_after: null,
            preimage_id: 'demo-space/partial-preimage',
            failure_reason: 'compaction_apply_failed',
            failed_phase: 'apply',
            rollback_outcome: 'partial',
            files_applied_before_failure: 1,
            apply_may_have_mutated: true,
            recovery_required: true,
            failures: [{
                filename: 'activeContext.md', error: 'ambiguous_or_missing_compaction_target',
                operation_index: 0, target_resolution: 'missing', target_match_count: 0,
                target_heading_sha256: 'c'.repeat(64),
            }],
            files: [{
                filename: 'activeContext.md', size: 220, max_size: 150,
                source_sha256: 'a'.repeat(64), result_sha256: 'b'.repeat(64),
                over_limit: true, ratio: 1.47, compacted_size: 100, reduction_pct: 55,
            }],
        };
    }
    if (compactMode === 'wrong-target') {
        return { status: 'ok', space_id: 'another-space', dry_run: dryRun, files: [] };
    }
    if (dryRun) {
        return {
            status: 'ok',
            space_id: 'demo-space',
            dry_run: true,
            files_total: 1,
            files_over_limit: 1,
            total_size_before: 220,
            total_size_after: 220,
            files: [{
                filename: 'activeContext.md', size: 220, max_size: 150,
                source_sha256: 'a'.repeat(64), over_limit: true, ratio: 1.47,
            }],
        };
    }
    return {
        status: 'ok',
        space_id: 'demo-space',
        dry_run: false,
        files_total: 1,
        files_over_limit: 1,
        total_size_before: 220,
        total_size_after: 100,
        preimage_id: 'demo-space/2026-09-09T120000Z',
        files: [{
            filename: 'activeContext.md', size: 220, max_size: 150,
            source_sha256: 'a'.repeat(64), result_sha256: 'b'.repeat(64),
            over_limit: true, ratio: 1.47, compacted_size: 100, reduction_pct: 55,
        }],
    };
}

const context = {
    console,
    esc: shellEsc,
    icon: () => '',
    pill: (_kind, label) => String(label ?? ''),
    statusDot: (_kind, label) => String(label ?? ''),
    fmtSize: value => `${Number(value || 0)} B`,
    renderTimestamp: value => String(value ?? ''),
    copyable: value => String(value ?? ''),
    pageHeader: title => String(title ?? ''),
    dataTable: (_headers, rows) => `<table>${rows || ''}</table>`,
    panel: value => String(value ?? ''),
    serverMessage: value => `<p>${escapeHtml(value)}</p>`,
    renderMarkdown: value => `<md>${escapeHtml(value)}</md>`,
    stateEmpty: ({ title = '' } = {}) => `<empty>${escapeHtml(title)}</empty>`,
    stateError: ({ title = '' } = {}) => `<error>${escapeHtml(title)}</error>`,
    stateLoading: value => `<loading>${escapeHtml(value)}</loading>`,
    stateUnavailable: value => `<unavailable>${escapeHtml(value)}</unavailable>`,
    attentionBanner: () => '<attention>',
    SPACE_ID_RE: /^[a-z0-9][a-z0-9-]{0,63}$/,
    TIERS: new Set(['short', 'mid', 'long']),
    document: {
        addEventListener() {},
        getElementById(id) { return elements[id] || null; },
        querySelector() { return null; },
    },
    AdminRouter: { epoch: 19 },
    AdminViews: { register() {} },
    registerAction() {},
    showToast(kind, message) { toasts.push({ kind, message }); },
    showModal(title, body, confirmLabel, onConfirm) {
        modal = { title, body, confirmLabel, onConfirm };
    },
    callTool: async (tool, args) => {
        calls.push({ tool, args });
        switch (tool) {
        case 'space_info': return spaceInfo;
        case 'live_read': return { status: 'ok', notes: [] };
        case 'bank_list': return { status: 'ok', file_count: 1, files: [{ filename: 'activeContext.md', size: 220 }] };
        case 'bank_read': return { status: 'ok', filename: 'activeContext.md', size: 220, content: '# Active' };
        case 'graph_status': return args.include_graph
            ? { status: 'ok', connected: true, reachable: true, graph_view: { status: 'ok', nodes: [], edges: [] } }
            : { status: 'ok', connected: false, embedded: true, bound: false };
        case 'space_rules': return { status: 'ok', rules: '# Proof rules' };
        case 'backup_list': return { status: 'ok', backups: [] };
        case 'admin_list_tokens': return { status: 'ok', tokens: [] };
        case 'bank_compact': return compactionResult(args.dry_run === true);
        default: return { status: 'ok' };
        }
    },
};
vm.createContext(context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('space-detail', render);",
    'globalThis.__spaceDetail = { render, renderTier, loadMid, runCompaction, confirmCompact, currentView: () => currentView };',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.ok(context.__spaceDetail, 'space-detail instrumentation failed');

async function settle() {
    for (let index = 0; index < 12; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

function render(spaceId, epoch, permissions = ['read', 'write', 'manage', 'admin']) {
    context.__spaceDetail.render(node(), { spaceId, tier: 'mid' }, {
        epoch,
        identity: { permissions },
    });
}

render('demo-space', 19);
await settle();
let view = context.__spaceDetail.currentView();
assert.ok(view);
assert.match(elements.sdTierPanel.innerHTML, /data-action="sd-compact-dry"/);
assert.match(elements.sdTierPanel.innerHTML, /Check files for compaction/);
assert.equal(elements.sdTierPanel.innerHTML.includes('Compact files…'), false);

view.tier = 'short';
context.__spaceDetail.renderTier(view);
assert.equal(elements.sdTierPanel.innerHTML.includes('sd-compact-dry'), false, 'compaction is MID-only');
view.tier = 'mid';
context.__spaceDetail.renderTier(view);

context.__spaceDetail.confirmCompact(view);
assert.equal(modal, null, 'Apply must not open before a successful dry run');
assert.ok(toasts.some(toast => toast.kind === 'error' && toast.message.includes('successful compaction dry run')));

const compactCallsBeforeDry = calls.filter(call => call.tool === 'bank_compact').length;
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
const dryCalls = calls.filter(call => call.tool === 'bank_compact');
assert.equal(dryCalls.length, compactCallsBeforeDry + 1);
assert.equal(JSON.stringify(dryCalls.at(-1).args), JSON.stringify({ space_id: 'demo-space', dry_run: true }));
assert.match(elements.sdTierPanel.innerHTML, /Files eligible for compaction/);
assert.match(elements.sdTierPanel.innerHTML, /No changes made/);
assert.match(elements.sdTierPanel.innerHTML, /does not generate rewritten content/);
assert.match(elements.sdTierPanel.innerHTML, /Compact files…/);
assert.match(elements.sdTierPanel.innerHTML, /<details class="compaction-details"><summary>Technical details<\/summary>/);
assert.doesNotMatch(elements.sdTierPanel.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g, ''), /SHA-256|Ratio|UTF-8 bytes/);
assert.match(elements.sdTierPanel.innerHTML, /a{64}/);

context.__spaceDetail.confirmCompact(view);
assert.equal(modal.title, 'Compact files');
assert.match(modal.body, /demo-space/);
assert.match(modal.body, /secondary detail may be lost/);
const confirmed = await modal.onConfirm();
assert.equal(confirmed, true);
await settle();
const applyCalls = calls.filter(call => call.tool === 'bank_compact' && call.args.dry_run === false);
assert.equal(applyCalls.length, 1);
assert.equal(JSON.stringify(applyCalls[0].args), JSON.stringify({ space_id: 'demo-space', dry_run: false }));
assert.match(elements.sdTierPanel.innerHTML, /Compaction applied/);
assert.match(elements.sdTierPanel.innerHTML, /demo-space\/2026-09-09T120000Z/);
assert.equal(view.compactDry, null, 'Apply must revoke the dry-run authorization');
assert.ok(toasts.some(toast => toast.kind === 'ok' && toast.message === 'Compaction applied.'));
assert.ok(calls.filter(call => call.tool === 'bank_list').length >= 2, 'Apply must refresh the MID reader');

compactMode = 'ok';
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
compactMode = 'pending';
const protectedApply = context.__spaceDetail.runCompaction(view, false);
await settle();
const resolveApply = pendingCompactResolve;
const retryMid = context.__spaceDetail.loadMid(view);
await retryMid;
assert.equal(view.compactApplying, true, 'a MID refresh must not cancel an in-flight Apply');
assert.equal(typeof pendingCompactResolve, 'function');
const bankListsBeforePartialRefresh = calls.filter(call => call.tool === 'bank_list').length;
const compactionCallsBeforeConcurrentDry = calls.filter(call => call.tool === 'bank_compact').length;
const concurrentDry = context.__spaceDetail.runCompaction(view, true);
await new Promise(resolve => setImmediate(resolve));
const concurrentDryStarted = calls.filter(call => call.tool === 'bank_compact').length > compactionCallsBeforeConcurrentDry;
if (concurrentDryStarted) {
    compactMode = 'ok';
    pendingCompactResolve(compactionResult(true));
}
assert.equal(await concurrentDry, false, 'a MID refresh must not allow a concurrent dry run during Apply');
compactMode = 'partial';
resolveApply(compactionResult(false));
assert.equal(await protectedApply, true);
await settle();
assert.ok(calls.filter(call => call.tool === 'bank_list').length >= bankListsBeforePartialRefresh + 1, 'partial Apply must refresh the MID reader');
assert.match(elements.sdTierPanel.innerHTML, /Compaction recovery required/);
const visibleRecovery = elements.sdTierPanel.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g, '');
assert.match(visibleRecovery, /Files changed before failure:<\/strong> 1/);
assert.match(visibleRecovery, /Final size could not be verified/);
assert.match(visibleRecovery, /Bank content may have changed/);
assert.match(visibleRecovery, /Rollback result:<\/strong> partial/);
assert.match(elements.sdTierPanel.innerHTML, /Source SHA-256/);
assert.match(elements.sdTierPanel.innerHTML, /target_resolution=missing/);
assert.match(elements.sdTierPanel.innerHTML, /No automatic retry/);
assert.match(elements.sdTierPanel.innerHTML, /role="status"/);
assert.equal(elements.sdTierPanel.innerHTML.includes('Compaction not applied (partial)'), false);

compactMode = 'conflict';
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
assert.match(elements.sdTierPanel.innerHTML, /Consolidation in progress/);
assert.match(elements.sdTierPanel.innerHTML, /retry when the lane is idle/);
assert.equal(elements.sdTierPanel.innerHTML.includes('<img'), false);

compactMode = 'error';
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
assert.match(elements.sdTierPanel.innerHTML, /Compaction refused or failed/);
assert.match(elements.sdTierPanel.innerHTML, /&lt;img/);
assert.match(elements.sdTierPanel.innerHTML, /&lt;bad&gt;\.md/);
assert.equal(elements.sdTierPanel.innerHTML.includes('<script>'), false);

compactMode = 'malformed';
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
assert.match(elements.sdTierPanel.innerHTML, /Compaction recovery required/);
assert.match(elements.sdTierPanel.innerHTML, /Failure reason:<\/strong> 42/);
assert.match(elements.sdTierPanel.innerHTML, /&lt;apply&gt;/);
assert.match(elements.sdTierPanel.innerHTML, /&lt;file-error&gt;/);
assert.doesNotMatch(elements.sdTierPanel.innerHTML, /<file-error>/);

compactMode = 'wrong-target';
assert.equal(await context.__spaceDetail.runCompaction(view, true), true);
await settle();
assert.match(elements.sdTierPanel.innerHTML, /Compaction refused or failed/);
assert.equal(toasts.filter(toast => toast.message === 'Compaction applied.').length, 1);

compactMode = 'pending';
const staleView = view;
const staleRun = context.__spaceDetail.runCompaction(staleView, true);
await settle();
assert.equal(staleView.compacting, true);
assert.match(elements.sdTierPanel.innerHTML, /Checking bank files…/);
assert.doesNotMatch(elements.sdTierPanel.innerHTML, /Running compaction…/);
render('demo-space', 20);
await settle();
assert.equal(typeof pendingCompactResolve, 'function');
pendingCompactResolve({
    status: 'ok', space_id: 'demo-space', dry_run: true,
    files_total: 1, files_over_limit: 1, total_size_before: 220, total_size_after: 220, files: [],
});
assert.equal(await staleRun, false, 'navigation must invalidate the old response');
await settle();
assert.equal(context.__spaceDetail.currentView().compactDry, null);
assert.equal(elements.sdTierPanel.innerHTML.includes('Files eligible for compaction'), false);

compactMode = 'ok';
render('demo-space', 21, ['read', 'write']);
await settle();
view = context.__spaceDetail.currentView();
context.__spaceDetail.renderTier(view);
assert.equal(elements.sdTierPanel.innerHTML.includes('sd-compact-dry'), false, 'write-only sessions must not see compaction');
const writeOnlyCount = calls.filter(call => call.tool === 'bank_compact').length;
assert.equal(await context.__spaceDetail.runCompaction(view, true), false);
assert.equal(calls.filter(call => call.tool === 'bank_compact').length, writeOnlyCount, 'write-only sessions must not invoke compaction');

console.log('admin space detail compaction runtime: ok');
