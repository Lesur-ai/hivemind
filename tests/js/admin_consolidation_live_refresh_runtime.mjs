import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const consolidationPath = process.argv[2];
assert.ok(consolidationPath, 'consolidation view path is required');

const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const stateStub = kind => options => {
    const o = typeof options === 'string' ? { title: options } : (options || {});
    return `<div class="state-${kind}">${escapeHtml(o.title || '')}</div>`;
};

function fakeElement() { return { innerHTML: '', isConnected: true, contains: () => false, querySelector: () => null, querySelectorAll: () => [], classList: { contains: () => true } }; }

function harness(enabled = true) {
    const timers = [];            // scheduled callbacks: {id, fn, ms, cancelled}
    const calls = [];             // callTool invocations: {name, args}
    const modal = { style: { display: 'none' }, shows: [] };
    let nextResponse = () => ({});
    const actions = {}, listeners = {};
    let generation = 1;
    const elements = { consolJobFreshness: fakeElement(), consolFreshness: fakeElement(), consolPickerActions: fakeElement(), consolPickSpace: { value: '' }, consolStaleResults: fakeElement(), consolStaleMinNotes: { value: '5' }, consolStaleMinAge: { value: '5' }, consolLanes: fakeElement(), consolSubtitle: fakeElement(), consolStale: fakeElement(), adminModal: modal };
    const ctx = {
        console,
        esc: escapeHtml,
        icon: () => '',
        panel: html => `<section>${html}</section>`,
        pageHeader: (t, a) => `<header>${escapeHtml(t)}${a}</header>`,
        serverMessage: m => `<p>${escapeHtml(m)}</p>`,
        statusDot: (s, t) => `<span>${escapeHtml(t)}</span>`,
        dataTable: (h, rows) => `<table>${Array.isArray(rows) ? rows.join('') : String(rows ?? '')}</table>`,
        renderTimestamp: v => `<time>${escapeHtml(v)}</time>`,
        copyable: v => `<code>${escapeHtml(v)}</code>`,
        monoBlock: v => `<pre>${escapeHtml(v)}</pre>`,
        truncateMiddle: v => String(v ?? ''),
        stateError: stateStub('error'), stateEmpty: stateStub('empty'),
        stateLoading: stateStub('loading'), stateUnavailable: stateStub('unavailable'),
        registerAction(name, fn) { actions[name] = fn; }, showToast() {}, showDestructiveModal() {},
        currentSessionGeneration: () => generation, sessionGenerationIsCurrent: g => g === generation,
        showModal(title, body, confirmLabel, onConfirm) { if (elements.consolJobSnapshot) elements.consolJobSnapshot.isConnected = false; elements.consolJobSnapshot = fakeElement(); modal.style.display = 'flex'; modal.shows.push(String(body)); modal.confirm = onConfirm; },
        closeModal() { modal.style.display = 'none'; },
        openConsolidationLauncher(options) { modal.shows.push(JSON.stringify(options.spaces)); },
        callTool: async (name, args) => { calls.push({ name, args }); return nextResponse(name, args); },
        AdminViews: { register() {} },
        AdminRouter: { epoch: 1, go() {}, refresh() {}, current: () => ({}) },
        document: {
            hidden: false,
            addEventListener(name, fn) { listeners[name] = fn; },
            dispatchEvent() {},
            querySelector() { return null; },
            querySelectorAll() { return []; },
            getElementById(id) { return elements[id] || null; },
        },
        window: {},
        CustomEvent: function(name) { this.type = name; },
        localStorage: { getItem: () => JSON.stringify({ enabled, intervalSeconds: 15 }), setItem() {} },
        setTimeout(fn, ms) { const id = timers.length + 1; timers.push({ id, fn, ms, cancelled: false }); return id; },
        clearTimeout(id) { const t = timers.find(x => x.id === id); if (t) t.cancelled = true; },
    };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(new URL('../../src/live_mem/static/js/admin/portal-refresh.js', import.meta.url), 'utf8') + '\nglobalThis.__refresh = PortalRefresh;', ctx);
    ctx.__refresh.beginSession();
    // Load the actual shared renderer, as the browser shell does. Keep this
    // seam present even in fixtures with no automatic-maintenance outcome.
    const appSource = fs.readFileSync(new URL('../../src/live_mem/static/js/admin-app.js', import.meta.url), 'utf8');
    const helperStart = appSource.indexOf('function renderAutoCompaction(');
    const helperEnd = appSource.indexOf('\nfunction ', helperStart + 1);
    assert.ok(helperStart >= 0 && helperEnd > helperStart, 'shared maintenance renderer is missing');
    vm.runInContext(appSource.slice(helperStart, helperEnd), ctx);

    const source = fs.readFileSync(consolidationPath, 'utf8');
    const instrumented = source.replace(
        "AdminViews.register('consolidation', render);",
        'globalThis.__live = { render, loadLanes, inspectJob, state, paintLanes, progressBar, renderJob };',
    );
    assert.notEqual(instrumented, source, 'consolidation instrumentation anchor missing');
    vm.runInContext(instrumented, ctx, { filename: consolidationPath });
    assert.ok(ctx.__live, 'consolidation instrumentation failed');
    const pending = () => timers.filter(t => !t.cancelled && !t.fired);
    const fire = async t => { t.fired = true; await t.fn(); await new Promise(r => setImmediate(r)); };
    return { ctx, elements, actions, visibility(hidden) { ctx.document.hidden = hidden; listeners.visibilitychange(); }, setGeneration(g) { generation = g; }, timers, calls, modal, pending, fire, setResponse(fn) { nextResponse = fn; } };
}

