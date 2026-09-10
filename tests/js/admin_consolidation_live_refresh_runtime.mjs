// ADMIN_CONSOLE_DESIGN §5.5.1 (owner decision 2026-09-05) — runtime proof of the
// bounded live refresh of the Consolidation view: while a lane shows a running
// or queued job the lanes reload every 60 s; otherwise nothing is scheduled.
// The tick drops on a route-epoch change or a lost session, re-arms without a
// network call while the tab is hidden, and a new render cancels a pending
// timer. The job inspector does the same for a running/queued job and stops on
// a terminal status, a closed modal or a superseded modal instance.
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

function fakeElement() { return { innerHTML: '', classList: { contains: () => true } }; }

function harness() {
    const timers = [];            // scheduled callbacks: {id, fn, ms, cancelled}
    const calls = [];             // callTool invocations: {name, args}
    const modal = { style: { display: 'none' }, shows: [] };
    let nextResponse = () => ({});
    const actions = {};
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
        showModal(title, body, confirmLabel, onConfirm) { modal.style.display = 'flex'; modal.shows.push(String(body)); modal.confirm = onConfirm; },
        closeModal() { modal.style.display = 'none'; },
        callTool: async (name, args) => { calls.push({ name, args }); return nextResponse(name, args); },
        AdminViews: { register() {} },
        AdminRouter: { epoch: 1, go() {}, refresh() {}, current: () => ({}) },
        document: {
            hidden: false,
            addEventListener() {},
            querySelector() { return null; },
            querySelectorAll() { return []; },
            getElementById(id) { return elements[id] || null; },
        },
        window: {},
        setTimeout(fn, ms) { const id = timers.length + 1; timers.push({ id, fn, ms, cancelled: false }); return id; },
        clearTimeout(id) { const t = timers.find(x => x.id === id); if (t) t.cancelled = true; },
    };
    vm.createContext(ctx);
    const source = fs.readFileSync(consolidationPath, 'utf8');
    const instrumented = source.replace(
        "AdminViews.register('consolidation', render);",
        'globalThis.__live = { render, loadLanes, inspectJob, state, paintLanes, progressBar };',
    );
    assert.notEqual(instrumented, source, 'consolidation instrumentation anchor missing');
    vm.runInContext(instrumented, ctx, { filename: consolidationPath });
    assert.ok(ctx.__live, 'consolidation instrumentation failed');
    const pending = () => timers.filter(t => !t.cancelled && !t.fired);
    const fire = async t => { t.fired = true; await t.fn(); await new Promise(r => setImmediate(r)); };
    return { ctx, elements, actions, setGeneration(g) { generation = g; }, timers, calls, modal, pending, fire, setResponse(fn) { nextResponse = fn; } };
}

const RUNNING = { status: 'ok', parallelism_model: 'one_worker_per_space', service_config: { batch_size: 2 },
    lanes: [{ space_id: 'demo', lane_state: 'running', running_job: { job_id: 'j1' }, queued_count: 0, latest_jobs: [] }] };
const QUEUED = { status: 'ok', lanes: [{ space_id: 'demo', lane_state: 'queued', running_job: null, queued_count: 1, latest_jobs: [] }] };
const IDLE = { status: 'ok', lanes: [{ space_id: 'demo', lane_state: 'idle', running_job: null, queued_count: 0, latest_jobs: [] }] };

const settle = () => new Promise(r => setImmediate(r));

