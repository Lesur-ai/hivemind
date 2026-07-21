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

function node() {
    const attributes = {};
    return {
        attributes,
        innerHTML: '',
        setAttribute(name, value) { attributes[name] = String(value); },
    };
}

const action = 'Inspect <demo/> exactly & do not delete any residue.';
const partial = {
    status: 'partial',
    space_id: 'demo',
    recovery_required: true,
    message: 'Payload deletion could not be confirmed.',
    files_total: 7,
    files_deleted: 3,
    failed_keys: ['demo/live/a.md', 'demo/<unsafe>&.md'],
    marker_preserved: false,
    recovery: { retry_safe: false, action },
};
const summary = node();
const calls = [];
const toasts = [];
const navigations = [];
let modal = null;
let modalOpen = false;

const context = {
    console,
    esc: escapeHtml,
    icon: () => '',
    document: {
        addEventListener() {},
        querySelector(selector) {
            return selector === '#adminModal .destructive-summary' ? summary : null;
        },
    },
    registerAction() {},
    AdminViews: { register() {} },
    AdminRouter: {
        epoch: 11,
        go(route) { navigations.push(route); },
    },
    showDestructiveModal(options) {
        modal = options;
        modalOpen = true;
    },
    callTool: async (tool, args) => {
        calls.push({ tool, args });
        return partial;
    },
    showToast(kind, message) { toasts.push({ kind, message }); },
};
vm.createContext(context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('space-detail', render);",
    'globalThis.__spaceDelete = { confirmSpaceDelete, setCurrentView: value => { currentView = value; } };',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.ok(context.__spaceDelete, 'space delete instrumentation failed');

const view = {
    ctx: { epoch: 11, identity: { permissions: ['read', 'write', 'manage'] } },
    info: { hive_status_label: 'hivemind_healthy' },
    spaceId: 'demo',
};
context.__spaceDelete.setCurrentView(view);
context.__spaceDelete.confirmSpaceDelete(view);

assert.ok(modal);
assert.equal(modalOpen, true);
assert.equal(modal.typedConfirmation, 'demo');
assert.ok(modal.bodyHtml.includes('Normal deletion is refused by the server.'));
assert.ok(modal.bodyHtml.includes('Advanced unsafe recovery is MCP-only'));
assert.equal(modal.bodyHtml.includes('NOT route-gated'), false);
assert.equal(modal.bodyHtml.includes('Quiescence required before deletion'), false);
assert.ok(modal.bodyHtml.includes('Access grants for this space remain on tokens'));
assert.equal(modal.bodyHtml.includes('notes, consolidation, graph operations, restore/GC, and Hivemind activity'), false);
assert.equal(modal.bodyHtml.includes('the lifecycle lock is not a universal barrier'), false);

const outcome = await modal.onConfirm();
if (outcome) modalOpen = false;

assert.equal(outcome, false, 'partial must keep the destructive modal open');
assert.equal(modalOpen, true);
assert.equal(calls.length, 1, 'partial must never auto-retry');
assert.equal(calls[0].tool, 'space_delete');
assert.equal(calls[0].args.space_id, 'demo');
assert.equal(calls[0].args.confirm, true);
assert.equal(Object.hasOwn(calls[0].args, 'unsafe_recovery'), false);
assert.equal(summary.attributes['data-recovery-required'], 'true');
assert.ok(summary.innerHTML.includes('not successful'));
assert.ok(summary.innerHTML.includes('files_total'));
assert.ok(summary.innerHTML.includes('>7<'));
assert.ok(summary.innerHTML.includes('files_deleted'));
assert.ok(summary.innerHTML.includes('>3<'));
assert.ok(summary.innerHTML.includes('marker_preserved'));
assert.ok(summary.innerHTML.includes('>false<'));
assert.ok(summary.innerHTML.includes('recovery.retry_safe'));
assert.ok(summary.innerHTML.includes(escapeHtml(action)));
assert.ok(summary.innerHTML.includes(escapeHtml(partial.failed_keys[0])));
assert.ok(summary.innerHTML.includes(escapeHtml(partial.failed_keys[1])));
assert.deepEqual(toasts, []);
assert.deepEqual(navigations, []);

console.log('admin space delete recovery runtime: ok');
