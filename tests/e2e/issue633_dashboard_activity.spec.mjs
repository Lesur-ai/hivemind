/** Issue #633 visual proof against the real local Portal bundle and local fixture responses. */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const CAPTURES = process.env.ISSUE633_CAPTURE_DIR || path.join(os.tmpdir(), 'hivemind-issue633-captures');
let server;
let origin;

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
};

test.beforeAll(async () => {
    fs.mkdirSync(CAPTURES, { recursive: true });
    server = http.createServer((request, response) => {
        const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
        const relative = pathname.startsWith('/static/') ? pathname.slice('/static/'.length) : pathname.slice(1);
        const candidate = path.resolve(STATIC, relative);
        if (!candidate.startsWith(STATIC + path.sep) || !fs.existsSync(candidate) || !fs.statSync(candidate).isFile()) {
            response.writeHead(404).end('not found');
            return;
        }
        response.writeHead(200, { 'content-type': CONTENT_TYPE[path.extname(candidate)] || 'application/octet-stream' });
        fs.createReadStream(candidate).pipe(response);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
});

test.afterAll(async () => {
    if (server) await new Promise(resolve => server.close(resolve));
});

function json(route, data) {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
}

const longDiagnostic = `Agent instructions: Do not wait for this job. Call bank_consolidation_status with the returned job identifier. ${'<diagnostic>'.repeat(160)}`;

function fixtureJobs() {
    return {
        running: {
            job_id: 'job-running-633', space_id: 'ops-space', status: 'running', scope_label: 'Agent: writer',
            agent: 'writer', requested_by: 'writer', started_at: '2026-09-30T08:10:00Z',
            message: longDiagnostic,
            progress: { notes_done: 3, notes_total: 12, batches_done: 1, batches_total: 4 },
        },
        queued: {
            job_id: 'job-queued-633', space_id: 'ops-space', status: 'queued', scope_label: 'All agents',
            agent: '', requested_by: 'operator', queued_at: '2026-09-30T08:12:00Z', queue_position: 2,
            message: longDiagnostic,
        },
        noOp: {
            job_id: 'job-noop-633', space_id: 'ops-space', status: 'succeeded', scope_label: 'My notes',
            agent: 'writer', requested_by: 'writer', finished_at: '2026-09-30T08:05:00Z', message: longDiagnostic,
            result: { notes_total: 4, notes_processed: 4, auto_compaction: { status: 'not_needed' } },
        },
        recovery: {
            job_id: 'job-recovery-633', space_id: 'ops-space', status: 'succeeded', scope_label: 'All agents',
            requested_by: 'operator', finished_at: '2026-09-30T08:03:00Z',
            result: { notes_total: 8, notes_processed: 8, auto_compaction: {
                status: 'partial', recovery_required: true, failure_reason: longDiagnostic,
                started_at: '2026-09-30T08:02:00Z', finished_at: '2026-09-30T08:03:00Z',
            } },
        },
        recoveryNeutral: {
            job_id: 'job-recovery-neutral-633', space_id: 'ops-space', status: 'succeeded', scope_label: 'All agents',
            finished_at: '2026-09-30T07:59:00Z',
            result: { auto_compaction: { status: 'not_needed', recovery_required: true } },
        },
        cancelled: {
            job_id: 'job-cancelled-633', space_id: 'ops-space', status: 'succeeded', scope_label: 'All agents',
            requested_by: 'operator', finished_at: '2026-09-30T08:02:00Z',
            result: { notes_total: 2, notes_processed: 2, auto_compaction: { status: 'cancelled' } },
        },
        failed: {
            job_id: 'job-failed-633', space_id: 'ops-space', status: 'failed', scope_label: 'Agent: analyst',
            agent: 'analyst', requested_by: 'operator', finished_at: '2026-09-30T08:01:00Z', error: longDiagnostic,
            result: { status: 'error', batches_completed: 0, batches_total: 4, notes_processed: 0, notes_total: 8 },
        },
        partial: {
            job_id: 'job-partial-633', space_id: 'ops-space', status: 'failed', scope_label: 'All agents',
            finished_at: '2026-09-30T08:01:30Z', error: 'Consolidation failed. Check server logs.',
            result: { status: 'partial', batches_completed: 2, batches_total: 4, notes_processed: 4, notes_total: 8 },
        },
        ambiguous: {
            job_id: 'job-ambiguous-633', space_id: 'ops-space', status: 'failed', scope_label: 'All agents',
            finished_at: '2026-09-30T08:01:20Z', error: 'Consolidation failed. Check server logs.',
            result: { status: 'partial', batches_completed: 0, batches_total: 4, notes_processed: 0, notes_total: 8 },
        },
        consolidationCancelled: {
            job_id: 'job-consolidation-cancelled-633', space_id: 'ops-space', status: 'failed', scope_label: 'All agents',
            finished_at: '2026-09-30T08:01:10Z',
            error: 'Consolidation was cancelled; bank recovery may be incomplete. Check the server logs before retrying.',
            result: { status: 'partial', failure_reason: 'consolidation_cancelled' },
        },
        missing: {
            job_id: 'job-missing-633', space_id: 'ops-space', status: 'succeeded', scope_label: 'All agents',
            finished_at: '2026-09-30T08:00:00Z',
        },
    };
}

test('Dashboard activity is concise, safe, compact and readable at the required viewports', async ({ page }) => {
    const jobs = fixtureJobs();
    const calls = [];
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.__issue633Xss = 0; });
    await page.route('**/api/spaces', route => json(route, { status: 'ok', spaces: [{ space_id: 'ops-space' }] }));
    await page.route('**/health', route => json(route, { version: 'issue-633-fixture' }));
    await page.route('**/api/tool', async route => {
        const request = route.request().postDataJSON();
        calls.push(request);
        switch (request.tool) {
        case 'system_whoami': return json(route, { status: 'ok', client_name: 'operator', auth_type: 'stored', permissions: ['read', 'write', 'manage'] });
        case 'space_list': return json(route, { status: 'ok', total: 1, spaces: [{ space_id: 'ops-space', last_consolidation: '2026-09-30T08:05:00Z', consolidation_count: 47, total_notes_processed: 824, description: 'Operations knowledge shared across the team' }] });
        case 'system_health': return json(route, { status: 'healthy', version: '1.6.0', spaces_count: 1, services: { s3: { status: 'ok' }, llmaas: { status: 'ok' } } });
        case 'bank_consolidation_queues': return json(route, { status: 'ok', lanes: [{
            space_id: 'ops-space', lane_state: 'running', queued_count: 1,
            running_job: jobs.running, queued_jobs: [jobs.queued],
            latest_jobs: [jobs.noOp, jobs.recovery, jobs.cancelled, jobs.failed, jobs.partial, jobs.ambiguous, jobs.consolidationCancelled, jobs.missing, jobs.recoveryNeutral],
        }] });
        default: return json(route, { status: 'error', message: `Unexpected ${request.tool}` });
        }
    });
    await page.goto(`${origin}/admin.html#/dashboard`);
    await expect(page.locator('#dashRunningJobs article')).toHaveCount(2);
    await expect(page.locator('#dashRecentJobs article')).toHaveCount(9);
    await expect(page.locator('#dashRunningJobs article[data-job-id="job-running-633"] [data-part="progress"]')).toContainText('3/12 notes');
    await expect(page.locator('#dashRunningJobs article[data-job-id="job-running-633"] [data-part="outcome"]')).toContainText('Consolidation in progress');
    await expect(page.locator('#dashRunningJobs article[data-job-id="job-running-633"] .dash-job-summary')).not.toContainText('bank_consolidation_status');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-noop-633"] [data-part="meta"]')).toContainText('My notes');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-noop-633"] [data-part="meta"]')).toContainText('writer');
    const noOpMeta = await page.locator('#dashRecentJobs article[data-job-id="job-noop-633"] [data-part="meta"]').innerText();
    expect((noOpMeta.match(/writer/g) || []).length).toBe(1);
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-noop-633"] .dash-maintenance-summary')).toContainText('No oversized files');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-noop-633"] .dash-maintenance-summary')).not.toContainText('Started:');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-recovery-633"] [data-part="outcome"]')).toContainText('Consolidation completed');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-recovery-633"] [data-part="outcome"]')).toContainText('Compaction incomplete');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-recovery-633"] [data-part="outcome"]')).toContainText('Recovery required');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-cancelled-633"] [data-part="outcome"]')).toContainText('Consolidation completed');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-cancelled-633"] [data-part="outcome"]')).toContainText('Compaction interrupted');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-failed-633"] [data-part="outcome"]')).toContainText('Consolidation failed');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-missing-633"] [data-part="outcome"]')).toContainText('Consolidation completed');
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-missing-633"] [data-part="outcome"]')).not.toContainText('No oversized files');
    const recovery = page.locator('#dashRecentJobs article[data-job-id="job-recovery-neutral-633"]');
    await expect(recovery.locator('.dash-maintenance-summary .status-dot').first()).toHaveClass(/error/);
    for (const [id, completed] of [['job-partial-633', 2], ['job-ambiguous-633', 0], ['job-consolidation-cancelled-633', null]]) {
        const row = page.locator(`#dashRecentJobs article[data-job-id="${id}"]`);
        await expect(row.locator('[data-part="status"] .status-dot')).toHaveClass(/error/);
        await expect(row.locator('.dash-job-summary')).toHaveText('Consolidation failed');
        await expect(row.locator('.dash-job-partial')).toHaveText('Partial result reported');
        await expect(row.locator('.dash-job-summary')).not.toHaveAttribute('role', 'alert');
        if (completed !== null) await expect(row.locator('.dash-job-result')).toHaveText(`${completed} of 4 batches reported completed`);
        else await expect(row.locator('.dash-job-result')).toHaveCount(0);
        await row.locator('.dash-job-details summary').click();
        await expect(row.locator('.server-msg-text')).toHaveText(id === 'job-consolidation-cancelled-633' ? jobs.consolidationCancelled.error : jobs.partial.error);
        await row.screenshot({ path: path.join(CAPTURES, `issue633-${id}.png`) });
        await row.locator('.dash-job-details summary').click();
    }
    await expect(page.locator('#dashRecentJobs .dash-job-summary[role="alert"]')).toHaveCount(0);
    await expect(page.locator('#dashRecentJobs article[data-job-id="job-failed-633"] .dash-job-partial, #dashRecentJobs article[data-job-id="job-failed-633"] .dash-job-result')).toHaveCount(0);
    await expect(page.locator('#dashRecentJobs img')).toHaveCount(0);
    const recoveryDetails = page.locator('#dashRecentJobs article[data-job-id="job-recovery-633"] .dash-job-details');
    await recoveryDetails.locator('summary').click();
    await expect(recoveryDetails).toContainText('Compaction incomplete');
    await expect(recoveryDetails).toContainText('Recovery must be checked before retrying.');
    await expect(recoveryDetails).toContainText(longDiagnostic);
    await expect(recoveryDetails).toContainText('Started:');
    await expect(recoveryDetails.locator('img')).toHaveCount(0);
    await recoveryDetails.locator('summary').click();
    const failedDetails = page.locator('#dashRecentJobs article[data-job-id="job-failed-633"] .dash-job-details');
    await failedDetails.locator('summary').click();
    await expect(failedDetails.locator('.server-msg-text')).toHaveText(longDiagnostic);
    await failedDetails.locator('summary').click();
    const detailsButton = page.locator('#dashRunningJobs article[data-job-id="job-running-633"] [data-action="dash-job"]');
    await expect(detailsButton).toBeVisible();
    expect(calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(1);

    const diagnosticDetails = page.locator('#dashRunningJobs article[data-job-id="job-running-633"] .dash-job-details');
    await expect(diagnosticDetails).toBeVisible();
    await expect(diagnosticDetails).not.toHaveAttribute('open', '');
    await detailsButton.focus();
    await page.keyboard.press('Tab');
    await expect(diagnosticDetails.locator('summary')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(diagnosticDetails).toHaveAttribute('open', '');
    await expect(diagnosticDetails).toContainText('bank_consolidation_status');
    await expect(diagnosticDetails.locator('.server-msg-text')).toHaveText(longDiagnostic);
    await expect(diagnosticDetails.locator('img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__issue633Xss)).toBe(0);
    await page.keyboard.press('Enter');
    await expect(diagnosticDetails).not.toHaveAttribute('open', '');
    await diagnosticDetails.locator('summary').evaluate(el => el.blur());

    for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
        await page.setViewportSize({ width, height });
        await page.evaluate(() => { document.documentElement.style.zoom = '100%'; });
        if (width === 390) {
            const summaryBox = await diagnosticDetails.locator('summary').boundingBox();
            expect(summaryBox.height).toBeGreaterThanOrEqual(44);
            await diagnosticDetails.locator('summary').click();
            await expect(diagnosticDetails).toHaveAttribute('open', '');
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'long diagnostic overflows at mobile width').toBe(true);
            await diagnosticDetails.locator('summary').click();
            await expect(diagnosticDetails).not.toHaveAttribute('open', '');
            await diagnosticDetails.locator('summary').evaluate(el => el.blur());
        }
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1), `horizontal overflow at ${width}px`).toBe(true);
        await page.screenshot({ path: path.join(CAPTURES, `issue633-dashboard-${width}x${height}.png`), fullPage: true });
        if (width === 390) {
            await page.locator('#dashRecentJobs article[data-job-id="job-noop-633"]').scrollIntoViewIfNeeded();
            await page.screenshot({ path: path.join(CAPTURES, 'issue633-dashboard-390x844-recent-jobs.png') });
        }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => { document.documentElement.style.zoom = '200%'; });
    expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'horizontal overflow at 200% zoom').toBe(true);
    await page.screenshot({ path: path.join(CAPTURES, 'issue633-dashboard-1440x900-200-percent.png'), fullPage: true });
    expect(errors).toEqual([]);
});

