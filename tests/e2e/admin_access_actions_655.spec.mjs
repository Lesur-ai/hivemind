/**
 * #655 — Access → Tokens keeps every row action menu reachable without a
 * prior horizontal scroll.
 *
 * Drives the REAL admin bundle (admin.html + admin-app.js + views-access.js +
 * admin.css, unmodified) in headless chromium with a controlled API. The
 * fixture reproduces the reported list: 18 tokens, long unbreakable names
 * and owner emails, several allowed spaces, a revoked row and the current
 * session. At scrollLeft = 0 every visible "•••" trigger must sit inside the
 * table scroller, be the topmost element at its centre, and leave the token
 * name readable; the opened menu must be entirely inside the viewport and
 * every item must be hit-testable (not painted over by later rows).
 *
 * Captures land in HIVEMIND_QA655_CAPTURE_DIR when set, otherwise in the
 * Playwright output directory, and are attached to the report.
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://admin-access-655.e2e';

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.woff2': 'font/woff2',
};

// Reference proof viewports (ADMIN_CONSOLE_DESIGN.md §2.6) plus their 200 %
// browser-zoom equivalents: half the CSS viewport at device pixel ratio 2.
const VIEWPORTS = [
    { name: 'desktop-1440x900', width: 1440, height: 900, scale: 1 },
    { name: 'narrow-768x1024', width: 768, height: 1024, scale: 1 },
    { name: 'desktop-1440x900-zoom200', width: 720, height: 450, scale: 2 },
    { name: 'narrow-768x1024-zoom200', width: 384, height: 512, scale: 2 },
];

const SPACES = ['customer-support-knowledge-production', 'research-notes-archive', 'benchmark-results', 'onboarding', 'qa'];

const TOKENS = Array.from({ length: 18 }, (_, index) => ({
    name: index % 3 === 0
        ? `orchestrator_agent_for_customer_support_production_mesh_${index}`
        : `agent-${index}`,
    hash: 'sha256:' + index.toString(16).padStart(2, '0').repeat(32),
    permissions: index % 4 === 0 ? ['read', 'write', 'manage'] : ['read', 'write'],
    space_ids: SPACES.slice(0, 1 + (index % SPACES.length)),
    email: index % 2 === 0
        ? `owner.${index}.with.a.deliberately.long.unbreakable.address@operations.example.invalid`
        : null,
    expires_at: index % 5 === 0 ? null : '2027-01-01T00:00:00Z',
    revoked: index === 5,
}));
const TARGET = TOKENS[0];

function json(route, obj) {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(obj) });
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

async function routeAccess(page) {
    const state = { tools: [] };
    await page.route('**/*', async route => {
        const p = new URL(route.request().url()).pathname;
        if (p === '/health') return json(route, { version: 'access-655' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: [] });
        if (p === '/api/tool') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.tools.push(body);
            if (body.tool === 'system_whoami') {
                return json(route, {
                    status: 'ok', client_name: 'access-655-admin', auth_type: 'stored',
                    token_hash: TARGET.hash, permissions: ['read', 'write', 'manage', 'admin'],
                });
            }
            if (body.tool === 'admin_list_tokens') return json(route, { status: 'ok', total: TOKENS.length, tokens: TOKENS });
            if (body.tool === 'space_list') {
                return json(route, { status: 'ok', total: SPACES.length, spaces: SPACES.map(space_id => ({ space_id })) });
            }
            return json(route, { status: 'error', message: 'unexpected tool ' + body.tool });
        }
        if (p === '/admin.html' || p === '/') return serveStatic(route, 'admin.html');
        if (p.startsWith('/static/')) return serveStatic(route, p.slice('/static/'.length));
        return route.fulfill({ status: 404, body: '' });
    });
    return state;
}

async function capture(page, info, name) {
    const dir = process.env.HIVEMIND_QA655_CAPTURE_DIR;
    const file = dir ? path.join(dir, name + '.png') : info.outputPath(name + '.png');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    await page.screenshot({ path: file, fullPage: false });
    await info.attach(name, { path: file, contentType: 'image/png' });
}

