import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'space-detail view path is required');

const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

function node(value = '') {
    const parts = new Map();
    return { attributes: {}, _html: '', htmlWrites: 0, get innerHTML() { return this._html; }, set innerHTML(value) { this._html = value; this.htmlWrites++; this.children = []; }, textContent: '', value, dataset: {}, children: [], parentNode: null, isConnected: true,
        scrollTop: 0, scrollLeft: 0, scrollHeight: 2000, clientHeight: 600, scrollWidth: 1000, clientWidth: 800,
        setAttribute(name, value) { this.attributes[name] = String(value); },
        getAttribute(name) { return this.attributes[name]; },
        querySelector(key) { if (!parts.has(key)) parts.set(key, node()); return parts.get(key); },
        querySelectorAll() { return []; }, contains() { return false; }, focus() {},
        getBoundingClientRect() { return { top: 0, bottom: 20 }; },
        appendChild(child) { return this.insertBefore(child, null); },
        insertBefore(child, before) { if (child === before) return child; if (child.parentNode) child.parentNode.children.splice(child.parentNode.children.indexOf(child), 1); this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, child); child.parentNode = this; return child; },
        remove() { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.isConnected = false; },
        classList: { toggle() {}, contains: () => false },
    };
}

const elements = {
    sdTierPanel: node(),
    sdAuxiliary: node(),
    sdConsolidationJobs: node(),
    sdConsolidationCompaction: node(),
    sdConsolidationCheck: node(),
    sdRulesPanel: node(),
    sdAccessPanel: node(),
    sdShortLimit: node('50'),
    sdShortCategory: node(''),
    sdShortAgent: node(''),
    sdShortSince: node(''),
};
for (const id of ['sdShortBody', 'sdShortFreshness', 'sdShortCount', 'sdShortFilterError', 'sdShortBound', 'sdFileTabs', 'sdBankPreview', 'sdBankMarkdown', 'sdBankFileMeta', 'sdMidListFreshness', 'sdMidFileFreshness', 'sdMidMetadataFreshness', 'sdLastConsolidation', 'sdMidCount', 'sdMidEmpty', 'sdMidCompactionActions', 'sdMidGraphPushActions', 'sdMidCompactionReport']) elements[id] = node();
let sessionGeneration = 1;
const listeners = {}, timers = [];
const calls = [];
const actions = {};
const operatorMounts = [], consolidationMounts = [], launchers = [], navigation = [];
let delayedMid = null;
let longConnected = false;
const toasts = [];
let modal = null;

const spaceInfo = {
    status: 'ok',
    space_id: 'demo-space',
    description: 'Runtime-proof space',
    hive_status_label: 'local_only',
    live: { notes_count: 1, total_size: 128 },
    bank: { files_count: 1, total_size: 256 },
    consolidation_count: 0,
    synthesis_exists: false,
    consolidation_queue: {
        lane_state: 'running',
        running_job: { job_id: 'job-1', status: 'running', scope_label: 'My notes', requested_by: 'research-agent', progress: { notes_done: 3, notes_total: 8 } },
        queued_jobs: [{ job_id: 'job-2', status: 'queued', queue_position: 1 }],
        queued_job_ids: ['job-2'],
        latest_jobs: [],
    },
};

