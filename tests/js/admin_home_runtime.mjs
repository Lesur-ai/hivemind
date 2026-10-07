import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';

const dashboardPath = process.argv[2];
const root = process.cwd();
const source = file => fs.readFileSync(path.join(root, 'src/live_mem/static/js/admin', file), 'utf8');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
function element() {
    const parts = new Map();
    return {
        innerHTML: '', textContent: '', dataset: {}, value: '', disabled: false, hidden: false, isConnected: true,
        clientWidth: 1100, style: { setProperty() {} }, children: [], parentNode: null, scrollTop: 0,
        querySelector(key) { if (!parts.has(key)) parts.set(key, element()); return parts.get(key); },
        querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {},
        appendChild(child) { return this.insertBefore(child, null); },
        insertBefore(child, before) {
            if (child === before) return child;
            if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1);
            const index = before ? this.children.indexOf(before) : this.children.length;
            this.children.splice(index, 0, child); child.parentNode = this; child.isConnected = true; return child;
        },
        remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; this.isConnected = false; },
        classList: { contains: () => true }, contains() { return false; },
    };
}

function harness(count = 21, enabled = false) {
    const actions = {}, views = {}, listeners = {}, elements = {}, calls = [], timers = [], storageWrites = [];
    const ids = ['dashHistoryGuarantee', 'dashRunningJobs', 'dashRecentJobs', 'dashSpacesTile', 'dashStartConsolidationBtn', 'dashLanesPanel', 'dashActivityPanel', 'dashLanesFreshness', 'dashInventoryFreshness', 'dashLatestSignal', 'dashCardScope', 'dashRecentSpaces', 'dashSpacesEmpty', 'dashNoteSpace', 'dashAddNotes', 'dashNotesPanel', 'dashNoteMessage', 'dashQueueWarnings', 'dashActivityEmpty', 'dashRecentEmpty', 'dashJobFreshness'];
    ids.forEach(id => { elements[id] = element(); });
    elements.adminModal = { style: { display: 'none' } };
    let generation = 1;
    let inventory = { status: 'ok', spaces: Array.from({ length: count }, (_, i) => ({ space_id: `space-${i + 1}`, last_consolidation: new Date(Date.UTC(2026, 8, 30, 8, 0, count - i)).toISOString(), consolidation_count: i + 1, total_notes_processed: (i + 1) * 12 })) };
    let job = null;
    let response = (name, args) => {
        if (name === 'space_list') return inventory;
        if (name === 'bank_consolidation_queues') return { status: 'ok', lanes: args.space_ids.split(',').map(space_id => ({ space_id, lane_state: job ? job.status : 'idle', running_job: job && job.status === 'running' ? { ...job, space_id } : null, queued_jobs: [], latest_jobs: job && job.status !== 'running' ? [{ ...job, space_id }] : [] })) };
        if (name === 'live_read') return { status: 'ok', notes: [{ filename: 'note.md', timestamp: '2026-09-29T10:00:00Z', agent: 'writer', category: 'observation', content: '<img onerror="bad()">' }], total: 1, has_more: false };
        return { status: 'error', message: 'unexpected ' + name };
    };
    const identity = { permissions: ['admin'], client_name: 'Alice' };
    const ctx = {
        console, Map, Set, URL, Object, Date, window: { innerHeight: 900, addEventListener(name, fn) { (listeners[name] ||= []).push(fn); } }, esc, icon: () => '', cache: {}, renderTimestamp: value => `<time>${esc(value)}</time>`, truncateMiddle: value => value,
        copyable: esc, statusDot: (kind, label) => `<span data-severity="${kind}">${esc(label)}</span>`, pageHeader: (title, actions) => `<header>${title}${actions}</header>`, panel: value => value,
        dataTable: (_, body) => body, serverMessage: esc, monoBlock: esc,
        renderAutoCompaction: result => result && result.auto_compaction ? `<section>${esc(result.auto_compaction.status)}</section>` : '',
        stateLoading: value => value || 'loading', stateError: value => esc(value.title), stateEmpty: value => esc(value.title), stateUnavailable: esc,
        _portalHealthFlight: null, _dashHealth: null, showToast() {}, checkPortalServices() { throw Error('health probe forbidden'); },
        showModal(_, html) { if (elements.dashJobSnapshot) elements.dashJobSnapshot.isConnected = false; elements.dashJobSnapshot = element(); elements.dashJobFreshness = element(); elements.adminModal.style.display = 'flex'; elements.adminModal.body = html; },
        openConsolidationLauncher(options) { ctx.launch = options; },
        currentSessionGeneration: () => generation, sessionGenerationIsCurrent: value => value === generation,
        _ctx: () => ({ identity, epoch: ctx.AdminRouter.epoch, sessionGeneration: generation }),
        AdminRouter: { epoch: 1, go() {} }, AdminViews: { register: (name, fn) => { views[name] = fn; } },
        registerAction: (name, fn) => { actions[name] = fn; },
        CustomEvent: function(type) { this.type = type; },
        document: { hidden: false, activeElement: null, getElementById: id => elements[id] || null, createElement: element,
            querySelector: () => null, querySelectorAll: () => [],
            addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
            dispatchEvent(event) { (listeners[event.type] || []).forEach(fn => fn(event)); } },
        localStorage: { getItem: () => JSON.stringify({ enabled, intervalSeconds: 15 }), setItem: (key, value) => storageWrites.push([key, value]) },
        setTimeout(fn, ms) { const timer = { fn, ms, cancelled: false }; timers.push(timer); return timer; },
        clearTimeout(timer) { if (timer) timer.cancelled = true; },
        callTool: async (name, args) => { calls.push({ name, args }); return response(name, args); },
    };
    vm.createContext(ctx);
    vm.runInContext(source('portal-refresh.js') + '\nglobalThis.refresh = PortalRefresh;', ctx);
    vm.runInContext(source('views-consolidation.js'), ctx);
    vm.runInContext(fs.readFileSync(dashboardPath, 'utf8'), ctx);
    ctx.refresh.beginSession();
    const content = element();
    const render = () => views.dashboard(content, {}, { epoch: ctx.AdminRouter.epoch, sessionGeneration: generation, identity });
    const pending = () => timers.filter(timer => !timer.cancelled && !timer.fired);
    const tick = async () => { const timer = pending()[0]; assert.ok(timer, 'eligible Home must keep following even idle'); timer.fired = true; await timer.fn(); await settle(); };
    return { ctx, actions, calls, elements, content, render, pending, tick, storageWrites,
        setResponse(fn) { const previous = response; response = (name, args) => fn(name, args, previous); },
        resize(width, height) { elements.dashRecentSpaces.clientWidth = width; ctx.window.innerHeight = height; (listeners.resize || []).forEach(fn => fn()); }, setJob(value) { job = value; }, nextSession() { generation++; }, setInventory(value) { inventory = value; } };
}

