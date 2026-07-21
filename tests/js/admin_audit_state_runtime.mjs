import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'views-audit.js path is required');

const source = fs.readFileSync(viewPath, 'utf8');
const stateDeclaration = 'const viewState = new WeakMap();';
assert.equal(source.split(stateDeclaration).length - 1, 1);
const instrumentedSource = source.replace(
    stateDeclaration,
    'const viewState = globalThis.__auditViewState = new WeakMap();',
);

const successfulResponse = Object.freeze({
    status: 'ok',
    entries: [{
        ts: '2026-07-13T14:52:03Z',
        event: 'admin_tool_call',
        tool: 'admin_audit_recent',
        arguments_keys: ['limit'],
        client: 'console-admin',
        auth_type: 'stored',
    }],
    total: 1,
    capacity: 500,
    scope_note: 'local scope',
});

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function createHarness() {
    let registeredRender = null;
    const responses = [];
    const calls = [];
    const handlers = {};
    const resultSlot = { innerHTML: '' };
    const refreshButton = { disabled: false };

    const root = {
        isConnected: true,
        querySelector(selector) {
            if (selector === '[data-audit-results]') return resultSlot;
            if (selector === '[data-audit-action="refresh"]') return refreshButton;
            return null;
        },
        addEventListener(type, handler) {
            handlers[type] = handler;
        },
        contains() {
            return true;
        },
    };

    const contentEl = {
        innerHTML: '',
        querySelector(selector) {
            return selector === '.audit-view' ? root : null;
        },
    };

    const context = {
        console,
        AdminRouter: { epoch: 1 },
        AdminViews: {
            register(name, render) {
                assert.equal(name, 'audit');
                registeredRender = render;
            },
        },
        callTool: async (tool, args) => {
            calls.push({ tool, args });
            assert.ok(responses.length > 0, 'unexpected audit request');
            return await responses.shift();
        },
        dataTable: (_headers, rows) => `TABLE:${rows}`,
        esc: escapeHtml,
        icon: () => '',
        pageHeader: (title, actions = '') => `HEADER:${title}:${actions}`,
        panel: body => `PANEL:${body}`,
        pill: (_kind, label) => `PILL:${label}`,
        renderTimestamp: value => `TIME:${value}`,
        stateEmpty: options => `EMPTY:${options.title}`,
        stateError: options => `ERROR:${options.message || options.title}`,
        stateLoading: () => 'LOADING',
        stateUnavailable: reason => `UNAVAILABLE:${reason}`,
    };
    vm.createContext(context);
    vm.runInContext(instrumentedSource, context, { filename: viewPath });
    assert.equal(typeof registeredRender, 'function');

    return {
        calls,
        enqueue(response) {
            responses.push(response);
        },
        renderAdmin() {
            registeredRender(contentEl, {}, {
                epoch: 1,
                identity: {
                    client_name: 'console-admin',
                    permissions: ['read', 'write', 'admin'],
                },
            });
        },
        clickRefresh() {
            const target = {
                closest(selector) {
                    return selector === '[data-audit-action="refresh"]'
                        ? refreshButton
                        : null;
                },
            };
            handlers.click({ target, preventDefault() {} });
        },
        inputFilter(value) {
            const input = {
                value,
                dataset: { auditFilter: 'client' },
                closest(selector) {
                    return selector === '[data-audit-filter]' ? this : null;
                },
            };
            handlers.input({ target: input });
        },
        html() {
            return resultSlot.innerHTML;
        },
        state() {
            return context.__auditViewState.get(root);
        },
    };
}

async function flushAsyncWork() {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
}

async function successThenFailureThenFilter() {
    const harness = createHarness();
    harness.enqueue(successfulResponse);
    harness.renderAdmin();
    await flushAsyncWork();
    assert.match(harness.html(), /TABLE:/);

    harness.enqueue({ status: 'error', message: 'backend unavailable' });
    harness.clickRefresh();
    await flushAsyncWork();
    assert.equal(harness.html(), 'ERROR:backend unavailable');
    assert.deepEqual(Array.from(harness.state().entries), []);
    assert.equal(harness.state().phase, 'error');
    assert.equal(harness.state().total, '—');
    assert.equal(harness.state().capacity, '—');
    assert.equal(harness.state().scopeNote, '');

    const failedHtml = harness.html();
    harness.inputFilter('does-not-match-old-data');
    assert.equal(harness.html(), failedHtml);
}

async function successThenPendingThenFilter() {
    const harness = createHarness();
    harness.enqueue(successfulResponse);
    harness.renderAdmin();
    await flushAsyncWork();
    assert.match(harness.html(), /TABLE:/);

    let finishPending;
    const pendingResponse = new Promise(resolve => {
        finishPending = resolve;
    });
    harness.enqueue(pendingResponse);
    harness.clickRefresh();
    assert.equal(harness.html(), 'LOADING');
    assert.equal(harness.state().phase, 'loading');

    harness.inputFilter('does-not-match-old-data');
    assert.equal(harness.html(), 'LOADING');

    finishPending({ status: 'error', message: 'eventual failure' });
    await flushAsyncWork();
    assert.equal(harness.html(), 'ERROR:eventual failure');
    assert.deepEqual(Array.from(harness.state().entries), []);
    assert.equal(harness.state().phase, 'error');
}

async function initialFailureThenFilter() {
    const harness = createHarness();
    harness.enqueue({ status: 'error', message: 'initial failure' });
    harness.renderAdmin();
    await flushAsyncWork();
    assert.equal(harness.html(), 'ERROR:initial failure');

    const failedHtml = harness.html();
    harness.inputFilter('anything');
    assert.equal(harness.html(), failedHtml);
    assert.doesNotMatch(harness.html(), /No events recorded/);
}

await successThenFailureThenFilter();
await successThenPendingThenFilter();
await initialFailureThenFilter();
console.log('admin audit state runtime: ok');