function recentSpaces() {
    const names = ['commerce-platform', 'design-system', 'agent-runtime', 'customer-research', 'release-engineering',
        'product-strategy', 'knowledge-base', 'operations', 'api-reference', 'service-quality', 'onboarding', 'security'];
    return [
        { space_id: 'never-consolidated', description: 'New project, waiting for its first synthesis' },
        ...names.map((space_id, index) => ({ space_id, description: ['Product decisions and implementation context',
            'Shared patterns, research and lessons learned', 'Context inherited by the next agent'][index % 3],
            last_consolidation: new Date(Date.UTC(2026, 8, 30, 8, 10 - index)).toISOString(),
            consolidation_count: 47 - index, total_notes_processed: 824 - index * 31 })),
        { space_id: 'invalid-date', last_consolidation: '2026-02-30T00:00:00Z' },
    ].reverse();
}

async function overviewFixture(page, spaces, permissions = ['read', 'write', 'manage'], fail = false) {
    const calls = [];
    await page.clock.install({ time: new Date('2026-09-30T08:20:00Z') });
    await page.route('**/api/spaces', route => json(route, { status: 'ok', spaces }));
    await page.route('**/health', route => json(route, { version: 'issue-633-fixture' }));
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON(); calls.push(req);
        if (req.tool === 'system_whoami') return json(route, { status: 'ok', client_name: 'product-team', auth_type: 'stored', permissions, allowed_resources: spaces.map(space => space.space_id) });
        if (req.tool === 'space_list') return json(route, fail ? { status: 'error', message: 'Metadata temporarily unavailable' } : { status: 'ok', spaces, total: spaces.length });
        if (req.tool === 'bank_consolidation_queues') return json(route, { status: 'ok', lanes: req.arguments.space_ids.split(',').map(space_id => ({
            space_id, lane_state: space_id === 'commerce-platform' ? 'running' : 'idle', queued_jobs: [],
            running_job: space_id === 'commerce-platform' ? { job_id: 'current-product', status: 'running',
                started_at: '2026-09-30T08:18:00Z', scope_label: 'All agents', requested_by: 'product-team',
                progress: { notes_done: 6, notes_total: 18 } } : null,
            latest_jobs: space_id === 'commerce-platform' ? [{ job_id: 'previous-product', status: 'succeeded',
                finished_at: '2026-09-30T08:10:00Z', scope_label: 'All agents', result: { notes_processed: 12 } }] : [],
        })) });
        return json(route, { status: 'error', message: `Unexpected ${req.tool}` });
    });
    return calls;
}