const context = {
    console,
    esc: escapeHtml,
    icon: () => '',
    pill: (_kind, label) => String(label ?? ''),
    statusDot: (_kind, label) => String(label ?? ''),
    fmtSize: value => `${Number(value || 0)} B`,
    renderTimestamp: value => String(value ?? ''),
    copyable: value => String(value ?? ''),
    pageHeader: title => String(title ?? ''),
    dataTable: () => '<table></table>',
    panel: value => String(value ?? ''),
    serverMessage: value => String(value ?? ''),
    renderMarkdown: value => `<md>${escapeHtml(value)}</md>`,
    stateEmpty: () => '<empty>',
    stateError: () => '<error>',
    stateLoading: () => '<loading>',
    stateUnavailable: () => '<unavailable>',
    attentionBanner: () => '<attention>',
    SPACE_ID_RE: /^[a-z0-9][a-z0-9-]{0,63}$/,
    TIERS: new Set(['short', 'mid', 'long']),
    document: {
        hidden: false, activeElement: null,
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        dispatchEvent(event) { (listeners[event.type] || []).forEach(fn => fn(event)); },
        createElement: () => node(),
        getElementById(id) { return elements[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
    },
    AdminRouter: { epoch: 19, go(path) { navigation.push(path); } },
    AdminViews: { register() {}, get(name) {
        assert.ok(['operator', 'consolidation'].includes(name));
        return (target, params, ctx) => (name === 'operator' ? operatorMounts : consolidationMounts).push({ target, params, ctx });
    } },
    history: { replaceState() {} },
    CustomEvent: function(type) { this.type = type; },
    currentSessionGeneration: () => sessionGeneration,
    sessionGenerationIsCurrent: value => value === sessionGeneration,
    localStorage: { getItem: () => null, setItem() {} },
    setTimeout(fn, ms) { const timer = { fn, ms }; timers.push(timer); return timer; },
    clearTimeout(timer) { if (timer) timer.cancelled = true; },
    openConsolidationLauncher(options) { launchers.push(options); },
    registerAction(name, callback) { actions[name] = callback; },
    showToast(kind, message) { toasts.push({ kind, message }); },
    showModal(title, body, confirmLabel, onConfirm) {
        modal = { title, body, confirmLabel, onConfirm };
    },
    callTool: async (tool, args) => {
        calls.push({ tool, args });
        switch (tool) {
        case 'space_info': return spaceInfo;
        case 'live_read': return { status: 'ok', notes: [] };
        case 'bank_list': return delayedMid || { status: 'ok', file_count: 1, files: [{ filename: 'activeContext.md', size: 32 }] };
        case 'bank_read': return { status: 'ok', filename: 'activeContext.md', size: 32, content: '# Active' };
        case 'graph_status': return args.include_graph
            ? { status: 'ok', connected: true, reachable: true, graph_view: { status: 'ok', nodes: [], edges: [] } }
            : { status: 'ok', connected: longConnected, embedded: true, bound: longConnected };
        case 'space_rules': return { status: 'ok', rules: '# Proof rules' };
        case 'admin_list_tokens': return { status: 'ok', tokens: [] };
        case 'bank_consolidate': return { status: 'queued', queue_position: 2 };
        case 'graph_push': return { status: 'ok', files_pushed: 1 };
        default: throw new Error(`Unexpected tool: ${tool}`);
        }
    },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(new URL('../../src/live_mem/static/js/admin/portal-refresh.js', import.meta.url), 'utf8') + '\nglobalThis.__refresh = PortalRefresh; PortalRefresh.beginSession();', context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('space-detail', render);",
    'globalThis.__spaceDetail = { render, renderTier, renderAuxiliary, confirmConsolidate, confirmGraphPush, currentView: () => currentView };',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.ok(context.__spaceDetail, 'space-detail instrumentation failed');
for (const retired of ['sd-retry-backups', 'sd-load-job', 'sd-confirm-backup-delete', 'sd-create-backup']) {
    assert.equal(actions[retired], undefined, `orphaned Space handler must not remain registered: ${retired}`);
}

async function settle() {
    for (let index = 0; index < 8; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

const content = node();
context.__spaceDetail.render(content, { spaceId: 'demo-space', tier: 'short' }, {
    epoch: 19,
    identity: { permissions: ['read', 'write', 'manage', 'admin'] },
});
await settle();

const preloadTools = calls.map(call => call.tool);
assert.deepEqual(preloadTools, ['space_info', 'live_read'], 'SHORT reads only its displayed surface');
assert.equal(context.__refresh.state().available, true, 'Memory registers the actual shared controller');
assert.equal(preloadTools.includes('bank_consolidate'), false, 'preload must not consolidate');
assert.equal(preloadTools.includes('graph_push'), false, 'preload must not project into long');
assert.ok(elements.sdTierPanel.innerHTML.includes('data-action="sd-confirm-consolidate"'));
assert.equal(elements.sdTierPanel.innerHTML.includes('Load recent notes'), false);

const view = context.__spaceDetail.currentView();
assert.ok(view, 'render must retain the live view');
let finishMid;
delayedMid = new Promise(resolve => { finishMid = resolve; });
actions['sd-select-tier']({ tier: 'mid' });
actions['sd-select-tier']({ tier: 'short' });
actions['sd-select-tier']({ tier: 'mid' });
await settle();
assert.equal(calls.filter(call => call.tool === 'bank_list').length, 1, 'switches do not overlap the first MID load');
assert.equal(calls.filter(call => call.tool === 'live_read').length, 1, 'return to loaded SHORT uses its view data');
finishMid({ status: 'ok', file_count: 1, files: [{ filename: 'activeContext.md', size: 32 }] });
await settle(); delayedMid = null;
assert.equal(calls.filter(call => call.tool === 'bank_read').length, 1, 'first MID file opens on first MID activation');
actions['sd-select-tier']({ tier: 'short' }); actions['sd-select-tier']({ tier: 'mid' });
await settle();
assert.equal(calls.filter(call => call.tool === 'bank_list').length, 2, 'returning to MID re-reads the same selected file without invisible SHORT work');
assert.ok(elements.sdTierPanel.innerHTML.includes('data-action="sd-confirm-graph-push"'));
assert.equal(elements.sdTierPanel.innerHTML.includes('Load bank files'), false);
const beforeConsolidation = calls.map(call => call.tool);

context.__spaceDetail.confirmConsolidate(view);
assert.deepEqual(calls.map(call => call.tool), beforeConsolidation, 'opening launcher performs no mutation');
assert.equal(launchers.length, 1);
const launcher = launchers[0];
assert.equal(launcher.spaces.length, 1);
assert.equal(launcher.spaces[0], view.info, 'reuse the authorized Space data');
assert.equal(launcher.spaceId, 'demo-space');
assert.equal(launcher.lanes.length, 1);
assert.equal(launcher.lanes[0].space_id, 'demo-space');
assert.equal(launcher.lanes[0].lane_state, spaceInfo.consolidation_queue.lane_state);
assert.equal(launcher.ctx, view.ctx, 'shared launcher owns the same route/session context');
assert.equal(navigation.length, 0, 'opening launcher does not leave the memory reader');
launcher.onSubmitted({ status: 'queued', job_id: 'accepted' });
assert.equal(navigation.at(-1), '/spaces/demo-space/consolidation');
context.AdminRouter.epoch++;
launcher.onSubmitted({ status: 'queued', job_id: 'late' });
assert.equal(navigation.length, 1, 'an obsolete consumer callback cannot navigate');
context.AdminRouter.epoch--;
assert.equal(calls.some(call => call.tool === 'bank_consolidate'), false, 'only the shared launcher performs enqueue');

context.__spaceDetail.confirmGraphPush(view);
assert.equal(calls.filter(call => call.tool === 'graph_push').length, 0, 'confirmation must precede projection');
assert.equal(modal.title, 'Index current MID files');
assert.ok(modal.body.includes('separate from automatic archiving'));
assert.ok(modal.body.includes('Volatile bank files are not included.'));
assert.equal(await modal.onConfirm(), true);
await settle();
const graphCalls = calls.filter(call => call.tool === 'graph_push');
assert.equal(graphCalls.length, 1);
assert.equal(graphCalls[0].args.space_id, 'demo-space');
assert.deepEqual(Object.keys(graphCalls[0].args), ['space_id']);
assert.equal(Object.hasOwn(graphCalls[0].args, 'include_volatile'), false);
assert.ok(toasts.some(toast => toast.kind === 'ok' && toast.message.includes('Current MID indexing finished')));

// Space owns its active-section reads. Consolidation and Backups are delegated
// to their existing renderers, whose reads and job semantics have separate tests.
longConnected = true;
for (const [params, permissions, expected] of [
    [{ tab: 'memory', tier: 'short' }, ['admin'], ['space_info', 'live_read']],
    [{ tab: 'memory', tier: 'mid' }, ['admin'], ['space_info', 'bank_list', 'bank_read']],
    [{ tab: 'long', tier: 'long', panel: 'overview' }, ['admin'], ['space_info', 'graph_status']],
    [{ tab: 'long', tier: 'long', panel: 'graph' }, ['admin'], ['space_info', 'graph_status']],
    [{ tab: 'rules' }, ['admin'], ['space_info', 'space_rules']],
    [{ tab: 'access' }, ['admin'], ['space_info', 'admin_list_tokens']],
    [{ tab: 'backups' }, ['admin'], ['space_info']],
    [{ tab: 'consolidation' }, ['admin'], ['space_info']],
    [{ tab: 'maintenance' }, ['admin'], ['space_info']],
    [{ tab: 'access' }, ['manage'], ['space_info']],
    [{ tab: 'backups' }, ['read'], ['space_info']],
    [{ tab: 'maintenance' }, ['write'], ['space_info']],
    [{ tab: 'backups' }, ['write'], ['space_info']],
    [{ tab: 'consolidation' }, ['read'], ['space_info']],
]) {
    const before = calls.length;
    const mounts = operatorMounts.length, jobMounts = consolidationMounts.length;
    context.AdminRouter.epoch += 1;
    context.__spaceDetail.render(node(), { spaceId: 'demo-space', ...params }, {
        epoch: context.AdminRouter.epoch, identity: { permissions },
    });
    await settle();
    const actual = calls.slice(before);
    assert.deepEqual(actual.map(call => call.tool), expected, `${params.tab}/${params.tier || ''} for ${permissions}`);
    if (params.tab === 'long') {
        assert.equal(actual[1].args.include_graph, params.panel === 'graph', 'only the visible Graph panel requests its projection');
    }
    if (params.tab === 'backups') {
        const permitted = permissions[0] !== 'read';
        assert.equal(operatorMounts.length - mounts, permitted ? 1 : 0, 'backups mounting respects write floor');
        if (permitted) {
            const mount = operatorMounts.at(-1), selected = context.__spaceDetail.currentView();
            assert.equal(mount.target, elements.sdAuxiliary);
            assert.equal(mount.params.tab, 'backups');
            assert.equal(mount.params.spaceId, 'demo-space');
            assert.equal(mount.ctx, selected.ctx);
            context.__spaceDetail.renderAuxiliary(selected);
            assert.equal(operatorMounts.length - mounts, 1, 'late reads do not remount backups');
        }
    }
    if (params.tab === 'consolidation') {
        const mount = consolidationMounts.at(-1), selected = context.__spaceDetail.currentView();
        assert.equal(consolidationMounts.length - jobMounts, 1);
        assert.equal(mount.target, elements.sdConsolidationJobs);
        assert.equal(mount.params.spaceId, 'demo-space');
        assert.equal(mount.params.embedded, true);
        assert.equal(mount.params.initialLane, spaceInfo.consolidation_queue);
        assert.equal(mount.params.initialLane.running_job, spaceInfo.consolidation_queue.running_job);
        assert.equal(mount.params.initialLane.queued_jobs, spaceInfo.consolidation_queue.queued_jobs);
        assert.equal(mount.ctx, selected.ctx);
        const tools = elements.sdAuxiliary.innerHTML;
        assert.ok(tools.includes('aria-label="For this space"'));
        assert.ok(tools.includes('File compaction and transfer to LONG are separate stages'));
        if (permissions[0] === 'admin') {
            assert.ok(tools.includes('href="#/spaces/demo-space/backups"'));
            assert.ok(tools.includes('href="#/spaces/demo-space/maintenance"'));
        } else {
            assert.equal(tools.includes('href="#/spaces/demo-space/backups"'), false);
            assert.equal(tools.includes('href="#/spaces/demo-space/maintenance"'), false);
            assert.ok(tools.includes('disabled>Check files for compaction'));
        }
        elements.sdAuxiliary.innerHTML = 'preserved child and tools';
        elements.sdConsolidationJobs.innerHTML = 'current job progress';
        selected.compactResult = { status: 'ok', dry_run: true, files: [], files_total: 1 };
        context.__spaceDetail.renderAuxiliary(selected);
        context.__spaceDetail.renderTier(selected);
        assert.equal(consolidationMounts.length - jobMounts, 1, 'late auxiliary data does not remount the job controller');
        assert.equal(elements.sdAuxiliary.innerHTML, 'preserved child and tools');
        assert.equal(elements.sdConsolidationJobs.innerHTML, 'current job progress');
        assert.match(elements.sdConsolidationCompaction.innerHTML, /Compaction|compaction/);
    }
    if (params.tab === 'maintenance') assert.equal(operatorMounts.length - mounts, permissions[0] === 'admin' ? 1 : 0);
}
// An unavailable scope does not even open the shared launcher.
const beforeDenied = launchers.length;
context.__spaceDetail.confirmConsolidate({ ...context.__spaceDetail.currentView(), ctx: { identity: { permissions: ['read'] } } });
assert.equal(launchers.length, beforeDenied);

// Starting on MID must not preload SHORT; the actual tier action loads it once.
context.AdminRouter.epoch += 1;
const beforeMid = calls.length;
context.__spaceDetail.render(node(), { spaceId: 'demo-space', tab: 'memory', tier: 'mid' }, {
    epoch: context.AdminRouter.epoch, identity: { permissions: ['admin'] },
});
await settle();
assert.deepEqual(calls.slice(beforeMid).map(call => call.tool), ['space_info', 'bank_list', 'bank_read']);
actions['sd-select-tier']({ tier: 'short' }); await settle();
assert.equal(calls.slice(beforeMid).filter(call => call.tool === 'live_read').length, 1);
actions['sd-select-tier']({ tier: 'mid' }); actions['sd-select-tier']({ tier: 'short' }); await settle();
assert.equal(calls.slice(beforeMid).filter(call => call.tool === 'live_read').length, 2, 'returning to SHORT performs one current read');

// Memory's real shared controller owns all refreshes. Mutable, local responses
// exercise changed content, partial failures and races without any production IO.
const priorCallTool = context.callTool;
let memoryNotes = [{ filename: 'note-a.md', timestamp: '2026-09-28T23:30:00Z', category: 'observation', agent: '<agent>', tags: ['<tag>'], content: 'A "quote", an apostrophe\'s and `<img onerror=x>`.' }];
let memoryFiles = [
    { filename: 'activeContext.md', size: 32, last_modified: '2026-09-27T10:00:00Z' },
    { filename: 'progress.md', size: 64, last_modified: '2026-09-28T10:00:00Z' },
];
let memoryContents = { 'activeContext.md': '# Active', 'progress.md': '# Progress 1' };
let notesResult = null, listResult = null, fileResult = null, metadataResult = null;
context.callTool = async (tool, args) => {
    if (!['space_info', 'live_read', 'bank_list', 'bank_read'].includes(tool)) return priorCallTool(tool, args);
    calls.push({ tool, args });
    if (tool === 'space_info') return metadataResult || { ...spaceInfo, last_consolidation: '2026-09-25T12:00:00Z' };
    if (tool === 'live_read') return notesResult || { status: 'ok', notes: memoryNotes, has_more: true };
    if (tool === 'bank_list') return listResult || { status: 'ok', file_count: memoryFiles.length, files: memoryFiles };
    return fileResult || { status: 'ok', filename: args.filename, size: 64, content: memoryContents[args.filename] };
};
async function openMemory(tier, permissions = ['read']) {
    context.__refresh.clearRoute(); context.AdminRouter.epoch++;
    // The fake DOM does not parse frame HTML; mimic creation of the mounted regions.
    for (const id of ['sdFileTabs', 'sdShortBody', 'sdBankMarkdown']) elements[id].innerHTML = '';
    context.__spaceDetail.render(node(), { spaceId: 'demo-space', tier }, { epoch: context.AdminRouter.epoch, identity: { permissions } });
    await settle(); return context.__spaceDetail.currentView();
}
async function tick() {
    const timer = timers.findLast(item => !item.cancelled);
    assert.ok(timer, 'one actual controller timer is armed');
    timer.cancelled = true; timer.fn(); await settle();
}
for (const tier of ['short', 'mid']) {
    context.document.hidden = true;
    context.document.dispatchEvent(new context.CustomEvent('visibilitychange'));
    const beforeHidden = calls.length;
    const hiddenView = await openMemory(tier);
    assert.deepEqual(calls.slice(beforeHidden).map(call => call.tool), ['space_info']);
    context.document.hidden = false;
    context.document.dispatchEvent(new context.CustomEvent('visibilitychange'));
    await settle();
    assert.deepEqual(calls.slice(beforeHidden).map(call => call.tool), tier === 'short'
        ? ['space_info', 'live_read'] : ['space_info', 'bank_list', 'bank_read']);
    assert.ok(tier === 'short' ? hiddenView.shortData : hiddenView.midFileData, 'visible return completes initial Memory read with auto off');
}
let selected = await openMemory('short');
assert.equal(context.__refresh.state().enabled, false, 'Memory auto-refresh defaults off');
assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1).args)), { space_id: 'demo-space', limit: 50, category: '', agent: '', since: '' });
assert.equal(selected.shortNewKeys.size, 0, 'first response does not invent new notes');
const localDate = new Date(memoryNotes[0].timestamp);
const localDay = `${localDate.getFullYear()}-${String(localDate.getMonth()+1).padStart(2,'0')}-${String(localDate.getDate()).padStart(2,'0')}`;
assert.ok(selected.shortGroups.has(localDay), 'day grouping follows local date, not UTC substring');
const firstRow = selected.shortRows.get('note-a.md');
assert.match(firstRow.querySelector('.sd-note-content').innerHTML, /<code>&lt;img onerror=x&gt;<\/code>/);
assert.doesNotMatch(firstRow.querySelector('.sd-note-content').innerHTML, /<img/);
assert.match(firstRow.querySelector('[data-part="meta"]').innerHTML, /&lt;agent&gt;/);
assert.match(firstRow.querySelector('[data-part="tags"]').innerHTML, /&lt;tag&gt;/);
assert.match(elements.sdShortBound.textContent, /more notes are available/);
const contentNode = firstRow.querySelector('.sd-note-content');
contentNode.innerHTML = contentNode.innerHTML.replaceAll('&#39;', "'").replaceAll('&quot;', '"');
const bodyWrites = contentNode.htmlWrites, frameWrites = elements.sdTierPanel.htmlWrites;
elements.sdShortCategory.value = 'decision'; elements.sdShortAgent.value = 'unapplied';
context.__refresh.configure({ enabled: true, intervalSeconds: 15 });
let beforeCycle = calls.length; await tick();
assert.deepEqual(calls.slice(beforeCycle).map(call => call.tool), ['live_read'], 'SHORT tick never rereads space metadata');
assert.equal(calls.at(-1).args.category, '', 'typing is not applying filters');
assert.equal(calls.at(-1).args.agent, '');
assert.equal(elements.sdShortAgent.value, 'unapplied');
assert.equal(elements.sdTierPanel.htmlWrites, frameWrites, 'the Memory frame is not remounted');
assert.equal(contentNode.htmlWrites, bodyWrites, 'browser-normalized quotes cannot replace unchanged note content');
assert.equal(selected.shortRows.get('note-a.md'), firstRow);

memoryNotes = [{ filename: 'note-new.md', timestamp: '2026-09-29T10:00:00Z', content: 'Fresh' }, ...memoryNotes, { filename: 'undated.md', content: 'Missing date' }];
await tick();
assert.deepEqual([...selected.shortNewKeys], ['note-new.md']);
assert.ok(selected.shortGroups.has('unknown'), 'no invented timestamp for undated notes');
elements.sdShortLimit.value = '999'; elements.sdShortSince.value = '2026-09-29T10:00';
actions['sd-apply-short-filters'](); await settle();
assert.equal(calls.at(-1).args.limit, 500);
assert.equal(calls.at(-1).args.category, 'decision');
assert.equal(calls.at(-1).args.agent, 'unapplied');
assert.equal(calls.at(-1).args.since, new Date('2026-09-29T10:00').toISOString());
assert.equal(selected.shortNewKeys.size, 0, 'a filter change resets the new-note comparison');
elements.sdShortSince.value = 'invalid'; beforeCycle = calls.length;
actions['sd-apply-short-filters'](); await settle();
assert.equal(calls.length, beforeCycle, 'invalid Since is refused before a read');
assert.match(elements.sdShortFilterError.innerHTML, /valid local date/);
const goodNotes = selected.shortData, goodShortTime = selected.shortSuccess;
notesResult = { status: 'error', message: '<failed-short>' };
await assert.rejects(context.__refresh.refresh());
assert.equal(selected.shortData, goodNotes); assert.equal(selected.shortSuccess, goodShortTime);
assert.match(elements.sdShortFreshness.innerHTML, /&lt;failed-short&gt;/);
assert.equal(context.__refresh.state().error, 'Refresh failed', 'region failures propagate to the shared backoff');
notesResult = null;

// Actual single-flight + last applied filter wins after a slow request.
let resolveNotes; notesResult = new Promise(resolve => { resolveNotes = resolve; });
const pendingNotes = context.__refresh.refresh(); await settle();
const beforeSuperseded = context.__refresh.state().lastSuccess;
elements.sdShortCategory.value = 'insight'; elements.sdShortSince.value = '';
actions['sd-apply-short-filters'](); await settle();
assert.equal(selected.shortData, goodNotes, 'no old response has painted yet');
let resolveCurrentNotes; notesResult = new Promise(resolve => { resolveCurrentNotes = resolve; });
resolveNotes({ status: 'error', message: 'obsolete failure' });
assert.equal((await pendingNotes).skipped, true, 'a superseded cycle is explicitly neutral');
await settle();
assert.equal(context.__refresh.state().lastSuccess, beforeSuperseded);
assert.equal(context.__refresh.state().error, 'Refresh failed', 'the superseded failure cannot clear the prior error while the current read waits');
notesResult = null; resolveCurrentNotes({ status: 'ok', notes: [], current: true }); await settle();
assert.equal(selected.shortData.current, true, 'old applied-filter response cannot overwrite the latest request');
assert.equal(calls.at(-1).args.category, 'insight');
context.__refresh.configure({ enabled: false, intervalSeconds: 15 });

selected = await openMemory('mid', ['admin']);
assert.equal(selected.midSelectedFilename, 'activeContext.md');
assert.equal(selected.midFileData.content, '# Active');
assert.match(elements.sdLastConsolidation.innerHTML, /2026-09-25T12:00:00Z/);
assert.match(elements.sdBankFileMeta.innerHTML, /2026-09-27T10:00:00Z/);
const readMemoryTools = context.callTool;
let resolvePush, pushCalls = 0;
context.callTool = (tool, args) => {
    if (tool !== 'graph_push') return readMemoryTools(tool, args);
    pushCalls++; return new Promise(resolve => { resolvePush = resolve; });
};
context.__spaceDetail.confirmGraphPush(selected);
const pushFlight = modal.onConfirm(); await settle();
assert.match(elements.sdMidGraphPushActions.innerHTML, /disabled/, 'the visible index action is disabled while the mutation is pending');
assert.equal(await modal.onConfirm(), false); assert.equal(pushCalls, 1, 'duplicate submission remains guarded');
resolvePush({ status: 'ok', files_pushed: 1 }); await pushFlight; await settle();
assert.doesNotMatch(elements.sdMidGraphPushActions.innerHTML, /disabled/);
context.callTool = readMemoryTools;
// If one MID region fails, then the tier changes while its sibling still waits,
// the old rejected cycle must not become a new controller failure.
let resolveOldMetadata, resolveTierNotes;
metadataResult = new Promise(resolve => { resolveOldMetadata = resolve; });
fileResult = { status: 'error', message: 'superseded MID file failure' };
const oldMidFlight = context.__refresh.refresh(); await settle();
notesResult = new Promise(resolve => { resolveTierNotes = resolve; });
actions['sd-select-tier']({ tier: 'short' }); await settle();
metadataResult = null; fileResult = null; resolveOldMetadata(spaceInfo);
await assert.doesNotReject(async () => {
    assert.equal((await oldMidFlight).skipped, true, 'already rejected MID region is neutral when its whole cycle was superseded');
});
await settle(); assert.equal(context.__refresh.state().error, null);
notesResult = null; resolveTierNotes({ status: 'ok', notes: memoryNotes }); await settle();
actions['sd-select-tier']({ tier: 'mid' }); await settle();
let resolveList; listResult = new Promise(resolve => { resolveList = resolve; });
const compactionControlWrites = elements.sdMidCompactionActions.htmlWrites;
const slowListFlight = context.__refresh.refresh(); await settle();
assert.equal(elements.sdMidCompactionActions.htmlWrites, compactionControlWrites, 'fast metadata must not disable/remount a compaction control while the list is still refreshing');
listResult = null; resolveList({ status: 'ok', files: memoryFiles }); await slowListFlight;
actions['sd-read-bank']({ filename: 'progress.md' });
assert.doesNotMatch(elements.sdMidFileFreshness.innerHTML, /state-degraded/, 'ordinary file loading is not an error');
assert.match(elements.sdMidFileFreshness.innerHTML, /Loading selected file/);
await settle();
assert.equal(selected.midSelectedFilename, 'progress.md');
assert.equal(selected.midFileData.content, '# Progress 1');
const selectedTab = selected.midTabs.get('progress.md'), reader = elements.sdBankPreview;
reader.scrollTop = 120; reader.scrollLeft = 15;
memoryFiles = [memoryFiles[1], memoryFiles[0]];
memoryContents['progress.md'] = '# Progress 2';
beforeCycle = calls.length; await context.__refresh.refresh();
assert.deepEqual(calls.slice(beforeCycle).map(call => call.tool).sort(), ['bank_list', 'bank_read', 'space_info']);
assert.equal(calls.at(-1).args.filename, 'progress.md', 'list reorder never changes the selected filename');
assert.equal(selected.midFileData.content, '# Progress 2', 'unchanged name must still reread changed content');
assert.equal(selected.midTabs.get('progress.md'), selectedTab, 'filename tabs keep their identity');
assert.match(elements.sdBankMarkdown.innerHTML, /Progress 2/);
assert.equal(reader.scrollTop, 120); assert.equal(reader.scrollLeft, 15);
const markdownWrites = elements.sdBankMarkdown.htmlWrites;
await context.__refresh.refresh();
assert.equal(elements.sdBankMarkdown.htmlWrites, markdownWrites, 'unchanged MID content keeps the sanitized DOM');
const goodFile = selected.midFileData, goodFileTime = selected.midFileSuccess, goodMetadata = selected.info, goodMetadataTime = selected.midMetadataSuccess;
fileResult = { status: 'error', message: '<file-error>' }; metadataResult = { status: 'error', message: '<metadata-error>' };
await assert.rejects(context.__refresh.refresh());
assert.equal(selected.midFileData, goodFile); assert.equal(selected.midFileSuccess, goodFileTime);
assert.equal(selected.info, goodMetadata); assert.equal(selected.midMetadataSuccess, goodMetadataTime);
assert.match(elements.sdMidFileFreshness.innerHTML, /&lt;file-error&gt;/);
assert.match(elements.sdMidMetadataFreshness.innerHTML, /&lt;metadata-error&gt;/);
metadataResult = null; await assert.rejects(context.__refresh.refresh());
assert.equal(selected.midMetadataError, '', 'the metadata region can recover independently');
assert.match(elements.sdMidFileFreshness.innerHTML, /&lt;file-error&gt;/, 'successful metadata must not clear the file error');
fileResult = null; await context.__refresh.refresh();
const beforeListFile = selected.midFileData;
listResult = { status: 'error', message: 'list failed' }; beforeCycle = calls.length;
await assert.rejects(context.__refresh.refresh());
assert.equal(calls.slice(beforeCycle).some(call => call.tool === 'bank_read'), false, 'failed list does not trigger an unbound file read');
assert.equal(selected.midFileData, beforeListFile); assert.match(elements.sdMidListFreshness.innerHTML, /list failed/);
listResult = null; memoryFiles = memoryFiles.filter(file => file.filename !== 'progress.md'); beforeCycle = calls.length;
await context.__refresh.refresh();
assert.equal(selected.midSelectedFilename, 'progress.md', 'disappearance cannot silently select another file');
assert.equal(selected.midFileData, beforeListFile); assert.match(elements.sdMidFileFreshness.innerHTML, /no longer in the list/);
assert.match(elements.sdBankFileMeta.innerHTML, /2026-09-28T10:00:00Z/, 'retained content keeps its last observed source modification date');
assert.equal(calls.slice(beforeCycle).some(call => call.tool === 'bank_read'), false);
assert.equal(selected.midTabs.get('activeContext.md').tabIndex, 0, 'remaining files stay keyboard reachable');
memoryFiles.push({ filename: 'progress.md', size: 64 });
await context.__refresh.refresh();
assert.match(elements.sdBankFileMeta.innerHTML, /Not recorded/, 'a fresh source without a date must not inherit a historical date');

// A→B→A during one delayed file read cannot paint B or create overlapping cycles.
let resolveFile; fileResult = new Promise(resolve => { resolveFile = resolve; });
const pendingFile = context.__refresh.refresh(); await settle();
actions['sd-read-bank']({ filename: 'activeContext.md' }); actions['sd-read-bank']({ filename: 'progress.md' });
beforeCycle = calls.length; await settle(); assert.equal(calls.length, beforeCycle);
fileResult = null; resolveFile({ status: 'ok', filename: 'progress.md', content: 'STALE FILE', size: 10 });
assert.equal((await pendingFile).skipped, true, 'superseded MID cycles are neutral too'); await settle();
assert.equal(selected.midSelectedFilename, 'progress.md');
assert.equal(selected.midFileData.content, '# Progress 2');
assert.match(elements.sdBankMarkdown.innerHTML, /Progress 2/, 'A→B→A must restore the visible content, even when unchanged');

// Disabling auto mid-flight drops its response; a manual refresh still works.
context.__refresh.configure({ enabled: true, intervalSeconds: 15 });
fileResult = new Promise(resolve => { resolveFile = resolve; }); await tick();
const beforeDisable = selected.midFileData;
context.__refresh.configure({ enabled: false, intervalSeconds: 15 });
fileResult = null; resolveFile({ status: 'ok', filename: 'progress.md', content: 'DISABLED RESPONSE', size: 5 }); await settle();
assert.equal(selected.midFileData, beforeDisable);
await context.__refresh.refresh();

// Route and session invalidation prevent old files from painting or queuing a follow-up.
for (const invalidate of [() => { context.AdminRouter.epoch++; context.__refresh.clearRoute(); }, () => { sessionGeneration++; context.__refresh.endSession(); }]) {
    context.__refresh.beginSession(); selected = await openMemory('mid');
    fileResult = new Promise(resolve => { resolveFile = resolve; });
    const flight = context.__refresh.refresh(); await settle();
    const oldFile = selected.midFileData;
    actions['sd-read-bank']({ filename: 'activeContext.md' });
    invalidate(); beforeCycle = calls.length;
    fileResult = null; resolveFile({ status: 'ok', filename: 'activeContext.md', content: 'OLD SESSION', size: 4 });
    await flight; await settle();
    assert.equal(selected.midFileData, oldFile); assert.equal(calls.length, beforeCycle);
}
context.__refresh.endSession(); beforeCycle = calls.length;
selected = await openMemory('short');
assert.deepEqual(calls.slice(beforeCycle).map(call => call.tool), ['space_info'], 'unavailable refresh session starts no Memory reads');
assert.match(selected.shortError, /Sign out and sign in again/);
assert.equal(selected.shortLoading, false);
// Logout during the initial space_info cannot mount a new consumer from old identity.
context.__refresh.beginSession();
let resolveInitial; metadataResult = new Promise(resolve => { resolveInitial = resolve; });
context.AdminRouter.epoch++;
const initialContent = node();
context.__spaceDetail.render(initialContent, { spaceId: 'demo-space', tier: 'short' }, { epoch: context.AdminRouter.epoch, identity: { permissions: ['read'] } });
await settle(); const loadingFrame = initialContent.innerHTML; beforeCycle = calls.length;
sessionGeneration++; context.__refresh.endSession();
metadataResult = null; resolveInitial(spaceInfo); await settle();
assert.equal(initialContent.innerHTML, loadingFrame, 'an old initial overview must not paint after logout');
assert.equal(context.__spaceDetail.currentView().info, null);
assert.equal(calls.length, beforeCycle);
console.log('admin Memory reader runtime: filters, timestamps, bounded calls, identity, failures and stale reads: ok');

// A failed initial load has no overview, so the page still identifies its target.
context.callTool = async () => ({ status: 'error', message: 'Unavailable' });
context.__spaceDetail.render(content, { spaceId: 'unavailable-space', tier: 'mid' }, {
    epoch: context.AdminRouter.epoch, identity: { permissions: ['read'] },
});
await settle();
assert.ok(content.innerHTML.includes('Space: unavailable-space'));
assert.ok(content.innerHTML.includes('<error>'));

console.log('admin space detail preload runtime: ok');