// ── 1. a running lane schedules exactly one 60 s reload; firing it reloads ──
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    assert.equal(h.calls.filter(c => c.name === 'bank_consolidation_queues').length, 1, 'initial load');
    assert.equal(h.calls[0].args.space_ids, '', 'the full load lets the server resolve the visible spaces');
    assert.equal(h.pending().length, 1, 'exactly one live timer while a job runs');
    assert.equal(h.pending()[0].ms, 60000, 'live period is one minute (owner arbitration 2026-09-05)');
    assert.ok(h.ctx.__live.state.liveTimer !== null, 'the pending timer is tracked in view state');
    const sub = h.ctx.document.getElementById('consolSubtitle').innerHTML;
    assert.ok(sub.includes('Live · refreshes every 60 s while a job runs'), 'the subtitle announces the live refresh');
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.filter(c => c.name === 'bank_consolidation_queues').length, 2, 'the tick reloads the lanes');
    assert.equal(h.calls[1].args.space_ids, 'demo', 'the tick re-reads the painted lanes by explicit id — in-memory path, never the storage scan');
    assert.equal(h.pending().length, 1, 'still running → re-armed once');
}

// ── 1b. the tick carries every painted lane (idle ones included), in paint order ──
{
    const h = harness();
    const TWO = { status: 'ok', lanes: [
        { space_id: 'alpha', lane_state: 'idle', running_job: null, queued_count: 0, latest_jobs: [] },
        { space_id: 'beta', lane_state: 'running', running_job: { job_id: 'j2' }, queued_count: 0, latest_jobs: [] },
    ] };
    h.setResponse(() => TWO);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls[1].args.space_ids, 'alpha,beta', 'idle lanes stay watched: a job enqueued on them is seen by the next tick');
}

// ── 2. a queued lane also arms; an idle lane arms nothing and stops the loop ──
{
    const h = harness();
    let response = QUEUED;
    h.setResponse(() => response);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    assert.equal(h.pending().length, 1, 'queued job arms the live refresh');
    response = IDLE;
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.pending().length, 0, 'idle lanes: nothing scheduled, the loop stops');
    assert.equal(h.ctx.__live.state.liveTimer, null);
    assert.equal(h.ctx.document.getElementById('consolSubtitle').innerHTML.includes('Live ·'), false, 'no live marker when idle');
}

// ── 3. a route-epoch change or a lost session drops the tick without a call ──
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    h.ctx.AdminRouter.epoch = 2;
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.length, 1, 'stale epoch: no reload');
    assert.equal(h.pending().length, 0, 'stale epoch: not re-armed');
}
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    // session lost: the shell's login overlay is visible (classList.contains('hidden') → false)
    h.ctx.document.getElementById = id => id === 'loginOverlay' ? { classList: { contains: () => false } } : fakeElement();
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.length, 1, 'lost session: no reload');
    assert.equal(h.pending().length, 0, 'lost session: not re-armed');
}

// ── 4. hidden tab: the tick re-arms without a network call ─────────────────
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    h.ctx.document.hidden = true;
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.length, 1, 'hidden tab: no reload');
    assert.equal(h.pending().length, 1, 'hidden tab: re-armed');
    h.ctx.document.hidden = false;
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.length, 2, 'visible again: reload resumes');
}

// ── 5. a new render cancels the pending timer (no duplicate loops) ─────────
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    const first = h.pending()[0];
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle(); await settle();
    assert.ok(first.cancelled, 'the previous timer is cancelled by the new render');
    assert.equal(h.pending().length, 1, 'exactly one live timer after re-render');
}

// ── 6. job inspector: running → 60 s re-read repainting the same modal; terminal → stop ──
{
    const h = harness();
    let job = { status: 'running', job_id: 'j1', space_id: 'demo', scope_label: 'All agents', progress: { phase: 'running', batch_size: 2, notes_total: 4, notes_done: 2, batches_total: 2, batches_done: 1, current_batch: 2 } };
    h.setResponse(name => name === 'bank_consolidation_status' ? job : IDLE);
    await h.ctx.__live.inspectJob('j1'); await settle();
    assert.equal(h.modal.style.display, 'flex');
    assert.equal(h.pending().length, 1, 'running job: one 60 s re-read armed');
    assert.equal(h.pending()[0].ms, 60000);
    job = { ...job, status: 'succeeded', result: { status: 'ok', notes_total: 4, notes_processed: 4, notes_deleted: 4, notes_remaining: 0, bank_files_updated: 1, batches_completed: 2, batches_total: 2 } };
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.filter(c => c.name === 'bank_consolidation_status').length, 2, 'the tick re-reads the job');
    assert.ok(h.modal.shows.length >= 2, 'the modal is repainted with the new payload');
    assert.equal(h.pending().length, 0, 'terminal job: no further re-read');
}

