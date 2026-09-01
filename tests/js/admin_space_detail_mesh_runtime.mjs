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
        innerHTML: '', value,
        setAttribute() {},
    };
}

const elements = {
    sdTierPanel: node(),
    sdAuxiliary: node(),
    sdMeshReadiness: node(),
    sdRulesPanel: node(),
    sdAccessPanel: node(),
    sdBackupsPanel: node(),
    sdShortLimit: node('50'),
    sdShortCategory: node(''),
    sdShortAgent: node(''),
    sdShortSince: node(''),
};
const documentHandlers = new Map();
const meshCalls = [];
let meshAvailable = true;
let meshResponse = null;

const baseSource = {
    space_id: 'demo-space',
    state: 'local_only_can_prepare',
    source_ready: false,
    source_initializable: true,
    can_create_invitation: false,
    resumable: false,
    reason_code: 'local_only_can_prepare',
    message: 'This local space can be prepared for Project Mesh.',
    state_token: '1'.repeat(64),
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
    renderMarkdown: value => escapeHtml(value),
    stateEmpty: () => '<empty>',
    stateError: () => '<error>',
    stateLoading: () => '<loading>',
    stateUnavailable: () => '<unavailable>',
    SPACE_ID_RE: /^[a-z0-9][a-z0-9-]{0,63}$/,
    TIERS: new Set(['short', 'mid', 'long']),
    document: {
        getElementById(id) { return elements[id] || null; },
        querySelector() { return null; },
        addEventListener(name, handler) { documentHandlers.set(name, handler); },
    },
    AdminRouter: { epoch: 1 },
    AdminViews: { register() {} },
    registerAction() {},
    showToast() {},
    showModal() {},
    meshIsAvailable() { return meshAvailable; },
    meshAdminSourceReadiness(spaceId) {
        meshCalls.push(spaceId);
        return Promise.resolve(meshResponse);
    },
    callTool: async (tool, args) => {
        if (tool === 'space_info') {
            return {
                status: 'ok', space_id: args.space_id, description: '',
                hive_status_label: 'hivemind_healthy',
                live: {}, bank: {}, consolidation_queue: {},
            };
        }
        if (tool === 'live_read') return { status: 'ok', notes: [] };
        if (tool === 'bank_list') return { status: 'ok', files: [] };
        if (tool === 'graph_status') return { status: 'ok', connected: false };
        if (tool === 'space_rules') return { status: 'ok', rules: '' };
        if (tool === 'backup_list') return { status: 'ok', backups: [] };
        if (tool === 'admin_list_tokens') return { status: 'ok', tokens: [] };
        throw new Error(`Unexpected tool: ${tool}`);
    },
};

vm.createContext(context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('space-detail', render);",
    'globalThis.__spaceDetail = { render, currentView: () => currentView };',
);
vm.runInContext(instrumented, context, { filename: viewPath });

async function settle() {
    for (let index = 0; index < 10; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

async function render({ permissions = ['admin'], available = true, response } = {}) {
    context.AdminRouter.epoch += 1;
    meshAvailable = available;
    meshResponse = response;
    elements.sdMeshReadiness.innerHTML = '';
    const before = meshCalls.length;
    context.__spaceDetail.render(node(), { spaceId: 'demo-space', tier: 'short' }, {
        epoch: context.AdminRouter.epoch,
        identity: { permissions },
    });
    await settle();
    return {
        html: elements.sdMeshReadiness.innerHTML,
        calls: meshCalls.length - before,
    };
}

// A contrary legacy hive label cannot suppress an action authorized by the
// targeted readiness record.
let outcome = await render({
    response: { status: 'ok', source: { ...baseSource } },
});
assert.equal(outcome.calls, 1);
assert.match(outcome.html, /Prepare for Project Mesh/);
assert.match(outcome.html, /data-mesh-source-action="prepare"/);
assert.match(outcome.html, /href="#\/mesh"/);

// Conversely, local_only metadata must never invent a prepare action when the
// targeted predicate refuses it.
outcome = await render({
    response: { status: 'ok', source: {
        ...baseSource,
        state: 'unsafe', source_initializable: false,
        reason_code: 'unsafe', message: 'Unsafe source.', state_token: '2'.repeat(64),
    } },
});
assert.equal(outcome.calls, 1);
assert.doesNotMatch(outcome.html, /Prepare for Project Mesh|Resume preparation/);
assert.match(outcome.html, /Unsafe source/);

// `preparing` alone is insufficient: the authoritative resumable flag must be
// true before Space Detail offers the resume entry point.
outcome = await render({
    response: { status: 'ok', source: {
        ...baseSource, state: 'preparing', resumable: false, state_token: '3'.repeat(64),
    } },
});
assert.doesNotMatch(outcome.html, /Resume preparation/);
outcome = await render({
    response: { status: 'ok', source: {
        ...baseSource, state: 'preparing', resumable: true, state_token: '4'.repeat(64),
    } },
});
assert.match(outcome.html, /Resume preparation/);
assert.match(outcome.html, /data-mesh-source-action="resume"/);

// Neither insufficient privilege nor an unavailable Mesh capability may issue
// the targeted admin request.
outcome = await render({
    permissions: ['manage'], available: true,
    response: { status: 'ok', source: { ...baseSource } },
});
assert.equal(outcome.calls, 0);
assert.equal(outcome.html, '');
outcome = await render({
    permissions: ['admin'], available: false,
    response: { status: 'ok', source: { ...baseSource } },
});
assert.equal(outcome.calls, 0);
assert.equal(outcome.html, '');

// The global Mesh capability probe is asynchronous at boot. Its one-shot
// availability event starts exactly one targeted read for an already-rendered
// admin Space Detail.
const eventStart = meshCalls.length;
meshAvailable = true;
meshResponse = { status: 'ok', source: { ...baseSource, state_token: '5'.repeat(64) } };
documentHandlers.get('admin:mesh-availability')({ detail: { available: true } });
await settle();
assert.equal(meshCalls.length - eventStart, 1);
assert.match(elements.sdMeshReadiness.innerHTML, /Prepare for Project Mesh/);

// A late response after navigation must have no DOM side effect.
let resolveLate;
meshAvailable = true;
context.meshAdminSourceReadiness = spaceId => {
    meshCalls.push(spaceId);
    return new Promise(resolve => { resolveLate = resolve; });
};
context.AdminRouter.epoch += 1;
elements.sdMeshReadiness.innerHTML = '';
context.__spaceDetail.render(node(), { spaceId: 'demo-space', tier: 'short' }, {
    epoch: context.AdminRouter.epoch,
    identity: { permissions: ['admin'] },
});
await settle();
context.AdminRouter.epoch += 1;
resolveLate({ status: 'ok', source: { ...baseSource, state_token: '6'.repeat(64) } });
await settle();
assert.doesNotMatch(elements.sdMeshReadiness.innerHTML, /Prepare for Project Mesh|Resume preparation/);

console.log('admin space detail mesh readiness runtime: ok');
