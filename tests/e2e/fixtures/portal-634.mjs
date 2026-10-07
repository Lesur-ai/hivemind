// Synthetic contract data only. No origin/category is inferred from a name.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test as baseTest, expect, chromium } from '@playwright/test';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const STATIC = path.join(ROOT, 'src/live_mem/static');
const zoomSessions = new WeakMap();
const nativeZoomPages = new WeakMap();

// The CI container copies only static/test trees. Missing Git is an explicit
// provenance limit in portable runs; qualification requires a clean checkout.
export function sourceProvenance({ strict = false, root = ROOT } = {}) {
    const mode = strict ? 'qualification' : 'portable';
    try {
        const options = { cwd: root, encoding: 'utf8', timeout: 3000, stdio: ['ignore', 'pipe', 'pipe'] };
        const checkout = execFileSync('git', ['rev-parse', '--show-toplevel'], options).trim();
        if (fs.realpathSync(checkout) !== fs.realpathSync(root)) throw new Error('Source root is not the Git checkout root');
        const sourceHead = execFileSync('git', ['rev-parse', 'HEAD'], options).trim();
        if (!/^[0-9a-f]{40}$/.test(sourceHead)) throw new Error('Invalid Git HEAD');
        const dirty = execFileSync('git', ['status', '--porcelain'], options).trim();
        if (strict && dirty) throw new Error('Qualification requires a clean checkout');
        return { sourceHead, dirty, provenance: { mode, status: 'git-verified' } };
    } catch (error) {
        if (strict) throw new Error(`Portal qualification provenance unavailable: ${error.message}`);
        return { sourceHead: null, dirty: null, provenance: { mode, status: 'unavailable', reason: 'Git executable or checkout unavailable; source SHA and cleanliness are not established' } };
    }
}

// The local-only native project reuses the strict journeys with a fresh Chrome
// profile. Portable CI continues to use Playwright's ordinary page fixture.
export const test = baseTest.extend({
    page: async ({ page }, use, info) => {
        if (info.project.metadata.nativeChromeZoom !== 200) return use(page);
        const profile = fs.mkdtempSync(info.outputPath('chrome-zoom-profile-'));
        const context = await chromium.launchPersistentContext(profile, {
            channel: 'chrome', headless: true, viewport: { width: 1440, height: 900 },
        });
        try {
            const settings = context.pages()[0];
            await settings.goto('chrome://settings/appearance');
            await settings.locator('#zoomLevel').selectOption({ label: '200%' });
            const settingsValue = await settings.locator('#zoomLevel').inputValue();
            await settings.locator('#zoomLevel').scrollIntoViewIfNeeded();
            await settings.screenshot({ path: info.outputPath('chrome-settings-200.png') });
            await info.attach('Chrome Settings zoom 200%', { path: info.outputPath('chrome-settings-200.png'), contentType: 'image/png' });
            const nativePage = await context.newPage();
            nativeZoomPages.set(nativePage, { settingsValue, browserVersion: context.browser().version(), headless: true,
                windowSizing: 'Playwright-managed 1440x900 window; native page zoom, no emulated DPR or CSS zoom' });
            await use(nativePage);
        } finally { await context.close(); }
    },
});
export const ORIGIN = 'http://portal-634.e2e';
export const HOSTILE = '<img src=x onerror="window.__portal634Xss=1">';
export const VIEWPORTS = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 768, height: 1024 },
    { name: 'mobile', width: 390, height: 844 },
    { name: 'zoom-200', width: 1440, height: 900, zoom: 2 },
];
const stamp = '2026-09-30T12:00:00Z';
const hash = i => crypto.createHash('sha256').update(`synthetic-source-${i}`).digest('hex');
const types = ['Product', 'Protocol', 'Person', 'Team', 'Decision', 'Evidence', 'Service', 'Process', 'Policy', 'Region', 'Metric', 'Event', 'Asset', 'Constraint', 'Long entity type with operator-readable words'];

export function homeInventory(count) {
    if (![0, 1, 20, 25].includes(count)) throw new Error('Unsupported acceptance inventory size');
    // Coprime permutations keep recency, ASCII IDs and reversed catalog distinct.
    const byRank = new Map();
    const byId = Array.from({ length: count }, (_, i) => {
        const rank = (i * 7 + 3) % count, space_id = `space-${String(i + 1).padStart(2, '0')}`;
        byRank.set(rank, space_id);
        return { space_id, description: `Synthetic retained project ${i + 1}. ${HOSTILE}`,
            last_consolidation: new Date(Date.UTC(2026, 8, 1, 12, rank)).toISOString(),
            consolidation_count: rank + 1, total_notes_processed: (rank + 1) * 23,
            live_notes_count: 1000 + i, bank_files_count: 100 + i };
    });
    const spaces = byId.map((_, i) => byId[(i * 3 + 1) % count]);
    return { spaces, expectedRecent: Array.from({ length: count }, (_, i) => byRank.get(count - 1 - i)),
        lanes: spaces.map(s => ({ space_id: s.space_id, lane_state: 'idle', queued_count: 0, running_job: null, queued_jobs: [], latest_jobs: [] })) };
}