// Dashboard activity keeps operator summaries short while exposing exact
// server and maintenance detail through an explicit escaped disclosure.
{
    const h = harness(1); h.render(); await settle();
    const recentRow = state => {
        assert.equal(h.elements.dashRecentJobs.children.length, 1, `one ${state} recent activity row is rendered`);
        return h.elements.dashRecentJobs.children[0];
    };
    const diagnostic = `Agent instruction: call bank_consolidation_status. ${'<raw>'.repeat(200)}`;
    h.setJob({ job_id: 'concise-job', space_id: 'space-1', status: 'succeeded',
        scope_label: 'Agent: researcher', agent: 'researcher', requested_by: 'researcher',
        finished_at: '2026-09-29T10:00:00Z', message: diagnostic,
        result: { notes_total: 4, notes_processed: 4, auto_compaction: { status: 'not_needed' } } });
    await h.ctx.refresh.refresh(); await settle();
    const row = recentRow('successful');
    const meta = row.querySelector('[data-part="meta"]').innerHTML;
    const summary = row.querySelector('[data-part="outcome"]').innerHTML;
    assert.match(meta, /Agent: researcher/);
    assert.equal((meta.match(/researcher/g) || []).length, 1, 'identical agent/requester identity appears once');
    assert.match(summary, /Consolidation completed/);
    assert.match(summary, /4 of 4 notes processed/);
    assert.match(summary, /Automatic compaction: No oversized files/);
    const mainSummary = summary.split('<details', 1)[0];
    assert.doesNotMatch(mainSummary, /bank_consolidation_status|<raw>/, 'agent message stays out of the main reading path');
    assert.match(summary, /<details[\s\S]*Server and maintenance details[\s\S]*bank_consolidation_status/);
    assert.match(summary, /&lt;raw&gt;/, 'hostile server text remains literal and escaped');

    h.setJob({ job_id: 'distinct-job', space_id: 'space-1', status: 'failed',
        scope_label: 'All agents', agent: 'A&B', requested_by: 'operator',
        error: 'failure detail', finished_at: '2026-09-29T10:05:00Z',
        result: { auto_compaction: { status: 'partial', recovery_required: true, failure_reason: 'repair needed' } } });
    await h.ctx.refresh.refresh(); await settle();
    const failed = recentRow('failed');
    const failedMeta = failed.querySelector('[data-part="meta"]').innerHTML;
    const failedSummary = failed.querySelector('[data-part="outcome"]').innerHTML;
    assert.match(failedMeta, /All agents/);
    assert.match(failedMeta, /Agent A&amp;B/);
    assert.doesNotMatch(failedMeta, /A&amp;amp;B/, 'identity values are escaped exactly once');
    assert.match(failedMeta, /operator/);
    assert.match(failedSummary, /Consolidation failed/);
    assert.match(failedSummary, /Automatic compaction: Compaction incomplete/);
    assert.match(failedSummary, /Recovery required/);
    assert.match(failedSummary, /failure detail/);

    h.setJob({ job_id: 'missing-result', space_id: 'space-1', status: 'succeeded', finished_at: '2026-09-29T10:10:00Z' });
    await h.ctx.refresh.refresh(); await settle();
    const missing = recentRow('result-missing').querySelector('[data-part="outcome"]').innerHTML;
    assert.match(missing, /Consolidation completed/);
    assert.doesNotMatch(missing, /No oversized files|Files compacted/);
}

