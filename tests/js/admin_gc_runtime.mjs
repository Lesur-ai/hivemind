import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'views-operator.js path is required');
const source = fs.readFileSync(viewPath, 'utf8');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

async function flushTasks() {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
}

function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        contains(value) { return values.has(value); },
    };
}

function element(extra = {}) {
    const listeners = {};
    return Object.assign({
        innerHTML: '',
        textContent: '',
        value: '',
        disabled: false,
        classList: classList(),
        addEventListener(type, handler) { listeners[type] = handler; },
        emit(type) {
            assert.equal(typeof listeners[type], 'function', `missing ${type} listener`);
            listeners[type]({ target: this });
        },
    }, extra);
}

function dryResponse({ count = 2, token = 'opaque-proof-A', message = 'scan <b>A</b>' } = {}) {
    return {
        status: 'ok',
        mode: 'dry-run',
        max_age_days: 7,
        cutoff_date: '2026-07-06T00:00:00+00:00',
        total_old_notes: count,
        total_old_size: 42,
        eligible_set_token: token,
        spaces: count ? {
            alpha: {
                total_notes: count + 1,
                old_notes: count,
                old_notes_size: 42,
                by_agent: { orphan: count },
                oldest: '20260101T000000',
                keys_count: count,
            },
        } : {},
        message,
    };
}

function createHarness() {
    let registeredRender = null;
    let shellSessionGeneration = 0;
    const actions = {};
    const calls = [];
    const pendingGc = [];
    const modals = [];
    const serverMessages = [];
    const toasts = [];

    const elements = {
        content: element(),
        loginOverlay: element({ classList: classList(['hidden']) }),
        opMaintPicker: element(),
        opMaintPanels: element(),
        opMaintSpace: element({ value: '' }),
        opGcMaxAge: element({ value: '7' }),
        opGcResults: element(),
        opCompactResults: element(),
        opRepairResults: element(),
    };

    const document = {
        getElementById(id) { return elements[id] || null; },
    };

    const context = {
        console,
        document,
        AdminRouter: {
            epoch: 7,
            refresh() {},
        },
        AdminViews: {
            register(name, render) {
                assert.equal(name, 'operator');
                registeredRender = render;
            },
        },
        currentSessionGeneration() { return shellSessionGeneration; },
        sessionGenerationIsCurrent(generation) {
            return generation === shellSessionGeneration;
        },
        registerAction(name, handler) {
            assert.equal(actions[name], undefined, `duplicate action ${name}`);
            actions[name] = handler;
        },
        callTool(tool, args) {
            calls.push({ tool, args });
            if (tool === 'space_list') {
                return Promise.resolve({
                    status: 'ok',
                    spaces: [{ space_id: 'alpha' }, { space_id: 'beta' }],
                });
            }
            assert.equal(tool, 'admin_gc_notes', `unexpected tool ${tool}`);
            const item = deferred();
            pendingGc.push({ ...item, args });
            return item.promise;
        },
        showModal(title, bodyHTML, btnLabel, onConfirm) {
            modals.push({ kind: 'neutral', title, bodyHTML, btnLabel, onConfirm });
        },
        showDestructiveModal(opts) {
            modals.push({ kind: 'destructive', ...opts });
        },
        showToast(kind, text) { toasts.push({ kind, text }); },
        esc: escapeHtml,
        icon: name => `ICON:${name}`,
        pageHeader: title => `HEADER:${title}`,
        panel: body => `PANEL:${body}`,
        stateEmpty: options => `EMPTY:${escapeHtml(options.title)}`,
        stateError: options => `ERROR:${escapeHtml(options.message || options.title)}`,
        stateLoading: text => `LOADING:${escapeHtml(text)}`,
        stateUnavailable: text => `UNAVAILABLE:${escapeHtml(text)}`,
        serverMessage(message) {
            serverMessages.push(String(message));
            return `<SERVER>${escapeHtml(message)}</SERVER>`;
        },
        fmtSize: value => `${String(value ?? '—')}B`,
    };
    vm.createContext(context);
    vm.runInContext(source, context, { filename: viewPath });
    assert.equal(typeof registeredRender, 'function');

    return {
        actions,
        calls,
        context,
        elements,
        modals,
        pendingGc,
        serverMessages,
        toasts,
        async render(sessionGeneration) {
            shellSessionGeneration = sessionGeneration;
            registeredRender(elements.content, { tab: 'maintenance' }, {
                epoch: context.AdminRouter.epoch,
                sessionGeneration,
                identity: {
                    // Deliberately identical across generations: client_name is
                    // not a session-owner marker.
                    client_name: 'same-name-admin',
                    permissions: ['read', 'write', 'manage', 'admin'],
                },
            });
            await flushTasks();
        },
        target(spaceId = 'alpha', maxAgeDays = 7) {
            elements.opMaintSpace.value = spaceId;
            elements.opGcMaxAge.value = String(maxAgeDays);
        },
        action(name) {
            assert.equal(typeof actions[name], 'function', `missing action ${name}`);
            return actions[name]({}, element());
        },
        gcCalls() {
            return calls.filter(call => call.tool === 'admin_gc_notes');
        },
        lastModal(kind = null) {
            const matching = kind ? modals.filter(modal => modal.kind === kind) : modals;
            assert.ok(matching.length, `no ${kind || ''} modal recorded`);
            return matching.at(-1);
        },
    };
}

