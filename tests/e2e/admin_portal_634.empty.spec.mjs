// Empty-state acceptance for the integrated Portal contract; ordinary CI, no xfail/skip.
import { expect as baseExpect } from '@playwright/test';
import { test, routePortal, configureViewport, enterLong, evidence, VIEWPORTS, ORIGIN, HOSTILE } from './fixtures/portal-634.mjs';

const expect = baseExpect.configure({ timeout: 5000 });
const panels = {
    documents: { tab: 'Documents', tool: 'long_document_list', list: '#sdDocumentsList', detail: '#sdDocumentDetail', inspect: 'sd-document-inspect', page: 'sd-documents-page', empty: /no documents/i },
    jobs: { tab: 'Ingestion jobs', tool: 'long_ingest_list', list: '#sdIngestList', detail: '#sdIngestDetail', inspect: 'sd-ingest-inspect', page: 'sd-ingest-page', empty: /no ingestion (?:jobs|history)/i },
    graph: { tab: 'Graph', tool: 'graph_status', list: '#sdLongSnapshot', detail: '#sdGraphDetails', empty: /no graph data|no nodes|empty graph/i },
};
const idle = { space_id: 'demo', lane_state: 'idle', queued_count: 0, running_job: null, queued_jobs: [], latest_jobs: [], guarantee: 'best_effort_in_process' };

