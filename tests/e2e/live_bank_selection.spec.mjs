/**
 * P12-2 (#254) — /live bank selection stale-response guard, REAL-SHELL proof.
 *
 * The node:vm harness (tests/js/bank_selection_runtime.mjs, run via
 * tests/test_bank_selection.py) proves selectBank()'s async guard LOGIC fast
 * and mutation-proof against a stubbed DOM/fetch. This spec drives the REAL
 * /live bundle (live.html + config.js + api.js + sidebar.js + dashboard.js +
 * timeline.js + bank.js + app.js, unmodified) in headless chromium, intercepts
 * every request to serve the static files and a controlled API, and DEFERS
 * /api/bank/{space}/{filename} responses so the in-flight window is
 * observable and resolvable out of order through real DOM clicks.
 *
 * Three orderings are driven end to end at both required viewports:
 *  - A selected, then B selected, then A resolves late with a SUCCESS body:
 *    B must remain the selected tab and the rendered content.
 *  - Same ordering, but A's late response is a server-shaped ERROR: the
 *    stale error must not replace B's already-rendered content either.
 *  - ABA: alpha selected, beta selected (and resolved), alpha re-selected —
 *    then the FIRST alpha request resolves after the SECOND one. Space and
 *    filename are identical for both alpha requests, so this is the one
 *    ordering that requires the request-generation guard specifically, not
 *    just the space/filename identity checks (Terra PR #257 review finding).
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../src/live_mem/static');
const ORIGIN = 'http://live.e2e';
const SPACE_ID = 'demo';

const CONTENT_TYPE = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
};

// Mirrors StaticFilesMiddleware._html_security_headers() (auth/middleware.py)
// so the real CSP is enforced in the test browser, not just documented.
const HTML_SECURITY_HEADERS = {
    'content-security-policy': "default-src 'self'; script-src 'self'; "
        + "style-src 'self' 'unsafe-inline'; "
        + "img-src 'self' data:; connect-src 'self'; "
        + "font-src 'self'; "
        + "frame-ancestors 'none'; object-src 'none'; "
        + "form-action 'self'; base-uri 'self'",
    'x-frame-options': 'DENY',
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'strict-origin-when-cross-origin',
    'permissions-policy': 'camera=(), microphone=(), geolocation=(), payment=()',
};

const BANK_FILES = [
    { filename: 'alpha.md', size: 42, last_modified: '2026-07-20T10:00:00Z' },
    { filename: 'beta.md', size: 43, last_modified: '2026-07-20T11:00:00Z' },
];

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
        headers: path.extname(file) === '.html' ? HTML_SECURITY_HEADERS : {},
    });
}

// Boots the real /live shell against a controlled API. Every
// /api/bank/{space}/{filename} request is DEFERRED (captured, not answered)
// so the test drives resolution order explicitly via fulfillOldest()/
// fulfillNewest(). `pending` is a plain insertion-ordered array rather than
// a Map keyed by space/filename so the SAME target (e.g. two overlapping
// requests for alpha.md) can have more than one request in flight at once —
// required to drive the ABA ordering below.
async function routeLive(page) {
    const state = {
        pending: [],
        unexpected: [],
        consoleErrors: [],
        pageErrors: [],
    };
    page.on('console', msg => { if (msg.type() === 'error') state.consoleErrors.push(msg.text()); });
    page.on('pageerror', err => state.pageErrors.push(String(err)));
    await page.addInitScript(() => {
        window.__cspViolations = [];
        document.addEventListener('securitypolicyviolation', e => {
            window.__cspViolations.push(`${e.violatedDirective}:${e.blockedURI}`);
        });
    });

    await page.route('**/*', async route => {
        const req = route.request();
        const url = new URL(req.url());
        const p = url.pathname;

        if (p === '/health') return json(route, { version: 'e2e' });
        if (p === '/api/spaces') {
            return json(route, { status: 'ok', spaces: [{ space_id: SPACE_ID, description: 'P12-2 e2e space' }] });
        }
        if (p === `/api/space/${SPACE_ID}`) {
            return json(route, {
                status: 'ok', space_id: SPACE_ID, description: 'P12-2 e2e space', owner: 'e2e',
                created_at: '2026-07-01T00:00:00Z',
                live: { notes_count: 0, total_size: 0 },
                bank: { files_count: BANK_FILES.length, total_size: 85 },
                consolidation_count: 0, total_notes_processed: 0,
            });
        }
        if (p === `/api/live/${SPACE_ID}`) return json(route, { status: 'ok', notes: [], total: 0 });
        if (p === `/api/bank/${SPACE_ID}`) {
            return json(route, { status: 'ok', space_id: SPACE_ID, files: BANK_FILES, total: BANK_FILES.length });
        }

        const bankFileMatch = p.match(/^\/api\/bank\/([^/]+)\/(.+)$/);
        if (bankFileMatch) {
            const [, reqSpaceId, encodedFilename] = bankFileMatch;
            const filename = decodeURIComponent(encodedFilename);
            state.pending.push({ key: `${reqSpaceId}/${filename}`, route }); // deferred — resolved explicitly by the test
            return;
        }

        if (p === '/live' || p === '/live/') return serveStatic(route, 'live.html');
        if (p.startsWith('/static/')) return serveStatic(route, p.slice('/static/'.length));

        state.unexpected.push(`${req.method()} ${p}`);
        return route.fulfill({ status: 404, body: '' });
    });

    return state;
}

