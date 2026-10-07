/**
 * Post-RC1 visual-QA proof against the real admin bundle and real shell.
 * Every network response is controlled, while HTML/CSS/JS/vendor assets are
 * served unmodified from src/live_mem/static.
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://admin-visual-qa.e2e';

function compactReport(dryRun, overrides = {}) {
    return {
        status: 'ok', space_id: 'demo', dry_run: dryRun,
        files_total: 2, files_over_limit: 1,
        total_size_before: 80000, total_size_after: dryRun ? 80000 : 40000,
        ...(dryRun ? {} : { preimage_id: 'demo/visual-qa-preimage' }),
        files: [{
            filename: 'activeContext.md', size: 70000, max_size: 35000,
            source_sha256: 'a'.repeat(64), over_limit: true, ratio: 2,
            ...(dryRun ? {} : { result_sha256: 'b'.repeat(64), compacted_size: 30000, reduction_pct: 57.14 }),
        }, {
            filename: 'progress.md', size: 10000, max_size: 35000,
            source_sha256: 'c'.repeat(64), over_limit: false, ratio: 0.29,
        }],
        ...overrides,
    };
}

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.woff2': 'font/woff2',
};

function json(route, obj, status = 200) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(obj) });
}

function serveStatic(route, relPath) {
    const file = path.join(STATIC, relPath);
    if (!file.startsWith(STATIC) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        return route.fulfill({ status: 404, body: 'not found' });
    }
    return route.fulfill({
        status: 200,
        contentType: CONTENT_TYPE[path.extname(file)] || 'application/octet-stream',
        body: fs.readFileSync(file),
    });
}

function spaceInfo() {
    return {
        status: 'ok', space_id: 'demo', description: 'Visual QA space', owner: 'qa',
        created_at: '2026-07-17T10:00:00Z', hive_status_label: 'local_only',
        live: { notes_count: 0, total_size: 0 },
        bank: { files_count: 2, total_size: 160 },
        consolidation_count: 0, synthesis_exists: false,
        consolidation_queue: { lane_state: 'idle', latest_jobs: [], queued_job_ids: [] },
    };
}

function graphStatus(includeGraph) {
    const base = {
        status: 'ok', connected: true, reachable: true, binding: 'embedded',
        graph_stats: { document_count: 1, entity_count: 2, relation_count: 2 },
        watermark: null, push_count: 1, files_pushed: 1,
    };
    if (!includeGraph) return base;
    return {
        ...base,
        graph_view: {
            status: 'ok', node_count: 3, edge_count: 2,
            total_node_count: 3, total_edge_count: 2, truncated: false,
            nodes: [
                { id: 'n1', label: 'Hivemind', type: 'Product', description: 'Unified agent memory', mentions: 9, node_type: 'entity' },
                { id: 'n2', label: 'Project Mesh', type: 'Protocol', description: 'Full-mesh coordination', mentions: 5, node_type: 'entity' },
                { id: 'n3', label: 'Architecture.md', filename: 'Architecture.md', type: 'Document', description: '', mentions: 0, node_type: 'document' },
            ],
            edges: [
                { id: 'e1', from: 'n1', to: 'n2', type: 'USES', description: '', weight: 1 },
                { id: 'e2', from: 'n3', to: 'n1', type: 'MENTIONS', description: '', weight: 1 },
            ],
        },
    };
}

async function routeConsole(page, { createToken = false, meshSource = null, compactResponse = null, graphResponse = null, permissions = ['read', 'write', 'manage', 'admin'], spaceData = null, accessTokens = null, rules = null, midContent = null } = {}) {
    const state = { calls: [], meshCalls: [] };
    await page.addInitScript(() => { window.__visualQaXss = 0; });
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        const p = url.pathname;
        if (p === '/health') return json(route, { version: 'visual-qa' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (p === '/api/admin/mesh/availability') {
            state.meshCalls.push({ method: route.request().method(), path: p });
            if (!meshSource) return route.fulfill({ status: 404, body: '' });
            return json(route, { status: 'ok' });
        }
        if (p === '/api/admin/mesh/status') {
            state.meshCalls.push({ method: route.request().method(), path: p });
            if (!meshSource) return route.fulfill({ status: 404, body: '' });
            return json(route, {
                status: 'ok', instance_id: 'visual-qa', display_name: 'Visual QA',
                public_url: 'https://mesh.invalid', fingerprint: 'visual-qa-fingerprint',
                pairings: [], source_readiness: [meshSource], eligible_spaces: [],
            });
        }
        if (p === '/api/admin/mesh/source-readiness/demo') {
            state.meshCalls.push({ method: route.request().method(), path: p });
            if (!meshSource) return route.fulfill({ status: 404, body: '' });
            return json(route, { status: 'ok', source: meshSource });
        }
        if (p === '/api/tool') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.calls.push(body);
            switch (body.tool) {
            case 'system_health': return json(route, {
                status: 'healthy', version: 'visual-qa', spaces_count: 1, uptime_seconds: 3600,
                services: { s3: { status: 'ok' }, llmaas: { status: 'ok' } },
            });
            case 'bank_consolidation_queues': return json(route, {
                status: 'ok', total_spaces: 1, active_spaces: 0, running_spaces: 0, queued_jobs: 0, failed_recent: 0,
                parallelism_model: 'one_worker_per_space', service_config: { batch_size: 3 },
                lanes: [{ space_id: 'demo', lane_state: 'idle', queued_count: 0, latest_jobs: Array.from({ length: 12 }, (_, index) => ({
                    job_id: `job-${index}`, space_id: 'demo', status: 'succeeded',
                    finished_at: `2026-09-09T12:${String(index).padStart(2, '0')}:00Z`,
                })) }],
            });
            case 'system_whoami': return json(route, {
                status: 'ok', client_name: 'visual-qa-admin', auth_type: 'stored', token_hash: 'current-hash',
                permissions,
            });
            case 'space_info': return json(route, spaceData || spaceInfo());
            case 'live_read': return json(route, { status: 'ok', notes: [], total: 0 });
            case 'bank_list': return json(route, {
                status: 'ok', file_count: 2,
                files: [
                    { filename: 'activeContext.md', size: 80, last_modified: '2026-07-17T10:00:00Z' },
                    { filename: 'progress.md', size: 80, last_modified: '2026-07-17T11:00:00Z' },
                ],
            });
            case 'bank_read': return json(route, {
                status: 'ok', filename: body.arguments.filename, size: 80,
                content: midContent ?? (body.arguments.filename === 'activeContext.md'
                    ? '# Current focus\n\n**Ready for visual QA.**'
                    : '# Progress\n\nSecond file selected.'),
            });
            case 'bank_compact': {
                const dryRun = body.arguments.dry_run === true;
                if (compactResponse) return json(route, await compactResponse(body.arguments));
                return json(route, dryRun ? {
                    status: 'ok', space_id: 'demo', dry_run: true,
                    files_total: 2, files_over_limit: 1,
                    total_size_before: 160, total_size_after: 160,
                    files: [{
                        filename: 'activeContext.md', size: 80, max_size: 64,
                        source_sha256: 'a'.repeat(64), over_limit: true, ratio: 1.25,
                    }],
                } : {
                    status: 'ok', space_id: 'demo', dry_run: false,
                    files_total: 2, files_over_limit: 1,
                    total_size_before: 160, total_size_after: 96,
                    preimage_id: 'demo/visual-qa-preimage',
                    files: [{
                        filename: 'activeContext.md', size: 80, max_size: 64,
                        source_sha256: 'a'.repeat(64), result_sha256: 'b'.repeat(64),
                        over_limit: true, ratio: 1.25, compacted_size: 16, reduction_pct: 80,
                    }],
                });
            }
            case 'graph_status': return json(route, graphResponse || graphStatus(body.arguments.include_graph === true));
            case 'space_rules': return json(route, {
                status: 'ok',
                rules: rules ?? '## Consolidation rules\n\n- Keep the bank concise.\n\n<img src="x" onerror="window.__visualQaXss = 1">',
            });
            case 'backup_list': return json(route, { status: 'ok', backups: [] });
            case 'space_list': return json(route, { status: 'ok', total: 1, spaces: [{ space_id: 'demo', live_notes_count: 12, bank_files_count: 6, last_consolidation: '2026-09-30T08:00:00Z', consolidation_count: 5, total_notes_processed: 60 }] });
            case 'admin_list_tokens': return json(route, {
                status: 'ok', total: accessTokens === null ? 4 : accessTokens.length, tokens: accessTokens || [
                    { name: 'internal-long', hash: 'sha256:' + '1'.repeat(64), permissions: ['read', 'write'], space_ids: ['demo'], revoked: false },
                    { name: 'wide-agent', hash: 'sha256:' + '2'.repeat(64), permissions: ['write', 'read'], space_ids: ['demo', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot'], email: 'wide-agent-with-a-deliberately-long-owner-address@example.invalid', revoked: false },
                    { name: 'admin-agent', hash: 'sha256:' + '3'.repeat(64), permissions: ['read', 'write', 'manage', 'admin'], space_ids: [], revoked: false },
                    { name: 'revoked-agent', hash: 'sha256:' + '4'.repeat(64), permissions: ['read'], space_ids: ['demo'], revoked: true },
                ],
            });
            case 'admin_create_token':
                if (!createToken) return json(route, { status: 'error', message: 'unexpected create' });
                return json(route, {
                    status: 'created', name: body.arguments.name, token: 'ONE-TIME-PLAINTEXT',
                    token_hash: 'sha256:' + 'a'.repeat(64), permissions: ['read', 'write'],
                    space_ids: ['demo', 'bravo'], snapshot_taken: true,
                    warning: 'Avertissement serveur à ne pas afficher',
                    info: 'Instantané de deux espaces',
                });
            case 'admin_delete_token': return json(route, { status: 'deleted', message: 'Token deleted' });
            default: return json(route, { status: 'error', message: 'unexpected tool ' + body.tool });
            }
        }
        if (p === '/admin.html' || p === '/') return serveStatic(route, 'admin.html');
        if (p.startsWith('/static/')) return serveStatic(route, p.slice('/static/'.length));
        return route.fulfill({ status: 404, body: '' });
    });
    return state;
}

for (const width of [1934, 1440, 1024, 768, 390]) {
test(`space sections keep their panels inside the content frame at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await routeConsole(page, {
        rules: '## Consolidation rules\n\nKeep file hierarchy visible.\n\n```text\nprojectbrief.md ' + 'long-code-line-'.repeat(32) + '\n```',
        accessTokens: Array.from({ length: 7 }, (_, index) => ({
            name: `qa-agent-${index}-${'x'.repeat(256)}`,
            permissions: index === 2 ? ['read', 'write'] : ['read', 'write', 'manage'],
            space_ids: ['demo'], revoked: index === 2,
        })),
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page).toHaveURL(/#\/spaces\/demo\/memory\/mid$/);
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    expect(state.calls.filter(call => !['system_whoami'].includes(call.tool)).map(call => call.tool)).toEqual(['space_info', 'bank_list', 'bank_read']);
    const audit = await page.getByRole('link', { name: 'Global audit', exact: true }).boundingBox();
    expect(audit.height).toBeGreaterThanOrEqual(44);
    for (const [section, selector, expectedTools] of [
        ['Rules', '#sdRulesPanel pre', ['space_info', 'space_rules']],
        ['Access', '#sdAccessPanel .table-scroll', ['space_info', 'admin_list_tokens']],
        ['Backups', '#opBackupsList', ['space_info', 'backup_list']],
        ['Maintenance', '#sdMaintenanceTools', ['space_info']],
    ]) {
        const beforeSection = state.calls.length;
        await page.getByRole('tab', { name: section, exact: true }).click();
        await expect(page.locator(selector)).toBeVisible();
        await expect.poll(() => state.calls.slice(beforeSection).map(call => call.tool)).toEqual(expectedTools);
        if (section === 'Rules') await expect(page.locator(selector)).toContainText('projectbrief.md');
        if (section === 'Access') await expect(page.locator('#sdAccessPanel tbody tr')).toHaveCount(7);
        if (section === 'Maintenance') {
            await expect(page.locator('#opMaintSpace')).toHaveValue('demo');
            await expect(page.locator('#opMaintSpace')).toBeDisabled();
            await expect(page.locator('#opMaintSpace option')).toHaveText(['— select a space —', 'demo']);
            expect(state.calls.filter(call => call.tool === 'space_list')).toHaveLength(0);
        }
        await page.evaluate(() => document.fonts.ready);
        const layout = await page.evaluate(() => {
            const content = document.querySelector('.content');
            const frame = document.querySelector('#sdSpacePanel').getBoundingClientRect();
            return {
                viewport: innerWidth, document: document.documentElement.scrollWidth,
                content: content.clientWidth, contentScroll: content.scrollWidth,
                left: frame.left, right: frame.right,
                panels: [...document.querySelectorAll('#sdAuxiliary .panel')].map(panel => {
                    const rect = panel.getBoundingClientRect(); return { left: rect.left, right: rect.right };
                }),
            };
        });
        expect(layout.document).toBeLessThanOrEqual(layout.viewport);
        expect(layout.contentScroll).toBeLessThanOrEqual(layout.content);
        expect(layout.panels.length).toBeGreaterThan(0);
        for (const panel of layout.panels) {
            expect(panel.left).toBeGreaterThanOrEqual(layout.left - 1);
            expect(panel.right).toBeLessThanOrEqual(layout.right + 1);
        }
        if (section === 'Rules' || section === 'Access') {
            const scroll = await page.locator(selector).evaluate(el => { el.scrollLeft = el.scrollWidth; const end = el.scrollLeft; el.scrollLeft = 0; return end; });
            expect(scroll).toBeGreaterThan(0);
        }
        await page.screenshot({ path: testInfo.outputPath(`space-${section.toLowerCase()}-${width}.png`) });
    }
});
}

test('space detail is a clean Markdown reader with lazy graph exploration and simplified deletion', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);

    await expect(page.getByRole('heading', { name: 'Memory Bank' })).toBeVisible();
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await page.getByRole('button', { name: 'Check files for compaction' }).click();
    await expect(page.getByRole('heading', { name: 'Files eligible for compaction' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Compact files…' })).toBeVisible();
    await page.getByRole('button', { name: 'Compact files…' }).click();
    await expect(page.getByRole('heading', { name: 'Compact files' })).toBeVisible();
    await expect(page.locator('#adminModal')).toContainText('demo');
    expect(state.calls.some(call => call.tool === 'bank_compact' && call.arguments.dry_run === false)).toBe(false);
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByRole('heading', { name: 'Compaction applied' })).toBeVisible();
    await expect(page.locator('#sdCompactionResults')).toContainText('Preimage reference');
    expect(state.calls.some(call => call.tool === 'bank_compact' && call.arguments.dry_run === true)).toBe(true);
    expect(state.calls.some(call => call.tool === 'bank_compact' && call.arguments.dry_run === false)).toBe(true);
    expect(state.calls.filter(call => call.tool === 'bank_list').length).toBeGreaterThanOrEqual(2);
    expect(state.calls.filter(call => call.tool === 'bank_read')[0].arguments.filename).toBe('activeContext.md');
    await expect(page.locator('[data-action="sd-confirm-bank-delete"]')).toHaveCount(0);

    await page.getByRole('tablist', { name: 'Memory Bank files', exact: true }).getByRole('tab', { name: 'progress.md', exact: true }).click();
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Progress');
    await page.getByRole('tab', { name: 'Rules', exact: true }).click();
    await expect(page.locator('#sdRulesPanel h2')).toHaveText('Consolidation rules');
    await expect(page.locator('#sdRulesPanel img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
    await page.getByRole('button', { name: 'Edit rules' }).click();
    await expect(page.locator('#sdRulesInput')).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('tab', { name: 'Long memory', exact: true }).click();
    await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
    expect(state.calls.filter(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toHaveLength(0);
    await page.getByRole('tab', { name: 'Graph', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Graph explorer' })).toBeVisible();
    await expect(page.locator('.sd-graph-node')).toHaveCount(3);
    await page.getByRole('button', { name: 'Inspect Architecture.md' }).click();
    await expect(page.locator('#sdGraphDetails')).toContainText('Architecture.md');
    await expect(page.getByText('Top entities')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Disconnect binding' })).toHaveCount(0);
    expect(state.calls.some(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toBe(true);

    await page.getByRole('tab', { name: 'Access', exact: true }).click();
    await expect(page.locator('#sdAccessPanel')).not.toContainText('internal-long');
    await page.getByRole('tab', { name: 'Maintenance', exact: true }).click();
    await page.locator('.sd-danger-zone').getByRole('button', { name: 'Delete space' }).click();
    await expect(page.locator('.destructive-summary')).not.toContainText('Quiescence');
    await expect(page.locator('.typed-challenge')).toHaveText('"demo"');
    expect(await page.locator('.typed-challenge').evaluate(el => getComputedStyle(el).textTransform)).toBe('none');
    await page.locator('#destructiveConfirmInput').fill('Demo');
    await expect(page.locator('#modalConfirmBtn')).toBeDisabled();
    await page.locator('#destructiveConfirmInput').fill('demo');
    await expect(page.locator('#modalConfirmBtn')).toBeEnabled();
});

test('timestamps follow the browser timezone with a visible offset', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ timezoneId: 'Europe/Paris' });
    const page = await context.newPage();
    try {
        await routeConsole(page);
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
        await page.locator('.sd-metadata summary').click();
        const created = page.locator('.sd-kv').filter({ hasText: 'Created' });
        await expect(created).toContainText('2026-07-17 12:00 UTC+02:00');
        await expect(created.locator('.mono-data')).toHaveAttribute(
            'title',
            '2026-07-17T10:00:00Z · Europe/Paris (UTC+02:00)',
        );
        await page.screenshot({ path: testInfo.outputPath('timestamps-browser-timezone.png') });
    } finally {
        await context.close();
    }
});

// HM-AUD-07: use the real Markdown renderer, sanitizer, shell dispatcher and
// callTool transport. Only the API is controlled; no privileged operation runs.
const DOCUMENT_COMMAND = `<a href="#/spaces/demo/memory/mid" title="Document link" DATA-ACTION="run" data-tool="admin_update_token" data-args="{&quot;token_hash&quot;:&quot;attacker-fixture&quot;,&quot;permissions&quot;:&quot;admin&quot;}"><strong>Read document</strong></a>\n\n[Documentation](https://docs.example.invalid/guide)`;

for (const [surface, panel] of [['Rules', '#sdRulesPanel'], ['MID', '#sdBankPreview']]) {
    test(`Markdown ${surface} strips console commands and preserves ordinary links`, async ({ page }) => {
        const route = surface === 'Rules' ? 'rules' : 'memory/mid';
        const content = DOCUMENT_COMMAND.replace('memory/mid', route);
        const state = await routeConsole(page, { rules: content, midContent: content });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/${route}`);
        const document = page.locator(`${panel} .markdown-body`);
        const link = document.getByRole('link', { name: 'Read document' });
        await expect(link).toBeVisible();
        await expect(document.locator('[data-action], [data-tool], [data-args]')).toHaveCount(0);
        await expect(link).toHaveAttribute('href', '#/spaces/demo/' + route);
        await expect(link).toHaveAttribute('title', 'Document link');
        await page.waitForLoadState('networkidle');
        const before = state.calls.length;
        await link.locator('strong').click();
        await page.waitForLoadState('networkidle');
        expect(state.calls).toHaveLength(before);
        await link.press('Enter');
        await page.waitForLoadState('networkidle');
        expect(state.calls).toHaveLength(before);
        await expect(page.locator('#adminModal')).not.toBeVisible();
        await document.getByRole('link', { name: 'Documentation' }).click();
        await expect(page).toHaveURL('https://docs.example.invalid/guide');
    });

    test(`Markdown ${surface} cannot dispatch console commands even with restored data attributes`, async ({ page }) => {
        const route = surface === 'Rules' ? 'rules' : 'memory/mid';
        const content = DOCUMENT_COMMAND.replace('memory/mid', route);
        const state = await routeConsole(page, { rules: content, midContent: content });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/${route}`);
        const link = page.locator(`${panel} .markdown-body`).getByRole('link', { name: 'Read document' });
        await expect(link).toBeVisible();
        // Bypass only the sanitizer to test the independent event boundary.
        await link.evaluate(el => {
            el.dataset.action = 'run';
            el.dataset.tool = 'admin_update_token';
            el.dataset.args = JSON.stringify({ token_hash: 'attacker-fixture', permissions: 'admin' });
        });
        await page.waitForLoadState('networkidle');
        const before = state.calls.length;
        await link.locator('strong').click();
        await page.waitForLoadState('networkidle');
        expect(state.calls).toHaveLength(before);
        await link.press('Enter');
        await page.waitForLoadState('networkidle');
        expect(state.calls).toHaveLength(before);
        await expect(page.locator('#adminModal')).not.toBeVisible();

        // Another registered handler must not be reachable through a document.
        await link.evaluate(el => { el.dataset.action = 'sd-edit-rules'; });
        await link.click();
        await expect(page.locator('#sdRulesInput')).not.toBeVisible();
        // The actual console-created control remains usable with a confirmation
        // surface; document-origin rejection is not a blanket action shutdown.
        if (surface === 'MID') await page.getByRole('tab', { name: 'Rules', exact: true }).click();
        await expect(page.getByRole('button', { name: 'Edit rules' })).toBeVisible();
        await page.waitForLoadState('networkidle');
        const beforeControl = state.calls.length;
        await page.getByRole('button', { name: 'Edit rules' }).click();
        await expect(page.locator('#sdRulesInput')).toBeVisible();
        await page.getByRole('button', { name: 'Cancel' }).click();
        await expect(page.locator('#adminModal')).not.toBeVisible();
        expect(state.calls).toHaveLength(beforeControl);
    });
}

test('vendored sanitizer preserves admin Markdown formatting and rejects active content', async ({ page }) => {
    const content = '# Safe document\n\n[Guide](https://docs.example.invalid/guide "Guide title")\n\n'
        + '| Name | Value |\n| --- | --- |\n| Example | 42 |\n\n'
        + '```html\n<img src=x onerror="window.__visualQaXss = 1">\n```\n\n'
        + '<script>window.__visualQaXss = 1</script>'
        + '<img src=x onerror="window.__visualQaXss = 1">'
        + '<svg onload="window.__visualQaXss = 1"></svg>'
        + '<iframe srcdoc="<script>parent.__visualQaXss = 1</script>"></iframe>'
        + '<a href="javascript:window.__visualQaXss=1" onclick="window.__visualQaXss=1">Unsafe link</a>';
    await routeConsole(page, { rules: content, midContent: content });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    for (const panel of ['#sdBankPreview', '#sdRulesPanel']) {
        if (panel === '#sdRulesPanel') await page.getByRole('tab', { name: 'Rules', exact: true }).click();
        const document = page.locator(`${panel} .markdown-body`);
        await expect(document.getByRole('heading', { name: 'Safe document' })).toBeVisible();
        await expect(document.locator('table tbody td')).toHaveText(['Example', '42']);
        await expect(document.locator('pre code')).toHaveText('<img src=x onerror="window.__visualQaXss = 1">\n');
        await expect(document.getByRole('link', { name: 'Guide' })).toHaveAttribute('href', 'https://docs.example.invalid/guide');
        await expect(document.getByRole('link', { name: 'Guide' })).toHaveAttribute('title', 'Guide title');
        await expect(document.locator('script, img, svg, iframe, [onerror], [onload], [onclick]')).toHaveCount(0);
        await expect(document.getByText('Unsafe link', { exact: true })).not.toHaveAttribute('href');
    }
    expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
});

test('space detail hides manual compaction from a write-only session', async ({ page }) => {
    await routeConsole(page, { permissions: ['read', 'write'] });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);

    await expect(page.getByRole('heading', { name: 'Memory Bank' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Check files for compaction' })).toHaveCount(0);
    await expect(page.locator('[data-action="sd-compact-dry"]')).toHaveCount(0);
});

test('Space management tabs stay hidden from write-only sessions and direct Maintenance remains read-only', async ({ page }) => {
    const state = await routeConsole(page, { permissions: ['read', 'write'] });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/memory/mid`);
    await expect(page.getByRole('heading', { name: 'Memory Bank' })).toBeVisible();
    for (const name of ['Access', 'Maintenance']) await expect(page.getByRole('tab', { name, exact: true })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: 'Backups', exact: true })).toBeVisible();
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/maintenance`);
    await expect(page.locator('#sdSpacePanel')).toContainText('This section requires additional permission.');
    await expect(page.locator('#opMaintSpace')).toHaveCount(0);
    const reachable = page.getByRole('tablist', { name: 'Space sections' }).locator('[role="tab"][tabindex="0"]');
    await expect(reachable).toHaveCount(1);
    await reachable.focus(); await expect(reachable).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('tab', { name: 'Memory', exact: true })).toHaveAttribute('aria-selected', 'true');
    expect(state.calls.filter(call => ['admin_gc_notes', 'bank_repair', 'bank_compact'].includes(call.tool))).toHaveLength(0);
});

test('space detail offers preparation only from targeted Mesh readiness and delegates without mutating', async ({ page }) => {
    const meshSource = {
        space_id: 'demo', state: 'local_only_can_prepare', source_ready: false,
        source_initializable: true, can_create_invitation: false, resumable: false,
        reason_code: 'local_only_can_prepare',
        message: 'This local space can be prepared for Project Mesh.',
        state_token: 'a'.repeat(64),
    };
    const state = await routeConsole(page, { meshSource });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo`);

    const entry = page.locator('[data-mesh-source-action="prepare"]');
    await expect(entry).toHaveText('Prepare for Project Mesh');
    await expect(page.locator('#sdMeshReadiness')).toContainText(meshSource.message);
    await expect.poll(() => state.meshCalls.filter(
        call => call.path === '/api/admin/mesh/source-readiness/demo',
    ).length).toBe(1);
    expect(state.meshCalls.some(call => call.method === 'POST')).toBe(false);

    await entry.click();
    await expect(page).toHaveURL(/#\/mesh$/);
    await expect(page.getByRole('heading', { name: 'Project Mesh' })).toBeVisible();
    expect(state.meshCalls.some(call => call.method === 'POST')).toBe(false);
});

test('Access row menus target the selected token and expose honest lifecycle capabilities', async ({ page }) => {
    const state = await routeConsole(page, { createToken: true });
    await page.goto(`${ORIGIN}/admin.html#/access`);

    await expect(page.locator('#accessTable')).not.toContainText('internal-long');
    await expect(page.locator('#accessTable')).toContainText('+3 more');
    await expect(page.locator('#accessCount')).toHaveText('3');

    const wideRow = page.locator('#accessTable tbody tr').filter({ hasText: 'wide-agent' });
    const wideMenu = wideRow.locator('.row-action-menu');
    const wideTrigger = wideRow.getByLabel('Actions for token wide-agent');
    const actionPanel = page.locator('.row-action-menu[open] .row-action-menu-panel');
    const bodyPanels = page.locator('body > .row-action-menu-panel');
    await wideTrigger.focus();
    expect(await wideTrigger.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe('none');
    await wideTrigger.press('Enter');
    await page.keyboard.press('Tab');
    const editAction = actionPanel.getByRole('button', { name: /^Edit token/ });
    await expect(editAction).toBeFocused();
    expect(await editAction.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe('none');
    await page.keyboard.press('Escape');
    await expect(wideMenu).not.toHaveAttribute('open', '');
    await expect(wideTrigger).toBeFocused();
    await wideTrigger.click();
    await page.getByRole('heading', { name: 'Access' }).click();
    await expect(wideMenu).not.toHaveAttribute('open', '');

    await wideTrigger.click();
    await page.locator('#content').getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(page.locator('#accessCount')).toHaveText('3');
    await expect(bodyPanels).toHaveCount(0);
    await expect(page.locator('.row-action-menu-panel:visible')).toHaveCount(0);

    await wideTrigger.click();
    await page.evaluate(() => { location.hash = '#/dashboard'; });
    await expect(page.locator('#dashLanesPanel')).toBeVisible();
    await expect(bodyPanels).toHaveCount(0);
    await expect(page.locator('.row-action-menu-panel:visible')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/access'; });
    await expect(page.locator('#accessCount')).toHaveText('3');

    await page.setViewportSize({ width: 1150, height: 800 });
    expect(await page.locator('.access-token-table .table-scroll').evaluate(el => getComputedStyle(el).overflowX)).toBe('auto');
    await wideTrigger.click();
    await expect(bodyPanels).toHaveCount(0);
    await expect.poll(() => actionPanel.evaluate(
        el => el.getBoundingClientRect().right,
    )).toBeLessThanOrEqual(1150);
    const desktopPanel = await actionPanel.boundingBox();
    expect(desktopPanel).not.toBeNull();
    expect(desktopPanel.x).toBeGreaterThanOrEqual(0);
    expect(desktopPanel.x + desktopPanel.width).toBeLessThanOrEqual(1150);
    await page.keyboard.press('Escape');

    await page.setViewportSize({ width: 1000, height: 800 });
    await wideTrigger.click();
    await actionPanel.getByRole('button', { name: /^Close menu/ }).click();
    await expect(wideMenu).not.toHaveAttribute('open', '');
    await page.setViewportSize({ width: 1280, height: 800 });

    await wideTrigger.click();
    await actionPanel.getByRole('button', { name: /^Edit token/ }).click();
    await expect(page.getByRole('heading', { name: 'Edit token' })).toBeVisible();
    await expect(page.locator('.mono-block')).toContainText('wide-agent');
    await page.getByRole('button', { name: 'Cancel' }).click();

    await wideTrigger.click();
    await actionPanel.getByRole('button', { name: /^Create replacement/ }).click();
    await expect(page.getByRole('heading', { name: 'Replace token safely' })).toBeVisible();
    await expect(page.locator('#adminModal')).toContainText('The existing secret cannot be shown again');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.locator('#ctName')).toHaveValue('wide-agent-replacement');
    await expect(page.locator('#ctPerms')).toHaveValue('write,read');
    await expect(page.locator('#ctSpaces')).toHaveValue('demo, bravo, charlie, delta, echo, foxtrot');
    await page.getByRole('button', { name: 'Cancel' }).click();

    const revokedRow = page.locator('#accessTable tbody tr').filter({ hasText: 'revoked-agent' });
    await revokedRow.getByLabel('Actions for token revoked-agent').click();
    await expect(actionPanel.getByRole('button', { name: /^Reactivate token/ })).toHaveCount(0);
    await expect(actionPanel).toContainText('Revoked permanently');
    await actionPanel.getByRole('button', { name: /^Delete permanently/ }).click();
    await expect(page.getByRole('heading', { name: 'Delete token permanently' })).toBeVisible();
    await expect(page.locator('#adminModal')).toContainText('permanently deletes the revoked token');
    await page.getByRole('button', { name: 'Cancel' }).click();

    await wideTrigger.click();
    await actionPanel.getByRole('button', { name: /^Delete permanently/ }).click();
    await expect(page.locator('#adminModal')).toContainText('immediately invalidates');
    expect(state.calls.some(call => call.tool === 'admin_delete_token')).toBe(false);
    await page.locator('#adminModal').getByRole('button', { name: 'Delete permanently' }).click();
    await expect.poll(() => state.calls.find(call => call.tool === 'admin_delete_token')).toEqual({
        tool: 'admin_delete_token',
        arguments: { token_hash: 'sha256:' + '2'.repeat(64) },
    });

    await page.getByRole('button', { name: 'Create token' }).click();
    await expect(page.locator('#ctSpacesHint')).toContainText('New spaces are not added automatically.');
    await page.fill('#ctName', 'snapshot-agent');
    await page.locator('#modalConfirmBtn').click();

    await expect(page.locator('#ctSecret')).toHaveText('ONE-TIME-PLAINTEXT');
    await expect(page.getByText('Token ID (full hash)')).toBeVisible();
    await expect(page.getByText('A manager needs this ID')).toBeVisible();
    await expect(page.getByText('Access granted to 2 existing spaces.')).toBeVisible();
    await expect(page.locator('#adminModal')).not.toContainText('Avertissement serveur');
    await expect(page.locator('#adminModal')).not.toContainText('Instantané');
});


for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`memory-first hierarchy remains readable at ${viewport.width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport);
        const state = await routeConsole(page);
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
        await expect(page.locator('#loginOverlay')).toHaveCSS('opacity', '0');
        await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
        await page.evaluate(() => document.fonts.ready);
        const tier = await page.locator('#sdTierPanel').boundingBox();
        await expect(page.getByRole('heading', { name: 'Consolidation', exact: true })).toHaveCount(0);
        const sections = await page.getByRole('tablist', { name: 'Space sections' }).boundingBox();
        expect(tier.y).toBeGreaterThan(sections.y);
        if (viewport.width === 1440) expect(tier.y).toBeLessThan(viewport.height); // reader starts within the first screen
        if (viewport.width === 390) {
            const title = await page.getByRole('heading', { name: 'Memory Bank', exact: true }).boundingBox();
            const actions = await page.locator('.tier-mid .sd-tier-actions').boundingBox();
            expect(title.y + title.height).toBeLessThanOrEqual(actions.y);
        }
        expect(state.calls.some(call => call.tool === 'graph_status' && call.arguments.include_graph)).toBe(false);
        await expect(page.locator('.sd-metadata')).not.toHaveAttribute('open', '');
        await page.locator('.sd-metadata summary').click();
        await expect(page.locator('.sd-metadata')).toContainText('qa');
        await expect(page.locator('.sd-metadata')).toContainText('local_only');
        await page.locator('.sd-metadata summary').click();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`space-mid-${viewport.width}.png`), fullPage: true });

        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        await expect(page.locator('#loginOverlay')).toHaveCSS('opacity', '0');
        await expect(page.locator('#portalHealth')).toHaveText('Services not checked');
        await expect(page.locator('#dashHealthCard')).toContainText('Services not checked');
        await expect(page.locator('#dashHealthCard')).not.toContainText("Couldn't load system health");
        expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(0);
        await page.locator('#portalCheckServices').click();
        await expect(page.locator('#dashHealthCard')).toContainText('Healthy');
        await expect(page.locator('#dashIdentityCard')).toHaveCount(0);
        await expect(page.locator('#identityBlock')).toContainText('visual-qa-admin');
        await page.screenshot({ path: testInfo.outputPath(`dashboard-${viewport.width}.png`), fullPage: true });

        await page.goto(`${ORIGIN}/admin.html#/consolidation`);
        await expect(page.locator('#loginOverlay')).toHaveCSS('opacity', '0');
        await expect(page.getByRole('heading', { name: 'In progress', exact: true })).toBeVisible();
        await expect(page.getByRole('heading', { name: 'Recent history', exact: true })).toBeVisible();
        await expect(page.locator('.consol-metrics')).toHaveCount(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`consolidation-${viewport.width}.png`), fullPage: true });
    });
}

test('compact space overview keeps unknowns and safety warnings visible with long names and zoom', async ({ page }, testInfo) => {
    const spaceData = { ...spaceInfo(), owner: 'operator-with-a-long-name'.repeat(4), description: 'A-long-unbroken-space-description'.repeat(12), hive_status_label: 'resync_required', live: null, bank: null, consolidation_count: undefined, synthesis_exists: undefined, consolidation_queue: {} };
    await routeConsole(page, { spaceData });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('#loginOverlay')).toHaveCSS('opacity', '0');
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await expect(page.getByRole('alert').filter({ hasText: 'RESYNC REQUIRED' })).toBeVisible();
    await expect(page.locator('.sd-metrics')).toContainText('not recorded');
    await expect(page.locator('.sd-metrics .summary-stats__value')).toHaveText(['—', '—', '—']);
    await expect(page.locator('.sd-lane--idle')).toHaveCount(0);
    await page.locator('.sd-metadata summary').click();
    await expect(page.locator('.sd-meta-row')).toContainText(spaceData.owner);
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('space-unknown-zoom-200.png'), fullPage: true });
});

// #523: failure-proving checks against the real browser and shell.
test('accessibility: modal contains both Tab directions and isolates hidden login', async ({ page }) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/rules`);
    const edit = page.getByRole('button', { name: 'Edit rules' });
    await edit.click();
    const close = page.locator('#adminModal .modal-close');
    const confirm = page.locator('#modalConfirmBtn');
    await confirm.focus();
    await page.keyboard.press('Tab');
    await expect(close).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(confirm).toBeFocused();
    await page.locator('#loginToken').evaluate(el => el.focus());
    await expect(confirm).toBeFocused();
    await edit.evaluate(el => el.focus());
    await expect(confirm).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(edit).toBeFocused();
    await edit.click();
    await edit.evaluate(el => el.remove());
    await page.keyboard.press('Escape');
    await expect(page.locator('#content')).toBeFocused();
    await expect(page.locator('.app')).not.toHaveAttribute('inert', '');
});

test('accessibility: both Space tablists activate manually and normalize deep links without speculative Long reads', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page).toHaveURL(/#\/spaces\/demo\/memory\/mid$/);
    const memory = page.getByRole('tab', { name: 'Memory', exact: true });
    const mid = page.getByRole('tab', { name: 'mid', exact: true });
    const short = page.getByRole('tab', { name: 'short', exact: true });
    const long = page.getByRole('tab', { name: 'Long memory', exact: true });
    await expect(page.getByRole('tablist', { name: 'Memory tier' }).getByRole('tab')).toHaveCount(2);
    await mid.focus(); await page.keyboard.press('Enter');
    await expect(mid).toBeFocused();
    await expect(mid).toHaveAttribute('aria-controls', 'sdTierPanel');
    await expect(page.locator('#sdTierPanel')).toHaveAttribute('aria-labelledby', await mid.getAttribute('id'));
    await page.keyboard.press('Home'); await expect(short).toBeFocused();
    await expect(mid).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('ArrowLeft'); await expect(mid).toBeFocused();
    await page.keyboard.press('ArrowRight'); await expect(short).toBeFocused();
    await page.keyboard.press(' ');
    await expect(short).toBeFocused(); await expect(short).toHaveAttribute('aria-selected', 'true');
    await expect(page).toHaveURL(/#\/spaces\/demo\/memory\/short$/);
    await expect(page.locator('.sd-tier-tab[tabindex="0"]')).toHaveCount(1);

    await memory.focus(); await page.keyboard.press('End');
    await expect(page.getByRole('tab', { name: 'Maintenance', exact: true })).toBeFocused();
    await expect(memory).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('Home'); await expect(memory).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name: 'Active work', exact: true })).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name: 'Consolidation', exact: true })).toBeFocused();
    await page.keyboard.press('ArrowRight'); await expect(long).toBeFocused();
    expect(state.calls.filter(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toHaveLength(0);
    await page.keyboard.press(' ');
    await expect(long).toBeFocused(); await expect(long).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('#sdSpacePanel')).toHaveAttribute('aria-labelledby', await long.getAttribute('id'));
    await expect(page).toHaveURL(/#\/spaces\/demo\/long\/overview$/);
    await expect(page.getByRole('tablist', { name: 'Long memory panels' })).toBeVisible();
    await expect(page.locator('.sd-graph-node')).toHaveCount(0);
    expect(state.calls.filter(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toHaveLength(0);
    await expect(page.getByRole('tablist', { name: 'Memory tier' })).toHaveCount(0);
    await expect(page.locator('.sd-space-tab[tabindex="0"]')).toHaveCount(1);
    await page.keyboard.press('Home'); await expect(memory).toBeFocused();
    await page.keyboard.press('Enter'); await expect(memory).toBeFocused();
    await expect(page).toHaveURL(/#\/spaces\/demo\/memory\/short$/);
});

test('accessibility: drawer navigation, breakpoint and session changes preserve the active focus surface', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    const toggle = page.locator('#sidebarMenuToggle');
    await toggle.click();
    const live = page.locator('.nav-live');
    await live.focus();
    await page.keyboard.press('Tab');
    await expect(toggle).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(live).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(toggle).toBeFocused();
    await toggle.click();
    await page.locator('#sidebar a[data-nav="spaces"]').click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('#content')).toBeFocused();
    await toggle.click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('#sidebar a[aria-current="page"]')).toBeFocused();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar--drawer-open/);
    await toggle.click();
    await page.evaluate(() => showLogin('Session expired.'));
    const token = page.locator('#loginToken');
    const viewer = page.locator('#loginOverlay a[href="/live"]');
    await expect(viewer).toBeVisible();
    expect((await viewer.boundingBox()).height).toBeGreaterThanOrEqual(44);
    await expect(token).toBeFocused();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await page.keyboard.press('Shift+Tab');
    await expect(viewer).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(token).toBeFocused();
    await toggle.evaluate(el => el.focus());
    await expect(token).toBeFocused();
});

test('accessibility: mobile filters and token menus expose 44px targets', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/access`);
    const trigger = page.locator('.row-action-trigger').first();
    await expect(trigger).toBeVisible();
    const triggerBox = await trigger.boundingBox();
    expect(triggerBox.width).toBeGreaterThanOrEqual(44);
    expect(triggerBox.height).toBeGreaterThanOrEqual(44);
    await trigger.click();
    for (const item of await page.locator('.row-action-menu[open] .row-action-item:visible').all()) {
        const box = await item.boundingBox();
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(390.5);
    }
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    const search = page.getByRole('searchbox', { name: 'Search spaces' });
    await expect(search).toBeVisible();
    const box = await search.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.x + box.width).toBeLessThanOrEqual(390.5);
});

test('accessibility: attention text has at least 4.5 rendered contrast', async ({ page }) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/access`);
    await expect(page.locator('#accessTable')).toBeVisible();
    const contrast = await page.evaluate(() => {
        const cell = document.querySelector('#accessTable td');
        cell.insertAdjacentHTML('beforeend', pill('warn', 'Expired'));
        const element = cell.querySelector('.pill-warn');
        const rgba = text => text.match(/[\d.]+/g).map(Number);
        const compose = (front, back) => front.slice(0, 3).map((value, i) => value * (front[3] ?? 1) + back[i] * (1 - (front[3] ?? 1)));
        let background = [255, 255, 255];
        const ancestors = [];
        for (let node = element; node; node = node.parentElement) ancestors.unshift(node);
        for (const node of ancestors) background = compose(rgba(getComputedStyle(node).backgroundColor), background);
        const text = compose(rgba(getComputedStyle(element).color), background);
        const luminance = rgb => rgb.map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
        const l1 = luminance(text), l2 = luminance(background);
        return (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
});


test('accessibility: a dialog with every control disabled keeps a focusable fallback', async ({ page }) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/rules`);
    await page.getByRole('button', { name: 'Edit rules' }).click();
    await page.evaluate(() => {
        document.querySelectorAll('#adminModal button, #adminModal textarea').forEach(el => { el.disabled = true; });
    });
    for (const key of ['Tab', 'Shift+Tab']) {
        await page.keyboard.press(key);
        await expect(page.locator('#adminModal .modal-card')).toBeFocused();
    }
    await page.keyboard.press('Escape');
    await expect(page.locator('#adminModal')).toBeVisible();
});

test('accessibility: dialog output stays usable at desktop tablet and mobile widths', async ({ page }, testInfo) => {
    await routeConsole(page);
    for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
        await page.setViewportSize(viewport);
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/rules`);
        await page.getByRole('button', { name: 'Edit rules' }).click();
        await page.locator('#modalConfirmBtn').focus();
        await page.keyboard.press('Tab');
        await expect(page.locator('#adminModal .modal-close')).toBeFocused();
        const box = await page.locator('#adminModal .modal-card').boundingBox();
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
        await page.screenshot({ path: testInfo.outputPath(`focus-dialog-${viewport.width}.png`), animations: 'disabled' });
        await page.keyboard.press('Escape');
    }
});

test('accessibility: session expiry clears the modal and keyboard re-login focuses the restored view', async ({ page }) => {
    await routeConsole(page);
    await page.route('**/api/login', route => json(route, { status: 'ok' }));
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/rules`);
    await page.getByRole('button', { name: 'Edit rules' }).click();
    await page.route('**/api/tool', route => {
        const body = route.request().postDataJSON();
        return body.tool === 'space_list' ? json(route, {}, 401) : route.fallback();
    });
    await page.evaluate(() => callTool('space_list').catch(() => {}));
    await expect(page.locator('#adminModal')).toHaveCount(0);
    await expect(page.locator('#loginToken')).toBeFocused();
    await page.evaluate(() => {
        document.getElementById('loginToken').disabled = true;
        document.getElementById('loginBtn').disabled = true;
    });
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginOverlay a[href="/live"]')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.locator('#loginOverlay a[href="/live"]')).toBeFocused();
    await page.locator('#loginOverlay a[href="/live"]').evaluate(el => el.removeAttribute('href'));
    await page.keyboard.press('Tab');
    await expect(page.locator('.login-card')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.locator('.login-card')).toBeFocused();
    await page.evaluate(() => {
        document.getElementById('loginToken').disabled = false;
        document.getElementById('loginBtn').disabled = false;
        document.querySelector('#loginOverlay .login-footer a').setAttribute('href', '/live');
    });
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginToken')).toBeFocused();
    await page.keyboard.type('E2E-OPERATOR-TOKEN');
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginBtn')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#loginOverlay')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Edit rules' })).toBeVisible();
    await expect(page.locator('#content')).toBeFocused();
    await expect(page.locator('.app')).not.toHaveAttribute('inert', '');
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Back to spaces' })).toBeFocused();
});

test('accessibility: successful keyboard sign-in focuses the requested view', async ({ page }) => {
    await routeConsole(page);
    await page.route('**/api/spaces', route => json(route, {}, 401));
    await page.route('**/api/login', route => json(route, { status: 'ok' }));
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('#loginToken')).toBeFocused();
    await page.keyboard.type('E2E-OPERATOR-TOKEN');
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginBtn')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await expect(page.locator('#content')).toBeFocused();
    await expect(page.locator('#loginOverlay')).toBeHidden();
    await expect(page.locator('#loginOverlay')).toHaveAttribute('inert', '');
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Back to spaces' })).toBeFocused();
});

for (const restoredCookie of [false, true]) {
    test(`accessibility: authenticated blocked boot focuses its explanation (${restoredCookie ? 'restored cookie' : 'keyboard sign-in'})`, async ({ page }) => {
        await routeConsole(page);
        if (!restoredCookie) await page.route('**/api/spaces', route => json(route, {}, 401));
        await page.route('**/api/login', route => json(route, { status: 'ok' }));
        await page.route('**/api/tool', route => {
            const body = route.request().postDataJSON();
            return body.tool === 'system_whoami'
                ? json(route, { status: 'read_only', message: 'This token is read-only.' })
                : route.fallback();
        });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
        if (!restoredCookie) {
            await expect(page.locator('#loginToken')).toBeFocused();
            await page.keyboard.type('E2E-READ-ONLY-TOKEN');
            await page.keyboard.press('Tab');
            await expect(page.locator('#loginBtn')).toBeFocused();
            await page.keyboard.press('Enter');
        }
        await expect(page.getByRole('heading', { name: 'Access blocked' })).toBeVisible();
        await expect.poll(() => page.evaluate(() => document.activeElement.id || document.activeElement.tagName)).toBe('content');
        await expect(page.locator('#loginOverlay')).toBeHidden();
        await expect(page.locator('.app')).not.toHaveAttribute('inert', '');
        await expect(page.locator('#content')).toContainText('This token is read-only.');
    });
}

test('accessibility: a current 401 during sign-in initialization retains login focus and allows keyboard retry', async ({ page }) => {
    await routeConsole(page);
    await page.route('**/api/spaces', route => json(route, {}, 401));
    await page.route('**/api/login', route => json(route, { status: 'ok' }));
    let releaseWhoami;
    const firstWhoami = new Promise(resolve => { releaseWhoami = resolve; });
    let whoamiCalls = 0;
    await page.route('**/api/tool', async route => {
        const body = route.request().postDataJSON();
        if (body.tool === 'space_list') return json(route, {}, 401);
        if (body.tool === 'system_whoami' && ++whoamiCalls === 1) {
            await firstWhoami;
            return json(route, { status: 'ok', client_name: 'expired-session', permissions: ['admin'] });
        }
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    const token = page.locator('#loginToken');
    const login = page.locator('#loginBtn');
    await expect(token).toBeFocused();
    await page.keyboard.type('E2E-OPERATOR-TOKEN');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await expect.poll(() => whoamiCalls).toBe(1);
    await page.evaluate(() => callTool('space_list').catch(() => {}));
    await expect(token).toBeFocused();
    await expect(login).toBeDisabled();
    await expect(page.locator('.app')).toHaveAttribute('inert', '');
    releaseWhoami();
    await expect(login).toBeEnabled();
    await expect(token).toBeFocused();
    await expect(page.locator('#identityBlock')).toBeEmpty();
    await page.keyboard.type('E2E-RETRY-TOKEN');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await expect(page.locator('#content')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Back to spaces' })).toBeFocused();
});

test('accessibility: stale cookie initialization cannot steal focus from a newer keyboard login', async ({ page }) => {
    await routeConsole(page);
    await page.route('**/api/login', route => json(route, { status: 'ok' }));
    let releaseWhoami;
    const firstWhoami = new Promise(resolve => { releaseWhoami = resolve; });
    let whoamiCalls = 0;
    await page.route('**/api/tool', async route => {
        const body = route.request().postDataJSON();
        if (body.tool === 'space_list') return json(route, {}, 401);
        if (body.tool === 'system_whoami' && ++whoamiCalls === 1) {
            await firstWhoami;
            return json(route, { status: 'ok', client_name: 'stale-cookie-session', permissions: ['admin'] });
        }
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect.poll(() => whoamiCalls).toBe(1);
    await page.evaluate(() => callTool('space_list').catch(() => {}));
    await expect(page.locator('#loginToken')).toBeFocused();
    await page.keyboard.type('E2E-NEWER-TOKEN');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await expect(page.locator('#content')).toBeFocused();
    await page.keyboard.press('Tab');
    const back = page.getByRole('link', { name: 'Back to spaces' });
    await expect(back).toBeFocused();
    const oldResponse = page.waitForResponse(response => response.url().endsWith('/api/tool')
        && response.request().postDataJSON().tool === 'system_whoami');
    releaseWhoami();
    await (await oldResponse).finished();
    // Let the response body's promise and its UI continuation finish.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(back).toBeFocused();
    await expect(page.locator('#identityBlock')).toContainText('visual-qa-admin');
    await expect(page.locator('#loginOverlay')).toBeHidden();
});

test('accessibility: keyboard logout retains login focus through cookie cleanup and re-login', async ({ page }) => {
    const state = await routeConsole(page);
    let releaseLogout;
    const logout = new Promise(resolve => { releaseLogout = resolve; });
    await page.route('**/api/logout', async route => {
        await logout;
        return json(route, { status: 'ok' });
    });
    let loginCalls = 0;
    await page.route('**/api/login', route => {
        loginCalls += 1;
        return json(route, { status: 'ok' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await page.locator('#portalCheckServices').click();
    await expect(page.locator('#portalHealth')).toHaveText('Services healthy');
    await page.getByRole('button', { name: 'Sign out' }).focus();
    await page.keyboard.press('Enter');
    const token = page.locator('#loginToken');
    const login = page.locator('#loginBtn');
    await expect(token).toBeFocused();
    await expect(login).toBeDisabled();
    await expect(page.locator('#identityBlock')).toBeEmpty();
    await expect(page.locator('#sdTierPanel')).toHaveCount(0);
    await expect(page.locator('.app')).toHaveAttribute('inert', '');
    await expect(page).toHaveURL(/#\/spaces\/demo\/memory\/mid$/);
    await page.keyboard.type('E2E-NEW-SESSION-TOKEN');
    await page.keyboard.press('Enter');
    expect(loginCalls).toBe(0);
    releaseLogout();
    await expect(login).toBeEnabled();
    await expect(token).toBeFocused();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Enter');
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
    await expect(page.locator('#content')).toBeFocused();
    expect(loginCalls).toBe(1);
    await page.evaluate(() => { location.hash = '#/dashboard'; });
    await expect(page.locator('#dashHealthCard')).toContainText('Services not checked');
    await expect(page.locator('#dashHealthCard')).not.toContainText("Couldn't load system health");
    await expect(page.locator('#portalHealth')).toHaveText('Services not checked');
    expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(1);
});

test('accessibility: completing authentication retains focus in an open mobile drawer', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeConsole(page);
    let releaseWhoami;
    const whoami = new Promise(resolve => { releaseWhoami = resolve; });
    let whoamiPending = false;
    await page.route('**/api/tool', async route => {
        if (route.request().postDataJSON().tool !== 'system_whoami') return route.fallback();
        whoamiPending = true;
        await whoami;
        return json(route, { status: 'read_only', message: 'This token is read-only.' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect.poll(() => whoamiPending).toBe(true);
    const toggle = page.locator('#sidebarMenuToggle');
    await toggle.focus();
    await page.keyboard.press('Enter');
    const firstLink = page.locator('#sidebar a.nav-item').first();
    await expect(firstLink).toBeFocused();
    releaseWhoami();
    await expect(page.locator('#content h1')).toHaveText('Access blocked');
    await expect(firstLink).toBeFocused();
    await expect(page.locator('#content')).toHaveAttribute('inert', '');
    await page.keyboard.press('Escape');
    await expect(toggle).toBeFocused();
    await expect(page.locator('#content')).not.toHaveAttribute('inert', '');
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`compaction separates checking from rewriting and keeps exact evidence at ${viewport.width}px`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.addInitScript(() => {
            Object.defineProperty(navigator, 'clipboard', { value: { writeText: async text => { window.__copiedValue = text; } } });
        });
        let finishScan;
        const scan = new Promise(resolve => { finishScan = resolve; });
        const state = await routeConsole(page, { compactResponse: args => args.dry_run ? scan : compactReport(false) });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
        await page.getByRole('button', { name: 'Check files for compaction' }).click();
        await expect(page.locator('#sdCompactionResults')).toContainText('Checking bank files…');
        expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments.dry_run)).toEqual([true]);
        finishScan(compactReport(true));
        const results = page.locator('#sdCompactionResults');
        await expect(results.getByRole('heading', { name: 'Files eligible for compaction' })).toBeVisible();
        await expect(results).toContainText('No changes made');
        await expect(results).toContainText('does not generate rewritten content');
        const details = results.locator('.compaction-details');
        await expect(details).toHaveJSProperty('open', false);
        await expect(results.locator('table:visible thead th')).toHaveCount(4);
        await details.locator('summary').click();
        await expect(details.getByText('Source SHA-256', { exact: true }).first()).toBeVisible();
        await details.getByRole('button', { name: /^Copy a{12}/ }).click();
        expect(await page.evaluate(() => window.__copiedValue)).toBe('a'.repeat(64));
        await expect(details).toContainText('70000 UTF-8 bytes');
        await details.locator('summary').click();
        await results.getByRole('button', { name: 'Compact files…' }).click();
        await expect(page.locator('#adminModal')).toContainText('secondary detail may be lost');
        expect(state.calls.filter(call => call.tool === 'bank_compact').length).toBe(1);
        await page.locator('#modalConfirmBtn').click();
        await expect(results.getByRole('heading', { name: 'Compaction applied' })).toBeVisible();
        expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments)).toEqual([
            { space_id: 'demo', dry_run: true }, { space_id: 'demo', dry_run: false },
        ]);
        await expect(details).toHaveJSProperty('open', false);
        await expect(results.locator('table:visible thead th')).toHaveCount(5);
        await expect(results.getByRole('button', { name: 'Compact files…' })).toHaveCount(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
        await page.screenshot({ path: `test-results/compaction-applied-${viewport.width}.png`, fullPage: true });
        await results.screenshot({ path: `test-results/compaction-applied-panel-${viewport.width}.png` });
    });
}

for (const outcome of ['conflict', 'unknown', 'recovery']) {
    test(`compaction ${outcome} stays honest without opening technical details`, async ({ page }) => {
        await page.setViewportSize({ width: 390, height: 844 });
        const response = outcome === 'conflict'
            ? { status: 'conflict', message: 'A consolidation is already running.' }
            : outcome === 'unknown'
                ? compactReport(false, { total_size_after: null })
                : compactReport(false, {
                    status: 'partial', recovery_required: true, apply_may_have_mutated: true,
                    total_size_after: null, files_applied_before_failure: 1,
                    failed_phase: 'apply', rollback_outcome: 'unverified',
                    failure_reason: 'compaction_apply_recovery_unverified',
                    remediation: 'Inspect the retained preimage before manual recovery.',
                    files: [{ filename: 'activeContext.md', error: 'compaction_apply_failed', source_sha256: 'a'.repeat(64) }],
                });
        const state = await routeConsole(page, { compactResponse: args => args.dry_run ? compactReport(true) : response });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
        await page.getByRole('button', { name: 'Check files for compaction' }).click();
        await page.getByRole('button', { name: 'Compact files…' }).click();
        await page.locator('#modalConfirmBtn').click();
        const results = page.locator('#sdCompactionResults');
        if (outcome === 'conflict') {
            await expect(results.getByRole('heading', { name: 'Consolidation in progress' })).toBeVisible();
        } else {
            await expect(results.getByText('Final size could not be verified.', { exact: true })).toBeVisible();
            await expect(results.locator('.compaction-details')).toHaveJSProperty('open', false);
            if (outcome === 'recovery') {
                await expect(results.getByRole('heading', { name: 'Compaction recovery required' })).toBeVisible();
                await expect(results.getByText('Bank content may have changed.')).toBeVisible();
                await expect(results.getByText('Rollback result: unverified')).toBeVisible();
                await expect(results.getByText('Files changed before failure: 1')).toBeVisible();
                await expect(results.getByText('Next step: Inspect the retained preimage before manual recovery.')).toBeVisible();
                await expect(results.locator('.compaction-failures')).toContainText('compaction_apply_failed');
                await expect(results).not.toContainText('No changes made');
            }
        }
        expect(state.calls.filter(call => call.tool === 'bank_compact').length).toBe(2);
        await page.screenshot({ path: `test-results/compaction-${outcome}-390.png`, fullPage: true });
        await results.screenshot({ path: `test-results/compaction-${outcome}-panel-390.png` });
    });
}

test('dashboard labels the actual job data and keeps service probes separate from overview refreshes', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.getByRole('heading', { name: 'Recent jobs', exact: true })).toBeVisible();
    await expect(page.locator('#dashCardScope')).toContainText('Latest 1 of 1');
    await expect(page.locator('#dashLanesPanel')).toContainText('No jobs in progress');
    await expect(page.locator('#dashActivityPanel article[data-job-id]')).toHaveCount(10);
    await page.locator('#dashActivityPanel .diagnostic-details summary').click();
    await expect(page.locator('#dashActivityPanel')).toContainText('Up to 10 recent jobs for these cards.');
    await expect(page.locator('#dashTokensTile')).toHaveCount(0);
    const count = tool => state.calls.filter(call => call.tool === tool).length;
    expect(count('system_health')).toBe(0);
    await page.locator('#portalRefresh').click();
    await expect.poll(() => count('bank_consolidation_queues')).toBe(2);
    expect(count('system_health')).toBe(0);
    await page.locator('#portalCheckServices').click();
    await expect.poll(() => count('system_health')).toBe(1);
    expect(count('bank_consolidation_queues')).toBe(2);
    await page.locator('.dash-diagnostics > summary').click();
    await page.getByRole('button', { name: 'System health — open details' }).click();
    await expect(page.locator('#adminModal')).toContainText('System health');
    expect(count('system_health')).toBe(1);
    await page.locator('#dashHealthModalRefreshBtn').click();
    await expect.poll(() => count('system_health')).toBe(2);
    expect(count('bank_consolidation_queues')).toBe(2);
});


test('polish: mobile compaction result title precedes its action', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await page.getByRole('button', { name: 'Check files for compaction', exact: true }).click();
    const title = page.getByRole('heading', { name: 'Files eligible for compaction', exact: true });
    await expect(title).toBeVisible();
    const action = page.getByRole('button', { name: 'Compact files…', exact: true });
    const titleBox = await title.boundingBox();
    const actionBox = await action.boundingBox();
    expect(titleBox.y + titleBox.height).toBeLessThanOrEqual(actionBox.y);
    expect(actionBox.height).toBeGreaterThanOrEqual(44);
    await page.locator('#sdCompactionResults').scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath('mobile-compaction-result.png'), fullPage: true });
});

test('polish: consolidation explains only known worker configuration and keeps raw diagnostics', async ({ page }, testInfo) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/consolidation`);
    const subtitle = page.locator('#consolSubtitle');
    await expect(subtitle).toContainText('1 worker per space');
    const details = subtitle.locator('details');
    await expect(details).toHaveJSProperty('open', false);
    await details.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(details.getByText('one_worker_per_space', { exact: true })).toBeVisible();
    await expect(subtitle).toContainText('batch size: 3 notes');
    await expect(page.locator('#consolLanes a[href="#/spaces/demo"]').first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('consolidation-known-worker.png'), fullPage: true });

    let workerModel = 'future_model<untrusted>';
    await page.route('**/api/tool', route => {
        const request = route.request().postDataJSON();
        if (request.tool !== 'bank_consolidation_queues') return route.fallback();
        return json(route, { status: 'ok', parallelism_model: workerModel, lanes: [], total_spaces: 0 });
    });
    await page.reload();
    await expect(subtitle).toContainText('Worker configuration unavailable');
    await expect(subtitle).not.toContainText('1 worker per space');
    await details.locator('summary').focus();
    await page.keyboard.press('Space');
    await expect(details.getByText(workerModel, { exact: true })).toBeVisible();
    await expect(subtitle.locator('untrusted')).toHaveCount(0);
    await expect(subtitle).not.toContainText('batch size');
    workerModel = undefined;
    await page.reload();
    await expect(subtitle).toContainText('Worker configuration unavailable');
    await details.locator('summary').click();
    await expect(details.getByText('Not reported', { exact: true })).toBeVisible();
});