// R1 F1: real queue terminal shapes include partial failure and cancellation;
// reported batches do not establish that durable writes were committed.
{
    const h = harness(1); h.render(); await settle();
    const cases = [
        { status: 'partial', batches_completed: 2, batches_total: 4, notes_processed: 4, notes_total: 8 },
        { status: 'partial', batches_completed: 0, batches_total: 4, notes_processed: 0, notes_total: 8 },
        { status: 'partial', failure_reason: 'consolidation_cancelled' },
        { status: 'error', batches_completed: 0, batches_total: 4 },
        { status: 'error', partial: true, batches_completed: 0, batches_total: 4 },
    ];
    for (const result of cases) {
        const error = result.failure_reason === 'consolidation_cancelled'
            ? 'Consolidation was cancelled; bank recovery may be incomplete. Check the server logs before retrying.'
            : 'Consolidation failed. Check server logs.';
        h.setJob({ job_id: 'server-terminal', status: 'failed', finished_at: '2026-09-30T08:01:00Z', result, error });
        await h.ctx.refresh.refresh(); await settle();
        assert.equal(h.elements.dashRecentJobs.children.length, 1);
        const row = h.elements.dashRecentJobs.children[0];
        const outcome = row.querySelector('[data-part="outcome"]').innerHTML;
        assert.match(row.querySelector('[data-part="status"]').innerHTML, /data-severity="error"[^>]*>Failed/);
        assert.match(outcome, /Consolidation failed/);
        assert.ok(outcome.includes(esc(error)), 'exact server error is retained');
        const main = outcome.split('<details', 1)[0];
        if (result.status === 'partial') assert.match(main, /Partial result reported/);
        else assert.doesNotMatch(main, /Partial result reported/);
        if (result.status === 'partial' && result.batches_total) assert.match(main, new RegExp(`${result.batches_completed} of 4 batches reported completed`));
        else assert.doesNotMatch(main, /batches reported completed/);
        assert.doesNotMatch(main, /writes (?:applied|committed)|changes (?:applied|committed)|partially completed|notes processed/);
        assert.doesNotMatch(main, /role="alert"/, 'historical failure is visible without assertive announcements');
    }
    h.setJob({ job_id: 'legacy-partial', status: 'succeeded', result: { status: 'partial', notes_processed: 4, notes_total: 4 } });
    await h.ctx.refresh.refresh(); await settle();
    const legacy = h.elements.dashRecentJobs.children[0].querySelector('[data-part="outcome"]').innerHTML;
    assert.match(legacy, /Partial result reported/);
    assert.doesNotMatch(legacy, /Consolidation completed|notes processed/, 'partial result is not presented as completed even with legacy succeeded status');
    for (const [completed, total] of [[-1, 4], [5, 4], [0.5, 4], [0, '4'], [0, -1], [0, Number.MAX_SAFE_INTEGER + 1]]) {
        h.setJob({ job_id: 'server-terminal', status: 'failed', result: { status: 'partial', batches_completed: completed, batches_total: total } });
        await h.ctx.refresh.refresh(); await settle();
        const outcome = h.elements.dashRecentJobs.children[0].querySelector('[data-part="outcome"]').innerHTML;
        assert.match(outcome, /Partial result reported/);
        assert.doesNotMatch(outcome, /batches reported completed/, 'invalid counters cannot invent completed batches');
    }
}
// R1 F2: recovery severity is tested independently of the additional recovery dot.
{
    const h = harness(1); h.setJob({ job_id: 'recovery', status: 'succeeded', result: { auto_compaction: { status: 'not_needed', recovery_required: true } } });
    h.render(); await settle();
    const outcome = h.elements.dashRecentJobs.children[0].querySelector('[data-part="outcome"]').innerHTML;
    assert.match(outcome, /data-severity="error">Automatic compaction: No oversized files/);
}
// R1 F2: server overreturn must never expand the explicitly visible queue scope.
{
    const h = harness(1);
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? {
        status: 'ok', lanes: [...previous(name, args).lanes, { space_id: 'outside-scope', lane_state: 'running',
            running_job: { job_id: 'outside-job', status: 'running' }, latest_jobs: [{ job_id: 'outside-history', status: 'failed' }] }],
    } : previous(name, args));
    h.render(); await settle();
    assert.equal(h.elements.dashRunningJobs.children.length, 0, 'overreturned active job is excluded');
    assert.equal(h.elements.dashRecentJobs.children.length, 0, 'overreturned history is excluded');
    h.actions['dash-job']({ space: 'outside-scope', jobId: 'outside-job' });
    assert.equal(h.elements.adminModal.style.display, 'none', 'inspector has no overreturned job snapshot');
    assert.equal(h.calls.at(-1).args.space_ids, 'space-1');
}