const RUNNING = { status: 'ok', parallelism_model: 'one_worker_per_space', service_config: { batch_size: 2 },
    lanes: [{ space_id: 'demo', lane_state: 'running', running_job: { job_id: 'j1' }, queued_count: 0, latest_jobs: [] }] };
const QUEUED = { status: 'ok', lanes: [{ space_id: 'demo', lane_state: 'queued', running_job: null, queued_count: 1, latest_jobs: [] }] };
const IDLE = { status: 'ok', lanes: [{ space_id: 'demo', lane_state: 'idle', running_job: null, queued_count: 0, latest_jobs: [] }] };

const settle = () => new Promise(r => setImmediate(r));

// A degraded identity must not leave the user waiting for a read that cannot run.
{
    const h = harness(false);
    h.ctx.__refresh.endSession();
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} });
    await settle();
    assert.equal(h.calls.length, 0);
    assert.match(h.elements.consolLanes.innerHTML, /Refresh unavailable.*sign in again/);
    await h.ctx.__live.inspectJob('unavailable');
    assert.match(h.modal.shows.at(-1), /Refresh unavailable.*sign in again/);
    assert.equal(h.calls.length, 0);
}

// Space embeds the same view, reusing its already-authorized lane snapshot.
{
    const h = harness(false), root = fakeElement();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(root, { spaceId: 'demo', embedded: true, initialLane: { ...RUNNING.lanes[0], parallelism_model: 'one_worker_per_space', service_config: { batch_size: 2 } } }, { epoch: 1, identity: {} });
    await settle();
    assert.equal(h.calls.length, 0, 'embedded initial lane avoids a redundant read');
    assert.equal(root.innerHTML.includes('All spaces'), false);
    assert.equal(root.innerHTML.includes('consolStale'), false);
    assert.match(h.elements.consolLanes.innerHTML, /data-job-id="j1"/);
    assert.match(h.elements.consolSubtitle.innerHTML, /1 worker per space/);
    assert.match(h.elements.consolSubtitle.innerHTML, /batch size:.*2/);
    assert.equal(h.elements.consolSubtitle.innerHTML.includes('Worker configuration unavailable'), false);
    h.ctx.__refresh.configure({ enabled: true, intervalSeconds: 15 });
    await h.fire(h.pending()[0]);
    assert.equal(h.calls[0].args.space_ids, 'demo');
}