export async function tabTo(page, target, maxSteps = 400) {
    await expect(target).toHaveCount(1);
    for (let step = 0; step < maxSteps; step++) {
        await page.keyboard.press('Tab');
        if (await target.evaluate(el => el === document.activeElement)) return step + 1;
    }
    throw new Error(`Target is unreachable through ${maxSteps} ordinary Tab presses`);
}

export async function selectedLabelGeometry(page) {
    return page.locator('#sdGraphCanvas').evaluate(canvas => {
        const clip = canvas.getBoundingClientRect();
        const visible = el => {
            let opacity = 1;
            for (let n = el; n instanceof Element; n = n.parentElement) {
                const s = getComputedStyle(n);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                opacity *= Number(s.opacity);
            }
            const r = el.getBoundingClientRect();
            return opacity >= .5 && r.width > 0 && r.height > 0 && r.right > clip.left && r.left < clip.right && r.bottom > clip.top && r.top < clip.bottom;
        };
        const selected = canvas.querySelector('.sd-graph-node.is-selected text');
        if (!selected || !visible(selected)) throw new Error('Selected canvas label must be visible');
        const label = selected.getBoundingClientRect().toJSON();
        const neighbors = [...canvas.querySelectorAll('.sd-graph-node:not(.is-selected) .sd-graph-shape')].filter(visible).map(shape => ({
            name: shape.closest('.sd-graph-node').getAttribute('aria-label'), tag: shape.tagName, box: shape.getBoundingClientRect().toJSON(),
        }));
        const overlaps = neighbors.filter(({ box: b, tag }) => {
            const left = Math.max(label.left, b.left, clip.left), right = Math.min(label.right, b.right, clip.right);
            const top = Math.max(label.top, b.top, clip.top), bottom = Math.min(label.bottom, b.bottom, clip.bottom);
            if (right <= left || bottom <= top) return false;
            if (tag === 'circle') {
                const cx = (b.left + b.right) / 2, cy = (b.top + b.bottom) / 2;
                const x = Math.max(left, Math.min(cx, right)), y = Math.max(top, Math.min(cy, bottom));
                return ((x - cx) / (b.width / 2)) ** 2 + ((y - cy) / (b.height / 2)) ** 2 < 1;
            }
            return true;
        });
        return { label, neighbors, overlaps };
    });
}