// Lightweight metadata ranks all spaces before selecting responsive cards.
{
    const h = harness(); h.render(); await settle();
    assert.deepEqual(h.calls.map(call => call.name), ['space_list', 'bank_consolidation_queues']);
    assert.equal(h.calls[0].args.include_counts, false);
    assert.equal(h.calls[1].args.space_ids, 'space-1,space-2,space-3,space-4,space-5,space-6');
    assert.equal(h.elements.dashRecentSpaces.children.length, 6);
    assert.equal(h.ctx.cache.spaces, undefined, 'lightweight snapshot cannot poison shared cache');
    assert.doesNotMatch(h.content.innerHTML, /Previous spaces|Next spaces/);
    assert.equal(h.pending().length, 0);
    h.ctx.refresh.configure({ enabled: true, intervalSeconds: 15 });
    await h.tick();
    assert.equal(h.calls.filter(call => call.name === 'space_list').length, 2);
    h.resize(1100, 800); await settle();
    assert.equal(h.elements.dashRecentSpaces.children.length, 6, '800px viewport retains the minimum two rows');
    h.resize(700, 1024); await settle();
    assert.equal(h.elements.dashRecentSpaces.children.length, 4);
    assert.equal(h.calls.at(-1).args.space_ids, 'space-1,space-2,space-3,space-4');
    h.resize(340, 844); await settle();
    assert.equal(h.elements.dashRecentSpaces.children.length, 2);
    h.resize(1500, 4000); await settle();
    assert.equal(h.elements.dashRecentSpaces.children.length, 12, 'hard maximum');
    h.setInventory({ status: 'ok', spaces: [
        { space_id: 'never' }, { space_id: 'bad', last_consolidation: 'yesterday' },
        { space_id: 'bad-day', last_consolidation: '2026-02-30T00:00:00Z' },
        { space_id: 'no-zone', last_consolidation: '2026-09-30T08:00:00' },
        { space_id: 'zeta', last_consolidation: '2026-09-30T08:00:00Z' },
        { space_id: 'alpha', last_consolidation: '2026-09-30T10:00:00+02:00', consolidation_count: -1 },
        { space_id: 'latest', last_consolidation: '2026-09-30T11:00:00Z', description: '<unsafe>' },
    ] });
    await h.ctx.refresh.refresh(); await settle();
    assert.equal(h.calls.at(-1).args.space_ids, 'latest,alpha,zeta', 'timestamp descending and stable ID ties');
    assert.match(h.elements.dashRecentSpaces.children[0].innerHTML, /&lt;unsafe&gt;/);
    assert.match(h.elements.dashRecentSpaces.children[1].innerHTML, /Unavailable/);
    assert.match(h.elements.dashRecentSpaces.children[1].innerHTML, /Lifetime/);
    const snapshot = h.elements.dashRecentSpaces.children[0];
    const regionSnapshot = ['dashLatestSignal', 'dashSpacesTile', 'dashSpacesEmpty'].map(id => h.elements[id].innerHTML);
    const successStamp = h.elements.dashInventoryFreshness.innerHTML;
    assert.match(regionSnapshot[0], /latest/); assert.match(successStamp, /Updated <time>[^<]+<\/time>/);
    h.setResponse((name, args, previous) => name === 'space_list' ? { status: 'error', message: 'metadata unavailable' } : previous(name, args));
    await assert.rejects(h.ctx.refresh.refresh()); await settle();
    assert.equal(h.elements.dashRecentSpaces.children[0], snapshot);
    assert.deepEqual(['dashLatestSignal', 'dashSpacesTile', 'dashSpacesEmpty'].map(id => h.elements[id].innerHTML), regionSnapshot, 'dated metadata regions survive a subsequent error');
    assert.ok(h.elements.dashInventoryFreshness.innerHTML.startsWith(successStamp), 'dated inventory success remains visible');
    assert.match(h.elements.dashInventoryFreshness.innerHTML, /metadata unavailable/);
}
// Empty or never-consolidated inventory never scans queues globally; still follows metadata.
for (const count of [0, 1, 20]) {
    const h = harness(count); h.render(); await settle();
    assert.equal(h.calls.length, count ? 2 : 1);
    if (count) assert.equal(h.calls[1].args.space_ids.split(',').length, Math.min(count, 6));
}
{
    const h = harness(1, true); h.setInventory({ status: 'ok', spaces: [{ space_id: 'new-space' }] });
    h.render(); await settle();
    assert.equal(h.calls.length, 1);
    assert.match(h.elements.dashSpacesEmpty.innerHTML, /No consolidations yet/);
    await h.tick();
    assert.deepEqual(h.calls.map(call => call.name), ['space_list', 'space_list']);
}