test('Maintenance requires a fresh visible check after entering the same Space from global tools', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/operator/maintenance`);
    await page.locator('#opMaintSpace').selectOption('demo');
    await page.getByRole('button', { name: 'Check files for compaction', exact: true }).click();
    await expect(page.locator('#opCompactResults')).toContainText('Files eligible for compaction');
    expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments.dry_run)).toEqual([true]);
    const inventoryCalls = state.calls.filter(call => call.tool === 'space_list').length;
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/maintenance`);
    await expect(page.locator('#opMaintSpace')).toHaveValue('demo');
    await expect(page.locator('#opMaintSpace')).toBeDisabled();
    expect(state.calls.filter(call => call.tool === 'space_list')).toHaveLength(inventoryCalls);
    await page.getByRole('button', { name: 'Compact files…', exact: true }).click();
    await expect(page.locator('#opCompactResults')).toContainText('Check files for demo before compaction.');
    await expect(page.locator('#adminModal')).toHaveCount(0);
    expect(state.calls.filter(call => call.tool === 'bank_compact')).toHaveLength(1);
    await page.getByRole('button', { name: 'Check files for compaction', exact: true }).click();
    await expect(page.locator('#opCompactResults')).toContainText('Files eligible for compaction');
    await page.getByRole('button', { name: 'Compact files…', exact: true }).click();
    await expect(page.locator('#adminModal')).toContainText('A preimage is saved before changes are applied.');
    expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments.dry_run)).toEqual([true, true]);
});