export async function graphLabelAssociations(page) {
    return page.locator('#sdGraphCanvas').evaluate(canvas => {
        const clip = canvas.getBoundingClientRect();
        const unit = clip.width / canvas.clientWidth;
        if (!Number.isFinite(unit) || unit <= 0) throw new Error('Canvas screen unit must be measurable');
        const contains = (outer, inner) => inner.left >= outer.left - unit && inner.right <= outer.right + unit && inner.top >= outer.top - unit && inner.bottom <= outer.bottom + unit;
        const visible = el => {
            let opacity = 1;
            for (let n = el; n instanceof Element; n = n.parentElement) {
                const style = getComputedStyle(n);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                opacity *= Number(style.opacity);
            }
            const r = el.getBoundingClientRect();
            return opacity >= .5 && r.width > 0 && r.height > 0 && r.right > clip.left && r.left < clip.right && r.bottom > clip.top && r.top < clip.bottom;
        };
        const shapes = [...canvas.querySelectorAll('.sd-graph-node .sd-graph-shape')].filter(visible).map(shape => ({
            node: shape.closest('.sd-graph-node'), box: shape.getBoundingClientRect().toJSON(),
        }));
        const gap = (a, b) => Math.hypot(Math.max(0, a.left - b.right, b.left - a.right), Math.max(0, a.top - b.bottom, b.top - a.bottom));
        const labels = [...canvas.querySelectorAll('.sd-graph-node text')].filter(visible).map(label => {
            const node = label.closest('.sd-graph-node'), box = label.getBoundingClientRect().toJSON();
            const own = shapes.find(shape => shape.node === node);
            const others = shapes.filter(shape => shape.node !== node).map(shape => ({ name: shape.node.getAttribute('aria-label'), distance: gap(box, shape.box) })).sort((a, b) => a.distance - b.distance);
            const ownDistance = own ? gap(box, own.box) : null;
            const selected = node.classList.contains('is-selected'), nodeId = node.getAttribute('data-node');
            const ownInCanvas = Boolean(own && contains(clip, own.box));
            const captions = [...canvas.querySelectorAll('rect.sd-graph-selected-caption')].filter(caption => nodeId && caption.getAttribute('data-node') === nodeId);
            const caption = captions.length === 1 ? captions[0] : null, captionBox = caption?.getBoundingClientRect().toJSON();
            const captionStyle = caption && getComputedStyle(caption);
            const captionShapeOverlaps = captionBox ? shapes.filter(shape => Math.min(captionBox.right, shape.box.right) > Math.max(captionBox.left, shape.box.left) && Math.min(captionBox.bottom, shape.box.bottom) > Math.max(captionBox.top, shape.box.top)).map(shape => shape.node.getAttribute('aria-label')) : [];
            const captionVerified = Boolean(selected && caption && visible(caption) && captionStyle.fill !== 'none' && captionStyle.fill !== 'transparent'
                && captionStyle.stroke !== 'none' && parseFloat(captionStyle.strokeWidth) >= 1 && label.textContent === 'Selected'
                && contains(clip, captionBox) && contains(captionBox, box) && !captionShapeOverlaps.length);
            const links = [...canvas.querySelectorAll('line.sd-graph-label-link')].filter(link => nodeId && link.getAttribute('data-node') === nodeId);
            const leaders = links.map(link => {
                let opacity = 1, painted = true;
                for (let n = link; n instanceof Element; n = n.parentElement) {
                    const style = getComputedStyle(n);
                    if (style.display === 'none' || style.visibility === 'hidden') painted = false;
                    opacity *= Number(style.opacity);
                }
                const style = getComputedStyle(link), matrix = link.getScreenCTM();
                const alphaMatch = /(?:,|\/)\s*([0-9.]+)\s*\)$/.exec(style.stroke);
                const strokeAlpha = style.stroke.startsWith('rgba(') || style.stroke.includes('/') ? Number(alphaMatch?.[1] || 0) : 1;
                painted = painted && opacity >= .5 && style.stroke !== 'none' && style.stroke !== 'transparent'
                    && opacity * Number(style.strokeOpacity) * strokeAlpha >= .5 && parseFloat(style.strokeWidth) >= 1;
                const values = ['x1', 'y1', 'x2', 'y2'].map(name => link.hasAttribute(name) ? Number(link.getAttribute(name)) : NaN);
                if (!matrix || !values.every(Number.isFinite)) return { painted, verified: false, reason: 'Missing finite screen geometry' };
                const start = new DOMPoint(values[0], values[1]).matrixTransform(matrix), end = new DOMPoint(values[2], values[3]).matrixTransform(matrix);
                const startDistance = own ? Math.hypot(start.x - (own.box.left + own.box.right) / 2, start.y - (own.box.top + own.box.bottom) / 2) : null;
                const endDistance = gap(box, { left: end.x, right: end.x, top: end.y, bottom: end.y });
                const inCanvas = [start, end].every(p => p.x >= clip.left && p.x <= clip.right && p.y >= clip.top && p.y <= clip.bottom);
                return { painted, start: { x: start.x, y: start.y }, end: { x: end.x, y: end.y }, startDistance, endDistance, inCanvas,
                    verified: Boolean(captionVerified && ownInCanvas && painted && inCanvas && startDistance <= 2 * unit && endDistance <= 2 * unit) };
            });
            const indicator = selected && label.textContent === 'Selected' && node.getAttribute('aria-label') !== 'Inspect Selected';
            const near = Boolean(ownInCanvas && !indicator && ownDistance <= 32 * unit);
            const linked = leaders.length === 1 && leaders[0].verified;
            return { name: node.getAttribute('aria-label'), nodeId, selected, label: box, ownShape: own?.box || null,
                ownDistance, ownInCanvas, nearestOther: others[0] || null, caption: captionBox || null, captionVerified, captionShapeOverlaps,
                leaders, associationMethod: near ? 'near-own-shape' : linked ? 'verified-selected-chip-leader' : null,
                associated: near || linked };
        });
        return { canvasUnit: unit, maximumNearCanvasUnits: 32, maximumLeaderEndCanvasUnits: 2, labels, detachedLabels: labels.filter(label => !label.associated) };
    });
}