// Idle Home continues following even when nested agent advice says not to poll.
{
    const h = harness(1, true); h.render(); await settle();
    await h.tick();
    h.setJob({ job_id: 'j1', status: 'running', polling: { recommended: false }, started_at: '2026-09-29T10:00:00Z', progress: { notes_done: 1, notes_total: 4 } });
    await h.tick();
    const row = h.elements.dashRunningJobs.children[0]; assert.ok(row, 'external job discovered from idle');
    const details = row.querySelector('button');
    h.actions['dash-job']({ space: 'space-1', jobId: 'j1' });
    const snapshot = h.elements.dashJobSnapshot;
    assert.match(snapshot.innerHTML, /1\/4 notes/);
    h.setJob({ job_id: 'j1', status: 'running', progress: { notes_done: 2, notes_total: 4 } });
    await h.tick();
    assert.equal(h.elements.dashRunningJobs.children[0], row, 'row identity survives progress');
    assert.equal(row.querySelector('button'), details, 'Details focus target survives progress');
    assert.match(snapshot.innerHTML, /2\/4 notes/);
    h.setJob(null); await h.tick();
    assert.match(snapshot.innerHTML, /2\/4 notes/, 'a missing job retains its last snapshot');
    assert.match(h.elements.dashJobFreshness.innerHTML, /no longer returned/);
    h.setJob({ job_id: 'j1', status: 'succeeded', finished_at: '2026-09-29T10:04:00Z', result: { notes_total: 4, notes_processed: 4 } });
    await h.tick();
    assert.match(snapshot.innerHTML, /Notes processed/);
    const terminal = snapshot.innerHTML;
    h.setJob({ job_id: 'j1', status: 'running', progress: { notes_done: 0, notes_total: 4 } });
    await h.tick(); assert.equal(snapshot.innerHTML, terminal, 'terminal detail stops changing while Home continues');
    assert.ok(h.calls.every(call => ['space_list', 'bank_consolidation_queues'].includes(call.name)), 'no detail status calls');
}

