import { expect } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test, sourceProvenance, tabTo, realisticData, routePortal, configureViewport, enterLong, closeDocumentInspector, evidence, VIEWPORTS, ORIGIN, HOSTILE } from './fixtures/portal-634.mjs';

test('634 fixture contract: bounded density, unique identity, portable provenance and dispatch scope', async ({ page }) => {
    const { graph, documents, jobs, lanes } = realisticData();
    expect(graph.graph_view.nodes).toHaveLength(160);
    expect(graph.graph_view.edges).toHaveLength(320);
    const ids = new Set(graph.graph_view.nodes.map(node => node.id));
    expect(ids.size).toBe(160);
    expect(graph.graph_view.nodes.filter(node => node.node_type === 'document')).toHaveLength(50);
    for (const edge of graph.graph_view.edges) {
        expect(ids.has(edge.from) && ids.has(edge.to)).toBe(true);
    }
    for (const id of ids) {
        expect(graph.graph_view.edges.some(edge => edge.from === id)).toBe(true);
        expect(graph.graph_view.edges.some(edge => edge.to === id)).toBe(true);
    }
    expect(documents).toHaveLength(50);
    expect(new Set(documents.map(doc => doc.filename)).size).toBe(1);
    for (const field of ['document_id', 'source_path', 'sha256']) expect(new Set(documents.map(doc => doc[field])).size).toBe(50);
    expect(documents.every(doc => !('origin' in doc) && !('captured_at' in doc))).toBe(true);
    expect(jobs).toHaveLength(50);
    expect([...new Set(jobs.map(job => job.status))]).toEqual(['running', 'queued', 'succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped']);
    expect(jobs.find(job => job.status === 'failed').ontology_diagnostics.message).toContain(HOSTILE);
    expect(jobs[48]).not.toHaveProperty('progress_percent');
    expect(lanes[0].queued_jobs[0].requested_by).not.toBe(lanes[0].queued_jobs[0].agent);
    const gitless = fs.mkdtempSync(path.join(os.tmpdir(), 'portal-634-provenance-'));
    try {
        expect(sourceProvenance({ root: gitless })).toMatchObject({ sourceHead: null, dirty: null, provenance: { mode: 'portable', status: 'unavailable' } });
        expect(() => sourceProvenance({ root: gitless, strict: true })).toThrow('qualification provenance unavailable');
    } finally { fs.rmdirSync(gitless); }
    const state = await routePortal(page);
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('#dashRunningJobs')).not.toBeEmpty();
    let release;
    state.defer = { tool: 'bank_consolidation_queues', promise: new Promise(resolve => { release = resolve; }) };
    const before = state.reads('bank_consolidation_queues').length;
    await page.evaluate(() => {
        window.__queueScopeProbe = fetch('/api/tool', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tool: 'bank_consolidation_queues', arguments: {} }) }).then(r => r.json());
    });
    await expect.poll(() => state.reads('bank_consolidation_queues').length).toBe(before + 1);
    await page.locator('#sidebar a[href="#/spaces"]').click();
    await expect(page).toHaveURL(/#\/spaces$/);
    release();
    expect(await page.evaluate(() => window.__queueScopeProbe)).toMatchObject({ status: 'error', message: 'Synthetic invalid explicit queue scope' });
    expect(state.unexpected).toEqual([{ invalidQueueScope: { tool: 'bank_consolidation_queues', arguments: {}, routeAtDispatch: '#/dashboard' } }]);
});