// ── 7. job inspector: closed or superseded modal stops the loop ────────────
{
    const h = harness();
    const running = { status: 'running', job_id: 'j1', space_id: 'demo', scope_label: 'All agents', progress: { phase: 'running' } };
    h.setResponse(() => running);
    await h.ctx.__live.inspectJob('j1'); await settle();
    assert.equal(h.pending().length, 1);
    h.ctx.closeModal();
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.calls.filter(c => c.name === 'bank_consolidation_status').length, 1, 'closed modal: no re-read');
    assert.equal(h.pending().length, 0, 'closed modal: not re-armed');
}
{
    const h = harness();
    const running = { status: 'running', job_id: 'j1', space_id: 'demo', scope_label: 'All agents', progress: { phase: 'running' } };
    h.setResponse(() => running);
    await h.ctx.__live.inspectJob('j1'); await settle();
    const first = h.pending()[0];
    await h.ctx.__live.inspectJob('j2'); await settle();   // a newer modal instance supersedes j1
    await h.fire(first); await settle();
    const statusCalls = h.calls.filter(c => c.name === 'bank_consolidation_status');
    assert.equal(statusCalls.filter(c => c.args.job_id === 'j1').length, 1, 'superseded modal: j1 is never re-read');
    assert.equal(h.pending().length, 1, 'only the newest modal keeps its loop');
}

// ── 8. job inspector: a typed error or a thrown re-read keeps the snapshot and re-arms ──
{
    const h = harness();
    const running = { status: 'running', job_id: 'j1', space_id: 'demo', scope_label: 'All agents', progress: { phase: 'running' } };
    let response = () => running;
    h.setResponse((name, args) => response(name, args));
    await h.ctx.__live.inspectJob('j1'); await settle();
    const shownBefore = h.modal.shows.length;
    response = () => ({ status: 'error', message: 'registry unavailable' });
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.modal.shows.length, shownBefore, 'typed error: the last good snapshot stays on screen');
    assert.ok(h.elements.consolJobFreshness.innerHTML.includes('stale') && h.elements.consolJobFreshness.innerHTML.includes('<time>'), 'job refresh error has last-success timestamp');
    assert.equal(h.pending().length, 1, 'typed error: re-armed');
    response = () => { throw new Error('network'); };
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.modal.shows.length, shownBefore, 'thrown re-read: the last good snapshot stays on screen');
    assert.equal(h.pending().length, 1, 'thrown re-read: re-armed');
    response = () => ({ status: 'rate_limited', message: 'slow down' });
    await h.fire(h.pending()[0]); await settle();
    assert.equal(h.modal.shows.length, shownBefore, 'sentinel: no repaint');
    assert.equal(h.pending().length, 0, 'sentinel: the loop ends');
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

// No overlapping lane requests, even when manual refresh arrives during a tick.
{
    const h = harness();
    h.setResponse(() => RUNNING);
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 1 });
    await settle();
    let resolve;
    h.setResponse(() => new Promise(r => { resolve = r; }));
    const tick = h.fire(h.pending()[0]);
    await settle();
    h.ctx.AdminRouter.epoch = 2;
    h.ctx.__live.render(fakeElement(), {}, { identity: { token_hash: 'sha256:a' }, epoch: 2 });
    await settle();
    assert.equal(h.calls.length, 2, 'new render waits for the outstanding request');
    h.setResponse(() => IDLE);
    resolve(RUNNING);
    await tick; await settle();
    assert.equal(h.calls.length, 3, 'latest full refresh starts after the old request settles');
    assert.equal(h.pending().length, 0, 'only current response can re-arm the timer');
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
