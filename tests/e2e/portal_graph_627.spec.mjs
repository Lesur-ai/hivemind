import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/live_mem/static');
// Unique intercepted origin: no shared server, port, account or production token.
const ORIGIN = 'http://portal-graph-627.e2e';
const PROOF = process.env.GRAPH_627_PROOF;
test.use({ hasTouch: true });

export function graphFixture() {
    const nodes = Array.from({ length: 160 }, (_, i) => ({
        id: `n${i + 1}`, label: i === 0 ? 'Hivemind' : i === 1 ? 'Project Mesh' :
            i >= 136 ? `Source ${i - 135} — architecture.md` : `Concept ${i + 1} — collective memory`,
        type: i >= 136 ? 'Document' : 'Concept', node_type: i >= 136 ? 'document' : 'entity',
        mentions: 160 - i, description: i === 0 ? 'Open memory layer for agents.' : 'A deterministic graph fixture.',
        ...(i >= 136 ? { filename: `architecture-${i - 135}.md` } : {}),
    }));
    const edges = [
        { id: 'e1', from: 'n1', to: 'n2', type: 'USES' },
        { id: 'e2', from: 'n3', to: 'n1', type: 'SUPPORTS' },
        { id: 'e3', from: 'n137', to: 'n1', type: 'MENTIONS' },
        ...Array.from({ length: 317 }, (_, i) => ({
            id: `e${i + 4}`, from: `n${4 + i % 156}`, to: `n${4 + (i * 13 + 1) % 156}`,
            type: i % 3 ? 'RELATED_TO' : 'MENTIONS', weight: 1,
        })),
    ];
    return { status: 'ok', nodes, edges, node_count: 160, edge_count: 320,
        total_node_count: 223, total_edge_count: 420, truncated: true };
}

function hubFixture(neighbors) {
    const graph = graphFixture();
    graph.edges = [
        ...Array.from({ length: neighbors }, (_, i) => ({ id: `hub${i}`, from: 'n1', to: `n${i + 2}`, type: 'USES' })),
        ...graph.edges.filter(edge => edge.from !== 'n1' && edge.to !== 'n1').slice(0, 320 - neighbors),
    ];
    return graph;
}

// Same graph projection as #634 realisticData(), copied from its read-only fixture.
function acceptanceFixture() {
    const types = ['Product', 'Protocol', 'Person', 'Team', 'Decision', 'Evidence', 'Service', 'Process', 'Policy', 'Region', 'Metric', 'Event', 'Asset', 'Constraint', 'Long entity type with operator-readable words'];
    const nodes = Array.from({ length: 160 }, (_, i) => i < 50 ? {
        id: `n${i + 1}`, label: `Source ${i + 1} — systemPatterns.md retained evidence`, filename: 'systemPatterns.md',
        type: 'Document', node_type: 'document', description: `Synthetic source ${i + 1}. <img src=x onerror="window.__portal634Xss=1">`, mentions: 0,
    } : {
        id: `n${i + 1}`, label: `Entity ${i + 1} — collective memory decision with a long readable label`,
        type: types[(i - 50) % types.length], node_type: 'entity',
        description: `Synthetic evidence for entity ${i + 1}. <img src=x onerror="window.__portal634Xss=1">`, mentions: i % 19 + 1,
    });
    const edges = nodes.flatMap((node, i) => [1, 17].map((distance, j) => ({
        id: `edge-${i * 2 + j + 1}`, from: node.id, to: nodes[(i + distance) % 160].id,
        type: i < 50 ? 'MENTIONS' : j ? 'SUPPORTS' : 'RELATED_TO', weight: 1,
    })));
    return { status: 'ok', nodes, edges, node_count: 160, edge_count: 320, total_node_count: 267, total_edge_count: 420, truncated: true };
}