for (const viewport of VIEWPORTS) {
    test(`634 contracts and integrated bundle capture — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        await page.clock.install();
        const state = await routePortal(page);
        await enterLong(page, 'documents');
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–50 / 50');
        await expect(page.locator('[data-action="sd-document-inspect"]')).toHaveCount(50);
        expect(state.reads('long_document_get')).toHaveLength(0);
        await evidence(page, state, info, 'documents-full-page-before-selection', viewport);
        await page.locator('[data-action="sd-document-inspect"]').first().click();
        await expect(page.locator('#sdDocumentDetail')).toContainText('doc-1');
        expect(state.reads('long_document_get').at(-1).arguments).toEqual({ space_id: 'demo', document_id: 'doc-1', include_content: false });
        // Capture before any content activation can scroll the distant inspector.
        await evidence(page, state, info, 'documents-first-selection', viewport);
        await page.locator('[data-action="sd-document-content"]').click();
        await expect(page.locator('#sdDocumentDetail pre')).toContainText(HOSTILE);
        expect(state.reads('long_document_get').at(-1).arguments.include_content).toBe(true);
        await evidence(page, state, info, 'documents-explicit-content', viewport);
        await closeDocumentInspector(page);
        await page.getByRole('tab', { name: 'Documents', exact: true }).click();
        await page.locator('[data-action="sd-document-inspect"]').last().click();
        await expect(page.locator('#sdDocumentDetail')).toContainText('doc-50');
        await evidence(page, state, info, 'documents-last-selection', viewport);
        await page.locator('[data-action="sd-document-content"]').click();
        await expect(page.locator('#sdDocumentDetail')).toContainText('binary content');
        expect(state.reads('long_document_get').at(-1).arguments).toMatchObject({ document_id: 'doc-50', include_content: true });
        await closeDocumentInspector(page);
        await page.locator('#sdDocumentQuery').fill('team-02/');
        await page.locator('[data-action="sd-documents-apply"]').click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–1 / 1');
        expect(state.reads('long_document_list').at(-1).arguments).toMatchObject({ limit: 50, offset: 0, query: 'team-02/' });

        await page.getByRole('tab', { name: 'Ingestion jobs', exact: true }).click();
        await expect(page.locator('#sdIngestPage')).toHaveText('1–50 / 50');
        await expect(page.locator('[data-action="sd-ingest-inspect"]')).toHaveCount(50);
        const off = state.calls.length;
        await page.clock.runFor(60001);
        expect(state.calls).toHaveLength(off);
        await evidence(page, state, info, 'jobs-full-page-before-selection', viewport);
        await page.locator('[data-action="sd-ingest-inspect"]').first().click();
        await expect(page.locator('#sdIngestDetail')).toContainText('ingest-1');
        await expect(page.locator('#sdIngestDetail')).toContainText('42%');
        expect(state.reads('long_ingest_status')).toHaveLength(0);
        await evidence(page, state, info, 'jobs-first-selection', viewport);
        // Other fixture jobs finish while this selected job is being followed.
        // Terminal detail must stop only once no visible job still needs follow.
        state.jobs = state.jobs.map((job, i) => i && ['running', 'queued'].includes(job.status)
            ? { ...job, status: 'succeeded', current_step: 'done', progress_percent: 100, finished_at: '2026-09-30T12:02:00Z' } : job);
        await page.locator('#portalAutoEnabled').check();
        const tick = state.calls.length;
        await page.clock.runFor(15001);
        await expect.poll(() => state.calls.length).toBe(tick + 1);
        expect(state.calls.at(-1).tool).toBe('long_ingest_list');
        state.hiddenJob = 'ingest-1';
        const missing = state.calls.length;
        await page.clock.runFor(15001);
        await expect.poll(() => state.calls.length).toBe(missing + 2);
        expect(state.calls.slice(missing).map(call => call.tool)).toEqual(['long_ingest_list', 'long_ingest_status']);
        await page.locator('#sdIngestDetail [data-action="sd-ingest-cancel"]').click();
        await expect(page.locator('#adminModal')).toContainText('ingest-1');
        expect(state.reads('long_ingest_cancel')).toHaveLength(0);
        await page.keyboard.press('Escape');
        expect(state.reads('long_ingest_cancel')).toHaveLength(0);
        await page.locator('#sdIngestDetail [data-action="sd-ingest-cancel"]').click();
        await page.locator('#modalConfirmBtn').click();
        const cancellation = page.locator('#sdIngestCancelResult');
        await expect(cancellation).toContainText('ingest-1');
        await expect(cancellation).toContainText('Cancellation requested');
        await expect(cancellation).toContainText('Updated');
        // §17.4 keeps the dated outcome outside an inspector that may close.
        await tabTo(page, cancellation.getByRole('button', { name: 'Check cancellation result', exact: true }));
        await evidence(page, state, info, 'jobs-cancellation-outcome', viewport);
        expect(state.reads('long_ingest_cancel').map(call => call.arguments)).toEqual([{ space_id: 'demo', job_id: 'ingest-1' }]);
        state.jobs[0] = { ...state.jobs[0], status: 'cancelled', current_step: 'cancelled', finished_at: '2026-09-30T12:02:00Z' };
        await page.clock.runFor(15001);
        await expect(page.locator('#sdIngestDetail')).toContainText(/cancelled/i);
        await expect(cancellation).toContainText('Job status: Cancelled');
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        const terminal = state.calls.length;
        await page.clock.runFor(60001);
        expect(state.calls).toHaveLength(terminal);
        state.jobs = [];
        const emptyHistory = state.calls.length;
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(page.locator('[data-action="sd-ingest-inspect"]')).toHaveCount(0);
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion history available');
        await expect(cancellation).toContainText('ingest-1');
        await expect(cancellation).toContainText('Job status: Cancelled');
        await expect(cancellation).toContainText('Updated');
        expect(state.calls.slice(emptyHistory).map(call => call.tool)).toEqual(['long_ingest_list']);
        expect(state.reads('long_ingest_cancel')).toHaveLength(1);
        await cancellation.locator('summary').click();
        await expect(cancellation.locator('details')).toContainText('Cancellation requested at the next phase boundary.');
        await cancellation.locator('summary').click();
        await evidence(page, state, info, 'jobs-empty-history-keeps-cancellation-outcome', viewport);
        await page.getByRole('tab', { name: 'Graph', exact: true }).click();
        await expect(page.locator('.sd-graph-node')).toHaveCount(160);
        await expect(page.locator('.sd-graph-edge')).toHaveCount(320);
        const before = state.calls.length;
        await page.locator('#sdGraphSearch').fill('Entity 51');
        const node = page.getByRole('button', { name: /^Inspect Entity 51 —/ }).first();
        await tabTo(page, node);
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphDetails')).toContainText('Synthetic evidence for entity 51');
        await expect(page.locator('#sdGraphDetails img')).toHaveCount(0);
        await evidence(page, state, info, 'graph-160-320-selected', viewport);
        await page.locator('#sdGraphFit').click();
        expect(state.calls).toHaveLength(before);
        expect(state.reads('graph_status').filter(call => call.arguments.include_graph === true)).toHaveLength(1);
        expect(state.reads('system_health')).toHaveLength(0);
        expect(state.reads('long_query')).toHaveLength(0);
    });
}

test('634 ownership: late graph read cannot paint after navigating to Documents', async ({ page }) => {
    const state = await routePortal(page);
    let release;
    state.defer = { tool: 'graph_status', promise: new Promise(resolve => { release = resolve; }) };
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/graph`);
    await expect.poll(() => state.reads('graph_status').length).toBe(1);
    await page.getByRole('tab', { name: 'Documents', exact: true }).click();
    await expect(page.locator('#sdDocumentsPage')).toHaveText('1–50 / 50');
    const lateResponse = page.waitForResponse(response => response.url().endsWith('/api/tool') && response.request().postDataJSON().tool === 'graph_status');
    release();
    await lateResponse;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.locator('#sdGraphCanvas')).toHaveCount(0);
    await expect(page).toHaveURL(/\/long\/documents$/);
    expect(state.unexpected).toEqual([]);
});

