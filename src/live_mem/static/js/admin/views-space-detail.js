/**
 * Space Detail — P8-3 (#141).
 *
 * Field-mapped renderers only: no raw-object fallback, no invented values.
 * Entry loads space_info first, then preloads the detail surfaces once without
 * polling. Every awaited continuation is epoch guarded.
 */
(function () {
    'use strict';

    const CATEGORIES = ['', 'observation', 'decision', 'todo', 'insight', 'question', 'progress', 'issue'];
    const SENTINEL_STATUSES = new Set(['read_only', 'rate_limited', 'truncated']);
    const RULES_LIMIT = 50000;
    const JOB_GUARANTEE_TOOLTIP = 'Job state lives in server memory: it does not survive a restart and history is trimmed.';
    let currentView = null;

    function safe(value) {
        return esc(String(value ?? ''));
    }

    function guaranteeBadge(value) {
        return `<span class="pill pill-neutral" title="${safe(JOB_GUARANTEE_TOOLTIP)}">${safe(value || 'not recorded')}</span>`;
    }

    function hasPermission(view, permission) {
        const permissions = Array.isArray(view.ctx.identity.permissions) ? view.ctx.identity.permissions : [];
        const hierarchy = ['read', 'write', 'manage', 'admin'];
        const required = hierarchy.indexOf(permission);
        if (required < 0) return false;
        return permissions.some(candidate => hierarchy.indexOf(candidate) >= required);
    }

    function guarded(view, epochAtCall) {
        return currentView === view && epochAtCall === AdminRouter.epoch;
    }

    function unavailableOrError(result, retryAction) {
        if (result && SENTINEL_STATUSES.has(result.status)) return stateUnavailable(result.message);
        return stateError({
            title: "Couldn't load this data",
            message: result && result.message ? result.message : '',
            retryAction,
        });
    }

    function keyValue(label, value, options = {}) {
        let rendered = '<span class="text-faint">not recorded</span>';
        if (value !== null && value !== undefined && value !== '') {
            rendered = options.timestamp ? renderTimestamp(value) : `<span class="mono-data">${safe(value)}</span>`;
        }
        return `<div class="sd-kv"><span class="micro-label">${safe(label)}</span>${rendered}</div>`;
    }

    function failClosedBanner(title, copy) {
        return `<div class="sd-banner sd-banner--error" role="alert">${icon('alert')}<div><strong>${safe(title)}</strong><p>${safe(copy)}</p></div></div>`;
    }

    function attentionBanner(title, copy) {
        return `<div class="sd-banner sd-banner--warn">${icon('alert')}<div><strong>${safe(title)}</strong><p>${safe(copy)}</p></div></div>`;
    }

    function hiveStatus(rawValue) {
        const raw = String(rawValue ?? '');
        switch (raw) {
        case 'not_a_space':
            return { raw, label: 'Not a space', marker: '<span class="sd-status-plain">Not a space</span>', banner: '' };
        case 'local_only':
            return { raw, label: 'Local only', marker: statusDot('neutral', 'Local only'), helper: 'not participating in Project Mesh', banner: '' };
        case 'hivemind_healthy':
            return { raw, label: 'Mesh healthy', marker: statusDot('ok', 'Mesh healthy'), banner: '' };
        case 'hivemind_blocked':
            return { raw, label: 'Mesh blocked', marker: statusDot('warn', 'Mesh blocked'), helper: 'mesh participation blocked — attention required', banner: '' };
        case 'unsafe':
            return {
                raw,
                label: 'UNSAFE — fail-closed',
                marker: pill('error', 'UNSAFE — fail-closed'),
                banner: failClosedBanner('UNSAFE — fail-closed', 'This space is fail-closed. Treat local state as unsafe until a clean resync completes. Do not restore backups over it.'),
            };
        case 'resync_required':
            return {
                raw,
                label: 'RESYNC REQUIRED — fail-closed',
                marker: pill('error', 'RESYNC REQUIRED — fail-closed'),
                banner: failClosedBanner('RESYNC REQUIRED — fail-closed', 'Corrupted or diverged critical state detected. This space is unsafe until a clean resync completes.'),
            };
        default:
            return {
                raw,
                label: raw || 'Unknown mesh state',
                marker: pill('error', raw || 'Unknown mesh state'),
                banner: failClosedBanner('Unknown mesh state — fail-closed', 'The server returned an unrecognized mesh state. Treat this space as unsafe until its critical state is verified.'),
            };
        }
    }

    function renderHeader(view) {
        const info = view.info;
        const status = hiveStatus(info.hive_status_label);
        const helper = status.helper ? `<p class="form-hint">${safe(status.helper)}</p>` : '';
        return `${status.banner}
            <div class="sd-summary">
                <div class="sd-summary__identity">
                    <span class="micro-label">SPACE ID</span>
                    ${copyable(info.space_id || view.spaceId)}
                    <p>${info.description ? safe(info.description) : '<span class="text-faint">No description</span>'}</p>
                </div>
                <div class="sd-summary__status">
                    ${status.marker}
                    <span class="mono-data">${safe(status.raw || 'missing')}</span>
                    ${helper}
                    ${typeof meshIsAvailable === 'function' && meshIsAvailable()
                        ? `<a class="sd-link" href="#/mesh/${encodeURIComponent(view.spaceId)}">View in Mesh</a>`
                        : ''}
                </div>
            </div>
            <div class="metric-grid sd-metrics">
                <div class="metric-card"><span class="micro-label">SHORT NOTES</span><div class="metric-value">${safe(info.live && info.live.notes_count)}</div><span class="body-small">${safe(fmtSize(info.live && info.live.total_size))}</span></div>
                <div class="metric-card"><span class="micro-label">MID FILES</span><div class="metric-value">${safe(info.bank && info.bank.files_count)}</div><span class="body-small">${safe(fmtSize(info.bank && info.bank.total_size))}</span></div>
                <div class="metric-card"><span class="micro-label">CONSOLIDATIONS</span><div class="metric-value">${safe(info.consolidation_count)}</div><span class="body-small">${info.last_consolidation ? renderTimestamp(info.last_consolidation) : 'not recorded'}</span></div>
                <div class="metric-card"><span class="micro-label">SYNTHESIS</span><div class="metric-value sd-metric-word">${info.synthesis_exists ? 'Ready' : 'Absent'}</div><span class="body-small">Owner: ${safe(info.owner || 'not recorded')}</span></div>
            </div>
            <div class="sd-meta-row">
                ${keyValue('created', info.created_at, { timestamp: true })}
                ${keyValue('owner', info.owner)}
            </div>`;
    }

    function laneSeverity(value) {
        if (value === 'running' || value === 'queued' || value === 'consolidating') return 'warn';
        if (value === 'failed' || value === 'error') return 'error';
        if (value === 'succeeded' || value === 'ok') return 'ok';
        return 'neutral';
    }

    function renderLane(view) {
        const queue = view.info.consolidation_queue || {};
        const latest = Array.isArray(queue.latest_jobs) ? queue.latest_jobs.slice(0, 10) : [];
        const queued = Array.isArray(queue.queued_job_ids) ? queue.queued_job_ids : [];
        const running = queue.running_job || null;
        const activity = !running && !latest.length && (queue.lane_state || 'idle') === 'idle'
            ? stateEmpty({ title: 'No consolidation activity', hint: 'This lane is idle.' })
            : `<div class="sd-lane-grid">
                ${keyValue('running job', running && (running.job_id || running))}
                ${keyValue('queued', queue.queued_count ?? queued.length)}
                ${keyValue('batch size', queue.service_config && queue.service_config.batch_size)}
                ${keyValue('guarantee', queue.guarantee)}
            </div>`;
        return `<div class="panel sd-section">
            <div class="panel-header"><div><h2>Consolidation lane</h2><div class="sd-inline">${statusDot(laneSeverity(queue.lane_state), queue.lane_state || 'idle')} ${guaranteeBadge(queue.guarantee)}</div></div><a class="sd-link" href="#/consolidation">Open consolidation</a></div>
            ${activity}
        </div>`;
    }

    function tierButtons(view) {
        return `<div class="sd-tier-tabs" role="tablist" aria-label="Memory tier">
            ${['short', 'mid', 'long'].map(tier => `<button type="button" class="sd-tier-tab${view.tier === tier ? ' active' : ''}" role="tab" aria-selected="${view.tier === tier ? 'true' : 'false'}" data-action="sd-select-tier" data-tier="${tier}">${safe(tier)}</button>`).join('')}
        </div>`;
    }

    function renderTier(view) {
        const target = document.getElementById('sdTierPanel');
        if (!target || currentView !== view) return;
        if (view.tier === 'short') target.innerHTML = renderShort(view);
        else if (view.tier === 'mid') target.innerHTML = renderMid(view);
        else {
            target.innerHTML = renderLong(view);
            mountLongGraph(view);
        }
    }

    function renderShort(view) {
        const data = view.shortData;
        const categoryOptions = CATEGORIES.map(value => `<option value="${safe(value)}"${view.shortFilters.category === value ? ' selected' : ''}>${safe(value || 'All categories')}</option>`).join('');
        let body = stateError({ title: "Couldn't load recent notes", retryAction: 'sd-retry-short' });
        if (view.shortLoading) body = stateLoading('Loading recent notes…');
        else if (data) body = renderShortData(view, data);
        return `<div class="item-card tier-short sd-tier-card">
            <div class="panel-header"><div><span class="micro-label">SHORT</span><h2>Live notes</h2></div><div class="sd-tier-actions">${data ? `<span class="count-pill">Showing ${safe(Array.isArray(data.notes) ? data.notes.length : 0)} notes</span>` : ''}${renderConsolidateAction(view)}</div></div>
            <div class="sd-filter-grid">
                <div><label class="form-label" for="sdShortLimit">Limit</label><input id="sdShortLimit" class="form-input mono" type="number" min="1" max="500" value="${safe(view.shortFilters.limit)}"></div>
                <div><label class="form-label" for="sdShortCategory">Category</label><select id="sdShortCategory" class="form-input">${categoryOptions}</select></div>
                <div><label class="form-label" for="sdShortAgent">Agent</label><input id="sdShortAgent" class="form-input mono" value="${safe(view.shortFilters.agent)}"></div>
                <div><label class="form-label" for="sdShortSince">Since</label><input id="sdShortSince" class="form-input mono" type="datetime-local" value="${safe(view.shortFilters.since)}"></div>
                <button type="button" class="btn btn-secondary sd-filter-submit" data-action="sd-apply-short-filters"${view.shortLoading ? ' disabled' : ''}>Apply filters</button>
            </div>
            <div id="sdShortBody">${body}</div>
        </div>`;
    }

    function renderShortData(view, data) {
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-short');
        const notes = Array.isArray(data.notes) ? data.notes : [];
        if (!notes.length) return stateEmpty({ title: 'No notes match these filters' });
        const rows = notes.map((note, index) => {
            const tags = Array.isArray(note.tags) && note.tags.length ? note.tags.map(tag => `<span class="chip">${safe(tag)}</span>`).join('') : '';
            const provenance = note.provenance && typeof note.provenance === 'object'
                ? `<span class="body-small">${safe(note.provenance.label || note.provenance.origin_agent || note.provenance.origin_node_id || '')}</span>` : '';
            return `<button type="button" class="sd-note" data-action="sd-preview-note" data-note-index="${index}">
                <span class="sd-note__top"><span>${pill('neutral', note.category || 'uncategorized')} <span class="mono-data">${safe(note.filename || note.note_id || '')}</span></span>${renderTimestamp(note.timestamp)}</span>
                <span class="body-small">${safe(note.agent || '')}</span><span class="sd-chip-row">${tags}${provenance}</span>
            </button>`;
        }).join('');
        return `<div class="sd-split"><div class="sd-list">${rows}<p class="form-hint">Showing ${safe(notes.length)} notes${data.has_more ? ' · more notes are available' : ''}</p></div><div id="sdNotePreview">${stateEmpty({ title: 'Select a note', hint: 'Content is previewed as plain text.' })}</div></div>`;
    }

    function renderMid(view) {
        const data = view.midData;
        let body = stateError({ title: "Couldn't load Memory Bank files", retryAction: 'sd-retry-mid' });
        if (view.midLoading) body = stateLoading('Loading bank files…');
        else if (data) body = renderMidData(view, data);
        return `<div class="item-card tier-mid sd-tier-card">
            <div class="panel-header"><div><span class="micro-label">MID</span><h2>Memory Bank</h2></div><div class="sd-tier-actions">${data && data.status === 'ok' ? `<span class="count-pill">${safe(data.file_count)} files</span>` : ''}${renderGraphPushAction(view)}</div></div>
            <div id="sdMidBody">${body}</div>
        </div>`;
    }

    function renderMidData(view, data) {
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-mid');
        const files = Array.isArray(data.files) ? data.files : [];
        if (!files.length) return stateEmpty({ title: 'No bank files' });
        const rows = files.map((file, index) => `<tr class="sd-file-row${view.midSelectedIndex === index ? ' selected' : ''}" data-action="sd-read-bank" data-file-index="${index}" tabindex="0" role="button" aria-label="Read ${safe(file.filename)}">
            <td><span class="mono-data">${safe(file.filename)}</span></td><td>${safe(fmtSize(file.size))}</td><td>${renderTimestamp(file.last_modified)}</td>
        </tr>`).join('');
        const preview = view.midPreviewHtml || stateLoading('Loading first file…');
        return `<div class="sd-split sd-mid-reader"><div>${dataTable(['File', 'Size', 'Last modified'], rows)}</div><article id="sdBankPreview" class="sd-reader">${preview}</article></div>`;
    }

    function graphStatsSection(graphStats) {
        if (!graphStats) return stateUnavailable('Long statistics are unavailable.');
        return `<div class="metric-grid sd-long-metrics">
            <div class="metric-card"><span class="micro-label">DOCUMENTS</span><div class="metric-value">${safe(graphStats.document_count)}</div></div>
            <div class="metric-card"><span class="micro-label">ENTITIES</span><div class="metric-value">${safe(graphStats.entity_count)}</div></div>
            <div class="metric-card"><span class="micro-label">RELATIONS</span><div class="metric-value">${safe(graphStats.relation_count)}</div></div>
        </div>`;
    }

    function renderGraphViewer(graph) {
        if (!graph) return stateLoading('Loading graph…');
        if (graph.status !== 'ok') return stateUnavailable(graph.message || 'Graph data is unavailable.');
        const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
        if (!nodes.length) return stateEmpty({ title: 'No graph data yet', hint: 'Push Mid to Long to create the first projection.' });
        const shown = `${safe(graph.node_count)} nodes · ${safe(graph.edge_count)} relations`;
        const truncated = graph.truncated
            ? `<span class="form-hint">Showing a safe preview of ${safe(graph.total_node_count)} nodes and ${safe(graph.total_edge_count)} relations.</span>`
            : '';
        return `<section class="sd-graph" aria-labelledby="sdGraphTitle">
            <div class="sd-graph-toolbar"><div><h3 id="sdGraphTitle">Graph explorer</h3><span class="count-pill">${shown}</span></div><div class="sd-graph-controls">
                <label class="sr-only" for="sdGraphSearch">Find a node</label><input id="sdGraphSearch" class="form-input" type="search" placeholder="Find a node…">
                <button type="button" class="btn btn-secondary btn-sm" id="sdGraphFit">Fit graph</button>
            </div></div>
            ${truncated}
            <div class="sd-graph-stage"><svg id="sdGraphCanvas" viewBox="0 0 960 520" role="img" aria-label="Knowledge graph"><g id="sdGraphViewport"></g></svg><aside id="sdGraphDetails" class="sd-graph-details" aria-live="polite"><span class="micro-label">NODE DETAILS</span><p>Select a node to inspect it.</p></aside></div>
        </section>`;
    }

    function mountLongGraph(view) {
        const graph = view.longData && view.longData.graph_view;
        const svg = document.getElementById('sdGraphCanvas');
        const viewport = document.getElementById('sdGraphViewport');
        if (!svg || !viewport || !graph || graph.status !== 'ok') return;
        const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
        const edges = Array.isArray(graph.edges) ? graph.edges : [];
        const svgNs = 'http://www.w3.org/2000/svg';
        const positions = new Map();
        const documentNodes = nodes.filter(node => node.node_type === 'document');
        const entityNodes = nodes.filter(node => node.node_type !== 'document');

        documentNodes.forEach((node, index) => {
            positions.set(node.id, { x: 76 + (index % 3) * 135, y: 58 + Math.floor(index / 3) * 58 });
        });
        entityNodes.forEach((node, index) => {
            const angle = index * 2.399963229728653;
            const radius = 34 + Math.sqrt(index + 1) * 26;
            positions.set(node.id, { x: 610 + Math.cos(angle) * radius, y: 260 + Math.sin(angle) * Math.min(radius, 220) });
        });

        edges.forEach(edge => {
            const from = positions.get(edge.from);
            const to = positions.get(edge.to);
            if (!from || !to) return;
            const line = document.createElementNS(svgNs, 'line');
            line.setAttribute('x1', String(from.x));
            line.setAttribute('y1', String(from.y));
            line.setAttribute('x2', String(to.x));
            line.setAttribute('y2', String(to.y));
            line.setAttribute('class', `sd-graph-edge${edge.type === 'MENTIONS' ? ' is-mention' : ''}`);
            const title = document.createElementNS(svgNs, 'title');
            title.textContent = edge.type || 'relation';
            line.appendChild(title);
            viewport.appendChild(line);
        });

        function showDetails(node) {
            const details = document.getElementById('sdGraphDetails');
            if (!details) return;
            details.textContent = '';
            const marker = document.createElement('span');
            marker.className = 'micro-label';
            marker.textContent = node.node_type === 'document' ? 'DOCUMENT' : String(node.type || 'ENTITY').toUpperCase();
            const title = document.createElement('h4');
            title.textContent = node.filename || node.label || 'Untitled';
            const description = document.createElement('p');
            description.textContent = node.description || (node.node_type === 'document' ? 'Source document in this graph.' : 'No description available.');
            const mentions = document.createElement('span');
            mentions.className = 'count-pill';
            mentions.textContent = `${Number(node.mentions) || 0} mentions`;
            details.append(marker, title, description, mentions);
        }

        nodes.forEach((node, index) => {
            const point = positions.get(node.id);
            if (!point) return;
            const group = document.createElementNS(svgNs, 'g');
            group.setAttribute('class', `sd-graph-node is-${node.node_type === 'document' ? 'document' : 'entity'}`);
            group.setAttribute('transform', `translate(${point.x} ${point.y})`);
            group.setAttribute('tabindex', '0');
            group.setAttribute('role', 'button');
            group.setAttribute('aria-label', `Inspect ${node.label || 'node'}`);
            group.dataset.search = `${node.label || ''} ${node.type || ''}`.toLowerCase();
            if (node.node_type === 'document') {
                const shape = document.createElementNS(svgNs, 'rect');
                shape.setAttribute('x', '-9'); shape.setAttribute('y', '-11');
                shape.setAttribute('width', '18'); shape.setAttribute('height', '22');
                shape.setAttribute('rx', '3');
                group.appendChild(shape);
            } else {
                const shape = document.createElementNS(svgNs, 'circle');
                shape.setAttribute('r', String(6 + Math.min(7, Math.log2((Number(node.mentions) || 0) + 1))));
                group.appendChild(shape);
            }
            const label = document.createElementNS(svgNs, 'text');
            label.setAttribute('x', '13');
            label.setAttribute('y', '4');
            label.setAttribute('class', `sd-graph-label${node.node_type === 'document' || index < 28 ? ' is-visible' : ''}`);
            label.textContent = String(node.label || '').slice(0, 34);
            group.appendChild(label);
            group.addEventListener('click', () => showDetails(node));
            group.addEventListener('keydown', event => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                showDetails(node);
            });
            viewport.appendChild(group);
        });

        let transform = { x: 0, y: 0, scale: 1 };
        function applyTransform() {
            viewport.setAttribute('transform', `translate(${transform.x} ${transform.y}) scale(${transform.scale})`);
        }
        const fit = document.getElementById('sdGraphFit');
        if (fit) fit.addEventListener('click', () => { transform = { x: 0, y: 0, scale: 1 }; applyTransform(); });
        svg.addEventListener('wheel', event => {
            event.preventDefault();
            transform.scale = Math.max(0.55, Math.min(2.4, transform.scale * (event.deltaY < 0 ? 1.1 : 0.9)));
            applyTransform();
        }, { passive: false });
        let drag = null;
        svg.addEventListener('pointerdown', event => {
            if (event.target.closest?.('.sd-graph-node')) return;
            drag = { x: event.clientX, y: event.clientY, ox: transform.x, oy: transform.y };
            svg.setPointerCapture(event.pointerId);
        });
        svg.addEventListener('pointermove', event => {
            if (!drag) return;
            transform.x = drag.ox + event.clientX - drag.x;
            transform.y = drag.oy + event.clientY - drag.y;
            applyTransform();
        });
        svg.addEventListener('pointerup', () => { drag = null; });

        const search = document.getElementById('sdGraphSearch');
        if (search) search.addEventListener('input', () => {
            const query = search.value.trim().toLowerCase();
            viewport.querySelectorAll('.sd-graph-node').forEach(group => {
                const match = !query || group.dataset.search.includes(query);
                group.classList.toggle('is-dimmed', !match);
                group.classList.toggle('is-match', Boolean(query && match));
            });
        });
    }

    function renderLong(view) {
        const data = view.longData;
        let body = stateError({ title: "Couldn't load long status", retryAction: 'sd-retry-long' });
        if (view.longLoading) body = stateLoading('Checking the embedded long runtime…');
        else if (data) body = renderLongData(view, data);
        return `<div class="item-card tier-long sd-tier-card">
            <div class="panel-header"><div><span class="micro-label">LONG</span><h2>Knowledge graph</h2></div></div>
            <div id="sdLongBody">${body}</div>
        </div>`;
    }

    function renderLongData(view, data) {
        if (data.status === 'not_found') return stateEmpty({ title: 'Space not found' });
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-long');

        if (data.connected === false) {
            const unbound = data.embedded === true || data.bound === false;
            return `${unbound
                ? attentionBanner('Not yet projected — auto-binds on first long push', 'The embedded long runtime is required; this space has not yet been projected.')
                : failClosedBanner('Long runtime unavailable', 'The required embedded long runtime is not configured for this space.')}
                ${data.message ? serverMessage(data.message) : ''}
                ${renderLongActions(view)}`;
        }

        if (data.reachable === false) {
            const outage = data.binding === 'embedded'
                ? ['Embedded long runtime unreachable', 'Embedded long runtime unreachable — this deployment is out of contract (ADR-0019).']
                : data.binding === 'explicit'
                    ? ['Explicit long runtime unreachable', 'The explicitly configured Graph Memory runtime cannot be reached. Check its URL and credentials.']
                    : ['Long runtime unreachable — binding unknown', 'The bound runtime is unreachable and its binding classification is unknown. Treat this state as unsafe until verified.'];
            return `${failClosedBanner(outage[0], outage[1])}
                ${data.error ? serverMessage(data.error) : ''}
                ${renderBinding(data)}${renderWatermark(data.watermark)}${renderLongActions(view)}`;
        }

        return `<div class="sd-long-stack">
            ${graphStatsSection(data.graph_stats)}
            ${renderGraphViewer(data.graph_view)}
            ${renderWatermark(data.watermark)}
            <div class="sd-meta-row">${keyValue('last push', data.last_push, { timestamp: true })}${keyValue('push count', data.push_count)}${keyValue('files pushed', data.files_pushed)}</div>
            ${renderLongActions(view)}
        </div>`;
    }

    function renderBinding(data) {
        if (data.binding === 'embedded') {
            return `<div class="sd-binding">${pill('neutral', 'Embedded long runtime (managed by Hivemind)')}</div>`;
        }
        if (data.binding === 'explicit') {
            const config = data.config || {};
            return `<div class="sd-binding sd-meta-row">${keyValue('URL', config.url)}${keyValue('memory id', config.memory_id)}${keyValue('ontology', config.ontology)}</div>`;
        }
        return failClosedBanner('Unknown long binding — fail-closed', 'The bound runtime did not report a recognized binding classification.');
    }

    function renderWatermark(watermark) {
        if (!watermark) return '';
        const flagged = watermark.flagged === true
            ? attentionBanner('High-water mark preserved', 'Push observed a bank_version regression (possible rollback/split-brain) — high-water mark preserved.') : '';
        return `<section class="sd-watermark"><h3>Derived watermark</h3>${flagged}<div class="sd-meta-row">
            ${keyValue('bank version', watermark.bank_version)}${keyValue('commit id', watermark.commit_id)}${keyValue('term', watermark.term)}${keyValue('provenance', watermark.provenance)}${keyValue('recorded', watermark.recorded_at, { timestamp: true })}${keyValue('flagged', watermark.flagged === true ? 'yes' : 'no')}
        </div><p class="form-hint">This watermark reports projection history only; it never decides mesh state.</p></section>`;
    }

    function renderLongActions(view) {
        if (!hasPermission(view, 'write')) return '';
        return `<div class="sd-actions-row">${renderGraphPushAction(view)}</div>`;
    }

    function renderConsolidateAction(view) {
        const disabled = view.consolidating || !hasPermission(view, 'write');
        const title = hasPermission(view, 'write') ? '' : ' title="Write permission is required"';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-confirm-consolidate"${disabled ? ' disabled' : ''}${title}>${icon('consolidation')}Consolidate</button>`;
    }

    function renderGraphPushAction(view) {
        const disabled = view.longMutating || !hasPermission(view, 'write');
        const title = hasPermission(view, 'write') ? '' : ' title="Write permission is required"';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-confirm-graph-push"${disabled ? ' disabled' : ''}${title}>${icon('push')}Push mid → long</button>`;
    }

    function renderRules(view) {
        if (!view.rulesData) {
            return `<div id="sdRulesBody">${view.rulesLoading ? stateLoading('Loading rules…') : stateError({ title: "Couldn't load rules", retryAction: 'sd-retry-rules' })}</div>`;
        }
        const data = view.rulesData;
        if (data.status === 'not_found') return stateEmpty({ title: 'No rules file for this space' });
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-rules');
        const rules = String(data.rules ?? '');
        const edit = hasPermission(view, 'manage')
            ? `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-edit-rules">Edit rules</button>`
            : '';
        return `<div class="sd-rules-toolbar">${edit}</div><article class="markdown-body">${renderMarkdown(rules)}</article>`;
    }

    function renderAccess(view) {
        if (!hasPermission(view, 'admin')) return stateUnavailable('Requires admin permission. No token query was made.');
        if (!view.accessData) return view.accessLoading ? stateLoading('Loading token metadata…') : stateError({ title: "Couldn't load access summary", retryAction: 'sd-retry-access' });
        const data = view.accessData;
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-access');
        const tokens = (Array.isArray(data.tokens) ? data.tokens : []).filter(token => token.name !== 'internal-long');
        const explicit = tokens.filter(token => Array.isArray(token.space_ids) && token.space_ids.includes(view.spaceId));
        const admins = tokens.filter(token => Array.isArray(token.permissions) && token.permissions.includes('admin'));
        function rows(group) {
            return group.map(token => `<tr class="${token.revoked ? 'row-muted' : ''}"><td>${safe(token.name)}</td><td>${(Array.isArray(token.permissions) ? token.permissions : []).map(p => `<span class="chip">${safe(p)}</span>`).join('')}</td><td>${token.revoked ? pill('neutral', 'revoked') : pill('ok', 'active')}</td></tr>`).join('');
        }
        return `<p class="body-small">Token grants listed below target this space directly; status shows whether each token is active. Admin grants are global.</p>
            <div class="sd-access-counts"><span class="count-pill">${safe(tokens.length)} tokens total</span><span class="count-pill">${safe(explicit.length)} explicit</span><span class="count-pill">${safe(admins.length)} admins</span></div>
            <h3>Explicit space access</h3>${explicit.length ? dataTable(['Client', 'Permissions', 'State'], rows(explicit)) : stateEmpty({ title: 'No explicit token access' })}
            <h3>Admin access</h3>${admins.length ? dataTable(['Client', 'Permissions', 'State'], rows(admins)) : stateEmpty({ title: 'No admin tokens' })}
            <a class="sd-link" href="#/access">Manage access</a>`;
    }

    function activityJobIds(view) {
        const queue = view.info.consolidation_queue || {};
        const ids = new Set();
        const add = job => {
            const jobId = typeof job === 'string' ? job : job && job.job_id;
            if (jobId) ids.add(String(jobId));
        };
        add(queue.running_job);
        (Array.isArray(queue.queued_job_ids) ? queue.queued_job_ids : []).forEach(add);
        (Array.isArray(queue.queued_jobs) ? queue.queued_jobs : []).forEach(add);
        (Array.isArray(queue.latest_jobs) ? queue.latest_jobs : []).forEach(add);
        return ids;
    }

    function renderJobProgress(job) {
        const progress = job.progress && typeof job.progress === 'object' ? job.progress : {};
        const position = Number(job.queue_position);
        const positionHtml = position === 1
            ? keyValue('queue position', 'running')
            : position >= 2 ? keyValue('queue position', `position ${position} in queue`) : '';
        return `<div class="sd-meta-row">
            ${keyValue('phase', progress.phase)}
            ${keyValue('notes', progress.notes_total === null || progress.notes_total === undefined ? null : `${progress.notes_done ?? 0} / ${progress.notes_total}`)}
            ${keyValue('batches', progress.batches_total === null || progress.batches_total === undefined ? null : `${progress.batches_done ?? 0} / ${progress.batches_total}`)}
            ${keyValue('current batch', progress.current_batch)}
            ${keyValue('batch size', progress.batch_size)}
            ${positionHtml}
        </div>`;
    }

    function renderJobResult(job) {
        const result = job.result && typeof job.result === 'object' ? job.result : null;
        if (!result) return '';
        if (result.notes_processed === 0) {
            return `<div class="sd-job-result"><h4>Nothing to consolidate</h4>${keyValue('notes processed', 0)}${result.message ? serverMessage(result.message) : ''}</div>`;
        }
        const metrics = [
            ['notes processed', result.notes_processed],
            ['bank files updated', result.bank_files_updated],
            ['bank files created', result.bank_files_created],
            ['bank files unchanged', result.bank_files_unchanged],
            ['operations applied', result.operations_applied],
            ['operations failed', result.operations_failed],
            ['synthesis size', result.synthesis_size],
            ['LLM tokens used', result.llm_tokens_used],
            ['LLM prompt tokens', result.llm_prompt_tokens],
            ['LLM completion tokens', result.llm_completion_tokens],
            ['batches completed', result.batches_completed],
            ['batches total', result.batches_total],
            ['batch size', result.batch_size],
            ['duration seconds', result.duration_seconds],
        ];
        const partial = job.status === 'succeeded'
            && Number.isFinite(Number(result.batches_completed))
            && Number.isFinite(Number(result.batches_total))
            && Number(result.batches_completed) < Number(result.batches_total)
            ? attentionBanner('Partial completion', 'The job succeeded before every planned batch completed. Review the reported metrics.') : '';
        return `<div class="sd-job-result"><h4>Result</h4>${partial}<div class="sd-meta-row">${metrics.map(([label, value]) => keyValue(label, value)).join('')}</div></div>`;
    }

    function renderJobInspector(view) {
        if (view.jobLoading) return stateLoading('Loading consolidation job…');
        const job = view.jobData;
        if (!job) return stateEmpty({ title: 'Select a job', hint: 'Job history is in-memory and limited to the latest 10 jobs for this space.' });
        if (job.status === 'not_found') {
            return stateEmpty({ title: 'Job unknown', hint: 'The server restarted or trimmed its history (100-job cap). Job history is in-memory and best-effort.' });
        }
        if (!['queued', 'running', 'succeeded', 'failed'].includes(job.status)) {
            return stateError({ title: 'Unknown consolidation job state', message: job.message || '' });
        }
        const failure = job.status === 'failed'
            ? stateError({ title: 'Consolidation failed', message: job.error || '' }) : '';
        const message = job.message ? serverMessage(job.message) : '';
        return `<div class="item-card sd-job-inspector">
            <div class="panel-header"><div><span class="micro-label">JOB INSPECTOR</span><h3>${safe(job.scope_label || 'Consolidation job')}</h3></div><div class="sd-inline">${statusDot(laneSeverity(job.status), job.status)} ${guaranteeBadge(job.guarantee)}</div></div>
            <div class="sd-meta-row">
                ${keyValue('job id', job.job_id)}
                ${keyValue('space', job.space_id)}
                ${keyValue('scope', job.scope)}
                ${keyValue('agent', job.agent)}
                ${keyValue('requested by', job.requested_by)}
                ${keyValue('requested', job.requested_at, { timestamp: true })}
                ${keyValue('queued', job.queued_at, { timestamp: true })}
                ${keyValue('started', job.started_at, { timestamp: true })}
                ${keyValue('finished', job.finished_at, { timestamp: true })}
            </div>
            ${renderJobProgress(job)}${message}${failure}${job.status === 'succeeded' ? renderJobResult(job) : ''}
            <button type="button" class="btn btn-secondary" data-action="sd-load-job" data-job-id="${safe(view.jobId)}">Refresh job</button>
        </div>`;
    }

    function renderActivity(view) {
        const queue = view.info.consolidation_queue || {};
        const jobs = Array.isArray(queue.latest_jobs) ? queue.latest_jobs.slice(0, 10) : [];
        if (!jobs.length) return stateEmpty({ title: 'No recorded activity', hint: 'In-memory history, since restart.' });
        const rows = jobs.map(job => `<tr><td>${statusDot(laneSeverity(job.status), job.status || 'unknown')}</td><td>${safe(job.scope_label)}</td><td>${renderTimestamp(job.finished_at)}</td><td>${copyable(job.job_id)}</td><td><button type="button" class="btn btn-ghost btn-sm" data-action="sd-load-job" data-job-id="${safe(job.job_id)}" aria-label="Inspect job ${safe(job.job_id)}">Inspect</button></td></tr>`).join('');
        return `${dataTable(['Status', 'Scope', 'Finished', 'Job id', ''], rows)}<p class="form-hint">Last 10 jobs for this space, since restart. Select a job for an explicit manual status check.</p><div id="sdJobInspector">${renderJobInspector(view)}</div>`;
    }

    function renderBackups(view) {
        if (!view.backupsData) return view.backupsLoading ? stateLoading('Loading backups…') : stateError({ title: "Couldn't load backups", retryAction: 'sd-retry-backups' });
        const data = view.backupsData;
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-backups');
        const backups = Array.isArray(data.backups) ? data.backups : [];
        if (!backups.length) return stateEmpty({ title: 'No backups for this space' });
        const rows = backups.map((backup, index) => `<tr><td class="mono-data">${safe(backup.backup_id)}</td><td>${renderTimestamp(backup.timestamp)}</td><td>${safe(backup.description || '')}</td><td>${safe(fmtSize(backup.total_size))}</td><td class="actions">${hasPermission(view, 'manage') ? `<button type="button" class="btn btn-ghost btn-sm" data-action="sd-confirm-backup-delete" data-backup-index="${index}" aria-label="Delete backup ${safe(backup.backup_id)}">Delete</button>` : ''}</td></tr>`).join('');
        return dataTable(['Backup', 'Created', 'Description', 'Size', ''], rows);
    }

    function renderAuxiliary(view) {
        const target = document.getElementById('sdAuxiliary');
        if (!target || currentView !== view) return;
        target.innerHTML = `<div class="grid-2 sd-aux-grid">
            <div class="sd-aux-main">
                <div class="panel sd-section"><div class="panel-header"><h2>Rules</h2></div><div id="sdRulesPanel">${renderRules(view)}</div></div>
                <div class="panel sd-section"><div class="panel-header"><h2>Recent activity</h2>${guaranteeBadge((view.info.consolidation_queue || {}).guarantee)}</div>${renderActivity(view)}</div>
            </div>
            <div class="sd-aux-side">
                <div class="panel sd-section"><div class="panel-header"><h2>Access summary</h2></div><div id="sdAccessPanel">${renderAccess(view)}</div></div>
                <div class="panel sd-section"><div class="panel-header"><h2>Space actions</h2></div>${renderSpaceActions(view)}</div>
            </div>
        </div>`;
    }

    function renderSpaceActions(view) {
        const canWrite = hasPermission(view, 'write');
        const canManage = hasPermission(view, 'manage');
        return `<div class="sd-action-stack">
            ${canWrite ? `<div class="form-group"><label class="form-label" for="sdBackupDescription">Backup description</label><input id="sdBackupDescription" class="form-input" maxlength="500"><button type="button" class="btn btn-secondary sd-action-button" data-action="sd-create-backup">${icon('plus')}Create backup</button></div>` : stateUnavailable('Write permission is required to create a backup.')}
            <div id="sdBackupsPanel">${renderBackups(view)}</div>
            ${canManage ? `<div class="sd-danger-zone"><h3>Delete space</h3><p>Permanently removes this space and its stored data. Existing token grants must be removed before reusing the same ID.</p><button type="button" class="btn btn-danger" data-action="sd-confirm-space-delete">${icon('trash')}Delete space</button></div>` : stateUnavailable('Manage permission is required to delete this space.')}
        </div>`;
    }

    function renderLoadedView(view) {
        view.contentEl.innerHTML = `<div class="page sd-page">
            ${pageHeader(`Space: ${view.spaceId}`, '<a class="btn btn-secondary" href="#/spaces">Back to spaces</a>')}
            ${renderHeader(view)}
            ${renderLane(view)}
            <section class="sd-section">${tierButtons(view)}<div id="sdTierPanel"></div></section>
            <div id="sdAuxiliary"></div>
        </div>`;
        renderTier(view);
        renderAuxiliary(view);
    }

    function preparePreload(view) {
        if (view.preloadStarted) return false;
        view.preloadStarted = true;
        view.shortLoading = true;
        view.midLoading = true;
        view.longLoading = true;
        view.rulesLoading = true;
        view.backupsLoading = true;
        view.accessLoading = hasPermission(view, 'admin');
        return true;
    }

    function startPreload(view) {
        // Deliberately launch independent, bounded reads once. There is no
        // timer, retry loop, or cross-tier dependency: every panel reports its
        // own honest loaded, empty, unavailable, or error state.
        void loadShort(view);
        void loadMid(view);
        void loadLong(view);
        void loadRules(view);
        void loadBackups(view);
        if (hasPermission(view, 'admin')) void loadAccess(view);
    }

    async function loadSpace(view) {
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('space_info', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (result.status === 'not_found') {
            view.contentEl.innerHTML = `<div class="page">${pageHeader('Space not found')}<div class="panel">${stateEmpty({ title: 'Space not found', actionHtml: '<a class="btn btn-secondary" href="#/spaces">Back to spaces</a>' })}</div></div>`;
            return;
        }
        if (result.status !== 'ok') {
            view.contentEl.innerHTML = `<div class="page">${pageHeader(`Space: ${view.spaceId}`)}${panel(unavailableOrError(result, 'sd-refresh-space'))}</div>`;
            return;
        }
        view.info = result;
        const shouldPreload = preparePreload(view);
        renderLoadedView(view);
        if (shouldPreload) startPreload(view);
    }

    async function loadShort(view) {
        const limitInput = document.getElementById('sdShortLimit');
        const categoryInput = document.getElementById('sdShortCategory');
        const agentInput = document.getElementById('sdShortAgent');
        const sinceInput = document.getElementById('sdShortSince');
        view.shortFilters = {
            limit: Math.min(500, Math.max(1, Number(limitInput && limitInput.value) || 50)),
            category: CATEGORIES.includes(categoryInput && categoryInput.value) ? categoryInput.value : '',
            agent: String(agentInput && agentInput.value || '').trim(),
            since: String(sinceInput && sinceInput.value || '').trim(),
        };
        const seqAtCall = ++view.shortSeq;
        view.shortLoading = true;
        renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('live_read', { space_id: view.spaceId, ...view.shortFilters });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.shortSeq) return;
        view.shortLoading = false;
        view.shortData = result;
        renderTier(view);
    }

    async function loadJob(view, jobId) {
        const normalizedJobId = String(jobId || '');
        if (!activityJobIds(view).has(normalizedJobId)) return;
        const seqAtCall = ++view.jobSeq;
        view.jobId = normalizedJobId;
        view.jobLoading = true;
        renderAuxiliary(view);
        const epochAtCall = view.ctx.epoch;
        const result = await callTool('bank_consolidation_status', { job_id: normalizedJobId });
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.jobSeq) return;
        view.jobLoading = false;
        view.jobData = result;
        renderAuxiliary(view);
    }

    async function loadMid(view) {
        const seqAtCall = ++view.midSeq;
        view.midReadSeq += 1;
        view.midLoading = true;
        view.midPreviewHtml = '';
        view.midSelectedIndex = null;
        renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('bank_list', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.midSeq) return;
        view.midLoading = false;
        view.midData = result;
        renderTier(view);
        const files = result && result.status === 'ok' && Array.isArray(result.files) ? result.files : [];
        if (files.length) void readBankFile(view, 0);
    }

    async function loadLong(view, includeGraph = false) {
        const seqAtCall = ++view.longSeq;
        view.longLoading = true;
        renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            const args = includeGraph ? { space_id: view.spaceId, include_graph: true } : { space_id: view.spaceId };
            result = await callTool('graph_status', args);
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.longSeq) return;
        view.longLoading = false;
        view.longData = result;
        view.longGraphLoaded = includeGraph || Boolean(result && Object.prototype.hasOwnProperty.call(result, 'graph_view'));
        renderTier(view);
        if (!includeGraph && view.tier === 'long' && result && result.connected === true && result.reachable !== false) {
            void loadLong(view, true);
        }
    }

    async function loadRules(view) {
        const target = document.getElementById('sdRulesPanel');
        view.rulesLoading = true;
        if (target) target.innerHTML = stateLoading('Loading rules…');
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('space_rules', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        view.rulesLoading = false;
        view.rulesData = result;
        renderAuxiliary(view);
    }

    async function loadAccess(view) {
        if (!hasPermission(view, 'admin')) return;
        const target = document.getElementById('sdAccessPanel');
        view.accessLoading = true;
        if (target) target.innerHTML = stateLoading('Loading token metadata…');
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('admin_list_tokens', { include_revoked: true });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        view.accessLoading = false;
        view.accessData = result;
        renderAuxiliary(view);
    }

    async function loadBackups(view) {
        const target = document.getElementById('sdBackupsPanel');
        view.backupsLoading = true;
        if (target) target.innerHTML = stateLoading('Loading backups…');
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('backup_list', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        view.backupsLoading = false;
        view.backupsData = result;
        renderAuxiliary(view);
    }

    async function readBankFile(view, index) {
        const files = view.midData && Array.isArray(view.midData.files) ? view.midData.files : [];
        const file = files[index];
        if (!file) return;
        const seqAtCall = ++view.midReadSeq;
        view.midSelectedIndex = index;
        view.midPreviewHtml = stateLoading('Loading file…');
        renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('bank_read', { space_id: view.spaceId, filename: file.filename });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.midReadSeq) return;
        if (result.status !== 'ok') {
            view.midPreviewHtml = unavailableOrError(result);
            renderTier(view);
            return;
        }
        const large = Number(result.size) > 409600 ? attentionBanner('Large file', 'This preview exceeds 400 KB and may be slow to inspect.') : '';
        const note = result.note ? `${serverMessage(result.note)}<a class="sd-link" href="#/operator/maintenance">Run Repair (dry-run)</a>` : '';
        view.midPreviewHtml = `<div class="sd-preview-header"><span class="mono-data">${safe(result.filename)}</span><span>${safe(fmtSize(result.size))}</span></div>${large}${note}<div class="markdown-body">${renderMarkdown(result.content)}</div>`;
        renderTier(view);
    }

    async function updateRules(view) {
        const input = document.getElementById('sdRulesInput');
        if (!input || !hasPermission(view, 'manage')) return false;
        const rules = input.value;
        if (rules.length > RULES_LIMIT) {
            showToast('error', 'Rules exceed the 50,000-character limit.');
            return false;
        }
        const epochAtCall = view.ctx.epoch;
        const result = await callTool('space_update_rules', { space_id: view.spaceId, rules });
        if (!guarded(view, epochAtCall)) return false;
        if (result.status === 'ok') {
            showToast('ok', `Rules updated (${fmtSize(result.size)}).`);
            await loadRules(view);
            if (!guarded(view, epochAtCall)) return false;
            return true;
        }
        showToast('error', result.message || 'Rules update failed.');
        return false;
    }

    function openRulesEditor(view) {
        if (!hasPermission(view, 'manage') || !view.rulesData || view.rulesData.status !== 'ok') return;
        const rules = String(view.rulesData.rules ?? '');
        showModal(
            'Edit rules',
            `<div class="form-group"><label class="form-label" for="sdRulesInput">Rules (Markdown)</label><textarea id="sdRulesInput" class="form-input mono" rows="22" maxlength="${RULES_LIMIT + 1}">${safe(rules)}</textarea><div class="sd-counter"><span id="sdRulesCount">${safe(rules.length)}</span> / ${RULES_LIMIT}</div></div>`,
            'Save rules',
            () => updateRules(view),
        );
    }

    async function createBackup(view) {
        const input = document.getElementById('sdBackupDescription');
        const description = String(input && input.value || '').trim();
        const epochAtCall = view.ctx.epoch;
        const result = await callTool('backup_create', { space_id: view.spaceId, description });
        if (!guarded(view, epochAtCall)) return;
        if (result.status === 'created') {
            showToast('ok', 'Backup created.');
            await loadBackups(view);
            if (!guarded(view, epochAtCall)) return;
        } else showToast('error', result.message || 'Backup creation failed.');
    }

    async function graphPush(view) {
        if (view.longMutating || !hasPermission(view, 'write')) return false;
        view.longMutating = true;
        renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('graph_push', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) {
            view.longMutating = false;
            return false;
        }
        view.longMutating = false;
        if (result.status === 'ok') {
            const pushed = result.files_pushed ?? result.pushed ?? 0;
            showToast('ok', `Mid-to-long push finished: ${pushed} file(s) pushed.`);
            await loadLong(view, true);
            if (!guarded(view, epochAtCall)) return false;
            return true;
        } else {
            showModal('Mid-to-long push refused', panel(serverMessage(result.message) || stateError({ title: 'The server refused or failed this operation.' })));
            renderTier(view);
            return false;
        }
    }

    function confirmGraphPush(view) {
        if (!hasPermission(view, 'write') || view.longMutating) return;
        const epochAtOpen = view.ctx.epoch;
        showModal(
            'Push mid → long',
            '<p class="body-small">Project the current mid-tier bank into the derived long graph. This is an explicit projection, not a routine flow; it never makes long memory authoritative for commits, recovery, audit, membership, or mesh state.</p><p class="body-small">Volatile bank files are not included.</p>',
            'Push mid → long',
            async () => {
                if (!guarded(view, epochAtOpen)) return false;
                return graphPush(view);
            },
        );
    }

    async function consolidate(view) {
        if (view.consolidating || !hasPermission(view, 'write')) return false;
        view.consolidating = true;
        renderTier(view);
        const args = { space_id: view.spaceId };
        // The confirmation copy promises all-agent scope to manage/admin,
        // so serialize that intent explicitly. Omission is caller-only.
        if (hasPermission(view, 'manage')) args.agent = '';
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('bank_consolidate', args);
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) {
            view.consolidating = false;
            return false;
        }
        view.consolidating = false;
        const queuePosition = Number(result.queue_position);
        if (result.status === 'running' || result.status === 'queued' || queuePosition >= 1) {
            showToast('ok', result.status === 'running' || queuePosition === 1
                ? 'Consolidation running'
                : `Consolidation queued (position ${queuePosition || '?'})`);
            await loadSpace(view);
            if (!guarded(view, epochAtCall)) return false;
            return true;
        }
        showModal('Consolidation refused', panel(serverMessage(result.message) || stateError({ title: 'The server refused or failed this operation.' })));
        renderTier(view);
        return false;
    }

    function confirmConsolidate(view) {
        if (!hasPermission(view, 'write') || view.consolidating) return;
        const scopeCopy = hasPermission(view, 'manage')
            ? `Consolidate all agents' live notes in space <code>${safe(view.spaceId)}</code>.`
            : `Consolidate only your own live notes in space <code>${safe(view.spaceId)}</code>; the server enforces that scope.`;
        const epochAtOpen = view.ctx.epoch;
        showModal(
            'Consolidate live notes',
            `<p class="body-small">${scopeCopy}</p><p class="body-small">Consolidation is asynchronous and no status polling will be started.</p>`,
            'Consolidate',
            async () => {
                if (!guarded(view, epochAtOpen)) return false;
                return consolidate(view);
            },
        );
    }

    function confirmBackupDelete(view, index) {
        const backups = view.backupsData && Array.isArray(view.backupsData.backups) ? view.backupsData.backups : [];
        const backup = backups[index];
        if (!backup) return;
        showDestructiveModal({
            title: 'Delete backup',
            verb: 'Delete backup',
            typedConfirmation: backup.backup_id,
            bodyHtml: `<p>Permanently delete backup <span class="mono-data">${safe(backup.backup_id)}</span>.</p>`,
            onConfirm: async () => {
                const epochAtCall = view.ctx.epoch;
                const result = await callTool('backup_delete', { backup_id: backup.backup_id, confirm: true });
                if (!guarded(view, epochAtCall)) return false;
                if (result.status === 'deleted' || result.status === 'ok') {
                    showToast('ok', 'Backup deleted.');
                    await loadBackups(view);
                    if (!guarded(view, epochAtCall)) return false;
                    return true;
                }
                showToast('error', result.message || 'Backup deletion failed.');
                return false;
            },
        });
    }

    function renderSpaceDeleteRecovery(result) {
        const recovery = result && result.recovery ? result.recovery : {};
        const failedKeys = result && Array.isArray(result.failed_keys) ? result.failed_keys : [];
        const failedKeysHtml = failedKeys.length
            ? `<ul class="sd-list">${failedKeys.map(key => `<li><code class="mono-data">${safe(key)}</code></li>`).join('')}</ul>`
            : '<code class="mono-data">[]</code>';
        const markerPreserved = result.marker_preserved === null ? 'null' : String(result.marker_preserved);
        const retrySafe = recovery.retry_safe === null ? 'null' : String(recovery.retry_safe);
        return `<div class="sd-delete-recovery" data-recovery-required="true">
            <div class="sd-banner sd-banner--error" role="alert">${icon('alert')}<div>
                <strong>Space deletion incomplete — recovery required (not successful)</strong>
                <p>${safe(result.message)}</p>
            </div></div>
            <div class="sd-meta-row">
                ${keyValue('files_total', result.files_total)}
                ${keyValue('files_deleted', result.files_deleted)}
                ${keyValue('marker_preserved', markerPreserved)}
                ${keyValue('recovery.retry_safe', retrySafe)}
            </div>
            <div class="form-group"><span class="micro-label">failed_keys</span>${failedKeysHtml}</div>
            <div class="form-group"><span class="micro-label">recovery.action</span><p>${safe(recovery.action)}</p></div>
            <p class="form-hint">No automatic retry, cleanup, success toast, or navigation was performed.</p>
        </div>`;
    }

    function showSpaceDeleteRecovery(result) {
        const summary = document.querySelector('#adminModal .destructive-summary');
        if (!summary) return;
        summary.setAttribute('data-recovery-required', 'true');
        summary.innerHTML = renderSpaceDeleteRecovery(result);
    }

    function confirmSpaceDelete(view) {
        const label = String(view.info.hive_status_label || '');
        const carriesSharedState = label !== 'not_a_space' && label !== 'local_only';
        const warning = carriesSharedState
            ? failClosedBanner('Shared-state deletion refused', 'This space carries shared Hivemind state. Normal deletion is refused by the server. Advanced unsafe recovery is MCP-only and is intentionally not exposed by this console.')
            : '';
        showDestructiveModal({
            title: 'Delete space',
            verb: 'Delete space',
            typedConfirmation: view.spaceId,
            bodyHtml: `${warning}<p>Permanently deletes this space and its stored data. Access grants for this space remain on tokens and must be removed before the same ID can be created again.</p>`,
            onConfirm: async () => {
                const epochAtCall = view.ctx.epoch;
                const result = await callTool('space_delete', { space_id: view.spaceId, confirm: true });
                if (!guarded(view, epochAtCall)) return false;
                if (result.status === 'partial' && result.recovery_required === true) {
                    showSpaceDeleteRecovery(result);
                    return false;
                }
                if (result.status === 'deleted' || result.status === 'ok') {
                    showToast('ok', 'Space deleted.');
                    AdminRouter.go('/spaces');
                    return true;
                }
                showToast('error', result.message || 'Space deletion failed.');
                return false;
            },
        });
    }

    function getView() {
        return currentView && currentView.info ? currentView : null;
    }

    registerAction('sd-select-tier', data => {
        const view = getView();
        if (!view || !TIERS.has(data.tier)) return;
        view.tier = data.tier;
        history.replaceState(null, '', `#/spaces/${encodeURIComponent(view.spaceId)}/${data.tier}`);
        const tabs = document.querySelector('.sd-tier-tabs');
        if (tabs) tabs.outerHTML = tierButtons(view);
        renderTier(view);
        if (data.tier === 'long' && !view.longLoading && !view.longGraphLoaded) void loadLong(view, true);
    });
    registerAction('sd-refresh-space', () => { const view = currentView; if (view) loadSpace(view); });
    registerAction('sd-apply-short-filters', () => { const view = getView(); if (view) loadShort(view); });
    registerAction('sd-retry-short', () => { const view = getView(); if (view) loadShort(view); });
    registerAction('sd-retry-mid', () => { const view = getView(); if (view) loadMid(view); });
    registerAction('sd-retry-long', () => { const view = getView(); if (view) loadLong(view, view.tier === 'long'); });
    registerAction('sd-retry-rules', () => { const view = getView(); if (view) loadRules(view); });
    registerAction('sd-retry-access', () => { const view = getView(); if (view) loadAccess(view); });
    registerAction('sd-retry-backups', () => { const view = getView(); if (view) loadBackups(view); });
    registerAction('sd-load-job', data => { const view = getView(); if (view) loadJob(view, data.jobId); });
    registerAction('sd-read-bank', data => { const view = getView(); if (view) readBankFile(view, Number(data.fileIndex)); });
    registerAction('sd-confirm-backup-delete', data => { const view = getView(); if (view) confirmBackupDelete(view, Number(data.backupIndex)); });
    registerAction('sd-preview-note', data => {
        const view = getView();
        const notes = view && view.shortData && Array.isArray(view.shortData.notes) ? view.shortData.notes : [];
        const note = notes[Number(data.noteIndex)];
        const target = document.getElementById('sdNotePreview');
        if (note && target) target.innerHTML = `<div class="sd-preview-header"><span class="mono-data">${safe(note.filename || note.note_id || '')}</span>${renderTimestamp(note.timestamp)}</div><pre class="mono-block">${safe(note.content)}</pre>`;
    });
    registerAction('sd-edit-rules', () => { const view = getView(); if (view) openRulesEditor(view); });
    registerAction('sd-create-backup', () => { const view = getView(); if (view) createBackup(view); });
    registerAction('sd-confirm-consolidate', () => { const view = getView(); if (view) confirmConsolidate(view); });
    registerAction('sd-confirm-graph-push', () => { const view = getView(); if (view) confirmGraphPush(view); });
    registerAction('sd-confirm-space-delete', () => { const view = getView(); if (view) confirmSpaceDelete(view); });

    document.addEventListener('input', event => {
        if (event.target.id !== 'sdRulesInput') return;
        const counter = document.getElementById('sdRulesCount');
        if (counter) counter.textContent = String(event.target.value.length);
        event.target.setAttribute('aria-invalid', event.target.value.length > RULES_LIMIT ? 'true' : 'false');
    });

    document.addEventListener('keydown', event => {
        const row = event.target.closest && event.target.closest('.sd-file-row');
        if (!row || (event.key !== 'Enter' && event.key !== ' ')) return;
        event.preventDefault();
        row.click();
    });

    function render(contentEl, params, ctx) {
        const spaceId = params && typeof params.spaceId === 'string' ? params.spaceId : '';
        if (!SPACE_ID_RE.test(spaceId)) {
            currentView = null;
            contentEl.innerHTML = `<div class="page">${pageHeader('Invalid space id')}<div class="panel">${stateError({ title: 'Invalid space id' })}<a class="btn btn-secondary" href="#/spaces">Back to spaces</a></div></div>`;
            return;
        }
        const tier = params && TIERS.has(params.tier) ? params.tier : 'short';
        const view = {
            contentEl, ctx, spaceId, tier, info: null,
            shortFilters: { limit: 50, category: '', agent: '', since: '' },
            shortData: null, shortLoading: false, shortSeq: 0,
            midData: null, midLoading: false, midSeq: 0, midReadSeq: 0,
            midSelectedIndex: null, midPreviewHtml: '',
            longData: null, longLoading: false, longMutating: false,
            longSeq: 0, longGraphLoaded: false,
            rulesData: null, rulesLoading: false,
            accessData: null, accessLoading: false,
            backupsData: null, backupsLoading: false,
            preloadStarted: false, consolidating: false,
            jobId: '', jobData: null, jobLoading: false, jobSeq: 0,
        };
        currentView = view;
        contentEl.innerHTML = `<div class="page">${pageHeader(`Space: ${spaceId}`, '<a class="btn btn-secondary" href="#/spaces">Back to spaces</a>')}${panel(stateLoading('Loading space…'))}</div>`;
        loadSpace(view).catch(() => {
            if (!guarded(view, ctx.epoch)) return;
            contentEl.innerHTML = `<div class="page">${pageHeader(`Space: ${spaceId}`)}${panel(stateError({ title: "Couldn't load this space", retryAction: 'sd-refresh-space' }))}</div>`;
        });
    }

    AdminViews.register('space-detail', render);
})();