async function resolveDry(harness, response = dryResponse()) {
    harness.action('op-gc-dry');
    const pending = harness.pendingGc.at(-1);
    assert.ok(pending, 'dry run did not call admin_gc_notes');
    pending.resolve(response);
    await flushTasks();
}

async function exactDeleteProofAndEscaping() {
    const h = createHarness();
    await h.render(11);
    h.target('alpha', 7);
    await resolveDry(h, dryResponse({ token: 'opaque-equal-count-proof', message: 'scan <b>verbatim</b>' }));

    assert.deepEqual(plain(h.gcCalls()[0].args), {
        space_id: 'alpha', max_age_days: 7, confirm: false,
    });
    assert.match(h.elements.opGcResults.innerHTML, /scan &lt;b&gt;verbatim&lt;\/b&gt;/);
    assert.doesNotMatch(h.elements.opGcResults.innerHTML, /scan <b>/);
    assert.doesNotMatch(h.elements.opGcResults.innerHTML, /opaque-equal-count-proof/);

    h.action('op-gc-delete');
    const confirmation = h.lastModal('destructive');
    assert.equal(confirmation.typedConfirmation, 'delete 2 notes');
    assert.match(
        confirmation.bodyHtml,
        /Deletes 2 orphan notes WITHOUT consolidating them\. Their content is lost\./,
    );

    const confirmationPromise = confirmation.onConfirm();
    await flushTasks();
    assert.deepEqual(plain(h.gcCalls()[1].args), {
        space_id: 'alpha',
        max_age_days: 7,
        confirm: true,
        delete_only: true,
        expected_eligible_set_token: 'opaque-equal-count-proof',
    });
    const destructiveCount = h.modals.filter(modal => modal.kind === 'destructive').length;
    h.action('op-gc-delete');
    h.action('op-gc-dry');
    assert.equal(h.modals.filter(modal => modal.kind === 'destructive').length, destructiveCount);
    assert.equal(h.gcCalls().length, 2, 'proof remained usable while delete was pending');
    h.pendingGc[1].resolve({
        status: 'deleted',
        action: 'delete',
        deleted: 2,
        delete_requested: 2,
        delete_failed: 0,
        message: 'supprimées <script>bad()</script>',
    });
    assert.equal(await confirmationPromise, false);
    await flushTasks();
    assert.match(h.elements.opGcResults.innerHTML, /Deletion complete/);
    assert.match(h.elements.opGcResults.innerHTML, /supprimées &lt;script&gt;bad\(\)&lt;\/script&gt;/);
    assert.equal(h.gcCalls().length, 2, 'delete must not auto-rescan or auto-retry');
}