test('Recent consolidation composition ranks the full snapshot, fits 6/4/2 and preserves card focus', async ({ page }) => {
    const spaces = recentSpaces(); const calls = await overviewFixture(page, spaces);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${origin}/admin.html#/dashboard`);
    const cards = page.locator('#dashRecentSpaces article');
    for (const [width, height, count] of [[1440, 900, 6], [1440, 800, 6], [768, 1024, 4], [390, 844, 2]]) {
        await page.setViewportSize({ width, height });
        await expect(cards).toHaveCount(count);
        await expect.poll(() => calls.filter(call => call.tool === 'bank_consolidation_queues').at(-1).arguments.space_ids)
            .toBe((await cards.evaluateAll(items => items.map(item => item.dataset.space))).join(','));
        await expect(cards.first()).toHaveAttribute('data-space', 'commerce-platform');
        await expect(cards.first()).toContainText('Latest consolidation');
        await expect(cards.first()).toContainText('Lifetime totals');
        await expect(cards.first()).toContainText('824');
        await expect(page.locator('#dashLatestSignal')).toContainText('commerce-platform');
        await expect(page.locator('[data-action="dash-next-page"]')).toHaveCount(0);
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        const firstCard = await cards.first().boundingBox();
        expect(firstCard.y).toBeLessThan(height - 100, 'first screen exposes real consolidation activity');
        if (width === 1440 && height === 900) {
            const lastCard = await cards.last().boundingBox();
            expect(lastCard.y + lastCard.height).toBeLessThanOrEqual(height + 40, 'desktop selection fits the available first screen');
        }
        await page.screenshot({ path: path.join(CAPTURES, `issue633-recent-spaces-${width}x${height}.png`) });
    }
    await expect(page.locator('#dashLatestSignal .mono-data')).toHaveCSS('color', 'rgb(163, 176, 195)');
    await expect(page.locator('#dashLatestSignal .unit-timezone')).toHaveCSS('color', 'rgb(163, 176, 195)');
    const link = cards.first().getByRole('link', { name: 'commerce-platform', exact: true });
    await link.focus();
    spaces.find(space => space.space_id === 'commerce-platform').total_notes_processed = 825;
    await page.evaluate(() => PortalRefresh.refresh());
    await expect(link).toBeFocused();
    await expect(cards.first()).toContainText('825');
    expect(calls.filter(call => call.tool === 'space_list').every(call => call.arguments.include_counts === false)).toBe(true);
    expect(calls.filter(call => ['space_info', 'system_health', 'bank_consolidation_status'].includes(call.tool))).toHaveLength(0);
});

