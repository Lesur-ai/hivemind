// Spaces inventory/activity contract: real source with a deterministic clock.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const sourcePath = process.argv[2];
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
function element() { return { dataset: {}, innerHTML: '', textContent: '', disabled: false, isConnected: true, addEventListener() {}, value: '' }; }
function harness() {
    const timers = [], calls = [], actions = {}, cells = [];
    const elements = Object.fromEntries(['spacesToolbar', 'spacesTableWrap', 'spacesFreshness', 'spacesQueueWarnings', 'spacesRefreshBtn', 'spacesSearch'].map(id => [id, element()]));
    let response = () => ({}), generation = 1;
    const ctx = {
        esc, icon: () => '', cache: {}, fmtTimestamp: v => ({ text: v, title: v }), renderTimestamp: v => `<time>${esc(v)}</time>`,
        statusDot: (severity, label) => `<span>${esc(label)}</span>`, pageHeader: (t, a) => `<header>${esc(t)}${a}</header>`,
        dataTable: (headers, rows) => `<table><thead>${headers.join('|')}</thead><tbody>${rows}</tbody></table>`,
        serverMessage: v => `<p>${esc(v)}</p>`, stateLoading: v => `<p>${esc(v)}</p>`,
        stateError: v => `<p>${esc(v.title)}</p>`, stateEmpty: v => `<p>${esc(v.title)}</p>`,
        showToast() {}, showModal() {}, _ctx: () => ({ identity: {} }),
        currentSessionGeneration: () => generation, sessionGenerationIsCurrent: v => v === generation,
        AdminRouter: { epoch: 1, refresh() {} }, AdminViews: { register() {} },
        registerAction: (name, fn) => { actions[name] = fn; },
        document: { hidden: false, getElementById: id => elements[id] || null, querySelectorAll: () => cells },
        setTimeout(fn, ms) { const t = { fn, ms, cancelled: false }; timers.push(t); return t; },
        clearTimeout(t) { t.cancelled = true; },
        callTool: async (name, args) => { calls.push({ name, args }); return response(name, args); },
    };
    vm.createContext(ctx);
    const source = fs.readFileSync(sourcePath, 'utf8');
    const instrumented = source.replace("AdminViews.register('spaces', render);", 'globalThis.__spaces = { render, _loadTable, _refreshActivity, _computeRows, _activityHtml, _latestFinished, _tableRowsHtml, _renderBody };');
    vm.runInContext(instrumented, ctx);
    const pending = () => timers.filter(t => !t.cancelled && !t.fired);
    return { ctx, calls, actions, elements, cells, pending, response(fn) { response = fn; }, nextSession() { generation++; }, async tick() { const t = pending()[0]; assert.ok(t); assert.equal(t.ms, 60000); t.fired = true; await t.fn(); await settle(); } };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const spaces = { status: 'ok', spaces: [
    { space_id: 'zebra', description: '<unsafe> Project', owner: 'Alice', live_notes_count: 2, bank_files_count: 6 },
    { space_id: 'alpha', description: 'First project', owner: 'Bob' },
] };
const running = { status: 'ok', lanes: [
    { space_id: 'alpha', lane_state: 'idle', queued_count: 0, latest_jobs: [] },
    { space_id: 'zebra', lane_state: 'running', running_job: { job_id: 'j1', status: 'running', scope_label: 'Agent: Alice', progress: { notes_done: 12, notes_total: 22 } }, queued_count: 1, latest_jobs: [] },
] };
async function loaded(payload = running) {
    const h = harness(); h.response(name => name === 'space_list' ? spaces : payload);
    h.ctx.__spaces.render(element(), {}, { epoch: 1, sessionGeneration: 1, identity: {} });
    await settle(); await settle(); return h;
}
// Real names, stable local search, units, safe strings, and terminal history selection.
{
    const h = await loaded();
    assert.equal(h.calls.length, 2, 'one aggregate inventory and one queues request');
    assert.equal(h.calls.filter(c => c.name === 'space_info' || c.name === 'bank_stale_spaces').length, 0);
    assert.equal(h.ctx.__spaces._computeRows().map(s => s.space_id).join(','), 'alpha,zebra');
    const html = h.elements.spacesTableWrap.innerHTML;
    assert.ok(html.includes('Space|Memory|Consolidation'));
    assert.ok(html.includes('&lt;unsafe&gt;'));
    assert.ok(html.includes('2 notes') && html.includes('6 bank files') && html.includes('— notes'));
    assert.ok(html.includes('12 / 22 notes') && html.includes('Agent: Alice') && html.includes('1 queued'));
    const id = 'space-' + 'a'.repeat(58);
    assert.ok(h.ctx.__spaces._tableRowsHtml([{ space_id: id }]).includes('>' + id + '</a>'), 'full 64-character label');
    h.elements.spacesSearch.value = 'alice'; h.elements.spacesSearch.oninput();
    assert.equal(h.ctx.__spaces._computeRows().map(s => s.space_id).join(','), 'zebra');
    h.elements.spacesSearch.value = 'missing'; h.elements.spacesSearch.oninput();
    assert.ok(h.elements.spacesTableWrap.innerHTML.includes('No spaces match your search'));
    assert.equal(h.calls.length, 2, 'search does not call tools');
    assert.equal(h.pending().length, 0, 'no polling when search paints no active space');
    const lane = { lane_state: 'failed', queued_count: 0, latest_jobs: [
        { job_id: 'active', status: 'running', finished_at: '2026-09-10T01:00:00Z' },
        { job_id: 'old', status: 'succeeded', finished_at: '2026-09-01T01:00:00Z' },
        { job_id: 'new', status: 'failed', result: { status: 'partial' }, finished_at: '2026-09-09T01:00:00Z' },
        { job_id: 'new', status: 'failed', finished_at: '2026-09-08T01:00:00Z' },
        { job_id: 'invalid', status: 'succeeded', finished_at: 'nonsense' },
    ] };
    assert.equal(h.ctx.__spaces._latestFinished(lane).job_id, 'new');
    assert.ok(h.ctx.__spaces._activityHtml(lane).includes('Partial'));
    assert.ok(h.ctx.__spaces._activityHtml(null).includes('Unavailable'));
    const conflicting = { running_job: { job_id: 'new' }, queued_jobs: [{ job_id: 'old' }], latest_jobs: lane.latest_jobs };
    assert.equal(h.ctx.__spaces._latestFinished(conflicting), null, 'active/queued IDs cannot reappear in terminal history');
    const undated = h.ctx.__spaces._activityHtml({ lane_state: 'idle', queued_count: 0, latest_jobs: [{ status: 'failed', job_id: 'undated', finished_at: 'invalid' }] });
    assert.ok(undated.includes('Failed result') && undated.includes('Result date unavailable'));
    assert.ok(!undated.includes('No recent history'));
    assert.ok(!h.ctx.__spaces._activityHtml(null).includes('Idle'));
    assert.ok(!h.ctx.__spaces._activityHtml({ running_job: { status: 'running', progress: { notes_done: 3 } } }).includes('3 / 0'));
}
// All reported forms of partial completion survive both dated/undated summaries.
{
    const h = await loaded();
    for (const result of [{ status: 'partial' }, { status: 'ok', partial: true }, { status: 'ok', batches_completed: 1, batches_total: 2 }]) {
        for (const finished_at of ['2026-09-09T01:00:00Z', undefined]) {
            const html = h.ctx.__spaces._activityHtml({ lane_state: 'idle', queued_count: 0, latest_jobs: [{ job_id: 'partial', status: 'succeeded', result, finished_at }] });
            assert.ok(html.includes('Partial'), 'partial outcome must not appear completed: ' + JSON.stringify({ result, finished_at }));
        }
    }
}
// Typed unavailable responses stop the automatic loop until a successful manual read.
for (const status of ['rate_limited', 'truncated', 'read_only']) {
    const h = await loaded();
    const snapshot = h.elements.spacesTableWrap.innerHTML;
    h.response(() => ({ status, message: 'Temporarily unavailable' }));
    await h.tick();
    assert.equal(h.pending().length, 0, status + ' must stop live refresh');
    assert.equal(h.elements.spacesFreshness.dataset.stale, 'true');
    assert.ok(h.elements.spacesFreshness.innerHTML.includes('Automatic refresh stopped'));
    assert.equal(h.elements.spacesTableWrap.innerHTML, snapshot, 'retain the active snapshot after ' + status);
    h.elements.spacesSearch.value = 'zebra'; h.elements.spacesSearch.oninput();
    assert.equal(h.pending().length, 0, 'search does not restart a paused loop');
    h.response(name => name === 'space_list' ? spaces : running);
    await h.ctx.__spaces._loadTable(1);
    assert.equal(h.pending().length, 1, 'successful manual refresh resumes known activity');
    assert.equal(h.elements.spacesFreshness.dataset.stale, 'false');
}
// One bounded timer; explicit painted IDs; live update touches activity only.
{
    const h = await loaded(); assert.equal(h.pending().length, 1);
    let resolve;
    h.response(() => new Promise(r => { resolve = r; }));
    const snapshot = h.elements.spacesTableWrap.innerHTML;
    const anchor = { innerHTML: 'prior activity', focused: true };
    h.cells.push({ dataset: { space: 'zebra' }, querySelector: () => anchor });
    const t = h.pending()[0]; t.fired = true; t.fn(); await settle();
    assert.equal(h.calls.at(-1).args.space_ids, 'alpha,zebra');
    void h.ctx.__spaces._refreshActivity(1); void h.ctx.__spaces._loadTable(1); await settle();
    assert.equal(h.calls.length, 3, 'no overlapping live or manual requests');
    resolve({ status: 'error', message: 'temporary outage' }); await settle(); await settle();
    assert.equal(h.elements.spacesTableWrap.innerHTML, snapshot, 'failed refresh retains the inventory and focused controls');
    assert.ok(h.elements.spacesFreshness.innerHTML.includes('showing last successful data'));
    assert.equal(h.elements.spacesFreshness.dataset.stale, 'true');
    assert.equal(h.pending().length, 1, 'known active snapshot remains refreshable');
    h.response(() => ({ status: 'ok', lanes: running.lanes.map(l => ({ ...l, running_job: null, queued_count: 0, lane_state: 'idle' })) }));
    await h.tick(); assert.equal(h.pending().length, 0, 'completed activity stops the timer');
    assert.equal(h.elements.spacesFreshness.dataset.stale, 'false');
    assert.equal(h.elements.spacesTableWrap.innerHTML, snapshot, 'live success does not replace the table');
    assert.ok(anchor.innerHTML.includes('No recent history'), 'activity content is refreshed');
    assert.equal(anchor.focused, true, 'the focused anchor is retained');
}
// Filtering scopes live reads without discarding cached activity for hidden rows.
{
    const h = await loaded();
    h.elements.spacesSearch.value = 'zebra'; h.elements.spacesSearch.oninput();
    h.response(() => ({ status: 'ok', lanes: [running.lanes[1]] }));
    await h.tick();
    assert.equal(h.calls.at(-1).args.space_ids, 'zebra');
    h.elements.spacesSearch.value = 'alpha'; h.elements.spacesSearch.oninput();
    assert.ok(h.elements.spacesTableWrap.innerHTML.includes('No recent history'), 'hidden lane cache survives a scoped tick');
    assert.equal(h.pending().length, 0, 'filtering to idle rows stops polling');
}
// F4: a scoped refresh replaces only access warnings for its requested IDs.
{
    const h = await loaded({ ...running, lanes: [running.lanes[1]], denied_spaces: [{ space_id: 'alpha', message: 'Original access denial' }] });
    h.elements.spacesSearch.value = 'zebra'; h.elements.spacesSearch.oninput();
    h.response(() => ({ status: 'ok', lanes: [running.lanes[1]], denied_spaces: [] }));
    await h.tick();
    assert.ok(h.elements.spacesQueueWarnings.innerHTML.includes('alpha: Original access denial'), 'filtered-out denied warning must survive scoped activity refresh');
    h.elements.spacesSearch.value = ''; h.elements.spacesSearch.oninput();
    h.response(() => ({ status: 'ok', lanes: [running.lanes[1]], denied_spaces: [{ space_id: 'alpha', message: 'Updated access denial' }] }));
    await h.tick();
    assert.ok(h.elements.spacesQueueWarnings.innerHTML.includes('alpha: Updated access denial'));
    assert.ok(!h.elements.spacesQueueWarnings.innerHTML.includes('Original access denial'), 'requested warning is replaced, not duplicated');
    h.response(() => running);
    await h.tick();
    assert.equal(h.elements.spacesQueueWarnings.innerHTML, '', 'confirmed access for requested IDs removes their prior warning');
}
// F8: typing keeps the existing deadline while some active row remains visible.
{
    const h = await loaded();
    const timer = h.pending()[0];
    for (const query of ['z', 'ze', 'zebra', 'Alice']) {
        h.elements.spacesSearch.value = query; h.elements.spacesSearch.oninput();
        assert.equal(h.pending()[0], timer, 'search must retain the timer identity and original deadline');
        assert.equal(timer.cancelled, false);
        assert.equal(timer.ms, 60000);
    }
    await h.tick();
    assert.equal(h.calls.at(-1).args.space_ids, 'zebra', 'retained timer still reads the currently painted IDs');
    const next = h.pending()[0];
    h.elements.spacesSearch.value = 'alpha'; h.elements.spacesSearch.oninput();
    assert.equal(next.cancelled, true, 'no visible active rows cancels the timer');
    assert.equal(h.pending().length, 0);
}
// Visibility, navigation, and session changes stop requests/late DOM effects.
{
    const h = await loaded(); h.ctx.document.hidden = true; await h.tick();
    assert.equal(h.calls.length, 2, 'hidden tab has no network call');
    h.ctx.document.hidden = false; h.ctx.AdminRouter.epoch++; await h.tick();
    assert.equal(h.calls.length, 2, 'route exit has no network call');
}
{
    const h = await loaded(); h.nextSession(); await h.tick();
    assert.equal(h.calls.length, 2, 'session change independently stops a tick');
}
{
    const h = await loaded(); let resolve;
    h.response(() => new Promise(r => { resolve = r; }));
    const t = h.pending()[0]; t.fired = true; t.fn(); await settle();
    const before = h.elements.spacesFreshness.innerHTML;
    h.nextSession(); resolve({ status: 'ok', lanes: [], denied_spaces: [{ space_id: 'secret', message: 'cross-session' }] }); await settle();
    assert.equal(h.elements.spacesFreshness.innerHTML, before, 'late cross-session completion has no DOM effect');
    assert.equal(h.elements.spacesQueueWarnings.innerHTML, '', 'late cross-session diagnostics cannot repaint');
    assert.equal(h.pending().length, 0);
}
console.log('admin spaces activity runtime: ok');
