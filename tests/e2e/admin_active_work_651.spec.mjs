import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// #651: Space "Active work" and the archive scope of LONG ingestion jobs, on the
// real admin bundle with a controlled API. Every request is intercepted.
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const STATIC = path.join(ROOT, 'src/live_mem/static');
const ORIGIN = 'http://127.0.0.1:18651';
const PROOF = process.env.HIVEMIND_QA651_CAPTURE_DIR || path.join(ROOT, 'proof-artifacts/portal-qa-651');
const HOSTILE = '<img src=x onerror="window.__xss=1">';
// Read tools only; the embedded Consolidation view opened from a row reads its own lanes and job.
const READ_TOOLS = new Set(['system_whoami', 'space_info', 'live_read', 'graph_status', 'long_ingest_list', 'long_ingest_status', 'bank_consolidation_queues', 'bank_consolidation_status']);
const CAPTURE = 'demo/2026-10-06T07-12-00-0123456789abcdef0123456789abcdef';
const VIEWPORTS = [[1440, 900], [768, 1024], [390, 844]];

const consolidationJob = (status, extra = {}) => ({
    status, job_id: `consol_651_${status}`, space_id: 'demo', agent: '', scope: 'all_agents', scope_label: 'All agents',
    requested_by: 'qa-651', guarantee: 'in_memory_best_effort', requested_at: '2026-10-06T08:00:00Z', queued_at: '2026-10-06T08:00:00Z',
    started_at: null, finished_at: null, progress: { phase: 'queued' }, ...extra,
});
const RUNNING = consolidationJob('running', {
    queue_position: 1, started_at: '2026-10-06T08:00:02Z',
    progress: { phase: 'compacting', batch_size: 2, notes_total: 12, notes_done: 12, batches_total: 6, batches_done: 6, current_batch: 6 },
});
const QUEUED = consolidationJob('queued', { queue_position: 2, agent: 'qa-agent', scope: 'agent', scope_label: 'Agent: qa-agent' });
const FINISHED = consolidationJob('succeeded', {
    started_at: '2026-10-06T07:00:02Z', finished_at: '2026-10-06T07:12:40Z',
    progress: { phase: 'done', notes_total: 8, notes_done: 8, batches_total: 4, batches_done: 4 },
    result: { status: 'ok', notes_total: 8, notes_processed: 8, auto_compaction: { status: 'ok', started_at: '2026-10-06T07:12:00Z', finished_at: '2026-10-06T07:12:39Z', preimage_id: CAPTURE } },
});
const lane = (running, queued, latest) => ({
    space_id: 'demo', lane_state: running ? 'running' : queued.length ? 'queued' : 'idle', parallelism_model: 'one_worker_per_space',
    guarantee: 'in_memory_best_effort', running_job: running, queued_count: queued.length, queued_job_ids: queued.map(job => job.job_id),
    queued_jobs: queued, latest_jobs: latest, service_config: { batch_size: 2 },
});
const graph = (projection, extra = {}) => ({
    status: 'ok', space_id: 'demo', connected: true, reachable: true, binding: 'embedded', embedded: true, bound: true,
    config: { ontology: 'general' }, mid_automation: { compaction_enabled: true, archive_enabled: true },
    mid_archive_projection: { oldest_at: null, oldest_age_seconds: null, error: null, ...projection }, ...extra,
});
const DOC_RUNNING = { job_id: 'ing_doc_651', filename: 'roadmap.pdf', source_path: 'docs/roadmap.pdf', status: 'running', current_step: 'llm_extract', progress_percent: 45, queue_position: 1, updated_at: '2026-10-06T08:01:00Z' };
const DOC_QUEUED = { job_id: 'ing_doc_651_q', filename: 'glossary.md', source_path: 'docs/glossary.md', status: 'queued', current_step: 'queued', queue_position: 2 };
const DOC_DONE = { job_id: 'ing_doc_651_done', filename: 'charter.md', source_path: 'docs/charter.md', status: 'succeeded', current_step: 'done', progress_percent: 100, created_entities: 12, created_relations: 9, updated_at: '2026-10-06T06:00:00Z' };
const ARCHIVE_RUNNING = { job_id: 'ing_arc_651', filename: 'activeContext.md', source_path: `.hivemind/mid-archive/${CAPTURE}/bank/activeContext.md`, status: 'running', current_step: 'embedding', progress_percent: 70, queue_position: 1, updated_at: '2026-10-06T08:02:00Z' };
const NOT_CONFIGURED = { status: 'error', reason: 'archive_not_configured', message: 'No archive destination is configured.' };