for (const state of ['manager-empty', 'reader-empty', 'never-consolidated', 'unavailable']) {
    test(`Dashboard has a composed permission-safe ${state} state`, async ({ page }) => {
        const spaces = state === 'never-consolidated' ? [{ space_id: 'new-project', description: 'Shared context' }] : [];
        const calls = await overviewFixture(page, spaces, state === 'manager-empty' ? ['read', 'write', 'manage'] : ['read'], state === 'unavailable');
        await page.setViewportSize({ width: 390, height: 844 });
        await page.goto(`${origin}/admin.html#/dashboard`);
        const empty = page.locator('#dashSpacesEmpty');
        await expect(empty).toContainText(state === 'never-consolidated' ? 'No consolidations yet' : state === 'unavailable' ? 'unavailable' : 'No spaces available');
        await expect(page.locator('#dashRecentSpaces article')).toHaveCount(0);
        await expect(empty.getByRole('link', { name: 'Create a space', exact: true })).toHaveCount(state === 'manager-empty' ? 1 : 0);
        if (state === 'reader-empty') await expect(empty).toContainText('Ask a manager');
        if (state === 'never-consolidated') await expect(empty.getByRole('link', { name: 'Explore spaces' })).toBeVisible();
        expect(calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(0);
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        await page.screenshot({ path: path.join(CAPTURES, `issue633-${state}-390x844.png`) });
    });
}

test('Dashboard missing dates with positive lifetime counters never claims first consolidation', async ({ page }) => {
    const spaces = [{ space_id: 'undated-history', consolidation_count: 4, last_consolidation: null }];
    const calls = await overviewFixture(page, spaces);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${origin}/admin.html#/dashboard`);
    for (const payload of [
        { space_id: 'undated-history', consolidation_count: 4, last_consolidation: null },
        { space_id: 'undated-history', total_notes_processed: 24 },
    ]) {
        spaces.splice(0, spaces.length, payload);
        await page.evaluate(() => PortalRefresh.refresh());
        await expect(page.locator('#dashSpacesEmpty')).toContainText('Consolidation dates unavailable');
        await expect(page.locator('#dashLatestSignal')).toContainText('Consolidation dates unavailable');
        await expect(page.locator('#dashSpacesEmpty')).not.toContainText('No consolidations yet');
        await expect(page.locator('#dashLatestSignal')).not.toContainText('first consolidation');
        await expect(page.locator('#dashRecentSpaces article')).toHaveCount(0);
        await expect(page.locator('#dashSpacesEmpty').getByRole('link', { name: 'Explore spaces' })).toBeVisible();
    }
    expect(calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(0);
    expect(calls.every(call => ['system_whoami', 'space_list'].includes(call.tool))).toBe(true);
    expect(calls.filter(call => call.tool === 'space_list').every(call => call.arguments.include_counts === false)).toBe(true);
});

test('Dashboard retains dated inventory after errors and excludes overreturned queue jobs', async ({ page }) => {
    const spaces = recentSpaces(); const calls = await overviewFixture(page, spaces);
    await page.goto(`${origin}/admin.html#/dashboard`);
    await expect(page.locator('#dashRecentSpaces article').first()).toContainText('commerce-platform');
    const ids = ['dashLatestSignal', 'dashSpacesTile', 'dashSpacesEmpty'];
    const snapshots = await page.evaluate(ids => ids.map(id => document.getElementById(id).innerHTML), ids);
    const stamp = await page.locator('#dashInventoryFreshness').innerText();
    await page.route('**/api/tool', async route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'space_list') return json(route, { status: 'error', message: 'Metadata temporarily unavailable' });
        if (req.tool === 'bank_consolidation_queues') return json(route, { status: 'ok', lanes: [
            ...req.arguments.space_ids.split(',').map(space_id => ({ space_id, lane_state: 'idle', queued_jobs: [], latest_jobs: [] })),
            { space_id: 'outside-scope', lane_state: 'running', running_job: { job_id: 'outside-job', status: 'running' },
                latest_jobs: [{ job_id: 'outside-history', status: 'failed' }] },
        ] });
        return route.fallback();
    });
    expect(await page.evaluate(() => PortalRefresh.refresh().then(() => '', error => error.message))).toBe('Metadata temporarily unavailable');
    await expect(page.locator('#dashInventoryFreshness')).toContainText('Metadata temporarily unavailable');
    expect(await page.evaluate(ids => ids.map(id => document.getElementById(id).innerHTML), ids)).toEqual(snapshots);
    await expect(page.locator('#dashInventoryFreshness')).toContainText(stamp);
    await expect(page.locator('#dashRecentSpaces article').first()).toContainText('commerce-platform');
    await expect(page.locator('#dashRunningJobs article, #dashRecentJobs article')).toHaveCount(0);
    await expect(page.locator('#dashActivityPanel')).not.toContainText('outside-scope');
    expect(calls.filter(call => call.tool === 'bank_consolidation_status')).toHaveLength(0);
});
