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
for (const id of ['sdShortBody', 'sdShortFreshness', 'sdShortCount', 'sdShortFilterError', 'sdShortBound', 'sdFileTabs', 'sdBankPreview', 'sdBankMarkdown', 'sdBankFileMeta', 'sdMidListFreshness', 'sdMidFileFreshness', 'sdMidMetadataFreshness', 'sdLastConsolidation', 'sdMidCount', 'sdMidEmpty', 'sdMidCompactionActions', 'sdMidCompactionReport']) elements[id] = node();
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
    URL,
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
    serverMessage: value => escapeHtml(value ?? ''),
    renderMarkdown: value => `<md>${escapeHtml(value)}</md>`,
    stateEmpty: options => `<empty>${escapeHtml(options.title || '')}${escapeHtml(options.hint || '')}${options.actionHtml || ''}</empty>`,
    stateError: options => `<error>${escapeHtml(options.title || '')}${escapeHtml(options.message || '')}</error>`,
    stateLoading: () => '<loading>',
    stateUnavailable: message => `<unavailable>${escapeHtml(message || '')}</unavailable>`,
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
    'globalThis.__spaceDetail = { render, renderTier, renderAuxiliary, confirmConsolidate, confirmGraphPush, refreshIngestions, safeLongEndpoint, currentView: () => currentView };',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.ok(context.__spaceDetail, 'space-detail instrumentation failed');
// R1 F6: generated fake values never appear in diagnostics or assertion output.
const queryKeys = ['sig', 'signature', 'X-Amz-Signature', 'key', 'auth', 'token', 'ordinary'];
const credentialValues = queryKeys.map(() => crypto.randomUUID());
const endpoint = new URL('https://graph.example.invalid/v1');
endpoint.username = crypto.randomUUID(); endpoint.password = crypto.randomUUID();
for (let index = 0; index < queryKeys.length; index++) endpoint.searchParams.append(queryKeys[index], credentialValues[index]);
endpoint.searchParams.append('auth', crypto.randomUUID());
credentialValues.push(endpoint.username, endpoint.password, endpoint.searchParams.getAll('auth')[1]);
const endpointHtml = context.__spaceDetail.safeLongEndpoint(endpoint.href);
assert.ok(!credentialValues.some(value => endpointHtml.includes(value)), 'R1 F6 query credential redaction');
assert.match(endpointHtml, /graph\.example\.invalid\/v1/);
const fallbackHtml = context.__spaceDetail.safeLongEndpoint(`relative/path?signature=${credentialValues[0]}&auth=${credentialValues[1]}`);
assert.ok(!credentialValues.some(value => fallbackHtml.includes(value)), 'R1 F6 malformed URL query redaction');
for (const retired of ['sd-retry-backups', 'sd-load-job', 'sd-confirm-backup-delete', 'sd-create-backup']) {
    assert.equal(actions[retired], undefined, `orphaned Space handler must not remain registered: ${retired}`);
}