async function neutralConsolidationAndPartialDetails() {
    const h = createHarness();
    await h.render(21);
    h.target('alpha', 7);
    h.action('op-gc-consolidate');
    const confirmation = h.lastModal('neutral');
    assert.equal(confirmation.title, 'Consolidate orphan notes');
    assert.equal(confirmation.btnLabel, 'Consolidate');
    assert.equal(h.modals.some(modal => modal.kind === 'destructive'), false);

    const confirmationPromise = confirmation.onConfirm();
    await flushTasks();
    assert.deepEqual(plain(h.gcCalls()[0].args), {
        space_id: 'alpha',
        max_age_days: 7,
        confirm: true,
        delete_only: false,
    });
    h.pendingGc[0].resolve({
        status: 'partial',
        reason: 'partial_consolidation',
        action: 'consolidate',
        consolidated: 1,
        consolidation_requested: 2,
        consolidation_failed: 1,
        consolidation_details: {
            alpha: {
                orphan: {
                    status: 'partial',
                    reason: 'incomplete_consolidation',
                    notes_processed: 1,
                    notes_requested: 2,
                    message: 'détail <unsafe>',
                },
            },
        },
        message: 'consolidation <partielle>',
    });
    assert.equal(await confirmationPromise, false);
    await flushTasks();
    const html = h.elements.opGcResults.innerHTML;
    assert.match(html, /Consolidation partial/);
    assert.match(html, /1\/2 orphan note\(s\) consolidated; 1 not consolidated/);
    assert.match(html, /incomplete_consolidation/);
    assert.match(html, /détail &lt;unsafe&gt;/);
    assert.equal(h.gcCalls().length, 1, 'consolidation must not auto-rescan or retry');
}

async function typedConflictAndPartialDeleteAreHonest() {
    const conflict = createHarness();
    await conflict.render(31);
    conflict.target('alpha', 7);
    await resolveDry(conflict, dryResponse({ token: 'set-A' }));
    conflict.action('op-gc-delete');
    const conflictPromise = conflict.lastModal('destructive').onConfirm();
    await flushTasks();
    conflict.pendingGc[1].resolve({
        status: 'conflict',
        reason: 'eligible_set_changed',
        action: 'delete',
        deleted: 0,
        message: 'ensemble remplacé à cardinalité égale',
    });
    assert.equal(await conflictPromise, false);
    await flushTasks();
    assert.match(conflict.elements.opGcResults.innerHTML, /Eligible note set changed/);
    assert.match(conflict.elements.opGcResults.innerHTML, /eligible_set_changed/);
    assert.doesNotMatch(conflict.elements.opGcResults.innerHTML, /Deletion complete/);
    assert.equal(conflict.gcCalls().length, 2, 'conflict must not auto-retry');

    const partial = createHarness();
    await partial.render(32);
    partial.target('alpha', 7);
    await resolveDry(partial, dryResponse({ token: 'set-partial' }));
    partial.action('op-gc-delete');
    const partialPromise = partial.lastModal('destructive').onConfirm();
    await flushTasks();
    partial.pendingGc[1].resolve({
        status: 'partial',
        reason: 'partial_delete',
        action: 'delete',
        delete_requested: 2,
        deleted: 1,
        delete_failed: 1,
        message: 'une note reste',
    });
    assert.equal(await partialPromise, false);
    await flushTasks();
    assert.match(partial.elements.opGcResults.innerHTML, /Deletion partial/);
    assert.match(partial.elements.opGcResults.innerHTML, /1\/2 orphan note\(s\) deleted; 1 not deleted/);
    assert.doesNotMatch(partial.elements.opGcResults.innerHTML, /Deletion complete/);
    assert.equal(partial.gcCalls().length, 2, 'partial delete must not auto-retry');
}

