// Strict final recipe, included in ordinary Admin E2E CI by *.spec.mjs.
// Known base defects remain ordinary failing assertions, with no xfail/skip.
import { expect as baseExpect } from '@playwright/test';
import { test, tabTo, selectedLabelGeometry, graphLabelAssociations, routePortal, configureViewport, enterLong, closeDocumentInspector, assertCompactNoOp, evidence, expectReachableDetail, VIEWPORTS, ORIGIN, HOSTILE } from './fixtures/portal-634.mjs';

const expect = baseExpect.configure({ timeout: 5000 });
const action = (page, name) => page.locator(`[data-action="${name}"]`);
async function frame(page) {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
}
async function touch(locator, viewport) {
    if (viewport.width !== 390) return;
    const box = await locator.boundingBox();
    expect(box.height, 'Mobile action target height').toBeGreaterThanOrEqual(44);
    expect(box.width, 'Mobile action target width').toBeGreaterThanOrEqual(44);
}
async function focusedRing(locator) {
    await expect(locator).toBeFocused();
    const ring = await locator.evaluate(el => {
        const s = getComputedStyle(el);
        return s.outlineStyle !== 'none' && Number.parseFloat(s.outlineWidth) >= 2 || s.boxShadow !== 'none';
    });
    expect(ring, 'Keyboard focus has a visible outline or shadow').toBe(true);
}
async function frameNoOpArticle(page, article) {
    await tabTo(page, article.locator('.dash-job-details > summary'));
    const bounds = await page.evaluate(() => ({
        width: innerWidth, height: innerHeight,
        topbarBottom: document.querySelector('#portalTopbar').getBoundingClientRect().bottom,
    }));
    const rectangle = () => article.evaluate(el => el.getBoundingClientRect().toJSON());
    const wheels = [];
    await page.mouse.move(bounds.width - 30, bounds.height - 30);
    for (let step = 0; step < 5; step++) {
        const box = await rectangle();
        const delta = box.bottom > bounds.height ? Math.min(120, box.bottom - bounds.height + 8)
            : box.top < bounds.topbarBottom ? Math.max(-120, box.top - bounds.topbarBottom - 8) : 0;
        if (!delta) break;
        await page.mouse.wheel(0, delta); wheels.push(delta);
        await expect.poll(async () => (await rectangle()).y).not.toBe(box.y);
    }
    await expect.poll(async () => {
        const box = await rectangle();
        return box.top >= bounds.topbarBottom && box.bottom <= bounds.height;
    }, { message: 'No-op article, summary and collapsed disclosure fit the unobscured CSS viewport' }).toBe(true);
    return { ...bounds, wheels, rectangle: await rectangle(), framing: 'Ordinary Tab to collapsed summary, bounded native wheel; no activation' };
}

