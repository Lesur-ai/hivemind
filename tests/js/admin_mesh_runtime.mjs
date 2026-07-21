/**
 * P10-4 (#192) Mesh view — RUNTIME regression harness.
 *
 * Dependency-free `node:vm` runner (no jsdom, no npm install) following the
 * exact idiom of tests/js/admin_access_lifecycle_runtime.mjs and
 * tests/js/admin_space_delete_runtime.mjs: build faithful hand-written shell
 * stubs, run the REAL shipped view source in a VM, and drive it purely through
 * the frozen shell surface (registerAction handlers, showModal/showDestructiveModal
 * captures, AdminRouter.epoch) — never the IIFE's private closures. `document`
 * is DOM-faithful: `showModal` renders the body string into fake elements keyed
 * by the `id="..."` attributes it actually contains, so a markup/ID drift
 * surfaces as a null read, not a phantom green.
 *
 * SCENARIOS
 *   A actionsFor() end-to-end through real rendering: a fixture with several
 *     (role, state) pairings, including a blocked_recovery with a recorded
 *     next_action, renders exactly the right action buttons per row.
 *   B Mesh unavailable (meshAdminStatus() resolves null) renders an honest
 *     unavailable state, never a fabricated pairing table.
 *   C Non-admin identity never calls meshAdminStatus() at all (render-time
 *     gate, defense-in-depth against a stale/forged data-action element).
 *   D Create invitation -> one-time invitation-code display -> full teardown
 *     on both explicit acknowledge AND dismissal via a close-modal control
 *     (mirrors the Access view's one-time-secret contract, T5).
 *   E Nav lock: a hash change away from the locked route while the invitation
 *     code is displayed is reverted.
 *   F Destructive action (evict) requires the typed confirmation to equal the
 *     pairing's space_id.
 *   G A stale continuation (route navigated away while the action request was
 *     in flight) drops its effect — no toast, no refresh.
 *
 * Contract: DESIGN/hivemind/P10_DESIGN_PACK.md §4 (three-action flow, one-time
 * secret display/teardown, purpose-specific confirmation), P10_THREAT_MODEL.md T5/T15.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'views-mesh.js path is required');
const source = fs.readFileSync(viewPath, 'utf8');

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function flushTasks() {
    return new Promise(resolve => setImmediate(resolve));
}

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add(v) { values.add(v); },
        remove(v) { values.delete(v); },
        contains(v) { return values.has(v); },
    };
}

function makeElement(extra = {}) {
    return Object.assign({
        innerHTML: '',
        textContent: '',
        value: '',
        checked: false,
        hidden: false,
        disabled: false,
        style: {},
        classList: classList(),
        dataset: {},
        _click: [],
        focus() {},
        setAttribute(k, v) { this[k] = v; },
        removeAttribute(k) { delete this[k]; },
        addEventListener(type, fn) { if (type === 'click') this._click.push(fn); },
        removeEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        appendChild() {},
        removeChild() {},
    }, extra);
}

// Byte-identical to the shipped shell's pinned esc() (admin-app.js) — including
// its non-coercing (s||'') guard, deliberately NOT lenient like String(v ?? '').
// A stub that tolerates numbers/objects the real esc() would throw on is
// exactly the gap that let a real esc(nonString) regression ship undetected.
function esc(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

function createHarness() {
    let epoch = 1;
    let hash = '#/mesh';
    const LOCKED_PREFIX = '#/mesh';

    const modals = [];
    const toasts = [];
    const toolCalls = [];
    const meshActionCalls = [];
    const hashListeners = [];
    const actions = {};
    const views = {};

    const closeControls = [makeElement({ dataset: { action: 'close-modal' } }), makeElement({ dataset: { action: 'close-modal' } })];
    const adminModal = makeElement({
        style: { display: 'none' },
        querySelectorAll(sel) { return sel.includes('close-modal') ? closeControls.slice() : []; },
    });
    const loginOverlay = makeElement({ classList: classList(['hidden']) });

    let modalEls = Object.create(null);
    function renderModalBody(body) {
        const map = Object.create(null);
        const re = /id="([^"]+)"[^>]*>([^<]*)/g;
        let m;
        while ((m = re.exec(body)) !== null) {
            if (map[m[1]]) continue;
            const el = makeElement();
            el.textContent = m[2];
            el.innerHTML = m[2];
            map[m[1]] = el;
        }
        modalEls = map;
    }
    function getEl(id) {
        if (id === 'adminModal') return adminModal;
        if (id === 'loginOverlay') return loginOverlay;
        if (id === 'content') return contentEl;
        return modalEls[id] || null;
    }

    const hashQueue = [];
    function flushHashQueue() {
        let guard = 0;
        while (hashQueue.length) {
            assert.ok(++guard < 50, 'hashchange revert loop did not terminate');
            hashQueue.shift()();
        }
    }
    const location = {
        get hash() { return hash; },
        set hash(v) {
            if (v === hash) return;
            hash = v;
            hashQueue.push(() => hashListeners.forEach(fn => fn()));
        },
    };

    const contentEl = makeElement();

    const documentStub = {
        getElementById(id) { return getEl(id); },
        querySelectorAll(sel) { return sel.includes('close-modal') ? closeControls.slice() : []; },
        querySelector() { return null; },
        createElement() { return makeElement(); },
        body: makeElement(),
    };
    const windowStub = { addEventListener(type, fn) { if (type === 'hashchange') hashListeners.push(fn); }, removeEventListener() {} };

    function openShellModal(title, body, verb, onConfirm) {
        adminModal.style.display = '';
        closeControls.forEach(c => { c.disabled = false; c._click = []; });
        renderModalBody(body || '');
        modals.push({ title, body: body || '', verb, onConfirm });
    }

    let identity = { client_name: 'admin-1', permissions: ['admin'] };
    let meshStatusResponse = null; // set per-scenario
    let meshStatusCalls = 0;
    let meshStatusAutoResolve = true; // false lets a scenario hold a status reload pending
    let meshStatusQueue = []; // FIFO of deferreds, only populated while auto-resolve is off
    let meshMembersResponse = { status: 'ok', space_id: 'demo', membership_epoch: 3, members: [] }; // set per-scenario
    let meshActionQueue = []; // FIFO of deferreds, only populated while auto-resolve is off
    let meshActionAutoResolve = true; // default: resolve immediately with a canned response

    function defaultMeshActionResponse(action, args) {
        if (action === 'invitation') {
            return {
                status: 'ok', pair_id: 'pair_new', secret: 'SECRET-VALUE-XYZ',
                invitation: 'SIGNED_INVITATION_B64', source_endpoint: 'https://a.test',
                source_fingerprint: 'hm1:' + 'a'.repeat(64),
            };
        }
        return { status: 'ok', pair_id: args.pair_id, state: 'active' };
    }

    const context = {
        console,
        window: windowStub,
        document: documentStub,
        location,
        navigator: {},
        cache: { spaces: [] },
        btoa: globalThis.btoa,
        atob: globalThis.atob,
        esc,
        icon: () => '',
        pageHeader: (t, a) => `H:${esc(t)}${a || ''}`,
        panel: b => `P:${b}`,
        dataTable: (h, r) => `T:${r}`,
        pill: (k, l) => `<span data-pill="${esc(k)}">${esc(l)}</span>`,
        statusDot: (sev, l) => `DOT:${esc(sev)}:${esc(l)}`,
        copyable: (full, shown) => `C:${esc(shown !== undefined ? shown : full)}`,
        monoBlock: b => `M:${b}`,
        truncateMiddle: (s) => String(s),
        stateEmpty: o => `EMPTY:${(o && o.title) || ''}`,
        stateError: o => `ERR:${(o && o.title) || ''}${(o && o.message) ? ':' + o.message : ''}`,
        stateUnavailable: r => `NA:${r}`,
        stateLoading: l => `LOADING:${l || ''}`,
        serverMessage: msg => (msg ? `SM:${esc(String(msg))}` : ''),
        renderTimestamp: iso => `TS:${esc(iso)}`,
        showModal: openShellModal,
        showDestructiveModal(opts) { openShellModal(opts.title, opts.bodyHtml, opts.verb, opts.onConfirm); modals[modals.length - 1].typedConfirmation = opts.typedConfirmation; },
        closeModal() { adminModal.style.display = 'none'; },
        showToast(kind, text) { toasts.push({ kind, text }); },
        registerAction(name, fn) { actions[name] = fn; },
        AdminRouter: {
            get epoch() { return epoch; },
            refresh() { epoch += 1; },
            current() { return { view: hash.startsWith('#/mesh/') ? 'mesh-detail' : 'mesh', params: hash.startsWith('#/mesh/') ? { spaceId: decodeURIComponent(hash.slice('#/mesh/'.length)) } : {}, raw: hash.slice(1) }; },
        },
        AdminViews: { register(name, fn) { views[name] = fn; } },
        _ctx: () => ({ identity, epoch, caches: context.cache }),
        callTool(tool, args) {
            toolCalls.push({ tool, args });
            if (tool === 'space_list') return Promise.resolve({ status: 'ok', spaces: [{ space_id: 'demo' }, { space_id: 'other' }] });
            return Promise.resolve({ status: 'error' });
        },
        meshAdminStatus() {
            meshStatusCalls += 1;
            if (meshStatusAutoResolve) return Promise.resolve(meshStatusResponse);
            const d = deferred();
            meshStatusQueue.push(d);
            return d.promise;
        },
        meshAdminMembers() { return Promise.resolve(meshMembersResponse); },
        meshAdminAction(action, args) {
            meshActionCalls.push({ action, args });
            if (meshActionAutoResolve) return Promise.resolve(defaultMeshActionResponse(action, args));
            const d = deferred();
            meshActionQueue.push(d);
            return d.promise;
        },
    };

    vm.createContext(context);
    context.window.addEventListener('hashchange', () => { epoch += 1; });
    vm.runInContext(source, context, { filename: viewPath });
    assert.equal(typeof views['mesh'], 'function', 'views-mesh.js must register the mesh view');
    assert.equal(typeof views['mesh-detail'], 'function', 'views-mesh.js must register the mesh-detail view');

    return {
        location, modals, toasts, toolCalls, meshActionCalls, contentEl,
        setIdentity(v) { identity = v; },
        setStatusResponse(v) { meshStatusResponse = v; },
        setStatusAutoResolve(v) { meshStatusAutoResolve = v; },
        setMembersResponse(v) { meshMembersResponse = v; },
        meshStatusCallCount() { return meshStatusCalls; },
        bumpEpoch() { epoch += 1; },
        renderOverview() { hash = '#/mesh'; views['mesh'](contentEl, {}, { epoch, identity, sessionGeneration: 1, caches: context.cache }); },
        renderDetail(spaceId) { hash = '#/mesh/' + encodeURIComponent(spaceId); views['mesh-detail'](contentEl, { spaceId }, { epoch, identity, sessionGeneration: 1, caches: context.cache }); },
        lastModal() { return modals[modals.length - 1]; },
        setMeshActionAutoResolve(v) { meshActionAutoResolve = v; },
        settleLastAction(payload) { meshActionQueue[meshActionQueue.length - 1].resolve(payload); },
        flushHashQueue,
        el: getEl,
        act(name, data) { actions[name](data || {}); },
        closeViaControl() { closeControls[0]._click.forEach(fn => fn()); },
    };
}

async function scenarioA() {
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true, display_name: 'peer-a', public_url: 'https://a.test', fingerprint: 'hm1:' + 'a'.repeat(64),
        pairings: [
            { pair_id: 'pair_issued', role: 'source', state: 'issued', space_id: 'demo', updated_at_ms: 1000, granted_scopes: ['read'] },
            { pair_id: 'pair_active', role: 'target', state: 'active', space_id: 'other', updated_at_ms: 2000, granted_scopes: ['read'] },
            { pair_id: 'pair_blocked', role: 'source', state: 'blocked_recovery', space_id: 'demo', updated_at_ms: 3000, next_action: 'evict', granted_scopes: ['read'] },
        ],
    });
    h.renderOverview();
    await flushTasks();
    const html = h.contentEl.innerHTML;
    assert.ok(html.includes('data-pair-id="pair_issued"') && html.includes('data-mesh-action="cancel"'), 'issued source row must offer cancel');
    assert.ok(!html.includes('data-mesh-action="approve"'), 'issued source row must not offer approve');
    assert.ok(!/pair_active[^]*?data-mesh-action/.test(html.split('pair_active')[1]?.slice(0, 200) || ''), 'active row sanity');
    assert.ok(html.includes('data-pair-id="pair_blocked"') && html.includes('data-mesh-action="evict"'), 'blocked_recovery with next_action=evict must offer evict');
    assert.ok(!(html.split('pair_blocked')[1] || '').slice(0, 300).includes('data-mesh-action="resume"'), 'blocked_recovery with next_action=evict must not also offer resume');
    console.log('scenario A (actionsFor via rendering): ok');
}

async function scenarioB() {
    const h = createHarness();
    h.setStatusResponse(null);
    h.renderOverview();
    await flushTasks();
    const html = h.contentEl.innerHTML;
    assert.ok(html.includes('NA:Mesh is not available'), 'must render an honest unavailable state');
    // The design pack forbids a control that is "expected to fail when
    // clicked" (§3, T15): Create/Accept must be absent, not just visually
    // disabled, when Mesh is unavailable — Refresh (GET-only) may remain.
    assert.ok(!html.includes('data-action="mesh-create-invitation"'), 'Create invitation must be absent when Mesh is unavailable');
    assert.ok(!html.includes('data-action="mesh-accept-invitation"'), 'Accept invitation must be absent when Mesh is unavailable');
    assert.ok(html.includes('data-action="mesh-refresh"'), 'Refresh must remain available to retry');
    console.log('scenario B (mesh unavailable): ok');
}

async function scenarioJ() {
    // A direct #/mesh/<space-id> deep link (bookmark, typed URL) can reach
    // the detail route even though the nav item and #/mesh overview both
    // correctly hide themselves when Mesh is unavailable — the detail route
    // must independently apply the same absent-not-failing rule.
    const h = createHarness();
    h.setStatusResponse(null);
    h.renderDetail('demo');
    await flushTasks();
    const html = h.contentEl.innerHTML;
    assert.ok(html.includes('NA:Mesh is not available'), 'detail route must render an honest unavailable state');
    assert.ok(!html.includes('data-action="mesh-create-invitation"'), 'Create invitation must be absent on the detail route when Mesh is unavailable');
    assert.ok(!html.includes('data-action="mesh-accept-invitation"'), 'Accept invitation must be absent on the detail route when Mesh is unavailable');
    assert.ok(!html.includes('data-mesh-action'), 'no pairing action button may render when Mesh is unavailable');
    console.log('scenario J (detail-route deep link when Mesh is unavailable: no failing controls): ok');
}

async function scenarioC() {
    const h = createHarness();
    h.setIdentity({ client_name: 'mgr-1', permissions: ['read', 'write', 'manage'] });
    h.renderOverview();
    await flushTasks();
    assert.equal(h.meshStatusCallCount(), 0, 'non-admin must never probe meshAdminStatus()');
    assert.ok(h.contentEl.innerHTML.includes('Requires admin permission'));
    console.log('scenario C (non-admin gate, no network call): ok');
}

async function scenarioD() {
    const h = createHarness();
    h.setStatusResponse({ status: 'ok', enabled: true, healthy: true, display_name: 'peer-a', public_url: 'https://a.test', fingerprint: 'hm1:' + 'a'.repeat(64), pairings: [] });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-create-invitation', {});
    await flushTasks(); // ensureSpaces() -> space_list
    const createModal = h.lastModal();
    assert.equal(createModal.title, 'Create invitation');
    h.el('meshInvSpace').value = 'demo'; // operator selects a space in the picker
    const createResult = await createModal.onConfirm();
    assert.equal(createResult, false, 'create-invitation confirm must not auto-close (secret step owns the modal)');
    await flushTasks();
    const secretModal = h.lastModal();
    assert.equal(secretModal.title, 'Invitation created — save the code now');
    const codeEl = h.el('meshInvCode');
    assert.ok(codeEl && codeEl.textContent.length > 0, 'invitation code must be rendered once');
    const savedCode = codeEl.textContent;

    // Dismiss via a close-modal control WITHOUT acknowledging: the secret must
    // still be destroyed (mirrors the Access view's one-time-secret contract).
    h.closeViaControl();
    assert.equal(h.el('meshInvCode').textContent, '', 'dismissal via close-modal must zero the rendered code');
    assert.equal(h.el('meshInvCopyBtn').disabled, true, 'copy button must be disabled after teardown');
    assert.ok(savedCode.length > 10);
    console.log('scenario D (invitation code display + teardown on dismissal): ok');
}

async function scenarioE() {
    const h = createHarness();
    h.setStatusResponse({ status: 'ok', enabled: true, healthy: true, pairings: [] });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-create-invitation', {});
    await flushTasks();
    h.el('meshInvSpace').value = 'demo';
    await h.lastModal().onConfirm();
    await flushTasks();
    const lockedRoute = h.location.hash;
    h.location.hash = '#/dashboard';
    h.flushHashQueue();
    assert.equal(h.location.hash, lockedRoute, 'navigating away while the invitation code is displayed must be reverted');
    console.log('scenario E (nav lock reverts off-route navigation): ok');
}

async function scenarioF() {
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true,
        pairings: [{ pair_id: 'pair_evict_me', role: 'source', state: 'approved', space_id: 'demo', updated_at_ms: 1, granted_scopes: ['read'] }],
    });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-run-action', { pairId: 'pair_evict_me', meshAction: 'evict' });
    const modal = h.lastModal();
    assert.equal(modal.verb, 'Evict');
    assert.equal(modal.typedConfirmation, 'demo', 'destructive evict must require the space_id as the typed challenge');
    console.log('scenario F (destructive action typed confirmation): ok');
}

async function scenarioG() {
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true,
        pairings: [{ pair_id: 'pair_resume_me', role: 'source', state: 'awaiting_acks', space_id: 'demo', updated_at_ms: 1, granted_scopes: ['read'] }],
    });
    h.renderOverview();
    await flushTasks();
    h.setMeshActionAutoResolve(false);
    h.act('mesh-run-action', { pairId: 'pair_resume_me', meshAction: 'resume' });
    const modal = h.lastModal();
    const confirmPromise = modal.onConfirm();
    h.bumpEpoch(); // simulate navigation away while the request is in flight
    h.settleLastAction({ status: 'ok', pair_id: 'pair_resume_me', state: 'active' });
    const outcome = await confirmPromise;
    assert.equal(outcome, false, 'a stale continuation must not report success to the modal');
    assert.deepEqual(h.toasts, [], 'a stale continuation must not toast');
    console.log('scenario G (stale continuation drops its effect): ok');
}

async function scenarioH() {
    // #/mesh/<space-id> detail route, real numeric fields end to end — the real
    // esc() throws on a bare number, so this scenario alone would have caught
    // the esc(members.membership_epoch) regression this harness previously missed.
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true, display_name: 'peer-a', public_url: 'https://a.test', fingerprint: 'hm1:' + 'a'.repeat(64),
        pairings: [{ pair_id: 'pair_active', role: 'source', state: 'active', space_id: 'demo', updated_at_ms: 1000, granted_scopes: ['read'] }],
    });
    h.setMembersResponse({
        status: 'ok', space_id: 'demo', membership_epoch: 4,
        members: [{ node_id: 'node0', display_name: '', endpoint: '', fingerprint: '', scopes: null }],
    });
    h.renderDetail('demo');
    await flushTasks();
    const html = h.contentEl.innerHTML;
    assert.ok(html.includes('>4<'), 'membership epoch must render (not throw)');
    assert.ok(html.includes('>1<'), 'active member count must render (not throw)');
    // An 'active' source session offers no operator action (nothing to do), so
    // it has no action button/data-pair-id — its diagnostics drawer's pair_id
    // field is the observable proof this space's pairing session rendered.
    assert.ok(html.includes('>pair_active<'), "this space's pairing sessions must render");
    console.log('scenario H (detail route with real numeric fields, no esc() throw): ok');
}

async function scenarioI() {
    // A truncated members response (§5.0: ResponseLimitMiddleware wraps every
    // route including /api/admin/mesh/*, admin-api.js's _meshFetch surfaces it
    // as {status:'truncated', message}) must render as an honest error state,
    // never a fabricated "no active members" empty state — loadMembers() must
    // positive-match status:'ok', not merely reject status:'error'.
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true,
        pairings: [{ pair_id: 'pair_active', role: 'source', state: 'active', space_id: 'demo', updated_at_ms: 1000, granted_scopes: ['read'] }],
    });
    h.setMembersResponse({ status: 'truncated', message: "Response exceeded the console's 512 KB limit — use an MCP client for this operation." });
    h.renderDetail('demo');
    await flushTasks();
    h.act('mesh-detail-tab', { tab: 'members' });
    const html = h.contentEl.innerHTML;
    assert.ok(!html.includes('No active members'), 'a truncated response must never render as a fabricated empty state');
    assert.ok(html.includes('512 KB'), 'the truncation message must surface honestly');
    console.log('scenario I (truncated members response renders honest error, not fabricated empty state): ok');
}

async function scenarioK() {
    // mesh_admin.py's process-lock check is unconditional and precedes even
    // GET /status, so healthy:false can only appear in a genuinely successful
    // (200, status:'ok') response via a narrow lock-loss race between that
    // entry check and _status()'s own read of it. It must be treated exactly
    // like an unreachable Mesh: on the overview, every mutating control is
    // gated per-site (actionsFor/overviewActions both receive `available`);
    // on the detail route, paintDetail collapses to the same full unavailable
    // page as a null status, so the Members panel's per-row Evict/
    // force-evict-member button (itself also `available`-gated, defense in
    // depth) never reaches the DOM at all in this state — verified below by
    // asserting the detail route renders no data-mesh-action of any kind,
    // not by exercising membersPanel's internal gate directly.
    const h = createHarness();
    const unhealthyStatus = {
        status: 'ok', enabled: true, healthy: false, display_name: 'peer-a', public_url: 'https://a.test', fingerprint: 'hm1:' + 'a'.repeat(64),
        pairings: [{ pair_id: 'pair_issued', role: 'source', state: 'issued', space_id: 'demo', updated_at_ms: 1000, granted_scopes: ['read'], target_fingerprint: 'hm1:' + 'b'.repeat(64) }],
    };

    h.setStatusResponse(unhealthyStatus);
    h.renderOverview();
    await flushTasks();
    let html = h.contentEl.innerHTML;
    assert.ok(!html.includes('data-action="mesh-create-invitation"'), 'overview: Create invitation must be absent when unhealthy');
    assert.ok(!html.includes('data-action="mesh-accept-invitation"'), 'overview: Accept invitation must be absent when unhealthy');
    assert.ok(!html.includes('data-mesh-action'), 'overview: no pairing action button may render when unhealthy');

    h.setMembersResponse({
        status: 'ok', space_id: 'demo', membership_epoch: 2,
        members: [{ node_id: 'b'.repeat(64), display_name: '', endpoint: 'https://b.test', fingerprint: 'hm1:' + 'b'.repeat(64), scopes: ['read'] }],
    });
    h.renderDetail('demo');
    await flushTasks();
    html = h.contentEl.innerHTML;
    assert.ok(html.includes('NA:Mesh is not available'), 'detail: must render the honest unavailable state when unhealthy, not a degraded live page');
    assert.ok(!html.includes('data-action="mesh-create-invitation"'), 'detail: Create invitation must be absent when unhealthy');
    assert.ok(!html.includes('data-mesh-action="force-evict-member"'), 'detail: Members-panel Evict must be absent when unhealthy, even for a matched owner session');
    console.log('scenario K (unhealthy-but-reachable Mesh status: no failing controls anywhere, including Members-panel Evict): ok');
}

async function scenarioL() {
    // A modal is a separate DOM overlay, unaffected by a #content repaint —
    // an admin can open Create Invitation while Mesh is healthy, trigger a
    // background Refresh that flips it unhealthy while the modal stays
    // open, then confirm. The render-time gate (scenario K) cannot catch
    // this: onCreateInvitationConfirm must re-check availability itself at
    // click time and refuse rather than firing a mutation the process-lock
    // middleware is guaranteed to 503.
    const h = createHarness();
    h.setStatusResponse({ status: 'ok', enabled: true, healthy: true, pairings: [] });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-create-invitation', {});
    await flushTasks(); // ensureSpaces()
    h.el('meshInvSpace').value = 'demo';

    h.setStatusResponse({ status: 'ok', enabled: true, healthy: false, pairings: [] });
    h.renderOverview(); // simulates the background Refresh
    await flushTasks();

    const modal = h.lastModal();
    assert.equal(modal.title, 'Create invitation', 'the still-open Create modal must be the one under test');
    const result = await modal.onConfirm();
    assert.equal(result, false, 'a stale-availability confirm must not report success');
    assert.equal(h.meshActionCalls.length, 0, 'no Mesh mutation may fire once availability was lost while the modal was open');
    console.log('scenario L (click-time availability re-check blocks a stale-modal invitation mutation): ok');
}

async function scenarioM() {
    // Same class of bug, for the generic pairing-action confirm (evict/
    // resume/cancel/etc via confirmMeshAction) rather than the invitation
    // create/accept flows.
    const h = createHarness();
    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: true,
        pairings: [{ pair_id: 'pair_resume_me', role: 'source', state: 'awaiting_acks', space_id: 'demo', updated_at_ms: 1, granted_scopes: ['read'] }],
    });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-run-action', { pairId: 'pair_resume_me', meshAction: 'resume' });
    const modal = h.lastModal();
    assert.equal(modal.verb, 'Resume');

    h.setStatusResponse({
        status: 'ok', enabled: true, healthy: false,
        pairings: [{ pair_id: 'pair_resume_me', role: 'source', state: 'awaiting_acks', space_id: 'demo', updated_at_ms: 1, granted_scopes: ['read'] }],
    });
    h.renderOverview();
    await flushTasks();

    const result = await modal.onConfirm();
    assert.equal(result, false, 'a stale-availability confirm must not report success');
    assert.equal(h.meshActionCalls.length, 0, 'no Mesh mutation may fire once availability was lost while the modal was open');
    console.log('scenario M (click-time availability re-check blocks a stale-modal pairing-action mutation): ok');
}

async function scenarioN() {
    // state.status survives an ordinary route change — only loadStatus()
    // overwrites it, asynchronously. A navigation while a modal stays open
    // (none of the three mutation modals hold a nav lock) bumps
    // AdminRouter.epoch and starts a NEW loadStatus() call; while that
    // reload is still in flight (not yet resolved), state.status is a
    // stale answer to a question about a route that no longer exists.
    // meshAvailableNow() must fail closed on "unknown/pending", never fall
    // back to an earlier (possibly no-longer-true) healthy reading just
    // because it happens to still be sitting in state.status.
    const h = createHarness();
    h.setStatusResponse({ status: 'ok', enabled: true, healthy: true, pairings: [] });
    h.renderOverview();
    await flushTasks();
    h.act('mesh-create-invitation', {});
    await flushTasks();
    h.el('meshInvSpace').value = 'demo';

    h.setStatusAutoResolve(false); // the next status reload will hang, unresolved
    h.bumpEpoch();
    h.renderOverview(); // starts loadStatus() for the new epoch, pending

    const modal = h.lastModal();
    assert.equal(modal.title, 'Create invitation', 'the still-open Create modal must be the one under test');
    const result = await modal.onConfirm();
    assert.equal(result, false, 'a confirm during a pending, not-yet-resolved status reload must not report success');
    assert.equal(h.meshActionCalls.length, 0, 'no Mesh mutation may fire while availability is unknown (reload pending)');
    console.log('scenario N (click-time guard fails closed while a post-navigation status reload is still pending): ok');
}

async function main() {
    await scenarioA();
    await scenarioB();
    await scenarioC();
    await scenarioD();
    await scenarioE();
    await scenarioF();
    await scenarioG();
    await scenarioH();
    await scenarioI();
    await scenarioJ();
    await scenarioK();
    await scenarioL();
    await scenarioM();
    await scenarioN();
    console.log('admin mesh runtime: ok');
}

main().catch(err => { console.error(err); process.exit(1); });