// Fresh browser is manual; enabling follows real progress using explicit IDs.
{
    const h = harness(false); h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    assert.equal(h.calls.length, 1); assert.equal(h.pending().length, 0, 'off means no periodic request');
    h.ctx.__refresh.configure({ enabled: true, intervalSeconds: 15 });
    assert.equal(h.pending()[0].ms, 15000);
    await h.fire(h.pending()[0]);
    assert.equal(h.calls[1].args.space_ids, 'demo', 'tick never runs global inventory');
    for (const seconds of [30, 60]) {
        h.ctx.__refresh.configure({ enabled: true, intervalSeconds: seconds });
        assert.equal(h.pending()[0].ms, seconds * 1000);
    }
}
{
    const h = harness();
    let response = { ...RUNNING, lanes: [...RUNNING.lanes, { ...IDLE.lanes[0], space_id: 'idle' }] };
    h.setResponse(() => response);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    await h.fire(h.pending()[0]);
    assert.equal(h.calls[1].args.space_ids, 'demo,idle', 'all painted lanes remain in scope');
    response = IDLE; await h.fire(h.pending()[0]);
    assert.equal(h.pending().length, 0, 'terminal activity stops detail/lane follow');
}
for (const change of ['route', 'session']) {
    const h = harness(); h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    if (change === 'route') h.ctx.AdminRouter.epoch = 2; else h.setGeneration(2);
    await h.fire(h.pending()[0]); assert.equal(h.calls.length, 1, change + ' stops reads');
}
{
    const h = harness(); h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    h.visibility(true); assert.equal(h.pending().length, 0);
    assert.equal(h.calls.length, 1);
    h.visibility(false); await settle(); assert.equal(h.calls.length, 2, 'one fresh read on visibility return');
    assert.equal(h.pending().length, 1);
}
// The lane payload updates an open detail; it is already the full job payload.
{
    const h = harness();
    let job = { status: 'running', job_id: 'j1', progress: { notes_done: 1, notes_total: 4 }, polling: { recommended: false } };
    let response = () => ({ status: 'ok', lanes: [{ space_id: 'demo', running_job: job.status === 'running' ? job : null, latest_jobs: [job] }] });
    h.setResponse(name => name === 'bank_consolidation_status' ? job : response());
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    await h.ctx.__live.inspectJob('j1');
    const calls = h.calls.filter(c => c.name === 'bank_consolidation_status').length;
    job = { ...job, progress: { notes_done: 3, notes_total: 4 } };
    let focusRestored = 0;
    h.ctx.document.activeElement = { dataset: { action: 'copy-value', value: 'j1' } };
    h.elements.consolJobSnapshot.contains = () => true;
    h.elements.consolJobSnapshot.querySelectorAll = () => [{ dataset: { action: 'copy-value', value: 'j1' }, focus() { focusRestored++; } }];
    await h.fire(h.pending()[0]);
    assert.equal(focusRestored, 1, 'updating the detail preserves focus on its matching action');
    assert.match(h.elements.consolJobSnapshot.innerHTML, /3\/4/);
    assert.equal(h.calls.filter(c => c.name === 'bank_consolidation_status').length, calls, 'no duplicate status read');
    assert.equal(h.modal.shows.length, 1, 'read paints the body without reopening the modal');
    job = { ...job, status: 'failed', error: '<failure>', result: { notes_total: 4, auto_compaction: { status: 'partial', recovery_required: true } } };
    await h.fire(h.pending()[0]);
    assert.match(h.elements.consolJobSnapshot.innerHTML, /Compaction incomplete/);
    assert.match(h.elements.consolJobSnapshot.innerHTML, /&lt;failure&gt;/);
    assert.equal(h.pending().length, 0);
}
for (const change of ['close', 'replace']) {
    const h = harness(); const job = { status: 'running', job_id: 'j1' };
    h.setResponse(name => name === 'bank_consolidation_status' ? job : IDLE);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    await h.ctx.__live.inspectJob('j1'); const count = h.calls.length;
    if (change === 'close') h.ctx.closeModal(); else h.ctx.showModal('Other', 'unrelated');
    await h.fire(h.pending()[0]);
    assert.equal(h.calls.length, count, change + ' stops hidden inspector');
    assert.equal(h.pending().length, 0);
}
{
    const h = harness(); let failing = false;
    const job = { status: 'running', job_id: 'j1' };
    h.setResponse(name => name === 'bank_consolidation_status' ? (failing ? { status: 'error', message: 'read failed' } : job) : IDLE);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    await h.ctx.__live.inspectJob('j1'); const before = h.elements.consolJobSnapshot.innerHTML;
    failing = true; await h.fire(h.pending()[0]);
    assert.equal(h.elements.consolJobSnapshot.innerHTML, before, 'detail read error retains snapshot');
    assert.match(h.elements.consolJobFreshness.innerHTML, /stale/);
    assert.equal(h.pending()[0].ms, 30000, 'first failure backs off');
    await h.fire(h.pending()[0]); assert.equal(h.pending()[0].ms, 60000);
    failing = false; await h.fire(h.pending()[0]); assert.equal(h.pending()[0].ms, 15000);
}