// Three explicit note spaces make at most five reads per metadata tick; note DOM stays in place.
{
    const h = harness(4, true); h.render(); await settle();
    assert.equal(h.calls.some(call => call.name === 'live_read'), false);
    h.elements.dashNoteSpace.value = 'outside'; h.actions['dash-add-notes'](); await settle();
    assert.equal(h.calls.length, 2, 'unloaded selection is rejected');
    for (let i = 1; i <= 3; i++) { h.elements.dashNoteSpace.value = `space-${i}`; h.actions['dash-add-notes'](); await settle(); }
    assert.equal(h.elements.dashNotesPanel.children.length, 3);
    assert.equal(h.elements.dashAddNotes.disabled, true);
    h.elements.dashNoteSpace.value = 'space-4'; h.actions['dash-add-notes'](); await settle();
    assert.equal(h.elements.dashNotesPanel.children.length, 3, 'fourth selection rejected');
    const card = h.elements.dashNotesPanel.children[0], list = card.querySelector('[data-part="list"]');
    const note = list.children[0]; assert.ok(note);
    assert.equal(note.querySelector('[data-part="content"]').textContent, '<img onerror="bad()">', 'hostile notes are text');
    const content = note.querySelector('[data-part="content"]');
    const originalText = content.textContent; let textWrites = 0;
    Object.defineProperty(content, 'textContent', { get: () => originalText, set() { textWrites++; } });
    const before = h.calls.length; await h.tick();
    assert.equal(textWrites, 0, 'unchanged text selection is not replaced');
    assert.equal(h.calls.length - before, 5, 'tick is metadata, one queue read and three feeds');
    assert.equal(list.children[0], note, 'unchanged note node survives refresh');
    assert.ok(h.calls.filter(call => call.name === 'live_read').every(call => call.args.limit === 20));
    assert.equal(h.calls.filter(call => call.name === 'space_list').length, 2);
    h.actions['dash-remove-notes']({ space: 'space-1' });
    const afterRemoval = h.calls.length; await h.tick();
    assert.equal(h.calls.length - afterRemoval, 4);
    assert.ok(h.calls.slice(afterRemoval).every(call => call.args.space_id !== 'space-1'));
    assert.equal(h.storageWrites.length, 0, 'selection and snapshots never enter browser storage');
}

// Incomplete or denied queue coverage is never described as idle.
{
    const h = harness(21);
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? { status: 'error', message: 'queues unavailable' } : previous(name, args));
    h.render(); await settle();
    assert.match(h.elements.dashRecentEmpty.innerHTML, /not loaded/i);
    assert.match(h.elements.dashActivityEmpty.innerHTML, /not loaded/i);
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? { status: 'ok', lanes: [], denied_spaces: [{ space_id: 'space-1', message: 'Access revoked' }] } : previous(name, args));
    await h.ctx.refresh.refresh();
    assert.match(h.elements.dashQueueWarnings.innerHTML, /Access revoked/);
    assert.match(h.elements.dashQueueWarnings.innerHTML, /space-2.*unavailable/);
    assert.doesNotMatch(h.elements.dashActivityEmpty.innerHTML, /No jobs in progress/);
}

// Unknown lane state and corrupt metadata never invent idle or a first-use story.
{
    const h = harness(1); h.render(); await settle();
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? { status: 'ok', lanes: [{ space_id: 'space-1', lane_state: 'unknown' }] } : previous(name, args));
    await h.ctx.refresh.refresh();
    assert.match(h.elements.dashActivityEmpty.innerHTML, /unavailable/i);
    assert.doesNotMatch(h.elements.dashActivityEmpty.innerHTML, /No jobs in progress/);
    h.setInventory({ status: 'ok', spaces: [{ space_id: 'space-1', last_consolidation: 'bad-date' }] });
    const before = h.calls.length;
    await h.ctx.refresh.refresh();
    assert.deepEqual(h.calls.slice(before).map(call => call.name), ['space_list']);
    assert.match(h.elements.dashSpacesEmpty.innerHTML, /Consolidation dates unavailable/);
    assert.doesNotMatch(h.elements.dashSpacesEmpty.innerHTML, /No consolidations yet/);
}
// Positive lifetime counters prove prior activity even when no date is supplied.
for (const key of ['consolidation_count', 'total_notes_processed']) {
    for (const date of [undefined, null, '']) {
        const h = harness(1);
        h.setInventory({ status: 'ok', spaces: [{ space_id: 'undated-history', last_consolidation: date, [key]: 3 }] });
        h.render(); await settle();
        assert.match(h.elements.dashLatestSignal.innerHTML, /Consolidation dates unavailable/);
        assert.match(h.elements.dashSpacesEmpty.innerHTML, /Consolidation dates unavailable/);
        assert.doesNotMatch(h.elements.dashLatestSignal.innerHTML, /first consolidation/);
        assert.doesNotMatch(h.elements.dashSpacesEmpty.innerHTML, /No consolidations yet/);
        assert.equal(h.elements.dashRecentSpaces.children.length, 0, 'a counter cannot invent a consolidation date');
        assert.deepEqual(h.calls.map(call => call.name), ['space_list'], 'undated spaces never expand the queue scope');
        h.setInventory({ status: 'ok', spaces: [
            { space_id: 'undated-history', [key]: 3 },
            { space_id: 'dated', last_consolidation: '2026-09-30T08:00:00Z' },
        ] });
        await h.ctx.refresh.refresh();
        assert.equal(h.calls.at(-1).args.space_ids, 'dated');
        assert.match(h.elements.dashCardScope.textContent, /1 consolidation date\(s\) unavailable/);
    }
}
{
    const h = harness(1);
    h.setInventory({ status: 'ok', spaces: [{ space_id: 'first-use', last_consolidation: null, consolidation_count: 0, total_notes_processed: 0 }] });
    h.render(); await settle();
    assert.match(h.elements.dashSpacesEmpty.innerHTML, /No consolidations yet/);
    assert.match(h.elements.dashLatestSignal.innerHTML, /first consolidation/);
}