// Geometry of the token table as the operator sees it. A row counts as
// visible when its trigger centre is inside the viewport vertically; for those
// rows the trigger must lie inside the scroller horizontally and win the hit
// test at its centre, and the token name must start inside the scroller and
// not be covered at its first glyph.
function tableGeometry() {
    const scroller = document.querySelector('#accessTable .access-token-table .table-scroll');
    const box = scroller.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight;
    const rows = Array.from(scroller.querySelectorAll('tbody tr')).map(row => {
        const trigger = row.querySelector('.row-action-trigger').getBoundingClientRect();
        const name = row.querySelector('.token-name');
        const nameRect = name.getBoundingClientRect();
        const centreX = trigger.left + trigger.width / 2;
        const centreY = trigger.top + trigger.height / 2;
        const visible = centreY > 0 && centreY < viewportHeight;
        let triggerHit = false;
        let nameHit = false;
        if (visible && centreX > 0 && centreX < viewportWidth) {
            const hit = document.elementFromPoint(centreX, centreY);
            triggerHit = !!hit && !!hit.closest('.row-action-trigger') && hit.closest('tr') === row;
        }
        const nameY = nameRect.top + Math.min(8, nameRect.height / 2);
        if (visible && nameY > 0 && nameY < viewportHeight) {
            const hit = document.elementFromPoint(nameRect.left + 2, nameY);
            nameHit = !!hit && hit.closest('.token-name') === name;
        }
        return {
            name: name.textContent,
            visible,
            triggerLeft: trigger.left,
            triggerRight: trigger.right,
            triggerHit,
            nameLeft: nameRect.left,
            nameHit,
        };
    });
    // The pinned column hides the scrolled data beneath it instead of letting
    // it show through the trigger: every actions cell is fully opaque.
    const translucentActionCells = Array.from(scroller.querySelectorAll('tr > :last-child'))
        .filter(cell => {
            const match = /rgba?\(([^)]+)\)/.exec(getComputedStyle(cell).backgroundColor);
            const parts = match ? match[1].split(',').map(Number) : [];
            return parts.length === 4 ? parts[3] < 1 : parts.length !== 3;
        }).length;
    return {
        translucentActionCells,
        scrollLeft: scroller.scrollLeft,
        overflow: scroller.scrollWidth - scroller.clientWidth,
        left: box.left,
        right: box.right,
        viewportWidth,
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        rows,
    };
}

function expectActionsReachable(geometry, label) {
    expect(geometry.pageOverflow, `${label}: page must not scroll horizontally`).toBeLessThanOrEqual(0);
    const visible = geometry.rows.filter(row => row.visible);
    expect(visible.length, `${label}: at least one row must be on screen`).toBeGreaterThan(0);
    for (const row of visible) {
        const where = `${label} row ${row.name}`;
        expect(row.triggerLeft, `${where}: trigger starts inside the table`).toBeGreaterThanOrEqual(geometry.left - 0.5);
        expect(row.triggerRight, `${where}: trigger ends inside the table`).toBeLessThanOrEqual(geometry.right + 0.5);
        expect(row.triggerRight, `${where}: trigger ends inside the viewport`).toBeLessThanOrEqual(geometry.viewportWidth + 0.5);
        expect(row.triggerHit, `${where}: trigger is the topmost element at its centre`).toBe(true);
    }
    expect(geometry.translucentActionCells, `${label}: actions cells must hide the data scrolled beneath them`).toBe(0);
}

// Every action in the open menu is inside the viewport and is the topmost
// element across its whole width: the panel's right edge overlaps the pinned
// actions column of later rows, so a centre-only probe would miss a panel
// painted under those cells. Items of the narrow action sheet are brought into
// view inside the sheet's own scroller first.
function openPanelGeometry() {
    const panel = document.querySelector('.row-action-menu[open] .row-action-menu-panel');
    if (!panel) return null;
    const rect = panel.getBoundingClientRect();
    const items = Array.from(panel.querySelectorAll('.row-action-item'))
        .filter(item => getComputedStyle(item).display !== 'none')
        .map(item => {
            item.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            const r = item.getBoundingClientRect();
            const y = r.top + r.height / 2;
            let hit = r.width > 12;
            for (let x = r.left + 4; x <= r.right - 4; x += 4) {
                const top = document.elementFromPoint(x, y);
                if (!top || top.closest('.row-action-item') !== item) hit = false;
            }
            return { text: item.textContent.trim(), hit };
        });
    return {
        left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
        viewportWidth: document.documentElement.clientWidth,
        viewportHeight: window.innerHeight,
        items,
    };
}

