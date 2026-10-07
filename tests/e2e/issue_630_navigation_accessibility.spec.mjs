import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://issue-630-navigation.e2e';
const CAPTURE_DIR_OVERRIDE = process.env.ISSUE_630_CAPTURE_DIR;

const NAV_NAMES = [
    'Dashboard', 'Spaces', 'Consolidation', 'Audit', 'Access',
    'Backups', 'Maintenance', 'Live viewer',
];

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.woff2': 'font/woff2',
};

function json(route, body, status = 200) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function serveStatic(route, relativePath) {
    const file = path.join(STATIC, relativePath);
    if (!file.startsWith(STATIC) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        return route.fulfill({ status: 404, body: 'not found' });
    }
    return route.fulfill({
        status: 200,
        contentType: CONTENT_TYPE[path.extname(file)] || 'application/octet-stream',
        body: fs.readFileSync(file),
    });
}

async function servePortal(page, { permissions = ['read', 'write', 'manage', 'admin'], meshAvailable = true } = {}) {
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/health') return json(route, { version: 'issue-630-test' });
        if (url.pathname === '/api/spaces') return json(route, { status: 'ok', spaces: [] });
        if (url.pathname === '/api/admin/mesh/availability') {
            return meshAvailable ? json(route, { status: 'ok' }) : route.fulfill({ status: 404, body: '' });
        }
        if (url.pathname === '/api/tool') {
            const { tool } = JSON.parse(route.request().postData() || '{}');
            if (tool === 'system_whoami') {
                return json(route, { status: 'ok', client_name: 'issue-630', auth_type: 'stored', permissions });
            }
            if (tool === 'space_list') return json(route, { status: 'ok', total: 0, spaces: [] });
            return json(route, { status: 'ok' });
        }
        if (url.pathname === '/admin.html' || url.pathname === '/') return serveStatic(route, 'admin.html');
        if (url.pathname.startsWith('/static/')) return serveStatic(route, url.pathname.slice('/static/'.length));
        return route.fulfill({ status: 404, body: '' });
    });
}

async function expectNavigationNames(page, { mesh = true } = {}) {
    for (const name of NAV_NAMES) {
        await expect(page.getByRole('link', { name, exact: true })).toHaveCount(1);
    }
    const links = page.locator('#sidebar a');
    const observedNames = await observedSidebarLinkNames(page);
    const expectedCount = NAV_NAMES.length + (mesh ? 1 : 0) + 1; // brand link
    await expect(links).toHaveCount(expectedCount);
    expect(observedNames).toHaveLength(expectedCount);
    expect(observedNames.every(name => typeof name === 'string' && name.trim().length > 0),
        `every sidebar anchor has an accessible name: ${JSON.stringify(observedNames)}`).toBe(true);
    expect(new Set(observedNames).size, 'sidebar link names are unique').toBe(expectedCount);
    for (const name of observedNames) {
        await expect(page.getByRole('link', { name, exact: true })).toHaveCount(1);
    }
    expect(observedNames).toContain('hivemind');
    if (mesh) await expect(page.getByRole('link', { name: 'Mesh', exact: true })).toHaveCount(1);
    else await expect(page.getByRole('link', { name: 'Mesh', exact: true })).toHaveCount(0);
    return observedNames;
}

async function observedSidebarLinkNames(page) {
    const cdp = await page.context().newCDPSession(page);
    try {
        const { root } = await cdp.send('DOM.getDocument');
        const { nodeIds } = await cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: '#sidebar a' });
        const names = [];
        for (const nodeId of nodeIds) {
            const { object } = await cdp.send('DOM.resolveNode', { nodeId });
            const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { objectId: object.objectId, fetchRelatives: false });
            const link = nodes.find(node => node.role?.value === 'link');
            names.push(link?.name?.value ?? '');
        }
        return names;
    } finally {
        await cdp.detach();
    }
}