async function settle() {
    for (let index = 0; index < 8; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

for (const id of ['sdLongStatus', 'sdLongSnapshot', 'sdOntologyConfig', 'sdOntologyFreshness', 'sdOntologySelect', 'sdOntologyDefinition', 'sdOntologyDefinitionFreshness', 'sdOntologyValidation', 'sdDocumentsFreshness', 'sdDocumentsList', 'sdDocumentsPage', 'sdDocumentFreshness', 'sdDocumentContentFreshness', 'sdDocumentDetail', 'sdDocumentSelectionNotice', 'sdDocumentQuery', 'sdDocumentStatus', 'sdIngestFreshness', 'sdIngestCancelResult', 'sdIngestList', 'sdIngestPage', 'sdIngestDetailFreshness', 'sdIngestDetail', 'sdIngestStatus', 'sdIngestBatch']) elements[id] = node();
let docs = [{ document_id: 'doc-a', filename: '<a>.md', source_path: '/a', ingestion_status: 'succeeded', size_bytes: 15 }, { document_id: 'doc-b', filename: 'b.md', ingestion_status: 'cleanup_pending' }];
let jobs = [{ job_id: 'job-a', status: 'running', filename: '<job>.md', current_step: 'extracting', progress_percent: 25, polling: { recommended: false } }];
let statusJob = { ...jobs[0] }, overrides = {};
const yaml = 'name: test\nentity_types: ["<img src=x>"]';
context.callTool = async (tool, args) => {
    calls.push({ tool, args });
    if (Object.hasOwn(overrides, tool)) return typeof overrides[tool] === 'function' ? overrides[tool](args) : overrides[tool];
    switch (tool) {
    case 'space_info': return spaceInfo;
    case 'graph_status': return { status: 'ok', connected: true, reachable: true, binding: 'embedded', config: { ontology: 'configured-not-in-catalog' }, graph_stats: { document_count: 2, entity_count: 3, relation_count: 1 }, mid_automation: { compaction_enabled: true, archive_enabled: false }, mid_archive_projection: { pending: 2 }, ...(args.include_graph ? { graph_view: { status: 'ok', nodes: [], edges: [] } } : {}) };
    case 'ontology_list': return { status: 'ok', count: 1, ontologies: [{ name: 'test', version: '1', entity_types_count: 1, relation_types_count: 0 }] };
    case 'ontology_get': return { status: 'ok', name: args.name, version: '1', description: '<description>', entity_types_count: 1, relation_types_count: 0, content: yaml };
    case 'ontology_validate': return { status: 'ok', valid: true, errors: [] };
    case 'long_document_list': return { status: 'ok', documents: args.offset === 50 ? [docs[1]] : docs, total_count: 51, count: args.offset === 50 ? 1 : docs.length, limit: 50, offset: args.offset, partial: true, warnings: ['<catalog warning>'] };
    case 'long_document_get': return { status: 'ok', document: { ...docs.find(doc => doc.document_id === args.document_id) }, ...(args.include_content ? { content: '<script>hostile</script>', content_format: 'text' } : {}) };
    case 'long_ingest_list': return { status: 'ok', jobs, total: 51, count: jobs.length, limit: 50, offset: args.offset, guarantee: 'in_memory_best_effort', polling: { recommended: false } };
    case 'long_ingest_status': return statusJob;
    case 'long_ingest_cancel': return { status: 'cancelling', job_id: args.job_id, message: '<cancellation requested>' };
    default: throw new Error(`Unexpected tool ${tool}`);
    }
};
async function open(panel, permissions = ['admin']) {
    context.__refresh.clearRoute(); context.AdminRouter.epoch++;
    for (const el of Object.values(elements)) { el.longMarkup = undefined; el.innerHTML = ''; }
    context.__spaceDetail.render(node(), { spaceId: 'demo-space', tab: 'long', panel }, { epoch: context.AdminRouter.epoch, identity: { permissions } });
    await settle(); return context.__spaceDetail.currentView();
}
async function tick() {
    const timer = timers.findLast(item => !item.cancelled);
    assert.ok(timer, 'active ingestion has one shared-controller timer'); timer.cancelled = true; timer.fn(); await settle();
}
for (const [panel, expected] of [
    ['overview', ['space_info', 'graph_status']], ['graph', ['space_info', 'graph_status']],
    ['ontology', ['space_info', 'graph_status', 'ontology_list']],
    ['documents', ['space_info', 'long_document_list']], ['jobs', ['space_info', 'long_ingest_list']],
]) {
    const before = calls.length;
    await open(panel);
    assert.match(elements.sdTierPanel.innerHTML, /Long memory panels/, 'five distinct LONG panels must be exposed');
    const actual = calls.slice(before);
    assert.deepEqual(actual.map(call => call.tool), expected, panel + ' reads only its displayed panel');
    if (['overview', 'graph', 'ontology'].includes(panel)) assert.equal(actual[1].args.include_graph, panel === 'graph');
    assert.equal(context.__refresh.state().available, panel === 'jobs', 'only ingestion jobs register automatic refresh');
}
let view = await open('ontology');
assert.match(elements.sdOntologyConfig.innerHTML, /configured-not-in-catalog/);
assert.equal(view.ontology.definition, undefined, 'catalog entry is not silently selected as the effective schema');
elements.sdOntologySelect.value = 'test'; actions['sd-ontology-load'](); await settle();
assert.equal(calls.at(-1).tool, 'ontology_get');
assert.match(elements.sdOntologyDefinition.innerHTML, /&lt;img src=x&gt;/);
assert.doesNotMatch(elements.sdOntologyDefinition.innerHTML, /<img/);
actions['sd-ontology-validate'](); await settle();
assert.equal(calls.at(-1).args.content_yaml, yaml);
assert.match(elements.sdOntologyValidation.innerHTML, /Valid structure/);
const originalDefinition = view.ontology.definition, oldDefinitionTime = view.ontology.definitionSuccess;
overrides.ontology_get = { status: 'error', message: '<definition failed>' };
actions['sd-ontology-load'](); await settle();
assert.equal(view.ontology.definition, originalDefinition); assert.equal(view.ontology.definitionSuccess, oldDefinitionTime);
assert.match(elements.sdOntologyDefinitionFreshness.innerHTML, /&lt;definition failed&gt;/);
overrides = {};

view = await open('documents');
assert.match(elements.sdDocumentsList.innerHTML, /Partial catalog/);
assert.match(elements.sdDocumentsList.innerHTML, /&lt;catalog warning&gt;/);
assert.equal(calls.at(-1).tool, 'long_document_list', 'listing cannot download content');
actions['sd-document-inspect']({ key: 'id:doc-a' }); await settle();
assert.equal(calls.at(-1).args.document_id, 'doc-a'); assert.equal(calls.at(-1).args.include_content, false);
assert.equal(view.documents.content, null, 'metadata inspection cannot download content');
actions['sd-document-content'](); await settle();
assert.equal(calls.at(-1).args.include_content, true);
assert.match(elements.sdDocumentDetail.innerHTML, /&lt;script&gt;hostile/); assert.doesNotMatch(elements.sdDocumentDetail.innerHTML, /<script>/);
const oldContent = view.documents.content, oldContentTime = view.documents.contentSuccess;
overrides.long_document_get = { status: 'error', message: 'content failed' };
actions['sd-document-content'](); await settle();
assert.equal(view.documents.content, oldContent); assert.equal(view.documents.contentSuccess, oldContentTime);
assert.match(elements.sdDocumentContentFreshness.innerHTML, /content failed/);
overrides.long_document_get = { status: 'ok', document: docs[0], content_format: 'raw', content_base64: 'SECRET_BINARY', content_note: '<raw fallback>' };
actions['sd-document-content'](); await settle();
assert.match(elements.sdDocumentDetail.innerHTML, /binary content/); assert.doesNotMatch(elements.sdDocumentDetail.innerHTML, /SECRET_BINARY/);
overrides = {};
elements.sdDocumentQuery.value = 'b'; elements.sdDocumentStatus.value = 'cleanup_pending';
actions['sd-documents-apply'](); await settle();
assert.equal(calls.at(-1).args.query, 'b'); assert.equal(calls.at(-1).args.status, 'cleanup_pending');
const firstDocumentPage = elements.sdDocumentsPage.innerHTML;
overrides.long_document_list = { status: 'error', message: 'next page failed' };
actions['sd-documents-page']({ step: '1' }); await settle();
assert.equal(elements.sdDocumentsPage.innerHTML, firstDocumentPage, 'failed page read retains the actual snapshot range');
overrides = {};
actions['sd-documents-page']({ step: '1' }); await settle();
assert.equal(calls.at(-1).args.offset, 50); assert.match(elements.sdDocumentsPage.innerHTML, /51–51/);
const pageCalls = calls.length; actions['sd-documents-page']({ step: '1' }); await settle(); assert.equal(calls.length, pageCalls);
actions['sd-documents-page']({ step: '-1' }); await settle();
let resolveOld; overrides.long_document_get = args => args.document_id === 'doc-a' ? new Promise(resolve => { resolveOld = resolve; }) : { status: 'ok', document: docs[1] };
actions['sd-document-inspect']({ key: 'id:doc-a' }); await settle();
actions['sd-document-inspect']({ key: 'id:doc-b' }); await settle();
resolveOld({ status: 'ok', document: docs[0] }); await settle();
assert.equal(view.documents.detail.document.document_id, 'doc-b', 'late document A cannot replace selected B');
overrides = {};

// #641 F2/L2/L4: recover real page shrink; preserve reading only with honest
// membership language, and reject hidden/stale/repeated recovery dispatch.
const retainedDetail = view.documents.detail;
overrides.long_document_list = { status: 'ok', documents: [docs[0]], count: 1, total_count: 1, offset: 0, limit: 50 };
actions['sd-long-refresh'](); await settle();
assert.equal(view.documents.detail, retainedDetail);
assert.match(elements.sdDocumentSelectionNotice.innerHTML, /not in the shown catalog page/);
const beyondEnd = { status: 'ok', documents: [], count: 0, total_count: 50, offset: 50, limit: 50 };
view.documents.offset = 50; overrides.long_document_list = beyondEnd;
actions['sd-long-refresh'](); await settle();
assert.equal(view.documents.selected, null, 'empty success must still tear down selected reading');
assert.match(elements.sdDocumentsList.innerHTML, /Return to the first page/);
assert.equal(typeof actions['sd-documents-first'], 'function');
let resolveFirst;
overrides.long_document_list = new Promise(resolve => { resolveFirst = resolve; });
const recoveryReads = calls.length;
actions['sd-documents-first'](); actions['sd-documents-first'](); await settle();
assert.equal(calls.length, recoveryReads + 1, 'loading rejects repeat first-page reads');
assert.equal(calls.at(-1).args.offset, 0);
assert.equal(calls.at(-1).args.status, 'cleanup_pending', 'recovery preserves applied filters');
resolveFirst({ status: 'ok', documents: docs, count: 2, total_count: 2, offset: 0, limit: 50 }); await settle();
const firstReads = calls.length; actions['sd-documents-first'](); await settle();
assert.equal(calls.length, firstReads, 'hidden first-page action cannot refetch a nonempty page');
for (const change of [{ offset: '50' }, { total_count: '50' }, { limit: 100 }, { count: 1 }, { documents: [docs[0]] }]) {
    view.documents.list = { ...beyondEnd, ...change };
    actions['sd-documents-first'](); await settle();
    assert.equal(calls.length, firstReads, 'malformed recovery dispatch must be rejected');
}
view.documents.list = beyondEnd;
context.AdminRouter.epoch++; actions['sd-documents-first'](); await settle();
assert.equal(calls.length, firstReads, 'old route cannot dispatch recovery'); context.AdminRouter.epoch--;
sessionGeneration++; actions['sd-documents-first'](); await settle();
assert.equal(calls.length, firstReads, 'old session cannot dispatch recovery'); sessionGeneration--;
overrides.long_document_list = { status: 'ok', documents: [], count: 0, total_count: 0, offset: 0, limit: 50 };
actions['sd-long-refresh'](); await settle();
overrides.long_document_list = { status: 'error', message: 'new filter failed' };
actions['sd-documents-apply'](); await settle();
assert.match(elements.sdDocumentsList.innerHTML, /Previous result retained/);
assert.doesNotMatch(elements.sdDocumentsList.innerHTML, /No documents (in|match)/);
overrides = {};

view = await open('jobs');
view.documents.list = beyondEnd;
const jobsReads = calls.length; actions['sd-documents-first'](); await settle();
assert.equal(calls.length, jobsReads, 'another LONG panel cannot dispatch document recovery');
assert.equal(context.__refresh.state().enabled, false);
let before = calls.length;
actions['sd-ingest-inspect']({ jobId: 'job-a' }); await settle();
assert.equal(calls.length, before, 'Inspect reuses the listed payload');
assert.match(elements.sdIngestDetail.innerHTML, /25%/);
context.__refresh.configure({ enabled: true, intervalSeconds: 15 });
const frameWrites = elements.sdTierPanel.htmlWrites, rowWrites = elements.sdIngestList.htmlWrites;
elements.sdIngestBatch.value = 'draft'; await tick();
assert.equal(calls.at(-1).args.batch_id, undefined, 'typed filter is not applied by a timer');
assert.equal(elements.sdIngestBatch.value, 'draft');
assert.equal(elements.sdTierPanel.htmlWrites, frameWrites); assert.equal(elements.sdIngestList.htmlWrites, rowWrites, 'unchanged source keeps controls and identifiers mounted');
jobs = [{ ...jobs[0], progress_percent: 60 }]; before = calls.length; await tick();
assert.deepEqual(calls.slice(before).map(call => call.tool), ['long_ingest_list']);
assert.match(elements.sdIngestDetail.innerHTML, /60%/);
jobs = []; statusJob = { ...statusJob, progress_percent: 75 }; before = calls.length; await tick();
assert.deepEqual(calls.slice(before).map(call => call.tool), ['long_ingest_list', 'long_ingest_status'], 'one missing active selected job may be followed');
assert.match(elements.sdIngestDetail.innerHTML, /75%/);
statusJob = { status: 'succeeded', job_id: 'job-a', finished_at: '2026-09-29T12:00:00Z', created_entities: 3 }; await tick();
assert.equal(context.__refresh.state().eligible, false, 'terminal detail stops its follow');
assert.equal(timers.some(item => !item.cancelled), false);
before = calls.length; await context.__refresh.refresh();
assert.deepEqual(calls.slice(before).map(call => call.tool), ['long_ingest_list'], 'terminal detail is not automatically rechecked');
actions['sd-ingest-check'](); await settle(); assert.equal(calls.at(-1).tool, 'long_ingest_status');
statusJob = { status: 'not_found', message: 'trimmed' }; actions['sd-ingest-check'](); await settle();
assert.match(elements.sdIngestDetail.innerHTML, /does not establish the ingestion result/);
jobs = [{ job_id: 'job-a', status: 'running', filename: 'a.md', polling: { recommended: false } }];
elements.sdIngestStatus.value = 'running'; elements.sdIngestBatch.value = 'batch-1';
actions['sd-ingest-apply'](); await settle();
assert.equal(calls.at(-1).args.status, 'running'); assert.equal(calls.at(-1).args.batch_id, 'batch-1');
actions['sd-ingest-page']({ step: '1' }); await settle(); assert.equal(calls.at(-1).args.offset, 50);
const lastList = view.ingestions.list, lastDate = view.ingestions.success;
overrides.long_ingest_list = { status: 'error', message: '<list failed>' };
await assert.rejects(context.__refresh.refresh());
assert.equal(view.ingestions.list, lastList); assert.equal(view.ingestions.success, lastDate);
assert.match(elements.sdIngestFreshness.innerHTML, /&lt;list failed&gt;/);
before = calls.length; actions['sd-ingest-page']({ step: '-1' }); await settle();
assert.equal(calls.length, before, 'failed list refresh cannot dispatch stale pagination');
overrides = {}; await context.__refresh.refresh();
actions['sd-ingest-inspect']({ jobId: 'job-a' });
before = calls.length; actions['sd-ingest-cancel']();
assert.equal(calls.length, before, 'cancel cannot occur before confirmation');
assert.match(modal.body, /demo-space/); assert.match(modal.body, /job-a/);
await modal.onConfirm(); await settle();
const cancelled = calls.filter(call => call.tool === 'long_ingest_cancel');
assert.equal(cancelled.length, 1); assert.deepEqual(JSON.parse(JSON.stringify(cancelled[0].args)), { space_id: 'demo-space', job_id: 'job-a' });
assert.equal(view.ingestions.detail.status, 'running', 'cooperative acknowledgement cannot invent a terminal state');
assert.match(elements.sdIngestCancelResult.innerHTML, /Job status: Running/);
assert.match(elements.sdIngestCancelResult.innerHTML, /&lt;cancellation requested&gt;/);
overrides.long_ingest_cancel = () => { throw new Error('connection lost'); };
actions['sd-ingest-cancel'](); await modal.onConfirm(); await settle();
assert.match(elements.sdIngestCancelResult.innerHTML, /Job status: Running/);
assert.equal(calls.filter(call => call.tool === 'long_ingest_cancel').length, 2, 'unknown outcome cannot replay a mutation');
overrides = {};
modal = null; view = await open('jobs', ['read']); actions['sd-ingest-inspect']({ jobId: 'job-a' }); before = calls.length;
actions['sd-ingest-cancel'](); await settle(); assert.equal(calls.length, before, 'read-only view cannot cancel'); assert.equal(modal, null, 'read-only view cannot open a cancel confirmation');

// #632: cancellation targets a row independently of the inspector, and must
// revalidate activity and permission when its confirmation is submitted.
view = await open('jobs');
actions['sd-ingest-cancel']({ jobId: 'job-a' });
assert.equal(view.ingestions.selected, undefined, 'row cancellation does not change inspector selection');
before = calls.length;
view.ingestions.list.jobs[0].status = 'succeeded';
assert.equal(await modal.onConfirm(), false);
assert.equal(calls.length, before, 'completed target cannot be cancelled by a stale modal');
jobs[0].status = 'running';
actions['sd-ingest-cancel']({ jobId: 'job-a' });
view.ctx.identity.permissions = ['read'];
assert.equal(await modal.onConfirm(), false);
assert.equal(calls.length, before, 'permission is checked again at confirmation');
view.ctx.identity.permissions = ['admin'];

// Superseded refresh consumers report skipped, including while awaiting selected detail.
view = await open('jobs');
let resolveList;
overrides.long_ingest_list = new Promise(resolve => { resolveList = resolve; });
const savedList = view.ingestions.list;
let pending = context.__spaceDetail.refreshIngestions(view, { isCurrent: () => true });
view.ingestions.revision++;
resolveList({ status: 'ok', jobs: [], total: 0, count: 0, offset: 0 });
assert.equal((await pending)?.skipped, true, 'superseded list must not report successful refresh');
assert.equal(view.ingestions.list, savedList);
overrides = {}; actions['sd-ingest-inspect']({ jobId: 'job-a' });
const savedDetail = view.ingestions.detail;
view.ingestions.checkDetail = true;
let resolveDetail;
overrides.long_ingest_status = new Promise(resolve => { resolveDetail = resolve; });
pending = context.__spaceDetail.refreshIngestions(view, { isCurrent: () => true });
await settle(); view.ingestions.revision++;
resolveDetail({ job_id: 'job-a', status: 'succeeded' });
assert.equal((await pending)?.skipped, true, 'superseded selected detail must not report successful refresh');
assert.equal(view.ingestions.detail, savedDetail);
overrides = {};

// R1 F1: pagination fields are untrusted; malformed values are not ranges or HTML.
view = await open('jobs'); actions['sd-ingest-inspect']({ jobId: 'job-a' });
for (const fields of [{ partial: true }, { count: 1 }, { offset: -1 }, { total: '0' }, { total: 51 }]) {
    overrides.long_ingest_list = { status: 'ok', jobs: [], count: 0, total: 0, offset: 0, ...fields };
    await context.__spaceDetail.refreshIngestions(view, { isCurrent: () => true });
    assert.equal(view.ingestions.selected, 'job-a', 'incomplete empty response preserves selection');
}
overrides = {};
view.ingestions.checkDetail = true;
let resolveLateEmptyDetail;
let emptyRaceStatusReads = 0;
overrides.long_ingest_status = () => ++emptyRaceStatusReads === 1
    ? new Promise(resolve => { resolveLateEmptyDetail = resolve; })
    : { job_id: 'job-a', status: 'running' };
const pendingBeforeEmpty = context.__spaceDetail.refreshIngestions(view, { isCurrent: () => true });
await settle();
overrides.long_ingest_list = { status: 'ok', jobs: [], count: 0, total: 0, offset: 0 };
assert.equal((await context.__spaceDetail.refreshIngestions(view, { isCurrent: () => true })).follow, false);
assert.equal(view.ingestions.selected, null, 'confirmed empty resets selected identity');
assert.equal(view.ingestions.detail, null, 'confirmed empty closes prior detail');
assert.equal(view.ingestions.checkDetail, false);
assert.equal(view.ingestions.detailSuccess, null);
resolveLateEmptyDetail({ job_id: 'job-a', status: 'running', progress_percent: 99 });
assert.equal((await pendingBeforeEmpty).skipped, true, 'late detail consumer is invalidated by confirmed empty');
assert.equal(view.ingestions.detail, null, 'late detail cannot resurrect an empty inspector');
overrides = {};
for (const [list, step] of [
    [{ offset: 0, count: 1, total: 51 }, '-1'],
    [{ offset: 50, count: 1, total: 51 }, '1'],
    [{ offset: 0.5, count: 1, total: 100 }, '1'],
    [{ offset: 0, count: 0, total: 51 }, '1'],
    [{ offset: 0, count: 1, total: '100' }, '1'],
    [{ offset: 0, count: 1, total: 100, partial: true }, '1'],
]) {
    overrides.long_ingest_list = { status: 'ok', jobs: list.count === 0 ? [] : [jobs[0]], ...list };
    await open('jobs');
    before = calls.length; actions['sd-ingest-page']({ step }); await settle();
    assert.equal(calls.length, before, `ingestion dispatch rejects ${JSON.stringify(list)} / ${step}`);
}
overrides = {};
for (const [panel, tool, pageId, collection, totalKey] of [
    ['documents', 'long_document_list', 'sdDocumentsPage', 'documents', 'total_count'],
    ['jobs', 'long_ingest_list', 'sdIngestPage', 'jobs', 'total'],
]) {
    for (const fields of [
        { offset: '<b id="injected">spoof</b>', count: 1 },
        { offset: 0, count: '<img src=x onerror=alert(1)>' },
        { count: 1 }, { offset: 0 }, { offset: null, count: 1 }, { offset: 0, count: true },
        { offset: -1, count: 1 }, { offset: 0, count: -1 },
        { offset: 0.5, count: 1 }, { offset: 0, count: 0.5 },
        { offset: Number.MAX_SAFE_INTEGER + 1, count: 0 },
        { offset: 0, count: Number.MAX_SAFE_INTEGER + 1 },
        { offset: Number.MAX_SAFE_INTEGER, count: 1 },
    ]) {
        overrides[tool] = { status: 'ok', [collection]: [], [totalKey]: 1, ...fields };
        await open(panel);
        assert.match(elements[pageId].innerHTML, /Pagination unavailable/, `${panel} rejects invalid pagination ${JSON.stringify(fields)}`);
        assert.doesNotMatch(elements[pageId].innerHTML, /<b|<img|NaN|undefined/);
    }
    overrides[tool] = { status: 'ok', [collection]: [], offset: 0, count: 1, [totalKey]: '<b id="injected">spoof</b>' };
    await open(panel);
    assert.match(elements[pageId].innerHTML, /1–1 \/ —/);
    assert.doesNotMatch(elements[pageId].innerHTML, /<b|spoof/);
    overrides[tool] = { status: 'ok', [collection]: [], offset: 0, count: 0, [totalKey]: 0 };
    await open(panel); assert.match(elements[pageId].innerHTML, /0–0 \/ 0/);
    overrides = {};
}

// PR640 R1 F1–F3: preserve banners and refuse metrics outside the backend contract.
overrides.graph_status = { status: 'ok', connected: false, embedded: true, bound: false };
await open('overview');
assert.match(elements.sdLongSnapshot.innerHTML, /sd-banner--warn[^]*<strong>Waiting for the first ingestion<\/strong>/, 'R1 F1 unbound attention banner');
assert.match(elements.sdLongSnapshot.innerHTML, /The first MID archive or documentary ingestion connects this space automatically\./);
assert.match(elements.sdLongSnapshot.innerHTML, /Unbound/);
overrides.graph_status = { status: 'ok', connected: false, message: 'No Graph Memory connection is configured' };
await open('overview');
assert.match(elements.sdLongSnapshot.innerHTML, /role="alert"/, 'R1 F2 unavailable runtime alert');
assert.match(elements.sdLongSnapshot.innerHTML, /Long runtime unavailable/);
assert.match(elements.sdLongSnapshot.innerHTML, /The required embedded long runtime is not configured for this space\./);
assert.match(elements.sdLongSnapshot.innerHTML, /Not configured/);
assert.doesNotMatch(elements.sdLongSnapshot.innerHTML, />Unknown</);
// Deliberately off-contract stale counts prove that reachability is independently required.
const staleCounts = { document_count: 4, entity_count: 5, relation_count: 6, entity_types: { stale: 5 } };
for (const payload of [
    { reachable: false, graph_stats: staleCounts },
    { graph_stats: staleCounts },
    { reachable: true, graph_stats: [] },
    { reachable: true, graph_stats: null },
    { reachable: true },
    { reachable: true, graph_stats: 'unavailable' },
    { reachable: true, graph_stats: new Date() },
]) {
    overrides.graph_status = { status: 'ok', connected: true, binding: 'embedded', ...payload };
    await open('overview');
    assert.doesNotMatch(elements.sdLongSnapshot.innerHTML, /class="metric-card"|Entities by type/, 'R1 F3 no metrics outside contract');
    assert.equal((elements.sdLongSnapshot.innerHTML.match(/<unavailable>/g) || []).length, 1, 'R1 F3 one unavailable state');
}
overrides.graph_status = { status: 'ok', connected: true, reachable: true, binding: 'explicit', config: { url: 'https://graph.example.invalid/v1', ontology: '<ontology>', memory_id: '<memory-id>' }, graph_stats: { document_count: 1, entity_count: 2, relation_count: 3, entity_types: { '<img src=x>': 2 } } };
await open('overview');
assert.match(elements.sdLongSnapshot.innerHTML, /&lt;img src=x&gt;/, 'R1 escaping of entity types');
assert.match(elements.sdLongSnapshot.innerHTML, /&lt;ontology&gt;/, 'R1 escaping of configured ontology');
assert.match(elements.sdLongSnapshot.innerHTML, /&lt;memory-id&gt;/, 'R1 escaping of memory ID');
assert.doesNotMatch(elements.sdLongSnapshot.innerHTML, /<img/);
overrides = {};
// Existing R1 F2/F3: a snapshot does not promise future data, and failed status is not absent config.
await open('graph');
assert.match(elements.sdLongSnapshot.innerHTML, /No nodes in this graph view/);
assert.match(elements.sdLongSnapshot.innerHTML, /Explore your documents or check LONG status in Overview/);
assert.doesNotMatch(elements.sdLongSnapshot.innerHTML, /Refresh after an ingestion/);
for (const status of ['not_found', 'read_only', 'rate_limited']) {
    overrides.graph_status = { status, message: '<status failed>', ...(status === 'not_found' ? {} : { mid_automation: { compaction_enabled: true, archive_enabled: false }, mid_archive_projection: { pending: 2 } }) };
    for (const panel of ['overview', 'graph', 'ontology']) {
        view = await open(panel);
        const html = elements[panel === 'ontology' ? 'sdOntologyConfig' : 'sdLongSnapshot'].innerHTML;
        assert.match(html, status === 'not_found' ? /Space not found/ : /<unavailable>/, `${panel} retains ${status}`);
        assert.doesNotMatch(html, /Configured ontology is not reported/);
        assert.equal(view.longSuccess, null);
    }
}
for (const panel of ['overview', 'graph', 'ontology']) {
    overrides = {}; view = await open(panel);
    const goodLongData = view.longData, goodLongDate = view.longSuccess;
    const region = panel === 'ontology' ? 'sdOntologyConfig' : 'sdLongSnapshot';
    const goodMarkup = elements[region].innerHTML;
    overrides.graph_status = { status: 'rate_limited', message: '<try later>', mid_automation: { compaction_enabled: false, archive_enabled: true }, mid_archive_projection: { pending: 9 } };
    actions['sd-long-refresh'](); await settle();
    assert.equal(view.longData, goodLongData, 'R1 F4 keep successful LONG snapshot');
    assert.equal(view.longSuccess, goodLongDate, 'R1 F4 keep success timestamp');
    assert.equal(elements[region].innerHTML, goodMarkup, 'R1 F4 keep visible content');
    assert.match(elements.sdLongStatus.innerHTML, /&lt;try later&gt;/);
    assert.match(elements.sdLongStatus.innerHTML, new RegExp(goodLongDate));
    if (panel === 'overview') assert.match(elements.sdLongSnapshot.innerHTML, /MID → LONG automation/);
}
overrides.graph_status = { status: 'ok', connected: true };
view = await open('ontology');
assert.match(elements.sdOntologyConfig.innerHTML, /Configured ontology is not reported/);
overrides = {};

// PR642 F1: shrinking totals are recoverable only by an explicit valid action.
for (const total of [0, 40]) {
    overrides.long_ingest_list = { status: 'ok', jobs: [], offset: 50, count: 0, total };
    view = await open('jobs'); before = calls.length;
    assert.match(elements.sdIngestList.innerHTML, /Back to first page/);
    await settle(); assert.equal(calls.length, before);
    actions['sd-ingest-first'](); await settle();
    assert.equal(calls.length, before + 1); assert.equal(calls.at(-1).args.offset, 0);
}
for (const patch of [{ partial: true }, { count: 1 }, { offset: -50 }, { total: '0' }, { offset: 0 }]) {
    overrides.long_ingest_list = { status: 'ok', jobs: [], offset: 50, count: 0, total: 0, ...patch };
    view = await open('jobs'); before = calls.length;
    assert.doesNotMatch(elements.sdIngestList.innerHTML, /Back to first page/);
    actions['sd-ingest-first'](); await settle(); assert.equal(calls.length, before);
}
overrides = {};

// F2: the cancelled target owns the single status slot ahead of a missing selection.
for (const outcome of ['cancelled', 'running', 'not_found', 'error', 'wrong-id']) {
    jobs = [{ job_id: 'job-a', status: 'running' }, { job_id: 'job-b', status: 'running' }];
    view = await open('jobs'); actions['sd-ingest-inspect']({ jobId: 'job-b' });
    overrides.long_ingest_cancel = () => {
        overrides.long_ingest_list = { status: 'ok', jobs: [{ job_id: 'job-c', status: 'running' }], offset: 0, count: 1, total: 1 };
        return { status: 'cancelling', message: '<cooperative request>' };
    };
    overrides.long_ingest_status = { job_id: outcome === 'wrong-id' ? 'unrelated' : 'job-a', status: outcome === 'wrong-id' ? 'cancelled' : outcome, message: '<status response>' };
    actions['sd-ingest-cancel']({ jobId: 'job-a' }); before = calls.length;
    await modal.onConfirm(); await settle();
    assert.deepEqual(calls.slice(before).map(c => c.tool), ['long_ingest_cancel', 'long_ingest_list', 'long_ingest_status']);
    assert.equal(calls.at(-1).args.job_id, 'job-a');
    assert.match(elements.sdIngestCancelResult.innerHTML, /Updated/);
    assert.equal(view.ingestions.detail.job_id, 'job-b');
    assert.equal(view.ingestions.cancelResult.job_status, ['cancelled', 'running'].includes(outcome) ? outcome : null);
    assert.match(elements.sdIngestCancelResult.innerHTML, ['cancelled', 'running'].includes(outcome) ? /Job status:/ : /result not confirmed/);
    overrides = {};
}
// Reuse a listed cancellation target, without consuming an extra status call.
jobs = [{ job_id: 'job-a', status: 'running' }]; view = await open('jobs');
overrides.long_ingest_cancel = () => { jobs = [{ job_id: 'job-a', status: 'cancelled' }]; return { status: 'cancelling' }; };
actions['sd-ingest-cancel']({ jobId: 'job-a' }); before = calls.length;
await modal.onConfirm(); await settle();
assert.deepEqual(calls.slice(before).map(c => c.tool), ['long_ingest_cancel', 'long_ingest_list']);
assert.equal(view.ingestions.cancelResult.job_status, 'cancelled');
overrides = {};
// A delayed cancellation status cannot overwrite a superseding local revision.
jobs = [{ job_id: 'job-a', status: 'running' }]; view = await open('jobs');
view.ingestions.cancelResult = { job_id: 'job-a', status: 'cancelling', requested_at: new Date().toISOString() }; view.ingestions.cancelPending = true;
overrides.long_ingest_list = { status: 'ok', jobs: [{ job_id: 'job-c', status: 'running' }], offset: 0, count: 1, total: 1 };
overrides.long_ingest_status = new Promise(resolve => { resolveOld = resolve; });
const delayedCancel = context.__refresh.refresh(); await settle();
view.ingestions.revision++;
resolveOld({ job_id: 'job-a', status: 'cancelled' });
assert.equal((await delayedCancel).skipped, true);
assert.equal(view.ingestions.cancelResult.job_status, undefined);
assert.equal(view.ingestions.cancelResult.checked_at, undefined);
overrides = {}; jobs = [{ job_id: 'job-a', status: 'running' }];

// All delayed readers and confirmations retain the route/session ownership.
view = await open('jobs'); actions['sd-ingest-inspect']({ jobId: 'job-a' }); actions['sd-ingest-cancel']();
sessionGeneration++; context.__refresh.endSession(); before = calls.length;
assert.equal(await modal.onConfirm(), false); assert.equal(calls.length, before);
context.__refresh.beginSession(); view = await open('documents');
overrides.long_document_get = new Promise(resolve => { resolveOld = resolve; });
actions['sd-document-inspect']({ key: 'id:doc-a' }); await settle();
sessionGeneration++; context.__refresh.endSession(); resolveOld({ status: 'ok', document: docs[0] }); await settle();
assert.equal(view.documents.detail, null, 'old-session metadata cannot paint');
overrides = {}; view = await open('jobs');
assert.match(view.ingestions.error, /Sign out and sign in again/);
console.log('admin LONG panels runtime: ok');