const scenarios = {
    active: () => ({ lane: lane(RUNNING, [QUEUED], [QUEUED, RUNNING, FINISHED]), graph: graph({ pending: 1, oldest_at: '2026-10-06T07:12:39Z', oldest_age_seconds: 3000 }), documents: [DOC_RUNNING, DOC_QUEUED, DOC_DONE], archive: [ARCHIVE_RUNNING] }),
    firstCapture: () => ({ lane: lane(null, [], []), graph: { status: 'ok', space_id: 'demo', connected: false, embedded: true, bound: false, message: 'Waiting for the first ingestion', mid_automation: { compaction_enabled: true, archive_enabled: true }, mid_archive_projection: { pending: 1, oldest_at: '2026-10-06T07:12:39Z', oldest_age_seconds: 60, error: null } }, documents: [], archive: NOT_CONFIGURED }),
    outage: () => ({ lane: lane(null, [], [FINISHED]), graph: graph({ pending: 2, oldest_at: '2026-10-06T07:12:39Z' }, { reachable: false, error: 'Connection refused' }), documents: [DOC_RUNNING], archive: [ARCHIVE_RUNNING] }),
    finished: () => ({ lane: lane(null, [], [FINISHED]), graph: graph({ pending: 0 }), documents: [DOC_DONE], archive: [ARCHIVE_RUNNING] }),
    pendingNoJob: () => ({ lane: lane(null, [], []), graph: graph({ pending: 1, oldest_at: '2026-10-06T07:12:39Z' }), documents: [], archive: [] }),
    projectionUnavailable: () => ({ lane: lane(null, [], []), graph: graph({ pending: 0, error: 'projection_unavailable' }), documents: [], archive: NOT_CONFIGURED }),
    archiveFailedJob: () => ({ lane: lane(null, [], []), graph: graph({ pending: 1, oldest_at: '2026-10-06T07:12:39Z' }), documents: [], archive: [{ ...ARCHIVE_RUNNING, status: 'failed', current_step: 'failed', error: 'Embedding provider refused the batch', updated_at: '2026-10-06T08:05:00Z' }] }),
    archiveFailure: () => ({ lane: lane(null, [], []), graph: graph({ pending: 1 }), documents: [], archive: { status: 'error', reason: 'archive_unavailable', message: 'The archive destination could not be verified.' } }),
};

