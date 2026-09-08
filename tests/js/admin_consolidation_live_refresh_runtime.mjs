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
    const elements = { consolLanes: fakeElement(), consolSubtitle: fakeElement(), consolStale: fakeElement(), adminModal: modal };
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
        registerAction() {}, showToast() {}, showDestructiveModal() {},
        showModal(title, body) { modal.style.display = 'flex'; modal.shows.push(String(body)); },
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
        'globalThis.__live = { render, loadLanes, inspectJob, state };',
    );
    assert.notEqual(instrumented, source, 'consolidation instrumentation anchor missing');
    vm.runInContext(instrumented, ctx, { filename: consolidationPath });
    assert.ok(ctx.__live, 'consolidation instrumentation failed');
    const pending = () => timers.filter(t => !t.cancelled && !t.fired);
    const fire = async t => { t.fired = true; await t.fn(); await new Promise(r => setImmediate(r)); };
    return { ctx, timers, calls, modal, pending, fire, setResponse(fn) { nextResponse = fn; } };
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
    assert.ok(sub.includes('live · refreshes every 60 s while a job runs'), 'the subtitle announces the live refresh');
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
    assert.equal(h.ctx.document.getElementById('consolSubtitle').innerHTML.includes('live ·'), false, 'no live marker when idle');
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

console.log('admin consolidation live refresh runtime: ok');
