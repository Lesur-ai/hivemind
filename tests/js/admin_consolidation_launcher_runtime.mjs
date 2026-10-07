import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('function openConsolidationLauncher(');
const end = source.indexOf('\nfunction ', start + 1);
assert.ok(start >= 0 && end > start, 'shared launcher is present');
const code = source.slice(start, end);
const escape = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function harness(permissions = ['write']) {
    const elements = {}, calls = [], submissions = [], toasts = [];
    let generation = 1, response = { status: 'running', job_id: 'j1', queue_position: 1 };
    const modal = { confirm: null, closed: 0, html: '', style: { display: 'none' } };
    elements.adminModal = modal;
    const ctx = {
        esc: escape, stateUnavailable: escape, serverMessage: value => `<p>${escape(value)}</p>`,
        showToast: (...args) => toasts.push(args),
        currentSessionGeneration: () => generation,
        sessionGenerationIsCurrent: value => value === generation,
        AdminRouter: { epoch: 1 },
        document: { getElementById: id => elements[id] || null },
        showModal(title, body, label, confirm) {
            modal.style.display = 'flex';
            for (const element of Object.values(elements)) element.isConnected = false;
            modal.html = body; modal.confirm = confirm;
            for (const id of ['portalConsolidationForm', 'portalConsolidationSpace', 'portalConsolidationScope', 'portalConsolidationSummary', 'portalConsolidationError', 'modalConfirmBtn']) {
                elements[id] = { isConnected: true, value: id.endsWith('Scope') ? 'mine' : id.endsWith('Space') ? 'demo' : '', innerHTML: '', textContent: label, disabled: false, addEventListener(name, handler) { this[name] = handler; } };
            }
        },
        closeModal() { modal.closed++; modal.style.display = 'none'; },
        callTool: async (tool, args) => { calls.push({ tool, args }); return typeof response === 'function' ? response() : response; },
    };
    vm.createContext(ctx); vm.runInContext(code, ctx);
    const view = { epoch: 1, sessionGeneration: 1, identity: { client_name: 'alice', permissions } };
    const open = options => ctx.openConsolidationLauncher({ spaces: [{ space_id: 'demo' }, { space_id: 'other' }], lanes: [], spaceId: 'demo', ctx: view, onSubmitted: result => submissions.push(result), ...options });
    return { ctx, view, elements, calls, submissions, toasts, modal, open, setGeneration: value => { generation = value; }, response: value => { response = value; } };
}

{
    const h = harness(); h.open();
    assert.match(h.modal.html, /My notes/); assert.match(h.modal.html, /All agents/);
    await h.modal.confirm();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].args.agent, 'alice', 'mine always sends a nonempty agent');
    assert.equal(h.calls[0].args.space_id, 'demo');
    assert.equal(h.submissions[0].job_id, 'j1');
    assert.equal(h.modal.closed, 1);
}
{
    const h = harness(['manage']); h.open({ lanes: [{ space_id: 'demo', running_job: { job_id: 'old' } }] });
    assert.equal(h.elements.modalConfirmBtn.textContent, 'Queue after current job');
    h.elements.portalConsolidationScope.value = 'all';
    const returned = { status: 'queued', job_id: 'coalesced', queue_position: 3 };
    h.response(returned); await h.modal.confirm();
    assert.equal(h.calls[0].args.agent, '', 'all scope is explicit');
    assert.equal(h.submissions[0], returned, 'returned server state stays authoritative');
}
for (const variant of ['permission', 'identity', 'space', 'scope']) {
    const h = harness(); h.open();
    if (variant === 'permission') h.elements.portalConsolidationScope.value = 'all';
    if (variant === 'identity') h.view.identity.client_name = '';
    if (variant === 'space') h.elements.portalConsolidationSpace.value = 'not-loaded';
    if (variant === 'scope') h.elements.portalConsolidationScope.value = 'invented';
    await h.modal.confirm(); assert.equal(h.calls.length, 0, variant + ' must fail before mutation');
}
{
    const h = harness(['read']); h.open();
    assert.equal(h.modal.confirm, undefined, 'read-only identity gets no mutation form');
}
{
    const h = harness(); h.open(); const pending = deferred(); h.response(() => pending.promise);
    const first = h.modal.confirm(), duplicate = h.modal.confirm();
    assert.equal(h.calls.length, 1, 'double submit does not duplicate mutation');
    pending.resolve({ status: 'running', job_id: 'j2' }); await Promise.all([first, duplicate]);
    assert.equal(h.submissions.length, 1);
}
for (const change of ['route', 'session', 'modal', 'close']) {
    const h = harness(); h.open(); const pending = deferred(); h.response(() => pending.promise);
    const call = h.modal.confirm();
    if (change === 'route') h.ctx.AdminRouter.epoch++;
    if (change === 'session') h.setGeneration(2);
    if (change === 'modal') h.open();
    if (change === 'close') h.ctx.closeModal();
    const closes = h.modal.closed;
    pending.resolve({ status: 'running', job_id: 'stale' }); await call;
    assert.equal(h.submissions.length, 0, change + ' drops stale callback');
    assert.equal(h.modal.closed, closes, change + ' cannot close another modal');
    assert.equal(h.toasts.length, ['close', 'modal'].includes(change) ? 1 : 0, change + ' success is signalled only to the same route and session');
}
for (const status of ['error', 'rate_limited', 'read_only', 'truncated']) {
    const h = harness(); h.open(); h.response({ status, message: '<unsafe>' });
    await h.modal.confirm();
    assert.equal(h.calls.length, 1); assert.equal(h.submissions.length, 0); assert.equal(h.modal.closed, 0);
    assert.match(h.modal.html, /&lt;unsafe&gt;/);
    assert.equal(h.modal.confirm, undefined, 'failed or unreadable outcome offers no one-click retry');
    assert.match(h.modal.html, /View jobs/);
}
{
    const h = harness(['manage']); h.open();
    h.elements.portalConsolidationScope.value = 'all';
    h.response(() => { throw new Error('Connection lost'); });
    const oldConfirm = h.modal.confirm;
    await oldConfirm();
    assert.match(h.modal.html, /outcome is unknown/);
    assert.match(h.modal.html, /href="#\/spaces\/demo\/consolidation"/);
    assert.equal(h.modal.confirm, undefined);
    await oldConfirm();
    assert.equal(h.calls.length, 1, 'old form cannot enqueue a duplicate after uncertain outcome');
}
console.log('admin consolidation launcher runtime: ok');