async function expectLabelAssociation(page) {
    const failures = await page.locator('#sdGraphCanvas').evaluate(svg => {
        const canvas = svg.getBoundingClientRect(), unit = canvas.width / svg.clientWidth;
        const gap = (a, b) => Math.hypot(Math.max(a.left - b.right, b.left - a.right, 0), Math.max(a.top - b.bottom, b.top - a.bottom, 0));
        const pointGap = (point, box) => Math.hypot(Math.max(box.left - point.x, point.x - box.right, 0), Math.max(box.top - point.y, point.y - box.bottom, 0));
        const link = svg.querySelector('.sd-graph-label-link');
        const tag = svg.querySelector('.sd-graph-selected-caption');
        const style = link && getComputedStyle(link);
        const linked = (node, shape, label) => {
            if (!node.classList.contains('is-selected') || !link || link.dataset.node !== node.dataset.node || style.display === 'none' || style.stroke === 'none' || Number(style.opacity) === 0 || Number.parseFloat(style.strokeWidth) < 1) return false;
            if (!tag || getComputedStyle(tag).display === 'none' || node.querySelector('.sd-graph-label').textContent !== 'Selected' || tag.dataset.node !== node.dataset.node) return false;
            const point = end => new DOMPoint(Number(link.getAttribute(`x${end}`)), Number(link.getAttribute(`y${end}`))).matrixTransform(link.getScreenCTM());
            return pointGap(point(1), shape) === 0 && pointGap(point(2), label) <= 10 * unit;
        };
        return [...svg.querySelectorAll('.sd-graph-node:not(.is-hidden)')].flatMap(node => {
            const text = node.querySelector('.sd-graph-label');
            if (getComputedStyle(text).display === 'none') return [];
            const label = text.getBoundingClientRect(), shape = node.querySelector('.sd-graph-shape').getBoundingClientRect();
            const inCanvas = shape.left >= canvas.left && shape.right <= canvas.right && shape.top >= canvas.top && shape.bottom <= canvas.bottom;
            const distance = gap(label, shape);
            return inCanvas && (distance <= 32 * unit || linked(node, shape, label)) ? [] : [{ owner: node.getAttribute('aria-label'), distance, inCanvas }];
        });
    });
    expect(failures, 'Every visible label must stay near its own visible node or explicitly link to that selected node').toEqual([]);
}

async function expectReadableLabel(page, node) {
    const label = node.locator('.sd-graph-label');
    await expect(label).toBeVisible();
    const box = await label.boundingBox(), canvas = await page.locator('#sdGraphCanvas').boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(12);
    expect(box.x).toBeGreaterThanOrEqual(canvas.x);
    expect(box.x + box.width).toBeLessThanOrEqual(canvas.x + canvas.width);
    expect(box.y).toBeGreaterThanOrEqual(canvas.y);
    expect(box.y + box.height).toBeLessThanOrEqual(canvas.y + canvas.height);
    await expectLabelAssociation(page);
}

