/** #629: real Portal bundle, offline document responses, no live credentials. */
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/live_mem/static');
const ORIGIN = 'http://documents-629.e2e';
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const documents = Array.from({ length: 50 }, (_, i) => ({
    document_id: `document-${i}`, filename: 'systemPatterns.md',
    source_path: `/captures/version-${String(i + 1).padStart(2, '0')}/${'long-repository-directory/'.repeat(3)}systemPatterns.md`,
    sha256: i.toString(16).padStart(64, 'a'), ingestion_status: ['running', 'succeeded', 'cleanup_pending', 'deprecated', 'future_status'][i % 5],
    repo_path: `docs/version-${i + 1}/systemPatterns.md`, size_bytes: 24000 + i,
    chunk_count: i, text_length: 22000 + i, content_type: 'md', last_ingest_job_id: `ingest-${i}`,
    source_modified_at: `2026-09-01T10:00:00Z`, ingested_at: `2026-09-30T10:${String(i).padStart(2, '0')}:00Z`,
}));

async function setup(page, options = {}) {
    const state = { calls: [], list: options.list, detail: options.detail, content: options.content };
    await page.route('**/*', async route => {
        const p = new URL(route.request().url()).pathname;
        const json = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
        if (p === '/health') return json({ version: 'qa-629' });
        if (p === '/api/spaces') return json({ status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (p === '/api/tool') {
            const call = JSON.parse(route.request().postData()); state.calls.push(call);
            switch (call.tool) {
            case 'system_whoami': return json({ status: 'ok', client_name: 'documents-qa', permissions: ['admin'], auth_type: 'stored' });
            case 'space_info': return json({ status: 'ok', space_id: 'demo', description: 'Document catalog QA', hive_status_label: 'local_only', live: { notes_count: 0 }, bank: { files_count: 0 }, consolidation_queue: { lane_state: 'idle', latest_jobs: [] } });
            case 'long_document_list': return json(state.list || { status: 'ok', documents, count: 50, total_count: 50, limit: 50, offset: 0 });
            case 'long_document_get': {
                const doc = documents.find(d => d.document_id === call.arguments.document_id);
                const override = call.arguments.include_content ? state.content : state.detail;
                return json(typeof override === 'function' ? await override(call.arguments) : override || { status: 'ok', document: doc, ...(call.arguments.include_content ? { content: 'Literal <script> document text', content_format: 'text' } : {}) });
            }
            default: return json({ status: 'error', message: `Unexpected tool: ${call.tool}` });
            }
        }
        const rel = p === '/admin.html' ? 'admin.html' : p.startsWith('/static/') ? p.slice(8) : '';
        const override = rel === 'js/admin/views-space-detail.js' ? process.env.HIVEMIND_QA629_VIEW : rel === 'css/admin.css' ? process.env.HIVEMIND_QA629_CSS : null;
        const file = path.join(STATIC, rel);
        if (override) return route.fulfill({ status: 200, contentType: MIME[path.extname(file)], body: fs.readFileSync(override) });
        if (rel && file.startsWith(STATIC + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) return route.fulfill({ status: 200, contentType: MIME[path.extname(file)], body: fs.readFileSync(file) });
        return route.fulfill({ status: 404, body: '' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/documents`);
    if (options.list?.status === 'error') await expect(page.locator('#sdDocumentsFreshness')).toContainText(options.list.message);
    else await expect(page.locator('#sdDocumentsList')).toContainText(options.list?.documents?.length === 0 ? 'No documents' : 'systemPatterns.md');
    return state;
}

async function capture(page, info, name) {
    await page.evaluate(() => {
        const modal = document.querySelector('#adminModal');
        if (modal?.style.display === 'flex') {
            modal.querySelector('.modal-body').scrollTop = 0;
            const detail = document.getElementById('sdDocumentDetail'); if (detail) detail.scrollTop = 0;
        } else {
            const content = document.querySelector('.content'), tabs = document.getElementById('sdLongTabs');
            if (content && tabs) content.scrollTop += tabs.getBoundingClientRect().top - content.getBoundingClientRect().top - 12;
        }
    });
    const dir = process.env.HIVEMIND_QA629_CAPTURE_DIR;
    const file = dir ? path.join(dir, name + '.png') : info.outputPath(name + '.png');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    await page.screenshot({ path: file, fullPage: false });
    await info.attach(name, { path: file, contentType: 'image/png' });
}

const inspect = (page, index) => page.locator(`[data-action="sd-document-inspect"][data-key="id:document-${index}"]`);
const prev = page => page.locator('[data-action="sd-documents-page"][data-step="-1"]');
const next = page => page.locator('[data-action="sd-documents-page"][data-step="1"]');

for (const [label, width, height, scale] of [['desktop', 1440, 900, 1], ['tablet', 768, 1024, 1], ['mobile', 390, 844, 1], ['zoom200', 720, 450, 2]]) {
    test(`${label}: 50 homonyms, adjacent/focused detail and keyboard restoration`, async ({ browser }, info) => {
        // 200% desktop zoom's reflow viewport (1440/2 x 900/2), rasterized at 2x.
        const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale });
        const page = await context.newPage();
        const state = await setup(page);
        await capture(page, info, `${label}-catalog`);
        await expect(prev(page)).toBeDisabled(); await expect(next(page)).toBeDisabled();
        await expect(page.locator('#sdDocumentsPagination')).toHaveCount(1);
        await expect(page.locator('#sdDocumentsPagination')).toBeHidden();
        await expect(page.locator('#sdDocumentDetail')).toHaveCount(0);
        await expect(page.locator('#sdDocumentStatus')).toHaveJSProperty('tagName', 'SELECT');
        await expect(page.locator('#sdDocumentsList')).toContainText('version-01');
        await expect(page.locator('#sdDocumentsList')).toContainText('version-50');
        await expect(page.locator('#sdDocumentsList')).toContainText('future_status');
        await expect(page.locator('#sdDocumentsList .status-dot')).toHaveCount(50);
        await expect(page.locator('#sdDocumentsList .sd-document-path [title]').first()).toHaveAttribute('title', documents[0].source_path);
        for (const index of [0, 49]) {
            await inspect(page, index).focus(); await page.keyboard.press('Enter');
            await expect(page.locator('#sdDocumentDetail')).toContainText(`ingest-${index}`);
            await expect(page.locator('#sdDocumentDetail')).toContainText(`docs/version-${index + 1}`);
            await expect(page.locator('#sdDocumentDetail')).toContainText('Text length');
            expect(state.calls.filter(c => c.tool === 'long_document_get').at(-1).arguments.include_content).toBe(false);
            await expect(inspect(page, index)).toHaveAttribute('aria-expanded', 'true');
            if (width >= 1200) {
                const list = await page.locator('#sdDocumentsList').boundingBox();
                const detail = await page.locator('#sdDocumentInspector').boundingBox();
                expect(detail.x).toBeGreaterThanOrEqual(list.x + list.width);
                await expect(page.locator('#sdDocumentInspectorTitle')).toBeFocused();
            } else {
                const dialog = page.getByRole('dialog'); await expect(dialog).toBeVisible();
                expect(await dialog.boundingBox()).toMatchObject({ x: 0, y: 0, width, height });
                for (let i = 0; i < 12; i++) { await page.keyboard.press('Tab'); expect(await dialog.evaluate(d => d.contains(document.activeElement))).toBe(true); }
            }
            await capture(page, info, `${label}-selected-${index + 1}`);
            await page.keyboard.press('Escape');
            await expect(inspect(page, index)).toBeFocused();
            await expect(inspect(page, index)).toHaveAttribute('aria-expanded', 'false');
        }
        expect(state.calls.filter(c => c.tool === 'long_document_get' && c.arguments.include_content)).toHaveLength(0);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        if (width < 768) expect((await inspect(page, 49).boundingBox()).height).toBeGreaterThanOrEqual(44);
        if (width < 768) for (const copy of await page.locator('#sdDocumentsList .copy-btn').all()) expect((await copy.boundingBox()).height).toBeGreaterThanOrEqual(44);
        expect(state.calls.every(c => ['system_whoami', 'space_info', 'long_document_list', 'long_document_get'].includes(c.tool))).toBe(true);
        await context.close();
    });
}

test('open inspector follows viewport changes without rereading metadata or content', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await setup(page);
    await inspect(page, 49).click();
    await expect(page.locator('#sdDocumentDetail')).toContainText('ingest-49');
    const reads = state.calls.filter(call => call.tool === 'long_document_get').length;
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.locator('#sdDocumentDetail')).toContainText('ingest-49');
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('#sdDocumentHost #sdDocumentInspector')).toBeVisible();
    await expect(page.locator('#sdDocumentInspectorTitle')).toBeFocused();
    expect(state.calls.filter(call => call.tool === 'long_document_get')).toHaveLength(reads);
    await page.keyboard.press('Escape');
    await expect(inspect(page, 49)).toBeFocused();
});

test('explicit content, binary/extracted/partial/error and stale responses', async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await setup(page); await inspect(page, 0).click();
    await expect(page.locator('#sdDocumentDetail')).toContainText('ingest-0');
    await page.getByRole('button', { name: 'Load content', exact: true }).click();
    await expect(page.locator('#sdDocumentDetail pre')).toHaveText('Literal <script> document text');
    await expect(page.locator('#sdDocumentDetail script')).toHaveCount(0);
    state.content = { status: 'ok', document: documents[0], content_format: 'raw', content_base64: '', content_note: '<binary fallback>' };
    await page.getByRole('button', { name: 'Load content', exact: true }).click();
    await expect(page.locator('#sdDocumentDetail')).toContainText('binary content');
    state.content = { status: 'ok', document: documents[0], content: 'Extracted PDF text', content_note: 'Text extracted from the binary document (pdf).', partial: true, warnings: ['<partial warning>'] };
    await page.getByRole('button', { name: 'Load content', exact: true }).click();
    await expect(page.locator('#sdDocumentDetail')).toContainText('Partial content response');
    await expect(page.locator('#sdDocumentDetail')).toContainText('<partial warning>');
    await capture(page, info, 'desktop-partial-content');
    state.content = { status: 'error', message: '<storage error>' };
    await page.getByRole('button', { name: 'Load content', exact: true }).click();
    await expect(page.locator('#sdDocumentContentFreshness')).toContainText('<storage error>');
    await expect(page.locator('#sdDocumentDetail pre')).toHaveText('Extracted PDF text');
    await capture(page, info, 'desktop-content-error');
    await page.getByRole('button', { name: 'Close document inspector' }).click();
    let resolve; state.detail = () => new Promise(r => { resolve = r; });
    await inspect(page, 0).click(); await expect(page.locator('#sdDocumentInspector')).toBeVisible();
    await expect.poll(() => Boolean(resolve)).toBe(true);
    await page.keyboard.press('Escape'); resolve({ status: 'ok', document: documents[0] });
    await expect(page.locator('#sdDocumentDetail')).toHaveCount(0);
    state.detail = { status: 'error', message: '<metadata error>' };
    await inspect(page, 1).click();
    await expect(page.locator('#sdDocumentFreshness')).toContainText('<metadata error>');
    await expect(page.getByRole('button', { name: 'Load content', exact: true })).toHaveCount(0);
    await capture(page, info, 'desktop-metadata-error');
});

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`empty and initial catalog error at ${width}px`, async ({ page }, info) => {
        await page.setViewportSize({ width, height });
        const state = await setup(page, { list: { status: 'ok', documents: [], offset: 0, count: 0, limit: 50, total_count: 0 } });
        await expect(prev(page)).toBeDisabled(); await expect(next(page)).toBeDisabled();
        await expect(page.locator('#sdDocumentsPagination')).toHaveCount(1);
        await expect(page.locator('#sdDocumentsPagination')).toBeHidden();
        await expect(page.locator('#sdDocumentDetail')).toHaveCount(0);
        await expect(page.locator('#sdDocumentsList')).toContainText('No documents in this catalog');
        await expect(page.locator('#sdDocumentsList')).toContainText('View the overview for this space’s long memory status.');
        await expect(page.locator('#sdDocumentsList a')).toHaveAttribute('href', '#/spaces/demo/long/overview');
        await expect(page.getByRole('button', { name: 'Clear filters', exact: true })).toHaveCount(0);
        // Hidden controls must still reject direct/programmatic dispatch.
        const reads = state.calls.length;
        await next(page).evaluate(button => button.click()); await prev(page).evaluate(button => button.click());
        expect(state.calls).toHaveLength(reads);
        await capture(page, info, `${width}-empty`);
        await page.locator('#sdDocumentQuery').fill('absent');
        await page.locator('#sdDocumentStatus').selectOption('cleanup_pending');
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await expect(page.locator('#sdDocumentsList')).toContainText('No documents match these filters');
        await expect(page.locator('#sdDocumentsList')).not.toContainText('No documents in this catalog');
        await expect(page.locator('#sdDocumentsList')).toContainText('Try another filename, source path or status, or clear the filters.');
        await expect(page.locator('#sdDocumentsPagination')).toHaveCount(1);
        await expect(page.locator('#sdDocumentsPagination')).toBeHidden();
        await capture(page, info, `${width}-filtered-empty`);
        const clear = page.getByRole('button', { name: 'Clear filters', exact: true });
        await clear.focus(); await page.keyboard.press('Enter');
        await expect(page.locator('#sdDocumentsList')).toContainText('No documents in this catalog');
        await expect(page.locator('#sdDocumentQuery')).toHaveValue('');
        await expect(page.locator('#sdDocumentStatus')).toHaveValue('');
        await expect(page.locator('#sdDocumentQuery')).toBeFocused();
        expect(state.calls.at(-1).arguments).toMatchObject({ offset: 0, query: '', status: '' });
        state.list = { ...state.list, partial: true, warnings: ['<partial catalog>'] };
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await expect(page.locator('#sdDocumentsList')).toContainText('No documents returned');
        await expect(page.locator('#sdDocumentsList')).toContainText('Partial catalog response');
        await expect(page.locator('#sdDocumentsList')).not.toContainText('No documents in this catalog');
        await expect(page.locator('#sdDocumentDetail')).toHaveCount(0);
        await expect(page.locator('#sdDocumentsList')).toContainText('Some documents may be unavailable. Try refreshing this view.');
        await capture(page, info, `${width}-partial-empty`);
        state.list = { ...state.list, partial: false, warnings: [], total_count: 1 };
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await expect(page.locator('#sdDocumentsList')).toContainText('No documents on this page. Refresh this view to try again.');
        await expect(page.locator('#sdDocumentsList')).not.toContainText('No documents in this catalog');
        await capture(page, info, `${width}-unknown-empty`);
        state.list = { status: 'error', message: '<catalog unavailable>' };
        // Remount, so the failure cannot be mistaken for an empty retained result.
        await page.reload();
        await expect(page.locator('#sdDocumentsFreshness')).toContainText('<catalog unavailable>');
        await expect(page.locator('#sdDocumentsList')).not.toContainText('No documents match');
        await expect(prev(page)).toBeDisabled(); await expect(next(page)).toBeDisabled();
        await capture(page, info, `${width}-initial-error`);
    });
}

test('empty responses use accepted filters, clear real selections and guard hidden dispatch', async ({ page }) => {
    const state = await setup(page, { list: { status: 'ok', documents: [], offset: 0, count: 0, limit: 50, total_count: 0 } });
    await page.locator('#sdDocumentQuery').fill('draft-only');
    await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
    await expect(page.locator('#sdDocumentsList')).toContainText('No documents in this catalog');
    expect(state.calls.at(-1).arguments.query || '').toBe('');
    state.list = { status: 'error', message: '<filter failed>' };
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsFreshness')).toContainText('<filter failed>');
    await expect(page.locator('#sdDocumentsList')).toContainText('Previous result retained');
    await expect(page.getByRole('button', { name: 'Clear filters', exact: true })).toBeVisible();
    state.list = { status: 'ok', documents, count: 50, offset: 0, limit: 50, total_count: 50 };
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('version-01');
    await expect(page.locator('#sdDocumentsPagination')).toBeHidden();
    await inspect(page, 0).click(); await expect(page.locator('#sdDocumentDetail')).toContainText('ingest-0');
    state.list = { status: 'ok', documents: [], offset: 0, count: 0, limit: 50, total_count: 0 };
    await page.locator('#sdDocumentQuery').fill('absent');
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('No documents match these filters');
    await expect(page.locator('#sdDocumentInspector')).toHaveCount(0);
    await expect(page.locator('#sdDocumentQuery')).toBeFocused();
    const hiddenReads = state.calls.length;
    await page.evaluate(async () => {
        _actionHandlers['sd-documents-page']({ step: '1' });
        _actionHandlers['sd-documents-page']({ step: '-1' });
        await new Promise(resolve => setTimeout(resolve, 50));
    });
    expect(state.calls).toHaveLength(hiddenReads);
    state.list = { status: 'ok', documents, offset: 0, count: 50, limit: 50, total_count: 51 };
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await expect(next(page)).toBeEnabled();
    await page.locator('#sdDocumentQuery').fill('absent');
    const reads = state.calls.length;
    await page.evaluate(async () => {
        // Router epoch is a getter; inject the underlying counter, not a no-op assignment.
        _epoch++;
        _actionHandlers['sd-documents-page']({ step: '1' });
        _actionHandlers['sd-documents-clear']();
        await new Promise(resolve => setTimeout(resolve, 50));
    });
    expect(state.calls).toHaveLength(reads);
    await expect(page.locator('#sdDocumentQuery')).toHaveValue('absent');
    await page.reload(); await expect(page.locator('#sdDocumentsList')).toContainText('version-01');
    const sessionReads = state.calls.length;
    await page.evaluate(async () => {
        _invalidateSessionGeneration();
        _actionHandlers['sd-documents-page']({ step: '1' });
        _actionHandlers['sd-documents-clear']();
        await new Promise(resolve => setTimeout(resolve, 50));
    });
    expect(state.calls).toHaveLength(sessionReads);
});

test('narrow detail closes on route/session change and discards the pending source', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const state = await setup(page);
    let resolve;
    state.detail = () => new Promise(r => { resolve = r; });
    await inspect(page, 0).click(); await expect.poll(() => Boolean(resolve)).toBe(true);
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.evaluate(() => { location.hash = '#/spaces/demo/memory/short'; });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    resolve({ status: 'ok', document: { ...documents[0], filename: 'OLD-ROUTE-DOCUMENT' } });
    await expect(page.locator('body')).not.toContainText('OLD-ROUTE-DOCUMENT');
    await page.evaluate(() => { location.hash = '#/spaces/demo/long/documents'; });
    await expect(page.locator('#sdDocumentsList')).toContainText('version-01');
    resolve = null; await inspect(page, 0).click(); await expect.poll(() => Boolean(resolve)).toBe(true);
    await page.evaluate(() => { _invalidateSessionGeneration(); wipeSession(); showLogin(); });
    resolve({ status: 'ok', document: { ...documents[0], filename: 'OLD-SESSION-DOCUMENT' } });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('OLD-SESSION-DOCUMENT');
    await expect(page.locator('#loginToken')).toBeVisible();
});

test('controlled filters, page bounds, malformed metadata and empty/failed catalog', async ({ page }, info) => {
    const state = await setup(page);
    await page.locator('#sdDocumentQuery').fill('version-50'); await page.locator('#sdDocumentStatus').selectOption('cleanup_pending');
    state.list = { status: 'ok', documents: [documents[49]], count: 1, total_count: 1, limit: 50, offset: 0, partial: true, warnings: ['<catalog warning>'] };
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('Partial catalog response');
    expect(state.calls.at(-1).arguments).toMatchObject({ limit: 50, offset: 0, query: 'version-50', status: 'cleanup_pending' });
    state.list = { status: 'ok', documents, count: 50, total_count: 51, limit: 50, offset: 0 };
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click(); await expect(next(page)).toBeEnabled();
    state.list = { status: 'ok', documents: [documents[49]], count: 1, total_count: 51, limit: 50, offset: 50 };
    await next(page).click(); await expect(next(page)).toBeDisabled(); await expect(prev(page)).toBeEnabled();
    expect(state.calls.at(-1).arguments.offset).toBe(50);
    for (const bad of [{ offset: '<img src=x>', count: 1 }, { offset: 0, count: true }, { offset: 0, count: 0 }, { offset: 0, count: 101 }, { offset: 0, count: 1, total_count: 0 }, { offset: 0, count: 1, limit: '50' }]) {
        state.list = { status: 'ok', documents: [documents[0]], count: 1, total_count: 51, limit: 50, offset: 0, ...bad };
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await expect(prev(page)).toBeDisabled(); await expect(next(page)).toBeDisabled();
        await expect(page.locator('#sdDocumentsPage img')).toHaveCount(0);
    }
    state.list = { status: 'ok', documents: [], count: 0, total_count: 0, limit: 50, offset: 0 };
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('No documents match these filters');
    await expect(page.locator('#sdDocumentInspector')).toHaveCount(0);
    await capture(page, info, 'desktop-filtered-empty');
    state.list = { status: 'error', message: '<catalog unavailable>' };
    await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
    await expect(page.locator('#sdDocumentsFreshness')).toContainText('<catalog unavailable>');
    await capture(page, info, 'desktop-catalog-error');
});

test('R1 F1: persisted statuses, attention and contained future labels', async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const persisted = ['running', 'succeeded', 'deprecated', 'cleanup_pending'];
    const rows = persisted.map((status, i) => ({ ...documents[i], ingestion_status: status }));
    rows.push({ ...documents[4], ingestion_status: 'unknown_future_status_with_a_long_value' });
    rows.push({ ...documents[5], ingestion_status: 'unknown' });
    const state = await setup(page, { list: { status: 'ok', documents: rows, count: 6, total_count: 6, offset: 0, limit: 50 } });
    expect(await page.locator('#sdDocumentStatus option').evaluateAll(items => items.map(item => item.value))).toEqual(['', ...persisted]);
    for (const status of persisted) {
        state.list = { status: 'ok', documents: rows.filter(item => item.ingestion_status === status), count: 1, total_count: 1, offset: 0, limit: 50 };
        await page.locator('#sdDocumentStatus').selectOption(status);
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await expect(page.locator('#sdDocumentsList tbody tr')).toHaveCount(1);
        expect(state.calls.at(-1).arguments.status).toBe(status);
    }
    await expect(page.locator('#sdDocumentsList .status-dot-wrap')).toContainText('Cleanup pending');
    await expect(page.locator('#sdDocumentsList .status-dot')).toHaveClass(/warn/);
    state.list = { status: 'ok', documents: rows, count: 6, total_count: 6, offset: 0, limit: 50 };
    await page.locator('#sdDocumentStatus').selectOption('');
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList .status-dot-label').nth(4)).toHaveText(rows[4].ingestion_status);
    await expect(page.locator('#sdDocumentsList .status-dot-label').nth(5)).toHaveText('Unknown');
    await expect(page.locator('#sdDocumentsList .status-dot').nth(4)).toHaveClass(/neutral/);
    const measurements = [];
    const contained = async (presentation) => {
        for (const index of [3, 4, 5]) {
            const cell = page.locator('#sdDocumentsList tbody tr').nth(index).locator('td').nth(1);
            const geometry = await cell.evaluate(el => {
                const rect = el.getBoundingClientRect(), badge = el.querySelector('.status-dot-wrap').getBoundingClientRect();
                const range = document.createRange(); range.selectNodeContents(el.querySelector('.status-dot-wrap'));
                return { cell: { left: rect.left, right: rect.right }, badge: { left: badge.left, right: badge.right }, text: [...range.getClientRects()].map(r => ({ left: r.left, right: r.right })) };
            });
            measurements.push({ presentation, status: rows[index].ingestion_status, viewport: page.viewportSize(), ...geometry });
            const assetHash = (relative, override) => createHash('sha256').update(fs.readFileSync(override || path.join(STATIC, relative))).digest('hex');
            const evidence = { qualification: 'Synthetic Browser fixture; table text and badge bounds, no live read or native zoom claim.',
                assets: { view: assetHash('js/admin/views-space-detail.js', process.env.HIVEMIND_QA629_VIEW), css: assetHash('css/admin.css', process.env.HIVEMIND_QA629_CSS) }, measurements };
            const output = process.env.HIVEMIND_QA629_CAPTURE_DIR ? path.join(process.env.HIVEMIND_QA629_CAPTURE_DIR, 'r1-F1-status-geometry.json') : info.outputPath('r1-F1-status-geometry.json');
            fs.mkdirSync(path.dirname(output), { recursive: true }); fs.writeFileSync(output, JSON.stringify(evidence, null, 2));
            expect(geometry.badge.right).toBeLessThanOrEqual(geometry.cell.right + 1);
            for (const rect of geometry.text) {
                expect(rect.left).toBeGreaterThanOrEqual(geometry.cell.left - 1);
                expect(rect.right).toBeLessThanOrEqual(geometry.cell.right + 1);
            }
        }
    };
    await contained('desktop catalog'); await capture(page, info, 'r1-F1-desktop-statuses');
    state.detail = { status: 'ok', document: rows[3] };
    await inspect(page, 3).click(); await expect(page.locator('#sdDocumentDetail')).toContainText('Cleanup pending');
    await contained('desktop selected catalog'); await capture(page, info, 'r1-F1-desktop-selected-statuses');
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole('dialog')).toBeVisible();
    await capture(page, info, 'r1-F1-mobile-cleanup');
    await page.keyboard.press('Escape');
    await contained('mobile catalog'); await capture(page, info, 'r1-F1-mobile-statuses');
});

for (const width of [1440, 390]) test(`R1 F2: ${width}px shrink 51 to 50 recovers to first page with applied filters`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page, { list: { status: 'ok', documents, count: 50, total_count: 51, offset: 0, limit: 50 } });
    for (const filtered of [false, true]) {
        if (filtered) {
            await page.locator('#sdDocumentQuery').fill('systemPatterns');
            await page.locator('#sdDocumentStatus').selectOption('succeeded');
            await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        }
        state.list = { status: 'ok', documents: [documents[49]], count: 1, total_count: 51, offset: 50, limit: 50 };
        await next(page).click(); await expect(page.locator('#sdDocumentsPage')).toContainText('51–51');
        state.list = { status: 'ok', documents: [], count: 0, total_count: 50, offset: 50, limit: 50 };
        await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
        await expect(page.locator('#sdDocumentsList')).toContainText('Return to the first page');
        await expect(page.locator('#sdDocumentsList')).not.toContainText('Refresh this view to try again');
        await capture(page, info, `r1-F2-${width}-${filtered ? 'filtered' : 'plain'}-shrink`);
        state.list = { status: 'ok', documents, count: 50, total_count: 50, offset: 0, limit: 50 };
        const gets = state.calls.filter(c => c.tool === 'long_document_get').length;
        await page.getByRole('button', { name: 'Back to first page', exact: true }).focus(); await page.keyboard.press('Enter');
        await expect(page.locator('#sdDocumentsList tbody tr')).toHaveCount(50);
        expect(state.calls.at(-1).arguments).toMatchObject({ offset: 0, limit: 50 });
        expect(state.calls.at(-1).arguments.query || '').toBe(filtered ? 'systemPatterns' : '');
        expect(state.calls.at(-1).arguments.status || '').toBe(filtered ? 'succeeded' : '');
        expect(state.calls.filter(c => c.tool === 'long_document_get')).toHaveLength(gets);
        await expect(page.locator('#sdDocumentQuery')).toBeFocused();
        await capture(page, info, `r1-F2-${width}-${filtered ? 'filtered' : 'plain'}-recovered`);
        state.list = { status: 'ok', documents, count: 50, total_count: 51, offset: 0, limit: 50 };
        await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
        await expect(next(page)).toBeEnabled();
    }
});

test('R1 L1: homonym row actions identify source and version accessibly', async ({ page }) => {
    await setup(page);
    const labels = [];
    for (const i of [0, 1, 49]) {
        const label = `Inspect systemPatterns.md, source ${documents[i].source_path}, version ${documents[i].sha256}`;
        await expect(inspect(page, i)).toHaveAccessibleName(label);
        labels.push(label);
        await expect(page.getByRole('button', { name: label, exact: true })).toHaveCount(1);
    }
    expect(new Set(labels).size).toBe(3);
});

test('R1 L2: off-page reading remains labelled with stable content and scroll', async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await setup(page, { content: { status: 'ok', document: documents[0], content: 'Retained reading\n'.repeat(150), content_format: 'text' } });
    await inspect(page, 0).click(); await page.getByRole('button', { name: 'Load content', exact: true }).click();
    await expect(page.locator('#sdDocumentDetail')).toContainText('Retained reading');
    await page.locator('#sdDocumentDetail').evaluate(el => { el.scrollTop = 100; window.retainedReader = el; });
    const gets = state.calls.filter(c => c.tool === 'long_document_get').length;
    state.list = { status: 'ok', documents: [documents[49]], count: 1, total_count: 1, offset: 0, limit: 50 };
    await page.locator('#sdDocumentQuery').fill('version-50'); await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentSelectionNotice')).toContainText('not in the shown catalog page');
    await expect(page.locator('#sdDocumentSelectionNotice')).not.toContainText('deleted');
    expect(await page.locator('#sdDocumentDetail').evaluate(el => el === window.retainedReader && el.scrollTop === 100)).toBe(true);
    expect(state.calls.filter(c => c.tool === 'long_document_get')).toHaveLength(gets);
    await capture(page, info, 'r1-L2-desktop-retained-reading');
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.locator('#sdDocumentSelectionNotice')).toContainText('not in the shown catalog page');
    await expect(page.locator('#sdDocumentDetail')).toContainText('Retained reading');
    expect(state.calls.filter(c => c.tool === 'long_document_get')).toHaveLength(gets);
    await capture(page, info, 'r1-L2-mobile-retained-reading');
    await page.setViewportSize({ width: 1440, height: 900 });
    state.list = { status: 'ok', documents, count: 50, total_count: 50, offset: 0, limit: 50 };
    await page.getByRole('button', { name: 'Refresh', exact: true }).last().click();
    await expect(page.locator('#sdDocumentSelectionNotice')).toBeEmpty();
    await expect(inspect(page, 0)).toHaveAttribute('aria-expanded', 'true');
});

test('R1 L3: malformed size is unavailable, numeric zero remains zero', async ({ page }, info) => {
    const rows = [{ ...documents[0], size_bytes: 'abc' }, { ...documents[1], size_bytes: 0 }];
    const state = await setup(page, { list: { status: 'ok', documents: rows, count: 2, total_count: 2, offset: 0, limit: 50 }, detail: { status: 'ok', document: rows[0] } });
    await expect(page.locator('#sdDocumentsList tbody tr').nth(0)).toContainText('Size unavailable');
    await expect(page.locator('#sdDocumentsList tbody tr').nth(0)).not.toContainText('0 B');
    await expect(page.locator('#sdDocumentsList tbody tr').nth(1)).toContainText('0 B');
    await inspect(page, 0).click(); await expect(page.locator('#sdDocumentDetail')).toContainText('Size unavailable');
    await expect(page.locator('#sdDocumentDetail')).not.toContainText('abc');
    await capture(page, info, 'r1-L3-malformed-size');
    state.detail = { status: 'ok', document: rows[1] };
    await page.keyboard.press('Escape'); await inspect(page, 1).click();
    await expect(page.locator('#sdDocumentDetail .sd-meta-row').filter({ hasText: 'Bytes' })).toHaveText('Bytes0');
});

for (const width of [1440, 390]) test(`R1 L4: ${width}px failed filter retains explicitly old empty snapshot`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page, { list: { status: 'ok', documents: [], count: 0, total_count: 0, offset: 0, limit: 50 } });
    await expect(page.locator('#sdDocumentsList')).toContainText('No documents in this catalog');
    const freshness = await page.locator('#sdDocumentsFreshness').textContent();
    state.list = { status: 'error', message: 'filter read failed' };
    await page.locator('#sdDocumentQuery').fill('systemPatterns'); await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('Previous result retained');
    await expect(page.locator('#sdDocumentsList')).toContainText('last successful read');
    await expect(page.locator('#sdDocumentsList')).not.toContainText('No documents in this catalog');
    await expect(page.locator('#sdDocumentsFreshness')).toContainText(freshness);
    await expect(page.locator('#sdDocumentsFreshness')).toContainText('filter read failed');
    await capture(page, info, `r1-L4-${width}-stale-empty`);
    state.list = { status: 'ok', documents: [documents[0]], count: 1, total_count: 1, offset: 0, limit: 50 };
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await expect(page.locator('#sdDocumentsList')).toContainText('version-01');
    await expect(page.locator('#sdDocumentsList')).not.toContainText('Previous result retained');
    await expect(page.locator('#sdDocumentQuery')).toBeFocused();
});
