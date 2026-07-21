import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'space-detail view path is required');

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

const spaceInfo = {
    status: 'ok',
    space_id: 'demo-space',
    description: 'Runtime-proof space',
    hive_status_label: 'local_only',
    live: { notes_count: 1, total_size: 128 },
    bank: { files_count: 1, total_size: 256 },
    consolidation_count: 0,
    synthesis_exists: false,
    consolidation_queue: { lane_state: 'idle', latest_jobs: [], queued_job_ids: [] },
};

const context = {
    console,
    esc: escapeHtml,
    icon: () => '',
    pill: (_kind, label) => String(label ?? ''),
    statusDot: (_kind, label) => String(label ?? ''),
    fmtSize: value => `${Number(value || 0)} B`,
    renderTimestamp: value => String(value ?? ''),
    copyable: value => String(value ?? ''),
    pageHeader: title => String(title ?? ''),
    dataTable: () => '<table></table>',
    panel: value => String(value ?? ''),
    serverMessage: value => String(value ?? ''),
    renderMarkdown: value => `<md>${escapeHtml(value)}</md>`,
    stateEmpty: () => '<empty>',
    stateError: () => '<error>',
    stateLoading: () => '<loading>',
    stateUnavailable: () => '<unavailable>',
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
        case 'bank_list': return { status: 'ok', file_count: 1, files: [{ filename: 'activeContext.md', size: 32 }] };
        case 'bank_read': return { status: 'ok', filename: 'activeContext.md', size: 32, content: '# Active' };
        case 'graph_status': return args.include_graph
            ? { status: 'ok', connected: true, reachable: true, graph_view: { status: 'ok', nodes: [], edges: [] } }
            : { status: 'ok', connected: false, embedded: true, bound: false };
        case 'space_rules': return { status: 'ok', rules: '# Proof rules' };
        case 'backup_list': return { status: 'ok', backups: [] };
        case 'admin_list_tokens': return { status: 'ok', tokens: [] };
        case 'bank_consolidate': return { status: 'queued', queue_position: 2 };
        case 'graph_push': return { status: 'ok', files_pushed: 1 };
        default: throw new Error(`Unexpected tool: ${tool}`);
        }
    },
};
vm.createContext(context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('space-detail', render);",
    'globalThis.__spaceDetail = { render, renderTier, confirmConsolidate, confirmGraphPush, currentView: () => currentView };',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.ok(context.__spaceDetail, 'space-detail instrumentation failed');

async function settle() {
    for (let index = 0; index < 8; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

const content = node();
context.__spaceDetail.render(content, { spaceId: 'demo-space', tier: 'short' }, {
    epoch: 19,
    identity: { permissions: ['read', 'write', 'manage', 'admin'] },
});
await settle();

const preloadTools = calls.map(call => call.tool);
assert.deepEqual(preloadTools.slice(0, 7), [
    'space_info',
    'live_read',
    'bank_list',
    'graph_status',
    'space_rules',
    'backup_list',
    'admin_list_tokens',
]);
assert.equal(preloadTools.filter(tool => tool === 'bank_read').length, 1, 'first file opens automatically');
assert.equal(preloadTools.includes('bank_consolidate'), false, 'preload must not consolidate');
assert.equal(preloadTools.includes('graph_push'), false, 'preload must not project into long');
assert.ok(elements.sdTierPanel.innerHTML.includes('data-action="sd-confirm-consolidate"'));
assert.equal(elements.sdTierPanel.innerHTML.includes('Load recent notes'), false);

const view = context.__spaceDetail.currentView();
assert.ok(view, 'render must retain the live view');
view.tier = 'mid';
context.__spaceDetail.renderTier(view);
assert.ok(elements.sdTierPanel.innerHTML.includes('data-action="sd-confirm-graph-push"'));
assert.equal(elements.sdTierPanel.innerHTML.includes('Load bank files'), false);

context.__spaceDetail.confirmConsolidate(view);
assert.deepEqual(calls.map(call => call.tool), preloadTools, 'confirmation must precede consolidation');
assert.equal(modal.title, 'Consolidate live notes');
assert.ok(modal.body.includes("all agents' live notes"));
assert.equal(await modal.onConfirm(), true);
await settle();
const consolidateCalls = calls.filter(call => call.tool === 'bank_consolidate');
assert.equal(consolidateCalls.length, 1);
assert.equal(consolidateCalls[0].args.space_id, 'demo-space');
assert.equal(consolidateCalls[0].args.agent, '');
assert.deepEqual(Object.keys(consolidateCalls[0].args), ['space_id', 'agent']);

context.__spaceDetail.confirmGraphPush(view);
assert.equal(calls.filter(call => call.tool === 'graph_push').length, 0, 'confirmation must precede projection');
assert.equal(modal.title, 'Push mid → long');
assert.ok(modal.body.includes('derived long graph'));
assert.ok(modal.body.includes('Volatile bank files are not included.'));
assert.equal(await modal.onConfirm(), true);
await settle();
const graphCalls = calls.filter(call => call.tool === 'graph_push');
assert.equal(graphCalls.length, 1);
assert.equal(graphCalls[0].args.space_id, 'demo-space');
assert.deepEqual(Object.keys(graphCalls[0].args), ['space_id']);
assert.equal(Object.hasOwn(graphCalls[0].args, 'include_volatile'), false);
assert.ok(toasts.some(toast => toast.kind === 'ok' && toast.message.includes('Consolidation queued')));
assert.ok(toasts.some(toast => toast.kind === 'ok' && toast.message.includes('Mid-to-long push finished')));

console.log('admin space detail preload runtime: ok');