export function realisticData() {
    const documents = Array.from({ length: 50 }, (_, i) => ({
        document_id: `doc-${i + 1}`, filename: 'systemPatterns.md',
        source_path: `synthetic/team-${String(i + 1).padStart(2, '0')}/decisions/retained-evidence-with-a-long-path/systemPatterns.md`,
        repo_path: `synthetic-repo/team-${i + 1}`, sha256: hash(i),
        ingestion_status: ['succeeded', 'running', 'failed', 'queued', 'unknown_future_status'][i % 5],
        chunk_count: i % 8 + 1, size_bytes: 4096 + i * 300, text_length: 2048 + i * 100,
        content_type: i === 49 ? 'application/pdf' : 'text/markdown',
        source_modified_at: `2026-09-${String(i % 28 + 1).padStart(2, '0')}T10:00:00Z`,
        ingested_at: stamp, last_ingest_job_id: `ingest-${i + 1}`,
    }));
    const nodes = Array.from({ length: 160 }, (_, i) => i < 50 ? {
        id: `n${i + 1}`, label: `Source ${i + 1} — systemPatterns.md retained evidence`,
        filename: 'systemPatterns.md', type: 'Document', node_type: 'document',
        description: `Synthetic source ${i + 1}. ${HOSTILE}`, mentions: 0,
    } : {
        id: `n${i + 1}`, label: `Entity ${i + 1} — collective memory decision with a long readable label`,
        type: types[(i - 50) % types.length], node_type: 'entity',
        description: `Synthetic evidence for entity ${i + 1}. ${HOSTILE}`, mentions: i % 19 + 1,
    });
    // Ring guarantees two useful outgoing and two incoming paths per node.
    const edges = nodes.flatMap((node, i) => [1, 17].map((distance, j) => ({
        id: `edge-${i * 2 + j + 1}`, from: node.id, to: nodes[(i + distance) % 160].id,
        type: i < 50 ? 'MENTIONS' : j ? 'SUPPORTS' : 'RELATED_TO', weight: 1,
    })));
    const states = ['running', 'queued', 'succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped'];
    const jobs = documents.map((doc, i) => {
        const status = states[i % states.length];
        return {
            job_id: `ingest-${i + 1}`, memory_id: 'synthetic-memory', filename: doc.filename,
            source_path: doc.source_path, sha256: doc.sha256, batch_id: `batch-${i % 3 + 1}`,
            status, current_step: status === 'running' ? 'llm_extract' : status === 'queued' ? 'queued' : status === 'succeeded' ? 'done' : status,
            ...(i === 48 ? {} : { progress_percent: status === 'running' ? 42 : status === 'succeeded' ? 100 : 0 }),
            created_entities: status === 'queued' ? 0 : i + 2, created_relations: status === 'queued' ? 0 : i + 3,
            queue_position: status === 'queued' ? i + 2 : status === 'running' ? 1 : 0,
            created_at: stamp, updated_at: '2026-09-30T12:00:10Z',
            started_at: status === 'queued' ? null : stamp,
            finished_at: ['running', 'queued'].includes(status) ? null : '2026-09-30T12:01:00Z',
            document_id: status === 'succeeded' ? doc.document_id : null,
            guarantee: 'in_memory_best_effort', polling: { recommended: false },
            ...(status === 'failed' ? {
                error: `Synthetic extraction failed. ${HOSTILE}`,
                ontology_diagnostics: { status: 'failed', phase: 'induction', reason: 'invalid_content', message: `Synthetic diagnostic ${HOSTILE}`, counts: { attempted: 3, accepted: 2 } },
            } : {}),
        };
    });
    const graph = {
        status: 'ok', connected: true, reachable: true, binding: 'embedded',
        config: { ontology: 'general', memory_id: 'synthetic-memory' },
        graph_stats: { document_count: 50, entity_count: 217, relation_count: 420,
            entity_types: Object.fromEntries(types.map((type, i) => [type, i === 0 ? 21 : 14])) },
        mid_automation: { compaction_enabled: true, archive_enabled: true },
        mid_archive_projection: { pending: 3, oldest_at: '2026-09-29T10:00:00Z' },
        graph_view: { status: 'ok', nodes, edges, node_count: 160, edge_count: 320,
            total_node_count: 267, total_edge_count: 420, truncated: true },
    };
    const consolidation = (id, status, actor, requestedBy, auto) => ({
        job_id: id, space_id: 'demo', status, agent: actor, requested_by: requestedBy,
        scope: 'agent', scope_label: `Agent ${actor}`, requested_at: stamp, started_at: stamp,
        ...(status === 'queued' ? { queue_position: 2 } : {}),
        ...(['succeeded', 'failed'].includes(status) ? { finished_at: '2026-09-30T12:01:00Z' } : {}),
        progress: { phase: 'processing', notes_total: 50, notes_done: status === 'running' ? 20 : 50, batches_total: 5, batches_done: 2, current_batch: 3, batch_size: 10 },
        message: `Do not wait for completion; use bank_consolidation_status for an explicit check. ${HOSTILE}`,
        polling: { recommended: false },
        ...(auto ? { result: { status: status === 'failed' ? 'partial' : 'ok', notes_processed: 50, notes_total: 50, auto_compaction: auto } } : {}),
    });
    const lanes = [{ space_id: 'demo', lane_state: 'running', queued_count: 1,
        running_job: consolidation('consol-running', 'running', 'qa-agent', 'qa-agent'),
        queued_jobs: [consolidation('consol-queued', 'queued', 'qa-agent', 'different-requester')],
        latest_jobs: [
            consolidation('consol-noop', 'succeeded', 'qa-agent', 'qa-agent', { status: 'not_needed', files_total: 6, files_over_limit: 0, files: [], total_size_before: 12000, total_size_after: 12000 }),
            consolidation('consol-recovery', 'succeeded', 'qa-agent', 'different-requester', { status: 'error', recovery_required: true, message: `Synthetic recovery required. ${HOSTILE}` }),
            consolidation('consol-partial', 'failed', 'qa-agent', 'qa-agent', { status: 'partial', message: 'Synthetic compaction incomplete', files: [] }),
        ], guarantee: 'best_effort_in_process' }];
    return { documents, jobs, graph, lanes };
}