// #522: Maintenance must use the real shell escaping contract.
for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`Maintenance check-confirm-apply and exact evidence at ${viewport.width}`, async ({ page }, testInfo) => {
      const capture = name => testInfo.outputPath(name + '.png');
      await page.setViewportSize(viewport);
      await page.addInitScript(() => {
        Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.__copiedValue = text;}}});
      });
      let finishScan;
      let checks = 0;
      const pending = new Promise(resolve => { finishScan = resolve; });
      const state = await routeConsole(page, { compactResponse: args => {
        if (!args.dry_run) return compactReport(false);
        checks++;
        return checks === 1 ? pending : compactReport(true, {
          files_over_limit: 0, total_size_before: 40000, total_size_after: 40000,
          files: [
            { filename: 'activeContext.md', size: 30000, max_size: 35000, source_sha256: 'b'.repeat(64), over_limit: false, ratio: 0.86 },
            { filename: 'progress.md', size: 10000, max_size: 35000, source_sha256: 'c'.repeat(64), over_limit: false, ratio: 0.29 },
          ],
        });
      }});
      await page.goto(`${ORIGIN}/admin.html#/operator/maintenance`);
      await page.locator('#opMaintSpace').selectOption('demo');
      const results = page.locator('#opCompactResults');
      const panel = results.locator('..');
      await page.getByRole('button',{ name: 'Compact files…', exact: true }).click();
      await expect(results).toContainText('Check files for demo before compaction.');
      expect(state.calls.filter(call => call.tool === 'bank_compact')).toHaveLength(0);
      await expect(page.locator('#adminModal')).toHaveCount(0);
      await page.getByRole('button',{ name: 'Check files for compaction', exact: true }).click();
      await expect(results).toContainText('Checking bank files…');
      expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments.dry_run)).toEqual([true]);
      finishScan(compactReport(true));
      await expect(results.getByRole('heading',{ name: 'Files eligible for compaction' })).toBeVisible();
      await expect(results).toContainText('1 of 2 files exceed the advisory size. No changes made.');
      await expect(results).toContainText('does not generate rewritten content');
      await expect(results.locator('table:visible thead th')).toHaveText(['File', 'Size', 'Advisory size', 'Result']);
      await expect(results.locator('details')).toHaveJSProperty('open', false);
      await panel.screenshot({path:capture(`maintenance-scan-${viewport.width}`)});
      await results.locator('summary').click();
      await expect(results.locator('details')).toContainText('70000 UTF-8 bytes');
      await results.getByRole('button',{ name: /^Copy a{12}/ }).click();
      expect(await page.evaluate(() => window.__copiedValue)).toBe('a'.repeat(64));
      for (const dismiss of await page.locator('.toast-close').all()) await dismiss.click();
      await panel.screenshot({path:capture(`maintenance-details-${viewport.width}`)});
      await results.locator('summary').click();
      await page.getByRole('button',{ name: 'Compact files…', exact: true }).click();
      await expect(page.locator('#adminModal')).toContainText('secondary detail may be lost');
      await expect(page.locator('#adminModal')).toContainText('A preimage is saved before changes are applied.');
      expect(state.calls.filter(call => call.tool === 'bank_compact')).toHaveLength(1);
      await page.locator('#adminModal').screenshot({path:capture(`maintenance-confirm-${viewport.width}`)});
      await page.locator('#modalConfirmBtn').click();
      await expect(results.getByRole('heading',{ name: 'Compaction applied' })).toBeVisible();
      await expect(results.getByRole('heading',{ name: 'Files eligible for compaction' })).toBeVisible();
      expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments)).toEqual([
        { space_id: 'demo', dry_run: true }, { space_id: 'demo', dry_run: false }, { space_id: 'demo', dry_run: true },
      ]);
      await expect(results.locator('table').first().locator('thead th')).toHaveText(['File', 'Before', 'Advisory size', 'After', 'Result']);
      await expect(results.locator('table').last().locator('thead th')).toHaveCount(4);
      await expect(results.locator('details[open]')).toHaveCount(0);
      await expect(results).toContainText('0 of 2 files exceed the advisory size.');
      for (const dismiss of await page.locator('.toast-close').all()) await dismiss.click();
      await panel.screenshot({path:capture(`maintenance-applied-${viewport.width}`)});
      if (viewport.width === 390) {
          const table = results.locator('table').first();
          const header = await table.locator('thead').boundingBox();
          expect(header.height, 'column labels must remain readable instead of wrapping letter by letter').toBeLessThan(80);
          const scroll = await table.locator('..').evaluate(el => ({ width: el.clientWidth, content: el.scrollWidth }));
          expect(scroll.content).toBeGreaterThan(scroll.width);
          await table.locator('..').evaluate(el => { el.scrollLeft = el.scrollWidth; });
          const lastColumn = await table.locator('th').last().boundingBox();
          const scrollBox = await table.locator('..').boundingBox();
          expect(lastColumn.x + lastColumn.width).toBeLessThanOrEqual(scrollBox.x + scrollBox.width + 1);
          expect(lastColumn.x).toBeGreaterThanOrEqual(scrollBox.x);
          await panel.screenshot({path:capture('maintenance-applied-scrolled-390')});
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      fs.writeFileSync(testInfo.outputPath(`maintenance-requests-${viewport.width}.json`),JSON.stringify(state.calls, null, 2));
    });
}
for (const flags of [{ recovery_required: true, apply_may_have_mutated: true }, { recovery_required: false, apply_may_have_mutated: true }]) {
    const scenario = flags.recovery_required ? 'recovery' : 'mutation-flag';
    test(`Maintenance ${scenario} stays visible at 390px`,async ({ page }, testInfo) => {
      const capture = name => testInfo.outputPath(name + '.png');
      await page.setViewportSize({ width: 390, height: 844 });
      const state = await routeConsole(page, { compactResponse: args => args.dry_run ? compactReport(true) : compactReport(false, {
        status: 'partial', ...flags, total_size_after: null, files_applied_before_failure: 1,
        failed_phase: 'apply', rollback_outcome: 'unverified',
        failure_reason: 'compaction_apply_recovery_unverified',
        remediation: 'Inspect the retained preimage before manual recovery.',
        files: [{ filename: 'activeContext.md', error: 'compaction_apply_failed', source_sha256: 'a'.repeat(64) }],
      })});
      await page.goto(`${ORIGIN}/admin.html#/operator/maintenance`);
      await page.locator('#opMaintSpace').selectOption('demo');
      await page.getByRole('button',{ name: 'Check files for compaction', exact: true }).click();
      const results = page.locator('#opCompactResults');
      await expect(results.getByRole('heading',{ name: 'Files eligible for compaction' })).toBeVisible();
      await page.getByRole('button',{ name: 'Compact files…', exact: true }).click();
      await page.locator('#modalConfirmBtn').click();
      await expect(results.getByRole('heading',{ name: 'Compaction recovery required' })).toBeVisible();
      await expect(results.locator('details')).toHaveJSProperty('open', false);
      for (const label of ['Final size could not be verified.','Bank content may have changed.','Next step: Inspect the retained preimage before manual recovery.','Recovery is required. No automatic retry was attempted.','Rollback result: unverified','Failed during: apply','Files changed before failure: 1','activeContext.md: compaction_apply_failed']) {
        await expect(results.getByText(label,{ exact: true })).toBeVisible();
      }
      expect(state.calls.filter(call => call.tool === 'bank_compact').map(call => call.arguments.dry_run)).toEqual([true, false]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await results.screenshot({path:capture(`maintenance-${scenario}-390`)});
      await page.screenshot({path:capture(`maintenance-${scenario}-page-390`),fullPage:true});
    });
}

// PR524 initial F3/F4/F5: diagnostics remain accessible and defensive.
test.describe('advisory diagnostic disclosures on touch screens', () => {
    test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });
    test('dashboard history diagnostics are reachable by touch and unknown worker metadata cannot run markup', async ({ page }, testInfo) => {
        await routeConsole(page);
        const rawModel = 'future_worker<untrusted>';
        await page.route('**/api/tool', route => {
            const request = route.request().postDataJSON();
            if (request.tool !== 'bank_consolidation_queues') return route.fallback();
            return json(route, { status: 'ok', parallelism_model: rawModel, total_spaces: 1, lanes: [{ space_id: 'demo', lane_state: 'idle', queued_count: 0, guarantee: 'in_memory_best_effort', latest_jobs: [
                { job_id: 'job-1', space_id: 'demo', status: 'succeeded', finished_at: '2026-09-09T12:00:00Z' },
            ] }] });
        });
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        const lanes = page.locator('#dashLanesPanel');
        await expect(lanes).toContainText('No jobs in progress');
        await expect(lanes.locator('untrusted')).toHaveCount(0);
        const history = page.locator('#dashActivityPanel details');
        await expect(history).toHaveJSProperty('open', false);
        await history.screenshot({ path: testInfo.outputPath('mobile-history-details-closed.png') });
        await history.locator('summary').tap();
        await expect(history.getByText('in_memory_best_effort', { exact: true })).toBeVisible();
        await expect(history).toContainText('does not survive a restart and history is trimmed');
        await history.screenshot({ path: testInfo.outputPath('mobile-history-details-open.png') });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath('touch-dashboard-diagnostics.png'), fullPage: true });
    });
});