async function setup(page, name, { hash = '#/spaces/demo/activity', permissions = ['admin'], mutate } = {}) {
    const state = { calls: [], ...scenarios[name]() };
    mutate?.(state);
    state.reads = tool => state.calls.filter(call => call.tool === tool);
    await page.addInitScript(() => { window.__xss = 0; });
    await page.route('**/*', async route => {
        const pathname = new URL(route.request().url()).pathname;
        const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
        if (pathname === '/api/spaces') return json({ status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (pathname === '/health') return json({ version: 'qa-651' });
        if (pathname === '/api/tool') {
            const call = route.request().postDataJSON(); state.calls.push(call);
            const args = call.arguments || {};
            if (call.tool === 'system_whoami') return json({ status: 'ok', client_name: 'qa-651', permissions, token_hash: 'sha256:' + '1'.repeat(64) });
            if (call.tool === 'space_info') return json({ status: 'ok', space_id: 'demo', description: 'Active work QA', owner: 'qa', created_at: '2026-09-01T00:00:00Z', hive_status_label: 'local_only', live: { notes_count: 3, total_size: 900 }, bank: { files_count: 6, total_size: 51000, files: [] }, consolidation_count: 4, consolidation_queue: state.lane });
            if (call.tool === 'bank_consolidation_queues') return json({ status: 'ok', lanes: [state.lane], denied_spaces: [] });
            if (call.tool === 'bank_consolidation_status') return json([state.lane.running_job, ...state.lane.queued_jobs, ...state.lane.latest_jobs].find(job => job?.job_id === args.job_id) || { status: 'not_found', job_id: args.job_id });
            if (call.tool === 'graph_status') return json(state.graph);
            if (call.tool === 'long_ingest_list') {
                if (state.hold && args.archive !== true && args.limit === 50) await state.hold;
                state.onList?.(args);
                const source = args.archive === true ? state.archive : state.documents;
                if (!Array.isArray(source)) return json(source);
                const filtered = source.filter(job => !args.status || job.status === args.status);
                const jobs = filtered.slice(args.offset || 0, (args.offset || 0) + args.limit);
                return json({ status: 'ok', jobs, count: jobs.length, total: filtered.length, offset: args.offset || 0, limit: args.limit, guarantee: 'in_memory_best_effort' });
            }
            if (call.tool === 'long_ingest_status') return json(state.documents.find(job => job.job_id === args.job_id) || { status: 'not_found', job_id: args.job_id });
            return json({ status: 'error', message: `Unexpected fixture tool: ${call.tool}` });
        }
        const relative = pathname === '/admin.html' || pathname === '/' ? 'admin.html' : pathname.startsWith('/static/') ? pathname.slice(8) : '';
        const file = path.join(STATIC, relative);
        if (relative && file.startsWith(STATIC) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
            // Mutation runs substitute only the Space Detail view (never edit the tree under test).
            const source = process.env.HIVEMIND_QA651_SOURCE && relative.endsWith('/views-space-detail.js') ? process.env.HIVEMIND_QA651_SOURCE : file;
            return route.fulfill({ contentType: types[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(source) });
        }
        return route.fulfill({ status: 404, body: '' });
    });
    await page.goto(`${ORIGIN}/admin.html${hash}`);
    return state;
}

const row = (page, key) => page.locator(`#sdActivityList [data-activity="${key}"]`);
const status = (page, key) => row(page, key).locator('.sd-activity-head .status-dot-label');
const settled = async page => {
    await expect(page.locator('#sdActivityFreshness')).toContainText('Updated');
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
};

async function capture(page, name, selector = '.sd-activity') {
    fs.mkdirSync(PROOF, { recursive: true });
    const target = page.locator(selector);
    await target.evaluate(el => { el.style.scrollMarginTop = `${document.getElementById('portalTopbar').getBoundingClientRect().height + 12}px`; el.scrollIntoView({ block: 'start' }); });
    await page.screenshot({ path: path.join(PROOF, `${name}-viewport.png`) });
    // The console scrolls inside its content region: capture each Active work
    // row separately so narrow viewports keep a complete visual record.
    const rows = selector === '.sd-activity' ? await page.locator('#sdActivityList > li').all() : [target];
    for (const [index, item] of rows.entries()) await item.screenshot({ path: path.join(PROOF, `${name}-${index + 1}.png`) });
}

function expectReadOnly(state) {
    expect(state.calls.map(call => call.tool).filter(tool => !READ_TOOLS.has(tool))).toEqual([]);
}

function expectScopes(state) {
    // The archive scope is always explicit; documentary reads never carry it.
    for (const call of state.reads('long_ingest_list')) expect([undefined, true]).toContain(call.arguments.archive);
}

for (const [width, height] of VIEWPORTS) {
    test(`651 active work identifies each job source at ${width}x${height}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const state = await setup(page, 'active');
        await settled(page);
        await expect(page.getByRole('tab', { name: 'Active work', exact: true })).toHaveAttribute('aria-selected', 'true');

        const consolidation = row(page, 'consolidation');
        await expect(consolidation.locator('.sd-activity-head .status-dot-label')).toHaveText('Running');
        for (const text of ['Phase: Compacting MID files', '12/12 notes', '6/6 batches', 'All agents', '1 more queued']) await expect(consolidation).toContainText(text);
        const compaction = row(page, 'compaction');
        await expect(compaction.locator('.sd-activity-head .status-dot-label')).toHaveText('In progress');
        await expect(compaction).toContainText('No percentage is reported');
        await expect(compaction).not.toContainText('%');

        const documents = row(page, 'documents');
        await expect(documents.locator('.sd-activity-head .status-dot-label')).toHaveText('Running');
        await expect(documents).toContainText('1 running · 1 queued');
        await expect(documents).toContainText('roadmap.pdf');
        await expect(documents).toContainText('Extracting entities and relations');
        await expect(documents).toContainText('45% reported');
        await expect(documents).toContainText('glossary.md');
        const archive = row(page, 'archive');
        await expect(archive.locator('.sd-activity-head .status-dot-label')).toHaveText('Running');
        await expect(archive).toContainText('1 capture pending indexing');
        await expect(archive).toContainText('activeContext.md');
        await expect(archive).toContainText('Creating embeddings');
        await expect(archive).toContainText('70% reported');
        // Never mixed: each memory only lists its own jobs.
        for (const name of ['activeContext.md']) await expect(documents).not.toContainText(name);
        for (const name of ['roadmap.pdf', 'glossary.md', 'charter.md']) await expect(archive).not.toContainText(name);

        // A queued job without reported progress gets no invented bar, percent or time.
        const queuedItem = documents.locator('.sd-activity-job').filter({ hasText: 'glossary.md' });
        await expect(queuedItem.locator('progress')).toHaveCount(0);
        await expect(queuedItem).not.toContainText('%');
        await expect(queuedItem).not.toContainText('Updated');
        await expect(queuedItem).toContainText('Position 2');

        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        for (const button of await page.locator('#sdActivityList button').all()) expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(44);
        await capture(page, `active-${width}`);

        expect(state.reads('graph_status').map(call => call.arguments)).toEqual([{ space_id: 'demo', include_graph: false }]);
        expect(state.reads('long_ingest_list').map(call => call.arguments)).toEqual(expect.arrayContaining([
            { space_id: 'demo', limit: 10, status: 'running' }, { space_id: 'demo', limit: 10, status: 'queued' },
            { space_id: 'demo', limit: 10, archive: true, status: 'running' }, { space_id: 'demo', limit: 10, archive: true, status: 'queued' },
        ]));
        expect(state.reads('long_ingest_list')).toHaveLength(4);
        // The entry cycle reuses the lane already returned by space_info.
        expect(state.reads('space_info')).toHaveLength(1);
        expect(state.reads('bank_consolidation_queues')).toHaveLength(0);
        expectReadOnly(state); expectScopes(state);
    });
}

test('651 an archive capture opens its existing ingestion detail in one click, read-only', async ({ page }) => {
    const state = await setup(page, 'active');
    await settled(page);
    await row(page, 'archive').getByRole('button', { name: 'Open capture indexing job activeContext.md' }).click();
    await expect(page).toHaveURL(/#\/spaces\/demo\/long\/jobs$/);
    expect(page.url()).not.toContain('ing_arc_651');
    await expect(page.getByRole('button', { name: 'MID capture archive' })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('button', { name: 'Space documents' })).toHaveAttribute('aria-pressed', 'false');
    const detail = page.locator('#sdIngestDetail');
    await expect(detail).toBeVisible();
    await expect(detail).toBeFocused();
    await expect(detail).toContainText('activeContext.md');
    await expect(detail).toContainText('MID capture archive');
    await expect(detail).toContainText('70% reported');
    await expect(page.locator('#sdIngestList')).toContainText('activeContext.md');
    for (const name of ['roadmap.pdf', 'glossary.md', 'charter.md']) await expect(page.locator('#sdIngestList')).not.toContainText(name);
    await expect(page.locator('[data-action="sd-ingest-cancel"]')).toHaveCount(0);
    await expect(detail).not.toContainText('Open document catalog');
    await expect.poll(() => state.reads('long_ingest_list').at(-1)?.arguments).toEqual({ space_id: 'demo', limit: 50, offset: 0, archive: true });
    // Status/cancel tools address the primary memory: never used for an archive job.
    await detail.getByRole('button', { name: 'Check selected job' }).click();
    await expect.poll(() => state.reads('long_ingest_list').filter(call => call.arguments.limit === 50).length).toBe(2);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.reads('long_ingest_status')).toHaveLength(0);
    expect(state.reads('long_ingest_cancel')).toHaveLength(0);
    await capture(page, 'archive-job-detail', '#sdIngestWorkspace');

    // Switching back clears the archive rows before the documentary read.
    let release;
    state.hold = new Promise(resolve => { release = resolve; });
    await page.getByRole('button', { name: 'Space documents' }).click();
    await expect.poll(() => state.reads('long_ingest_list').at(-1)?.arguments).toEqual({ space_id: 'demo', limit: 50, offset: 0 });
    // While the documentary read is pending, no archive row remains on screen.
    await expect(page.locator('#sdIngestList')).toContainText('Loading ingestion jobs');
    await expect(page.locator('#sdIngestList')).not.toContainText('activeContext.md');
    release(); state.hold = null;
    await expect(page.locator('#sdIngestDetail')).toBeHidden();
    await expect(page.locator('#sdIngestList')).toContainText('roadmap.pdf');
    await expect(page.locator('#sdIngestList')).not.toContainText('activeContext.md');
    await expect(page.locator('[data-action="sd-ingest-cancel"]').first()).toBeVisible();
    expect(state.reads('long_ingest_list').at(-1).arguments).toEqual({ space_id: 'demo', limit: 50, offset: 0 });
    expectReadOnly(state); expectScopes(state);
});

test('651 an archive job missing from the listed page is kept, flagged and not followed', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('hivemind.portal.autoRefresh', JSON.stringify({ enabled: true, intervalSeconds: 15 })));
    await page.clock.install();
    const state = await setup(page, 'active');
    await settled(page);
    // The archive history moved on: the selected job is no longer listed.
    state.archive = [{ ...ARCHIVE_RUNNING, job_id: 'ing_arc_651_other', filename: 'progress.md', status: 'succeeded', current_step: 'done', progress_percent: 100 }];
    await row(page, 'archive').getByRole('button', { name: 'Open capture indexing job activeContext.md' }).click();
    const detail = page.locator('#sdIngestDetail');
    await expect(detail).toContainText('not in the listed archive page');
    await expect(detail).toContainText('activeContext.md');
    await expect(page.locator('#sdIngestList')).toContainText('progress.md');
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    // Not followed: the controller is not eligible, and no cycle starts later.
    expect(await page.evaluate(() => PortalRefresh.state().eligible)).toBe(false);
    const reads = state.calls.length;
    await page.clock.runFor(60001);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.calls).toHaveLength(reads);
    expect(state.reads('long_ingest_status')).toHaveLength(0);
    expectReadOnly(state); expectScopes(state);
});

test('651 a document ingestion and a consolidation open their existing detail', async ({ page }) => {
    const state = await setup(page, 'active');
    await settled(page);
    await row(page, 'documents').getByRole('button', { name: 'Open document ingestion job roadmap.pdf' }).click();
    await expect(page).toHaveURL(/#\/spaces\/demo\/long\/jobs$/);
    await expect(page.getByRole('button', { name: 'Space documents' })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#sdIngestDetail')).toContainText('roadmap.pdf');
    await expect(page.locator('#sdIngestDetail')).not.toContainText('MID capture archive');
    await expect.poll(() => state.reads('long_ingest_list').at(-1)?.arguments).toEqual({ space_id: 'demo', limit: 50, offset: 0 });

    await page.getByRole('tab', { name: 'Active work', exact: true }).click();
    await settled(page);
    // Keyboard activation from the row action.
    await row(page, 'consolidation').getByRole('button', { name: 'Open consolidation job consol_651_running' }).focus();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#\/spaces\/demo\/consolidation$/);
    const modal = page.locator('#adminModal');
    await expect(modal).toContainText('Consolidation job');
    await expect(modal).toContainText('consol_651_running');
    await expect.poll(() => state.reads('bank_consolidation_status').map(call => call.arguments)).toEqual([{ job_id: 'consol_651_running' }]);
    expect(page.url()).not.toContain('consol_651_running');
    await page.keyboard.press('Escape');

    await page.getByRole('tab', { name: 'Active work', exact: true }).click();
    await settled(page);
    await row(page, 'compaction').getByRole('button', { name: /compaction/ }).click();
    await expect(page.locator('#adminModal')).toContainText('consol_651_running');
    expectReadOnly(state); expectScopes(state);
});

test('651 first capture without archive is a normal wait, distinct from failures', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await setup(page, 'firstCapture');
    await settled(page);
    const archive = row(page, 'archive');
    await expect(archive.locator('.sd-activity-head .status-dot-label')).toHaveText('Archive not yet created');
    await expect(archive.locator('.dot-warn')).toHaveCount(1);
    await expect(archive).toContainText('1 capture pending indexing');
    await expect(archive).toContainText('normal wait, not an outage');
    await expect(status(page, 'documents')).toHaveText('Not bound yet');
    await expect(status(page, 'consolidation')).toHaveText('Idle');
    await expect(page.locator('#sdActivityList .dot-error')).toHaveCount(0);
    expect(state.reads('long_ingest_list').map(call => call.arguments)).toEqual([{ space_id: 'demo', limit: 10, archive: true, status: 'running' }]);
    await capture(page, 'first-capture-390');

    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/jobs`);
    await page.getByRole('button', { name: 'MID capture archive' }).click();
    await expect(page.locator('#sdIngestList')).toContainText('No MID capture archive yet');
    await expect(page.locator('#sdIngestFreshness')).not.toContainText('No archive destination');
    expectReadOnly(state); expectScopes(state);
});

test('651 archive destination failure is reported as a failure', async ({ page }) => {
    const state = await setup(page, 'archiveFailure');
    await expect(status(page, 'archive')).toHaveText('Jobs unavailable');
    await expect(row(page, 'archive').locator('.dot-error')).toHaveCount(1);
    await expect(row(page, 'archive')).toContainText('The archive destination could not be verified.');
    await expect(row(page, 'archive')).not.toContainText('Archive not yet created');
    await expect(page.locator('#sdActivityFreshness')).toContainText('Some reads failed');
    expectReadOnly(state); expectScopes(state);
});

test('651 Graph outage reads no job list and says so', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    const state = await setup(page, 'outage');
    await settled(page);
    for (const key of ['documents', 'archive']) {
        await expect(status(page, key)).toHaveText('Graph unavailable');
        await expect(row(page, key)).toContainText('Connection refused');
    }
    await expect(row(page, 'archive')).toContainText('2 captures pending indexing');
    await expect(row(page, 'documents')).not.toContainText('roadmap.pdf');
    await expect(row(page, 'archive')).not.toContainText('activeContext.md');
    expect(state.reads('long_ingest_list')).toHaveLength(0);
    await capture(page, 'graph-outage-768');
    expectReadOnly(state);
});

test('651 terminal job and idle sources stay distinct', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await setup(page, 'finished');
    await settled(page);
    const consolidation = row(page, 'consolidation');
    await expect(consolidation.locator('.sd-activity-head .status-dot-label')).toHaveText('Last job completed');
    await expect(consolidation.locator('.mono-data[title]').first()).toHaveAttribute('title', /^2026-10-06T07:12:40Z/);
    await expect(status(page, 'compaction')).toHaveText('Files compacted');
    await expect(row(page, 'compaction').locator('.mono-data[title]')).toHaveAttribute('title', /^2026-10-06T07:12:39Z/);
    // Nothing active: the latest documentary outcome is the terminal state.
    await expect(status(page, 'documents')).toHaveText('Last job completed');
    await expect(row(page, 'documents')).toContainText('No document ingestion is running or queued.');
    await expect(row(page, 'documents')).toContainText('charter.md');
    await expect(row(page, 'documents').getByRole('button', { name: 'Open document ingestion job charter.md' })).toBeVisible();
    await expect(status(page, 'archive')).toHaveText('No capture pending');
    await expect(row(page, 'archive')).toContainText('does not prove that every current MID file is indexed');
    // Zero backlog: the archive queue is not read (CLI parity).
    expect(state.reads('long_ingest_list').filter(call => call.arguments.archive)).toHaveLength(0);
    expect(state.reads('long_ingest_list').map(call => call.arguments)).toEqual([
        { space_id: 'demo', limit: 10, status: 'running' }, { space_id: 'demo', limit: 10, status: 'queued' }, { space_id: 'demo', limit: 1 }]);
    await capture(page, 'terminal-1440');
    expectReadOnly(state);
});