export async function routePortal(page, options = {}) {
    const state = { ...realisticData(), calls: [], unexpected: [], assets: {}, errors: [],
        content: `# Synthetic retained source\n\n${HOSTILE}\n\n${'Readable evidence paragraph.\n'.repeat(40)}`,
        spaces: [{ space_id: 'demo', description: 'Synthetic Portal acceptance space', total_notes_processed: 150, live_notes_count: 50, bank_files_count: 6, last_consolidation: '2026-09-30T12:01:00Z', consolidation_count: 3 }], notes: [],
        permissions: ['read', 'write', 'manage', 'admin'], ...options };
    await page.addInitScript(() => {
        window.__portal634Xss = 0;
        const fetchAtDispatch = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            const url = new URL(input instanceof Request ? input.url : input, location.href);
            if (url.origin === location.origin && url.pathname === '/api/tool') {
                const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
                headers.set('X-Portal-634-Route', location.hash);
                return fetchAtDispatch(input, { ...init, headers });
            }
            return fetchAtDispatch(input, init);
        };
    });
    page.on('pageerror', error => state.errors.push(error.message));
    const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    await page.route('**/*', async route => {
        const url = new URL(route.request().url()), p = url.pathname;
        if (url.origin !== ORIGIN) { state.unexpected.push(url.href); return route.abort(); }
        if (p === '/health') return json(route, { version: '634-synthetic' });
        if (p === '/api/spaces') return json(route, { status: 'ok', spaces: state.spaces });
        if (p === '/api/admin/mesh/availability') return route.fulfill({ status: 404, body: '' });
        if (p === '/api/logout') return json(route, { status: 'ok' });
        if (p === '/api/tool') {
            const req = route.request().postDataJSON(), a = req.arguments;
            const routeAtDispatch = route.request().headers()['x-portal-634-route'] || null;
            req.routeAtDispatch = routeAtDispatch;
            state.calls.push(req);
            if (['space_info', 'graph_status', 'long_document_list', 'long_document_get', 'long_ingest_list', 'long_ingest_status', 'long_ingest_cancel'].includes(req.tool) && a.space_id !== 'demo') {
                state.unexpected.push({ outOfScope: req });
                return json(route, { status: 'error', message: 'Synthetic space access denied' }, 403);
            }
            if (['long_document_list', 'long_ingest_list'].includes(req.tool) && (!Number.isInteger(a.limit) || a.limit < 1 || a.limit > 100 || !Number.isInteger(a.offset) || a.offset < 0)) {
                state.unexpected.push({ invalidBounds: req });
                return json(route, { status: 'error', message: 'Synthetic invalid pagination bounds' });
            }
            const deferred = state.defer?.tool === req.tool ? state.defer : null;
            if (deferred) await deferred.promise;
            if (deferred?.body) return json(route, deferred.body);
            if (state.fail?.tool === req.tool) return json(route, { status: 'error', message: state.fail.message || 'Synthetic service unavailable' });
            switch (req.tool) {
            case 'system_whoami': return json(route, { status: 'ok', client_name: 'qa-agent', auth_type: 'stored', permissions: state.permissions, allowed_spaces: state.spaces.map(space => space.space_id) });
            case 'space_list': return json(route, { status: 'ok', total: state.spaces.length, spaces: state.spaces.map(space => {
                if (a.include_counts !== false) return space;
                const { live_notes_count, bank_files_count, ...metadata } = space;
                return metadata;
            }) });
            case 'space_info': return json(route, { status: 'ok', space_id: 'demo', description: 'Synthetic Portal acceptance space', owner: 'qa-operator', created_at: stamp, hive_status_label: 'local_only',
                live: { notes_count: state.spaces[0]?.live_notes_count ?? 0, total_size: state.spaces[0]?.live_notes_count ? 8000 : 0 },
                bank: { files_count: state.spaces[0]?.bank_files_count ?? 0, total_size: state.spaces[0]?.bank_files_count ? 12000 : 0 },
                last_consolidation: state.spaces[0]?.last_consolidation ?? null, consolidation_queue: state.lanes[0], consolidation_count: state.lanes[0]?.latest_jobs?.length ?? 0 });
            case 'bank_consolidation_queues': {
                const ids = typeof a.space_ids === 'string' && a.space_ids ? a.space_ids.split(',')
                    : ['#/spaces', '#/consolidation'].includes(routeAtDispatch) ? state.spaces.map(space => space.space_id) : [];
                if (!ids.length || ids.some(id => !state.spaces.some(space => space.space_id === id))) {
                    state.unexpected.push({ invalidQueueScope: req });
                    return json(route, { status: 'error', message: 'Synthetic invalid explicit queue scope' });
                }
                const lanes = state.lanes.filter(lane => ids.includes(lane.space_id));
                return json(route, { status: 'ok', total_spaces: ids.length, active_spaces: lanes.filter(lane => lane.lane_state !== 'idle').length,
                    running_spaces: lanes.filter(lane => lane.running_job).length, queued_jobs: lanes.reduce((n, lane) => n + (lane.queued_count || 0), 0),
                    failed_recent: lanes.flatMap(lane => lane.latest_jobs || []).filter(job => job.status === 'failed').length, lanes, ...state.queueEnvelope });
            }
            case 'live_read': return json(route, { status: 'ok', notes: state.notes, total: state.notes.length, has_more: false });
            case 'graph_status': {
                const { graph_view, ...overview } = state.graph;
                return json(route, { ...overview, ...(a.include_graph ? { graph_view } : {}) });
            }
            case 'long_document_list': {
                const docs = state.documents.filter(d => (!a.query || `${d.filename} ${d.source_path}`.includes(a.query)) && (!a.status || d.ingestion_status === a.status));
                const documents = docs.slice(a.offset, a.offset + a.limit);
                return json(route, { status: 'ok', documents, count: documents.length, total_count: docs.length, limit: a.limit, offset: a.offset, ...state.documentEnvelope });
            }
            case 'long_document_get': {
                const document = state.documents.find(d => d.document_id === a.document_id || d.source_path === a.source_path);
                if (!document) return json(route, { status: 'not_found' });
                return json(route, { status: 'ok', document, ...(a.include_content ? document.content_type === 'application/pdf' ? { content_format: 'raw', content_base64: 'U1lOVEhFVElD', content_note: 'Synthetic binary' } : { content: state.content, content_format: 'text' } : {}) });
            }
            case 'long_ingest_list': {
                const rows = state.jobs.filter(j => j.job_id !== state.hiddenJob && (!a.status || j.status === a.status) && (!a.batch_id || j.batch_id === a.batch_id));
                const jobs = rows.slice(a.offset, a.offset + a.limit);
                return json(route, { status: 'ok', jobs, count: jobs.length, total: rows.length, limit: a.limit, offset: a.offset, guarantee: 'in_memory_best_effort', polling: { recommended: false }, ...state.jobEnvelope });
            }
            case 'long_ingest_status': return json(route, state.jobs.find(j => j.job_id === a.job_id) || { status: 'not_found', job_id: a.job_id });
            case 'long_ingest_cancel': return json(route, { status: 'cancelling', job_id: a.job_id, message: 'Cancellation requested at the next phase boundary.' });
            default: state.unexpected.push(req); return json(route, { status: 'error', message: 'Unexpected fixture tool' });
            }
        }
        const rel = p === '/admin.html' || p === '/' ? 'admin.html' : p.startsWith('/static/') ? p.slice(8) : null;
        const file = rel && path.resolve(STATIC, rel);
        if (!file || !file.startsWith(STATIC + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
            state.unexpected.push(p); return route.fulfill({ status: 404, body: '' });
        }
        let body = fs.readFileSync(file);
        if (state.mutationNoOp && rel === 'js/admin/views-dashboard.js') {
            const marker = "return summary.join('') + details;";
            if (!body.toString().includes(marker)) throw new Error('No-op mutation anchor absent');
            const current = fs.readFileSync(path.join(STATIC, 'js/admin-app.js'), 'utf8');
            const start = current.indexOf('function renderAutoCompaction('), end = current.indexOf('// All Portal entry points');
            if (start < 0 || end <= start) throw new Error('Initial renderer anchors absent');
            const rendererSHA256 = crypto.createHash('sha256').update(current.slice(start, end)).digest('hex');
            // Recorded from the initial commit once; works in shallow CI without
            // fetching history and refuses an unreviewed helper replacement.
            if (rendererSHA256 !== 'e8689ce6946dec8c33575bb64a9c496a164e052bd804efa1fa909b2a2872324a') throw new Error('Initial compaction renderer has changed; mutation must be reviewed');
            state.mutation = { kind: `no-op ${state.mutationNoOp}`, rendererSHA256 };
            const changes = {
                'uncollapsed-state': "return summary.join('') + (compaction?.status === 'not_needed' ? renderAutoCompaction(result) : '') + details;",
                'details-open': "return summary.join('') + (compaction?.status === 'not_needed' ? details.replace('<details ', '<details open ') : details);",
            };
            if (state.mutationNoOp === 'unsafe-html') {
                const escaped = 'serverMessage(job.message)';
                if (!body.toString().includes(escaped)) throw new Error('Escaping mutation anchor absent');
                body = Buffer.from(body.toString().replace(escaped, "(job.job_id === 'consol-noop' ? String(job.message) : serverMessage(job.message))"));
            } else {
                if (!changes[state.mutationNoOp]) throw new Error('Unknown no-op mutation');
                body = Buffer.from(body.toString().replace(marker, changes[state.mutationNoOp]));
            }
        }
        if (state.mutationDashboardOrder && rel === 'js/admin/views-dashboard.js') {
            const original = `.sort((a, b) => _consolidationTime(b.last_consolidation) - _consolidationTime(a.last_consolidation)\n                || (a.space_id < b.space_id ? -1 : a.space_id > b.space_id ? 1 : 0))`;
            const replacements = { ascii: '.sort((a, b) => a.space_id < b.space_id ? -1 : a.space_id > b.space_id ? 1 : 0)', reverse: '.reverse()' };
            if (!replacements[state.mutationDashboardOrder] || !body.toString().includes(original)) throw new Error('Ranking mutation anchor absent');
            state.mutation = { kind: `dashboard ranking ${state.mutationDashboardOrder}` };
            body = Buffer.from(body.toString().replace(original, replacements[state.mutationDashboardOrder]));
        }
        state.assets[rel] = crypto.createHash('sha256').update(body).digest('hex');
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
        return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body });
    });
    state.reads = tool => state.calls.filter(req => req.tool === tool);
    return state;
}