// Hidden resizes read nothing; route ownership prevents a retired view resizing.
{
    const h = harness(21); h.render(); await settle();
    const before = h.calls.length; h.ctx.document.hidden = true;
    h.resize(340, 844); await settle();
    assert.equal(h.calls.length, before);
    assert.equal(h.elements.dashRecentSpaces.children.length, 6);
    h.ctx.document.hidden = false;
    h.ctx.document.dispatchEvent({ type: 'visibilitychange' }); await settle();
    assert.equal(h.elements.dashRecentSpaces.children.length, 2);
    const end = h.calls.length; h.ctx.AdminRouter.epoch++;
    h.resize(1500, 4000); await settle();
    assert.equal(h.calls.length, end);
    assert.equal(h.elements.dashRecentSpaces.children.length, 2);
}

// Partial read failures retain each affected region's own data and success time.
{
    const h = harness(1, true); h.render(); await settle();
    h.elements.dashNoteSpace.value = 'space-1'; h.actions['dash-add-notes'](); await settle();
    const card = h.elements.dashNotesPanel.children[0];
    const note = card.querySelector('[data-part="list"]').children[0];
    const notesStamp = card.querySelector('[data-part="freshness"]').innerHTML;
    h.setResponse((name, args, previous) => name === 'live_read' ? { status: 'error', message: 'notes unavailable' } : previous(name, args));
    await h.tick();
    assert.equal(card.querySelector('[data-part="list"]').children[0], note);
    assert.ok(card.querySelector('[data-part="freshness"]').innerHTML.startsWith(notesStamp));
    assert.match(card.querySelector('[data-part="freshness"]').innerHTML, /notes unavailable/);
    assert.doesNotMatch(h.elements.dashLanesFreshness.innerHTML, /failed|unavailable/);
    assert.equal(h.pending()[0].ms, 30000, 'partial failure enters shared backoff');
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? Promise.reject(Error('429')) : name === 'live_read' ? { status: 'ok', notes: [], has_more: false } : previous(name, args));
    const activityStamp = h.elements.dashLanesFreshness.innerHTML;
    await h.tick();
    assert.ok(h.elements.dashLanesFreshness.innerHTML.startsWith(activityStamp));
    assert.match(h.elements.dashLanesFreshness.innerHTML, /429/);
    assert.doesNotMatch(card.querySelector('[data-part="freshness"]').innerHTML, /notes unavailable/);
    assert.equal(h.pending()[0].ms, 60000);
}

// A slow read is single-flight, and expiry prevents late paint or further reads.
{
    const h = harness(21, true); h.render(); await settle();
    const original = h.elements.dashLanesFreshness.innerHTML;
    let finish;
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? new Promise(resolve => { finish = resolve; }) : previous(name, args));
    const timer = h.pending()[0]; timer.fired = true; timer.fn(); await settle();
    const count = h.calls.length;
    h.ctx.refresh.refresh(); h.resize(340, 844); await settle();
    assert.equal(h.calls.length, count, 'no overlap or scope change during slow read');
    h.nextSession();
    finish({ status: 'ok', lanes: [{ space_id: 'space-1', lane_state: 'running', running_job: { job_id: 'late-session-job', status: 'running' } }] }); await settle();
    assert.equal(h.elements.dashRunningJobs.children.length, 0, 'late response cannot introduce a job');
    assert.equal(h.elements.dashLanesFreshness.innerHTML, original, 'old session cannot paint');
    assert.equal(h.pending().length, 0);
}
{
    const h = harness(1); let finish;
    h.setResponse(() => new Promise(resolve => { finish = resolve; }));
    h.render(); await settle(); h.nextSession();
    finish({ status: 'ok', spaces: [{ space_id: 'private-space' }] }); await settle();
    assert.equal(h.calls.length, 1, 'no dependent queue request after session expiry');
    assert.doesNotMatch(h.elements.dashNoteSpace.innerHTML, /private-space/, 'expired inventory cannot paint choices');
    assert.equal(h.ctx.cache.spaces, undefined);
}