test('651 pending captures without an observable job are not shown as running', async ({ page }) => {
    const state = await setup(page, 'pendingNoJob');
    await settled(page);
    const archive = row(page, 'archive');
    await expect(archive.locator('.sd-activity-head .status-dot-label')).toHaveText('Capture pending');
    await expect(archive).toContainText('No capture indexing job is running or queued.');
    await expect(archive.locator('progress')).toHaveCount(0);
    expect(state.reads('long_ingest_list').filter(call => call.arguments.archive)).toHaveLength(3);
    expectReadOnly(state); expectScopes(state);
});

test('651 opt-in refresh follows jobs to terminal states without polling LONG status', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('hivemind.portal.autoRefresh', JSON.stringify({ enabled: true, intervalSeconds: 15 })));
    await page.clock.install();
    const state = await setup(page, 'active');
    await settled(page);
    const entry = state.calls.length;
    await page.clock.runFor(15001);
    await expect.poll(() => state.calls.length).toBe(entry + 5);
    expect(state.calls.slice(entry).map(call => call.tool).sort()).toEqual(['long_ingest_list', 'long_ingest_list', 'long_ingest_list', 'long_ingest_list', 'space_info']);
    // Everything finishes: the next tick renders terminal states, then following stops.
    state.lane = lane(null, [], [FINISHED]);
    state.documents = [DOC_DONE];
    state.archive = [{ ...ARCHIVE_RUNNING, status: 'succeeded', current_step: 'done', progress_percent: 100 }];
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    await page.clock.runFor(15001);
    await expect(status(page, 'consolidation')).toHaveText('Last job completed');
    await expect(status(page, 'documents')).toHaveText('Last job completed');
    // The backlog stays the last explicit read; no job is shown as running.
    await expect(status(page, 'archive')).toHaveText('Capture pending');
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(await page.evaluate(() => PortalRefresh.state().eligible)).toBe(false);
    const terminal = state.calls.length;
    await page.clock.runFor(120001);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.calls).toHaveLength(terminal);
    expect(state.reads('graph_status')).toHaveLength(1);
    // An explicit refresh re-reads LONG status.
    await page.getByRole('button', { name: 'Refresh active work' }).click();
    await expect.poll(() => state.reads('graph_status').length).toBe(2);
    expectReadOnly(state); expectScopes(state);
});