export async function configureViewport(page, viewport) {
    if (nativeZoomPages.has(page)) {
        expect(viewport.zoom, 'Native project only qualifies the 200% journeys').toBe(2);
        await page.emulateMedia({ reducedMotion: 'reduce' });
        return; // Playwright sizes the window; Chrome owns zoom/DPR, no CSS zoom.
    }
    await page.setViewportSize({ width: viewport.zoom ? 720 : viewport.width, height: viewport.zoom ? 450 : viewport.height });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    if (viewport.zoom) {
        const cdp = await page.context().newCDPSession(page);
        await cdp.send('Emulation.setDeviceMetricsOverride', { width: 720, height: 450, deviceScaleFactor: 2, mobile: false });
        zoomSessions.set(page, cdp);
    }
}

export async function enterLong(page, panel = 'overview') {
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('#dashRunningJobs')).not.toBeEmpty();
    await page.locator('#sidebar a[href="#/spaces"]').click();
    await page.locator('#content a[href="#/spaces/demo"]').first().click();
    await page.getByRole('tab', { name: 'Long memory', exact: true }).click();
    await expect(page).toHaveURL(/\/long\/overview$/);
    await expect(page.locator('#sdLongBody')).toBeVisible();
    await expect(page.locator('#sdLongStatus')).toContainText('Updated');
    if (panel !== 'overview') {
        const names = { graph: 'Graph', documents: 'Documents', jobs: 'Ingestion jobs' };
        await page.getByRole('tablist', { name: 'Long memory panels' }).getByRole('tab', { name: names[panel], exact: true }).click();
    }
}