test('634 errors retain real catalog data and warnings without false empty state', async ({ page }, info) => {
    await configureViewport(page, VIEWPORTS[0]);
    const state = await routePortal(page, { documentEnvelope: { partial: true, warnings: [{ message: `Synthetic partial catalog. ${HOSTILE}` }] } });
    await enterLong(page, 'documents');
    await expect(page.locator('#sdDocumentsList')).toContainText('Synthetic partial catalog');
    state.fail = { tool: 'long_document_list' };
    await page.locator('[data-action="sd-long-refresh"]').click();
    await expect(page.locator('#sdDocumentsFreshness')).toContainText('Synthetic service unavailable');
    await expect(page.locator('[data-action="sd-document-inspect"]')).toHaveCount(50);
    await evidence(page, state, info, 'documents-partial-read-error', VIEWPORTS[0]);
});

for (const viewport of VIEWPORTS) {
    test(`634 overview response variants preserve automation and read bounds — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await enterLong(page);
        const base = structuredClone(state.graph);
        for (const [name, overrides, expected] of [
            ['healthy', {}, 'Captures pending indexing'],
            ['unbound', { connected: false, embedded: true, bound: false }, 'Waiting for the first ingestion'],
            ['unreachable', { reachable: false, error: `Synthetic Graph outage ${HOSTILE}` }, 'Embedded long runtime unreachable'],
            ['unknown-binding', { reachable: false, binding: null }, 'binding unknown'],
            ['explicit-unreachable', { reachable: false, binding: 'explicit', config: { ontology: 'general', url: 'https://synthetic.invalid/mcp', memory_id: 'synthetic-memory' } }, 'Explicit long runtime unreachable'],
            ['partial-zero-backlog', { graph_stats: { entity_count: 217 }, mid_archive_projection: { pending: 0, oldest_at: null } }, 'zero backlog'],
        ]) {
            state.graph = { ...base, ...overrides };
            const before = state.reads('graph_status').length;
            await page.locator('[data-action="sd-long-refresh"]').click();
            await expect.poll(() => state.reads('graph_status').length).toBe(before + 1);
            await expect(page.locator('#sdLongSnapshot')).toContainText(expected);
            if (name === 'unbound') {
                await expect(page.locator('.sd-long-health')).toContainText('Unbound');
                await expect(page.locator('.sd-long-metrics .metric-value')).toHaveCount(0);
            }
            await expect(page.locator('#sdLongSnapshot')).toContainText('MID → LONG automation');
            await evidence(page, state, info, `overview-${name}`, viewport);
        }
        expect(state.reads('graph_status').every(req => req.arguments.include_graph === false)).toBe(true);
        expect(state.reads('long_document_list')).toHaveLength(0);
        expect(state.reads('system_health')).toHaveLength(0);
    });
}
