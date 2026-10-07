import { test, expect, chromium } from '@playwright/test';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const STATIC = path.join(ROOT, 'src/live_mem/static');
const ORIGIN = 'http://127.0.0.1:18632'; // All requests intercepted; no production/server access.
const PROOF = path.join(ROOT, 'proof-artifacts/portal-qa-632');
const baseline = process.env.PORTAL_632_BASELINE === '1';
const statuses = ['running', 'queued', 'failed', 'succeeded', 'cancelled', 'skipped'];
const jobs50 = () => Array.from({ length: 50 }, (_, i) => ({
    job_id: `ing_632_${i}`, filename: ['projectbrief.md', 'techContext.md', 'research-notes.md', 'systemPatterns.md', 'cancelled.md', 'unchanged.md'][i % 6],
    status: statuses[i % 6], current_step: ['llm_extract', 'queued', 'failed', 'done', 'cancelled', 'skipped'][i % 6],
    ...(i % 6 === 0 ? { progress_percent: 62 } : i % 6 === 3 ? { progress_percent: 100 } : {}),
    created_entities: i % 6 === 3 ? 41 : 0, created_relations: i % 6 === 3 ? 32 : 0,
    ...(i % 6 === 1 ? { queue_position: i + 1 } : {}),
    updated_at: '2026-09-30T15:13:00Z', polling: { recommended: false },
    ...(i % 6 === 2 ? { error: 'Ontology validation failed <img src=x onerror=window.__xss=1>', ontology_diagnostics: {
        entity_other: { count: 0, denominator: 12, rate: 0 },
        relation_related_to: { count: 2, denominator: 8, rate: 0.25 },
        '<script>': { reported_type: '<img src=x onerror=window.__xss=1>', message: 'Type is absent from the selected ontology.' },
    } } : {}),
}));

