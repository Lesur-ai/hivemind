/**
 * P10-4 (#192) Mesh view — REAL-SHELL integration proof (P8-7 gate pattern).
 *
 * The node:vm harness (tests/js/admin_mesh_runtime.mjs) proves views-mesh.js's
 * logic fast and mutation-proof against a stubbed shell, calling the registered
 * view functions directly. It cannot prove behaviours the REAL shell owns: hash
 * routing through admin-app.js's `_matchRoute`/`AdminRouter._dispatch`, the
 * capability-gated sidebar nav item appearing/disappearing based on a REAL
 * `GET /api/admin/mesh/status`, and the real `showDestructiveModal`'s
 * disabled-until-typed-match confirm button. This spec drives the REAL bundle
 * (admin.html + admin-api.js + admin-app.js + every view module, unmodified) in
 * headless chromium.
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://admin-mesh.e2e';

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
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

const FINGERPRINT = 'hm1:' + 'a'.repeat(64);

function statusBody(pairings = [], healthy = true) {
    return {
        status: 'ok', enabled: true, healthy,
        display_name: 'e2e-instance', public_url: 'https://source.e2e', fingerprint: FINGERPRINT,
        pairings,
    };
}

// Wires the standard boot sequence (health/whoami/spaces) plus a controllable
// Mesh admin surface. `meshEnabled: false` makes /api/admin/mesh/status 404,
// exactly like the real middleware when HIVEMIND_MESH_ENABLED=false (it is
// never mounted at all — see mesh_admin.py / server.py).
async function routeShell(page, { permissions, meshEnabled, pairings = [], members = null, meshHealthy = true, invitationSecret = 'E2E-SECRET-VALUE' }) {
    const state = { tools: [], meshCalls: [] };
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        const p = url.pathname;

        if (p === '/health') return json(route, { version: 'e2e' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: [] });

        if (p === '/api/tool') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.tools.push(body.tool);
            if (body.tool === 'system_whoami') {
                return json(route, {
                    status: 'ok', client_name: 'admin-e2e', auth_type: 'stored',
                    token_hash: 'e2e-hash', permissions,
                });
            }
            if (body.tool === 'space_list') {
                return json(route, { status: 'ok', spaces: [{ space_id: 'demo' }] });
            }
            return json(route, { status: 'error', message: 'unexpected tool ' + body.tool });
        }

        if (p === '/api/admin/mesh/status') {
            state.meshCalls.push('status');
            if (!meshEnabled) return route.fulfill({ status: 404, body: 'not found' });
            const healthy = typeof meshHealthy === 'function' ? meshHealthy() : meshHealthy;
            return json(route, statusBody(pairings, healthy));
        }
        if (p === '/api/admin/mesh/invitation') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.meshCalls.push({ action: 'invitation', body });
            return json(route, {
                status: 'ok', pair_id: 'pair_e2e', secret: invitationSecret,
                invitation: 'SIGNED_INVITATION_B64', source_endpoint: 'https://source.e2e',
                source_fingerprint: FINGERPRINT,
            });
        }
        if (p === '/api/admin/mesh/evict') {
            state.meshCalls.push('evict');
            return json(route, { status: 'ok', pair_id: 'pair_evict', state: 'cancelled', evicted_node: 'node0' });
        }
        if (p.startsWith('/api/admin/mesh/members/')) {
            const spaceId = decodeURIComponent(p.slice('/api/admin/mesh/members/'.length));
            state.meshCalls.push({ action: 'members', spaceId });
            return json(route, members || { status: 'ok', space_id: spaceId, membership_epoch: 1, members: [] });
        }

        if (p === '/admin.html' || p === '/') return serveStatic(route, 'admin.html');
        if (p.startsWith('/static/')) return serveStatic(route, p.slice('/static/'.length));
        return route.fulfill({ status: 404, body: '' });
    });
    return state;
}

test('real shell: a direct #/mesh deep link when Mesh is unavailable renders no failing controls and fires no mesh POST', async ({ page }) => {
    const s = await routeShell(page, { permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: false });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    await expect(page.getByText('Mesh is not available')).toBeVisible();
    await expect(page.locator('[data-action="mesh-create-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-action="mesh-accept-invitation"]')).toHaveCount(0);
    expect(s.meshCalls.filter(c => typeof c === 'object')).toEqual([]);
});

test('real shell: a direct #/mesh/<space-id> deep link when Mesh is unavailable renders no failing controls', async ({ page }) => {
    const s = await routeShell(page, { permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: false });
    await page.goto(`${ORIGIN}/admin.html#/mesh/demo`);
    await expect(page.getByText('Mesh is not available')).toBeVisible();
    await expect(page.locator('[data-action="mesh-create-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-action="mesh-accept-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-mesh-action]')).toHaveCount(0);
    expect(s.meshCalls.filter(c => typeof c === 'object')).toEqual([]);
});

test('real shell: an unhealthy-but-reachable Mesh status (200, healthy:false) renders no failing controls on either route', async ({ page }) => {
    // mesh_admin.py's process-lock check is unconditional and precedes even
    // GET /status, so healthy:false can only appear in a genuinely successful
    // response via a narrow lock-loss race — it must be treated exactly like
    // an unreachable Mesh, not like a live, actionable instance.
    await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true, meshHealthy: false,
        pairings: [{ pair_id: 'pair_issued', role: 'source', state: 'issued', space_id: 'demo', updated_at_ms: Date.now(), granted_scopes: ['read'] }],
    });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    await expect(page.getByText('Project Mesh')).toBeVisible();
    await expect(page.locator('[data-action="mesh-create-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-action="mesh-accept-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-mesh-action]')).toHaveCount(0);

    await page.goto(`${ORIGIN}/admin.html#/mesh/demo`);
    await expect(page.getByText('Mesh is not available')).toBeVisible();
    await expect(page.locator('[data-action="mesh-create-invitation"]')).toHaveCount(0);
    await expect(page.locator('[data-mesh-action]')).toHaveCount(0);
});

test('real shell: a Create Invitation modal left open across a background health flip cannot fire the POST', async ({ page }) => {
    // The Create modal (unlike the one-time-secret display modal) holds no
    // navigation lock, so browser back/forward still dispatches a real route
    // change — and hence a fresh loadStatus() — while it stays visually open
    // (the shell router never touches #adminModal). That's the realistic
    // path to a stale-but-open modal: the operator opens Create, navigates
    // away and back (e.g. Back button) while deciding, Mesh's health changed
    // in the meantime, and they return to find the same modal still open.
    let healthy = true;
    const s = await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true, meshHealthy: () => healthy,
    });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    await page.getByRole('button', { name: 'Create invitation' }).click();
    await page.selectOption('#meshInvSpace', 'demo');

    healthy = false;
    await page.evaluate(() => { window.location.hash = '#/dashboard'; });
    await page.evaluate(() => { window.location.hash = '#/mesh'; });
    await page.waitForTimeout(100);
    await expect(page.locator('#adminModal')).toBeVisible();

    // The Create modal is still open (a separate overlay, untouched by the
    // route dispatches above) — confirm it.
    await page.locator('#modalConfirmBtn').click();
    await expect(page.getByText('Mesh became unavailable')).toBeVisible();
    expect(s.meshCalls.filter(c => typeof c === 'object')).toEqual([]);
});

test('real shell: Mesh nav is absent when the instance has no Mesh capability, present for an admin session when it does', async ({ page }) => {
    await routeShell(page, { permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: false });
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('.sidebar a[data-nav="dashboard"]')).toBeVisible();
    await page.waitForTimeout(200); // load-time capability probe settles
    await expect(page.locator('.sidebar a[data-nav="mesh"]')).toHaveCount(0);
});

test('real shell: Mesh nav is absent for a non-admin session even when Mesh is enabled (never a disabled control)', async ({ page }) => {
    await routeShell(page, { permissions: ['read', 'write', 'manage'], meshEnabled: true });
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await page.waitForTimeout(200);
    await expect(page.locator('.sidebar a[data-nav="mesh"]')).toHaveCount(0);
});

test('real shell: Mesh nav appears for an admin session, and #/mesh renders via real hash routing', async ({ page }) => {
    const s = await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true,
        pairings: [{ pair_id: 'pair_issued', role: 'source', state: 'issued', space_id: 'demo', updated_at_ms: Date.now(), granted_scopes: ['read'] }],
    });
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    const meshNav = page.locator('.sidebar a[data-nav="mesh"]');
    await expect(meshNav).toBeVisible();
    await meshNav.click();
    await expect(page).toHaveURL(/#\/mesh$/);
    await expect(page.getByRole('heading', { name: 'Project Mesh' })).toBeVisible();
    await expect(page.getByText(FINGERPRINT)).toBeVisible();
    // The pairing needs an operator action, so it legitimately appears twice:
    // once in "Needs your attention" and once in "All pairings".
    await expect(page.locator('[data-pair-id="pair_issued"]')).toHaveCount(2);
    expect(s.meshCalls.filter(c => c === 'status').length).toBeGreaterThan(0);
});

test('real shell: a cleared invitation canary is absent from the DOM and screenshot', async ({ page }, testInfo) => {
    const invitationCanary = 'P10-5-INVITATION-CANARY';
    const privateKeyCanary = 'ed25519-private:v1:P10-5-PRIVATE-KEY-CANARY';
    const snapshotCanary = 'P10-5-SNAPSHOT-CANARY';
    await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true,
        invitationSecret: invitationCanary,
    });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    await page.getByRole('button', { name: 'Create invitation' }).click();
    await page.selectOption('#meshInvSpace', 'demo');
    await page.locator('#modalConfirmBtn').click();
    const codeEl = page.locator('#meshInvCode');
    await expect(codeEl).toBeVisible();
    const code = await codeEl.textContent();
    expect(JSON.parse(Buffer.from(code, 'base64url').toString('utf8')).secret).toBe(invitationCanary);

    // Dismiss via the header × WITHOUT acknowledging — the real shell's
    // close-modal listener must still run the view's teardown.
    await page.locator('#adminModal .modal-close').click();
    await page.getByRole('button', { name: 'Create invitation' }).click(); // reopen to inspect a fresh modal state
    await expect(page.locator('#meshInvCode')).toHaveCount(0); // prior secret DOM is gone, not resurrected
    const afterClear = await page.content();
    for (const canary of [invitationCanary, privateKeyCanary, snapshotCanary]) {
        expect(afterClear).not.toContain(canary);
    }
    const screenshot = testInfo.outputPath('mesh-invitation-cleared.png');
    await page.screenshot({ path: screenshot, fullPage: true });
    expect(fs.readFileSync(screenshot).includes(Buffer.from(invitationCanary))).toBe(false);
});

test('real shell: keyboard modal and mobile-drawer flows retain visible, recoverable focus', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    await routeShell(page, { permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    await expect(page.locator('#sidebarMenuToggle')).toBeVisible();

    // A keyboard user can open the narrow-screen drawer, reaches its first
    // link, and Escape returns them to the visible Menu toggle.
    const drawerToggle = page.locator('#sidebarMenuToggle');
    await drawerToggle.focus();
    await page.keyboard.press('Enter');
    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#sidebar')).toHaveClass(/sidebar--drawer-open/);
    await expect(page.locator('#sidebar a.nav-item').first()).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(drawerToggle).toHaveAttribute('aria-expanded', 'false');
    await expect(drawerToggle).toBeFocused();

    // Enter opens the real dialog.  Escape activates the dialog's normal
    // close action (rather than merely hiding it) and restores caller focus.
    const create = page.getByRole('button', { name: 'Create invitation' });
    await create.focus();
    await expect(create).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#adminModal .modal-close')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#adminModal')).toBeHidden();
    await expect(create).toBeFocused();
    expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
    await context.close();
});

const VISUAL_VIEWPORTS = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 768, height: 1024 },
    { name: 'mobile', width: 390, height: 844 },
];

const VISUAL_MESH_STATES = [
    { name: 'disabled', meshEnabled: false },
    { name: 'empty', meshEnabled: true },
    { name: 'issued', meshEnabled: true, pairingState: 'issued' },
    { name: 'claimed', meshEnabled: true, pairingState: 'claimed' },
    { name: 'approving', meshEnabled: true, pairingState: 'approved' },
    { name: 'transferring', meshEnabled: true, pairingState: 'transferring' },
    { name: 'awaiting-acks', meshEnabled: true, pairingState: 'awaiting_acks' },
    { name: 'active', meshEnabled: true, pairingState: 'active' },
    { name: 'expired', meshEnabled: true, pairingState: 'expired' },
    { name: 'cancelled', meshEnabled: true, pairingState: 'cancelled' },
    { name: 'refused', meshEnabled: true, pairingState: 'refused' },
    { name: 'unsafe', meshEnabled: true, meshHealthy: false },
    { name: 'blocked-recovery', meshEnabled: true, pairingState: 'blocked_recovery' },
];

test('real shell: every material Mesh state is responsive across the required viewports', async ({ browser }, testInfo) => {
    // This matrix opens 78 independently routed pages and records screenshots.
    // Keep its timeout scoped to this deliberately exhaustive CI probe.
    test.setTimeout(120_000);
    for (const state of VISUAL_MESH_STATES) {
        for (const viewport of VISUAL_VIEWPORTS) {
            const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, reducedMotion: 'reduce' });
            const page = await context.newPage();
            const pairings = state.pairingState ? [{
                pair_id: `pair_${state.name}`, role: 'source', state: state.pairingState,
                space_id: 'demo', updated_at_ms: Date.now(), granted_scopes: ['read'],
                next_action: state.pairingState === 'blocked_recovery' ? 'resume' : undefined,
                phase: state.pairingState === 'blocked_recovery' ? 'network_failure' : undefined,
            }] : [];
            const errors = [];
            page.on('pageerror', error => errors.push(error));
            await routeShell(page, {
                permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: state.meshEnabled,
                meshHealthy: state.meshHealthy ?? true, pairings,
            });
            expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
            for (const route of ['#/mesh', '#/mesh/demo']) {
                await page.goto(`${ORIGIN}/admin.html${route}`);
                await expect(page.locator('body')).toBeVisible();
                const metrics = await page.evaluate(() => ({
                    client: document.documentElement.clientWidth,
                    scroll: document.documentElement.scrollWidth,
                }));
                expect(metrics.scroll).toBeLessThanOrEqual(metrics.client);
                const clippedControls = await page.evaluate(() => {
                    const viewportRight = document.documentElement.clientWidth;
                    return [...document.querySelectorAll('.page-header-actions > *, .sd-meta-row .mono-chip')]
                        .filter(el => {
                            const style = getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none' && rect.width > 0 && rect.height > 0
                                && (rect.left < 0 || rect.right > viewportRight + 0.5);
                        })
                        .map(el => el.outerHTML.slice(0, 160));
                });
                expect(clippedControls).toEqual([]);
                if (viewport.name === 'mobile') {
                    const undersizedTouchTargets = await page.evaluate(() =>
                        [...document.querySelectorAll('.page-header-actions .btn, .sd-tier-tab, .sidebar-menu-toggle, .nav-item, .nav-live, .icon-btn, .copy-btn')]
                            .filter(el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return style.display !== 'none' && rect.width > 0 && rect.height > 0
                                    && (rect.width < 44 || rect.height < 44);
                            })
                            .map(el => `${el.tagName.toLowerCase()}.${el.className}:${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`)
                    );
                    expect(undersizedTouchTargets).toEqual([]);
                }
                await page.screenshot({ path: testInfo.outputPath(`mesh-${state.name}-${route === '#/mesh' ? 'overview' : 'detail'}-${viewport.name}.png`), fullPage: true });
            }
            expect(errors).toEqual([]);
            await context.close();
        }
    }
});

test('real shell: a destructive Mesh action keeps the confirm button disabled until the typed space id matches', async ({ page }) => {
    await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true,
        pairings: [{ pair_id: 'pair_evict', role: 'source', state: 'approved', space_id: 'demo', updated_at_ms: Date.now(), granted_scopes: ['read'] }],
    });
    await page.goto(`${ORIGIN}/admin.html#/mesh`);
    // Also legitimately duplicated across "Needs your attention" and "All
    // pairings" — either instance drives the same confirm flow.
    await page.locator('[data-pair-id="pair_evict"][data-mesh-action="evict"]').first().click();
    const confirmBtn = page.locator('#modalConfirmBtn');
    await expect(confirmBtn).toBeVisible();
    await expect(confirmBtn).toBeDisabled();
    await page.locator('#destructiveConfirmInput').fill('wrong-space');
    await expect(confirmBtn).toBeDisabled();
    await page.locator('#destructiveConfirmInput').fill('demo');
    await expect(confirmBtn).toBeEnabled();
});

test('real shell: #/mesh/<space-id> detail route renders real numeric fields (membership epoch, member count) without throwing', async ({ page }) => {
    await routeShell(page, {
        permissions: ['read', 'write', 'manage', 'admin'], meshEnabled: true,
        pairings: [{ pair_id: 'pair_active', role: 'source', state: 'active', space_id: 'demo', updated_at_ms: Date.now(), granted_scopes: ['read'] }],
        members: {
            status: 'ok', space_id: 'demo', membership_epoch: 4,
            members: [{ node_id: 'node0', display_name: '', endpoint: '', fingerprint: '', scopes: null }],
        },
    });
    const errors = [];
    page.on('pageerror', err => errors.push(err));
    await page.goto(`${ORIGIN}/admin.html#/mesh/demo`);
    await expect(page.getByRole('heading', { name: 'demo' })).toBeVisible();
    await expect(page.getByText('MEMBERSHIP EPOCH')).toBeVisible();
    await expect(page.locator('.sd-kv', { hasText: 'MEMBERSHIP EPOCH' })).toContainText('4');
    await expect(page.locator('.sd-kv', { hasText: 'ACTIVE MEMBERS' })).toContainText('1');

    // Members tab, joined enrichment fields.
    await page.getByRole('tab', { name: 'Members' }).click();
    await expect(page.getByRole('columnheader', { name: 'Display name' })).toBeVisible();

    expect(errors).toEqual([]);
});