test('space heading navigation identifies the route space without duplicating its visible title', async ({ page }) => {
    await routeConsole(page);
    for (const spaceId of ['demo', 'other-space']) {
        await page.goto(`${ORIGIN}/admin.html#/spaces/${spaceId}/mid`);
        const heading = page.getByRole('heading', { level: 1, name: `Space ${spaceId}`, exact: true });
        await expect(heading).toBeVisible();
        await expect(heading).toHaveText('Space');
    }
});

test('malformed Maintenance diagnostics retain visible recovery and escape server values', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routeConsole(page, { compactResponse: args => args.dry_run ? compactReport(true) : compactReport(false, {
        status: 7, failure_reason: 42, failed_phase: ['<apply>'], rollback_outcome: true,
        remediation: ['Inspect <preimage>'], recovery_required: true, apply_may_have_mutated: true,
        files_applied_before_failure: 1, total_size_after: null,
        files: [{ filename: 23, error: ['<file-error>'], ratio: 2 }],
    }) });
    await page.goto(`${ORIGIN}/admin.html#/operator/maintenance`);
    await page.locator('#opMaintSpace').selectOption('demo');
    await page.getByRole('button', { name: 'Check files for compaction', exact: true }).click();
    const results = page.locator('#opCompactResults');
    await expect(results.getByRole('heading', { name: 'Files eligible for compaction' })).toBeVisible();
    await page.getByRole('button', { name: 'Compact files…', exact: true }).click();
    await page.locator('#modalConfirmBtn').click();
    await expect(results.getByRole('heading', { name: 'Compaction recovery required' })).toBeVisible();
    await expect(results.locator('details')).toHaveJSProperty('open', false);
    for (const label of ['Failure reason: 42', 'Failed during: <apply>', 'Rollback result: true', 'Next step: Inspect <preimage>', 'Bank content may have changed.', 'Final size could not be verified.']) {
        await expect(results.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(results.locator('apply, preimage, file-error')).toHaveCount(0);
    await results.screenshot({ path: testInfo.outputPath('maintenance-malformed-recovery-closed.png') });
    await results.locator('summary').click();
    await expect(results.getByText('<file-error>', { exact: true })).toBeVisible();
    await results.screenshot({ path: testInfo.outputPath('maintenance-malformed-recovery.png') });
});

// PR #524 F1: ordinary labels follow the shared body typography; API values do not.
const LABEL_MESH_PAIRING = {
    pair_id: 'pair_raw_CASE_17', space_id: 'demo', role: 'source', state: 'active',
    base_epoch: 17, updated_at_ms: 1788948000000,
    source_fingerprint: 'hm1:' + 'a'.repeat(64), source_endpoint: 'https://source.example.invalid',
    target_fingerprint: 'hm1:' + 'b'.repeat(64), target_endpoint: 'https://target.example.invalid',
    granted_scopes: ['bank_read', 'graph_query'], invitation_digest: 'sha256:' + 'c'.repeat(64),
    activation_event_id: 'BANK_COMMIT_raw_17', last_error: 'RAW_DIAGNOSTIC_code',
};

async function routeLabelProof(page) {
    const source = {
        space_id: 'demo', state: 'ready', source_ready: true, source_initializable: false,
        can_create_invitation: true, resumable: false, reason_code: 'ready',
        message: 'Ready to create an invitation.', state_token: 'd'.repeat(64),
    };
    const calls = await routeConsole(page, { meshSource: source });
    await page.route('**/api/admin/mesh/status', route => json(route, {
        status: 'ok', enabled: true, healthy: true, display_name: 'QA_instance_RAW',
        fingerprint: LABEL_MESH_PAIRING.source_fingerprint, public_url: LABEL_MESH_PAIRING.source_endpoint,
        pairings: [LABEL_MESH_PAIRING], source_readiness: [source], eligible_spaces: ['demo'],
    }));
    await page.route('**/api/admin/mesh/members/demo', route => json(route, {
        status: 'ok', space_id: 'demo', membership_epoch: 17, members: [],
    }));
    await page.route('**/api/tool', route => {
        const body = route.request().postDataJSON();
        if (body.tool !== 'admin_audit_recent') return route.fallback();
        calls.calls.push(body);
        return json(route, {
            status: 'ok', total: 1, capacity: 500, scope_note: 'RAW_scope_note',
            entries: [{ ts: '2026-09-09T10:00:00Z', event: 'admin_tool_call', tool: 'bank_read',
                arguments_keys: ['space_id', 'filename'], client: 'QA_client_RAW', auth_type: 'stored' }],
        });
    });
    return calls;
}

test('Space consolidation summary uses readable labels and preserves exact scope and guarantee', async ({ page }) => {
    const spaceData = spaceInfo();
    spaceData.consolidation_queue = {
        lane_state: 'running', running_job: { job_id: 'job_RAW_17', status: 'running', scope_label: 'agent_RAW_17', progress: { notes_done: 3, notes_total: 8 } }, queued_count: 2,
        parallelism_model: 'one_worker_per_space', service_config: { batch_size: 3 },
        guarantee: 'in_memory_best_effort', latest_jobs: [],
    };
    const state = await routeConsole(page, { spaceData });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('#sdTierPanel .panel-header .micro-label')).toHaveText('MID');
    await expect(page.locator('#sdTierPanel .panel-header .micro-label')).toHaveCSS('font-family', /JetBrains Mono/);
    await page.getByRole('tab', { name: 'Consolidation', exact: true }).click();
    await expect(page.locator('.consol-job-meta')).toContainText('agent_RAW_17');
    await expect(page.getByRole('img', { name: 'Progress as of last refresh: 3/8 notes' })).toBeVisible();
    await expect(page.locator('#consolLanes')).toContainText('2 queued; job details unavailable');
    await expect(page.locator('#consolSubtitle')).toContainText('1 worker per space');
    await expect(page.locator('#consolSubtitle')).toContainText('batch size: 3 notes');
    await expect(page.locator('#consolSubtitle')).not.toContainText('Worker configuration unavailable');
    expect(state.calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(0);
    await page.locator('#consolSubtitle summary').click();
    await expect(page.locator('#consolSubtitle')).toContainText('in_memory_best_effort');
    const label = page.locator('.consol-job-meta').first();
    await expect(label).toHaveCSS('font-family', /Hanken Grotesk/);
    await expect(label).toHaveCSS('text-transform', 'none');
});

test('F1 labels: shell distinguishes unavailable and server messages without altering server text', async ({ page }) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/consolidation`);
    await expect(page.getByRole('heading', { name: 'In progress', exact: true })).toBeVisible();
    const labels = await page.evaluate(() => {
        const host = document.createElement('div');
        host.innerHTML = stateUnavailable('RAW_missing_STATE') + serverMessage('RAW_<server>&_Message');
        return { labels: [...host.querySelectorAll('.micro-label, .server-msg-label')].map(el => el.textContent), text: host.textContent };
    });
    expect(labels.labels).toEqual(['Not available', 'Server message']);
    expect(labels.text).toContain('RAW_missing_STATE');
    expect(labels.text).toContain('RAW_<server>&_Message');
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`F1 labels: Mesh and Audit preserve raw diagnostics at ${viewport.width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport);
        const calls = await routeLabelProof(page);
        await page.goto(`${ORIGIN}/admin.html#/mesh`);
        await expect(page.locator('#content .panel').first().locator('.micro-label')).toHaveText(['Display name', 'Fingerprint', 'Public URL']);
        await expect(page.locator('#content .panel').first()).toContainText('QA_instance_RAW');
        await page.evaluate(async () => { await document.fonts.ready; });
        await page.screenshot({ path: testInfo.outputPath(`mesh-${viewport.width}.png`), fullPage: true });

        await page.goto(`${ORIGIN}/admin.html#/mesh/demo`);
        await expect(page.locator('#content .panel').first().locator('.sd-meta-row').first().locator(':scope > .sd-kv > .micro-label')).toHaveText(['Membership epoch', 'Active members', 'Pairing sessions']);
        const diagnostics = page.locator('.mesh-diagnostics').first();
        await diagnostics.locator('summary').click();
        await expect(diagnostics.locator('.micro-label')).toHaveText([
            'Pair ID', 'Base epoch', 'Source fingerprint', 'Source endpoint', 'Target fingerprint', 'Target endpoint',
            'Granted scopes', 'Invitation digest', 'Claim digest', 'Approval digest', 'Bootstrap manifest digest', 'Activation event ID', 'Last error',
        ]);
        await expect(diagnostics).toContainText(LABEL_MESH_PAIRING.pair_id);
        await expect(diagnostics).toContainText(LABEL_MESH_PAIRING.activation_event_id);
        await expect(diagnostics).toContainText(LABEL_MESH_PAIRING.last_error);
        await expect(diagnostics).toContainText(LABEL_MESH_PAIRING.invitation_digest);
        await page.screenshot({ path: testInfo.outputPath(`mesh-diagnostics-${viewport.width}.png`), fullPage: true });
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(viewport.width);

        await page.goto(`${ORIGIN}/admin.html#/audit`);
        await expect(page.locator('.audit-view .micro-label')).toHaveText(['In-memory audit scope', 'Server scope note']);
        await expect(page.locator('.audit-view .form-label')).toHaveText(['Event type', 'Requested tool', 'Client']);
        await expect(page.locator('.audit-view tbody')).toContainText('admin_tool_call');
        await expect(page.locator('.audit-tool')).toHaveText('bank_read');
        await expect(page.locator('.audit-key-chip')).toHaveText(['space_id', 'filename']);
        await expect(page.locator('.audit-server-scope p')).toHaveText('RAW_scope_note');
        await page.screenshot({ path: testInfo.outputPath(`audit-${viewport.width}.png`), fullPage: true });
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(viewport.width);
        expect(calls.calls.filter(call => call.tool === 'admin_audit_recent')).toHaveLength(1);
        expect(calls.meshCalls.every(call => call.method === 'GET')).toBe(true);
    });
}

test('F1 labels: Consolidation job status headings preserve empty and failure messages', async ({ page }) => {
    await routeConsole(page);
    let job = { status: 'succeeded', job_id: 'job-0', space_id: 'demo', result: { notes_total: 0, message: 'RAW_empty_message' } };
    await page.route('**/api/tool', route => {
        if (route.request().postDataJSON().tool === 'bank_consolidation_status') return json(route, job);
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/consolidation`);
    const inspect = page.locator('[data-action="consol-job"]').first();
    await inspect.click();
    await expect(page.locator('.consol-nothing .micro-label')).toHaveText('Nothing to do');
    await expect(page.locator('.consol-nothing .server-msg-text')).toHaveText('RAW_empty_message');
    await page.keyboard.press('Escape');
    job = { status: 'failed', job_id: 'job-0', space_id: 'demo', error: 'RAW_failure_code: <retry>&' };
    await inspect.click();
    await expect(page.locator('#adminModal .state-error .micro-label')).toHaveText('Failed');
    await expect(page.locator('#adminModal .server-msg-text')).toHaveText('RAW_failure_code: <retry>&');
});


const readableSpaceId = 'project-memory-' + 'x'.repeat(49);
async function readableSpacesFixture(page) {
    const state = await routeConsole(page);
    const running = { job_id: 'running-now', space_id: readableSpaceId, status: 'running', scope_label: "All agents' notes", started_at: '2026-09-09T12:00:00Z', progress: { notes_done: 12, notes_total: 22 } };
    const older = { job_id: 'older-finished', space_id: readableSpaceId, status: 'succeeded', scope_label: 'My notes', finished_at: '2026-09-08T12:00:00Z' };
    const latest = { job_id: 'latest-partial', space_id: readableSpaceId, status: 'failed', scope_label: "All agents' notes", finished_at: '2026-09-09T11:00:00Z', result: { status: 'partial', notes_processed: 4, notes_total: 9 }, error: 'Some notes remain.' };
    const lanes = [
        { space_id: readableSpaceId, lane_state: 'running', running_job: running, queued_count: 0, queued_jobs: [], latest_jobs: [running, older, latest, latest], guarantee: 'in_memory_best_effort' },
        { space_id: 'quiet-space', lane_state: 'idle', queued_count: 0, queued_jobs: [], latest_jobs: [] },
    ];
    await page.route('**/api/tool', async route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'space_list') {
            state.calls.push(req);
            return json(route, { status: 'ok', spaces: [
                { space_id: 'quiet-space', description: 'No recent jobs', owner: 'reader' },
                { space_id: readableSpaceId, description: 'A clear description <img src=x onerror="window.__visualQaXss=1">', owner: 'maintainer', live_notes_count: 22, bank_files_count: 6 },
            ] });
        }
        if (req.tool === 'bank_consolidation_queues') {
            state.calls.push(req);
            const ids = req.arguments.space_ids;
            return json(route, { status: 'ok', lanes: ids ? lanes.filter(l => ids.split(',').includes(l.space_id)) : lanes, parallelism_model: 'one_worker_per_space' });
        }
        return route.fallback();
    });
    return state;
}

for (const width of [1440, 768, 720, 390, 320]) {
    test(`spaces amendment: readable names and scoped job navigation at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 900 });
        // 1440 physical pixels at 200% browser zoom has a 720 CSS-pixel layout.
        if (width === 720) {
            const cdp = await page.context().newCDPSession(page);
            await cdp.send('Emulation.setDeviceMetricsOverride', { width: 720, height: 450, deviceScaleFactor: 2, mobile: false });
        }
        const state = await readableSpacesFixture(page);
        await page.goto(`${ORIGIN}/admin.html#/spaces`);
        const search = page.getByRole('searchbox', { name: /Search spaces/ });
        await expect(search).toBeVisible();
        await expect(page.locator('#spacesTableWrap th')).toHaveText(['Space', 'Memory', 'Consolidation']);
        const space = page.getByRole('link', { name: readableSpaceId, exact: true });
        await expect(space).toHaveText(readableSpaceId);
        await expect(space).toHaveCSS('font-size', '16px');
        await expect(space).toHaveCSS('font-weight', '600');
        await expect(space).toHaveCSS('font-family', /Hanken Grotesk/);
        await expect(page.locator('.page-header h1')).toHaveCSS('font-weight', '600');
        await expect(page.locator('#spacesTableWrap')).toContainText('12 / 22');
        const status = page.locator('#spacesTableWrap .status-dot-label').filter({ hasText: /^Running$/ }).first();
        await expect(status).toHaveCSS('font-family', /Hanken Grotesk/);
        await expect(status).toHaveCSS('font-size', '13px');
        await expect(status).toHaveCSS('line-height', '18px');
        await expect(status).toHaveCSS('font-weight', '500');
        expect(state.calls.filter(c => c.tool === 'space_info' || c.tool === 'graph_status' || c.tool === 'bank_stale_spaces')).toHaveLength(0);
        expect(state.calls.filter(c => c.tool === 'space_list')).toHaveLength(1);
        expect(state.calls.filter(c => c.tool === 'bank_consolidation_queues')).toHaveLength(1);
        const before = state.calls.length;
        await search.fill('maintainer');
        await expect(page.locator('#spacesTableWrap tbody tr')).toHaveCount(1);
        await expect(search).toBeFocused();
        expect(state.calls.length).toBe(before);
        await search.fill('missing');
        await expect(page.locator('#spacesTableWrap')).toContainText('No spaces match');
        await search.fill('');
        await expect(page.locator('#spacesTableWrap tbody tr')).toHaveCount(2);
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({ path: testInfo.outputPath(`spaces-amendment-${width}.png`), fullPage: true, animations: 'disabled' });
        await page.locator(`a[href="#/consolidation/${readableSpaceId}"]`).first().focus();
        await page.keyboard.press('Enter');
        await expect(page.getByRole('heading', { name: 'In progress', exact: true })).toBeVisible();
        await expect(page.locator('[data-nav="consolidation"]')).toHaveAttribute('aria-current', 'page');
        await expect(page.locator('[data-nav="spaces"]')).not.toHaveAttribute('aria-current', 'page');
        await expect(page.getByRole('heading', { name: 'Recent history', exact: true })).toBeVisible();
        await expect(page.locator('#consolLanes')).not.toContainText('quiet-space');
        await expect(page.locator('[data-action="consol-job"][data-job-id="latest-partial"]')).toHaveCount(1);
        await expect(page.locator('#consolLanes')).toContainText('Partial');
        const jobIds = await page.locator('[data-action="consol-job"]').evaluateAll(nodes => nodes.map(n => n.dataset.jobId));
        expect(jobIds.indexOf('latest-partial')).toBeLessThan(jobIds.indexOf('older-finished'));
        expect(jobIds.filter(id => id === 'running-now')).toHaveLength(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`consolidation-amendment-${width}.png`), fullPage: true, animations: 'disabled' });
    });
}