for (const viewport of VIEWPORTS) {
    test.describe(`#655 token actions at ${viewport.name}`, () => {
        test.use({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.scale });

        test('actions stay reachable at scrollLeft 0 and the open menu is fully usable', async ({ page }, testInfo) => {
            await routeAccess(page);
            await page.goto(`${ORIGIN}/admin.html#/access`);
            await expect(page.locator('#accessCount')).toHaveText(String(TOKENS.length));
            await expect(page.locator('#accessTable tbody tr')).toHaveCount(TOKENS.length);
            await page.evaluate(() => document.fonts && document.fonts.ready);

            const initial = await page.evaluate(tableGeometry);
            // Non-vacuous precondition: the data really overflows its scroller.
            expect(initial.overflow, 'fixture must overflow the table scroller').toBeGreaterThan(0);
            expect(initial.scrollLeft).toBe(0);
            expectActionsReachable(initial, `${viewport.name} scrollLeft=0`);
            for (const row of initial.rows.filter(r => r.visible)) {
                expect(row.nameLeft, `token ${row.name} name starts inside the table`).toBeGreaterThanOrEqual(initial.left - 0.5);
                expect(row.nameHit, `token ${row.name} name is not covered`).toBe(true);
            }
            await capture(page, testInfo, `${viewport.name}-scroll-left-0`);

            // Overflowing data still scrolls internally; the actions stay put.
            await page.locator('#accessTable .table-scroll').evaluate(el => { el.scrollLeft = el.scrollWidth; });
            const scrolled = await page.evaluate(tableGeometry);
            expect(scrolled.scrollLeft).toBeGreaterThan(0);
            expectActionsReachable(scrolled, `${viewport.name} scrolled right`);
            // A hovered row keeps its actions cell opaque over the scrolled data.
            await page.locator('#accessTable tbody tr').first().locator('.token-name').hover();
            expectActionsReachable(await page.evaluate(tableGeometry), `${viewport.name} hovered row`);
            await page.locator('#accessTable .table-scroll').evaluate(el => { el.scrollLeft = 0; });

            // Keyboard: focusing and opening the first row's menu needs no
            // horizontal scroll and targets that exact token.
            const row = page.locator('#accessTable tbody tr').first();
            const menu = row.locator('.row-action-menu');
            const trigger = row.getByLabel(`Actions for token ${TARGET.name}`);
            await expect(menu).toHaveAttribute('data-hash', TARGET.hash);
            await trigger.focus();
            await expect(trigger).toBeFocused();
            await trigger.press('Enter');
            await expect(menu).toHaveAttribute('open', '');
            expect(await page.locator('#accessTable .table-scroll').evaluate(el => el.scrollLeft)).toBe(0);
            await expect.poll(async () => {
                const panel = await page.evaluate(openPanelGeometry);
                return panel && panel.items.length > 0 && panel.items.every(item => item.hit);
            }, { message: 'every open-menu item is the topmost element across its whole width' }).toBe(true);
            const panel = await page.evaluate(openPanelGeometry);
            expect(panel.left).toBeGreaterThanOrEqual(0);
            expect(panel.top).toBeGreaterThanOrEqual(0);
            expect(panel.right).toBeLessThanOrEqual(panel.viewportWidth);
            expect(panel.bottom).toBeLessThanOrEqual(panel.viewportHeight);
            for (const item of panel.items) expect(item.hit, `menu item "${item.text}" is not covered`).toBe(true);
            await capture(page, testInfo, `${viewport.name}-menu-open`);

            await page.keyboard.press('Tab');
            const edit = page.locator('.row-action-menu[open] .row-action-menu-panel').getByRole('button', { name: /^Edit token/ });
            await expect(edit).toBeFocused();
            await page.keyboard.press('Enter');
            await expect(page.getByRole('heading', { name: 'Edit token' })).toBeVisible();
            await expect(page.locator('#adminModal .mono-block')).toHaveText(TARGET.name);
            await page.getByRole('button', { name: 'Cancel' }).click();

            await trigger.focus();
            await trigger.press('Enter');
            await expect(menu).toHaveAttribute('open', '');
            await page.keyboard.press('Escape');
            await expect(menu).not.toHaveAttribute('open', '');
            await expect(trigger).toBeFocused();
        });
    });
}