export async function assertCompactNoOp(noop) {
    const strict = expect.configure({ timeout: 5000 });
    const summary = noop.locator('.dash-maintenance-summary');
    await strict(summary).toHaveText('Automatic compaction: No oversized files');
    const metrics = await summary.evaluate(el => ({ height: el.getBoundingClientRect().height, lineHeight: Number.parseFloat(getComputedStyle(el).lineHeight) }));
    strict(metrics.height, 'No-op maintenance is at most two short text lines').toBeLessThanOrEqual(metrics.lineHeight * 2 + 1);
    await strict(noop.locator('[data-part="outcome"] > .state')).toHaveCount(0);
    const disclosure = noop.locator('.dash-job-details');
    await strict(disclosure).toHaveJSProperty('open', false);
    await strict(disclosure.locator('.state')).toBeHidden();
    await disclosure.locator('summary').click();
    await strict(disclosure.locator('.state')).toBeVisible();
    await strict(disclosure).toContainText('No oversized files');
    await strict(disclosure).toContainText('bank_consolidation_status');
    await strict(disclosure.locator('img')).toHaveCount(0);
    await disclosure.locator('summary').click();
    await strict(disclosure).toHaveJSProperty('open', false);
}

export async function closeDocumentInspector(page) {
    const dialog = page.getByRole('dialog', { name: 'Document details', exact: true });
    if (await dialog.count()) await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    else await page.getByRole('button', { name: 'Close document inspector', exact: true }).click();
    await expect(page.locator('#sdDocumentInspector')).toHaveCount(0);
}

