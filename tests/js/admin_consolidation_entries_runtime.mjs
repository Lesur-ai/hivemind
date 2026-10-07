// Existing view entry points delegate to the shared launcher without new reads.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const [spacesPath, dashboardPath] = process.argv.slice(2);
const settle = () => new Promise(resolve => setImmediate(resolve));
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const element = () => {
    const parts = new Map();
    return { clientWidth: 1100, style: { setProperty() {} }, innerHTML: '', textContent: '', dataset: {}, value: '', disabled: false, isConnected: true, children: [], parentNode: null,
        addEventListener() {}, setAttribute() {},
        remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; this.isConnected = false; },
        querySelector(key) { if (!parts.has(key)) parts.set(key, element()); return parts.get(key); },
        appendChild(child) { this.children.push(child); child.parentNode = this; },
        insertBefore(child, before) { if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1); this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, child); child.parentNode = this; },
    };
};
const inventory = { status: 'ok', spaces: [
    { space_id: 'alpha', description: 'A', last_consolidation: '2026-09-30T08:00:00Z', consolidation_count: 2, total_notes_processed: 12 },
    { space_id: 'beta', description: 'B', last_consolidation: '2026-09-29T08:00:00Z', consolidation_count: 1, total_notes_processed: 4 },
] };
const queues = { status: 'ok', lanes: [{ space_id: 'alpha', lane_state: 'running', queued_count: 0, running_job: { job_id: 'private-job', status: 'running' } }] };

function harness(view, permissions = ['write']) {
    const actions = {}, views = {}, elements = {}, calls = [], launches = [], routes = [], bindings = {}, documentListeners = {}, windowListeners = {};
    const elementIds = ['spacesToolbar', 'spacesTableWrap', 'spacesFreshness', 'spacesQueueWarnings', 'spacesRefreshBtn', 'spacesSearch', 'dashSpacesTile', 'dashLanesPanel', 'dashActivityPanel', 'dashStartConsolidationBtn', 'dashRunningJobs', 'dashRecentJobs', 'dashLanesFreshness', 'dashInventoryFreshness', 'dashRecentSpaces', 'dashSpacesEmpty', 'dashLatestSignal', 'dashCardScope', 'dashNoteSpace', 'dashAddNotes', 'dashNotesPanel', 'dashNoteMessage', 'dashQueueWarnings', 'dashActivityEmpty', 'dashRecentEmpty'];
    elementIds.forEach(id => { elements[id] = element(); });
    let generation = 1;
    let respond = name => name === 'space_list' ? inventory : queues;
    const identity = { permissions, client_name: 'Alice' };
    const context = {
        window: { innerHeight: 900, addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); } },
        esc, icon: () => '', cache: {}, renderTimestamp: esc, truncateMiddle: value => value,
        copyable: esc, statusDot: (_, value) => esc(value), pageHeader: (title, body) => `<header>${title}${body}</header>`, panel: value => value,
        dataTable: (headers, rows) => `<table>${headers.join('|')}${rows}</table>`,
        serverMessage: esc, stateLoading: esc, stateError: value => esc(value.title), stateEmpty: value => esc(value.title), stateUnavailable: esc,
        showModal() {}, showToast() {}, renderAutoCompaction: () => '', _portalHealthFlight: null, _dashHealth: null,
        _ctx: () => ({ identity, epoch: context.AdminRouter.epoch, sessionGeneration: generation }),
        currentSessionGeneration: () => generation, sessionGenerationIsCurrent: value => value === generation,
        AdminRouter: { epoch: 1, go: route => routes.push(route), refresh() {} },
        AdminViews: { register: (name, fn) => { views[name] = fn; } },
        registerAction(name, fn) { actions[name] = fn; bindings[name] = (bindings[name] || 0) + 1; },
        document: { hidden: false, activeElement: null, createElement: element, dispatchEvent(event) { (documentListeners[event.type] || []).forEach(fn => fn(event)); }, getElementById: id => elements[id] || null, querySelector: () => null, querySelectorAll: () => [], addEventListener(name, fn) { (documentListeners[name] ||= []).push(fn); } },
        setTimeout: () => ({}), clearTimeout() {},
        CustomEvent: function(type) { this.type = type; },
        localStorage: { getItem: () => null, setItem() {} },
        openConsolidationLauncher: options => launches.push(options),
        callTool: async (name, args) => { calls.push({ name, args }); return respond(name, args); },
    };
    vm.createContext(context);
    if (view === 'dashboard') {
        vm.runInContext(fs.readFileSync(new URL('../../src/live_mem/static/js/admin/portal-refresh.js', import.meta.url), 'utf8') + '\nPortalRefresh.beginSession();', context);
        vm.runInContext(fs.readFileSync(new URL('../../src/live_mem/static/js/admin/views-consolidation.js', import.meta.url), 'utf8'), context);
    }
    vm.runInContext(fs.readFileSync(view === 'spaces' ? spacesPath : dashboardPath, 'utf8'), context);
    const content = element();
    const render = () => {
        // A real route render replaces the content subtree with fresh elements.
        elementIds.forEach(id => { elements[id] = element(); });
        return views[view](content, {}, { epoch: context.AdminRouter.epoch, sessionGeneration: generation, identity });
    };
    return { context, content, calls, launches, routes, actions, elements, bindings, render, identity,
        response(fn) { respond = fn; }, nextSession() { generation++; } };
}