async function noInspector(page, selector) {
    if (!await page.locator(selector).count()) return;
    const box = await page.locator(selector).boundingBox();
    expect.soft(box?.height || 0, 'No large phantom inspector without a selection').toBeLessThan(100);
}
async function noPagination(page, panel) {
    if (!panel.page) return;
    const buttons = page.locator(`[data-action="${panel.page}"]`);
    for (const button of await buttons.all()) {
        if (await button.isVisible()) await expect.soft(button, 'An empty page has no active pager').toBeDisabled();
    }
}
async function noHomePager(page) {
    await expect.soft(page.locator('#dashPrevPage'), 'No Previous spaces control on Dashboard').toBeHidden();
    await expect.soft(page.locator('#dashNextPage'), 'No Next spaces control on Dashboard').toBeHidden();
}
async function framed(page) {
    expect.soft(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Empty state fits viewport').toBe(true);
}
async function frameEmptyState(page, region, viewport) {
    const parts = region.locator('.state-empty').locator('h3, .state-hint, .btn');
    expect(await parts.count()).toBeGreaterThanOrEqual(2);
    for (const part of await parts.all()) await expect(part).toBeVisible();
    const rectangles = () => parts.evaluateAll(elements => elements.map(el => el.getBoundingClientRect().toJSON()));
    const topbarBottom = await page.locator('#portalTopbar').evaluate(el => el.getBoundingClientRect().bottom);
    const wheels = [];
    await page.mouse.move(viewport.width - 30, viewport.height - 30);
    for (let step = 0; step < 5; step++) {
        const boxes = await rectangles();
        const delta = boxes.some(r => r.bottom > viewport.height) ? 200 : boxes.some(r => r.top < topbarBottom) ? -200 : 0;
        if (!delta) break;
        await page.mouse.wheel(0, delta); wheels.push(delta);
        await expect.poll(async () => (await rectangles()).map(r => r.y)).not.toEqual(boxes.map(r => r.y));
    }
    await expect.poll(async () => (await rectangles()).every(r => r.top >= topbarBottom && r.bottom <= viewport.height)).toBe(true);
    return { wheels, topbarBottom, rectangles: await rectangles(), framing: 'Bounded native wheel; title, hint and actions fully inside the unobscured viewport' };
}

for (const viewport of [VIEWPORTS[0], VIEWPORTS[2]]) {
    for (const manager of [true, false]) {
        test(`#633 zero visible spaces, relevant ${manager ? 'manager' : 'writer'} actions — ${viewport.name}`, async ({ page }, info) => {
            await configureViewport(page, viewport);
            const state = await routePortal(page, { spaces: [], lanes: [], permissions: manager ? ['read', 'write', 'manage', 'admin'] : ['read', 'write'] });
            await page.goto(`${ORIGIN}/admin.html#/dashboard`);
            await expect(page.locator('#dashSpacesEmpty')).toContainText(/no spaces/i);
            state.observations = { emptyStateFraming: await frameEmptyState(page, page.locator('#dashSpacesEmpty'), viewport) };
            await evidence(page, state, info, `home-zero-spaces-${manager ? 'manager' : 'writer'}`, viewport);
            await noHomePager(page);
            await expect.soft(page.locator('#dashStartConsolidationBtn')).toBeDisabled();
            await expect.soft(page.locator('#dashAddNotes')).toBeDisabled();
            await expect.soft(page.locator('#dashRunningJobs [data-job-id]')).toHaveCount(0);
            await expect.soft(page.locator('#dashRecentJobs [data-job-id]')).toHaveCount(0);
            await expect.soft(page.locator('#dashLanesPanel')).not.toContainText(/loading|not loaded/i);
            // Home budgets stop here; following Create space has its own reads.
            expect(state.reads('bank_consolidation_queues')).toHaveLength(0);
            expect(state.reads('live_read')).toHaveLength(0);
            expect(state.reads('system_health')).toHaveLength(0);
            const create = page.locator('#dashSpacesEmpty').getByRole('link', { name: /create a space/i });
            if (manager) {
                await expect.soft(create).toBeVisible();
                await create.click();
                await expect(page).toHaveURL(/#\/spaces$/);
                await expect.soft(page.locator('#spacesTableWrap').getByRole('button', { name: 'Create space', exact: true })).toBeVisible();
            } else {
                await expect.soft(create).toHaveCount(0);
                await expect.soft(page.locator('#dashSpacesEmpty')).toContainText(/manager/i);
            }
            await framed(page);
        });
    }

    test(`#633 idle space and explicit empty notes are complete states — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page, { spaces: [{ space_id: 'demo', live_notes_count: 0, bank_files_count: 0 }], lanes: [idle], notes: [] });
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        await expect(page.locator('#dashActivityEmpty')).toContainText(/no recent spaces to follow/i);
        expect(state.reads('live_read')).toHaveLength(0);
        await expect(page.locator('#dashSpacesEmpty')).toContainText('No consolidations yet');
        expect(state.reads('bank_consolidation_queues')).toHaveLength(0);
        await page.locator('.dash-notes > summary').click();
        await page.locator('#dashNoteSpace').selectOption('demo');
        await page.locator('#dashAddNotes').click();
        await expect(page.locator('#dashNotesPanel')).toContainText(/no notes/i);
        await evidence(page, state, info, 'home-idle-and-empty-notes', viewport);
        await noHomePager(page);
        await expect.soft(page.locator('#dashActivityEmpty')).not.toContainText(/loading|not loaded|unavailable/i);
        await expect.soft(page.locator('#dashRecentEmpty')).toContainText(/explore consolidation.*history/i);
        await expect.soft(page.locator('#dashRunningJobs [data-job-id]')).toHaveCount(0);
        expect(state.reads('live_read')).toHaveLength(1);
        expect(state.reads('system_health')).toHaveLength(0);
        await framed(page);
    });

    for (const name of Object.keys(panels)) {
        const panel = panels[name];
        test(`#634 zero ${name}, no pager or phantom inspector — ${viewport.name}`, async ({ page }, info) => {
            await configureViewport(page, viewport);
            const state = await routePortal(page, { documents: [], jobs: [] });
            state.graph.graph_view = { status: 'ok', nodes: [], edges: [], node_count: 0, edge_count: 0, total_node_count: 0, total_edge_count: 0, truncated: false };
            state.graph.graph_stats = { document_count: 0, entity_count: 0, relation_count: 0, entity_types: {} };
            await enterLong(page, name);
            await expect(page.locator(panel.list)).toContainText(panel.empty);
            state.observations = { emptyStateFraming: await frameEmptyState(page, page.locator(panel.list), viewport) };
            await evidence(page, state, info, `${name}-zero`, viewport);
            await noPagination(page, panel);
            await noInspector(page, panel.detail);
            if (panel.inspect) await expect.soft(page.locator(`[data-action="${panel.inspect}"]`)).toHaveCount(0);
            else await expect.soft(page.locator('.sd-graph-node')).toHaveCount(0);
            await expect.soft(page.locator(panel.list)).not.toContainText(/loading|unavailable/i);
            expect(state.reads('long_document_get')).toHaveLength(0);
            expect(state.reads('long_ingest_status')).toHaveLength(0);
            await framed(page);
        });

        test(`#634 ${name} loading and first-read failure cannot masquerade as empty — ${viewport.name}`, async ({ page }, info) => {
            await configureViewport(page, viewport);
            const state = await routePortal(page);
            await enterLong(page);
            let release;
            state.defer = { tool: panel.tool, promise: new Promise(resolve => { release = resolve; }) };
            state.fail = { tool: panel.tool, message: `Synthetic unavailable ${name}. ${HOSTILE}` };
            const before = state.reads(panel.tool).length;
            await page.getByRole('tab', { name: panel.tab, exact: true }).click();
            await expect.poll(() => state.reads(panel.tool).length).toBe(before + 1);
            await expect(page.locator('#sdLongBody')).toContainText(/loading/i);
            await evidence(page, state, info, `${name}-loading`, viewport);
            await expect.soft(page.locator(panel.list)).not.toContainText(panel.empty);
            const response = page.waitForResponse(r => r.url().endsWith('/api/tool') && r.request().postDataJSON().tool === panel.tool);
            release();
            await response;
            await expect(page.locator('#sdLongBody')).toContainText(`Synthetic unavailable ${name}`);
            await evidence(page, state, info, `${name}-first-read-failure`, viewport);
            await expect.soft(page.locator(panel.list)).not.toContainText(panel.empty);
            await expect.soft(page.locator('#sdLongBody')).not.toContainText(/loading/i);
            await noInspector(page, panel.detail);
            await noPagination(page, panel);
            expect(state.reads(panel.tool)).toHaveLength(before + 1);
            await framed(page);
        });
    }

    for (const name of ['documents', 'jobs']) {
        const panel = panels[name];
        test(`#634 ${name} filter without results remains distinct from empty inventory — ${viewport.name}`, async ({ page }, info) => {
            await configureViewport(page, viewport);
            const state = await routePortal(page);
            await enterLong(page, name);
            await expect(page.locator(`[data-action="${panel.inspect}"]`)).toHaveCount(50);
            if (name === 'documents') await page.locator('#sdDocumentQuery').fill('no-such-synthetic-source');
            else await page.locator('#sdIngestBatch').fill('no-such-synthetic-batch');
            await page.locator(`[data-action="${name === 'documents' ? 'sd-documents-apply' : 'sd-ingest-apply'}"]`).click();
            await expect(page.locator(panel.list)).toContainText(/no .*match/i);
            expect(state.reads(panel.tool).at(-1).arguments).toMatchObject(name === 'documents'
                ? { query: 'no-such-synthetic-source', limit: 50, offset: 0 }
                : { batch_id: 'no-such-synthetic-batch', limit: 50, offset: 0 });
            await evidence(page, state, info, `${name}-filter-no-results`, viewport);
            await noPagination(page, panel);
            await noInspector(page, panel.detail);
            await expect.soft(page.locator(`[data-action="${panel.inspect}"]`)).toHaveCount(0);
            expect(state[name]).toHaveLength(50);
            await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
            await expect(page.locator(`[data-action="${panel.inspect}"]`)).toHaveCount(50);
            expect(state.reads(panel.tool).at(-1).arguments).toMatchObject(name === 'documents' ? { query: '', status: '', offset: 0 } : { batch_id: '', status: '', offset: 0 });
            await framed(page);
        });
    }

    test(`#629 partial empty catalog shows incompleteness and warning — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page, { documents: [], documentEnvelope: { partial: true, total_count: null, warnings: [{ message: `Synthetic incomplete catalog ${HOSTILE}` }] } });
        await enterLong(page, 'documents');
        await expect(page.locator('#sdDocumentsList')).toContainText(/partial/i);
        await expect(page.locator('#sdDocumentsList')).toContainText('Synthetic incomplete catalog');
        await evidence(page, state, info, 'documents-partial-empty', viewport);
        await expect.soft(page.locator('#sdDocumentsPage')).toContainText(/unavailable|unknown|—/i);
        await noPagination(page, panels.documents);
        await noInspector(page, panels.documents.detail);
        await framed(page);
    });

    test(`#632 writer permission has no empty-history mutation actions — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page, { permissions: ['read', 'write'], jobs: [] });
        await enterLong(page, 'jobs');
        await expect(page.locator('#sdIngestList')).toContainText(panels.jobs.empty);
        await evidence(page, state, info, 'jobs-empty-writer-permission', viewport);
        await expect.soft(page.locator('[data-action="sd-ingest-cancel"]')).toHaveCount(0);
        await noPagination(page, panels.jobs);
        await noInspector(page, panels.jobs.detail);
        expect(state.reads('long_ingest_cancel')).toHaveLength(0);
        expect(state.calls.filter(call => /^(admin_|backup_|space_(create|delete|update)|bank_(repair|compact|gc))/.test(call.tool))).toHaveLength(0);
        await framed(page);
    });
}