async function proofInvalidationAndReorderedScans() {
    const changed = createHarness();
    await changed.render(41);
    changed.target('alpha', 7);
    await resolveDry(changed, dryResponse({ token: 'proof-before-change' }));

    const destructiveBeforeAge = changed.modals.filter(m => m.kind === 'destructive').length;
    changed.elements.opGcMaxAge.value = '8';
    changed.elements.opGcMaxAge.emit('input');
    changed.action('op-gc-delete');
    assert.equal(changed.modals.filter(m => m.kind === 'destructive').length, destructiveBeforeAge);
    assert.match(changed.elements.opGcResults.innerHTML, /Run a fresh dry run/);

    changed.target('alpha', 7);
    await resolveDry(changed, dryResponse({ token: 'proof-before-target-change' }));
    changed.elements.opMaintSpace.value = 'beta';
    changed.elements.opMaintSpace.emit('change');
    changed.elements.opMaintSpace.value = 'alpha';
    changed.action('op-gc-delete');
    assert.equal(changed.modals.filter(m => m.kind === 'destructive').length, destructiveBeforeAge);

    const reordered = createHarness();
    await reordered.render(42);
    reordered.target('alpha', 7);
    reordered.action('op-gc-dry');
    const older = reordered.pendingGc[0];
    reordered.action('op-gc-dry');
    const newer = reordered.pendingGc[1];
    newer.resolve(dryResponse({ token: 'newer-proof', message: 'newer scan' }));
    await flushTasks();
    older.resolve(dryResponse({ token: 'older-proof', message: 'older scan' }));
    await flushTasks();
    assert.match(reordered.elements.opGcResults.innerHTML, /newer scan/);
    assert.doesNotMatch(reordered.elements.opGcResults.innerHTML, /older scan/);

    reordered.action('op-gc-delete');
    const deletion = reordered.lastModal('destructive').onConfirm();
    await flushTasks();
    assert.equal(reordered.gcCalls()[2].args.expected_eligible_set_token, 'newer-proof');
    reordered.pendingGc[2].resolve({ status: 'conflict', reason: 'eligible_set_changed', deleted: 0 });
    await deletion;
    await flushTasks();

    const failed = createHarness();
    await failed.render(43);
    failed.target('alpha', 7);
    await resolveDry(failed, dryResponse({ token: 'old-proof' }));
    failed.action('op-gc-dry');
    const destructiveBeforeFailure = failed.modals.filter(m => m.kind === 'destructive').length;
    failed.action('op-gc-delete');
    assert.equal(failed.modals.filter(m => m.kind === 'destructive').length, destructiveBeforeFailure);
    failed.pendingGc[1].resolve({ status: 'error', reason: 'route_refused', message: 'refusée' });
    await flushTasks();
    failed.action('op-gc-delete');
    assert.equal(failed.modals.filter(m => m.kind === 'destructive').length, destructiveBeforeFailure);
}

async function sessionGenerationOwnsProofAndContinuations() {
    const h = createHarness();
    await h.render(51);
    h.target('alpha', 7);
    await resolveDry(h, dryResponse({ token: 'session-51-proof' }));

    await h.render(52);
    h.target('alpha', 7);
    const destructiveBefore = h.modals.filter(m => m.kind === 'destructive').length;
    h.action('op-gc-delete');
    assert.equal(h.modals.filter(m => m.kind === 'destructive').length, destructiveBefore);

    await resolveDry(h, dryResponse({ token: 'session-52-proof' }));
    h.action('op-gc-delete');
    const oldDelete = h.lastModal('destructive');
    const oldDeletePromise = oldDelete.onConfirm();
    await flushTasks();
    const oldPending = h.pendingGc.at(-1);

    await h.render(53);
    h.target('alpha', 7);
    h.action('op-gc-consolidate');
    const newerModal = h.lastModal('neutral');
    assert.equal(newerModal.title, 'Consolidate orphan notes');
    const modalCount = h.modals.length;
    const htmlBeforeLateResponse = h.elements.opGcResults.innerHTML;

    oldPending.resolve({
        status: 'deleted',
        deleted: 2,
        delete_requested: 2,
        delete_failed: 0,
        message: 'late prior-session success',
    });
    assert.equal(await oldDeletePromise, false);
    await flushTasks();
    assert.equal(h.modals.length, modalCount, 'prior-session completion replaced a newer modal');
    assert.equal(h.elements.opGcResults.innerHTML, htmlBeforeLateResponse);
    assert.doesNotMatch(h.elements.opGcResults.innerHTML, /late prior-session success/);
}

async function invalidThresholdsNeverReachTheServer() {
    for (const raw of ['-1', '1.5', '']) {
        const h = createHarness();
        await h.render(61);
        h.elements.opMaintSpace.value = 'alpha';
        h.elements.opGcMaxAge.value = raw;
        h.action('op-gc-dry');
        h.action('op-gc-consolidate');
        h.action('op-gc-delete');
        assert.equal(h.gcCalls().length, 0, `invalid threshold ${JSON.stringify(raw)} reached server`);
        assert.match(h.elements.opGcResults.innerHTML, /whole number greater than or equal to 0/);
    }
}

await exactDeleteProofAndEscaping();
await neutralConsolidationAndPartialDetails();
await typedConflictAndPartialDeleteAreHonest();
await proofInvalidationAndReorderedScans();
await sessionGenerationOwnsProofAndContinuations();
await invalidThresholdsNeverReachTheServer();

console.log('admin GC runtime: ok');