async function expectSelectedLabelClear(page) {
    await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
    const overlaps = await page.locator('#sdGraphCanvas').evaluate(svg => {
        const label = svg.querySelector('.sd-graph-node.is-selected .sd-graph-label').getBoundingClientRect();
        const chip = svg.querySelector('.sd-graph-selected-caption');
        const bounds = svg.getBoundingClientRect(), boxes = [label];
        if (chip && getComputedStyle(chip).display !== 'none') {
            const rect = chip.getBoundingClientRect(); boxes.push(rect);
            if (rect.left < bounds.left || rect.right > bounds.right || rect.top < bounds.top || rect.bottom > bounds.bottom) return ['Selected chip outside canvas'];
            if (label.left < rect.left || label.right > rect.right || label.top < rect.top || label.bottom > rect.bottom) return ['Selected text outside its chip'];
        }
        return [...svg.querySelectorAll('.sd-graph-node:not(.is-hidden)')].flatMap(node => {
            const shape = node.querySelector('.sd-graph-shape').getBoundingClientRect();
            return boxes.some(box => Math.min(box.right, shape.right) > Math.max(box.left, shape.left) && Math.min(box.bottom, shape.bottom) > Math.max(box.top, shape.top)) ? [node.getAttribute('aria-label')] : [];
        });
    });
    expect(overlaps, 'Selected label must leave every visible node shape unobscured').toEqual([]);
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`634 five-node selection label clears node shapes at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const calls = await boot(page, acceptanceFixture()), before = calls.length;
        await page.locator('#sdGraphSearch').fill('Entity 51');
        const node = page.getByRole('button', { name: /^Inspect Entity 51 —/ }).first();
        await node.focus(); await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphMode')).toHaveText('Neighborhood · 5 received nodes');
        await expect(page.locator('.sd-graph-node:not(.is-hidden)')).toHaveCount(5);
        await expectSelectedLabelClear(page);
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, `five-node-clear-${width}`);
        await page.locator('#sdGraphDetails').getByRole('button', { name: /^Inspect Entity 52/ }).click();
        await page.locator('#sdGraphBack').click(); await expectSelectedLabelClear(page);
        await page.locator('#sdGraphZoomIn').click(); await expectSelectedLabelClear(page);
        await page.locator('#sdGraphFit').click(); await expectSelectedLabelClear(page);
        // The neighboring document still owns its SVG pointer/touch hit target.
        const sourceShape = page.locator('.sd-graph-node:not(.is-hidden).is-document .sd-graph-shape').first();
        if (width === 390) await sourceShape.tap(); else await sourceShape.click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('systemPatterns.md');
        await page.locator('#sdGraphBack').click(); await expectSelectedLabelClear(page);
        await expect(page.locator('.sd-graph-node:not(.is-hidden)')).toHaveCount(5);
        expect(calls.length).toBe(before);
        await expect(page.locator('#sdGraphDetails img')).toHaveCount(0);
    });
}

for (const neighbors of [3, 60, 120]) {
    test(`mobile selected label remains readable with ${neighbors} neighbors`, async ({ page }) => {
        await page.setViewportSize({ width: 390, height: 844 });
        const calls = await boot(page, neighbors === 3 ? graphFixture() : hubFixture(neighbors)), before = calls.length;
        await page.locator('#sdGraphSearch').fill('Hivemind');
        await page.locator('#sdGraphResults [data-node]').first().click();
        await expect(page.locator('#sdGraphMode')).toHaveText(`Neighborhood · ${neighbors + 1} received nodes`);
        await expectSelectedLabelClear(page);
        expect(await page.locator('.sd-graph-node.is-selected').evaluate(node => node === node.parentElement.lastElementChild)).toBeTruthy();
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded();
        await capture(page, `hub-${neighbors}-390`);
        await page.getByRole('button', { name: 'Inspect Project Mesh', exact: true }).last().click();
        await page.locator('#sdGraphBack').click();
        await expectSelectedLabelClear(page);
        await page.locator('#sdGraphZoomIn').tap(); await page.locator('#sdGraphZoomOut').tap();
        await page.locator('#sdGraphFit').click();
        await expectSelectedLabelClear(page);
        expect(calls.length).toBe(before);
    });
}

for (const width of [1440, 390]) {
    test(`SVG pointer selection keeps its hit target at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        const graph = graphFixture(); graph.nodes = graph.nodes.slice(0, 3); graph.edges = graph.edges.slice(0, 2);
        const calls = await boot(page, graph), before = calls.length;
        const node = page.getByRole('button', { name: 'Inspect Hivemind', exact: true });
        if (width === 390) await node.tap(); else await node.click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
        await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
        expect(calls.length).toBe(before);
    });


}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`best-effort salient overview labels stay associated or suppressed at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        await boot(page);
        const salient = page.locator('.sd-graph-node[aria-label="Inspect Hivemind"]');
        await expect(salient.locator('.sd-graph-shape')).toBeVisible();
        await expect(salient).toHaveAttribute('aria-label', 'Inspect Hivemind');
        await expectLabelAssociation(page);
        await expect(page.locator('.sd-graph-label-link')).toHaveCSS('display', 'none');
        await salient.focus(); await expectLabelAssociation(page);
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, `associated-overview-${width}`);
    });
}

for (const width of [1440, 768, 390]) {
    for (const neighbors of [60, 120]) {
        test(`long hub labels retain ownership through focus pan and zoom at ${width}px with ${neighbors} neighbors`, async ({ page }) => {
            await page.setViewportSize({ width, height: width === 768 ? 1024 : width === 390 ? 844 : 900 });
            const graph = hubFixture(neighbors); graph.nodes[0].label = 'Hivemind — collective awareness and memory';
            const calls = await boot(page, graph), before = calls.length;
            await expectLabelAssociation(page);
            const neighbor = page.locator('.sd-graph-node[aria-label="Inspect Project Mesh"]');
            await neighbor.focus(); await expectLabelAssociation(page);
            await page.locator('.sd-graph-node[aria-label="Inspect Hivemind — collective awareness and memory"] .sd-graph-shape').hover();
            await expectLabelAssociation(page);
            await page.locator('#sdGraphSearch').fill('Hivemind'); await page.locator('#sdGraphResults [data-node]').first().click();
            await neighbor.focus(); await expectSelectedLabelClear(page);
            if (width !== 1440) await expect(page.locator('.sd-graph-selected-caption')).toBeVisible();
            await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, `associated-hub-${neighbors}-${width}`);
            await page.locator('#sdGraphZoomIn').click(); await expectSelectedLabelClear(page);
            // Zoom may crop the desktop ring's top neighbor. Restore it via
            // the real Fit control before asking the operator to click it.
            const frame = await page.locator('#sdGraphCanvas').boundingBox();
            const projected = await neighbor.locator('.sd-graph-shape').boundingBox();
            if (projected.y < frame.y || projected.y + projected.height > frame.y + frame.height) await page.locator('#sdGraphFit').click();
            await neighbor.locator('.sd-graph-shape').scrollIntoViewIfNeeded();
            const shapeBox = await neighbor.locator('.sd-graph-shape').boundingBox();
            const hitPoint = { x: shapeBox.x + shapeBox.width / 2, y: shapeBox.y + shapeBox.height / 2 };
            const hitFrame = await page.locator('#sdGraphCanvas').boundingBox();
            expect(hitPoint.x).toBeGreaterThanOrEqual(hitFrame.x); expect(hitPoint.x).toBeLessThanOrEqual(hitFrame.x + hitFrame.width);
            expect(hitPoint.y).toBeGreaterThanOrEqual(hitFrame.y); expect(hitPoint.y).toBeLessThanOrEqual(hitFrame.y + hitFrame.height);
            // Dense ring hit circles may overlap; the selected caption's empty
            // group rectangle must never intercept a real neighbor target.
            const hitId = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.sd-graph-node')?.dataset.node, hitPoint);
            expect(hitId).toBeDefined(); expect(hitId).not.toBe('n1');
            const hitNode = graph.nodes.find(node => node.id === hitId);
            if (width === 390) await page.touchscreen.tap(hitPoint.x, hitPoint.y); else await page.mouse.click(hitPoint.x, hitPoint.y);
            await expect(page.locator('#sdGraphDetails h4')).toHaveText(hitNode.filename || hitNode.label);
            await page.locator('#sdGraphBack').click(); await expectSelectedLabelClear(page);
            await page.locator('#sdGraphFit').click(); await expectSelectedLabelClear(page);
            const canvas = page.locator('#sdGraphCanvas'); await canvas.scrollIntoViewIfNeeded();
            const box = await canvas.boundingBox();
            const prior = await page.locator('#sdGraphViewport').getAttribute('transform');
            await page.mouse.move(box.x + 4, box.y + box.height - 4); await page.mouse.down();
            await page.mouse.move(box.x + 4 + box.width * .8, box.y + box.height - 24, { steps: 3 }); await page.mouse.up();
            expect(await page.locator('#sdGraphViewport').getAttribute('transform')).not.toBe(prior);
            const selected = page.locator('.sd-graph-node.is-selected');
            expect((await selected.locator('.sd-graph-shape').boundingBox()).x).toBeGreaterThan(box.x + box.width);
            await expectLabelAssociation(page);
            await expect(selected.locator('.sd-graph-label')).toBeHidden();
            await expect(page.locator('#sdGraphDetails h4')).toHaveText(graph.nodes[0].label);
            await page.locator('#sdGraphFit').click(); await expectSelectedLabelClear(page);
            expect(calls.length).toBe(before);
        });
    }
}

for (const width of [1440, 768, 390]) {
    test(`selected chip reserves its full box for short and wide names at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: width === 768 ? 1024 : width === 390 ? 844 : 900 });
        const graph = hubFixture(60); graph.nodes[0].label = 'I';
        await boot(page, graph);
        await page.locator('#sdGraphSearch').fill('I'); await page.locator('#sdGraphResults [data-node]').first().click();
        for (let i = 0; i < 10; i++) await page.locator('#sdGraphZoomOut').click();
        await expect(page.locator('.sd-graph-selected-caption')).toBeVisible();
        await expect(page.locator('.sd-graph-node.is-selected .sd-graph-label')).toHaveText('Selected');
        await expectSelectedLabelClear(page);
        graph.nodes[0].label = 'Ｈ'.repeat(40);
        await page.locator('[data-action="sd-long-refresh"]').click();
        await expect(page.locator('.sd-graph-node').filter({ has: page.locator(`title`, { hasText: graph.nodes[0].label }) })).toHaveCount(1);
        await page.locator('#sdGraphSearch').fill('Ｈ'); await page.locator('#sdGraphResults [data-node]').first().click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText(graph.nodes[0].label);
        await expectSelectedLabelClear(page);
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, `associated-wide-${width}`);
    });
}