test('spaces amendment: invalid scoped route makes no queue request', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/consolidation/invalid%2Fspace`);
    await expect(page.locator('#content')).toContainText('Invalid space id');
    expect(state.calls.some(c => c.tool === 'bank_consolidation_queues')).toBe(false);
});

// #526 / F3: ordinary status badges follow the same reading face as labels.
test('typography: ordinary status pills use Hanken sentence case', async ({ page }) => {
    await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/access`);
    const active = page.locator('#content .pill').filter({ hasText: /^Active$/ }).first();
    await expect(active).toBeVisible();
    await expect(active).toHaveCSS('font-family', /Hanken Grotesk/);
    await expect(active).toHaveCSS('font-size', '11px');
    await expect(active).toHaveCSS('line-height', '14px');
    await expect(active).toHaveCSS('font-weight', '500');
    await expect(active).toHaveCSS('text-transform', 'none');
    await expect(active).toHaveCSS('letter-spacing', 'normal');
    const header = page.locator('#content .data-table th').first();
    await expect(header).toHaveCSS('font-family', /Hanken Grotesk/);
    await expect(header).toHaveCSS('font-weight', '500');
    await expect(header).toHaveCSS('text-transform', 'none');
    await expect(header).toHaveCSS('line-height', '18px');
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    const label = page.locator('label[for="spacesSearch"]');
    await expect(label).toHaveCSS('font-family', /Hanken Grotesk/);
    await expect(label).toHaveCSS('font-size', '13px');
    await expect(label).toHaveCSS('line-height', '18px');
    await expect(label).toHaveCSS('font-weight', '500');
    await expect(label).toHaveCSS('text-transform', 'none');
});

// F5/F6: absence of lane visibility and the explicit scan remain distinct.
test('Consolidation shows no visible spaces and lets the notes scan collapse', async ({ page }) => {
    const state = await routeConsole(page);
    let scans = 0;
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'bank_consolidation_queues') {
            state.calls.push(req);
            return json(route, { status: 'ok', lanes: [], denied_spaces: [] });
        }
        if (req.tool === 'bank_stale_spaces') {
            scans += 1;
            return json(route, { status: 'ok', spaces: [], total_spaces: 0, total_stale: 0, min_notes: 5, min_age_days: 5 });
        }
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/consolidation`);
    await expect(page.getByText('No spaces visible', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'In progress', exact: true })).toHaveCount(0);
    const open = page.getByRole('button', { name: 'Find notes', exact: true });
    await open.focus();
    await page.keyboard.press('Enter');
    const close = page.getByRole('button', { name: 'Hide notes', exact: true });
    await expect(close).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => scans).toBe(1);
    await close.focus();
    await page.keyboard.press('Enter');
    await expect(open).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('#consolStaleMinNotes')).toHaveCount(0);
    expect(scans).toBe(1);
    expect(state.calls.filter(c => c.tool === 'bank_consolidation_queues')).toHaveLength(1);
});

for (const width of [1440, 390]) {
    test(`RC2 automation: unbound space exposes paused transfer and retained backlog at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 1000 });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await routeConsole(page, { graphResponse: {
            status: 'ok', connected: false, embedded: true, bound: false,
            mid_automation: { compaction_enabled: true, archive_enabled: false },
            mid_archive_projection: { pending: 3, oldest_at: '2026-09-24T08:00:00Z',
                error: '<img src=x onerror="window.__visualQaXss=1">' },
        }});
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long`);
        const automation = page.getByRole('region', { name: 'MID to LONG automation' });
        await expect(automation).toContainText('Disabled by configuration');
        await expect(automation).toContainText('Captures pending indexing');
        await expect(automation).toContainText('3');
        await expect(automation).toContainText('A zero backlog does not mean every current MID file is indexed.');
        await expect(automation).toContainText('<img src=x');
        await expect(automation.locator('img')).toHaveCount(0);
        await expect(page.getByText('Waiting for the first ingestion')).toBeVisible();
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
        const overflow = await automation.evaluate(el => el.scrollWidth > el.clientWidth + 1);
        expect(overflow).toBe(false);
        expect(errors).toEqual([]);
        await page.screenshot({ path: testInfo.outputPath(`rc2-automation-${width}.png`), fullPage: true });
    });
}

test('RC2 automation: both job inspectors distinguish completed consolidation from failed maintenance', async ({ page }, testInfo) => {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const job = { status: 'succeeded', job_id: 'job-0', space_id: 'demo', result: {
        status: 'ok', notes_processed: 3, notes_total: 3, auto_compaction: {
            status: 'partial', started_at: '2026-09-24T08:00:00Z', finished_at: '2026-09-24T08:01:00Z',
            recovery_required: true, failure_reason: 'compaction_apply_recovery_unverified',
            files: [{ filename: '<img src=x onerror="window.__visualQaXss=1">.md', over_limit: true }],
        },
    }};
    const space = spaceInfo();
    space.consolidation_queue.latest_jobs = [job];
    await routeConsole(page, { spaceData: space });
    await page.route('**/api/tool', route => {
        if (route.request().postDataJSON().tool === 'bank_consolidation_status') return json(route, job);
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/consolidation`);
    await page.locator('[data-action="consol-job"]').first().click();
    const modal = page.locator('#adminModal');
    await expect(modal).toContainText('Compaction incomplete');
    await expect(modal).toContainText('Recovery must be checked before retrying.');
    await expect(modal).not.toContainText('LONG indexing has its own status.');
    await expect(modal.locator('img')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('rc2-job-consolidation.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/consolidation`);
    await page.locator('[data-action="consol-job"][data-job-id="job-0"]').first().click();
    await expect(page.getByRole('heading', { name: 'Automatic MID compaction — Compaction incomplete' })).toBeVisible();
    await expect(page.locator('#consolJobSnapshot').getByText('Recovery must be checked before retrying.')).toBeVisible();
    await expect(page.locator('#consolJobSnapshot')).not.toContainText('LONG indexing has its own status.');
    expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath('rc2-job-space.png'), fullPage: true });
});

test('RC2 automation: current-file push is distinct and a partial response is not success', async ({ page }) => {
    await routeConsole(page);
    await page.route('**/api/tool', route => {
        if (route.request().postDataJSON().tool === 'graph_push') return json(route, { status: 'ok', pushed: 1, errors: 2 });
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long`);
    await page.getByRole('button', { name: 'Index current MID files' }).click();
    await expect(page.locator('#adminModal')).toContainText('separate from automatic archiving');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByText('Indexing incomplete: 1 file(s) indexed, 2 error(s).')).toBeVisible();
});

// #597: prove the shell/controller contract with an explicitly synthetic consumer.
// Memory now registers its own reads; this case then substitutes a synthetic consumer
// to isolate the shared shell/controller contract from its business requests.
for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 720, height: 450 }]) {
    test(`Portal topbar and refresh controller remain usable at ${viewport.width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport);
        await page.clock.install();
        const state = await routeConsole(page);
        await page.route('**/api/logout', route => json(route, { status: 'ok' }));
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/memory/mid`);
        await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
        await expect(page.locator('#portalOrigin')).toHaveText(ORIGIN);
        await expect(page.locator('#portalHealth')).toHaveText('Services not checked');
        await expect(page.locator('#portalAutoControl')).toBeVisible();
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(0);
        await page.evaluate(() => {
            window.__portalControllerProof = [];
            PortalRefresh.register({ refresh: async ({ automatic, isCurrent }) => {
                if (isCurrent()) window.__portalControllerProof.push(automatic);
                return { polling: { recommended: false }, jobs: [{ polling: { recommended: false } }] };
            } }, { epoch: AdminRouter.epoch, sessionGeneration: currentSessionGeneration() });
        });
        await expect(page.locator('#portalAutoControl')).toBeVisible();
        await page.locator('#portalRefresh').click();
        await expect(page.locator('#portalFreshness')).toContainText('Updated');
        expect(await page.evaluate(() => window.__portalControllerProof)).toEqual([false]);
        await page.locator('#portalAutoEnabled').check();
        await page.locator('#portalInterval').selectOption('30');
        await page.locator('#portalInterval').focus();
        await page.clock.runFor(30001);
        await expect.poll(() => page.evaluate(() => window.__portalControllerProof)).toEqual([false, true]);
        await expect(page.locator('#portalInterval')).toBeFocused();
        expect(await page.evaluate(() => JSON.parse(localStorage.getItem('hivemind.portal.autoRefresh')))).toEqual({ enabled: true, intervalSeconds: 30 });
        expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(0);
        await page.locator('#portalCheckServices').click();
        await expect(page.locator('#portalHealth')).toHaveText('Services healthy');
        expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(1);
        const layout = await page.locator('#portalTopbar').evaluate(el => ({
            left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right,
            documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
        }));
        expect(layout.left).toBeGreaterThanOrEqual(0);
        expect(layout.right).toBeLessThanOrEqual(viewport.width + 1);
        expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
        // 720 CSS pixels is the available layout at 200% zoom on a 1440px display.
        await page.screenshot({ path: testInfo.outputPath(`portal-topbar-controller-${viewport.width}.png`), fullPage: true });
        if (viewport.width === 1440) {
            await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
            await page.screenshot({ path: testInfo.outputPath('portal-topbar-zoom-200.png'), fullPage: true });
            await page.evaluate(() => { document.documentElement.style.zoom = ''; });
        }
        await page.getByRole('button', { name: 'Sign out' }).click();
        await expect(page.locator('#loginToken')).toBeFocused();
        await expect(page.locator('#portalAutoControl')).toBeHidden();
        expect(await page.evaluate(() => JSON.parse(localStorage.getItem('hivemind.portal.autoRefresh')))).toEqual({ enabled: false, intervalSeconds: 30 });
        await page.clock.runFor(60001);
        expect(await page.evaluate(() => window.__portalControllerProof)).toEqual([false, true]);
        expect(state.calls.filter(call => call.tool === 'system_health')).toHaveLength(1);
    });
}

// #601: shared launcher and real opt-in job following on existing RC2 APIs.
async function portalJobsFixture(page, options = {}) {
    const state = await routeConsole(page, options);
    const common = { space_id: 'demo', requested_by: 'visual-qa-admin', scope_label: 'Agent: visual-qa-admin',
        guarantee: 'in_memory_best_effort', requested_at: '2026-09-29T08:00:00Z', polling: { recommended: false } };
    const running = { ...common, job_id: 'portal-running', status: 'running', started_at: '2026-09-29T08:01:00Z',
        progress: { phase: 'Processing notes', notes_done: 3, notes_total: 12, batches_done: 1, batches_total: 4 } };
    const queued = { ...common, job_id: 'portal-queued', status: 'queued', queue_position: 2, queued_at: '2026-09-29T08:02:00Z' };
    const failed = { ...common, job_id: 'portal-failed', status: 'failed', finished_at: '2026-09-29T07:59:00Z',
        error: 'Retained failure: <img src=x onerror="window.__visualQaXss=1">' };
    state.lane = { space_id: 'demo', lane_state: 'running', running_job: running, queued_count: 1,
        queued_jobs: [queued], latest_jobs: [failed], guarantee: 'in_memory_best_effort' };
    state.accepted = [];
    await page.route('**/api/tool', async route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'space_info') {
            state.calls.push(req);
            return json(route, { ...spaceInfo(), consolidation_queue: state.lane });
        }
        if (req.tool === 'bank_consolidation_queues') {
            state.calls.push(req);
            return json(route, { status: 'ok', lanes: [state.lane], parallelism_model: 'one_worker_per_space', polling: { recommended: false } });
        }
        if (req.tool === 'bank_consolidation_status') {
            state.calls.push(req);
            const job = [state.lane.running_job, ...state.lane.queued_jobs, ...state.lane.latest_jobs]
                .find(item => item && item.job_id === req.arguments.job_id);
            return json(route, job || { status: 'not_found' });
        }
        if (req.tool === 'bank_consolidate') {
            state.calls.push(req);
            state.accepted.push(req.arguments);
            return json(route, { ...queued, status: 'queued', coalesced: true });
        }
        return route.fallback();
    });
    return state;
}

for (const width of [1440, 768, 390]) {
    test(`Portal jobs: progress, history and preserved reading through terminal state at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.clock.install();
        const state = await portalJobsFixture(page);
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/consolidation`);
        await expect(page.locator('#consolLanes .consol-job-row')).toHaveCount(3);
        await expect(page.locator('#consolLanes')).toContainText('Running');
        await expect(page.locator('#consolLanes')).toContainText('Queued');
        await expect(page.locator('#consolLanes')).toContainText('Position 2');
        await expect(page.locator('#consolLanes')).toContainText('Requested by visual-qa-admin');
        await expect(page.locator('#consolLanes')).toContainText('Retained failure: <img');
        await expect(page.locator('#consolLanes img')).toHaveCount(0);
        await expect(page.locator('#consolLanes [title^="2026-09-29T08:01:00Z"]')).toBeVisible();
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        await page.clock.runFor(15001);
        expect(state.calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(0);
        await page.getByRole('button', { name: 'Check files for compaction', exact: true }).click();
        await expect(page.locator('#sdConsolidationCompaction')).toContainText('Files eligible for compaction');
        await page.locator('#sdConsolidationCompaction').evaluate(el => { window.__reportNode = el; });
        await page.locator('#portalAutoEnabled').check();
        await page.locator('#portalInterval').selectOption('15');
        expect(await page.locator('#consolLanes').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.locator('#consolLanes').screenshot({ path: testInfo.outputPath(`portal-jobs-active-${width}.png`) });
        await page.getByRole('button', { name: 'Inspect job portal-running', exact: true }).click();
        await expect(page.locator('#consolJobSnapshot')).toContainText('3/12 notes');
        await page.locator('#consolJobSnapshot').evaluate(el => { window.__jobReader = el; });
        const refreshJob = page.locator('[data-action="consol-job-refresh"]');
        await refreshJob.focus();
        state.lane.running_job = { ...state.lane.running_job, progress: { ...state.lane.running_job.progress, notes_done: 6, batches_done: 2 } };
        await page.clock.runFor(15001);
        await expect(page.locator('#consolJobSnapshot')).toContainText('6/12 notes');
        await expect(refreshJob).toBeFocused();
        expect(await page.evaluate(() => window.__jobReader === document.getElementById('consolJobSnapshot'))).toBe(true);
        expect(await page.evaluate(() => window.__reportNode === document.getElementById('sdConsolidationCompaction'))).toBe(true);
        await expect(page.locator('#sdConsolidationCompaction')).toContainText('Files eligible for compaction');
        expect(state.calls.filter(call => call.tool === 'bank_consolidation_status')).toHaveLength(1);
        expect(state.calls.filter(call => call.tool === 'bank_consolidation_queues').every(call => call.arguments.space_ids === 'demo')).toBe(true);
        const finished = { ...state.lane.running_job, status: 'succeeded', finished_at: '2026-09-29T08:05:00Z',
            progress: { notes_done: 12, notes_total: 12 }, result: { notes_total: 12, notes_processed: 12 } };
        state.lane = { ...state.lane, lane_state: 'idle', running_job: null, queued_count: 0, queued_jobs: [], latest_jobs: [finished, state.lane.latest_jobs[0]] };
        await page.clock.runFor(15001);
        await expect(page.locator('#consolLanes')).toContainText('No jobs in progress');
        await expect(page.locator('#consolJobSnapshot')).toContainText('Notes processed');
        await expect(refreshJob).toBeFocused();
        const reads = state.calls.filter(call => ['bank_consolidation_queues', 'bank_consolidation_status'].includes(call.tool)).length;
        await page.clock.runFor(60001);
        expect(state.calls.filter(call => ['bank_consolidation_queues', 'bank_consolidation_status'].includes(call.tool))).toHaveLength(reads);
        expect(state.calls.filter(call => call.tool === 'bank_compact')).toHaveLength(1);
        expect(state.accepted).toHaveLength(0);
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`portal-jobs-terminal-${width}.png`), fullPage: true });
    });
}

test('Portal jobs: Home, Spaces and Space entries share explicit My notes and All agents confirmation', async ({ page }) => {
    const state = await portalJobsFixture(page);
    for (const [route, selector, scope] of [
        ['dashboard', '#dashStartConsolidationBtn', 'mine'],
        ['spaces', '[data-action="spaces-start-consolidation"]', 'all'],
        ['spaces/demo/memory/short', '[data-action="sd-confirm-consolidate"]', 'mine'],
        ['spaces/demo/consolidation', '[data-action="consol-picker"]', 'all'],
    ]) {
        await page.goto(`${ORIGIN}/admin.html#/${route}`);
        await expect(page.locator(selector)).toBeEnabled();
        const inventoryReads = state.calls.filter(call => call.tool === 'space_list').length;
        const accepted = state.accepted.length;
        await page.locator(selector).click();
        await expect(page.locator('#portalConsolidationSpace')).toHaveValue('demo');
        await expect(page.locator('#portalConsolidationScope')).toHaveValue('mine');
        await page.locator('#portalConsolidationScope').selectOption(scope);
        await expect(page.locator('#modalConfirmBtn')).toHaveText('Queue after current job');
        expect(state.calls.filter(call => call.tool === 'space_list')).toHaveLength(inventoryReads);
        expect(state.accepted).toHaveLength(accepted);
        await page.locator('#modalConfirmBtn').click();
        await expect.poll(() => state.accepted.length).toBe(accepted + 1);
        expect(state.accepted.at(-1)).toEqual({ space_id: 'demo', agent: scope === 'all' ? '' : 'visual-qa-admin' });
        await expect(page.locator('#adminModal')).toBeHidden();
        await expect(page).toHaveURL(/#\/spaces\/demo\/consolidation$/);
        expect(page.url()).not.toContain('portal-queued');
    }
});

test('Portal jobs: write permission keeps All agents and privileged tools unavailable', async ({ page }) => {
    const state = await portalJobsFixture(page, { permissions: ['read', 'write'] });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/consolidation`);
    await expect(page.getByRole('button', { name: 'Garbage-collect notes', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Repair bank', exact: true })).toBeDisabled();
    await page.locator('[data-action="consol-picker"]').click();
    await expect(page.locator('#portalConsolidationScope option[value="all"]')).toBeDisabled();
    await expect(page.locator('#portalConsolidationForm')).toContainText('All agents requires manage or admin permission.');
    await page.locator('#modalConfirmBtn').click();
    await expect.poll(() => state.accepted.length).toBe(1);
    expect(state.accepted[0]).toEqual({ space_id: 'demo', agent: 'visual-qa-admin' });
});

test('Portal jobs: Restore a backup stays within its Space and requires the exact backup confirmation', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 1000 });
    const state = await portalJobsFixture(page);
    const restores = [];
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'backup_list') {
            state.calls.push(req);
            return json(route, { status: 'ok', total: 2, backups: [
                { backup_id: 'demo/backup-601', space_id: 'demo', timestamp: '2026-09-29T08:00:00Z', files_count: 2, total_size: 160 },
                { backup_id: 'unrelated/backup-601', space_id: 'unrelated', timestamp: '2026-09-29T08:00:00Z' },
            ] });
        }
        if (req.tool === 'backup_restore') {
            restores.push(req.arguments);
            return json(route, { status: 'ok', space_id: 'demo', files_restored: 2 });
        }
        return route.fallback();
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/consolidation`);
    await page.getByRole('link', { name: 'Restore a backup', exact: true }).click();
    await expect(page.locator('#opBackupsList tbody tr')).toHaveCount(1);
    await expect(page.locator('#opBackupsList')).not.toContainText('unrelated');
    await expect(page.getByRole('button', { name: 'Back up all spaces', exact: true })).toHaveCount(0);
    expect(state.calls.filter(call => call.tool === 'space_list')).toHaveLength(0);
    expect(state.calls.filter(call => call.tool === 'backup_list').every(call => call.arguments.space_id === 'demo')).toBe(true);
    await page.getByRole('button', { name: 'Restore backup demo/backup-601', exact: true }).click();
    await expect(page.locator('#modalConfirmBtn')).toBeDisabled();
    await page.locator('#destructiveConfirmInput').fill('demo');
    await expect(page.locator('#modalConfirmBtn')).toBeDisabled();
    expect(restores).toHaveLength(0);
    await page.locator('#destructiveConfirmInput').fill('demo/backup-601');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByRole('heading', { name: 'Restore complete', exact: true })).toBeVisible();
    expect(restores).toEqual([{ backup_id: 'demo/backup-601', confirm: true }]);
    await expect(page.locator('#adminModal')).toContainText('Access was not restored.');
});


