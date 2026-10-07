/** Issue #631 visual and behavior proof against the real local admin bundle. */
import { test, expect } from '@playwright/test';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

test.use({ viewport: { width: 1440, height: 900 } });

const ROOT = path.resolve(process.env.PORTAL_QA_631_STATIC_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/live_mem/static'));
const CAPTURES = process.env.PORTAL_QA_631_CAPTURE_DIR || '/tmp/portal-qa-631-captures';
const BASELINE_MODE = process.env.PORTAL_QA_631_BASELINE === '1';
const TYPES = {
    'Data Store': 12,
    ['Long entity type '.repeat(9)]: 3,
    '<img src=x onerror="window.__qaXss=1">': 1,
};
let server, origin;

test.beforeAll(async () => {
    server = http.createServer((req, res) => {
        const pathname = new URL(req.url, 'http://127.0.0.1').pathname;
        const relative = pathname === '/' ? 'admin.html' : pathname.replace(/^\/(?:static\/)?/, '');
        const target = path.resolve(ROOT, relative);
        if (!target.startsWith(ROOT + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
            res.writeHead(404); res.end('not found'); return;
        }
        const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
        res.writeHead(200, { 'content-type': contentTypes[path.extname(target)] || 'application/octet-stream' });
        fs.createReadStream(target).pipe(res);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
    fs.mkdirSync(CAPTURES, { recursive: true });
});

test.afterAll(async () => {
    if (server) await new Promise(resolve => server.close(resolve));
});

const spaceInfo = {
    status: 'ok', space_id: 'demo', description: 'Issue 631 visual proof', owner: 'qa',
    hive_status_label: 'local_only', live: { notes_count: 0, total_size: 0 },
    bank: { files_count: 2, total_size: 128 }, consolidation_queue: { lane_state: 'idle', latest_jobs: [] },
};

function healthyStatus() {
    return {
        status: 'ok', connected: true, reachable: true, binding: 'embedded',
        config: { ontology: 'general' },
        graph_stats: { document_count: 4, entity_count: 16, relation_count: 9, entity_types: TYPES },
        mid_automation: { compaction_enabled: true, archive_enabled: false },
        mid_archive_projection: { pending: 2, oldest_at: '2026-09-29T07:45:00Z' },
        watermark: { bank_version: 8, commit_id: 'commit-8', term: 3, provenance: 'bank_push', recorded_at: '2026-09-29T08:00:00Z', flagged: false },
        push_count: 3, files_pushed: 6,
    };
}

async function openOverview(page, state, permissions = ['read', 'write', 'manage', 'admin'], panel = 'overview') {
    state.calls.length = 0;
    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/spaces') return route.fulfill({ json: { status: 'ok', spaces: [{ space_id: 'demo' }] } });
        if (url.pathname !== '/api/tool') return route.fulfill({ status: 404, body: '' });
        const req = route.request().postDataJSON();
        state.calls.push(req);
        if (req.tool === 'system_whoami') return route.fulfill({ json: { status: 'ok', client_name: 'issue-631', permissions } });
        if (req.tool === 'space_info') return route.fulfill({ json: spaceInfo });
        if (req.tool === 'graph_status') return route.fulfill({ json: state.status });
        if (req.tool === 'ontology_list') return route.fulfill({ json: { status: 'ok', ontologies: [], count: 0 } });
        return route.fulfill({ json: { status: 'error', message: `Unexpected call: ${req.tool}` } });
    });
    await page.route('**/health', route => route.fulfill({ json: { version: 'local' } }));
    await page.goto(`${origin}/admin.html#/spaces/demo/long/${panel}`);
    await expect(page.locator(panel === 'ontology' ? '#sdOntologyConfig' : '#sdLongSnapshot')).not.toContainText('Loading long');
    await expect.poll(() => state.calls.filter(call => call.tool === 'graph_status').length).toBe(1);
}

async function scrollOverviewToTop(page) {
    await page.locator('#sdLongSnapshot').evaluate(el => {
        const content = document.querySelector('.content');
        content.scrollTop += el.getBoundingClientRect().top - content.getBoundingClientRect().top - 84;
    });
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`#631 LONG Overview is honest, responsive and keyboard usable at ${width}x${height}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        await page.addInitScript(() => { window.__qaXss = 0; });
        const state = { status: healthyStatus(), calls: [] };
        await openOverview(page, state);
        if (BASELINE_MODE) {
            await scrollOverviewToTop(page);
            await page.screenshot({ path: path.join(CAPTURES, `before-${width}x${height}.png`), fullPage: true });
            return;
        }

        const snapshot = page.locator('#sdLongSnapshot');
        await expect(snapshot).toContainText('Connected and reachable');
        await expect(snapshot).toContainText('Embedded long runtime');
        await expect(snapshot).toContainText('general');
        await expect(snapshot).toContainText('does not establish the schema');
        await expect(snapshot).toContainText('Graph documents');
        await expect(snapshot).toContainText('16');
        await expect(snapshot).toContainText('Relations');
        await expect(snapshot).toContainText('Data Store');
        await expect(snapshot).toContainText('Long entity type '.repeat(9));
        await expect(snapshot).toContainText('12');
        await expect(snapshot.locator('img')).toHaveCount(0);
        expect(await page.evaluate(() => window.__qaXss)).toBe(0);
        await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('Captures pending indexing');
        await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('2');
        await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('Oldest capture');
        await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('2026-09-29');
        await expect(snapshot).toContainText('Index current MID files');
        expect(state.calls.map(call => call.tool).sort()).toEqual(['graph_status', 'space_info', 'system_whoami']);
        expect(state.calls.filter(call => call.tool === 'graph_status').map(call => call.arguments)).toEqual([{ space_id: 'demo', include_graph: false }]);
        if (width === 768) expect(await page.locator('.sd-page-long').evaluate(el => getComputedStyle(el).paddingLeft)).toBe('20px');
        await scrollOverviewToTop(page);
        await page.screenshot({ path: path.join(CAPTURES, `after-${width}x${height}.png`), fullPage: true });
        if (width === 390) {
            await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
            await scrollOverviewToTop(page);
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
            const zoomOverflow = await page.locator('#sdLongSnapshot').evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth, nodes: [...el.querySelectorAll('*')].filter(node => node.scrollWidth > node.clientWidth + 1).map(node => ({ tag: node.tagName, class: node.className, width: node.clientWidth, scroll: node.scrollWidth, text: node.textContent.slice(0, 50) })) }));
            expect(zoomOverflow.scroll <= zoomOverflow.width + 1, JSON.stringify(zoomOverflow)).toBe(true);
            await page.screenshot({ path: path.join(CAPTURES, 'after-390x844-css-zoom-200.png'), fullPage: true });
            await page.evaluate(() => { document.documentElement.style.zoom = '1'; });
        }

        const tabs = page.getByRole('tablist', { name: 'Long memory panels' });
        const overview = tabs.getByRole('tab', { name: 'Overview', exact: true });
        await overview.focus();
        await page.keyboard.press('ArrowRight');
        await expect(tabs.getByRole('tab', { name: 'Ontology', exact: true })).toBeFocused();
        await expect(overview).toHaveAttribute('aria-selected', 'true');
        await page.keyboard.press('Enter');
        await expect(page).toHaveURL(/\/long\/ontology$/);
        await expect(page.locator('#sdOntologyConfig')).toContainText('general');

        await page.goto(`${origin}/admin.html#/spaces/demo/long/overview`);
        const metric = page.locator('#sdLongSnapshot');
        state.status = {
            status: 'ok', connected: true, config: {},
            mid_automation: { compaction_enabled: null, archive_enabled: true },
            mid_archive_projection: { pending: 0 },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText('reachability unknown');
        await expect(metric).toContainText('Configured ontology is not reported');
        await expect(metric).toContainText('Long statistics are unavailable.');
        await expect(metric.locator('.metric-card')).toHaveCount(0);
        const automation = page.getByRole('region', { name: 'MID to LONG automation' });
        await expect(automation).toContainText('0');
        await expect(automation).toContainText('A zero backlog does not mean every current MID file is indexed.');

        state.status = {
            status: 'ok', connected: true, reachable: true, binding: 'explicit', config: { ontology: 'declared-schema', url: 'https://user:pass@graph.example.invalid/v1?token=top-secret', memory_id: '<graph-main>' },
            graph_stats: { document_count: 0, entity_count: 0, relation_count: 0, entity_types: {} },
            mid_automation: { compaction_enabled: false, archive_enabled: false },
            mid_archive_projection: { pending: 0 },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText('Connected and reachable');
        await expect(metric).toContainText('Explicit Graph Memory runtime');
        await expect(metric).toContainText('https://graph.example.invalid/v1?token=%5Bredacted%5D');
        await expect(metric).toContainText('<graph-main>');
        await expect(metric).not.toContainText('top-secret');
        await expect(metric).not.toContainText('user:pass');
        await expect(metric).toContainText('declared-schema');
        await expect(metric.locator('img')).toHaveCount(0);
        await expect(metric).toContainText('0');

        state.status = {
            status: 'ok', connected: true, reachable: true, binding: 'future-binding',
            graph_stats: { entity_count: 0, entity_types: {} },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText('Unknown long binding — fail-closed');
        await expect(metric).not.toContainText('Connected and reachable');

        state.status = {
            status: 'ok', connected: false, bound: false, embedded: true,
            mid_automation: { compaction_enabled: true, archive_enabled: false },
            mid_archive_projection: { pending: 4, oldest_at: '2026-09-20T12:00:00Z' },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText('Unbound');
        await expect(metric).toContainText('Waiting for the first ingestion');
        await expect(automation).toContainText('4');
        await expect(metric).toContainText('Index current MID files');

        state.status = {
            status: 'ok', connected: true, reachable: false, binding: 'explicit',
            mid_automation: { compaction_enabled: false, archive_enabled: true },
            mid_archive_projection: { pending: 1 },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText(/unreachable/i);
        await expect(metric.locator('.metric-card')).toHaveCount(0);
        await expect(automation).toContainText('1');
        await expect(metric).toContainText('Index current MID files');

        state.status = {
            status: 'ok', connected: true, reachable: true, binding: 'embedded',
            graph_stats: { document_count: null, entity_count: -1, relation_count: 9007199254740992 },
            mid_automation: { compaction_enabled: false, archive_enabled: false },
            mid_archive_projection: { pending: null },
        };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(metric).toContainText('Unavailable');
        await expect(metric).toContainText(/reachable/i);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        expect(await page.locator('#sdLongSnapshot').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);

    });
}

test('#631 preserves LONG push permission and automation after a failed status read', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    if (BASELINE_MODE) return;
    const state = { status: healthyStatus(), calls: [] };
    await openOverview(page, state);
    const snapshot = page.locator('#sdLongSnapshot');
    const goodMarkup = await snapshot.innerHTML();
    const goodDate = await page.locator('#sdLongStatus > .mono-data').getAttribute('title');
    expect(goodDate).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    state.status = { status: 'read_only', message: '<status temporarily unavailable>', mid_automation: { compaction_enabled: true, archive_enabled: true }, mid_archive_projection: { pending: 3 } };
    await page.locator('[data-action="sd-long-refresh"]').click();
    await expect(page.locator('#sdLongStatus')).toContainText('<status temporarily unavailable>');
    expect(await snapshot.innerHTML()).toBe(goodMarkup);
    expect(await page.locator('#sdLongStatus > .mono-data').getAttribute('title')).toBe(goodDate);
    await expect(snapshot.locator('img')).toHaveCount(0);
    await expect(snapshot).toContainText('Index current MID files');
    await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('2');
    await page.screenshot({ path: path.join(CAPTURES, 'after-status-failure-automation.png'), fullPage: true });

    const readOnlyState = { status: healthyStatus(), calls: [] };
    const readOnlyPage = await page.context().newPage();
    await readOnlyPage.setViewportSize({ width: 1440, height: 900 });
    await openOverview(readOnlyPage, readOnlyState, ['read']);
    await expect(readOnlyPage.locator('#sdLongSnapshot')).not.toContainText('Index current MID files');
    expect(readOnlyState.calls.filter(call => call.tool === 'graph_status')).toHaveLength(1);
});

test('PR640 F2 no configured runtime keeps the fail-closed alert', async ({ page }) => {
    const state = { status: { status: 'ok', connected: false, message: 'No Graph Memory connection is configured' }, calls: [] };
    await openOverview(page, state);
    const health = page.getByRole('region', { name: 'LONG connection and configuration' });
    await expect(health.getByRole('alert')).toContainText('Long runtime unavailable');
    await expect(health.getByRole('alert')).toContainText('The required embedded long runtime is not configured for this space.');
    await expect(health).toContainText('Not configured');
    await expect(health).not.toContainText('Unknown');
    const healthBounds = await health.boundingBox();
    const bannerBounds = await health.locator('.sd-long-health-state--banner').boundingBox();
    expect(Math.abs(bannerBounds.width - healthBounds.width)).toBeLessThanOrEqual(2);
    await page.screenshot({ path: path.join(CAPTURES, 'after-not-configured.png'), fullPage: true });
});

for (const [name, payload] of [
    ['unreachable', { reachable: false }], // GraphBridge omits graph_stats on connection failure.
    ['stats-failed', { reachable: true, graph_stats: null }], // memory_stats failure returns null.
    ['stats-malformed', { reachable: true, graph_stats: [] }],
    ['reachability-unknown', {}],
]) {
    test(`PR640 F3 ${name} has one unavailable state without volume cards`, async ({ page }) => {
        const state = { status: { status: 'ok', connected: true, binding: 'embedded', ...payload,
            mid_automation: { compaction_enabled: true, archive_enabled: false }, mid_archive_projection: { pending: 2 } }, calls: [] };
        await openOverview(page, state);
        const snapshot = page.locator('#sdLongSnapshot');
        await expect(snapshot.locator('.metric-card')).toHaveCount(0);
        await expect(snapshot.locator('.sd-long-volume')).toHaveCount(0);
        await expect(snapshot).not.toContainText('Entities by type');
        await expect(snapshot.getByText('Long statistics are unavailable.', { exact: true })).toHaveCount(1);
        await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('2');
        await page.screenshot({ path: path.join(CAPTURES, `after-${name}.png`), fullPage: true });
    });
}

for (const panel of ['overview', 'ontology']) {
    test(`PR640 F4 initial ${panel} failure renders the error instead of absence`, async ({ page }) => {
        const state = { status: { status: 'error', message: '<recovery required>',
            mid_automation: { compaction_enabled: true, archive_enabled: false }, mid_archive_projection: { pending: 3 } }, calls: [] };
        await openOverview(page, state, ['admin'], panel);
        const region = page.locator(panel === 'ontology' ? '#sdOntologyConfig' : '#sdLongSnapshot');
        await expect(region).toContainText('<recovery required>');
        await expect(region).not.toContainText('Configured ontology is not reported');
        await expect(region.locator('img')).toHaveCount(0);
        await expect(page.locator('#sdLongStatus > .mono-data')).toHaveCount(0);
        if (panel === 'overview') await expect(page.getByRole('region', { name: 'MID to LONG automation' })).toContainText('3');
        expect(state.calls.filter(call => call.tool === 'graph_status')).toHaveLength(1);
        await page.screenshot({ path: path.join(CAPTURES, `after-initial-${panel}-error.png`), fullPage: true });
    });
}

test('PR640 F6 alternate credential queries are redacted and config text stays escaped', async ({ page }) => {
    const keys = ['sig', 'signature', 'X-Amz-Signature', 'key', 'auth', 'token', 'ordinary'];
    const secrets = keys.map(() => crypto.randomUUID());
    const endpoint = new URL('https://graph.example.invalid/v1');
    endpoint.username = crypto.randomUUID(); endpoint.password = crypto.randomUUID();
    keys.forEach((key, index) => endpoint.searchParams.append(key, secrets[index]));
    secrets.push(endpoint.username, endpoint.password);
    const state = { status: { ...healthyStatus(), binding: 'explicit', config: { url: endpoint.href,
        memory_id: '<img src=x onerror="window.__qaXss=1">', ontology: '<img src=x>' } }, calls: [] };
    await openOverview(page, state);
    const health = page.getByRole('region', { name: 'LONG connection and configuration' });
    const text = await health.textContent();
    expect(secrets.some(value => text.includes(value)), 'query/userinfo values must not render').toBe(false);
    await expect(health).toContainText('https://graph.example.invalid/v1');
    await expect(health).toContainText('<img src=x>');
    await expect(health.locator('img')).toHaveCount(0);
});
