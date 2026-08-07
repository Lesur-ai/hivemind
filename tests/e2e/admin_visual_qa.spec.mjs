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

async function routeConsole(page, { createToken = false } = {}) {
    const state = { calls: [] };
    await page.addInitScript(() => { window.__visualQaXss = 0; });
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        const p = url.pathname;
        if (p === '/health') return json(route, { version: 'visual-qa' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (p === '/api/tool') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.calls.push(body);
            switch (body.tool) {
            case 'system_whoami': return json(route, {
                status: 'ok', client_name: 'visual-qa-admin', auth_type: 'stored', token_hash: 'current-hash',
                permissions: ['read', 'write', 'manage', 'admin'],
            });
            case 'space_info': return json(route, spaceInfo());
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
            case 'graph_status': return json(route, graphStatus(body.arguments.include_graph === true));
            case 'space_rules': return json(route, {
                status: 'ok',
                rules: '## Consolidation rules\n\n- Keep the bank concise.\n\n<img src="x" onerror="window.__visualQaXss = 1">',
            });
            case 'backup_list': return json(route, { status: 'ok', backups: [] });
            case 'space_list': return json(route, { status: 'ok', spaces: [{ space_id: 'demo' }] });
            case 'admin_list_tokens': return json(route, {
                status: 'ok', total: 4, tokens: [
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

test('space detail is a clean Markdown reader with lazy graph exploration and simplified deletion', async ({ page }) => {
    const state = await routeConsole(page);
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/mid`);

    await expect(page.getByRole('heading', { name: 'Memory Bank' })).toBeVisible();
    await expect(page.locator('#sdBankPreview h1')).toHaveText('Current focus');
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
    await expect(page.locator('#adminModal')).toContainText('does not expose an atomic regenerate-or-replace operation');
    await page.locator('#modalConfirmBtn').click();
    await expect(page.locator('#ctName')).toHaveValue('wide-agent-replacement');
    await expect(page.locator('#ctPerms')).toHaveValue('write,read');
    await expect(page.locator('#ctSpaces')).toHaveValue('demo, bravo, charlie, delta, echo, foxtrot');
    await page.getByRole('button', { name: 'Cancel' }).click();

    const revokedRow = page.locator('#accessTable tbody tr').filter({ hasText: 'revoked-agent' });
    await revokedRow.getByLabel('Actions for token revoked-agent').click();
    await expect(actionPanel.getByRole('button', { name: /^Reactivate token/ })).toBeDisabled();
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