// Job-first layout, valid terminal history, active exclusion and honest unknowns.
{
    const h = harness();
    h.setResponse(() => ({ status: 'ok', lanes: [{ space_id: 'demo', lane_state: 'running', queued_count: 1,
        running_job: { job_id: 'active', status: 'running', scope_label: 'All agents' },
        queued_jobs: [{ job_id: 'queued', status: 'queued', queue_position: 2, scope_label: 'Agent B' }],
        latest_jobs: [
            { job_id: 'active', status: 'succeeded', finished_at: '2026-09-09T14:00:00Z' },
            { job_id: 'old', status: 'succeeded', finished_at: '2026-09-09T10:00:00Z' },
            { job_id: 'new', status: 'failed', finished_at: '2026-09-09T12:00:00Z', result: { status: 'partial' }, error: '<unsafe>' },
            { job_id: 'new', status: 'failed', finished_at: '2026-09-09T12:00:00Z' },
            { job_id: 'bad-date', status: 'failed', finished_at: 'not-a-date' },
            { job_id: 'unknown', status: 'future_state' },
        ] }, { space_id: 'idle-space', lane_state: 'idle', queued_count: 0, latest_jobs: [] }] }));
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    const html = h.elements.consolLanes.innerHTML;
    assert.ok(html.includes('In progress') && html.includes('Recent history'), 'jobs replace the lane inventory');
    assert.equal(html.includes('Total spaces'), false, 'inventory metrics removed');
    assert.equal(html.includes('idle-space'), false, 'idle spaces do not produce rows');
    for (const id of ['active', 'queued', 'old', 'new', 'bad-date', 'unknown']) {
        assert.equal(html.split(`data-job-id="${id}"`).length - 1, 1, `job ${id} rendered once`);
    }
    assert.ok(html.indexOf('data-job-id="new"') < html.indexOf('data-job-id="old"'), 'history sorted by valid finish time');
    assert.ok(html.includes('Partial completion'), 'partial outcome visible');
    assert.ok(html.indexOf('data-job-id="old"') < html.indexOf('data-job-id="bad-date"'), 'undated terminals follow dated history');
    assert.ok(html.includes('Completion time unavailable'), 'invalid terminal timestamp is diagnosed');
    assert.ok(html.includes('Unknown') && html.includes('future_state'), 'unrecognized state never becomes idle');
    assert.ok(html.includes('&lt;unsafe&gt;') && !html.includes('<unsafe>'), 'job errors escaped');
    assert.equal(h.calls.length, 1, 'render never scans stale banks or performs per-space calls');
}

// Missing counters never manufacture a percentage or zero.
{
    const h = harness();
    for (const progress of [{ notes_total: 9 }, { notes_done: 3 }, { notes_total: 9, notes_done: NaN }, { notes_total: -9, notes_done: 1 }]) {
        const html = h.ctx.__live.progressBar(progress);
        assert.ok(html.includes('consol-bar-indeterminate'), 'incomplete progress remains indeterminate');
        assert.equal(html.includes('0/9'), false, 'missing done is not zero');
        assert.equal(html.includes('NaN'), false, 'non-finite counter never displayed');
    }
}