for (const viewport of VIEWPORTS) {
    test(`#630 named rail and drawer keyboard journey — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        await expect(page.locator('#dashRunningJobs')).not.toBeEmpty();
        await evidence(page, state, info, 'navigation-rail', viewport);
        if (viewport.width < 1024 || viewport.zoom) await page.getByRole('button', { name: /menu/i }).click();
        for (const [href, name] of [['#/dashboard', 'Dashboard'], ['#/spaces', 'Spaces'], ['#/consolidation', 'Consolidation'], ['#/audit', 'Audit'], ['#/access', 'Access'], ['#/operator/backups', 'Backups'], ['#/operator/maintenance', 'Maintenance'], ['/live', 'Live viewer']]) {
            await expect(page.locator(`#sidebar a[href="${href}"]:not(.sidebar-brand)`)).toHaveAccessibleName(name);
        }
        const spaces = page.getByRole('link', { name: 'Spaces', exact: true });
        await tabTo(page, spaces);
        await focusedRing(spaces);
        await page.keyboard.press('Enter');
        await expect(page).toHaveURL(/#\/spaces$/);
        await expect(spaces).toHaveAttribute('aria-current', 'page');
        if (viewport.width < 1024 || viewport.zoom) {
            await page.getByRole('button', { name: /menu/i }).click();
            await expect(spaces).toHaveAccessibleName('Spaces');
            await evidence(page, state, info, 'navigation-open-menu', viewport);
            await page.keyboard.press('Escape');
            await expect(page.getByRole('button', { name: /menu/i })).toBeFocused();
        }
        await frame(page);
    });

    test(`#627 dense graph search, relation, neighbor, Back — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await enterLong(page, 'graph');
        await expect(page.locator('.sd-graph-node')).toHaveCount(160);
        await expect(page.locator('.sd-graph-edge')).toHaveCount(320);
        const overviewAssociations = await graphLabelAssociations(page);
        expect.soft(overviewAssociations.detachedLabels, 'Visible overview labels must remain associated with their own visible node').toEqual([]);
        const requests = state.calls.length;
        await page.locator('#sdGraphSearch').fill('Entity 51');
        const selected = page.getByRole('button', { name: /^Inspect Entity 51 —/ }).first();
        await tabTo(page, selected);
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphDetails')).toContainText('Synthetic evidence for entity 51');
        const selectedGeometry = await selectedLabelGeometry(page);
        const selectedAssociations = await graphLabelAssociations(page);
        expect.soft(selectedAssociations.labels.filter(label => label.selected)).toHaveLength(1);
        expect.soft(selectedAssociations.detachedLabels, 'Visible selected-neighborhood labels must remain associated with their own visible node').toEqual([]);
        state.observations = { selectedLabelGeometry: selectedGeometry, overviewAssociations, selectedAssociations };
        await evidence(page, state, info, 'graph-selected-before-neighbor', viewport);
        expect.soft(selectedGeometry.neighbors).toHaveLength(4);
        expect.soft(selectedGeometry.overlaps, 'Selected label must not cover a visible neighboring node shape').toEqual([]);
        const labels = await page.locator('#sdGraphCanvas text').evaluateAll(elements => elements.flatMap(el => {
            let opacity = 1;
            for (let n = el; n instanceof Element; n = n.parentElement) opacity *= Number(getComputedStyle(n).opacity);
            const style = getComputedStyle(el), r = el.getBoundingClientRect(), matrix = el.getScreenCTM();
            if (style.display === 'none' || style.visibility === 'hidden' || opacity < 0.5 || !r.width || !matrix) return [];
            return [{ x: r.x, y: r.y, right: r.right, bottom: r.bottom, font: parseFloat(style.fontSize) * Math.hypot(matrix.a, matrix.b) }];
        }));
        expect.soft(labels.every(label => label.font >= 10), 'Visible canvas labels must remain readable at screen scale').toBe(true);
        const overlap = labels.some((a, i) => labels.slice(i + 1).some(b => Math.min(a.right, b.right) > Math.max(a.x, b.x) + 2 && Math.min(a.bottom, b.bottom) > Math.max(a.y, b.y) + 2));
        expect.soft(overlap, 'Visible labels must not obscure one another').toBe(false);
        const graph = page.locator('.sd-graph');
        await expect.soft(graph).toContainText(/160\s*\/\s*267/);
        await expect.soft(graph).toContainText(/320\s*\/\s*420/);
        await expect.soft(graph.locator('.sd-graph-legend > span').nth(1)).toHaveText('Document');
        await expect.soft(graph.locator('.sd-graph-legend > span').first()).toHaveText('Entity');
        await expect.soft(page.locator('#sdGraphDetails')).toContainText(/outgoing/i);
        await expect.soft(page.locator('#sdGraphDetails')).toContainText(/incoming/i);
        await expect.soft(page.locator('#sdGraphDetails')).toContainText(/preview|snapshot|shown|truncat/i);
        const neighbor = page.locator('#sdGraphDetails').getByRole('button').filter({ hasText: /Entity 52/ }).first();
        await tabTo(page, neighbor);
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphDetails')).toContainText('Synthetic evidence for entity 52');
        await page.locator('#sdGraphDetails').getByRole('button', { name: /back/i }).click();
        await expect(page.locator('#sdGraphDetails')).toContainText('Synthetic evidence for entity 51');
        const zoom = page.getByRole('button', { name: /zoom in/i });
        await touch(zoom, viewport);
        await zoom.click();
        await page.locator('#sdGraphFit').click();
        expect(state.calls).toHaveLength(requests);
        await frame(page);
        await evidence(page, state, info, 'graph-neighbor-return', viewport);

        // Same bounded projection, dense selected fallback: prove the actual
        // chip/leader branch after the original local-only read-budget check.
        const projection = state.graph.graph_view, selectedId = 'n51';
        const hubNeighbors = projection.nodes.filter(node => node.id !== selectedId).slice(0, 120);
        projection.edges = [...hubNeighbors.map((node, i) => ({ id: `caption-hub-${i}`, from: selectedId, to: node.id, type: 'RELATED_TO', weight: 1 })),
            ...projection.edges.filter(edge => edge.from !== selectedId && edge.to !== selectedId).slice(0, 200)];
        const hubReads = state.reads('graph_status').length;
        await action(page, 'sd-long-refresh').click();
        await expect.poll(() => state.reads('graph_status').length).toBe(hubReads + 1);
        await expect(page.locator('.sd-graph-node')).toHaveCount(160);
        await page.locator('#sdGraphSearch').fill('Entity 51');
        await tabTo(page, page.getByRole('button', { name: /^Inspect Entity 51 —/ }).first()); await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphDetails')).toContainText('Entity 51 — collective memory decision with a long readable label');
        // Immediate inspector access is established above. Frame the selected
        // visual association with native wheel, below the sticky instance bar.
        const visibleFrame = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, topbarBottom: document.querySelector('#portalTopbar').getBoundingClientRect().bottom }));
        const associationFrame = async () => {
            const label = await page.locator('.sd-graph-node.is-selected text').boundingBox();
            const shape = await page.locator('.sd-graph-node.is-selected .sd-graph-shape').boundingBox();
            const chip = await page.locator('.sd-graph-selected-caption').boundingBox();
            return [label, shape, chip].filter(Boolean);
        };
        const wheels = []; await page.mouse.move(visibleFrame.width - 30, visibleFrame.height - 30);
        for (let step = 0; step < 3; step++) {
            const boxes = await associationFrame();
            const delta = boxes.some(r => r.y < visibleFrame.topbarBottom) ? -200 : boxes.some(r => r.y + r.height > visibleFrame.height) ? 200 : 0;
            if (!delta) break;
            await page.mouse.wheel(0, delta); wheels.push(delta);
            await expect.poll(async () => (await associationFrame()).map(r => r.y)).not.toEqual(boxes.map(r => r.y));
        }
        await expect.poll(async () => (await associationFrame()).every(r => r.y >= visibleFrame.topbarBottom && r.y + r.height <= visibleFrame.height)).toBe(true);
        const hubAssociation = await graphLabelAssociations(page), hubGeometry = await selectedLabelGeometry(page);
        expect(hubAssociation.detachedLabels, 'Dense selected labels need a genuine owner-associated chip and leader').toEqual([]);
        const hubLabel = hubAssociation.labels.find(label => label.selected);
        expect(hubLabel).toBeTruthy();
        if (['tablet', 'mobile'].includes(viewport.name)) expect(hubLabel.associationMethod).toBe('verified-selected-chip-leader');
        expect(hubGeometry.overlaps).toEqual([]);
        expect(state.reads('graph_status')).toHaveLength(hubReads + 1);
        state.observations = { hubNeighbors: 120, hubAssociation, selectedLabelGeometry: hubGeometry, explicitGraphRefreshes: 1,
            associationFraming: { ...visibleFrame, wheels, rectangles: await associationFrame(), method: 'Bounded native wheel after immediate inspector assertion' } };
        await evidence(page, state, info, 'graph-selected-linked-caption', viewport);
    });

    for (const index of [0, 49]) {
        test(`#629 full 50-row homonym selection ${index + 1}, detail and return — ${viewport.name}`, async ({ page }, info) => {
            await configureViewport(page, viewport);
            const state = await routePortal(page);
            await enterLong(page, 'documents');
            const inspect = action(page, 'sd-document-inspect').nth(index);
            await expect(action(page, 'sd-document-inspect')).toHaveCount(50);
            await evidence(page, state, info, `documents-list-select-${index + 1}`, viewport);
            const rowBefore = inspect.locator('xpath=ancestor::tr');
            await expect(rowBefore.locator('.sd-document-path [title]')).toHaveAttribute('title', state.documents[index].source_path);
            await expect(rowBefore.locator(`[title="${state.documents[index].sha256}"]`)).toBeVisible();
            await expect.soft(page.locator('#sdDocumentStatus')).toHaveJSProperty('tagName', 'SELECT');
            await expect.soft(page.locator('[data-action="sd-documents-page"][data-step="-1"]')).toBeDisabled();
            await expect.soft(page.locator('[data-action="sd-documents-page"][data-step="1"]')).toBeDisabled();
            await touch(inspect, viewport);
            await tabTo(page, inspect);
            await focusedRing(inspect);
            await page.keyboard.press('Enter');
            await expect(page.locator('#sdDocumentDetail')).toContainText(`doc-${index + 1}`);
            await evidence(page, state, info, `documents-detail-select-${index + 1}`, viewport);
            await expectReachableDetail(page, '#sdDocumentInspector');
            if (viewport.name === 'desktop') {
                const statusGeometry = await page.locator('#sdDocumentsList tbody tr').nth(index === 49 ? 44 : 4).evaluate(row => {
                    const cell = row.children[1], next = row.children[2], range = document.createRange();
                    range.selectNodeContents(cell);
                    return { suppliedStatus: cell.innerText, statusLabel: range.getBoundingClientRect().toJSON(), statusCell: cell.getBoundingClientRect().toJSON(), versionCell: next.getBoundingClientRect().toJSON() };
                });
                state.observations = { unknownDocumentStatus: statusGeometry };
                await evidence(page, state, info, `documents-status-cell-select-${index + 1}`, viewport);
                expect.soft(statusGeometry.statusLabel.right, 'Supplied status must not obscure the neighboring version metadata').toBeLessThanOrEqual(statusGeometry.versionCell.left + 1);
            }
            const row = await inspect.evaluate(el => el.closest('tr, article')?.outerHTML || '');
            expect(row, 'Selected row must have explicit feedback').toMatch(/aria-selected="true"|is-selected|selected-row/);
            await expect(page.locator('#sdDocumentDetail')).toContainText(state.documents[index].repo_path);
            await expect(page.locator('#sdDocumentDetail')).toContainText(state.documents[index].last_ingest_job_id);
            expect(state.reads('long_document_get').every(req => !req.arguments.include_content)).toBe(true);
            await action(page, 'sd-document-content').click();
            if (index === 49) await expect(page.locator('#sdDocumentDetail')).toContainText(/binary|text preview.*unavailable/i);
            else await expect(page.locator('#sdDocumentDetail')).toContainText(HOSTILE);
            await closeDocumentInspector(page);
            await expect(inspect).toBeFocused();
            await frame(page);
            await evidence(page, state, info, `documents-return-select-${index + 1}`, viewport);
        });
    }

    test(`#631 healthy overview binding, volumes and supplied distribution — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await enterLong(page);
        const body = page.locator('#sdLongBody');
        await evidence(page, state, info, 'overview-healthy', viewport);
        await expect(body).toContainText(/embedded/i);
        await expect(body).toContainText('general');
        for (const type of Object.keys(state.graph.graph_stats.entity_types)) await expect(body).toContainText(type);
        await expect(body).toContainText('Captures pending indexing');
        await expect(body).toContainText(/zero backlog does not mean every current MID file is indexed/i);
        expect(state.reads('graph_status').map(req => req.arguments.include_graph)).toEqual([false]);
        expect(state.reads('long_document_list')).toHaveLength(0);
        await frame(page);
    });

    test(`#632 50 jobs, failure diagnostic, focused detail and return — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await enterLong(page, 'jobs');
        await expect(action(page, 'sd-ingest-inspect')).toHaveCount(50);
        await evidence(page, state, info, 'jobs-mixed-list', viewport);
        await expect.soft(page.locator('#sdIngestList')).toContainText('42%');
        await expect.soft(page.locator('#sdIngestList')).not.toContainText('in_memory_best_effort');
        await expect.soft(page.locator('[data-action="sd-ingest-page"][data-step="1"]')).toBeDisabled();
        await expect.soft(page.locator('[data-action="sd-ingest-page"][data-step="-1"]')).toBeDisabled();
        const inspect = action(page, 'sd-ingest-inspect').nth(3);
        await touch(inspect, viewport);
        await tabTo(page, inspect);
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdIngestDetail')).toContainText('ingest-4');
        await evidence(page, state, info, 'jobs-failure-selected', viewport);
        await expectReachableDetail(page, '#sdIngestDetail');
        await expect(page.locator('#sdIngestDetail')).toContainText('invalid_content');
        await expect(page.locator('#sdIngestDetail')).toContainText(HOSTILE);
        await expect(page.locator('#sdIngestDetail img')).toHaveCount(0);
        await page.getByRole('button', { name: /close.*job|back to.*jobs/i }).click();
        await expect(inspect).toBeFocused();
        for (const index of [0, 49]) {
            const boundary = action(page, 'sd-ingest-inspect').nth(index);
            await tabTo(page, boundary); await page.keyboard.press('Enter');
            await expect(page.locator('#sdIngestDetail')).toContainText(`ingest-${index + 1}`);
            await expectReachableDetail(page, '#sdIngestDetail');
            await evidence(page, state, info, `jobs-boundary-${index + 1}`, viewport);
            await page.getByRole('button', { name: 'Close job inspector', exact: true }).click();
            await expect(boundary).toBeFocused();
        }
        await page.locator('#sdIngestStatus').selectOption('queued');
        await action(page, 'sd-ingest-apply').click();
        expect(state.reads('long_ingest_list').at(-1).arguments).toMatchObject({ limit: 50, offset: 0, status: 'queued' });
        await frame(page);
    });

    test(`#633 mixed activity concise main path, discoverable diagnostics — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const state = await routePortal(page);
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        const running = page.locator('#dashRunningJobs [data-job-id="consol-running"]').first();
        await expect(running).toContainText('Running');
        await evidence(page, state, info, 'dashboard-mixed-activity', viewport);
        await expect(page.locator('.dash-notes')).toHaveJSProperty('open', false);
        await expect(page.locator('.dash-diagnostics')).toHaveJSProperty('open', false);
        expect(state.reads('space_list').map(call => call.arguments)).toEqual([{ include_counts: false }]);
        expect(state.reads('bank_consolidation_queues').map(call => call.arguments.space_ids)).toEqual(['demo']);
        expect(state.reads('live_read')).toHaveLength(0);
        await expect(running.locator('.dash-job-details')).toHaveJSProperty('open', false);
        const visibleText = await running.locator('.dash-job-summary').innerText();
        expect.soft(visibleText).not.toContain('bank_consolidation_status');
        expect.soft((await running.locator('[data-part="meta"]').innerText()).match(/qa-agent/g) || []).toHaveLength(1);
        await expect.soft(page.locator('#dashRunningJobs')).toContainText('different-requester');
        const noop = page.locator('#dashRecentJobs article[data-job-id="consol-noop"]');
        await tabTo(page, noop.getByRole('button', { name: 'Details', exact: true }));
        const noOpFraming = await frameNoOpArticle(page, noop);
        state.observations = { noOpFraming, noOp: await noop.evaluate(el => ({
            article: el.getBoundingClientRect().toJSON(), maintenance: el.querySelector('.dash-maintenance-summary').getBoundingClientRect().toJSON(),
            detailsOpen: el.querySelector('.dash-job-details').open, originalWholeArticleHeuristic: 230,
        })) };
        await evidence(page, state, info, 'dashboard-noop-compact', viewport);
        await assertCompactNoOp(noop);
        const resultDetails = noop.getByRole('button', { name: 'Details', exact: true });
        await resultDetails.click();
        await expect(page.getByRole('dialog')).toContainText('No oversized files');
        await page.keyboard.press('Escape'); await expect(resultDetails).toBeFocused();
        await expect.soft(page.locator('#dashRecentJobs')).toContainText(/recovery/i);
        const details = running.getByRole('button', { name: 'Details', exact: true });
        await tabTo(page, details);
        await page.keyboard.press('Enter');
        await expect(page.locator('#adminModal')).toContainText('bank_consolidation_status');
        await expect(page.locator('#adminModal img')).toHaveCount(0);
        await evidence(page, state, info, 'dashboard-diagnostics-detail', viewport);
        await page.keyboard.press('Escape');
        await expect(details).toBeFocused();
        // Failed consolidation and a partial result are independent facts.
        // Compaction's outcome cannot stand in for the consolidation result.
        const partial = page.locator('#dashRecentJobs article[data-job-id="consol-partial"]');
        await expect(partial).toContainText('Failed');
        await tabTo(page, partial.getByRole('button', { name: 'Details', exact: true }));
        const partialResultText = (await partial.locator('[data-part="outcome"] > :not(details):not(.dash-maintenance-summary)').allInnerTexts()).join(' ');
        state.observations.failedPartial = { suppliedStatus: 'failed', suppliedResultStatus: 'partial', visibleConsolidationOutcome: partialResultText };
        await evidence(page, state, info, 'dashboard-failed-partial-result', viewport);
        expect.soft(partialResultText, 'A failed job must retain its supplied partial consolidation result').toMatch(/partial/i);
        await frame(page);
    });
}

for (const panel of ['documents', 'jobs']) {
    test(`#${panel === 'documents' ? '629' : '632'} empty and malformed pages disable navigation (${panel})`, async ({ page }, info) => {
        await configureViewport(page, VIEWPORTS[0]);
        const state = await routePortal(page, { documents: [], jobs: [] });
        await enterLong(page, panel);
        const prefix = panel === 'documents' ? 'sd-documents' : 'sd-ingest';
        await evidence(page, state, info, `${panel}-empty`, VIEWPORTS[0]);
        await expect(action(page, `${prefix}-page`)).toHaveCount(2);
        for (const button of await action(page, `${prefix}-page`).all()) await expect.soft(button).toBeDisabled();
        const selector = panel === 'documents' ? '#sdDocumentDetail' : '#sdIngestDetail';
        const box = await page.locator(selector).count() ? await page.locator(selector).boundingBox() : null;
        expect.soft(box?.height || 0, 'Empty inspector does not reserve a large reading pane').toBeLessThan(100);
        state[panel === 'documents' ? 'documentEnvelope' : 'jobEnvelope'] = { offset: '<b>spoof</b>', count: -1 };
        await action(page, 'sd-long-refresh').click();
        await expect(page.locator(panel === 'documents' ? '#sdDocumentsPage' : '#sdIngestPage')).toContainText('Pagination unavailable');
        for (const button of await action(page, `${prefix}-page`).all()) await expect(button).toBeDisabled();
        await expect(page.locator('#sdLongBody b')).toHaveCount(0);
    });
}

