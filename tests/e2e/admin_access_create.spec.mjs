/**
 * P8-5 (#143) Access view — REAL-SHELL integration proof (P8-7 gate).
 *
 * The node:vm harness (tests/js/admin_access_lifecycle_runtime.mjs) proves the
 * view's async LOGIC fast and mutation-proof against a stubbed shell. It cannot,
 * by construction, prove behaviours the real shell owns. The Terra PR #167 review
 * called those out; this spec drives the REAL bundle (admin.html + admin-api.js +
 * admin-app.js + the view modules, unmodified) in headless chromium, intercepts
 * every request to serve the static files and a controlled API, and DEFERS
 * admin_create_token so the in-flight window is observable.
 *
 * Test 1 — single in-flight create ([high] R3): the real Create modal renders a
 * real #modalConfirmBtn; clicking it disables the confirm button AND both
 * dismissal controls (the header × and the footer Cancel — [medium] R4); a
 * second user activation of any control issues NO second admin_create_token and
 * does not close the modal; resolving `created` renders the one-time secret.
 *
 * Test 2 — navigate while pending ([high] R4): with the create deferred, an
 * off-route hash change is REVERTED by the view's navigation lock in the real
 * browser, and when the create then resolves the secret renders over the LOCKED
 * route, never the destination. This exercises the realizable navigate-during-
 * deferred-create ordering. The sub-tick adversarial ordering the finding posits
 * (a network continuation running BETWEEN the synchronous hash write and its own
 * queued hashchange dispatch) cannot be forced deterministically from a driver,
 * and hardening the create->secret handoff for it would change the frozen,
 * already-merged views-access.js — out of this test-only PR's scope, tracked
 * separately in issue #168.
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://admin.e2e';
const LOCKED_HASH = '#/access';

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

// Boot the real console onto #/access, open the Create modal, and submit through
// the real shell confirm button — leaving admin_create_token DEFERRED (in flight)
// so the caller can drive the pending window. Returns handles to observe/settle.
async function startPendingCreate(page) {
    const state = { tools: [], createRoute: null };
    let signalCaptured;
    state.captured = new Promise(resolve => (signalCaptured = resolve));

    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        const p = url.pathname;

        if (p === '/health') return json(route, { version: 'e2e' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: [] });   // valid session probe

        if (p === '/api/tool') {
            const body = JSON.parse(route.request().postData() || '{}');
            state.tools.push(body.tool);
            if (body.tool === 'system_whoami') {
                return json(route, {
                    status: 'ok', client_name: 'admin-e2e', auth_type: 'stored',
                    token_hash: 'e2e-hash', permissions: ['read', 'write', 'manage', 'admin'],
                });
            }
            if (body.tool === 'admin_list_tokens') return json(route, { status: 'ok', tokens: [], total: 0 });
            if (body.tool === 'admin_create_token') { state.createRoute = route; signalCaptured(); return; } // DEFER
            return json(route, { status: 'error', message: 'unexpected tool ' + body.tool });
        }

        if (p === '/admin.html' || p === '/') return serveStatic(route, 'admin.html');
        if (p.startsWith('/static/')) return serveStatic(route, p.slice('/static/'.length));
        return route.fulfill({ status: 404, body: '' });
    });

    await page.goto(`${ORIGIN}/admin.html${LOCKED_HASH}`);
    await page.getByRole('button', { name: 'Create token' }).click();
    await expect(page.locator('#ctName')).toBeVisible();
    await page.fill('#ctName', 'e2e-token');
    state.confirm = page.locator('#modalConfirmBtn');
    await expect(state.confirm).toBeEnabled();
    await state.confirm.click();

    await state.captured;                              // admin_create_token is now in flight (deferred)
    return state;
}

function createdBody(token) {
    return {
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'created', token, name: 'e2e-token', permissions: ['read'] }),
    };
}

test('real shell: an in-flight create locks the confirm button AND both dismissal controls, blocks a duplicate admin_create_token, then delivers the one-time secret', async ({ page }) => {
    const s = await startPendingCreate(page);

    // The shell disabled the confirm button synchronously, before awaiting
    // onConfirm — the single in-flight mechanism.
    expect(s.tools.filter(t => t === 'admin_create_token').length).toBe(1);
    await expect(s.confirm).toBeDisabled();

    // The view locked BOTH real dismissal controls — the header × and the footer
    // Cancel, not just the first ([medium] R4).
    const closeControls = page.locator('#adminModal [data-action="close-modal"]');
    await expect(closeControls).toHaveCount(2);
    const n = await closeControls.count();
    for (let i = 0; i < n; i++) await expect(closeControls.nth(i)).toBeDisabled();

    // A real second activation of ANY control (each Cancel/× and the confirm)
    // must issue no second admin_create_token and must not close the modal —
    // force:true simulates a genuine user click on a disabled control.
    for (let i = 0; i < n; i++) await closeControls.nth(i).click({ force: true });
    await s.confirm.click({ force: true });
    await page.waitForTimeout(200);
    expect(s.tools.filter(t => t === 'admin_create_token').length).toBe(1);
    await expect(page.locator('#adminModal')).toBeVisible();   // still the pending create modal

    // Resolve the deferred create; the REAL shell renders the one-time secret.
    await s.createRoute.fulfill(createdBody('E2E-ONE-TIME-SECRET'));
    await expect(page.locator('#ctSecret')).toHaveText('E2E-ONE-TIME-SECRET');
    expect(s.tools.filter(t => t === 'admin_create_token').length).toBe(1);   // exactly one, whole flow
});

test('real shell: navigating while the create is in flight reverts the route, and the secret renders in-context — never over the destination', async ({ page }) => {
    const s = await startPendingCreate(page);
    expect(new URL(page.url()).hash).toBe(LOCKED_HASH);

    // Navigate away while the create is deferred; the view's navigation lock must
    // revert the off-route hash change in the real browser (hashchange fires as a
    // real queued task, before the network reply we fulfil afterwards).
    await page.evaluate(() => { window.location.hash = '#/spaces'; });
    await page.waitForFunction(() => window.location.hash === '#/access');

    // Now resolve the deferred create: the secret must render over the LOCKED
    // route it was requested on, never the destination the operator tried to
    // reach while it was pending.
    await s.createRoute.fulfill(createdBody('IN-CONTEXT-SECRET'));
    await expect(page.locator('#ctSecret')).toHaveText('IN-CONTEXT-SECRET');
    expect(new URL(page.url()).hash).toBe(LOCKED_HASH);
});