async function indexOfPending(state, filename, fromEnd) {
    const key = `${SPACE_ID}/${filename}`;
    await expect.poll(() => state.pending.some(entry => entry.key === key), { timeout: 5000 }).toBe(true);
    const indices = state.pending.reduce((acc, entry, i) => { if (entry.key === key) acc.push(i); return acc; }, []);
    return fromEnd ? indices[indices.length - 1] : indices[0];
}

// Fulfills the OLDEST still-pending request for `filename` (FIFO — the only
// ordering needed when at most one request per filename is ever in flight).
async function fulfillOldest(state, filename, body) {
    const index = await indexOfPending(state, filename, false);
    const [{ route }] = state.pending.splice(index, 1);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

// Fulfills the NEWEST still-pending request for `filename` — used to resolve
// a re-selection (the second alpha request in an alpha -> beta -> alpha
// ordering) while an older request for the SAME filename remains pending.
async function fulfillNewest(state, filename, body) {
    const index = await indexOfPending(state, filename, true);
    const [{ route }] = state.pending.splice(index, 1);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

const VIEWPORTS = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 768, height: 1024 },
];

for (const viewport of VIEWPORTS) {
    test(`real /live shell: a stale bank response never clobbers a newer selection @ ${viewport.name}`, async ({ browser }, testInfo) => {
        const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
        const page = await context.newPage();
        const state = await routeLive(page);

        await page.goto(`${ORIGIN}/live?space=${SPACE_ID}`);

        // Initial auto-select of the first bank file (renderBankTabs()) — settle
        // it to reach a stable baseline before driving the controlled race.
        await fulfillOldest(state, 'alpha.md', { status: 'ok', filename: 'alpha.md', content: '# Alpha\n\nInitial content.' });
        await expect(page.locator('#bankContent')).toContainText('Initial content');
        await expect(page.locator('.bank-tab.active')).toHaveText('alpha.md');

        // Deterministic for the rest of the test — no auto-refresh mid-race.
        await page.selectOption('#refreshInterval', '0');

        // --- Ordering 1: select A, select B, B resolves, A resolves LATE with
        // a SUCCESS body — B must remain the selected tab and the rendered
        // content; the stale success from A must be silently discarded.
        await page.locator('.bank-tab', { hasText: 'alpha.md' }).click();
        await page.locator('.bank-tab', { hasText: 'beta.md' }).click();

        await fulfillOldest(state, 'beta.md', { status: 'ok', filename: 'beta.md', content: '# Beta\n\nSecond file, current selection.' });
        await expect(page.locator('#bankContent')).toContainText('Second file, current selection');
        await expect(page.locator('.bank-tab.active')).toHaveText('beta.md');

        await fulfillOldest(state, 'alpha.md', { status: 'ok', filename: 'alpha.md', content: '# STALE ALPHA SUCCESS — must never render' });
        await page.waitForTimeout(200); // let the stale continuation run, if it were going to
        await expect(page.locator('#bankContent')).toContainText('Second file, current selection');
        await expect(page.locator('#bankContent')).not.toContainText('STALE ALPHA SUCCESS');
        await expect(page.locator('.bank-tab.active')).toHaveText('beta.md');

        await page.screenshot({ path: testInfo.outputPath(`bank-race-success-${viewport.name}.png`), fullPage: true });

        // --- Ordering 2: re-arm with a fresh in-flight pair, but this time the
        // stale late response is a server-shaped ERROR, not a success.
        await page.locator('.bank-tab', { hasText: 'beta.md' }).click();  // becomes the stale "A"
        await page.locator('.bank-tab', { hasText: 'alpha.md' }).click(); // becomes the current "B"

        await fulfillOldest(state, 'alpha.md', { status: 'ok', filename: 'alpha.md', content: '# Alpha again\n\nThird render, current selection.' });
        await expect(page.locator('#bankContent')).toContainText('Third render, current selection');
        await expect(page.locator('.bank-tab.active')).toHaveText('alpha.md');

        await fulfillOldest(state, 'beta.md', { status: 'error', message: 'STALE-ERROR must never render' });
        await page.waitForTimeout(200);
        await expect(page.locator('#bankContent')).toContainText('Third render, current selection');
        await expect(page.locator('#bankContent')).not.toContainText('STALE-ERROR');
        await expect(page.locator('.bank-tab.active')).toHaveText('alpha.md');

        await page.screenshot({ path: testInfo.outputPath(`bank-race-error-${viewport.name}.png`), fullPage: true });

        // --- Ordering 3 (ABA): alpha selected, beta selected and resolved,
        // alpha RE-selected, then the FIRST alpha request resolves after the
        // SECOND one. Space and filename are identical for both alpha
        // requests — only the request-generation guard can tell them apart
        // (Terra PR #257 review finding).
        await page.locator('.bank-tab', { hasText: 'alpha.md' }).click(); // alpha request #1 (stale-to-be)
        await page.locator('.bank-tab', { hasText: 'beta.md' }).click();
        await fulfillOldest(state, 'beta.md', { status: 'ok', filename: 'beta.md', content: '# Beta\n\nABA intermediate selection.' });
        await expect(page.locator('#bankContent')).toContainText('ABA intermediate selection');

        await page.locator('.bank-tab', { hasText: 'alpha.md' }).click(); // alpha request #2 (current)

        await fulfillNewest(state, 'alpha.md', { status: 'ok', filename: 'alpha.md', content: '# Alpha\n\nABA second render, current selection.' });
        await expect(page.locator('#bankContent')).toContainText('ABA second render, current selection');
        await expect(page.locator('.bank-tab.active')).toHaveText('alpha.md');

        await fulfillOldest(state, 'alpha.md', { status: 'ok', filename: 'alpha.md', content: '# STALE ABA ALPHA FIRST — must never render' });
        await page.waitForTimeout(200);
        await expect(page.locator('#bankContent')).toContainText('ABA second render, current selection');
        await expect(page.locator('#bankContent')).not.toContainText('STALE ABA ALPHA FIRST');
        await expect(page.locator('.bank-tab.active')).toHaveText('alpha.md');

        await page.screenshot({ path: testInfo.outputPath(`bank-race-aba-${viewport.name}.png`), fullPage: true });

        expect(state.unexpected).toEqual([]);
        expect(state.consoleErrors).toEqual([]);
        expect(state.pageErrors).toEqual([]);
        expect(await page.evaluate(() => window.__cspViolations)).toEqual([]);

        await context.close();
    });
}