// Each mutant preserves the summary/disclosure, then violates one specific clause.
test('#633 compact no-op oracle rejects uncollapsed state, open details and unsafe HTML', async ({ page, context }, info) => {
    test.setTimeout(60_000);
    for (const kind of ['uncollapsed-state', 'details-open', 'unsafe-html']) {
        const mutantPage = await context.newPage(), viewport = VIEWPORTS[2];
        await configureViewport(mutantPage, viewport);
        const state = await routePortal(mutantPage, { mutationNoOp: kind });
        if (kind === 'unsafe-html') state.lanes[0].latest_jobs[0].message = 'Use bank_consolidation_status for an explicit check. <img alt="unescaped-synthetic-markup">';
        await mutantPage.goto(`${ORIGIN}/admin.html#/dashboard`);
        const noop = mutantPage.locator('#dashRecentJobs article[data-job-id="consol-noop"]');
        await expect(noop.locator('.dash-maintenance-summary')).toHaveText('Automatic compaction: No oversized files');
        await expect(noop.locator('.dash-job-details')).toHaveCount(1);
        await tabTo(mutantPage, noop.getByRole('button', { name: 'Details', exact: true }));
        let rejected;
        try { await assertCompactNoOp(noop); } catch (error) { rejected = error; }
        expect(rejected, `The ${kind} mutant must be rejected by the same compactness oracle`).toBeTruthy();
        expect(rejected.message).toContain(kind === 'details-open' ? 'toHaveJSProperty' : 'toHaveCount');
        state.observations = { oracleRejection: { kind, matcher: rejected.matcherResult?.name, message: rejected.message } };
        await evidence(mutantPage, state, info, `mutation-noop-${kind}`, viewport);
        expect(state.mutation.kind).toBe(`no-op ${kind}`);
        await mutantPage.close();
    }
});