test('mobile long selected label fits the canvas and outranks neighbor focus', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const graph = hubFixture(60); graph.nodes[0].label = 'Hivemind — collective awareness and memory';
    const calls = await boot(page, graph), before = calls.length;
    await page.locator('#sdGraphSearch').fill('Hivemind'); await page.locator('#sdGraphResults [data-node]').first().click();
    await page.locator('.sd-graph-node[aria-label="Inspect Project Mesh"]').focus();
    await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
    await expect(page.locator('#sdGraphDetails h4')).toHaveText(graph.nodes[0].label);
    await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, 'hub-long-label-390');
    expect(calls.length).toBe(before);
});

for (const width of [1440, 768, 390]) {
    test(`late font readiness preserves selected label geometry at ${width}px`, async ({ page }) => {
        await page.addInitScript(() => {
            const ready = new Promise(resolve => { window.releaseGraphFonts = resolve; });
            Object.defineProperty(document.fonts, 'status', { get: () => 'loading' });
            Object.defineProperty(document.fonts, 'ready', { get: () => ready });
            const measure = SVGTextContentElement.prototype.getComputedTextLength;
            window.graphLabelReads = 0;
            SVGTextContentElement.prototype.getComputedTextLength = function (...args) {
                if (this.classList.contains('sd-graph-label')) window.graphLabelReads++;
                return measure.apply(this, args);
            };
        });
        await page.setViewportSize({ width, height: 900 });
        const graph = hubFixture(60); graph.nodes[0].label = 'Hivemind — collective awareness and memory';
        const calls = await boot(page, graph), before = calls.length;
        await page.locator('#sdGraphSearch').fill('Hivemind'); await page.locator('#sdGraphResults [data-node]').first().click();
        await page.locator('.sd-graph-node[aria-label="Inspect Project Mesh"]').focus();
        await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
        const reads = await page.evaluate(() => window.graphLabelReads);
        // Resolve after layout/focus, with the actual bundled label font loaded.
        await page.evaluate(async () => { await document.fonts.load('13px "JetBrains Mono"'); window.releaseGraphFonts(); });
        await expect.poll(() => page.evaluate(() => window.graphLabelReads)).toBe(reads + 160);
        await expectSelectedLabelClear(page);
        await page.locator('#sdGraphZoomIn').click(); await page.locator('#sdGraphFit').click();
        await expectSelectedLabelClear(page);
        expect(await page.evaluate(() => window.graphLabelReads)).toBe(reads + 160);
        expect(calls.length).toBe(before);
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded(); await capture(page, `late-font-${width}`);
    });
}