test('651 hostile job and server values stay literal', async ({ page }) => {
    const state = await setup(page, 'active', { mutate: value => {
        value.documents = [{ ...DOC_RUNNING, filename: `doc${HOSTILE}`, current_step: HOSTILE }];
        value.archive = [{ ...ARCHIVE_RUNNING, filename: `arc${HOSTILE}` }];
        value.lane = lane({ ...RUNNING, scope_label: HOSTILE, progress: { ...RUNNING.progress, phase: HOSTILE } }, [], []);
        value.graph = graph({ pending: 1, error: HOSTILE });
    } });
    await settled(page);
    await expect(row(page, 'documents')).toContainText(`doc${HOSTILE}`);
    await expect(row(page, 'archive')).toContainText(`arc${HOSTILE}`);
    await expect(row(page, 'consolidation')).toContainText(`Phase: ${HOSTILE}`);
    // A projection error outranks an observable job (#651 R1 F2).
    await expect(status(page, 'archive')).toHaveText('Needs attention');
    await expect(row(page, 'archive')).toContainText('1 running · 0 queued');
    await expect(row(page, 'archive')).toContainText(`Indexing problem: ${HOSTILE}`);
    await expect(page.locator('#sdActivityList img, #sdActivityList script')).toHaveCount(0);
    expect(await page.evaluate(() => window.__xss)).toBe(0);
    expectReadOnly(state);
});

