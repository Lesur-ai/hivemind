/**
 * #578 — Browser proof for the Graph UI's untrusted HTML boundaries.
 *
 * The real Graph shell and every browser dependency are served from disk.
 * API responses remain synthetic so the suite exercises the UI boundary only.
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const GRAPH_STATIC = path.resolve(
    process.env.GRAPH_STATIC_ROOT
        || path.join(REPO, 'services/graph-memory/src/mcp_memory/static'),
);
const ORIGIN = 'http://graph-security.e2e';

const ACTIVE_HTML =
    '"><svg data-xss-probe onload="globalThis.__graphXssExecutions = '
    + '(globalThis.__graphXssExecutions || 0) + 1"></svg>'
    + '<span data-probe="x">XSS-PROBE</span>';

const ANSWER = [
    '## Useful answer',
    '',
    '[Safe documentation](https://safe.example/docs)',
    '',
    '<svg data-xss-probe onload="globalThis.__graphXssExecutions = '
        + '(globalThis.__graphXssExecutions || 0) + 1"></svg>',
    '<script>globalThis.__graphXssExecutions = 999</script>',
    '<a href="javascript:globalThis.__graphXssExecutions = 998">Unsafe link</a>',
    '<a href="#" data-action="export-answer-html" '
        + 'onclick="globalThis.__graphXssExecutions = 997">Forged command</a>',
].join('\n');

const FALLBACK_ANSWER = [
    '## Useful fallback',
    '',
    '<svg data-xss-probe onload="globalThis.__graphXssExecutions = '
        + '(globalThis.__graphXssExecutions || 0) + 1"></svg>',
].join('\n');

const CONTENT_TYPE = {
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
};

function safeGraph() {
    return {
        status: 'ok',
        nodes: [
            {
                id: 'entity-safe', label: 'Safe entity', type: 'Organization',
                description: 'Safe description', mentions: 3,
                source_docs: ['document-safe'], node_type: 'entity',
            },
            {
                id: 'neighbor-safe', label: 'Safe neighbor', type: 'Person',
                description: '', mentions: 1, source_docs: [], node_type: 'entity',
            },
            {
                id: 'doc:document-safe', label: 'safe.md', type: 'Document',
                description: '', mentions: 0, source_docs: [], node_type: 'document',
            },
        ],
        edges: [
            { id: 'edge-safe', from: 'entity-safe', to: 'neighbor-safe', type: 'RELATED_TO', description: '' },
            { id: 'mention-safe', from: 'doc:document-safe', to: 'entity-safe', type: 'MENTIONS', description: '' },
        ],
        documents: [{ id: 'document-safe', filename: 'safe.md' }],
    };
}

function hostileGraph() {
    const documentId = `DOCUMENT-ID${ACTIVE_HTML}`;
    const neighborId = `NEIGHBOR-ID${ACTIVE_HTML}`;
    return {
        status: 'ok',
        nodes: [
            {
                id: 'entity-safe',
                label: `ENTITY-MARKER${ACTIVE_HTML}`,
                type: `TYPE-MARKER${ACTIVE_HTML}`,
                description: `DESCRIPTION-MARKER${ACTIVE_HTML}`,
                mentions: 3,
                source_docs: [documentId],
                node_type: 'entity',
            },
            {
                id: neighborId, label: 'Neighbor', type: 'Person',
                description: '', mentions: 1, source_docs: [], node_type: 'entity',
            },
            {
                id: `doc:${documentId}`, label: 'Document', type: 'Document',
                description: '', mentions: 0, source_docs: [], node_type: 'document',
            },
        ],
        edges: [
            {
                id: 'edge-hostile', from: 'entity-safe', to: neighborId,
                type: `RELATION-MARKER${ACTIVE_HTML}`, description: '',
            },
            {
                id: 'mention-hostile', from: `doc:${documentId}`, to: 'entity-safe',
                type: 'MENTIONS', description: '',
            },
        ],
        documents: [{ id: documentId, filename: `DOCUMENT-MARKER${ACTIVE_HTML}` }],
    };
}

function json(route, value, status = 200) {
    return route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(value),
    });
}

function serveFile(route, root, relative) {
    const file = path.resolve(root, relative);
    const inside = file === root || file.startsWith(root + path.sep);
    if (!inside || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        return route.fulfill({ status: 404, body: 'not found' });
    }
    return route.fulfill({
        status: 200,
        contentType: CONTENT_TYPE[path.extname(file)] || 'application/octet-stream',
        body: fs.readFileSync(file),
    });
}

async function openGraph(page, options = {}) {
    const state = {
        graph: options.graph || safeGraph(),
        ask: options.ask || { status: 'ok', answer: 'Safe answer', entities: [], source_documents: [] },
        requests: [],
        externalRequests: [],
    };

    await page.addInitScript(() => {
        localStorage.setItem('mcp_auth_token', 'E2E-BEARER-SENTINEL');
        globalThis.__graphXssExecutions = 0;
    });
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== ORIGIN) {
            state.externalRequests.push(url.href);
            return route.fulfill({ status: 502, body: 'unexpected external request' });
        }

        const pathname = url.pathname;
        if (pathname === '/graph' || pathname === '/graph.html' || pathname === '/') {
            return serveFile(route, GRAPH_STATIC, 'graph.html');
        }
        if (pathname.startsWith('/static/')) {
            return serveFile(route, GRAPH_STATIC, decodeURIComponent(pathname.slice('/static/'.length)));
        }
        if (pathname === '/api/memories') {
            return json(route, { status: 'ok', memories: [{ id: 'demo', name: 'Demo memory' }] });
        }
        if (pathname === '/api/graph/demo') return json(route, state.graph);
        if (pathname === '/api/ask') {
            state.requests.push(JSON.parse(route.request().postData() || '{}'));
            return json(route, state.ask);
        }
        return route.fulfill({ status: 404, body: 'not found' });
    });

    await page.goto(`${ORIGIN}/graph`, { waitUntil: 'load' });
    await expect(page.locator('#loginOverlay')).toHaveClass(/hidden/);
    await expect(page.locator('#memorySelect option')).toHaveCount(2);
    expect(state.externalRequests).toEqual([]);
    return state;
}

function expectNoExternalRequests(state) {
    expect(state.externalRequests).toEqual([]);
}

async function loadMemory(page) {
    await page.locator('#memorySelect').selectOption('demo');
    await page.locator('#loadBtn').click();
    await expect(page.locator('#nodeCount')).toHaveText(/\d+/);
    await expect(page.locator('#askBtn')).toBeEnabled();
}

async function ask(page, question) {
    await page.locator('#askBtn').click();
    await page.locator('#askInput').fill(question);
    await page.locator('#askSubmitBtn').click();
    await expect(page.locator('#askSubmitBtn')).toBeEnabled();
}

async function xssExecutions(page) {
    return page.evaluate(() => globalThis.__graphXssExecutions || 0);
}

async function openDownload(context, download) {
    const downloaded = await download.path();
    expect(downloaded).toBeTruthy();
    const html = fs.readFileSync(downloaded, 'utf8');
    const exported = await context.newPage();
    await exported.goto(`data:text/html;base64,${Buffer.from(html).toString('base64')}`, {
        waitUntil: 'load',
    });
    return { exported, html };
}

test('untrusted graph fields stay text in filters and the entity list', async ({ page }) => {
    const state = await openGraph(page, { graph: hostileGraph() });
    await loadMemory(page);

    await expect(page.locator('#body-entityTypes')).toContainText('TYPE-MARKER');
    await expect(page.locator('#body-edgeTypes')).toContainText('RELATION-MARKER');
    await expect(page.locator('#body-documents')).toContainText('DOCUMENT-MARKER');
    await expect(page.locator('#entityList')).toContainText('ENTITY-MARKER');
    await expect(page.locator(
        '#body-entityTypes [data-xss-probe], #body-edgeTypes [data-xss-probe], '
        + '#body-documents [data-xss-probe], #entityList [data-xss-probe]',
    )).toHaveCount(0);
    expect(await xssExecutions(page)).toBe(0);
    expectNoExternalRequests(state);
});

test('untrusted node, document, relation and neighbor fields stay text in details', async ({ page }) => {
    const state = await openGraph(page);
    await loadMemory(page);

    await page.evaluate(active => {
        const primary = appState.currentData.nodes.find(node => node.id === 'entity-safe');
        const neighbor = appState.currentData.nodes.find(node => node.id === 'neighbor-safe');
        const document = appState.currentData.documents[0];
        const edge = appState.currentData.edges.find(item => item.id === 'edge-safe');
        const documentId = `DETAIL-DOCUMENT-ID${active}`;
        const neighborId = `DETAIL-NEIGHBOR-ID${active}`;

        primary.label = `DETAIL-ENTITY-MARKER${active}`;
        primary.type = `DETAIL-TYPE-MARKER${active}`;
        primary.description = `DETAIL-DESCRIPTION-MARKER${active}`;
        primary.source_docs = [documentId];
        document.id = documentId;
        document.filename = `DETAIL-DOCUMENT-MARKER${active}`;
        neighbor.id = neighborId;
        edge.to = neighborId;
        edge.type = `DETAIL-RELATION-MARKER${active}`;
    }, ACTIVE_HTML);

    await page.locator('#entityList .entity-item').first().click();
    await expect(page.locator('#nodeDetails')).toHaveClass(/visible/);
    await expect(page.locator('#detailContent')).toContainText('DETAIL-ENTITY-MARKER');
    await expect(page.locator('#detailContent')).toContainText('DETAIL-DESCRIPTION-MARKER');
    await expect(page.locator('#detailContent')).toContainText('DETAIL-DOCUMENT-MARKER');
    await expect(page.locator('#detailContent')).toContainText('DETAIL-RELATION-MARKER');
    await expect(page.locator('#detailContent [data-xss-probe]')).toHaveCount(0);
    expect(await xssExecutions(page)).toBe(0);
    expectNoExternalRequests(state);
});

test('one sanitized answer is safe in both the live panel and downloaded HTML', async ({ page, context }) => {
    const state = await openGraph(page, {
        ask: { status: 'ok', answer: ANSWER, entities: [], source_documents: [] },
    });
    await loadMemory(page);
    await ask(page, 'Show useful evidence');

    await expect(page.locator('.ask-answer h2')).toHaveText('Useful answer');
    await expect(page.locator('.ask-answer a[href="https://safe.example/docs"]'))
        .toHaveText('Safe documentation');
    await expect(page.locator(
        '.ask-answer script, .ask-answer iframe, .ask-answer object, '
        + '.ask-answer embed, .ask-answer [onload], .ask-answer [onclick], '
        + '.ask-answer [data-action], .ask-answer a[href^="javascript:"]',
    )).toHaveCount(0);
    expect(await xssExecutions(page)).toBe(0);
    expect(state.requests).toHaveLength(1);

    const pending = page.waitForEvent('download');
    await page.locator('[data-action="export-answer-html"]').click();
    const { exported, html } = await openDownload(context, await pending);

    expect(html).not.toContain('data-action="export-answer-html"');
    await expect(exported.locator('.answer h2')).toHaveText('Useful answer');
    await expect(exported.locator('.answer a[href="https://safe.example/docs"]'))
        .toHaveText('Safe documentation');
    await expect(exported.locator(
        '.answer script, .answer iframe, .answer object, .answer embed, '
        + '.answer [onload], .answer [onclick], .answer [data-action], '
        + '.answer a[href^="javascript:"]',
    )).toHaveCount(0);
    expect(await xssExecutions(exported)).toBe(0);
    await exported.close();
    expectNoExternalRequests(state);
});

test('API and thrown error messages render as text', async ({ page }) => {
    const state = await openGraph(page, {
        ask: { status: 'error', message: `API-ERROR-MARKER${ACTIVE_HTML}` },
    });
    await loadMemory(page);
    await ask(page, 'First error');
    const apiResult = {
        text: await page.locator('#askBody').innerText(),
        active: await page.locator('#askBody [data-xss-probe]').count(),
        executions: await xssExecutions(page),
    };

    await page.evaluate(active => {
        globalThis.__graphXssExecutions = 0;
        globalThis.__forcedAskError = `THROWN-ERROR-MARKER${active}`;
        globalThis.eval(
            'apiAsk = async () => { throw new Error(globalThis.__forcedAskError); }',
        );
    }, ACTIVE_HTML);
    state.ask = { status: 'ok', answer: 'unused' };
    await ask(page, 'Second error');
    const thrownResult = {
        text: await page.locator('#askBody').innerText(),
        active: await page.locator('#askBody [data-xss-probe]').count(),
        executions: await xssExecutions(page),
    };

    expect(apiResult.text).toContain('API-ERROR-MARKER');
    expect(apiResult.active).toBe(0);
    expect(apiResult.executions).toBe(0);
    expect(thrownResult.text).toContain('THROWN-ERROR-MARKER');
    expect(thrownResult.active).toBe(0);
    expect(thrownResult.executions).toBe(0);
    expectNoExternalRequests(state);
});

for (const sanitizerFailure of ['absent', 'unsupported', 'throws']) {
    test(`Markdown fails closed when DOMPurify is ${sanitizerFailure}`, async ({ page, context }) => {
        const state = await openGraph(page, {
            ask: { status: 'ok', answer: FALLBACK_ANSWER, entities: [], source_documents: [] },
        });
        await loadMemory(page);
        await page.evaluate(mode => {
            globalThis.__graphXssExecutions = 0;
            if (mode === 'absent') {
                delete globalThis.DOMPurify;
            } else if (mode === 'unsupported') {
                globalThis.DOMPurify = { isSupported: false, sanitize: value => value };
            } else {
                globalThis.DOMPurify = {
                    isSupported: true,
                    sanitize() { throw new Error('forced sanitizer failure'); },
                };
            }
        }, sanitizerFailure);

        await ask(page, `Fallback ${sanitizerFailure}`);
        await expect(page.locator('.ask-answer')).toContainText('## Useful fallback');
        await expect(page.locator('.ask-answer pre')).toHaveCount(1);
        await expect(page.locator('.ask-answer [data-xss-probe]')).toHaveCount(0);
        expect(await xssExecutions(page)).toBe(0);

        const pending = page.waitForEvent('download');
        await page.locator('[data-action="export-answer-html"]').click();
        const { exported } = await openDownload(context, await pending);
        await expect(exported.locator('.answer')).toContainText('## Useful fallback');
        await expect(exported.locator('.answer pre')).toHaveCount(1);
        await expect(exported.locator('.answer [data-xss-probe]')).toHaveCount(0);
        expect(await xssExecutions(exported)).toBe(0);
        await exported.close();
        expectNoExternalRequests(state);
    });
}