test('text widths are measured per mount, never during pan zoom or resize', async ({ page }) => {
    await page.addInitScript(() => {
        const original = SVGTextContentElement.prototype.getComputedTextLength;
        window.graphLabelReads = 0;
        SVGTextContentElement.prototype.getComputedTextLength = function (...args) {
            if (this.classList.contains('sd-graph-label')) window.graphLabelReads++;
            return original.apply(this, args);
        };
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    const graph = graphFixture(), calls = await boot(page, graph), before = calls.length;
    await page.evaluate(() => document.fonts.ready);
    const reads = await page.evaluate(() => window.graphLabelReads);
    expect(reads).toBeGreaterThanOrEqual(160); expect(reads).toBeLessThanOrEqual(320);
    await page.locator('#sdGraphSearch').fill('Hivemind');
    await page.locator('#sdGraphResults [data-node]').first().click();
    for (let i = 0; i < 3; i++) {
        await page.locator('#sdGraphZoomIn').click(); await page.locator('#sdGraphZoomOut').click();
    }
    const canvas = page.locator('#sdGraphCanvas'); await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + 8, box.y + 8); await page.mouse.wheel(0, -100);
    await page.mouse.down(); await page.mouse.move(box.x + 60, box.y + 45, { steps: 3 }); await page.mouse.up();
    await page.locator('#sdGraphFit').click();
    await page.setViewportSize({ width: 390, height: 844 }); await page.locator('#sdGraphFit').click();
    await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
    expect(await page.evaluate(() => window.graphLabelReads)).toBe(reads);
    expect(calls.length).toBe(before);
    graph.nodes[0].label = 'Refreshed Hivemind label with a longer name';
    await page.locator('[data-action="sd-long-refresh"]').click();
    await expect(page.locator('.sd-graph-node[aria-label="Inspect Refreshed Hivemind label with a longer name"]')).toBeAttached();
    await page.evaluate(() => document.fonts.ready);
    const refreshedReads = await page.evaluate(() => window.graphLabelReads);
    expect(refreshedReads - reads).toBeGreaterThanOrEqual(160);
    expect(refreshedReads - reads).toBeLessThanOrEqual(320);
    await page.locator('#sdGraphSearch').fill('Refreshed Hivemind');
    await page.locator('#sdGraphResults [data-node]').first().click();
    await expectReadableLabel(page, page.locator('.sd-graph-node.is-selected'));
    expect(await page.evaluate(() => window.graphLabelReads)).toBe(refreshedReads);
    expect(calls.filter(call => call.tool === 'graph_status')).toHaveLength(2);
});

async function boot(page, graph = graphFixture()) {
    const calls = [];
    await page.route('**/*', async route => {
        const pathname = new URL(route.request().url()).pathname;
        const json = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
        if (pathname === '/health') return json({ version: 'graph-627-test' });
        if (pathname === '/api/spaces') return json({ status: 'ok', spaces: [{ space_id: 'demo' }] });
        if (pathname === '/api/tool') {
            const body = route.request().postDataJSON(); calls.push(body);
            if (body.tool === 'system_whoami') return json({ status: 'ok', client_name: 'Graph operator', auth_type: 'stored', permissions: ['read', 'write', 'manage'] });
            if (body.tool === 'space_info') return json({ status: 'ok', space_id: 'demo', description: 'Graph QA', owner: 'qa', hive_status_label: 'local_only', live: { notes_count: 0 }, bank: { files_count: 0 }, consolidation_queue: { lane_state: 'idle', latest_jobs: [], queued_job_ids: [] } });
            if (body.tool === 'graph_status') return json({ status: 'ok', connected: true, reachable: true, binding: 'embedded', graph_view: graph });
            if (body.tool === 'long_document_list') return json({ status: 'ok', documents: [], total: 0, limit: 50, offset: 0 });
            throw new Error(`Unexpected request: ${body.tool}`);
        }
        const relative = pathname === '/admin.html' ? 'admin.html' : pathname.startsWith('/static/') ? pathname.slice(8) : '';
        if (relative && !relative.includes('..') && fs.existsSync(path.join(STATIC, relative))) {
            const contentType = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' }[path.extname(relative)];
            return route.fulfill({ contentType, body: fs.readFileSync(path.join(STATIC, relative)) });
        }
        return route.fulfill({ status: 404, body: '' });
    });
    await page.goto(`${ORIGIN}/admin.html#/spaces/demo/long/graph`);
    await expect(page.locator('.sd-graph-node')).toHaveCount(graph.nodes.length);
    return calls;
}

async function capture(page, name) {
    if (!PROOF) return;
    fs.mkdirSync(PROOF, { recursive: true });
    await page.screenshot({ path: path.join(PROOF, name + '.png'), fullPage: true });
}

for (const [width, height] of [[1440, 900], [390, 844]]) {
    test(`empty projection guides explicit panel navigation ${width}x${height}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const calls = await boot(page, { status: 'ok', nodes: [], edges: [], total_node_count: 0, total_edge_count: 0 });
        const empty = page.locator('.sd-graph-empty');
        await expect(empty).toContainText('No nodes in this graph view');
        await expect(empty).toContainText('Explore your documents or check LONG status in Overview.');
        await expect(page.locator('#sdGraphCanvas, #sdGraphDetails')).toHaveCount(0);
        await empty.scrollIntoViewIfNeeded();
        await capture(page, `empty-${width}`);
        expect(calls.filter(c => c.tool === 'graph_status')).toHaveLength(1);
        expect(calls.filter(c => c.tool === 'long_document_list')).toHaveLength(0);
        const actions = empty.locator('button');
        const sizes = await actions.evaluateAll(buttons => buttons.map(b => b.getBoundingClientRect().height));
        expect(sizes.every(h => h >= 44)).toBeTruthy();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
        await empty.getByRole('button', { name: 'Open Overview', exact: true }).focus();
        await page.keyboard.press('Enter');
        await expect(page).toHaveURL(/\/long\/overview$/);
        await expect(page.locator('#sdLongTab-overview')).toBeFocused();
        await page.locator('#sdLongTab-graph').click();
        await expect(empty).toBeVisible();
        const documents = empty.getByRole('button', { name: 'Open Documents', exact: true });
        if (width === 390) await documents.tap(); else await documents.click();
        await expect(page).toHaveURL(/\/long\/documents$/);
        await expect(page.locator('#sdLongTab-documents')).toBeFocused();
        await expect.poll(() => calls.filter(c => c.tool === 'long_document_list').length).toBe(1);
        expect(calls.every(c => ['system_whoami', 'space_info', 'graph_status', 'long_document_list'].includes(c.tool))).toBeTruthy();
    });

    test(`zero local matches clear stale inspector and restore search focus ${width}x${height}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const calls = await boot(page), before = calls.length;
        const search = page.locator('#sdGraphSearch'), results = page.locator('#sdGraphResults');
        await search.fill('Hivemind');
        await results.locator('[data-node]').first().click();
        await page.getByRole('button', { name: 'Inspect Project Mesh', exact: true }).last().click();
        await search.fill('No such node 627');
        await expect(results).toContainText('No matching nodes in this graph view');
        await expect(results).toContainText('Try another name or type, or clear the search.');
        await expect(search).toBeFocused();
        await expect(page.locator('.sd-graph-node.is-selected')).toHaveCount(0);
        await expect(page.locator('.sd-graph-node.is-hidden')).toHaveCount(0);
        await expect(page.locator('#sdGraphDetails')).not.toContainText('Project Mesh');
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Node details');
        await expect(page.locator('#sdGraphBack, #sdGraphSources')).toHaveCount(0);
        await page.keyboard.press('Enter');
        await expect(search).toHaveValue('No such node 627');
        await results.scrollIntoViewIfNeeded();
        await capture(page, `zero-matches-${width}`);
        const clear = results.getByRole('button', { name: 'Clear search', exact: true });
        expect((await clear.boundingBox()).height).toBeGreaterThanOrEqual(44);
        await clear.focus(); await page.keyboard.press('Enter');
        await expect(search).toHaveValue(''); await expect(search).toBeFocused();
        await expect(results).toBeEmpty();
        await search.fill('Hivemind'); await results.locator('[data-node]').first().click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
        await expect(page.locator('#sdGraphBack')).toBeDisabled();
        expect(calls.length).toBe(before);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    });
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
    test(`dense graph local keyboard journey ${width}x${height}`, async ({ page }) => {
        await page.setViewportSize({ width, height });
        const calls = await boot(page);
        await page.locator('#sdGraphCanvas').scrollIntoViewIfNeeded();
        await capture(page, `overview-${width}`);
        // Capture-only runs report SKIPPED, never acceptance GREEN.
        test.skip(!!process.env.GRAPH_627_BASELINE, 'Baseline capture only; acceptance journey not executed');
        await expect(page.locator('.sd-graph-counts')).toHaveText('160 / 223 nodes · 320 / 420 relations');
        await expect(page.locator('.sd-graph-legend')).toContainText('Document');
        const before = calls.length;
        await page.locator('#sdGraphSearch').fill('Hivemind');
        const result = page.locator('#sdGraphResults button').first();
        await result.focus(); await page.keyboard.press('Enter');
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
        await expect(page.locator('#sdGraphDetails h4')).toBeFocused();
        await expect(page.locator('.sd-graph-node.is-selected')).toHaveAttribute('aria-pressed', 'true');
        await expect(page.locator('#sdGraphDetails')).toContainText('Outgoing relations · 1');
        await expect(page.locator('#sdGraphDetails')).toContainText('Incoming relations · 2');
        await expect(page.locator('#sdGraphDetails')).toContainText('USES');
        await expect(page.locator('#sdGraphDetails')).toContainText('SUPPORTS');
        const neighbor = page.getByRole('button', { name: 'Inspect Project Mesh', exact: true }).last();
        if (width === 390) await neighbor.tap(); else await neighbor.click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Project Mesh');
        await page.locator('#sdGraphBack').click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
        await page.locator('#sdGraphSources button').click();
        await expect(page.locator('#sdGraphDetails h4')).toHaveText('architecture-1.md');
        await expect(page.locator('#sdGraphDetails')).toContainText('Document reader identity is not returned');
        await expect(page.locator('#sdGraphDetails a')).toHaveCount(0);
        await page.locator('#sdGraphBack').click();
        if (width === 390) {
            await page.locator('#sdGraphZoomIn').tap();
            await page.locator('#sdGraphZoomOut').tap();
        } else {
            await page.locator('#sdGraphZoomIn').click();
            await page.locator('#sdGraphZoomOut').click();
        }
        await page.locator('#sdGraphFit').click();
        await expectSelectedLabelClear(page);
        await expect(page.locator('#sdGraphDetails')).toContainText('Relations within this snapshot');
        expect(calls.length).toBe(before);
        // Separate inspector never overlaps the canvas, including tablet/mobile.
        const canvas = await page.locator('#sdGraphCanvas').boundingBox();
        const inspector = await page.locator('#sdGraphDetails').boundingBox();
        expect(inspector.x >= canvas.x + canvas.width - 1 || inspector.y >= canvas.y + canvas.height - 1).toBeTruthy();
        const sizes = await page.locator('.sd-graph-controls button, #sdGraphDetails button').evaluateAll(buttons => buttons.map(b => ({ h: b.getBoundingClientRect().height, w: b.getBoundingClientRect().width })));
        expect(sizes.every(s => s.h >= 44 && s.w >= 44)).toBeTruthy();
        await capture(page, `selected-${width}`);
        await page.locator('#sdGraphDetails h4').scrollIntoViewIfNeeded();
        await capture(page, `inspector-${width}`);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    });
}