// Scoped load, picker from the aggregate response, and stale bulk capture stay scoped.
{
    const h = harness();
    h.setResponse(name => name === 'bank_stale_spaces'
        ? { status: 'ok', spaces: [{ space_id: 'demo', live_notes_count: 7 }, { space_id: 'other', live_notes_count: 9 }], total_spaces: 2, total_stale: 2, min_notes: 5, min_age_days: 5 }
        : { status: 'ok', lanes: [RUNNING.lanes[0], { space_id: 'other', running_job: { job_id: 'other-job', status: 'running' }, queued_count: 0 }] });
    const root = fakeElement();
    h.ctx.__live.render(root, { spaceId: 'demo' }, { identity: { token_hash: 'sha256:a', client_name: 'owner', permissions: ['manage'] }, epoch: 1 });
    await settle();
    assert.equal(h.calls[0].args.space_ids, 'demo', 'scoped entry uses the explicit target');
    assert.ok(root.innerHTML.includes('All spaces'), 'scope has a clear exit');
    assert.equal(h.elements.consolLanes.innerHTML.includes('other-job'), false, 'unrelated jobs excluded');
    h.actions['consol-picker']();
    assert.ok(h.modal.shows.at(-1).includes('demo'), 'picker consumes the last response');
    assert.equal(h.calls.length, 1, 'opening picker does not scan spaces');
    await h.actions['consol-stale-scan']();
    assert.equal(h.elements.consolStaleResults.innerHTML.includes('other'), false, 'stale rows scoped');
    await h.actions['consol-stale-all']();
    assert.ok(h.modal.shows.at(-1).includes('demo') && !h.modal.shows.at(-1).includes('<li><code>other'), 'bulk confirmation captures only the visible scope');
}

// Manual read during the same view's tick shares its outstanding cycle.
{
    const h = harness(); h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { epoch: 1, identity: {} }); await settle();
    let resolve; h.setResponse(() => new Promise(r => { resolve = r; }));
    const tick = h.fire(h.pending()[0]); await settle();
    const manual = h.ctx.__refresh.refresh(); await settle();
    assert.equal(h.calls.length, 2, 'same view has only one in-flight cycle');
    resolve(IDLE); await tick; await manual;
    assert.equal(h.pending().length, 0);
}

// Errors retain the last successful snapshot, clearly labeled with update time.
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    const before = h.elements.consolLanes.innerHTML;
    h.setResponse(() => ({ status: 'error', message: 'queue unavailable' }));
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.elements.consolLanes.innerHTML, before, 'refresh error retains the last rows');
    assert.ok(h.elements.consolFreshness.innerHTML.includes('stale') && h.elements.consolFreshness.innerHTML.includes('<time>'), 'stale data carries the last success timestamp');
}

// A response from a former session cannot repaint or arm a new session, even
// if the login overlay is already hidden again and route epoch did not change.
{
    const h = harness();
    let resolve;
    h.setResponse(() => new Promise(r => { resolve = r; }));
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    h.setGeneration(2);
    h.elements.consolLanes.innerHTML = 'new session';
    resolve(RUNNING); await settle();
    assert.equal(h.elements.consolLanes.innerHTML, 'new session', 'old-session response dropped');
    assert.equal(h.pending().length, 0, 'old-session response cannot schedule another read');
}

// A decoded comma/path/oversized target cannot widen a scoped request.
for (const spaceId of ['demo,other', '../other', 'a'.repeat(65)]) {
    const h = harness();
    const root = fakeElement();
    h.ctx.__live.render(root, { spaceId }, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    assert.equal(h.calls.length, 0, 'invalid scope makes no tool request');
    assert.ok(root.innerHTML.includes('Invalid space id'));
}

{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    h.ctx.AdminRouter.epoch = 2;
    h.ctx.document.hidden = true;
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.pending().length, 0, 'stale route never re-arms a hidden-tab timer');
}

for (const status of ['constructor', '__proto__', 'toString']) {
    const h = harness();
    h.ctx.__live.paintLanes({ lanes: [{ space_id: 'demo', lane_state: 'idle', latest_jobs: [{ job_id: 'unknown-prototype-key', status }] }] });
    assert.ok(h.elements.consolLanes.innerHTML.includes('Unknown'), 'prototype keys remain unknown states');
    assert.ok(h.elements.consolLanes.innerHTML.includes(`<code>${status}</code>`), 'raw unknown state stays inspectable');
}

