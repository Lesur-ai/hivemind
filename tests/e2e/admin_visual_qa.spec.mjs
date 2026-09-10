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

async function routeConsole(page, { createToken = false, meshSource = null, compactResponse = null, permissions = ['read', 'write', 'manage', 'admin'], spaceData = null, accessTokens = null, rules = null } = {}) {
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
                content: body.arguments.filename === 'activeContext.md'
                    ? '# Current focus\n\n**Ready for visual QA.**'
                    : '# Progress\n\nSecond file selected.',
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
            case 'graph_status': return json(route, graphStatus(body.arguments.include_graph === true));
            case 'space_rules': return json(route, {
                status: 'ok',
                rules: rules ?? '## Consolidation rules\n\n- Keep the bank concise.\n\n<img src="x" onerror="window.__visualQaXss = 1">',
            });
            case 'backup_list': return json(route, { status: 'ok', backups: [] });
            case 'space_list': return json(route, { status: 'ok', total: 1, spaces: [{ space_id: 'demo', live_notes_count: 12, bank_files_count: 6 }] });
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
test(`space detail keeps auxiliary panels inside the content frame at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await routeConsole(page, {
        rules: '## Consolidation rules\n\nKeep file hierarchy visible.\n\n```text\nprojectbrief.md ' + 'long-code-line-'.repeat(12) + '\n```',
        accessTokens: Array.from({ length: 7 }, (_, index) => ({
            name: `qa-agent-${index}-${'x'.repeat(64)}`,
            permissions: index === 2 ? ['read', 'write'] : ['read', 'write', 'manage'],
            space_ids: ['demo'],
            revoked: index === 2,
        })),
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('#sdAccessPanel tbody tr')).toHaveCount(7);
    await expect(page.locator('#sdRulesPanel pre')).toContainText('projectbrief.md');
    await page.evaluate(() => document.fonts.ready);

    const layout = await page.evaluate(() => {
        const content = document.querySelector('.content');
        const grid = document.querySelector('.sd-aux-grid');
        const gridRect = grid.getBoundingClientRect();
        const frameRect = document.querySelector('.sd-tier-card').getBoundingClientRect();
        return {
            viewportWidth: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            contentWidth: content.clientWidth,
            contentScrollWidth: content.scrollWidth,
            frameLeft: frameRect.left,
            frameRight: frameRect.right,
            gridLeft: gridRect.left,
            gridRight: gridRect.right,
            panels: [...grid.querySelectorAll('.panel')].map(panel => {
                const rect = panel.getBoundingClientRect();
                return { left: rect.left, right: rect.right };
            }),
        };
    });

    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth);
    expect(layout.contentScrollWidth).toBeLessThanOrEqual(layout.contentWidth);
    expect(Math.abs(layout.gridLeft - layout.frameLeft)).toBeLessThanOrEqual(1);
    expect(Math.abs(layout.gridRight - layout.frameRight)).toBeLessThanOrEqual(1);
    expect(layout.panels).toHaveLength(4);
    for (const panel of layout.panels) {
        expect(panel.left).toBeGreaterThanOrEqual(layout.gridLeft - 1);
        expect(panel.right).toBeLessThanOrEqual(layout.gridRight + 1);
    }
    for (const selector of ['#sdAccessPanel .table-scroll', '#sdRulesPanel pre']) {
        const scroll = await page.locator(selector).evaluate(el => {
            el.scrollLeft = el.scrollWidth;
            const end = el.scrollLeft;
            el.scrollLeft = 0;
            return end;
        });
        expect(scroll).toBeGreaterThan(0);
    }
    await page.locator('.sd-aux-grid').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: testInfo.outputPath('space-detail-auxiliary-contained.png') });
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

    await page.getByRole('button', { name: 'Read progress.md' }).click();
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Progress');
    await expect(page.locator('#sdRulesPanel h2')).toHaveText('Consolidation rules');
    await expect(page.locator('#sdRulesPanel img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__visualQaXss)).toBe(0);
    await page.getByRole('button', { name: 'Edit rules' }).click();
    await expect(page.locator('#sdRulesInput')).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('tab', { name: 'long' }).click();
    await expect(page.getByRole('heading', { name: 'Graph explorer' })).toBeVisible();
    await expect(page.locator('.sd-graph-node')).toHaveCount(3);
    await page.getByRole('button', { name: 'Inspect Architecture.md' }).click();
    await expect(page.locator('#sdGraphDetails')).toContainText('Architecture.md');
    await expect(page.getByText('Top entities')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Disconnect binding' })).toHaveCount(0);
    expect(state.calls.some(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toBe(true);

    await expect(page.locator('#sdAccessPanel')).not.toContainText('internal-long');
    await page.locator('.sd-danger-zone').getByRole('button', { name: 'Delete space' }).click();
    await expect(page.locator('.destructive-summary')).not.toContainText('Quiescence');
    await expect(page.locator('.typed-challenge')).toHaveText('"demo"');
    expect(await page.locator('.typed-challenge').evaluate(el => getComputedStyle(el).textTransform)).toBe('none');
    await page.locator('#destructiveConfirmInput').fill('Demo');
    await expect(page.locator('#modalConfirmBtn')).toBeDisabled();
    await page.locator('#destructiveConfirmInput').fill('demo');
    await expect(page.locator('#modalConfirmBtn')).toBeEnabled();
});

test('space detail hides manual compaction from a write-only session', async ({ page }) => {
    await routeConsole(page, { permissions: ['read', 'write'] });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);

    await expect(page.getByRole('heading', { name: 'Memory Bank' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Check files for compaction' })).toHaveCount(0);
    await expect(page.locator('[data-action="sd-compact-dry"]')).toHaveCount(0);
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
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.locator('#accessCount')).toHaveText('3');
    await expect(bodyPanels).toHaveCount(0);
    await expect(page.locator('.row-action-menu-panel:visible')).toHaveCount(0);

    await wideTrigger.click();
    await page.evaluate(() => { location.hash = '#/dashboard'; });
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
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
        const queue = await page.getByRole('heading', { name: 'Consolidation', exact: true }).boundingBox();
        expect(tier.y).toBeLessThan(queue.y);
        if (viewport.width === 1440) expect(tier.y).toBeLessThan(400);
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
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
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

test('accessibility: memory tabs retain focus and activate manually without speculative Long reads', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    const mid = page.getByRole('tab', { name: 'mid', exact: true });
    const long = page.getByRole('tab', { name: 'long', exact: true });
    const short = page.getByRole('tab', { name: 'short', exact: true });
    await mid.focus();
    await page.keyboard.press('Enter');
    await expect(mid).toBeFocused();
    await expect(mid).toHaveAttribute('aria-controls', 'sdTierPanel');
    await expect(page.locator('#sdTierPanel')).toHaveAttribute('aria-labelledby', await mid.getAttribute('id'));
    await page.keyboard.press('End');
    await expect(long).toBeFocused();
    await expect(mid).toHaveAttribute('aria-selected', 'true');
    expect(state.calls.filter(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toHaveLength(0);
    await page.keyboard.press('Home');
    await expect(short).toBeFocused();
    await page.keyboard.press('ArrowLeft');
    await expect(long).toBeFocused();
    await page.keyboard.press(' ');
    await expect(long).toBeFocused();
    await expect(long).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('#sdTierPanel')).toHaveAttribute('aria-labelledby', await long.getAttribute('id'));
    await expect(page.locator('.sd-graph-node')).toHaveCount(3);
    expect(state.calls.filter(call => call.tool === 'graph_status' && call.arguments.include_graph === true)).toHaveLength(1);
    await page.keyboard.press('ArrowRight');
    await expect(short).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(short).toBeFocused();
    await expect(page.locator('.sd-tier-tab[tabindex="0"]')).toHaveCount(1);
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
    const login = page.locator('#loginBtn');
    await expect(token).toBeFocused();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await page.keyboard.press('Shift+Tab');
    await expect(login).toBeFocused();
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
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
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
        await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
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
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
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
    await expect(page.locator('.login-card')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.locator('.login-card')).toBeFocused();
    await page.evaluate(() => {
        document.getElementById('loginToken').disabled = false;
        document.getElementById('loginBtn').disabled = false;
    });
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginToken')).toBeFocused();
    await page.keyboard.type('E2E-OPERATOR-TOKEN');
    await page.keyboard.press('Tab');
    await expect(page.locator('#loginBtn')).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#loginOverlay')).toBeHidden();
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
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
    await routeConsole(page);
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
    await page.getByRole('button', { name: 'Sign out' }).focus();
    await page.keyboard.press('Enter');
    const token = page.locator('#loginToken');
    const login = page.locator('#loginBtn');
    await expect(token).toBeFocused();
    await expect(login).toBeDisabled();
    await expect(page.locator('#identityBlock')).toBeEmpty();
    await expect(page.locator('#sdTierPanel')).toHaveCount(0);
    await expect(page.locator('.app')).toHaveAttribute('inert', '');
    await expect(page).toHaveURL(/#\/spaces\/demo\/mid$/);
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
    await expect(page.getByRole('heading', { name: 'Recent consolidation jobs' })).toBeVisible();
    await expect(page.locator('#dashSpacesTile')).toContainText('12 notes · 6 bank files');
    await expect(page.locator('#dashLanesPanel')).toContainText('1 worker per space');
    await expect(page.locator('#dashLanesPanel')).toContainText('batch size: 3 notes');
    await expect(page.locator('#dashActivityPanel .dash-activity-row')).toHaveCount(10);
    await expect(page.locator('#dashActivityPanel')).toContainText('History is limited and cleared when the server restarts.');
    const count = tool => state.calls.filter(call => call.tool === tool).length;
    expect(count('system_health')).toBe(1);
    await page.getByRole('button', { name: 'Refresh overview', exact: true }).click();
    await expect.poll(() => count('bank_consolidation_queues')).toBe(2);
    expect(count('system_health')).toBe(1);
    await page.getByRole('button', { name: 'Check services', exact: true }).click();
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
    test('dashboard raw worker and history diagnostics are reachable by touch', async ({ page }, testInfo) => {
        await routeConsole(page);
        const rawModel = 'future_worker<untrusted>';
        await page.route('**/api/tool', route => {
            const request = route.request().postDataJSON();
            if (request.tool !== 'bank_consolidation_queues') return route.fallback();
            return json(route, { status: 'ok', parallelism_model: rawModel, total_spaces: 1, lanes: [{ latest_jobs: [
                { job_id: 'job-1', space_id: 'demo', status: 'succeeded', finished_at: '2026-09-09T12:00:00Z' },
            ] }] });
        });
        await page.goto(`${ORIGIN}/admin.html#/dashboard`);
        const lanes = page.locator('#dashLanesPanel');
        await expect(lanes).toContainText('Worker configuration unavailable');
        const details = lanes.locator('details');
        await expect(details).toHaveJSProperty('open', false);
        await lanes.screenshot({ path: testInfo.outputPath('mobile-worker-details-closed.png') });
        await details.locator('summary').tap();
        await expect(details.getByText(rawModel, { exact: true })).toBeVisible();
        await expect(details.locator('untrusted')).toHaveCount(0);
        await lanes.screenshot({ path: testInfo.outputPath('mobile-worker-details-open.png') });
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
        lane_state: 'running', running_job: { job_id: 'job_RAW_17', scope_label: 'agent_RAW_17', progress: { notes_done: 3, notes_total: 8 } }, queued_count: 2,
        service_config: { batch_size: 3 }, guarantee: 'in_memory_best_effort', latest_jobs: [],
    };
    await routeConsole(page, { spaceData });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);
    await expect(page.locator('.sd-lane-grid .micro-label')).toHaveText(['Scope', 'Notes processed', 'Queued']);
    await expect(page.locator('.sd-lane-grid .mono-data')).toHaveText(['agent_RAW_17', '3 / 8', '2']);
    await expect(page.locator('#sdTierPanel .panel-header .micro-label')).toHaveText('MID');
    await expect(page.locator('#sdTierPanel .panel-header .micro-label')).toHaveCSS('font-family', /JetBrains Mono/);
    const label = page.locator('.sd-lane-grid .micro-label').first();
    await expect(label).toHaveCSS('font-family', /Hanken Grotesk/);
    await expect(label).toHaveCSS('font-size', '13px');
    await expect(label).toHaveCSS('line-height', '18px');
    await expect(label).toHaveCSS('font-weight', '500');
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