test('Fit uses visible bounds after zoom/pan and labels never shrink below reading size', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await boot(page);
    await page.locator('#sdGraphZoomIn').click();
    const canvas = page.locator('#sdGraphCanvas');
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + 10, box.y + 10); await page.mouse.down();
    await page.mouse.move(box.x + 110, box.y + 90); await page.mouse.up();
    await page.locator('#sdGraphFit').click();
    const geometry = await canvas.evaluate(svg => {
        const screen = svg.getBoundingClientRect();
        return [...svg.querySelectorAll('.sd-graph-node')].filter(g => !g.classList.contains('is-hidden')).map(g => {
            const r = g.querySelector('.sd-graph-shape').getBoundingClientRect();
            return r.left >= screen.left && r.right <= screen.right && r.top >= screen.top && r.bottom <= screen.bottom;
        });
    });
    expect(geometry.every(Boolean)).toBeTruthy();
    await expectLabelAssociation(page);
    const labels = await page.locator('.sd-graph-label').evaluateAll(labels => labels.filter(l => l.style.display !== 'none').map(l => l.getBoundingClientRect()));
    expect(labels.length).toBeGreaterThan(0); expect(labels.length).toBeLessThan(20);
    expect(labels.every(l => l.height >= 12)).toBeTruthy();
    for (let i = 0; i < labels.length; i++) for (let j = 0; j < i; j++) {
        const a = labels[i], b = labels[j];
        expect(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top).toBeTruthy();
    }
});