test('651 R1 F1 a job that fails while followed is reported as failed, not idle', async ({ page }) => {
    const state = await setup(page, 'active');
    await settled(page);
    await expect(status(page, 'documents')).toHaveText('Running');
    state.documents = [{ ...DOC_RUNNING, status: 'failed', current_step: 'failed', error: 'LLM extraction failed', updated_at: '2026-10-06T08:04:00Z' }];
    await page.getByRole('button', { name: 'Refresh active work' }).click();
    await expect(status(page, 'documents')).toHaveText('Last job failed');
    const documents = row(page, 'documents');
    await expect(documents.locator('.sd-activity-head .dot-error')).toHaveCount(1);
    await expect(documents).toContainText('roadmap.pdf');
    await expect(documents).toContainText('LLM extraction failed');
    expect(state.reads('long_ingest_list').at(-1).arguments).toEqual({ space_id: 'demo', limit: 1 });
    await documents.getByRole('button', { name: 'Open document ingestion job roadmap.pdf' }).click();
    await expect(page.locator('#sdIngestDetail')).toContainText('LLM extraction failed');
    expectReadOnly(state); expectScopes(state);
});

test('651 R1 F1 a failed capture indexing job outranks the pending wait', async ({ page }) => {
    const state = await setup(page, 'archiveFailedJob');
    await settled(page);
    const archive = row(page, 'archive');
    await expect(status(page, 'archive')).toHaveText('Last job failed');
    await expect(archive).toContainText('1 capture pending indexing');
    await expect(archive).toContainText('Embedding provider refused the batch');
    await expect(archive).toContainText('activeContext.md');
    expect(state.reads('long_ingest_list').filter(call => call.arguments.archive).map(call => call.arguments)).toEqual([
        { space_id: 'demo', limit: 10, archive: true, status: 'running' }, { space_id: 'demo', limit: 10, archive: true, status: 'queued' }, { space_id: 'demo', limit: 1, archive: true }]);
    expectReadOnly(state); expectScopes(state);
});