async function setup(page, options = {}) {
    const state = { calls: [], jobs: jobs50(), total: 51, cancel: { status: 'cancelling', message: 'Stops at the next phase boundary.' }, ...options };
    await page.addInitScript(() => { window.__xss = 0; });
    await page.route('**/*', async route => {
        const p = new URL(route.request().url()).pathname;
        const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
        if (p === '/api/spaces') return json({ status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (p === '/api/tool') {
            const call = route.request().postDataJSON(); state.calls.push(call);
            const args = call.arguments;
            if (call.tool === 'system_whoami') return json({ status: 'ok', client_name: 'qa-632', permissions: state.permissions || ['admin'] });
            if (call.tool === 'space_info') return json({ status: 'ok', space_id: 'demo', description: 'Ingestion QA fixtures', hive_status_label: 'local_only', live: {}, bank: {}, consolidation_queue: { lane_state: 'idle' } });
            if (call.tool === 'long_ingest_list') {
                const filtered = state.jobs.filter(j => (!args.status || j.status === args.status) && (!args.batch_id || j.batch_id === args.batch_id));
                const jobs = args.status || args.batch_id ? filtered.slice(args.offset, args.offset + 50) : args.offset === 50 ? [state.jobs[49]].filter(Boolean) : state.jobs;
                return json(state.list || { status: 'ok', jobs, count: jobs.length, total: args.status || args.batch_id ? filtered.length : state.total, offset: args.offset, limit: 50, guarantee: 'in_memory_best_effort' });
            }
            if (call.tool === 'long_ingest_status') return json((typeof state.detail === 'function' ? await state.detail(args) : await state.detail) || state.jobs.find(j => j.job_id === args.job_id) || { status: 'not_found' });
            if (call.tool === 'long_ingest_cancel') { state.afterCancel?.(); return json(state.cancel); }
            if (call.tool === 'space_list') return json({ status: 'ok', spaces: [] });
            return json({ status: 'error', message: `Unexpected fixture tool: ${call.tool}` });
        }
        if (p === '/health') return json({ version: 'qa-632' });
        const relative = p === '/admin.html' || p === '/' ? 'admin.html' : p.startsWith('/static/') ? p.slice(8) : '';
        const file = path.join(STATIC, relative);
        if (relative && file.startsWith(STATIC) && fs.existsSync(file) && fs.statSync(file).isFile()) {
            const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
            const source = process.env.PORTAL_632_SOURCE && relative.endsWith('/views-space-detail.js') ? process.env.PORTAL_632_SOURCE : file;
            return route.fulfill({ contentType: types[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(source) });
        }
        return route.fulfill({ status: 404, body: '' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/jobs`);
    await expect(page.locator(options.readyRegion || '#sdIngestList')).toContainText(options.readyText || 'projectbrief.md');
    return state;
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`632 real bundle selection and visual proof ${width}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const state = await setup(page);
        await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]').focus();
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdIngestDetail')).toContainText('research-notes.md');
        fs.mkdirSync(PROOF, { recursive: true });
        if (baseline) {
            await page.screenshot({ path: path.join(PROOF, `before-${width}.png`), fullPage: true }); return;
        }
        await expect(page.locator('#sdIngestDetail')).toBeFocused();
        await expect(page.locator('#sdIngestList tbody tr')).toHaveCount(50);
        for (const [index, status] of ['Running', 'Queued', 'Failed', 'Succeeded', 'Cancelled', 'Skipped'].entries()) {
            await expect(page.locator('#sdIngestList tbody tr').nth(index).locator('.status-dot-label')).toHaveText(status);
        }
        for (const dot of await page.locator('#sdIngestList .status-dot').all()) {
            const box = await dot.boundingBox(); expect(box.width).toBe(8); expect(box.height).toBe(8);
        }
        await expect(page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]')).toHaveAttribute('aria-expanded', 'true');
        await expect(page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]')).toHaveAccessibleName('Inspect ingestion research-notes.md');
        await expect(page.locator('#sdIngestDetail')).toContainText('Ontology diagnostics');
        await expect(page.locator('#sdIngestDetail')).toContainText('2');
        await expect(page.locator('#sdIngestDetail img, #sdIngestDetail script')).toHaveCount(0);
        expect(await page.evaluate(() => window.__xss)).toBe(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.screenshot({ path: path.join(PROOF, `after-${width}.png`), fullPage: true });
        await page.locator('#sdIngestDetail h4').evaluate(el => {
            const detail = el.closest('article');
            if (detail.scrollHeight > detail.clientHeight) detail.scrollTop = el.offsetTop - detail.offsetTop - 16;
            else { el.style.scrollMarginTop = `${document.getElementById('portalTopbar').getBoundingClientRect().height + 12}px`; el.scrollIntoView({ block: 'start' }); }
        });
        await page.screenshot({ path: path.join(PROOF, `after-${width}-diagnostics.png`), fullPage: true });
        await page.getByRole('button', { name: 'Close job inspector' }).click();
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await expect(page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]')).toBeFocused();
        await page.locator('#sdIngestList').evaluate(el => { el.style.scrollMarginTop = `${document.getElementById('portalTopbar').getBoundingClientRect().height + 12}px`; el.scrollIntoView({ block: 'start' }); });
        await page.screenshot({ path: path.join(PROOF, `after-${width}-list.png`), fullPage: true });
        const row = page.locator('[data-job-id="ing_632_0"]').filter({ has: page.locator('td') });
        await expect(row).toContainText('62%'); await expect(row).toContainText('Extracting entities and relations');
        expect(state.calls.filter(c => c.tool === 'long_ingest_list')).toHaveLength(1);
        expect(state.calls.filter(c => c.tool === 'long_ingest_status')).toHaveLength(0);
        expect(state.calls.filter(c => c.tool === 'long_ingest_list')[0].arguments.limit).toBe(50);
    });
}

test('632 status filter labels preserve API values', async ({ page }) => {
    const state = await setup(page);
    expect(await page.locator('#sdIngestStatus option').allTextContents()).toEqual(['Any status', 'Queued', 'Running', 'Succeeded', 'Failed', 'Cancelled', 'Skipped', 'Changed · skipped']);
    await page.locator('#sdIngestStatus').selectOption({ label: 'Changed · skipped' });
    await page.getByRole('button', { name: 'Apply filters' }).click();
    await expect.poll(() => state.calls.filter(c => c.tool === 'long_ingest_list').at(-1).arguments.status).toBe('changed_skipped');
});

for (const total of [40, 0]) {
    test(`642 F1 explicit first-page recovery after total shrinks to ${total}`, async ({ page }) => {
        const state = await setup(page);
        await page.locator('#sdIngestNext').click();
        await expect(page.locator('#sdIngestPage')).toContainText('51–51');
        state.list = { status: 'ok', jobs: [], offset: 50, count: 0, total };
        await page.evaluate(() => PortalRefresh.refresh());
        const reads = state.calls.length;
        await expect(page.getByRole('button', { name: 'Back to first page', exact: true })).toBeVisible();
        await page.screenshot({ path: path.join(PROOF, `round-2/recovery-total-${total}.png`), fullPage: true });
        expect(state.calls).toHaveLength(reads);
        state.list = null;
        await page.getByRole('button', { name: 'Back to first page', exact: true }).focus();
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdIngestList')).toContainText('projectbrief.md');
        expect(state.calls.filter(c => c.tool === 'long_ingest_list').at(-1).arguments.offset).toBe(0);
        expect(state.calls).toHaveLength(reads + 1);
        await expect(page.locator('#sdIngestStatus')).toBeFocused();
    });
}

for (const [other, outcome] of [[false, 'cancelled'], [true, 'cancelled'], [false, 'not_found'], [false, 'error'], [false, 'running']]) {
    test(`642 F2 Running-filter cancellation outcome ${outcome}, other job ${other}`, async ({ page }) => {
        await page.clock.install();
        const target = jobs50()[0], another = { ...jobs50()[6], job_id: 'other', filename: 'other.md' };
        const state = await setup(page, { jobs: other ? [target, another] : [target], total: other ? 2 : 1,
            cancel: { status: 'cancelling', message: 'Cancellation requested (best effort at the next phase boundary).' } });
        await page.locator('#sdIngestStatus').selectOption('running');
        await page.getByRole('button', { name: 'Apply filters' }).click();
        await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_0"]').click();
        state.afterCancel = () => { target.status = 'cancelled'; };
        state.detail = outcome === 'not_found' || outcome === 'error' ? { status: outcome, message: '<unavailable outcome>' } : { ...target, status: outcome };
        await page.locator('#sdIngestDetail [data-action="sd-ingest-cancel"]').click();
        const before = state.calls.length;
        await page.getByRole('button', { name: 'Request cancellation', exact: true }).click();
        await expect.poll(() => state.calls.filter(c => c.tool === 'long_ingest_status').length).toBe(1);
        await expect(page.locator('#sdIngestCancelResult')).toContainText('Updated');
        const primary = page.locator('#sdIngestCancelResult > p').first();
        await expect(primary).not.toContainText('pending cooperative stop');
        if (outcome === 'cancelled') await expect(primary).toContainText('Cancelled');
        else if (outcome === 'running') await expect(primary).toContainText('Running');
        else { await expect(primary).toContainText('not confirmed'); await expect(primary).not.toContainText('Cancelled'); }
        expect(state.calls.slice(before).map(c => c.tool)).toEqual(['long_ingest_cancel', 'long_ingest_list', 'long_ingest_status']);
        if (other) await expect(page.locator('#sdIngestDetail')).toBeVisible();
        else await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await page.screenshot({ path: path.join(PROOF, `round-2/cancel-${outcome}-${other}.png`), fullPage: true });
        if (!other) {
            expect(await page.evaluate(() => PortalRefresh.state().eligible)).toBe(false);
            const reads = state.calls.length; await page.clock.runFor(60001); expect(state.calls).toHaveLength(reads);
        }
        if (outcome !== 'cancelled') {
            state.detail = { ...target, status: 'cancelled' };
            await page.getByRole('button', { name: 'Check cancellation result', exact: true }).click();
            await expect(primary).toContainText('Cancelled');
            expect(state.calls.filter(c => c.tool === 'long_ingest_cancel')).toHaveLength(1);
        }
    });
}

test('642 F3 F4 readable stage, single failure label, honest active position and exact cancel message', async ({ page }) => {
    const state = await setup(page);
    state.jobs = [{ ...jobs50()[0], current_step: 'ontology_construction', queue_position: 1 }, jobs50()[2]]; state.total = 2;
    await page.evaluate(() => PortalRefresh.refresh());
    await expect(page.locator('#sdIngestList')).toContainText('Building ontology');
    await expect(page.locator('#sdIngestList')).toContainText('Active (position 1)');
    await expect(page.locator('#sdIngestList')).not.toContainText('ontology_construction');
    await expect(page.locator('#sdIngestList')).not.toContainText('Queue 1');
    await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]').click();
    await expect(page.locator('#sdIngestDetail .status-dot-label')).toHaveText('Failed');
    await expect(page.locator('#sdIngestDetail .sd-ingest-stage')).not.toContainText('Failed');
    state.cancel = { status: 'noop', message: 'Job already completed' };
    await page.locator('[data-action="sd-ingest-cancel"][data-job-id="ing_632_0"]').click();
    await page.getByRole('button', { name: 'Request cancellation', exact: true }).click();
    await expect(page.locator('#sdIngestCancelResult')).toContainText('Job already completed');
    expect((await page.locator('#sdIngestCancelResult').innerText()).match(/Job already completed/g)).toHaveLength(1);
});

for (const [width, height] of [[1440, 900], [390, 844]]) {
    test(`632 empty history and filtered empty state ${width}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const state = await setup(page, { jobs: [], total: 0, readyText: 'No ingestion history available' });
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion history available');
        await expect(page.locator('#sdIngestList')).toContainText('This history may be incomplete.');
        await expect(page.locator('#sdIngestList')).toContainText('Browse the document catalog to see available documents.');
        await expect(page.locator('#sdIngestList')).not.toContainText('best-effort');
        await expect(page.locator('#sdIngestList')).not.toContainText('durable record');
        await expect(page.locator('#sdIngestList')).not.toContainText('This does not establish');
        await expect(page.locator('#sdIngestList')).not.toContainText('after restart');
        await expect(page.locator('#sdIngestList .state-empty a')).toHaveAttribute('href', '#/spaces/demo/long/documents');
        await expect(page.getByLabel('Ingestion pages')).toBeHidden();
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await page.locator('#sdIngestList').evaluate(el => { el.style.scrollMarginTop = `${document.getElementById('portalTopbar').getBoundingClientRect().height + 12}px`; el.scrollIntoView({ block: 'start' }); });
        await page.screenshot({ path: path.join(PROOF, `after-empty-${width}.png`), fullPage: true });
        await page.locator('#sdIngestStatus').selectOption('failed');
        await page.locator('#sdIngestBatch').fill('batch-no-match');
        await page.getByRole('button', { name: 'Apply filters' }).click();
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion jobs match these filters');
        await expect(page.getByRole('button', { name: 'Clear filters', exact: true })).toBeVisible();
        await expect(page.getByLabel('Ingestion pages')).toBeHidden();
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.locator('#sdIngestList').evaluate(el => { el.style.scrollMarginTop = `${document.getElementById('portalTopbar').getBoundingClientRect().height + 12}px`; el.scrollIntoView({ block: 'start' }); });
        await page.screenshot({ path: path.join(PROOF, `after-filter-empty-${width}.png`), fullPage: true });
        state.jobs = [jobs50()[0]]; state.total = 1;
        await page.getByRole('button', { name: 'Clear filters', exact: true }).focus();
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdIngestList')).toContainText('projectbrief.md');
        await expect(page.locator('#sdIngestStatus')).toHaveValue('');
        await expect(page.locator('#sdIngestBatch')).toHaveValue('');
        expect(state.calls.filter(c => c.tool === 'long_ingest_list').at(-1).arguments).toEqual({ space_id: 'demo', limit: 50, offset: 0, status: '', batch_id: '' });
        await expect(page.getByLabel('Ingestion pages')).toBeHidden();
        await expect(page.locator('#sdIngestStatus')).toBeFocused();
        await page.locator('[data-action="sd-ingest-inspect"]').click();
        state.jobs = []; state.total = 0;
        await page.locator('#sdIngestStatus').selectOption('failed');
        await page.getByRole('button', { name: 'Apply filters' }).click();
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion jobs match these filters');
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
    });
}

test('632 errors partial reads and malformed pagination never imply empty history', async ({ page }) => {
    test.skip(baseline);
    const state = await setup(page);
    for (const list of [
        { status: 'ok', jobs: [], offset: 0, count: 0, total: 51 },
        { status: 'ok', jobs: [], offset: '<img src=x>', count: 0, total: 51 },
        { status: 'ok', jobs: [], offset: 0, count: 0, total: '51' },
        { status: 'ok', jobs: [], offset: 0, count: 1, total: 51 },
        { status: 'ok', jobs: [], offset: 0, count: 0, total: 0, partial: true, warnings: ['<source unavailable>'] },
        { status: 'partial', jobs: [], offset: 0, count: 0, total: 0, message: '<partial read>' },
        { status: 'error', jobs: [], offset: 0, count: 0, total: 0, message: 'Access denied' },
    ]) {
        state.list = list; await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
        if (list.status === 'ok') await expect(page.locator('#sdIngestList')).toContainText('Ingestion history is incomplete');
        else await expect(page.locator('#sdIngestFreshness')).toContainText(list.message);
        await expect(page.locator('#sdIngestList .state-empty')).toHaveCount(0);
        await expect(page.locator('#sdIngestList')).not.toContainText('No ingestion history');
        await expect(page.getByLabel('Ingestion pages')).toBeHidden();
        if (list.partial) await expect(page.locator('#sdIngestList')).toContainText('<source unavailable>');
        if (list.status !== 'ok') await expect(page.locator('#sdIngestFreshness')).toContainText(list.message);
        await expect(page.locator('[data-action="sd-ingest-page"]')).toHaveCount(2);
        for (const button of await page.locator('[data-action="sd-ingest-page"]').all()) await expect(button).toBeDisabled();
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await expect(page.locator('#sdIngestList')).not.toContainText('in_memory_best_effort');
    }
    state.list = null; await page.getByRole('button', { name: 'Apply filters' }).click();
    await expect(page.locator('[data-action="sd-ingest-page"][data-step="-1"]')).toBeDisabled();
    await page.locator('[data-action="sd-ingest-page"][data-step="1"]').click();
    await expect(page.locator('#sdIngestPage')).toContainText('51–51');
    await expect(page.locator('[data-action="sd-ingest-page"][data-step="1"]')).toBeDisabled();
});

test('632 initial denied history is unavailable and has no empty inspector', async ({ page }) => {
    await setup(page, { permissions: ['read'], readyRegion: '#sdIngestFreshness', readyText: 'Access denied', list: { status: 'error', message: 'Access denied' } });
    await expect(page.locator('#sdIngestList .state-empty')).toHaveCount(0);
    await expect(page.getByLabel('Ingestion pages')).toBeHidden();
    await expect(page.locator('#sdIngestDetail')).toBeHidden();
});

for (const [index, status] of [[3, 'succeeded'], [0, 'running']]) {
    test(`632 confirmed empty refresh closes selected ${status} inspector and stops follow`, async ({ page }) => {
        await page.setViewportSize(status === 'running' ? { width: 390, height: 844 } : { width: 1440, height: 900 });
        await page.clock.install();
        const state = await setup(page);
        await page.locator(`[data-action="sd-ingest-inspect"][data-job-id="ing_632_${index}"]`).click();
        await page.evaluate(() => PortalRefresh.configure({ enabled: true, intervalSeconds: 15 }));
        if (status === 'succeeded') await page.getByRole('button', { name: 'Close job inspector' }).focus();
        else await page.locator('#sdIngestDetail').focus();
        state.jobs = []; state.total = 0;
        await page.evaluate(() => PortalRefresh.refresh());
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion history available');
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await expect(page.locator('#sdIngestStatus')).toBeFocused();
        expect(state.calls.filter(c => c.tool === 'long_ingest_status')).toHaveLength(0);
        expect(await page.evaluate(() => PortalRefresh.state())).toMatchObject({ enabled: true, intervalSeconds: 15, eligible: false, busy: false });
        const calls = state.calls.length;
        await page.clock.runFor(60001);
        expect(state.calls).toHaveLength(calls);
        await page.screenshot({ path: path.join(PROOF, `after-empty-close-${status}.png`), fullPage: true });
    });
}

test('632 incomplete failed and nonempty refreshes retain selected detail without stealing focus', async ({ page }) => {
    const state = await setup(page);
    await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_3"]').click();
    for (const list of [
        { status: 'ok', jobs: [], count: 0, total: 0, offset: 0, partial: true },
        { status: 'ok', jobs: [], count: 1, total: 0, offset: 0 },
        { status: 'error', message: 'Access denied' },
        { status: 'ok', jobs: [state.jobs[0]], count: 1, total: 1, offset: 0 },
    ]) {
        state.list = list;
        await page.evaluate(() => PortalRefresh.refresh().catch(() => {}));
        await expect(page.locator('#sdIngestDetail')).toBeVisible();
        await expect(page.locator('#sdIngestDetail')).toContainText('systemPatterns.md');
        await expect(page.locator('#sdIngestDetail')).toBeFocused();
    }
    state.list = { status: 'ok', jobs: [], count: 0, total: 0, offset: 0 };
    await page.locator('#sdIngestBatch').focus();
    await page.evaluate(() => PortalRefresh.refresh());
    await expect(page.locator('#sdIngestDetail')).toBeHidden();
    await expect(page.locator('#sdIngestBatch')).toBeFocused();
});

test('632 progress and optional fields are honest', async ({ page }) => {
    test.skip(baseline);
    const state = await setup(page);
    state.jobs = [{ job_id: 'sparse', status: 'skipped' }, { job_id: 'bad', status: 'failed', progress_percent: 101, created_entities: -1, updated_at: 'not a date', ontology_diagnostics: {} }];
    state.total = 2;
    await page.getByRole('button', { name: 'Apply filters' }).click();
    await expect(page.locator('#sdIngestList progress')).toHaveCount(0);
    await expect(page.locator('#sdIngestList')).not.toContainText('%');
    await expect(page.locator('#sdIngestList')).not.toContainText('not a date');
    await page.locator('[data-action="sd-ingest-inspect"]').first().click();
    await expect(page.locator('#sdIngestDetail')).not.toContainText('Entities created');
    await expect(page.locator('#sdIngestDetail')).not.toContainText('Ontology diagnostics');
    await page.locator('[data-action="sd-ingest-inspect"]').last().click();
    await expect(page.locator('#sdIngestDetail progress')).toHaveCount(0);
    await expect(page.locator('#sdIngestDetail')).not.toContainText('Entities created');
    await expect(page.locator('#sdIngestDetail')).not.toContainText('not a date');
});

for (const outcome of ['cancelling', 'cancelled', 'not_found', 'noop', 'error']) {
    test(`632 row cancellation requires confirmation and reports ${outcome}`, async ({ page }) => {
        test.skip(baseline);
        const state = await setup(page, { cancel: { status: outcome, message: '<server cancellation outcome>' } });
        await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]').click();
        await page.locator('[data-action="sd-ingest-cancel"][data-job-id="ing_632_0"]').click();
        await expect(page.locator('.modal-body')).toContainText('ing_632_0');
        expect(state.calls.filter(c => c.tool === 'long_ingest_cancel')).toHaveLength(0);
        await page.getByRole('button', { name: 'Request cancellation', exact: true }).click();
        await expect(page.locator('#sdIngestCancelResult')).toContainText('<server cancellation outcome>');
        expect(state.calls.filter(c => c.tool === 'long_ingest_cancel').map(c => c.arguments)).toEqual([{ space_id: 'demo', job_id: 'ing_632_0' }]);
        await expect(page.locator('#sdIngestDetail')).toContainText('research-notes.md');
        await expect(page.locator('#sdIngestCancelResult img')).toHaveCount(0);
        if (outcome === 'cancelling') await expect(page.locator('#sdIngestList tbody tr').first()).toContainText('Running');
    });
}

test('632 reflow equivalent to 200% retains readable detail and focus', async ({ page }) => {
    test.skip(baseline);
    await page.setViewportSize({ width: 1440, height: 900 });
    await setup(page);
    // Halved CSS viewport verifies reflow; this is not native browser zoom.
    await page.setViewportSize({ width: 720, height: 450 });
    await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]').focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#sdIngestDetail')).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(PROOF, 'after-200-layout.png'), fullPage: true });
});

test('632 optional native Chrome zoom 200% proof', async () => {
    test.skip(baseline || process.env.PORTAL_632_NATIVE_ZOOM !== '1', 'Local proof requires installed Google Chrome; reflow runs in CI.');
    fs.mkdirSync(PROOF, { recursive: true });
    const profile = fs.mkdtempSync(path.join(PROOF, 'chrome-zoom-'));
    let context;
    try {
        const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim();
        const trackedChanges = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], { cwd: ROOT, encoding: 'utf8' }).trim();
        expect(trackedChanges).toBe('');
        context = await chromium.launchPersistentContext(profile, { channel: 'chrome', headless: true, viewport: { width: 1440, height: 900 } });
        const settings = context.pages()[0];
        await settings.goto('chrome://settings/appearance');
        await settings.locator('#zoomLevel').selectOption({ label: '200%' });
        const page = await context.newPage();
        await setup(page);
        const metrics = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, dpr: devicePixelRatio }));
        expect(metrics.width).toBe(720); expect(metrics.dpr).toBe(2);
        await page.locator('[data-action="sd-ingest-inspect"][data-job-id="ing_632_2"]').focus();
        await page.keyboard.press('Enter');
        await expect(page.locator('#sdIngestDetail')).toBeFocused();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        const session = await context.newCDPSession(page);
        const capture = await session.send('Page.captureScreenshot', { fromSurface: true, captureBeyondViewport: false });
        fs.writeFileSync(path.join(PROOF, 'after-native-200.png'), Buffer.from(capture.data, 'base64'));
        await session.detach();
        fs.writeFileSync(path.join(PROOF, 'native-zoom-metrics.json'), JSON.stringify(metrics, null, 2));
        fs.writeFileSync(path.join(PROOF, 'native-zoom-provenance.json'), JSON.stringify({ head, cwd: ROOT, trackedChanges, captured_at: new Date().toISOString(), browser: context.browser().version(), zoom: 'Chrome Settings 200%', metrics, temporaryProfile: profile }, null, 2));
    } finally { try { await context?.close(); } finally { fs.rmSync(profile, { recursive: true, force: true }); } }
});