test('missing neighbor and hostile labels stay honest and literal', async ({ page }) => {
    const graph = graphFixture();
    graph.edges[0].to = 'absent';
    graph.nodes[0].description = '<img src=x onerror="window.graphXss=1">';
    const calls = await boot(page, graph);
    await page.locator('#sdGraphSearch').fill('Hivemind');
    await page.locator('#sdGraphResults button').first().click();
    await expect(page.locator('#sdGraphDetails')).toContainText('Neighbor not in this snapshot');
    await expect(page.locator('#sdGraphDetails img')).toHaveCount(0);
    expect(await page.evaluate(() => window.graphXss)).toBeUndefined();
    expect(calls.filter(c => c.tool === 'graph_status')).toHaveLength(1);
});

test('200% zoom and viewport resize preserve readable local selection', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await boot(page);
    // CSS zoom scales layout and controls; DPR alone is not browser zoom.
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    await page.locator('#sdGraphSearch').fill('Hivemind');
    await page.locator('#sdGraphResults button').first().click();
    await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
    await expectSelectedLabelClear(page);
    const canvas = await page.locator('#sdGraphCanvas').boundingBox();
    expect(canvas.width).toBeGreaterThan(250);
    await page.locator('#sdGraphDetails h4').scrollIntoViewIfNeeded();
    await capture(page, 'zoom-200');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => { document.documentElement.style.zoom = ''; });
    await page.locator('#sdGraphFit').click();
    await expect(page.locator('#sdGraphDetails h4')).toHaveText('Hivemind');
    await expectSelectedLabelClear(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test('refresh discards synthetic IDs and detached selection handlers', async ({ page }) => {
    const graph = graphFixture();
    const calls = await boot(page, graph);
    await page.locator('#sdGraphSearch').fill('Hivemind');
    const stale = await page.locator('#sdGraphResults button').first().elementHandle();
    await stale.click();
    graph.nodes[0].label = 'Different snapshot node';
    await page.locator('[data-action="sd-long-refresh"]').click();
    await expect(page.locator('#sdGraphDetails h4')).toHaveText('Node details');
    await stale.evaluate(button => button.click());
    await expect(page.locator('#sdGraphDetails h4')).toHaveText('Node details');
    await expect(page.locator('.sd-graph-node.is-selected')).toHaveCount(0);
    expect(calls.filter(c => c.tool === 'graph_status')).toHaveLength(2);
});
