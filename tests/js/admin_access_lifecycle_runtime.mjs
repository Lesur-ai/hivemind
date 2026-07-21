/**
 * P8-5 (#143) Access view — async-lifecycle RUNTIME regression harness.
 *
 * WHY THIS EXISTS
 * ---------------
 * tests/test_admin_ui_p8_5.py pins the Access view's async-lifecycle contract by
 * STATIC source inspection (import-light, §7.2.1). Those pins prove the *shape*
 * of the guards (ordering of statements, which signals a branch reads) but they
 * cannot prove the *behaviour* — that, when the deferred `admin_create_token`
 * promise actually resolves at a hostile moment, the one-time secret is or isn't
 * surfaced, the modal is or isn't re-enabled, the route is or isn't reverted.
 * The Terra R2–R4 reviews (commits 27d559e, 0a2fc0b, 140d054 on branch
 * claude/p8-5-implementation-3cb723, PR #158) verified those behaviours with
 * ad-hoc browser harnesses that were NEVER committed. This file commits them.
 *
 * DECISION: pytest suite via a subprocess `node` runner, NOT a separate JS test
 * target (documented in tests/test_admin_ui_p8_5.py). The repo has no
 * package.json / npm test surface; CI already runs `pytest tests` with Node 24
 * on PATH. Two sibling harnesses set the exact idiom this file follows:
 *   - tests/js/admin_session_generation_runtime.mjs (shell session ownership)
 *   - tests/js/admin_audit_state_runtime.mjs        (P8-6 audit view state)
 * All three are dependency-free `node:vm` runners (no jsdom, no npm install) that
 * load the REAL shipped view source with faithful hand-written shell stubs and
 * control async ordering with deferred promises. A thin pytest wrapper shells
 * `node <this> <views-access.js>` and asserts exit 0 + the terminal `ok` marker.
 *
 * HOW IT WORKS
 * ------------
 * views-access.js is a self-contained IIFE whose only outside surface is the
 * frozen shell globals it calls (showModal / registerAction / callTool / _ctx /
 * AdminRouter / window+location, …). We build that surface, run the real source
 * in a VM, and then drive the view purely through it — capturing the modal
 * onConfirm from showModal, the action handlers from registerAction, and the
 * pending tool promise from callTool. Assertions are BLACK-BOX: they observe
 * only what a browser would (location.hash after a nav attempt, the shell
 * close-controls' `.disabled`, and the modal DOM nodes the shell renders from
 * the view's body string) — never the IIFE's private _navLock / _modalGen. That
 * keeps the harness honest and makes each check mutation-proof: reverting the
 * corresponding guard in views-access.js flips exactly one scenario RED.
 *
 * DOM FIDELITY (Terra PR #167 review, [medium]). The fake `document` is NOT a
 * fabricate-anything stub. `showModal` RENDERS the view's body: it registers
 * exactly the element IDs the body actually contains (with their inner text) and
 * `getElementById` returns null for any other ID — so a markup/ID drift or a
 * body the shell fails to insert surfaces as a null, not a phantom green. The
 * one-time-secret assertion reads the rendered `#ctSecret` text node, not the
 * captured body argument. `logout()` models the frozen shell's wipeSession:
 * it removes the modal (`#adminModal` hidden AND its rendered body dropped)
 * WITHOUT running the view's teardown, so a held nav lock is genuinely orphaned.
 * The acknowledge in scenario C goes through `confirmModal()`, which models the
 * shell's `if (ok) closeModal()` contract, so it asserts the modal actually
 * hides (not just that the secret cleared).
 *
 * HASHCHANGE FIDELITY (Terra PR #167 review, [high]). hashchange is modeled as a
 * QUEUED browser task, not a synchronous call inside the `location.hash` setter.
 * navigate() drains the queue immediately (the realizable ordering — a revert
 * queued at nav time runs before any strictly-later network reply); scenario I
 * drives the async ordering explicitly. The ordering the finding warns about (a
 * network continuation slotting between a sync hash write and its own queued
 * dispatch) is excluded by the event loop — see scenario I's comment.
 *
 * SCENARIOS
 *   A off-route nav reverts while pending, stays put once logged out
 *   B stale cross-session failure leaves the newer modal locked and unclosed
 *   C created-success pins the route while the secret shows, frees it on ack,
 *     secret zeroed  (plaintext asserted on the rendered #ctSecret node)
 *   D same-session epoch churn re-enables (never strands) the still-owned modal
 *   E logout-during-secret -> re-login -> navigate is NOT trapped (self-heal)
 *   F Stop waiting recovers a hung create: in-context delivers, out-of-context
 *     drops, abandoned error drops silently
 *   G Stop waiting -> dismiss dialog -> late `created` does NOT reopen the secret
 *     (and, staying on the open dialog, it still delivers in-context)
 *   H a `created` response that resolves AFTER a session boundary must suppress
 *     the prior session's one-time token (Terra PR #167 R1 review, [high]):
 *       H1 logged-out-now (overlay visible)   — _sessionEnded overlay branch
 *       H2 logout-then-relogin (identity ref) — _sessionEnded identity branch
 *   I async-queued hashchange: the queued nav-lock revert pins the route while
 *     the create is pending, so the secret is delivered in-context and never
 *     rendered over the navigated-to route (Terra PR #167 R2 review, [high])
 *
 * Contract: DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §3.1.4, §3.3.2, §7.1.6, §7.4.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'views-access.js path is required');
const source = fs.readFileSync(viewPath, 'utf8');

const LOCKED_ROUTE = '#/access';

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

// Drain the microtask queue so a just-resolved deferred's `.then` continuations
// inside the VM run before we assert. A macrotask (setImmediate) sits strictly
// after all pending microtasks.
function flushTasks() {
    return new Promise(resolve => setImmediate(resolve));
}

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add(v) { values.add(v); },
        remove(v) { values.delete(v); },
        contains(v) { return values.has(v); },
        toggle(v, force) {
            if (force === true) values.add(v);
            else if (force === false) values.delete(v);
            else if (values.has(v)) values.delete(v);
            else values.add(v);
        },
    };
}

// A permissive fake element (only the surface views-access.js touches). `_click`
// records element-level click listeners so a dismiss can fire the view's own
// close-control teardown (showTokenSecret.destroySecret).
function makeElement(extra = {}) {
    return Object.assign({
        innerHTML: '',
        textContent: '',
        value: '',
        hidden: false,
        disabled: false,
        onclick: null,
        style: {},
        classList: classList(),
        dataset: {},
        _click: [],
        focus() {},
        select() {},
        remove() {},
        replaceChildren() { this.innerHTML = ''; },
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

function createHarness() {
    let epoch = 1;                       // AdminRouter.epoch (shell router churns it)
    let currentIdentity = { client_name: 'admin-1', permissions: ['admin'] };
    let hash = LOCKED_ROUTE;             // location.hash backing store

    const modals = [];                   // every showModal/_openDestructive call
    const toasts = [];                   // every showToast(kind, msg)
    const toolCalls = [];                // { tool, args }
    const pendingTools = [];             // deferreds callTool hands out, in order
    const hashListeners = [];            // window 'hashchange' handlers, in order
    const actions = {};                  // registerAction(name, fn)
    let registeredView = null;           // AdminViews.register('access', fn)

    // Persistent shell chrome: the single modal frame + its two dismissal
    // controls (× and Cancel). These are frame markup, not body markup, so they
    // are always resolvable — unlike modal-body IDs, which live and die with the
    // rendered body below.
    const closeControls = [
        makeElement({ dataset: { action: 'close-modal' } }),
        makeElement({ dataset: { action: 'close-modal' } }),
    ];
    const adminModal = makeElement({
        style: { display: 'none' },
        querySelectorAll(sel) {
            return sel.indexOf('close-modal') !== -1 ? closeControls.slice() : [];
        },
    });
    const loginOverlay = makeElement({ classList: classList(['hidden']) }); // logged-in

    // Rendered modal body: exactly the IDs the current body string declares,
    // with their inner text — nothing else. Replaced on each showModal, dropped
    // on wipeSession. getElementById returns null for anything not present.
    let modalEls = Object.create(null);
    function renderModalBody(body) {
        const map = Object.create(null);
        const re = /id="([^"]+)"[^>]*>([^<]*)/g;
        let m;
        while ((m = re.exec(body)) !== null) {
            if (map[m[1]]) continue;     // first declaration wins
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
        return modalEls[id] || null;
    }

    // location: assigning a *different* hash updates it synchronously and QUEUES
    // the hashchange dispatch as a browser task — it does NOT fire inline, since
    // real engines dispatch hashchange from a LATER task (Terra PR #167 [high]).
    // The nav-lock handler may re-assign hash (revert) during a dispatch, which
    // queues another dispatch; flushHashQueue() drains them iteratively with a
    // backstop that turns a runaway revert loop (a real regression) into a loud
    // failure instead of a hang. Tests drain explicitly: navigate() drains
    // immediately (the realizable ordering — a revert queued at nav time runs
    // strictly before any later network continuation), while scenario I controls
    // the drain to exercise the async ordering the [high] finding raised.
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
        replace(v) { this.hash = v; },
    };

    const documentStub = {
        getElementById(id) { return getEl(id); },
        // `#adminModal [data-action="close-modal"]` (used by _setModalDismissible).
        querySelectorAll(sel) {
            return sel.indexOf('close-modal') !== -1 ? closeControls.slice() : [];
        },
        querySelector() { return null; },
        createElement() { return makeElement(); },
        body: makeElement(),
    };

    const windowStub = {
        addEventListener(type, fn) { if (type === 'hashchange') hashListeners.push(fn); },
        removeEventListener() {},
    };

    // esc is real so the rendered #ctSecret node carries the exact plaintext.
    function esc(v) {
        return String(v)
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }

    // Fresh shell modal: visible, dismissible, no stale element-level close
    // listeners, and the body rendered into the DOM (real showModal inserts it).
    function openShellModal(title, body, verb, onConfirm) {
        adminModal.style.display = '';
        closeControls.forEach(c => { c.disabled = false; c.style.pointerEvents = ''; c._click = []; });
        renderModalBody(body || '');
        modals.push({ title, body: body || '', verb, onConfirm });
    }

    const context = {
        console,
        window: windowStub,
        document: documentStub,
        location,
        navigator: {},
        cache: { spaces: [] },
        esc,
        icon: () => '',
        pageHeader: (t, a) => 'H:' + t + (a || ''),
        panel: b => 'P:' + b,
        dataTable: (h, r) => 'T:' + r,
        pill: (k, l) => 'PILL:' + l,
        copyable: (full, shown) => 'C:' + esc(shown),
        monoBlock: b => 'M:' + b,
        stateEmpty: o => 'EMPTY:' + (o && o.title),
        stateError: o => 'ERR:' + (o && o.message),
        stateUnavailable: r => 'NA:' + r,
        stateLoading: l => 'LOADING:' + l,
        serverMessage: msg => msg ? 'SM:' + esc(String(msg)) : '',
        fmtTimestamp: ts => ({ title: String(ts), text: String(ts) }),
        showModal: openShellModal,
        showDestructiveModal(opts) {
            openShellModal(opts.title, opts.bodyHtml, opts.verb, opts.onConfirm);
        },
        closeModal() { adminModal.style.display = 'none'; },
        showToast(kind, msg) { toasts.push({ kind, msg }); },
        registerAction(name, fn) { actions[name] = fn; },
        // The shell hashchange->dispatch listener (admin-app.js) bumps the route
        // epoch on every dispatch; this stub reproduces exactly that observable
        // (it does NOT re-render — no scenario depends on the repaint, and the
        // view's staleness logic reads only AdminRouter.epoch). Registered FIRST,
        // so on a revert bounce epoch churns twice, mirroring the real
        // Back->dispatch->revert->dispatch sequence exercised by scenario D.
        AdminRouter: {
            get epoch() { return epoch; },
            refresh() { epoch += 1; },
            go(path) { location.hash = '#' + path; },
            current() { return { view: 'access', params: {}, raw: '/access' }; },
        },
        AdminViews: { register(name, fn) { registeredView = fn; } },
        _ctx: () => ({ identity: currentIdentity }),
        callTool(tool, args) {
            toolCalls.push({ tool, args });
            const d = deferred();
            pendingTools.push(d);
            return d.promise;
        },
    };

    vm.createContext(context);
    context.window.addEventListener('hashchange', () => { epoch += 1; });
    vm.runInContext(source, context, { filename: viewPath });
    assert.equal(typeof registeredView, 'function', 'views-access.js must register the access view');

    return {
        // observers
        location,
        modals,
        toasts,
        toolCalls,
        el: getEl,
        lastModal() { return modals[modals.length - 1]; },
        secretModals() { return modals.filter(m => m.title === 'Token created — save it now'); },
        // drivers
        openCreate() { actions['access-create'](); },
        confirm() { return this.lastModal().onConfirm(); },
        // Model showModal's confirm wiring: `const ok = await onConfirm(); if
        // (ok === true) closeModal();`. Lets scenario C prove a truthy ack
        // actually reaches the shell's close-on-success (Terra PR #167 [medium]).
        async confirmModal() {
            const ok = await this.lastModal().onConfirm();
            if (ok === true) context.closeModal();
            return ok;
        },
        settleTool(kind, value) {
            const d = pendingTools.shift();
            assert.ok(d, 'no pending tool call to settle');
            if (kind === 'resolve') d.resolve(value); else d.reject(value || new Error('tool failed'));
        },
        // Navigate the way a browser settles it: set the hash, then let the
        // queued hashchange task run (the realizable ordering).
        navigate(to) { location.hash = to; flushHashQueue(); },
        // Async-ordering primitives for scenario I: queue a hashchange without
        // dispatching it, and drain the queue on demand.
        setHashPending(to) { location.hash = to; },
        flushHash() { flushHashQueue(); },
        stopWaiting() {
            const btn = this.el('ctStopWaiting');
            assert.ok(btn && typeof btn.onclick === 'function',
                'Stop-waiting control must be rendered and wired while in flight');
            btn.onclick();
        },
        // A user dismissal (×/Cancel): no-op while the modal is locked, else fire
        // any element-level close listeners then the shell delegation (closeModal).
        dismiss() {
            if (closeControls[0].disabled) return false;
            closeControls.forEach(c => c._click.slice().forEach(fn => fn()));
            context.closeModal();
            return true;
        },
        // wipeSession mimic: show the overlay, rebind the identity reference, and
        // REMOVE the modal (hide the frame AND drop its rendered body) WITHOUT
        // running the view's teardown — so a held nav lock is genuinely orphaned.
        logout() {
            loginOverlay.classList.remove('hidden');
            currentIdentity = { loggedOut: true };
            adminModal.style.display = 'none';
            modalEls = Object.create(null);
        },
        login(name) {
            loginOverlay.classList.add('hidden');
            currentIdentity = { client_name: name || 'admin-2', permissions: ['admin'] };
        },
        dismissible() { return !closeControls[0].disabled; },
        modalOpen() { return adminModal.style.display !== 'none'; },
    };
}

// Fill the create form and fire the confirm, returning the (pending) onConfirm
// promise. Leaves the flow parked on the awaited admin_create_token deferred.
function startCreate(h, name = 'agent-x') {
    h.openCreate();
    h.el('ctName').value = name;
    h.el('ctPerms').value = 'read,write';
    return h.confirm();
}

// ─────────────────────────────── scenarios ───────────────────────────────

// A — while the create request is pending the nav lock reverts an off-route
// hash change; once logged out the lock self-heals and navigation proceeds.
async function scenarioA() {
    const h = createHarness();
    const p = startCreate(h);
    assert.equal(h.dismissible(), false, '[A] pending create must lock the modal');

    h.navigate('#/spaces');
    assert.equal(h.location.hash, LOCKED_ROUTE, '[A] off-route nav must revert to the locked route while pending');

    h.logout();
    h.navigate('#/spaces');
    assert.equal(h.location.hash, '#/spaces', '[A] once logged out the lock self-heals and nav proceeds');

    h.settleTool('resolve', { status: 'error' });
    await flushTasks();
    await p;
}

// B — a stale cross-session create FAILURE must not re-enable a newer session's
// still-locked create modal (Terra R2 f1). Ownership (gen+session) is checked
// before any modal mutation.
async function scenarioB() {
    const h = createHarness();
    const p1 = startCreate(h, 'sess1-token');           // session 1 create pending

    h.logout();
    h.login('admin-2');                                 // session boundary crossed
    const p2 = startCreate(h, 'sess2-token');           // session 2 create pending + locked
    assert.equal(h.dismissible(), false, '[B] session-2 modal must be locked while its create is pending');

    h.settleTool('resolve', { status: 'error' });       // session 1 FIFO resolves first
    await flushTasks();
    await p1;

    assert.equal(h.dismissible(), false, '[B] stale session-1 failure must NOT re-enable the newer locked modal');
    assert.equal(h.modalOpen(), true, '[B] stale session-1 failure must NOT close the newer modal');

    h.settleTool('resolve', { status: 'error' });       // let session 2 finish cleanly
    await flushTasks();
    await p2;
}

// C — a created response surfaces the one-time secret; the route stays pinned
// while the plaintext is on screen, and the acknowledge zeroes the secret and
// frees navigation. The plaintext is asserted on the RENDERED #ctSecret node.
async function scenarioC() {
    const h = createHarness();
    const p = startCreate(h);
    assert.equal(h.toolCalls[0].tool, 'admin_create_token',
        '[C] the harness must drive the real create path (admin_create_token)');

    h.navigate('#/spaces');
    assert.equal(h.location.hash, LOCKED_ROUTE, '[C] route pinned while the create is pending');

    h.settleTool('resolve', {
        status: 'created', token: 'SEKRET-PLAINTEXT', name: 'agent-x', permissions: ['read'],
    });
    await flushTasks();
    await p;

    assert.equal(h.lastModal().title, 'Token created — save it now', '[C] a created response must surface the secret modal');
    assert.equal(h.el('ctSecret').textContent, 'SEKRET-PLAINTEXT',
        '[C] the one-time plaintext must be rendered into the #ctSecret DOM node');

    h.navigate('#/spaces');
    assert.equal(h.location.hash, LOCKED_ROUTE, '[C] route pinned while the secret is displayed');

    const ack = await h.confirmModal();       // "I have saved it" acknowledge (through the shell contract)
    assert.equal(ack, true, '[C] the acknowledge must return truthy so the shell closes the modal');
    assert.equal(h.modalOpen(), false, '[C] a truthy acknowledge must reach the shell close-on-success (modal hidden)');
    assert.equal(h.el('ctCopyBtn').disabled, true, '[C] acknowledge must destroy the secret (Copy disabled)');
    assert.equal(h.el('ctSecret').textContent, '', '[C] acknowledge must zero the rendered secret node');

    h.navigate('#/spaces');
    assert.equal(h.location.hash, '#/spaces', '[C] navigation is freed once the secret is acknowledged');
}

// D — same session, but the nav-lock revert churns the route epoch. A create
// FAILURE on the still-owned modal must re-enable it (the error path's ownership
// check deliberately excludes epoch — Terra R2 f1 corollary).
async function scenarioD() {
    const h = createHarness();
    const p = startCreate(h);

    h.navigate('#/spaces');                  // Back -> dispatch -> revert -> dispatch: epoch churns
    assert.equal(h.location.hash, LOCKED_ROUTE, '[D] route reverts (same session, lock held)');

    h.settleTool('resolve', { status: 'error', message: 'server refused' });
    await flushTasks();
    await p;

    assert.equal(h.dismissible(), true, '[D] epoch churn must NOT strand the still-owned modal — error path re-enables it');
    assert.equal(h.el('ctNameErr').hidden, false, '[D] the create error must be shown on the re-enabled modal');
}

// E — logout while the one-time secret is on screen orphans the secret's nav
// lock; after re-login the FIRST navigation must self-heal the lock and proceed
// (never trap the new session on the dead flow's route).
async function scenarioE() {
    const h = createHarness();
    const p = startCreate(h);
    h.settleTool('resolve', {
        status: 'created', token: 'SEKRET', name: 'agent-x', permissions: ['read'],
    });
    await flushTasks();
    await p;
    assert.equal(h.lastModal().title, 'Token created — save it now', '[E] precondition: secret is displayed');

    h.logout();                              // 401/logout mid-secret: lock orphaned, no teardown
    h.login('admin-2');                      // re-login as a different session

    h.navigate('#/spaces');
    assert.equal(h.location.hash, '#/spaces', '[E] re-login must NOT be trapped by the orphaned lock (self-heal)');

    h.navigate('#/operator');
    assert.equal(h.location.hash, '#/operator', '[E] subsequent navigation must keep working');
}

// F — the "Stop waiting" escape recovers a hung create WITHOUT abandoning the
// live promise: an in-context arrival still delivers, an out-of-context arrival
// drops, and an abandoned error drops silently.
async function scenarioF() {
    // F1 — in-context created still delivers after Stop waiting.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.stopWaiting();
        assert.equal(h.dismissible(), true, '[F1] Stop waiting must re-enable dismissal');
        assert.ok(h.el('ctNameErr').textContent.includes('Stopped waiting'), '[F1] operator must be warned');

        h.settleTool('resolve', { status: 'created', token: 'S', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 1, '[F1] an in-context created arrival must still deliver the secret');
    }
    // F2 — out-of-context created (operator navigated away) drops.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.stopWaiting();
        h.navigate('#/spaces');                // lock released by escape -> nav proceeds, epoch changes
        assert.equal(h.location.hash, '#/spaces', '[F2] after Stop waiting navigation is free');

        h.settleTool('resolve', { status: 'created', token: 'S', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 0, '[F2] an out-of-context created arrival must NOT surface the secret');
    }
    // F3 — abandoned error drops silently.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.stopWaiting();
        const toastsBefore = h.toasts.length;
        h.settleTool('resolve', { status: 'error', message: 'boom' });
        await flushTasks();
        await p;
        assert.ok(h.el('ctNameErr').textContent.includes('Stopped waiting'),
            '[F3] an abandoned error must drop silently (no showCreateError overwrite)');
        assert.equal(h.toasts.length, toastsBefore, '[F3] an abandoned error must not toast');
    }
}

// G — Terra R4: after Stop waiting re-enables ×/Cancel, DISMISSING the create
// dialog (which only hides it — epoch/gen/session unchanged) must drop a late
// `created` instead of REOPENING the secret; staying on the open dialog still
// delivers.
async function scenarioG() {
    // G1 — dismiss then late created => no secret reopened.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.stopWaiting();
        assert.equal(h.dismiss(), true, '[G1] Stop waiting must make the dialog dismissible again');
        assert.equal(h.modalOpen(), false, '[G1] precondition: the create dialog is dismissed (hidden)');

        h.settleTool('resolve', { status: 'created', token: 'LATE-SECRET', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 0, '[G1] a dismissed dialog must NOT reopen the late one-time secret');
        assert.equal(h.modalOpen(), false, '[G1] the modal must stay hidden');
    }
    // G2 — stay on the open dialog => late created still delivers in-context.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.stopWaiting();                       // stay on the dialog (no dismiss, no nav)
        h.settleTool('resolve', { status: 'created', token: 'IN-CTX', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 1, '[G2] staying on the open dialog still delivers the in-context secret');
    }
}

// H — a `created` response resolving AFTER a session boundary must suppress the
// prior session's one-time token (never render the plaintext in a dead/other
// session). This exercises the create branch's `_sessionEnded(sessionAtCall)`
// guard, which A–G never reached (Terra PR #167 review, [high]). Two orderings,
// one per branch of _sessionEnded:
async function scenarioH() {
    // H1 — logged-out-now: the login overlay is visible (overlay branch).
    {
        const h = createHarness();
        const p = startCreate(h);
        h.logout();                            // overlay visible; still the same (now dead) identity
        h.settleTool('resolve', { status: 'created', token: 'LEAK-1', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 0, '[H1] created while logged out must NOT surface the secret');
        assert.ok(!h.modals.some(m => m.body.includes('LEAK-1')),
            '[H1] the logged-out session plaintext must never be rendered');
    }
    // H2 — logout-then-relogin: overlay hidden again but the identity reference
    // changed (identity branch). This is the cross-session token-leak path.
    {
        const h = createHarness();
        const p = startCreate(h);
        h.logout();
        h.login('admin-2');                    // different session; overlay hidden again
        h.settleTool('resolve', { status: 'created', token: 'LEAK-2', name: 'agent-x', permissions: ['read'] });
        await flushTasks();
        await p;
        assert.equal(h.secretModals().length, 0,
            '[H2] created after logout+relogin must NOT surface the prior session secret (cross-session leak)');
        assert.ok(!h.modals.some(m => m.body.includes('LEAK-2')),
            '[H2] the prior session plaintext must never be rendered in the new session');
    }
}

// I — async-queued hashchange fidelity (Terra PR #167 review, [high]). hashchange
// is modeled as a QUEUED browser task, not a synchronous call inside the setter.
// This drives the ordering explicitly: a navigation while the create is pending
// queues the nav-lock revert, that revert task runs, and only THEN the (strictly
// later) network reply resolves — proving the secret is delivered in-context and
// never rendered over the navigated-to route.
//
// The inverse ordering the finding describes — the `created` continuation running
// BETWEEN the synchronous hash write and its own queued hashchange dispatch — is
// excluded by the JS event loop, not merely unmodeled: a network-backed promise
// reaction is a microtask that drains at the end of whichever task resolved it,
// never between a later task's synchronous hash write and that task's queued
// macrotask. Both realizable interleavings are safe: if the revert wins,
// showTokenSecret captures the locked route (below); if the create wins first,
// the secret's OWN nav lock reverts the subsequent navigation (scenarios C/E).
async function scenarioI() {
    const h = createHarness();
    const p = startCreate(h);

    h.setHashPending('#/spaces');            // navigate: hash set, hashchange QUEUED (not yet dispatched)
    assert.equal(h.location.hash, '#/spaces', '[I] the hash updates synchronously; the dispatch is deferred');
    h.flushHash();                            // the queued revert task runs (before any network reply could)
    assert.equal(h.location.hash, LOCKED_ROUTE, '[I] the queued revert pins the route while the create is pending');

    h.settleTool('resolve', { status: 'created', token: 'ASYNC-SEK', name: 'agent-x', permissions: ['read'] });
    await flushTasks();
    await p;
    assert.equal(h.el('ctSecret').textContent, 'ASYNC-SEK', '[I] the secret is delivered in-context');
    assert.equal(h.location.hash, LOCKED_ROUTE, '[I] the secret is never rendered over the navigated-to route');
}

await scenarioA();
await scenarioB();
await scenarioC();
await scenarioD();
await scenarioE();
await scenarioF();
await scenarioG();
await scenarioH();
await scenarioI();
console.log('admin access lifecycle runtime: ok');