test('issue #630 names every shell navigation link and preserves keyboard, route, and permission behavior', async ({ page }, testInfo) => {
    const captureDir = CAPTURE_DIR_OVERRIDE || testInfo.outputPath('issue-630');
    fs.mkdirSync(captureDir, { recursive: true });
    await servePortal(page);

    const captures = [];
    for (const viewport of [
        { width: 1440, height: 900, label: '1440x900' },
        { width: 768, height: 1024, label: '768x1024' },
        { width: 390, height: 844, label: '390x844' },
    ]) {
        await page.setViewportSize(viewport);
        await page.goto(`${ORIGIN}/admin.html#/spaces`);
        await expect(page.locator('#sidebar a[data-nav="spaces"]')).toHaveAttribute('aria-current', 'page');
        await page.screenshot({ path: path.join(captureDir, `after-${viewport.label}-rail.png`), fullPage: true });

        if (viewport.width <= 1023) {
            await expect(page.locator('#sidebar .nav-label').first()).toHaveCSS('display', 'none');
            await expectNavigationNames(page);
            const toggle = page.getByRole('button', { name: 'Menu', exact: true });
            await expect(toggle).toHaveAttribute('aria-expanded', 'false');
            await toggle.focus();
            await page.keyboard.press('Enter');
            await expect(page.locator('#sidebar')).toHaveClass(/sidebar--drawer-open/);
            await expect(page.locator('#sidebar a.nav-item').first()).toBeFocused();
            await expect(page.locator('#sidebar .nav-label').first()).toHaveCSS('display', 'block');
            await expectNavigationNames(page);
            await page.screenshot({ path: path.join(captureDir, `after-${viewport.label}-menu-open.png`), fullPage: true });

            await page.keyboard.press('Tab');
            const spaces = page.getByRole('link', { name: 'Spaces', exact: true });
            await expect(spaces).toBeFocused();
            await expect(spaces).toHaveCSS('outline-style', 'solid');

            const audit = page.getByRole('link', { name: 'Audit', exact: true });
            await audit.focus();
            await page.keyboard.press('Enter');
            await expect(page).toHaveURL(/#\/audit$/);
            await expect(page.locator('#sidebar a[data-nav="audit"]')).toHaveAttribute('aria-current', 'page');
            await expect(toggle).toHaveAttribute('aria-expanded', 'false');
            await expect(page.locator('#content')).toBeFocused();
        } else {
            await expectNavigationNames(page);
            await page.goto(`${ORIGIN}/admin.html#/dashboard`);
            await expect(page).toHaveURL(/#\/dashboard$/);
            await expectNavigationNames(page);
            const spaces = page.getByRole('link', { name: 'Spaces', exact: true });
            await spaces.focus();
            await expect(spaces).toBeFocused();
            await page.keyboard.press('Enter');
            await expect(page).toHaveURL(/#\/spaces$/);
            await expect(spaces).toHaveAttribute('aria-current', 'page');
        }

        captures.push({ viewport: viewport.label, route: new URL(page.url()).hash, names: await observedSidebarLinkNames(page), mesh: true });
    }

    // 720×450 CSS pixels verifies the reflow expected at 200% of 1440×900; it does not change native browser zoom.
    await page.setViewportSize({ width: 720, height: 450 });
    await page.goto(`${ORIGIN}/admin.html#/spaces`);
    await expectNavigationNames(page);
    await page.getByRole('button', { name: 'Menu', exact: true }).click();
    await expectNavigationNames(page);
    await page.screenshot({ path: path.join(captureDir, 'reflow-200-percent-equivalent-720x450.png'), fullPage: true });
    captures.push({ viewport: '200% (720x450 CSS px)', route: new URL(page.url()).hash, names: await observedSidebarLinkNames(page), mesh: true });

    // The existing capability-gated Mesh link stays absent for non-admin identities.
    const nonAdminPage = await page.context().newPage();
    await servePortal(nonAdminPage, { permissions: ['read', 'write'], meshAvailable: false });
    await nonAdminPage.setViewportSize({ width: 390, height: 844 });
    await nonAdminPage.goto(`${ORIGIN}/admin.html#/spaces`);
    await expectNavigationNames(nonAdminPage, { mesh: false });
    await expect(nonAdminPage.locator('#sidebar a[data-nav="backups"]')).toBeVisible();
    await expect(nonAdminPage.locator('#sidebar a[data-nav="maintenance"]')).toBeVisible();
    await nonAdminPage.close();

    fs.writeFileSync(path.join(captureDir, 'accessible-navigation.json'), `${JSON.stringify(captures, null, 2)}\n`);
});