// PR #610 R1: ambiguous submission, dismissed launchers and refresh-session recovery.
test('Portal jobs F4: failed submission replaces confirmation with a working View jobs link', async ({ page }, testInfo) => {
    const state = await portalJobsFixture(page);
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (req.tool !== 'bank_consolidate') return route.fallback();
        state.calls.push(req);
        return json(route, { status: 'error', message: 'Request interrupted; its outcome is unknown.' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    await page.locator('[data-action="spaces-start-consolidation"]').click();
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByRole('heading', { name: 'Consolidation not confirmed', exact: true })).toBeVisible();
    await expect(page.locator('#adminModal')).toContainText('Request interrupted; its outcome is unknown.');
    await expect(page.locator('#portalConsolidationForm')).toHaveCount(0);
    await expect(page.locator('#modalConfirmBtn')).toHaveCount(0);
    expect(state.calls.filter(call => call.tool === 'bank_consolidate')).toHaveLength(1);
    await page.locator('#adminModal').screenshot({ path: testInfo.outputPath('consolidation-unconfirmed.png') });
    await page.getByRole('link', { name: 'View jobs', exact: true }).click();
    await expect(page).toHaveURL(/#\/spaces\/demo\/consolidation$/);
    await expect(page.locator('#adminModal')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Inspect job portal-running', exact: true })).toBeVisible();
    expect(state.calls.filter(call => call.tool === 'bank_consolidate')).toHaveLength(1);
});

test('Portal jobs F4: accepted submission stays visible after its launcher was dismissed', async ({ page }) => {
    const state = await portalJobsFixture(page);
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    await page.route('**/api/tool', async route => {
        const req = route.request().postDataJSON();
        if (req.tool !== 'bank_consolidate') return route.fallback();
        state.calls.push(req);
        await pending;
        return json(route, { status: 'queued', space_id: 'demo', job_id: 'accepted-after-close' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    await page.locator('[data-action="spaces-start-consolidation"]').click();
    await page.locator('#modalConfirmBtn').click();
    await expect.poll(() => state.calls.filter(call => call.tool === 'bank_consolidate').length).toBe(1);
    await expect(page.locator('#modalConfirmBtn')).toBeDisabled();
    await page.locator('#adminModal').getByRole('button', { name: 'Close', exact: true }).click();
    await expect(page.locator('#adminModal')).toBeHidden();
    finish();
    await expect(page.locator('#toastStack')).toContainText('Consolidation queued');
    await expect(page.locator('#adminModal')).toBeHidden();
    await expect(page).toHaveURL(/#\/spaces$/);
    expect(state.calls.filter(call => call.tool === 'bank_consolidate')).toHaveLength(1);
});

test('Portal jobs F2: missing refresh session offers sign-in recovery instead of an endless loader', async ({ page }, testInfo) => {
    const state = await portalJobsFixture(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    await expect(page.locator('[data-action="spaces-start-consolidation"]')).toBeVisible();
    await page.evaluate(() => {
        PortalRefresh.endSession();
        location.hash = '#/consolidation';
    });
    await expect(page.locator('#consolLanes')).toContainText('Sign out and sign in again.');
    await expect(page.locator('#consolLanes')).not.toContainText('Loading consolidation jobs');
    await page.locator('#consolLanes').screenshot({ path: testInfo.outputPath('consolidation-refresh-unavailable.png') });
    const queueReads = state.calls.filter(call => call.tool === 'bank_consolidation_queues').length;
    await page.evaluate(() => { location.hash = '#/spaces/demo/consolidation'; });
    await expect(page.locator('#consolFreshness')).toContainText('Sign out and sign in again.');
    await expect(page.getByRole('button', { name: 'Inspect job portal-running', exact: true })).toBeVisible();
    await expect(page.locator('#consolLanes')).not.toContainText('Loading consolidation jobs');
    expect(state.calls.filter(call => call.tool === 'bank_consolidation_queues')).toHaveLength(queueReads);
});

// #599: Home uses one bounded read cycle even while its visible lanes are idle.
async function portalHomeFixture(page) {
    const state = await routeConsole(page);
    const spaces = Array.from({ length: 21 }, (_, i) => ({ space_id: `space-${String(i + 1).padStart(2, '0')}`,
        description: `Research space ${i + 1}`, last_consolidation: new Date(Date.UTC(2026, 8, 30, 8, 0, 21 - i)).toISOString(), consolidation_count: 4, total_notes_processed: 48, live_notes_count: 25, bank_files_count: 3 }));
    state.jobs = [];
    state.queueError = false;
    state.noteErrors = new Set();
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (req.tool === 'space_list') {
            state.calls.push(req);
            return json(route, { status: 'ok', total: spaces.length, spaces });
        }
        if (req.tool === 'bank_consolidation_queues') {
            state.calls.push(req);
            if (state.queueError) return json(route, { status: 'error', message: 'Activity temporarily unavailable' });
            const ids = String(req.arguments.space_ids || '').split(',');
            return json(route, { status: 'ok', polling: { recommended: false }, lanes: spaces.filter(space => ids.includes(space.space_id)).map(space => {
                const jobs = state.jobs.filter(job => job.space_id === space.space_id);
                return { space_id: space.space_id, lane_state: jobs.some(job => job.status === 'running') ? 'running' : 'idle',
                    running_job: jobs.find(job => job.status === 'running') || null,
                    queued_count: jobs.filter(job => job.status === 'queued').length,
                    queued_jobs: jobs.filter(job => job.status === 'queued'),
                    latest_jobs: jobs.filter(job => ['succeeded', 'failed'].includes(job.status)), guarantee: 'in_memory_best_effort' };
            }) });
        }
        if (req.tool === 'live_read') {
            state.calls.push(req);
            const sid = req.arguments.space_id;
            if (state.noteErrors.has(sid)) return json(route, { status: 'error', message: `Notes unavailable for ${sid}` });
            return json(route, { status: 'ok', total: 20, has_more: true, notes: Array.from({ length: 20 }, (_, index) => ({
                filename: `${sid}-note-${index}.md`, timestamp: '2026-09-29T08:00:00Z', agent: 'research-agent', category: 'observation',
                content: index === 0 ? `Research marker ${sid}` : `Note ${index} in ${sid}: <img src=x onerror="window.__visualQaXss=1"> remains literal.`,
            })) });
        }
        return route.fallback();
    });
    state.advance = async (milliseconds = 15001) => {
        const start = state.calls.length;
        await page.clock.runFor(milliseconds);
        await expect.poll(() => state.calls.slice(start).length).toBe(5);
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        const calls = state.calls.slice(start);
        expect(calls.map(call => call.tool).sort()).toEqual(['bank_consolidation_queues', 'live_read', 'live_read', 'live_read', 'space_list']);
        expect(calls.filter(call => call.tool === 'live_read').every(call => call.arguments.limit === 20)).toBe(true);
        expect(new Set(calls.filter(call => call.tool === 'live_read').map(call => call.arguments.space_id)).size).toBe(3);
        return calls;
    };
    return state;
}

test('Portal Home: keyboard focus survives job reorder and transition to history', async ({ page }) => {
    await page.clock.install();
    const state = await portalHomeFixture(page);
    const job = { job_id: 'focus-job', space_id: 'space-01', status: 'queued', scope_label: 'My notes' };
    state.jobs = [{ ...job, job_id: 'running-first', status: 'running' }, job];
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    const details = page.locator('[data-action="dash-job"][data-job-id="focus-job"]');
    await expect(details).toBeVisible();
    await page.locator('#portalAutoEnabled').check();
    await details.focus();
    state.jobs = [{ ...job, status: 'running' }, { ...job, job_id: 'running-first', status: 'queued' }];
    await page.clock.runFor(15001);
    await expect(page.locator('#dashRunningJobs article').first()).toHaveAttribute('data-job-id', 'focus-job');
    await expect(details).toBeFocused();
    state.jobs = [{ ...job, status: 'succeeded', finished_at: '2026-09-29T08:05:00Z' }];
    await page.clock.runFor(15001);
    await expect(page.locator('#dashRecentJobs [data-action="dash-job"]')).toBeVisible();
    await expect(details).toBeFocused();
});

test('Portal Home: failed visible-card reads are not presented as empty history', async ({ page }) => {
    const state = await portalHomeFixture(page);
    state.queueError = true;
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('#dashLanesFreshness')).toContainText('Activity temporarily unavailable');
    await expect(page.locator('#dashRecentEmpty')).toContainText('not loaded');
    await expect(page.locator('#dashActivityEmpty')).toContainText('not loaded');
    await expect(page.locator('#dashRecentEmpty')).not.toContainText('restart');
    state.queueError = false;
    await page.locator('#portalRefresh').click();
    await expect(page.locator('#dashRecentEmpty')).toContainText('No completed jobs');
    state.queueError = true;
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator('#dashLanesFreshness')).toContainText('Activity temporarily unavailable');
    await expect(page.locator('#dashRecentEmpty')).toContainText('not loaded');
});

for (const width of [1440, 768, 390]) {
    test(`Portal Home: bounded idle discovery and stable reading at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await page.clock.install();
        const state = await portalHomeFixture(page);
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        const cardCount = width === 1440 ? 6 : width === 768 ? 4 : 2;
        await expect(page.locator('#dashRecentSpaces article')).toHaveCount(cardCount);
        await expect(page.locator('#dashLanesPanel')).toContainText('No jobs in progress');
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        await expect(page.locator('#dashTokensTile')).toHaveCount(0);
        const initialReads = state.calls.length;
        expect(state.calls.filter(call => call.tool === 'space_list')).toHaveLength(1);
        const firstQueue = state.calls.find(call => call.tool === 'bank_consolidation_queues');
        expect(firstQueue.arguments.space_ids.split(',')).toEqual(Array.from({ length: cardCount }, (_, index) => `space-${String(index + 1).padStart(2, '0')}`));
        await page.clock.runFor(30001);
        expect(state.calls).toHaveLength(initialReads);
        await page.locator('.dash-notes > summary').click();
        for (const sid of ['space-01', 'space-02', 'space-03']) {
            await page.locator('#dashNoteSpace').selectOption(sid);
            await page.locator('#dashAddNotes').click();
            await expect(page.locator('#dashNotesPanel').getByText(`Research marker ${sid}`, { exact: true })).toBeVisible();
        }
        await expect(page.locator('#dashNotesPanel section[data-space]')).toHaveCount(3);
        await expect(page.locator('#dashAddNotes')).toBeDisabled();
        await expect(page.locator('#dashNotesPanel img')).toHaveCount(0);
        await page.locator('#portalAutoEnabled').check();
        const job = { job_id: 'home-live', space_id: 'space-01', status: 'running', scope_label: 'Agent: research-agent',
            requested_by: 'research-agent', started_at: '2026-09-29T08:01:00Z', polling: { recommended: false },
            progress: { notes_done: 3, notes_total: 12, batches_done: 1, batches_total: 4 } };
        state.jobs = [job];
        await state.advance();
        await expect(page.locator('#dashLanesPanel article[data-job-id="home-live"]')).toContainText('3/12 notes');
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`portal-home-active-${width}.png`), fullPage: true });
        const removeNotes = page.getByRole('button', { name: 'Remove notes for space-01', exact: true });
        await removeNotes.focus();
        const scrollTop = await page.locator('.content').evaluate(el => { el.scrollTop = 450; return el.scrollTop; });
        expect(scrollTop).toBeGreaterThan(0);
        await page.evaluate(() => {
            window.__homeJob = document.querySelector('#dashLanesPanel article[data-job-id="home-live"]');
            window.__homeNote = document.querySelector('#dashNotesPanel article[data-note-id="space-01-note-0.md"]');
            window.__homeCard = document.querySelector('#dashRecentSpaces article');
        });
        state.jobs = [{ ...job, progress: { ...job.progress, notes_done: 6, batches_done: 2 } }];
        await state.advance();
        await expect(page.locator('#dashLanesPanel article[data-job-id="home-live"]')).toContainText('6/12 notes');
        await expect(removeNotes).toBeFocused();
        expect(await page.locator('.content').evaluate(el => el.scrollTop)).toBe(scrollTop);
        expect(await page.evaluate(() => !!window.__homeJob && window.__homeJob === document.querySelector('#dashLanesPanel article[data-job-id="home-live"]'))).toBe(true);
        expect(await page.evaluate(() => !!window.__homeNote && window.__homeNote === document.querySelector('#dashNotesPanel article[data-note-id="space-01-note-0.md"]'))).toBe(true);
        expect(await page.evaluate(() => !!window.__homeCard && window.__homeCard === document.querySelector('#dashRecentSpaces article'))).toBe(true);
        await page.locator('#dashLanesPanel [data-action="dash-job"][data-job-id="home-live"]').click();
        await expect(page.locator('#dashJobSnapshot')).toContainText('6/12 notes');
        await page.locator('#dashJobSnapshot').evaluate(el => { window.__homeReader = el; });
        state.jobs = [{ ...job, progress: { ...job.progress, notes_done: 9, batches_done: 3 } }];
        await state.advance();
        await expect(page.locator('#dashJobSnapshot')).toContainText('9/12 notes');
        expect(await page.evaluate(() => window.__homeReader === document.getElementById('dashJobSnapshot'))).toBe(true);
        expect(state.calls.filter(call => call.tool === 'bank_consolidation_status')).toHaveLength(0);
        await page.keyboard.press('Escape');
        const priorFreshness = await page.locator('#dashLanesFreshness').textContent();
        state.queueError = true;
        await state.advance();
        await expect(page.locator('#dashLanesFreshness')).toContainText('Activity temporarily unavailable');
        await expect(page.locator('#dashLanesPanel article[data-job-id="home-live"]')).toContainText('9/12 notes');
        expect(await page.locator('#dashLanesFreshness').textContent()).toContain(priorFreshness.trim());
        state.queueError = false;
        state.noteErrors.add('space-02');
        state.jobs = [{ ...job, status: 'succeeded', finished_at: '2026-09-29T08:05:00Z', result: { notes_total: 12, notes_processed: 12 } }];
        await state.advance(30001);
        await expect(page.locator('#dashLanesPanel')).toContainText('No jobs in progress');
        await expect(page.locator('#dashActivityPanel article[data-job-id="home-live"]')).toBeVisible();
        await expect(page.locator('#dashLanesFreshness')).not.toContainText('Activity temporarily unavailable');
        await expect(page.locator('#dashNotesPanel section[data-space="space-02"]')).toContainText('Notes unavailable for space-02');
        await expect(page.locator('#dashNotesPanel').getByText('Research marker space-02', { exact: true })).toBeVisible();
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`portal-home-${width}.png`), fullPage: true });
        expect(state.calls.filter(call => call.tool === 'space_list').every(call => call.arguments.include_counts === false)).toBe(true);
        await expect(page.locator('[data-action="dash-next-page"]')).toHaveCount(0);
        expect(state.calls.filter(call => ['admin_list_tokens', 'system_health', 'bank_consolidate'].includes(call.tool))).toHaveLength(0);
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
    });
}

// #600: the real shared controller owns only the Memory tier being read.
for (const [path, tool, selector] of [
    ['spaces/demo/memory/short', 'live_read', '#sdShortCount'],
    ['spaces/demo/memory/mid', 'bank_read', '#sdBankMarkdown h1'],
    ['dashboard', 'bank_consolidation_queues', '#dashCardScope'],
    ['consolidation', 'bank_consolidation_queues', '#consolLanes'],
]) {
    test(`Portal initial read survives hidden-to-visible with Auto-refresh off: ${path}`, async ({ page }) => {
        // Headless tabs do not reliably change OS visibility. Exercise the real
        // bundle's visibility event with a controlled document.hidden value.
        await page.addInitScript(() => {
            window.__hidden612 = true;
            Object.defineProperty(document, 'hidden', { get: () => window.__hidden612 });
        });
        const state = await routeConsole(page);
        await page.goto(`${ORIGIN}/admin.html#/${path}`);
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().available)).toBe(true);
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        expect(state.calls.filter(req => req.tool === tool)).toHaveLength(0);
        await page.evaluate(() => { window.__hidden612 = false; document.dispatchEvent(new Event('visibilitychange')); });
        await expect.poll(() => state.calls.filter(req => req.tool === tool).length).toBe(1);
        await expect(page.locator(selector)).not.toBeEmpty();
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        await page.evaluate(() => { window.__hidden612 = true; document.dispatchEvent(new Event('visibilitychange')); window.__hidden612 = false; document.dispatchEvent(new Event('visibilitychange')); });
        expect(state.calls.filter(req => req.tool === tool)).toHaveLength(1);
    });
}

test('Portal Memory: current MID index button reflects its pending mutation', async ({ page }) => {
    await routeConsole(page);
    let finish;
    await page.route('**/api/tool', route => {
        if (route.request().postDataJSON().tool !== 'graph_push') return route.fallback();
        return new Promise(resolve => { finish = async () => { await json(route, { status: 'ok', pushed: 1, errors: 0 }); resolve(); }; });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/memory/mid`);
    await expect(page.locator('#sdBankMarkdown h1')).toBeVisible();
    const button = page.locator('#sdTierPanel [data-action="sd-confirm-graph-push"]');
    await button.click();
    await page.locator('#modalConfirmBtn').click();
    await expect.poll(() => typeof finish).toBe('function');
    await expect(button).toBeDisabled();
    await finish();
    await expect(button).toBeEnabled();
});

async function screenshotMemoryRegion(page, selector, outputPath) {
    await page.locator(selector).evaluate(el => {
        const content = document.querySelector('.content');
        content.scrollTop += el.getBoundingClientRect().top - content.getBoundingClientRect().top - 16;
    });
    await page.screenshot({ path: outputPath });
}

async function portalMemoryFixture(page) {
    const state = await routeConsole(page);
    const filenames = ['activeContext.md', 'progress.md', ...Array.from({ length: 10 }, (_, i) => `chapter-${i + 1}.md`)];
    state.files = filenames.map(filename => ({ filename, size: 2400, last_modified: '2026-09-28T12:00:00Z' }));
    state.lastConsolidation = '2026-09-27T10:00:00Z';
    state.notes = [
        { filename: 'recent.md', timestamp: '2026-09-29T12:00:00Z', agent: 'research-agent', category: 'observation', tags: ['source'],
            content: 'Keep `it\'s "literal" <code>` and <img src=x onerror="window.__visualQaXss=1"> as text.\nSecond source line.' },
        ...Array.from({ length: 8 }, (_, i) => ({ filename: `older-${i}.md`, timestamp: '2026-09-28T12:00:00Z', agent: 'other-agent', category: 'decision', content: `Retained note ${i}\n` + 'Reading context.\n'.repeat(8) })),
        { filename: 'undated.md', agent: 'research-agent', category: 'observation', content: 'Undated source stays undated.' },
    ];
    state.contents = new Map(filenames.map(filename => [filename, `# ${filename}\n\n` + Array.from({ length: 50 }, (_, i) => `Paragraph ${i}: stable reading context for this selected document.`).join('\n\n')]));
    state.readError = false;
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (!['space_info', 'live_read', 'bank_list', 'bank_read'].includes(req.tool)) return route.fallback();
        state.calls.push(req);
        if (req.tool === 'space_info') return json(route, { ...spaceInfo(), last_consolidation: state.lastConsolidation });
        if (req.tool === 'live_read') {
            const { agent, category, since, limit } = req.arguments;
            const matched = state.notes.filter(note => (!agent || note.agent === agent) && (!category || note.category === category)
                && (!since || (Number.isFinite(Date.parse(note.timestamp)) && Date.parse(note.timestamp) >= Date.parse(since))));
            const notes = matched.slice(0, limit);
            return json(route, { status: 'ok', notes, total: notes.length, has_more: matched.length > limit });
        }
        if (req.tool === 'bank_list') return json(route, { status: 'ok', files: state.files, file_count: state.files.length });
        if (state.readError) return json(route, { status: 'error', message: 'Selected file temporarily unavailable' });
        return json(route, { status: 'ok', filename: req.arguments.filename, content: state.contents.get(req.arguments.filename), size: 2400 });
    });
    state.tick = async (expectedTools, duration = 15001) => {
        const start = state.calls.length;
        await page.clock.runFor(duration);
        await expect.poll(() => state.calls.slice(start).map(req => req.tool).sort()).toEqual([...expectedTools].sort());
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        return state.calls.slice(start);
    };
    return state;
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`Portal Memory: SHORT feed and selected MID remain readable through refresh at ${viewport.width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport);
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await page.clock.install();
        const state = await portalMemoryFixture(page);
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/memory/short`);
        await expect(page.locator('#sdShortBody article[data-note-id]')).toHaveCount(10);
        await expect(page.locator('#sdShortBody section[data-day]')).toHaveCount(3);
        await expect(page.locator('#sdShortSince')).toHaveValue('');
        await expect(page.locator('#sdShortAgent')).toHaveValue('');
        await expect(page.locator('#sdShortCount')).toContainText('10');
        await expect(page.locator('#sdShortBound')).toContainText('10 returned notes');
        await expect(page.locator('#sdShortBound')).not.toContainText('more notes');
        await expect(page.locator('#sdShortBody .sd-note-content code')).toHaveText('it\'s "literal" <code>');
        await expect(page.locator('#sdShortBody')).toContainText('<img src=x onerror="window.__visualQaXss=1">');
        await expect(page.locator('#sdShortBody img, #sdShortBody [data-action]')).toHaveCount(0);
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        const initialReads = state.calls.length;
        await page.clock.runFor(30001);
        expect(state.calls).toHaveLength(initialReads);
        const retainedNote = page.locator('#sdShortBody article[data-note-id="recent.md"]');
        await retainedNote.evaluate(el => { window.__memoryNote = el; window.__memoryCode = el.querySelector('code'); });
        await page.locator('#sdShortAgent').fill('unapplied-agent');
        await page.locator('#portalAutoEnabled').check();
        await page.locator('#sdShortAgent').focus();
        const shortScroll = await page.locator('#sdShortBody').evaluate(el => { el.scrollTop = 400; return el.scrollTop; });
        expect(shortScroll).toBeGreaterThan(0);
        state.notes.unshift({ filename: 'new.md', timestamp: '2026-09-29T13:00:00Z', agent: 'research-agent', category: 'progress', content: 'A genuinely later returned note.' });
        const shortTick = await state.tick(['live_read']);
        expect(shortTick[0].arguments).toMatchObject({ space_id: 'demo', limit: 50, agent: '', category: '', since: '' });
        await expect(page.locator('#sdShortAgent')).toHaveValue('unapplied-agent');
        await expect(page.locator('#sdShortAgent')).toBeFocused();
        expect(await page.evaluate(() => window.__memoryNote === document.querySelector('#sdShortBody article[data-note-id="recent.md"]'))).toBe(true);
        expect(await page.locator('#sdShortBody').evaluate(el => el.scrollTop)).toBeGreaterThanOrEqual(shortScroll);
        await expect(page.locator('#sdShortBody article[data-note-id="new.md"]')).toContainText('New');
        expect(await page.evaluate(() => window.__memoryCode === document.querySelector('#sdShortBody article[data-note-id="recent.md"] code'))).toBe(true);
        const selection = await retainedNote.locator('code').evaluate(el => {
            const range = document.createRange(); range.selectNodeContents(el);
            const selected = window.getSelection(); selected.removeAllRanges(); selected.addRange(range);
            return selected.toString();
        });
        await state.tick(['live_read']);
        expect(await page.evaluate(() => window.getSelection().toString())).toBe(selection);
        expect(await page.evaluate(() => window.__memoryCode === document.querySelector('#sdShortBody article[data-note-id="recent.md"] code'))).toBe(true);
        await page.locator('#sdShortAgent').fill('');
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.locator('#sdShortBody').evaluate(el => { el.scrollTop = 0; });
        await screenshotMemoryRegion(page, '#sdShortFreshness', testInfo.outputPath(`portal-short-${viewport.width}.png`));
        await page.locator('#sdShortAgent').fill('research-agent');
        await page.locator('#sdShortCategory').selectOption('observation');
        await page.locator('#sdShortSince').fill('2026-09-28T10:00');
        await page.locator('[data-action="sd-apply-short-filters"]').click();
        const since = await page.evaluate(() => new Date('2026-09-28T10:00').toISOString());
        await expect.poll(() => state.calls.filter(req => req.tool === 'live_read').at(-1).arguments).toMatchObject({ agent: 'research-agent', category: 'observation', since });
        await expect(page.locator('#sdShortBody article[data-note-id]')).toHaveCount(1);
        await expect(page.locator('#sdShortBody article[data-note-id="recent.md"]')).toBeVisible();
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);

        await page.getByRole('tab', { name: 'mid', exact: true }).click();
        const tabs = page.getByRole('tablist', { name: 'Memory Bank files', exact: true });
        await expect(tabs.getByRole('tab')).toHaveCount(12);
        await tabs.getByRole('tab', { name: 'chapter-10.md', exact: true }).click();
        await expect(page.locator('#sdBankMarkdown h1')).toHaveText('chapter-10.md');
        await tabs.getByRole('tab', { name: 'progress.md', exact: true }).click();
        await expect(page.locator('#sdBankMarkdown h1')).toHaveText('progress.md');
        await expect(page.locator('#sdLastConsolidation [title^="2026-09-27T10:00:00Z"]')).toBeVisible();
        await expect(page.locator('#sdBankFileMeta [title^="2026-09-28T12:00:00Z"]')).toBeVisible();
        const reader = page.locator('#sdBankPreview');
        await reader.focus();
        const midScroll = await reader.evaluate(el => { window.__memoryReader = el; window.__memoryMarkdown = document.getElementById('sdBankMarkdown').firstElementChild; el.scrollTop = 310; return el.scrollTop; });
        expect(midScroll).toBeGreaterThan(0);
        await state.tick(['bank_list', 'bank_read', 'space_info']);
        await expect(reader).toBeFocused();
        expect(await reader.evaluate(el => el.scrollTop)).toBe(midScroll);
        expect(await page.evaluate(() => window.__memoryMarkdown === document.getElementById('sdBankMarkdown').firstElementChild)).toBe(true);
        const selectedTab = tabs.getByRole('tab', { name: 'progress.md', exact: true });
        await selectedTab.focus();
        state.files = [...state.files].reverse();
        await state.tick(['bank_list', 'bank_read', 'space_info']);
        await expect(selectedTab).toBeFocused();
        await expect(selectedTab).toHaveAttribute('aria-selected', 'true');
        expect(await page.evaluate(() => window.__memoryMarkdown === document.getElementById('sdBankMarkdown').firstElementChild)).toBe(true);
        await reader.focus();
        expect(await reader.evaluate(el => el.scrollTop)).toBe(midScroll);
        state.files = state.files.map(file => file.filename === 'progress.md' ? { ...file, last_modified: '2026-09-29T14:00:00Z' } : file);
        state.contents.set('progress.md', state.contents.get('progress.md').replace('# progress.md', '# Same filename, updated content'));
        state.lastConsolidation = '2026-09-29T13:30:00Z';
        const midTick = await state.tick(['bank_list', 'bank_read', 'space_info']);
        expect(midTick.find(req => req.tool === 'bank_read').arguments.filename).toBe('progress.md');
        await expect(page.locator('#sdBankMarkdown h1')).toHaveText('Same filename, updated content');
        await expect(tabs.getByRole('tab', { name: 'progress.md', exact: true })).toHaveAttribute('aria-selected', 'true');
        await expect(reader).toBeFocused();
        expect(await reader.evaluate(el => el.scrollTop)).toBe(midScroll);
        expect(await page.evaluate(() => window.__memoryReader === document.getElementById('sdBankPreview'))).toBe(true);
        await expect(page.locator('#sdLastConsolidation [title^="2026-09-29T13:30:00Z"]')).toBeVisible();
        await expect(page.locator('#sdBankFileMeta [title^="2026-09-29T14:00:00Z"]')).toBeVisible();
        const fileFreshness = await page.locator('#sdMidFileFreshness').textContent();
        state.readError = true;
        await state.tick(['bank_list', 'bank_read', 'space_info']);
        await expect(page.locator('#sdMidFileFreshness')).toContainText('Selected file temporarily unavailable');
        await expect(page.locator('#sdMidFileFreshness')).toContainText(fileFreshness.trim());
        await expect(page.locator('#sdBankMarkdown h1')).toHaveText('Same filename, updated content');
        await expect(reader).toBeFocused();
        expect(await reader.evaluate(el => el.scrollTop)).toBe(midScroll);
        await page.locator('#portalAutoEnabled').uncheck();
        await reader.evaluate(el => { el.scrollTop = 0; });
        await page.locator('.content').evaluate(el => { el.scrollTop = 0; });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        expect(await page.locator('.content').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await screenshotMemoryRegion(page, '#sdMidBody', testInfo.outputPath(`portal-mid-${viewport.width}.png`));

        state.notes = []; state.files = []; state.readError = false;
        await page.getByRole('tab', { name: 'short', exact: true }).click();
        await expect(page.locator('#sdShortBody article')).toHaveCount(0);
        await expect(page.locator('#sdShortBody')).toContainText('No notes');
        await page.getByRole('tab', { name: 'mid', exact: true }).click();
        await expect(page.locator('#sdFileTabs [role="tab"]')).toHaveCount(0);
        await expect(page.locator('#sdMidBody')).toContainText('No bank files');
        await screenshotMemoryRegion(page, '#sdTierPanel', testInfo.outputPath(`portal-memory-empty-${viewport.width}.png`));
        expect(state.calls.filter(req => ['system_health', 'graph_status', 'bank_consolidation_queues', 'space_list', 'admin_list_tokens', 'bank_consolidate'].includes(req.tool))).toHaveLength(0);
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
    });
}