// Old-session note choices are discarded, and a new empty inventory never scans globally.
{
    const h = harness(1); h.render(); await settle();
    h.elements.dashNoteSpace.value = 'space-1'; h.actions['dash-add-notes'](); await settle();
    h.nextSession(); h.ctx.refresh.beginSession(); h.ctx.AdminRouter.epoch++; h.render(); await settle();
    const before = h.calls.length;
    await h.ctx.refresh.refresh(); await settle();
    assert.equal(h.calls.slice(before).some(call => call.name === 'live_read'), false);
    h.setInventory({ status: 'ok', spaces: [] });
    const end = h.calls.length; await h.ctx.refresh.refresh(); await settle();
    assert.deepEqual(h.calls.slice(end).map(call => call.name), ['space_list']);
}

// Display limit remains20 even if a server returns more; diagnostics are not invented.
{
    const h = harness(1); h.render(); await settle();
    assert.equal(h.elements.dashHistoryGuarantee.innerHTML, '');
    h.setResponse((name, args, previous) => name === 'live_read' ? { status: 'ok', notes: Array.from({ length: 21 }, (_, i) => ({ filename: `n${i}.md`, content: `note ${i}` })), has_more: true } : previous(name, args));
    h.elements.dashNoteSpace.value = 'space-1'; h.actions['dash-add-notes'](); await settle();
    const card = h.elements.dashNotesPanel.children[0];
    assert.equal(card.querySelector('[data-part="list"]').children.length, 20);
    assert.match(card.querySelector('[data-part="bound"]').textContent, /More notes/);
}

// A route switch or disabling auto before completion cannot repaint the old scope.
for (const cancel of ['route', 'off']) {
    const h = harness(1, true); h.render(); await settle();
    const original = h.elements.dashLanesFreshness.innerHTML; let finish;
    h.setResponse((name, args, previous) => name === 'bank_consolidation_queues' ? new Promise(resolve => { finish = resolve; }) : previous(name, args));
    const timer = h.pending()[0]; timer.fired = true; timer.fn(); await settle();
    if (cancel === 'route') { h.ctx.AdminRouter.epoch++; h.ctx.refresh.clearRoute(); }
    else h.ctx.refresh.configure({ enabled: false, intervalSeconds: 15 });
    finish({ status: 'ok', lanes: [{ space_id: 'space-1', lane_state: 'running', running_job: { job_id: 'late-session-job', status: 'running' } }] }); await settle();
    assert.equal(h.elements.dashRunningJobs.children.length, 0, 'late response cannot introduce a job');
    assert.equal(h.elements.dashLanesFreshness.innerHTML, original);
    assert.equal(h.pending().length, 0);
}

// A view may render after whoami failed: never bypass the inactive controller.
{
    const h = harness(1); h.ctx.refresh.endSession(); h.render(); await settle();
    assert.equal(h.calls.length, 0);
    assert.match(h.elements.dashActivityEmpty.innerHTML, /Refresh unavailable\. Sign out and sign in again\./);
    assert.equal(h.ctx.refresh.state().available, false);
    assert.equal(h.pending().length, 0);
}
// A failed initial inventory is unavailable, not a fake empty count or perpetual loading.
{
    const h = harness(1); h.setResponse(() => ({ status: 'error', message: 'Inventory unavailable' }));
    h.render(); await settle();
    assert.deepEqual(h.calls.map(call => call.name), ['space_list']);
    assert.match(h.elements.dashInventoryFreshness.innerHTML, /Inventory unavailable/);
    assert.match(h.elements.dashSpacesTile.innerHTML, /Couldn.t load spaces/);
    assert.match(h.elements.dashLanesFreshness.innerHTML, /awaits a successful/);
}
console.log('admin Home runtime: ok');