// F4 sibling: a bounded tick has no evidence about denied spaces it did not read.
{
    const h = harness();
    h.setResponse(() => ({ ...RUNNING, denied_spaces: [
        { space_id: 'denied-a', message: 'first refusal' },
        { space_id: 'denied-b', message: 'other refusal' },
    ] }));
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    h.setResponse(() => RUNNING);
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.at(-1).args.space_ids, 'demo', 'tick only reads loaded lane IDs');
    assert.ok(h.elements.consolLanes.innerHTML.includes('denied-a') && h.elements.consolLanes.innerHTML.includes('denied-b'), 'scoped tick preserves unrequested access denials');
    h.setResponse(() => ({ ...RUNNING, denied_spaces: [{ space_id: 'denied-a', message: 'updated refusal' }] }));
    await h.ctx.__live.loadLanes(1, ['demo', 'denied-a']);
    assert.equal(h.elements.consolLanes.innerHTML.includes('first refusal'), false, 'requested denial is replaced');
    assert.ok(h.elements.consolLanes.innerHTML.includes('updated refusal') && h.elements.consolLanes.innerHTML.includes('other refusal'));
    h.setResponse(() => ({ ...RUNNING, lanes: [...RUNNING.lanes, { ...IDLE.lanes[0], space_id: 'denied-a' }] }));
    await h.ctx.__live.loadLanes(1, ['demo', 'denied-a']);
    assert.equal(h.elements.consolLanes.innerHTML.includes('updated refusal'), false, 'successful re-read clears only its target denial');
    assert.ok(h.elements.consolLanes.innerHTML.includes('other refusal'));
    h.setResponse(() => IDLE);
    await h.ctx.__live.loadLanes(1);
    assert.equal(h.elements.consolLanes.innerHTML.includes('other refusal'), false, 'full successful read replaces the entire denied set');
}

// F5: no lane visibility is distinct from visible, inactive spaces and denials.
{
    const h = harness();
    h.ctx.__live.paintLanes({ lanes: [], denied_spaces: [] });
    assert.ok(h.elements.consolLanes.innerHTML.includes('No spaces visible'), 'empty registry shows missing visibility');
    assert.equal(h.elements.consolLanes.innerHTML.includes('No jobs in progress'), false, 'missing visibility is not quiet activity');
    h.ctx.__live.paintLanes(IDLE);
    assert.ok(h.elements.consolLanes.innerHTML.includes('No jobs in progress'), 'visible inactive spaces retain the jobs empty state');
    assert.equal(h.elements.consolLanes.innerHTML.includes('No spaces visible'), false);
    h.ctx.__live.paintLanes({ lanes: [], denied_spaces: [{ space_id: 'denied', message: 'permission refused' }] });
    assert.ok(h.elements.consolLanes.innerHTML.includes('permission refused'), 'denied state keeps the real cause');
    assert.equal(h.elements.consolLanes.innerHTML.includes('No spaces visible'), false);
}

// F6: the expanded scan panel can collapse without another scan, and a late
// scan cannot update the closed panel or its cache. Reopening explicitly scans.
{
    const h = harness();
    h.setResponse(() => IDLE);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    let resolve;
    h.setResponse(() => new Promise(r => { resolve = r; }));
    h.actions['consol-stale-toggle']();
    assert.ok(h.elements.consolStale.innerHTML.includes('data-action="consol-stale-toggle"') && h.elements.consolStale.innerHTML.includes('Hide notes'), 'expanded panel offers a collapse control');
    assert.ok(h.elements.consolStale.innerHTML.includes('aria-expanded="true"'));
    assert.equal(h.calls.length, 2, 'opening performs exactly one scan');
    h.actions['consol-stale-toggle']();
    assert.equal(h.ctx.__live.state.staleMode, false);
    assert.ok(h.elements.consolStale.innerHTML.includes('Find notes') && !h.elements.consolStale.innerHTML.includes('consolStaleMinNotes'));
    assert.ok(h.elements.consolStale.innerHTML.includes('aria-expanded="false"'));
    assert.equal(h.calls.length, 2, 'collapsing performs no tool call');
    resolve({ status: 'ok', spaces: [{ space_id: 'demo' }], min_notes: 5, min_age_days: 5 });
    await settle();
    assert.equal(h.ctx.__live.state.staleData, null, 'late scan cannot repopulate a collapsed panel cache');
    h.setResponse(() => ({ status: 'ok', spaces: [], min_notes: 5, min_age_days: 5 }));
    h.actions['consol-stale-toggle']();
    await settle();
    assert.equal(h.calls.length, 3, 'reopening scans explicitly, with no lane reload');
    assert.equal(h.calls.at(-1).name, 'bank_stale_spaces');
}

console.log('admin consolidation live refresh runtime: ok');