async function portalLongFixture(page) {
    const state = await routeConsole(page, { permissions: ['read', 'write'] });
    state.yaml = 'name: example\nversion: "1.0"\ndescription: \'<img src=x onerror="window.__visualQaXss=1">\'\n' + Array.from({ length: 25 }, (_, i) => `# Definition line ${i + 1}`).join('\n');
    state.content = 'Source text <img src=x onerror="window.__visualQaXss=1"> & literal `code`\n' + Array.from({ length: 60 }, (_, i) => `Evidence paragraph ${i + 1}: the text stays bound to its source.`).join('\n');
    state.documents = Array.from({ length: 51 }, (_, i) => ({
        document_id: `doc-${i + 1}`, filename: `source-${i + 1}.txt`, source_path: `research/source-${i + 1}.txt`,
        ingestion_status: 'succeeded', size_bytes: 4096, text_length: 2048, chunk_count: 3, content_type: 'text/plain',
        sha256: 'a'.repeat(64), source_modified_at: '2026-09-28T10:00:00Z', ingested_at: '2026-09-29T12:00:00Z',
    }));
    state.jobs = Array.from({ length: 51 }, (_, i) => ({
        job_id: `ingest-${i + 1}`, status: i === 0 ? 'running' : 'succeeded', filename: `source-${i + 1}.txt`,
        source_path: `research/source-${i + 1}.txt`, batch_id: 'batch-demo', current_step: i === 0 ? 'extracting' : 'complete',
        progress_percent: i === 0 ? 40 : 100, created_entities: i === 0 ? 2 : 8, created_relations: 3,
        created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:00:10Z', polling: { recommended: false },
        ...(i ? { finished_at: '2026-09-29T12:01:00Z' } : {}),
    }));
    state.hiddenActive = false;
    state.cancelled = false;
    state.cancels = [];
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON(), args = req.arguments;
        if (!['graph_status', 'ontology_list', 'ontology_get', 'ontology_validate', 'long_document_list', 'long_document_get', 'long_ingest_list', 'long_ingest_status', 'long_ingest_cancel'].includes(req.tool)) return route.fallback();
        state.calls.push(req);
        if (req.tool === 'graph_status') return json(route, { ...graphStatus(args.include_graph === true), config: { ontology: 'configured-not-in-catalogue' } });
        if (req.tool === 'ontology_list') return json(route, { status: 'ok', count: 1, ontologies: [{ name: 'example', version: '1.0', description: '<img src=x onerror="window.__visualQaXss=1">', entity_types_count: 2, relation_types_count: 1 }] });
        if (req.tool === 'ontology_get') return json(route, { status: 'ok', name: 'example', version: '1.0', entity_types_count: 2, relation_types_count: 1, content: state.yaml });
        if (req.tool === 'ontology_validate') return json(route, { status: 'ok', valid: true, errors: [], entity_types_count: 2, relation_types_count: 1 });
        if (req.tool === 'long_document_list') {
            if (state.documentsError) return json(route, { status: 'error', message: 'Document catalogue temporarily unavailable' });
            const docs = state.documents.filter(d => (!args.status || d.ingestion_status === args.status) && (!args.query || (d.filename + d.source_path).includes(args.query)));
            return json(route, { status: 'ok', space_id: 'demo', documents: docs.slice(args.offset || 0, (args.offset || 0) + args.limit), count: Math.min(args.limit, Math.max(0, docs.length - (args.offset || 0))), total_count: docs.length, limit: args.limit, offset: args.offset || 0, partial: true, warnings: ['Synthetic retained catalogue warning'] });
        }
        if (req.tool === 'long_document_get') {
            const document = state.documents.find(d => d.document_id === args.document_id || d.source_path === args.source_path);
            return json(route, { status: 'ok', document, ...(args.include_content ? { content: state.content, content_format: 'text', content_note: 'Original text' } : {}) });
        }
        if (req.tool === 'long_ingest_list') {
            let jobs = state.hiddenActive ? state.jobs.slice(1) : state.jobs;
            jobs = jobs.filter(j => (!args.status || j.status === args.status) && (!args.batch_id || j.batch_id === args.batch_id));
            return json(route, { status: 'ok', jobs: jobs.slice(args.offset || 0, (args.offset || 0) + args.limit), total: jobs.length, count: Math.min(args.limit, Math.max(0, jobs.length - (args.offset || 0))), limit: args.limit, offset: args.offset || 0, guarantee: 'best_effort_in_process', polling: { recommended: false } });
        }
        if (req.tool === 'long_ingest_status') return json(route, { ...state.jobs[0], ...(state.cancelled ? { status: 'cancelled', current_step: 'cancelled', finished_at: '2026-09-29T12:02:00Z' } : {}) });
        state.cancels.push(args);
        return json(route, { status: 'cancelling', job_id: args.job_id, message: 'Cancellation requested at the next phase boundary.' });
    });
    return state;
}