test('651 R1 F2 an unreadable capture backlog is a failure, never a normal wait', async ({ page }) => {
    const state = await setup(page, 'projectionUnavailable');
    await settled(page);
    const archive = row(page, 'archive');
    await expect(status(page, 'archive')).toHaveText('Needs attention');
    await expect(archive.locator('.sd-activity-head .dot-error')).toHaveCount(1);
    await expect(archive).toContainText('Capture backlog unknown');
    await expect(archive).toContainText('Indexing problem: projection_unavailable');
    await expect(archive).toContainText('could not be read');
    await expect(archive).not.toContainText('normal wait');
    await expect(archive).not.toContainText('0 captures pending');
    expectReadOnly(state); expectScopes(state);
});

test('651 R1 F3 an archive job that left an empty history keeps its last read', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('hivemind.portal.autoRefresh', JSON.stringify({ enabled: true, intervalSeconds: 15 })));
    await page.clock.install();
    const state = await setup(page, 'active');
    await settled(page);
    state.archive = [];
    await row(page, 'archive').getByRole('button', { name: 'Open capture indexing job activeContext.md' }).click();
    const detail = page.locator('#sdIngestDetail');
    await expect(detail).toBeVisible();
    await expect(detail).toContainText('activeContext.md');
    await expect(detail).toContainText('not in the listed archive page');
    await expect(page.locator('#sdIngestList')).toContainText('No capture indexing jobs in the available history');
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    // Not followed: the controller is not eligible, and no cycle starts later.
    expect(await page.evaluate(() => PortalRefresh.state().eligible)).toBe(false);
    const reads = state.calls.length;
    await page.clock.runFor(60001);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.calls).toHaveLength(reads);
    expect(state.reads('long_ingest_status')).toHaveLength(0);
    expectReadOnly(state); expectScopes(state);
});