export async function evidence(page, state, info, name, viewport) {
    if (viewport.zoom) {
        // Playwright screenshots may reset CDP metrics to their configured
        // viewport. Capture through CDP to preserve both reflow and scale.
        // Keep the metrics session alive until the page closes: detaching a
        // second session also clears emulation before the next interaction.
        let cdp = zoomSessions.get(page);
        if (!cdp) { cdp = await page.context().newCDPSession(page); zoomSessions.set(page, cdp); }
        const result = await cdp.send('Page.captureScreenshot', nativeZoomPages.has(page)
            ? { format: 'png', fromSurface: true, captureBeyondViewport: false }
            : { format: 'png', captureBeyondViewport: false, clip: { x: 0, y: 0, width: 720, height: 450, scale: 1 } });
        fs.writeFileSync(info.outputPath(`${name}.png`), Buffer.from(result.data, 'base64'));
    } else await page.screenshot({ path: info.outputPath(`${name}.png`) });
    const measurements = await page.evaluate(() => ({
        width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
        hash: location.hash, focus: document.activeElement?.outerHTML.slice(0, 500),
        documentOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        contentOverflow: document.querySelector('.content').scrollWidth > document.querySelector('.content').clientWidth + 1,
    }));
    const packet = { ...sourceProvenance({ strict: nativeZoomPages.has(page) || process.env.PORTAL_634_QUALIFY === '1' }), capturedAtUTC: new Date().toISOString(),
        viewport, measurements, zoomMethod: nativeZoomPages.has(page) ? 'Chrome Settings native 200%' : viewport.zoom ? 'CDP emulated reflow' : 'ordinary viewport',
        nativeZoom: nativeZoomPages.get(page) || null,
        observations: state.observations || {}, mutation: state.mutation || null, assets: state.assets, calls: state.calls, errors: state.errors, unexpected: state.unexpected,
        qualification: 'Controlled synthetic acceptance; assertions and this exact source/assets qualify only the exercised scenarios.' };
    fs.writeFileSync(info.outputPath(`${name}.json`), JSON.stringify(packet, null, 2));
    await info.attach(name, { path: info.outputPath(`${name}.png`), contentType: 'image/png' });
    await info.attach(`${name}-manifest`, { path: info.outputPath(`${name}.json`), contentType: 'application/json' });
    expect(measurements.width).toBe(viewport.zoom ? 720 : viewport.width);
    expect(measurements.height).toBe(viewport.zoom ? 450 : viewport.height);
    expect(measurements.dpr).toBe(viewport.zoom ? 2 : 1);
    const png = fs.readFileSync(info.outputPath(`${name}.png`));
    expect(png.readUInt32BE(16)).toBe(viewport.width);
    expect(png.readUInt32BE(20)).toBe(viewport.height);
    expect(state.unexpected, 'No request escapes the controlled real-bundle harness').toEqual([]);
    expect(state.errors, 'No browser runtime error').toEqual([]);
    expect(await page.evaluate(() => window.__portal634Xss)).toBe(0);
}

export async function expectReachableDetail(page, selector) {
    const visible = await page.locator(selector).evaluate(el => {
        const r = el.getBoundingClientRect();
        return { top: r.top, left: r.left, right: r.right, width: r.width, height: r.height, focus: (el.closest('[role="dialog"]') || el).contains(document.activeElement) };
    });
    expect(visible.top, 'Selection must reveal detail without test-driven scrolling').toBeGreaterThanOrEqual(0);
    expect(visible.top, 'Detail heading must be inside the viewport after selection').toBeLessThan(await page.evaluate(() => innerHeight - 44));
    expect(visible.width).toBeGreaterThan(180);
    expect(visible.right).toBeLessThanOrEqual(await page.evaluate(() => innerWidth + 1));
    expect(visible.focus, 'Keyboard focus must transfer to the selected detail').toBe(true);
}