// #614 R1: Graph pagination is external data, including numeric-looking fields.
for (const [panel, tool, selector, rowsKey, totalKey] of [
    ['documents', 'long_document_list', '#sdDocumentsPage', 'documents', 'total_count'],
    ['jobs', 'long_ingest_list', '#sdIngestPage', 'jobs', 'total'],
]) {
    test(`Portal LONG: pagination rejects hostile and missing values in ${panel}`, async ({ page }) => {
        const state = await portalLongFixture(page);
        const hostile = '<b data-pagination-injection>spoof</b>';
        let payload = { offset: hostile, count: 1, [totalKey]: 1 }, reads = 0;
        await page.route('**/api/tool', route => {
            if (route.request().postDataJSON().tool !== tool) return route.fallback();
            reads++;
            return json(route, { status: 'ok', [rowsKey]: [{ ...state[rowsKey][0], filename: `pagination-probe-${reads}` }], ...payload });
        });
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/${panel}`);
        await expect(page.locator(selector)).not.toBeEmpty();
        await expect(page.locator('[data-pagination-injection]')).toHaveCount(0);
        await expect(page.locator(selector)).toContainText('Pagination unavailable');
        for (const invalid of [
            { offset: 0, count: hostile, [totalKey]: 1 },
            { [totalKey]: 1 },
            { offset: -1, count: 1, [totalKey]: 1 },
            { offset: Number.MAX_SAFE_INTEGER, count: 2, [totalKey]: 1 },
        ]) {
            payload = invalid;
            const before = reads;
            await page.locator('[data-action="sd-long-refresh"]').click();
            await expect.poll(() => reads).toBe(before + 1);
            await expect(page.locator(panel === 'documents' ? '#sdDocumentsList' : '#sdIngestList')).toContainText(`pagination-probe-${before + 1}`);
            await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
            await expect(page.locator(selector)).toContainText('Pagination unavailable');
            await expect(page.locator('[data-pagination-injection]')).toHaveCount(0);
            await expect(page.locator(selector)).not.toContainText('NaN');
        }
        payload = { offset: 0, count: 1, [totalKey]: hostile };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(page.locator(selector)).toHaveText('1–1 / —');
        await expect(page.locator('[data-pagination-injection]')).toHaveCount(0);
        payload = { offset: 0, count: 1, [totalKey]: 1 };
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(page.locator(selector)).toHaveText('1–1 / 1');
    });
}

// #458: LONG panels must keep their reads and displayed evidence separate.
for (const width of [1440, 768, 390]) {
    test(`Portal LONG: distinct readers, explicit content and bounded job follow at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: width === 768 ? 1024 : 900 });
        await page.clock.install();
        const state = await portalLongFixture(page);
        const calls = tool => state.calls.filter(call => call.tool === tool);
        const tabs = page.getByRole('tablist', { name: 'Long memory panels' });
        const action = name => page.locator(`[data-action="${name}"]`);
        const capture = async (name, selector = '#sdLongTabs', inner = null) => {
            await page.locator(selector).evaluate((el, inner) => {
                const content = document.querySelector('.content');
                content.scrollTop += el.getBoundingClientRect().top - content.getBoundingClientRect().top - 12;
                if (inner) el.scrollTop += el.querySelector(inner).getBoundingClientRect().top - el.getBoundingClientRect().top - 20;
            }, inner);
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.querySelector('.content').scrollWidth <= document.querySelector('.content').clientWidth + 1)).toBe(true);
            await page.screenshot({ path: testInfo.outputPath(`portal-long-${name}-${width}.png`) });
        };
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/overview`);
        await expect(tabs).toBeVisible();
        await expect(tabs.getByRole('tab')).toHaveText(['Overview', 'Ontology', 'Documents', 'Ingestion jobs', 'Graph']);
        await expect(page.locator('#sdLongSnapshot')).toContainText('Index current MID files');
        expect(calls('graph_status').map(call => call.arguments.include_graph)).toEqual([false]);
        for (const tab of await tabs.getByRole('tab').all()) {
            expect(await tab.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'Each LONG tab must fit its own label without overlapping another tab').toBe(true);
        }
        await expect(page.locator('#portalAutoControl')).toBeHidden();
        await capture('overview');

        await tabs.getByRole('tab', { name: 'Overview', exact: true }).focus();
        await page.keyboard.press('ArrowRight');
        await expect(tabs.getByRole('tab', { name: 'Ontology', exact: true })).toBeFocused();
        expect(calls('ontology_list')).toHaveLength(0);
        await page.keyboard.press('Enter');
        await expect(page).toHaveURL(/\/long\/ontology$/);
        await expect(page.locator('#sdOntologyConfig')).toContainText('configured-not-in-catalogue');
        await expect(page.locator('#sdOntologySelect option')).toHaveText(['Choose an ontology', 'example']);
        expect(calls('ontology_get')).toHaveLength(0);
        await page.locator('#sdOntologySelect').selectOption('example');
        await action('sd-ontology-load').click();
        await expect(page.locator('#sdOntologyDefinition pre')).toHaveText(state.yaml);
        await expect(page.locator('#sdOntologyDefinition img')).toHaveCount(0);
        await action('sd-ontology-validate').click();
        await expect(page.locator('#sdOntologyValidation')).toContainText('Valid structure');
        await expect(page.locator('#sdOntologyValidation')).toContainText('does not measure classification quality');
        expect(calls('ontology_validate').map(call => call.arguments)).toEqual([{ space_id: 'demo', content_yaml: state.yaml }]);
        await expect(page.locator('#sdOntologyConfig')).toContainText('configured-not-in-catalogue');
        await capture('ontology', '#sdOntologyDefinition');

        await tabs.getByRole('tab', { name: 'Documents', exact: true }).click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–50 / 51');
        expect(calls('long_document_get')).toHaveLength(0);
        await page.locator('[data-action="sd-documents-page"][data-step="1"]').click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('51–51 / 51');
        await expect(page.locator('#sdDocumentsList')).toContainText('source-51.txt');
        expect(calls('long_document_list').at(-1).arguments.offset).toBe(50);
        await page.locator('[data-action="sd-documents-page"][data-step="-1"]').click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–50 / 51');
        await page.locator('#sdDocumentQuery').fill('source-51');
        await action('sd-long-refresh').click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–50 / 51');
        expect(calls('long_document_list').at(-1).arguments.query || '').toBe('');
        await action('sd-documents-apply').click();
        await expect(page.locator('#sdDocumentsPage')).toHaveText('1–1 / 1');
        expect(calls('long_document_list').at(-1).arguments).toMatchObject({ space_id: 'demo', limit: 50, offset: 0, query: 'source-51' });
        await expect(page.locator('#sdDocumentsList')).toContainText('Synthetic retained catalogue warning');
        await action('sd-document-inspect').click();
        await expect(page.locator('#sdDocumentDetail')).toContainText('doc-51');
        await expect(page.locator('#sdDocumentDetail')).toContainText('Source modified');
        await expect(page.locator('#sdDocumentDetail')).toContainText('Ingested');
        expect(calls('long_document_get').map(call => call.arguments)).toEqual([{ space_id: 'demo', document_id: 'doc-51', include_content: false }]);
        await action('sd-document-content').click();
        await expect(page.locator('#sdDocumentDetail pre')).toHaveText(state.content);
        await expect(page.locator('#sdDocumentDetail img')).toHaveCount(0);
        expect(calls('long_document_get').at(-1).arguments).toEqual({ space_id: 'demo', document_id: 'doc-51', include_content: true });
        await page.locator('#sdDocumentDetail').evaluate(el => {
            el.focus(); el.scrollTop = 90; window.__longReader = el; window.__longText = el.querySelector('pre');
            const range = document.createRange(); range.setStart(window.__longText.firstChild, 0); range.setEnd(window.__longText.firstChild, 11);
            const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        });
        const docReads = calls('long_document_get').length;
        await page.clock.runFor(60001);
        expect(calls('long_document_get')).toHaveLength(docReads);
        state.documentsError = true;
        await action('sd-long-refresh').evaluate(el => el.click());
        await expect(page.locator('#sdDocumentsFreshness')).toContainText('temporarily unavailable');
        await expect(page.locator('#sdDocumentDetail')).toBeFocused();
        expect(await page.evaluate(() => window.__longReader === document.querySelector('#sdDocumentDetail') && window.__longText === document.querySelector('#sdDocumentDetail pre') && getSelection().toString() === 'Source text')).toBe(true);
        expect(await page.locator('#sdDocumentDetail').evaluate(el => el.scrollTop)).toBe(90);
        state.documentsError = false;
        await capture('documents', '#sdDocumentDetail', '.sd-long-content');

        // #629's narrow inspector is now a focused shared dialog. Close it
        // before activating another LONG tab and prove row focus restoration.
        if (width < 1200) {
            await page.keyboard.press('Escape');
            await expect(action('sd-document-inspect')).toBeFocused();
        }

        await tabs.getByRole('tab', { name: 'Ingestion jobs', exact: true }).click();
        await expect(page.locator('#sdIngestPage')).toHaveText('1–50 / 51');
        await expect(page.locator('#portalAutoEnabled')).not.toBeChecked();
        const offReads = calls('long_ingest_list').length;
        await page.clock.runFor(60001);
        expect(calls('long_ingest_list')).toHaveLength(offReads);
        await page.locator('[data-action="sd-ingest-page"][data-step="1"]').click();
        await expect(page.locator('#sdIngestPage')).toHaveText('51–51 / 51');
        await expect(page.locator('#sdIngestList')).toContainText('source-51.txt');
        await page.locator('#sdIngestStatus').selectOption('running');
        await page.locator('#sdIngestBatch').fill('batch-demo');
        await action('sd-ingest-apply').click();
        await expect(page.locator('#sdIngestPage')).toHaveText('1–1 / 1');
        await action('sd-ingest-inspect').click();
        await expect(page.locator('#sdIngestDetail')).toContainText('40%');
        expect(calls('long_ingest_status')).toHaveLength(0);
        await capture('jobs', '#sdIngestDetail');
        await page.locator('#sdIngestBatch').fill('unapplied-draft');
        await page.locator('#portalAutoEnabled').check();
        await page.locator('#sdIngestDetail').evaluate(el => { el.focus(); window.__ingestReader = el; window.__ingestProgress = el.querySelector('progress'); });
        const beforeTick = state.calls.length;
        await page.clock.runFor(15001);
        await expect.poll(() => state.calls.length).toBe(beforeTick + 1);
        expect(state.calls.at(-1)).toMatchObject({ tool: 'long_ingest_list', arguments: { status: 'running', batch_id: 'batch-demo', offset: 0 } });
        await expect(page.locator('#sdIngestDetail')).toBeFocused();
        expect(await page.evaluate(() => window.__ingestReader === document.querySelector('#sdIngestDetail') && window.__ingestProgress === document.querySelector('#sdIngestDetail progress'))).toBe(true);
        // A selected job missing from a nonempty page may receive one status read.
        Object.assign(state.jobs[1], { status: 'running', current_step: 'extracting', progress_percent: 25, finished_at: undefined });
        state.hiddenActive = true;
        const beforeMissing = state.calls.length;
        await page.clock.runFor(15001);
        await expect.poll(() => state.calls.length).toBe(beforeMissing + 2);
        expect(state.calls.slice(beforeMissing).map(call => call.tool)).toEqual(['long_ingest_list', 'long_ingest_status']);
        expect(calls('long_ingest_status').at(-1).arguments).toEqual({ space_id: 'demo', job_id: 'ingest-1' });
        await page.locator('#sdIngestDetail [data-action="sd-ingest-cancel"]').click();
        await expect(page.locator('#adminModal')).toContainText('ingest-1');
        await expect(page.locator('#adminModal')).toContainText('demo');
        expect(state.cancels).toHaveLength(0);
        await page.keyboard.press('Escape');
        expect(state.cancels).toHaveLength(0);
        await page.locator('#sdIngestDetail [data-action="sd-ingest-cancel"]').click();
        await page.locator('#modalConfirmBtn').click();
        await expect(page.locator('#adminModal')).toBeHidden();
        await expect(page.locator('#sdIngestCancelResult')).toContainText('Job status: Running');
        await expect(page.locator('#sdIngestCancelResult')).toContainText('Updated');
        await expect(page.locator('#sdIngestDetail')).toContainText('Running');
        expect(state.cancels).toEqual([{ space_id: 'demo', job_id: 'ingest-1' }]);
        await expect.poll(() => calls('long_ingest_status').length).toBe(2);
        state.cancelled = true;
        state.jobs[1].status = 'succeeded';
        await page.clock.runFor(15001);
        await expect(page.locator('#sdIngestList')).toContainText('No ingestion jobs match these filters');
        await expect(page.locator('#sdIngestDetail')).toBeHidden();
        await expect(action('sd-ingest-cancel')).toHaveCount(0);
        await expect(page.locator('#sdIngestCancelResult')).toContainText('Job status: Cancelled');
        const terminalReads = state.calls.length;
        await page.clock.runFor(60001);
        expect(state.calls).toHaveLength(terminalReads);
        expect(state.cancels).toHaveLength(1);

        expect(calls('graph_status').filter(call => call.arguments.include_graph === true)).toHaveLength(0);
        await tabs.getByRole('tab', { name: 'Graph', exact: true }).click();
        await expect(page.locator('.sd-graph-node')).toHaveCount(3);
        expect(calls('graph_status').map(call => call.arguments.include_graph)).toEqual([false, false, true]);
        const graphReads = state.calls.length;
        await page.locator('#sdGraphSearch').fill('Hivemind');
        await page.getByRole('button', { name: 'Inspect Hivemind', exact: true }).click();
        await expect(page.locator('#sdGraphDetails')).toContainText('Unified agent memory');
        await page.locator('#sdGraphFit').click();
        expect(state.calls).toHaveLength(graphReads);
        await expect(page.locator('#portalAutoControl')).toBeHidden();
        await capture('graph');
        await page.goBack();
        await expect(page).toHaveURL(/\/long\/jobs$/);
        await expect(tabs.getByRole('tab', { name: 'Ingestion jobs', exact: true })).toHaveAttribute('aria-selected', 'true');
        expect(calls('graph_status').filter(call => call.arguments.include_graph === true)).toHaveLength(1);
        expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
        expect(calls('bank_list')).toHaveLength(0);
        expect(calls('live_read')).toHaveLength(0);
        expect(calls('long_query')).toHaveLength(0);
    });
}

// #602: exercise the existing credential lifecycle through additive grants.
async function portalAccessFixture(page, { manager = false, partialCreate = false, proxyFailure = false } = {}) {
    const permissions = manager ? ['read', 'write', 'manage'] : ['read', 'write', 'manage', 'admin'];
    const state = await routeConsole(page, { permissions });
    state.secret = 'SYNTHETIC-602-ONE-TIME-TOKEN';
    state.hash = 'sha256:' + 'b'.repeat(64);
    state.created = [];
    state.grants = [];
    state.spaces = ['demo', 'bravo', 'charlie'];
    await page.addInitScript(() => {
        window.__copied602 = [];
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
            writeText: text => {
                window.__copied602.push(text);
                return window.__deferCopy602 ? new Promise(resolve => { window.__finishCopy602 = resolve; }) : Promise.resolve();
            },
        } });
    });
    await page.route('**/api/logout', route => json(route, { status: 'ok' }));
    await page.route('**/api/tool', route => {
        const req = route.request().postDataJSON();
        if (!['space_list', 'space_create', 'admin_create_token', 'token_create', 'admin_update_token', 'space_invite_token'].includes(req.tool)) return route.fallback();
        state.calls.push(req);
        if (req.tool === 'space_list') return json(route, { status: 'ok', spaces: state.spaces.map(space_id => ({ space_id })), total: state.spaces.length });
        if (req.tool === 'space_create') {
            state.spaces.push(req.arguments.space_id);
            return json(route, { status: 'created', space_id: req.arguments.space_id, token_message: 'Space ready. Choose whether to create a token.' });
        }
        if (['admin_create_token', 'token_create'].includes(req.tool)) {
            state.created.push(req);
            return json(route, { status: partialCreate ? 'partial' : 'created', recovery_required: partialCreate,
                message: partialCreate ? 'Inspect the token registry before granting access.' : '',
                token: state.secret, token_hash: state.hash, name: req.arguments.name, permissions: ['read', 'write'], space_ids: [] });
        }
        state.grants.push(req);
        const sid = req.arguments.space_ids_add || req.arguments.space_id;
        if (sid === 'charlie') return json(route, { status: 'partial', recovery_required: true, message: 'Admin must check this grant.' });
        if (sid === 'bravo' && state.grants.filter(call => (call.arguments.space_ids_add || call.arguments.space_id) === sid).length === 1) {
            if (proxyFailure) return route.fulfill({ status: 504, contentType: 'text/html', body: '<html>Gateway Timeout</html>' });
            return json(route, { status: 'error', message: 'Temporary grant failure.' });
        }
        return json(route, { status: 'ok', added: true, space_ids_added: [sid], mcp_reconnect_required: true });
    });
    state.create = async () => {
        await page.goto(`${ORIGIN}/admin.html#/access`);
        await page.getByRole('button', { name: 'Create token', exact: true }).click();
        await page.locator('#ctName').fill('onboarding-agent');
        await page.locator('#modalConfirmBtn').click();
        await expect(page.locator('#ctSecret')).toHaveText(state.secret);
    };
    return state;
}

test('Portal Access: an HTML 504 after a grant leaves its outcome unconfirmed', async ({ page }) => {
    const state = await portalAccessFixture(page, { proxyFailure: true });
    await state.create();
    await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
    await page.locator('.ao-space[value="bravo"]').check();
    await page.locator('#aoGrant').click();
    await expect(page.locator('#aoResults')).toContainText('bravo: Not confirmed — retry available');
    await expect(page.locator('#aoResults')).not.toContainText('Refused by server');
    expect(state.grants).toHaveLength(1);
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    test(`Portal Access: saved token, scoped grants and client configuration at ${viewport.width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport);
        const manager = viewport.width === 768;
        const state = await portalAccessFixture(page, { manager });
        await state.create();
        expect(state.created.map(req => req.tool)).toEqual([manager ? 'token_create' : 'admin_create_token']);
        await expect(page.locator('#ctTokenHash')).toHaveText(state.hash);
        expect(state.grants).toHaveLength(0);
        await page.locator('#ctSecret').evaluate(el => { window.__secret602Node = el; window.__oldCopy602 = document.getElementById('ctCopyBtn'); });
        await page.getByRole('button', { name: 'Copy token', exact: true }).click();
        await expect.poll(() => page.evaluate(() => window.__copied602)).toEqual([state.secret]);
        await page.locator('#toastStack').getByRole('button', { name: 'Dismiss', exact: true }).click();
        await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
        await expect(page.getByRole('heading', { name: 'Grant space access', exact: true })).toBeVisible();
        await expect(page.locator('#aoTokenHash')).toHaveText(state.hash);
        await expect(page.locator('.ao-space')).toHaveCount(3);
        expect(await page.evaluate(() => window.__secret602Node.textContent)).toBe('');
        await page.evaluate(() => window.__oldCopy602.click());
        expect(await page.evaluate(() => window.__copied602)).toEqual([state.secret]);
        await expect(page.locator('#ctSecret')).toHaveCount(0);
        await page.locator('#aoGrant').click();
        expect(state.grants).toHaveLength(0);
        for (const sid of ['demo', 'bravo', 'charlie']) await page.locator(`.ao-space[value="${sid}"]`).check();
        await page.locator('#aoGrant').click();
        await expect(page.locator('#aoResults')).toContainText('bravo: Refused by server — retry available');
        await expect(page.locator('#aoResults')).toContainText('Temporary grant failure.');
        await expect(page.locator('#aoResults')).toContainText('Needs checking');
        await expect(page.locator('#aoResults')).not.toContainText('Reconnect the MCP client');
        await expect(page.locator('#aoRetry')).toBeEnabled();
        expect(state.grants).toHaveLength(3);
        await page.locator('#aoRetry').click();
        await expect(page.locator('#aoRetry')).toBeDisabled();
        await expect(page.locator('#aoResults')).toContainText('bravo: Granted');
        await expect(page.locator('#aoGrant')).toBeEnabled();
        expect(state.grants.map(req => req.arguments.space_ids_add || req.arguments.space_id)).toEqual(['demo', 'bravo', 'charlie', 'bravo']);
        expect(state.grants.every(req => req.tool === (manager ? 'space_invite_token' : 'admin_update_token'))).toBe(true);
        for (const req of state.grants) expect(req.arguments).toEqual(manager
            ? { token_hash: state.hash, space_id: req.arguments.space_id }
            : { token_hash: state.hash, space_ids_add: req.arguments.space_ids_add });
        expect(state.created).toHaveLength(1);
        await expect(page.locator('#aoResults')).toContainText('charlie: Needs checking');
        await expect(page.locator('#aoResults')).not.toContainText('Not confirmed');
        await expect(page.locator('#aoResults')).not.toContainText('Refused by server');
        await page.screenshot({ path: testInfo.outputPath(`portal-grants-${viewport.width}.png`) });
        await page.getByRole('button', { name: 'Configure client', exact: true }).click();
        await expect(page.locator('#aoUrl')).toHaveValue('');
        await expect(page.locator('#aoSnippet')).toBeEmpty();
        await page.locator('#aoCopyConfig').click();
        await expect(page.locator('#aoConfigError')).toContainText('absolute HTTP(S) MCP URL');
        expect(await page.evaluate(() => window.__copied602)).toEqual([state.secret]);
        await page.locator('#aoUrl').fill('https://user:password@proxy.example.invalid/mcp');
        await expect(page.locator('#aoSnippet')).toBeEmpty();
        const externalUrl = 'https://proxy.example.invalid/company/hivemind/mcp';
        await page.locator('#aoUrl').fill(externalUrl);
        await expect(page.locator('#aoConfigError')).toBeHidden();
        await page.locator('#aoUrl').focus();
        await page.keyboard.press('Tab');
        await expect(page.locator('#aoClient')).toBeFocused();
        await page.keyboard.press('Shift+Tab');
        await expect(page.locator('#aoUrl')).toBeFocused();
        await expect(page.locator('#aoSnippet')).toContainText('bearer_token_env_var = "HIVEMIND_TOKEN"');
        await expect(page.locator('#aoSnippet')).toContainText(externalUrl);
        await expect(page.locator('#aoSnippet')).not.toContainText(ORIGIN);
        await expect(page.locator('#aoSnippet')).not.toContainText(state.secret);
        await page.locator('#aoCopyConfig').click();
        await expect.poll(() => page.evaluate(() => window.__copied602.length)).toBe(2);
        await page.locator('#toastStack').getByRole('button', { name: 'Dismiss', exact: true }).click();
        expect((await page.evaluate(() => window.__copied602))[1]).not.toContain(state.secret);
        await page.locator('#aoClient').selectOption('claude');
        const config = JSON.parse(await page.locator('#aoSnippet').textContent());
        expect(config.mcpServers.hivemind).toEqual({ type: 'http', url: externalUrl, headers: { Authorization: 'Bearer ${HIVEMIND_TOKEN}' } });
        await expect(page.locator('#adminModal')).toContainText('they do not verify a connection');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        expect(await page.locator('.modal-card').evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`portal-client-${viewport.width}.png`) });
        await page.getByRole('button', { name: viewport.width === 1440 ? 'Done' : 'Finish later', exact: true }).click();
        await expect(page.locator('#adminModal')).toBeHidden();
        expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))).not.toContain(state.secret);
        expect(page.url()).not.toContain(state.secret);
        expect(state.created).toHaveLength(1);
        expect(state.calls.filter(req => ['system_health', 'live_read'].includes(req.tool))).toHaveLength(0);
        if (manager) expect(state.calls.filter(req => req.tool.startsWith('admin_'))).toHaveLength(0);
    });
}

test('Portal Access: the Token ID remains copyable after the one-time secret is acknowledged', async ({ page }) => {
    const state = await portalAccessFixture(page);
    await state.create();
    await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
    await expect(page.locator('#ctSecret')).toHaveCount(0);
    await expect(page.locator('#aoTokenHash')).toHaveText(state.hash);
    await page.locator('#aoCopyHashBtn').click();
    await expect.poll(() => page.evaluate(() => window.__copied602)).toEqual([state.hash]);
});

test('Portal Access: navigation clears grant and client handoffs without retaining the Token ID', async ({ page }) => {
    const state = await portalAccessFixture(page);
    await state.create();
    await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
    await expect(page.locator('#aoTokenHash')).toHaveText(state.hash);
    await page.locator('.ao-space[value="demo"]').check();
    await page.locator('#aoGrant').click();
    await expect(page.locator('#aoResults')).toContainText('demo: Granted');
    await page.evaluate(() => {
        window.__grantHash615 = document.getElementById('aoTokenHash');
        window.__grantResults615 = document.getElementById('aoResults');
        window.__grantCopy615 = document.getElementById('aoCopyHashBtn');
        location.hash = '#/dashboard';
    });
    await expect(page).toHaveURL(/#\/dashboard$/);
    await expect(page.locator('#adminModal')).toBeHidden();
    expect(await page.evaluate(() => [window.__grantHash615.textContent, window.__grantResults615.textContent])).toEqual(['', '']);
    await page.evaluate(() => window.__grantCopy615.click());
    expect(await page.evaluate(() => window.__copied602)).toEqual([]);

    await state.create();
    await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
    await page.getByRole('button', { name: 'Configure client', exact: true }).click();
    await page.locator('#aoUrl').fill('https://proxy.example.invalid/mcp');
    await expect(page.locator('#aoSnippet')).not.toBeEmpty();
    await page.evaluate(() => {
        window.__clientSnippet615 = document.getElementById('aoSnippet');
        location.hash = '#/dashboard';
    });
    await expect(page).toHaveURL(/#\/dashboard$/);
    await expect(page.locator('#adminModal')).toBeHidden();
    expect(await page.evaluate(() => window.__clientSnippet615.textContent)).toBe('');
});

for (const dismissal of ['Close', 'Cancel', 'Escape']) {
    test(`Portal Access: ${dismissal} refreshes Access after token acknowledgement`, async ({ page }) => {
        const state = await portalAccessFixture(page);
        await state.create();
        await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
        await expect(page.getByRole('heading', { name: 'Grant space access', exact: true })).toBeVisible();
        if (dismissal === 'Cancel') {
            await page.getByRole('button', { name: 'Configure client', exact: true }).click();
            await expect(page.getByRole('heading', { name: 'Configure client', exact: true })).toBeVisible();
        }
        const before = state.calls.filter(req => req.tool === 'admin_list_tokens').length;
        if (dismissal === 'Escape') await page.keyboard.press('Escape');
        else await page.locator(`#adminModal [data-action="close-modal"]${dismissal === 'Cancel' ? ':has-text("Cancel")' : '.modal-close'}`).click();
        await expect(page.locator('#adminModal')).toBeHidden();
        await expect.poll(() => state.calls.filter(req => req.tool === 'admin_list_tokens').length).toBe(before + 1);
        expect(await page.locator('body').textContent()).not.toContain(state.secret);
    });
}

test('Portal Access: partial creation preserves both values and cannot proceed to grants', async ({ page }) => {
    const state = await portalAccessFixture(page, { manager: true, partialCreate: true });
    await state.create();
    await expect(page.getByRole('heading', { name: 'Creation needs checking — preserve both values', exact: true })).toBeVisible();
    await expect(page.locator('#ctTokenHash')).toHaveText(state.hash);
    await expect(page.locator('#adminModal')).toContainText('Inspect the token registry before granting access.');
    await page.getByRole('button', { name: 'I saved both values', exact: true }).click();
    await expect(page.locator('#adminModal')).toBeHidden();
    await expect(page.locator('#aoSpaces')).toHaveCount(0);
    expect(state.created).toHaveLength(1);
    expect(state.grants).toHaveLength(0);
});

test('Portal Access: logout destroys a secret and its captured copy handler before clipboard completion', async ({ page }) => {
    const state = await portalAccessFixture(page);
    await state.create();
    const listReads = state.calls.filter(req => req.tool === 'admin_list_tokens').length;
    await page.evaluate(() => {
        window.__deferCopy602 = true;
        window.__secret602Node = document.getElementById('ctSecret');
        window.__oldCopy602 = document.getElementById('ctCopyBtn');
    });
    await page.locator('#ctCopyBtn').click();
    await expect.poll(() => page.evaluate(() => typeof window.__finishCopy602)).toBe('function');
    await page.evaluate(() => doLogout());
    await expect(page.locator('#loginToken')).toBeVisible();
    await expect(page.locator('#adminModal')).toHaveCount(0);
    expect(await page.evaluate(() => window.__secret602Node.textContent)).toBe('');
    await page.evaluate(() => { window.__finishCopy602(); window.__oldCopy602.click(); });
    expect(await page.evaluate(() => window.__copied602)).toEqual([state.secret]);
    await expect(page.locator('#toastStack')).toBeEmpty();
    expect(state.grants).toHaveLength(0);
    expect(state.calls.filter(req => req.tool === 'admin_list_tokens')).toHaveLength(listReads);
});

test('Portal Access: a created Space opens the shared form without creating a token automatically', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await portalAccessFixture(page, { manager: true });
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    await page.getByRole('button', { name: 'Create space', exact: true }).click();
    await page.locator('#csSpaceId').fill('new-space');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByRole('heading', { name: 'Create token', exact: true })).toBeVisible();
    await expect(page.locator('#adminModal')).toContainText('Space ready. Choose whether to create a token.');
    expect(state.calls.filter(req => req.tool === 'space_create')).toHaveLength(1);
    expect(state.created).toHaveLength(0);
    await page.locator('#ctName').fill('space-reader');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.locator('#ctSecret')).toHaveText(state.secret);
    await page.getByRole('button', { name: 'I have saved it', exact: true }).click();
    await expect(page.locator('.ao-space[value="new-space"]')).toBeChecked();
    expect(state.created.map(req => req.tool)).toEqual(['token_create']);
    expect(state.grants).toHaveLength(0);
});