for (const view of ['spaces', 'dashboard']) {
    const action = view === 'spaces' ? 'spaces-start-consolidation' : 'dash-start-consolidation';
    const h = harness(view);
    h.render(); await settle();
    assert.equal(typeof h.actions[action], 'function', view + ' exposes the shared launcher');
    assert.equal(h.calls.length, 2, view + ' reuses its two existing route-entry reads');
    const html = view === 'spaces' ? h.elements.spacesTableWrap.innerHTML : h.content.innerHTML;
    assert.ok(html.includes('Start a consolidation'));
    if (view === 'spaces') {
        assert.ok(html.includes('Space|Memory|Consolidation'), 'three existing columns remain');
        h.actions[action]({ space: '../outside' });
        h.actions[action]({ space: 'not-loaded' });
        assert.equal(h.launches.length, 0, 'only a valid loaded row may launch');
    } else {
        assert.equal(h.elements.dashStartConsolidationBtn.disabled, false, 'loaded spaces enable the launcher');
        assert.equal(h.calls[0].args.include_counts, false, 'Dashboard uses the metadata-only inventory');
        assert.equal(h.calls[1].args.space_ids, 'alpha,beta', 'queue scope is exactly the visible dated cards');
        assert.equal(h.elements.dashRecentSpaces.children.length, 2, 'fixture actually renders recent consolidation cards');
    }
    h.actions[action]({ space: 'alpha' });
    assert.equal(h.calls.length, 2, 'opening a launcher makes no new inventory call');
    assert.equal(h.launches.length, 1);
    const launch = h.launches[0];
    assert.equal(launch.spaces.map(space => space.space_id).join(','), 'alpha,beta');
    assert.equal(launch.lanes[0].running_job.job_id, 'private-job', 'known lane state reaches the shared confirmation');
    assert.equal(launch.ctx.identity.client_name, 'Alice');
    assert.equal(launch.ctx.epoch, 1);
    assert.equal(launch.ctx.sessionGeneration, 1);
    if (view === 'spaces') assert.equal(launch.spaceId, 'alpha');
    launch.onSubmitted({ status: 'queued', space_id: 'not-loaded', job_id: 'private-job' });
    assert.equal(h.routes.length, 0, 'an unexpected server target is never navigated');
    launch.onSubmitted({ status: 'queued', space_id: 'beta', job_id: 'private-job' });
    assert.deepEqual(h.routes, ['/spaces/beta/consolidation'], 'follow returned space without exposing job ID in URL');
    h.context.AdminRouter.epoch++;
    launch.onSubmitted({ status: 'queued', space_id: 'alpha' });
    h.actions[action]({ space: 'alpha' });
    assert.equal(h.routes.length, 1, 'late callback does not navigate after route exit');
    assert.equal(h.launches.length, 1, 'stale action cannot launch on another route');

    const stale = harness(view);
    stale.render(); await settle();
    stale.actions[action]({ space: 'alpha' });
    stale.nextSession();
    stale.launches[0].onSubmitted({ status: 'queued', space_id: 'alpha' });
    stale.actions[action]({ space: 'alpha' });
    assert.equal(stale.routes.length, 0, 'late callback cannot navigate a new session');
    assert.equal(stale.launches.length, 1, 'stale session cannot launch with old inventory');

    const read = harness(view, ['read']);
    read.render(); await settle();
    const readHtml = view === 'spaces' ? read.elements.spacesTableWrap.innerHTML : read.content.innerHTML;
    assert.ok(!readHtml.includes(`data-action="${action}"`), 'read-only identity has no mutation button');
    read.actions[action]({ space: 'alpha' });
    assert.equal(read.launches.length, 0, 'direct action invocation also requires write permission');
    for (const permission of ['manage', 'admin']) {
        const privileged = harness(view, [permission]);
        privileged.render(); await settle();
        privileged.actions[action]({ space: 'alpha' });
        assert.equal(privileged.launches.length, 1, permission + ' retains the launch entry point');
    }
    h.render(); await settle();
    assert.equal(h.bindings[action], 1, 'rendering does not add duplicate action handlers');
}

// A delayed Home inventory may not enable the launcher in a subsequent session.
{
    const h = harness('dashboard');
    const resolvers = [];
    h.response(() => new Promise(resolve => resolvers.push(resolve)));
    h.render();
    assert.ok(h.content.innerHTML.includes('id="dashStartConsolidationBtn" data-action="dash-start-consolidation" disabled'));
    await settle();
    h.nextSession();
    resolvers[0](inventory); await settle();
    assert.equal(resolvers.length, 1, 'expired inventory cannot trigger a queue read');
    assert.equal(h.elements.dashSpacesTile.innerHTML, '', 'old-session inventory is not painted');
    assert.equal(h.context.cache.spaces, undefined, 'old-session spaces are not cached');
    h.actions['dash-start-consolidation']({});
    assert.equal(h.launches.length, 0);
}
console.log('admin consolidation entry points runtime: ok');