for (const [scope, scenario, archive, job, label] of [
    ['documents', 'finished', false, DOC_QUEUED, 'Queued'],
    ['archive', 'pendingNoJob', true, ARCHIVE_RUNNING, 'Running'],
]) {
    test(`651 R2 a ${scope} job that turns active between list reads is shown and followed`, async ({ page }) => {
        const state = await setup(page, scenario, { mutate: value => {
            // The job appears after the running/queued snapshots, before the newest-job read.
            value.onList = args => {
                if (args.limit === 1 && !args.status && (args.archive === true) === archive) value[archive ? 'archive' : 'documents'] = [job];
            };
        } });
        await settled(page);
        const key = archive ? 'archive' : 'documents';
        await expect(status(page, key)).toHaveText(label);
        await expect(row(page, key)).toContainText(job.filename);
        await expect(row(page, key)).toContainText(label === 'Running' ? '1 running · 0 queued' : '0 running · 1 queued');
        await expect(row(page, key)).not.toContainText('Latest job:');
        // Still active work: the shared controller keeps following it.
        expect(await page.evaluate(() => PortalRefresh.state().eligible)).toBe(true);
        expectReadOnly(state); expectScopes(state);
    });
}

test('651 Active work is reachable from the Space tabs by keyboard', async ({ page }) => {
    const state = await setup(page, 'finished', { hash: '#/spaces/demo/memory/short' });
    const memory = page.getByRole('tab', { name: 'Memory', exact: true });
    await expect(memory).toHaveAttribute('aria-selected', 'true');
    await memory.focus();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name: 'Active work', exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#\/spaces\/demo\/activity$/);
    await expect(page.getByRole('tab', { name: 'Active work', exact: true })).toBeFocused();
    await settled(page);
    expectReadOnly(state);
});
