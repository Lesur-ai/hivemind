/**
 * Space Detail — P8-3 (#141).
 *
 * Field-mapped renderers only: no raw-object fallback, no invented values.
 * Entry loads space_info first, then only the active section. Memory tiers
 * use one opt-in PortalRefresh consumer. Every awaited continuation is epoch guarded.
 */
(function () {
    'use strict';

    const CATEGORIES = ['', 'observation', 'decision', 'todo', 'insight', 'question', 'progress', 'issue'];
    const SENTINEL_STATUSES = new Set(['read_only', 'rate_limited', 'truncated']);
    const RULES_LIMIT = 50000;
    const SOURCE_STATE_TOKEN_RE = /^[0-9a-f]{64}$/;
    let currentView = null;
    let nextTabFocus = '';
    let nextLongFocus = '';
    // Active work "Open job" target (#651): session memory only, consumed or
    // discarded by the next Space render; job IDs never enter a URL or storage.
    let pendingHandoff = null;
    const ACTIVITY_JOB_LIMIT = 10;
    const ACTIVITY_JOBS_SHOWN = 3;

    function safe(value) {
        return esc(String(value ?? ''));
    }

    function safeLongEndpoint(value) {
        if (typeof value !== 'string') return safe(value);
        try {
            const endpoint = new URL(value);
            endpoint.username = '';
            endpoint.password = '';
            for (const key of [...endpoint.searchParams.keys()]) {
                endpoint.searchParams.set(key, '[redacted]');
            }
            return safe(endpoint.href);
        } catch (_) {
            return safe(value.replace(/(\/\/)[^/@\s]+@/, '$1[redacted]@')
                .replace(/\?[^#]*/, '?[redacted]'));
        }
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

    function meshReadinessAvailable(view) {
        return hasPermission(view, 'admin')
            && typeof meshIsAvailable === 'function'
            && meshIsAvailable() === true;
    }

    function authoritativeMeshSource(view, result) {
        const source = result && result.status === 'ok' ? result.source : null;
        if (!source || source.space_id !== view.spaceId || !SPACE_ID_RE.test(source.space_id)) return null;
        if (typeof source.state !== 'string'
            || typeof source.source_ready !== 'boolean'
            || typeof source.source_initializable !== 'boolean'
            || typeof source.can_create_invitation !== 'boolean'
            || typeof source.resumable !== 'boolean'
            || typeof source.reason_code !== 'string'
            || typeof source.message !== 'string'
            || typeof source.state_token !== 'string'
            || !SOURCE_STATE_TOKEN_RE.test(source.state_token)) return null;
        return source;
    }

    function meshReadinessAction(source) {
        if (!source) return null;
        if (source.state === 'local_only_can_prepare' && source.source_initializable === true) {
            return { label: 'Prepare for Project Mesh', kind: 'prepare' };
        }
        if (source.state === 'preparing'
            && source.source_initializable === true
            && source.resumable === true) {
            return { label: 'Resume preparation', kind: 'resume' };
        }
        return null;
    }

    function renderMeshReadiness(view) {
        if (!meshReadinessAvailable(view)) return '';
        if (view.meshReadinessLoading) return '<p class="form-hint">Checking Project Mesh readiness…</p>';
        if (view.meshReadinessError) {
            return `<p class="form-hint">${safe(view.meshReadinessError)}</p><button type="button" class="btn btn-ghost btn-sm" data-action="sd-retry-mesh-readiness">Retry readiness</button>`;
        }
        const source = view.meshReadiness;
        if (!source) return '';
        const action = meshReadinessAction(source);
        if (action) {
            return `<a class="sd-link" data-mesh-source-action="${action.kind}" href="#/mesh">${safe(action.label)}</a><p class="form-hint">${safe(source.message)}</p>`;
        }
        const href = source.source_ready === true
            ? `#/mesh/${encodeURIComponent(view.spaceId)}`
            : '#/mesh';
        return `<a class="sd-link" href="${href}">${source.can_create_invitation === true ? 'View in Project Mesh' : 'Inspect Project Mesh readiness'}</a><p class="form-hint">${safe(source.message)}</p>`;
    }

    function updateMeshReadiness(view) {
        const target = document.getElementById('sdMeshReadiness');
        if (!target || currentView !== view) return;
        target.innerHTML = renderMeshReadiness(view);
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
        const size = value => value == null ? 'not recorded' : safe(fmtSize(value));
        const count = value => safe(value ?? '—');
        return `${status.banner}
            <section class="sd-overview" aria-label="Space overview">
                <div class="sd-summary">
                    <div class="sd-summary__identity">
                        ${copyable(info.space_id || view.spaceId)}
                        <p>${info.description ? safe(info.description) : '<span class="text-faint">No description</span>'}</p>
                        <span class="chip">${safe((view.ctx.identity.permissions || []).join(' · '))}</span>
                    </div>
                    <div class="sd-summary__status">
                        ${status.marker}
                        ${helper}
                        <div id="sdMeshReadiness">${renderMeshReadiness(view)}</div>
                    </div>
                </div>
                <dl class="summary-stats sd-metrics">
                    <div><dt>Short notes</dt><dd><span class="summary-stats__value">${count(info.live && info.live.notes_count)}</span><span class="body-small">${size(info.live && info.live.total_size)}</span></dd></div>
                    <div><dt>Bank files</dt><dd><span class="summary-stats__value">${count(info.bank && info.bank.files_count)}</span><span class="body-small">${size(info.bank && info.bank.total_size)}</span></dd></div>
                    <div><dt>Consolidations</dt><dd><span class="summary-stats__value">${count(info.consolidation_count)}</span></dd></div>
                    <div><dt>Synthesis</dt><dd>${info.synthesis_exists == null ? 'not recorded' : info.synthesis_exists ? 'Ready' : 'Absent'}</dd></div>
                </dl>
                <details class="sd-metadata">
                    <summary>Space details</summary>
                    <div class="sd-meta-row">
                        ${keyValue('Created', info.created_at, { timestamp: true })}
                        ${keyValue('Owner', info.owner)}
                        ${keyValue('Last consolidation', info.last_consolidation, { timestamp: true })}
                        ${keyValue('Mesh state code', status.raw)}
                    </div>
                </details>
            </section>`;
    }

    function tierButtons(view) {
        return `<div class="sd-tier-tabs" role="tablist" aria-label="Memory tier">
            ${['short', 'mid'].map(tier => `<button type="button" class="sd-tier-tab${view.tier === tier ? ' active' : ''}" role="tab" id="sdTierTab-${tier}" aria-controls="sdTierPanel" tabindex="${view.tier === tier ? '0' : '-1'}" aria-selected="${view.tier === tier ? 'true' : 'false'}" data-action="sd-select-tier" data-tier="${tier}">${safe(tier)}</button>`).join('')}
        </div>`;
    }

    function spaceSections(view) {
        const tabs = [['memory', 'Memory'], ['activity', 'Active work'], ['consolidation', 'Consolidation'], ['long', 'Long memory'], ['rules', 'Rules']];
        if (hasPermission(view, 'manage')) tabs.push(['access', 'Access']);
        if (hasPermission(view, 'write')) tabs.push(['backups', 'Backups']);
        if (hasPermission(view, 'manage')) tabs.push(['maintenance', 'Maintenance']);
        return tabs;
    }

    function spaceTabs(view) {
        const tabs = spaceSections(view);
        const hasActive = tabs.some(([tab]) => tab === view.tab);
        return `<div class="sd-space-nav"><div class="sd-space-tabs" role="tablist" aria-label="Space sections">${tabs.map(([tab, label], index) => `<button type="button" class="sd-space-tab${view.tab === tab ? ' active' : ''}" id="sdSpaceTab-${tab}" role="tab" aria-controls="sdSpacePanel" aria-selected="${view.tab === tab}" tabindex="${view.tab === tab || (!hasActive && index === 0) ? '0' : '-1'}" data-action="sd-select-tab" data-tab="${tab}">${label}</button>`).join('')}</div>${hasPermission(view, 'admin') ? '<a class="sd-link" href="#/audit">Global audit</a>' : ''}</div>`;
    }

    function renderTier(view) {
        if (view.tab === 'consolidation') {
            const report = document.getElementById('sdConsolidationCompaction');
            const action = document.getElementById('sdConsolidationCheck');
            if (currentView === view && report) report.innerHTML = renderCompactionReport(view);
            if (currentView === view && action && hasPermission(view, 'manage')) action.innerHTML = renderCompactionAction(view);
            return;
        }
        const target = document.getElementById('sdTierPanel');
        if (!target || currentView !== view) return;
        if (view.tier === 'long') {
            if (view.longMounted !== target) { target.innerHTML = renderLong(view); view.longMounted = target; }
            paintLong(view); return;
        }
        if (target.dataset.memoryTier !== view.tier || view.memoryMounted !== target) {
            target.innerHTML = view.tier === 'short' ? renderShort(view) : renderMid(view);
            target.dataset.memoryTier = view.tier; view.memoryMounted = target;
            view.shortRows = new Map(); view.shortGroups = new Map(); view.midTabs = new Map();
            view.midRenderedContent = null; view.midRenderedFilename = null;
        }
        if (view.tier === 'short') renderShortData(view, view.shortData);
        else renderMidData(view, view.midData);
    }

    function memoryHtml(node, html) { if (node && node.innerHTML !== html) node.innerHTML = html; }
    function memoryFreshness(id, timestamp, error) {
        memoryHtml(document.getElementById(id), `${timestamp ? `Updated ${renderTimestamp(timestamp)}` : 'Not updated yet'}${error ? `<p class="state-degraded" role="status">${safe(error)}</p>` : ''}`);
    }
    function shortInlineCode(text) { return safe(text).replace(/`([^`\n]+)`/g, '<code>$1</code>'); }
    function localNoteDay(timestamp) {
        const date = new Date(timestamp);
        if (!timestamp || !Number.isFinite(date.getTime())) return { key: 'unknown', label: 'Date unavailable' };
        return { key: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`, label: new Intl.DateTimeFormat(undefined, { dateStyle: 'long' }).format(date) };
    }

    function renderShort(view) {
        const draft = view.shortDraft;
        const categories = CATEGORIES.map(value => `<option value="${safe(value)}"${draft.category === value ? ' selected' : ''}>${safe(value || 'All categories')}</option>`).join('');
        return `<div class="item-card tier-short sd-tier-card">
            <div class="panel-header"><div><span class="micro-label mono-data">SHORT</span><h2>Live notes</h2><p class="body-small">Pending notes · no implicit date or agent filter.</p></div><div class="sd-tier-actions"><span id="sdShortCount" class="count-pill"></span>${renderConsolidateAction(view)}</div></div>
            <div class="sd-filter-grid">
                <div><label class="form-label" for="sdShortLimit">Limit</label><input id="sdShortLimit" class="form-input mono" type="number" min="1" max="500" value="${safe(draft.limit)}"></div>
                <div><label class="form-label" for="sdShortCategory">Category</label><select id="sdShortCategory" class="form-input">${categories}</select></div>
                <div><label class="form-label" for="sdShortAgent">Agent</label><input id="sdShortAgent" class="form-input" value="${safe(draft.agent)}"></div>
                <div><label class="form-label" for="sdShortSince">Since…</label><input id="sdShortSince" class="form-input" type="datetime-local" value="${safe(draft.since)}"></div>
                <button type="button" class="btn btn-secondary sd-filter-submit" data-action="sd-apply-short-filters">Apply filters</button>
            </div><div id="sdShortFilterError" role="status"></div><div id="sdShortFreshness" class="body-small text-muted"></div>
            <div id="sdShortBody" class="sd-note-feed"></div><p id="sdShortBound" class="form-hint"></p>
        </div>`;
    }

    function renderShortData(view, data) {
        const body = document.getElementById('sdShortBody');
        if (!body) return;
        memoryFreshness('sdShortFreshness', view.shortSuccess, view.shortError);
        if (!data) { memoryHtml(body, view.shortError ? unavailableOrError({ message: view.shortError }, 'sd-retry-short') : stateLoading('Loading recent notes…')); return; }
        const notes = data.notes;
        if (!view.shortRows.size) body.innerHTML = '';
        const beforeScroll = body.scrollTop;
        const top = body.getBoundingClientRect().top;
        const anchor = [...body.querySelectorAll('.sd-note')].find(row => row.getBoundingClientRect().bottom > top);
        const anchorTop = anchor?.getBoundingClientRect().top;
        const groups = new Map(), retained = new Set();
        notes.forEach((note, index) => {
            const day = localNoteDay(note.timestamp), key = String(note.filename || note.note_id || index);
            retained.add(key);
            let group = view.shortGroups.get(day.key);
            if (!group) {
                group = document.createElement('section'); group.dataset.day = day.key; group.className = 'sd-note-day';
                group.innerHTML = `<h3>${safe(day.label)}</h3><div class="sd-note-day-list"></div>`;
                view.shortGroups.set(day.key, group);
            }
            if (!groups.has(day.key)) groups.set(day.key, []);
            groups.get(day.key).push(key);
            let row = view.shortRows.get(key);
            if (!row) {
                row = document.createElement('article'); row.className = 'sd-note'; row.dataset.noteId = key;
                row.innerHTML = '<div class="sd-note__top" data-part="meta"></div><div class="sd-chip-row" data-part="tags"></div><div class="sd-note-content"></div>';
                view.shortRows.set(key, row);
            }
            const date = Number.isFinite(Date.parse(note.timestamp)) ? renderTimestamp(note.timestamp) : 'Time unavailable';
            memoryHtml(row.querySelector('[data-part="meta"]'), `<span>${safe(note.agent || 'Agent unavailable')} · ${pill('neutral', note.category || 'uncategorized')}${view.shortNewKeys.has(key) ? ' <span class="pill pill-neutral">New</span>' : ''}</span>${date}`);
            const tags = Array.isArray(note.tags) ? note.tags.map(tag => `<span class="chip">${safe(tag)}</span>`).join('') : '';
            const provenance = note.provenance && typeof note.provenance === 'object' ? `<span>${safe(note.provenance.label || note.provenance.origin_agent || note.provenance.origin_node_id || '')}</span>` : '';
            memoryHtml(row.querySelector('[data-part="tags"]'), tags + provenance);
            if (row.renderedContent !== note.content) {
                row.querySelector('.sd-note-content').innerHTML = shortInlineCode(note.content);
                row.renderedContent = note.content;
            }
        });
        let groupIndex = 0;
        for (const [day, keys] of groups) {
            const group = view.shortGroups.get(day), list = group.querySelector('.sd-note-day-list');
            if (body.children[groupIndex] !== group) body.insertBefore(group, body.children[groupIndex] || null);
            keys.forEach((key, index) => { const row = view.shortRows.get(key); if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null); });
            groupIndex++;
        }
        for (const [key, row] of view.shortRows) if (!retained.has(key)) { row.remove(); view.shortRows.delete(key); }
        for (const [key, group] of view.shortGroups) if (!groups.has(key)) { group.remove(); view.shortGroups.delete(key); }
        if (!notes.length) memoryHtml(body, stateEmpty({ title: 'No notes match these filters' }));
        body.scrollTop = anchor?.isConnected ? beforeScroll + anchor.getBoundingClientRect().top - anchorTop : beforeScroll;
        const count = document.getElementById('sdShortCount'); if (count) count.textContent = `Showing ${notes.length} notes`;
        const bound = document.getElementById('sdShortBound'); if (bound) bound.textContent = `Showing ${notes.length} returned notes${data.has_more === true ? ' · more notes are available' : ''}.`;
    }

    function renderMid(view) {
        return `<div class="item-card tier-mid sd-tier-card">
            <div class="panel-header"><div><span class="micro-label mono-data">MID</span><h2>Memory Bank</h2></div><div class="sd-tier-actions"><span id="sdMidCount" class="count-pill"></span><span id="sdMidCompactionActions">${renderCompactionAction(view)}</span><span id="sdMidGraphPushActions">${renderGraphPushAction(view)}</span><a class="sd-link" href="#/spaces/${encodeURIComponent(view.spaceId)}/long/overview">Long memory</a></div></div>
            <div id="sdMidBody"><p id="sdLastConsolidation"></p><div id="sdMidMetadataFreshness" class="body-small text-muted"></div><div id="sdMidListFreshness" class="body-small text-muted"></div>
            <div id="sdFileTabs" class="sd-file-tabs" role="tablist" aria-label="Memory Bank files"></div><div id="sdMidEmpty"></div>
            <div id="sdMidFileFreshness" class="body-small text-muted"></div><article id="sdBankPreview" class="sd-reader sd-bank-reader" role="tabpanel" aria-label="Selected Memory Bank file" tabindex="0"><div id="sdBankFileMeta" class="sd-preview-header"></div><div id="sdBankMarkdown" class="markdown-body"></div></article></div>
            <div id="sdMidCompactionReport">${renderCompactionReport(view)}</div>
        </div>`;
    }

    function renderMidData(view, data) {
        memoryFreshness('sdMidListFreshness', view.midListSuccess, view.midListError);
        memoryFreshness('sdMidMetadataFreshness', view.midMetadataSuccess, view.midMetadataError);
        memoryFreshness('sdMidFileFreshness', view.midFileSuccess, view.midFileError);
        if (view.midSelectedFilename && !view.midFileData && !view.midFileError) memoryHtml(document.getElementById('sdMidFileFreshness'), 'Loading selected file…');
        memoryHtml(document.getElementById('sdLastConsolidation'), `Last consolidation: ${view.info?.last_consolidation ? renderTimestamp(view.info.last_consolidation) : 'Not recorded'}`);
        const files = data && Array.isArray(data.files) ? data.files : [];
        const parent = document.getElementById('sdFileTabs'), retained = new Set();
        const focusedTab = parent?.contains(document.activeElement) ? document.activeElement : null;
        if (parent) files.forEach((file, index) => {
            retained.add(file.filename);
            let tab = view.midTabs.get(file.filename);
            if (!tab) {
                tab = document.createElement('button'); tab.type = 'button'; tab.className = 'sd-file-tab';
                tab.dataset.action = 'sd-read-bank'; tab.dataset.filename = file.filename;
                tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', 'sdBankPreview'); tab.textContent = file.filename;
                view.midTabs.set(file.filename, tab);
            }
            const selected = file.filename === view.midSelectedFilename;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected || (index === 0 && !files.some(item => item.filename === view.midSelectedFilename)) ? 0 : -1;
            if (parent.children[index] !== tab) parent.insertBefore(tab, parent.children[index] || null);
        });
        for (const [name, tab] of view.midTabs) if (!retained.has(name)) { tab.remove(); view.midTabs.delete(name); }
        if (focusedTab?.isConnected && document.activeElement !== focusedTab) focusedTab.focus({ preventScroll: true });
        const count = document.getElementById('sdMidCount'); if (count) count.textContent = data ? `${files.length} files` : '';
        memoryHtml(document.getElementById('sdMidEmpty'), !data ? (view.midListError ? unavailableOrError({ message: view.midListError }, 'sd-retry-mid') : stateLoading('Loading bank files…')) : !files.length ? stateEmpty({ title: 'No bank files' }) : '');
        const preview = view.midFileData;
        const meta = preview ? `<span>${safe(preview.filename)} · ${safe(fmtSize(preview.size))}</span><span>Last modified: ${preview.last_modified ? renderTimestamp(preview.last_modified) : 'Not recorded'}</span>` : '';
        memoryHtml(document.getElementById('sdBankFileMeta'), meta);
        const reader = document.getElementById('sdBankPreview'), markdown = document.getElementById('sdBankMarkdown');
        if (reader && markdown && preview && (view.midRenderedFilename !== preview.filename || view.midRenderedContent !== preview.content)) {
            const top = reader.scrollTop, left = reader.scrollLeft, focused = document.activeElement;
            const wasInside = focused && markdown.contains(focused), href = wasInside ? focused.getAttribute('href') : null;
            memoryHtml(markdown, view.midPreviewHtml);
            view.midRenderedFilename = preview.filename; view.midRenderedContent = preview.content;
            if (wasInside) {
                const link = href && [...markdown.querySelectorAll('a[href]')].find(item => item.getAttribute('href') === href);
                (link || reader).focus({ preventScroll: true });
            }
            reader.scrollTop = Math.min(top, Math.max(0, reader.scrollHeight - reader.clientHeight));
            reader.scrollLeft = Math.min(left, Math.max(0, reader.scrollWidth - reader.clientWidth));
        }
        memoryHtml(document.getElementById('sdMidCompactionActions'), renderCompactionAction(view));
        memoryHtml(document.getElementById('sdMidGraphPushActions'), renderGraphPushAction(view));
        memoryHtml(document.getElementById('sdMidCompactionReport'), renderCompactionReport(view));
    }

    function compactByteText(value) {
        return (typeof value === 'number' && Number.isFinite(value) && value >= 0)
            ? `${value} UTF-8 bytes`
            : 'unknown / not asserted';
    }

    function compactTargetDetail(failure) {
        if (!failure || typeof failure !== 'object'
            || failure.error !== 'ambiguous_or_missing_compaction_target') return '';
        const index = failure.operation_index;
        const resolution = failure.target_resolution;
        const count = failure.target_match_count;
        const sha256 = failure.target_heading_sha256;
        if (!Number.isSafeInteger(index) || index < 0
            || (resolution !== 'missing' && resolution !== 'ambiguous')
            || !Number.isSafeInteger(count) || count < 0
            || typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)
            || (resolution === 'missing' && count !== 0)
            || (resolution === 'ambiguous' && count < 2)) return '';
        return `operation_index=${index}; target_resolution=${resolution}; target_match_count=${count}; target_heading_sha256=${sha256}`;
    }

    function compactFailureRows(failures) {
        if (!Array.isArray(failures)) return '';
        return failures.map(f => {
            if (!f || typeof f !== 'object'
                || typeof f.filename !== 'string' || typeof f.error !== 'string') return '';
            const detail = compactTargetDetail(f);
            return `<tr><td class="mono">${safe(f.filename)}</td><td class="mono">${safe(f.error)}</td><td class="mono">${safe(detail || '—')}${detail ? copyable(f.target_heading_sha256, 'Target fingerprint') : ''}</td></tr>`;
        }).join('');
    }

    function compactSize(value) {
        return typeof value === 'number' && Number.isFinite(value) && value >= 0
            ? fmtSize(value) : 'Unknown';
    }

    function compactEvidence(data) {
        const files = Array.isArray(data.files) ? data.files : [];
        const id = data.preimage_id ? `<p class="body-small"><strong>Preimage reference:</strong> ${copyable(String(data.preimage_id))}</p>` : '';
        const rows = files.filter(file => file && typeof file === 'object').map(file => {
            const hash = value => typeof value === 'string' && value
                ? copyable(value, value.length > 24 ? `${value.slice(0, 12)}…${value.slice(-8)}` : value) : '—';
            const values = [
                ['Source SHA-256', hash(file.source_sha256)],
                ['Result SHA-256', hash(file.result_sha256)],
                ['Original UTF-8 bytes', safe(compactByteText(file.size))],
                ['Advisory UTF-8 bytes', safe(compactByteText(file.max_size))],
                ['Size / advisory ratio', safe(file.ratio ?? '—')],
                ['Compacted UTF-8 bytes', safe(compactByteText(file.compacted_size))],
                ['Reduction', safe(typeof file.reduction_pct === 'number' ? `${file.reduction_pct}%` : '—')],
                ['File failure', safe(file.error || '—')],
            ];
            return `<div class="compaction-file-evidence"><h4>${safe(file.filename || '')}</h4><dl>${values.map(([label, value]) => `<dt>${safe(label)}</dt><dd>${value}</dd>`).join('')}</dl></div>`;
        }).join('');
        return `${id}<p class="body-small">Report status: ${safe(data.status || 'unknown')} · ${safe(data.files_total ?? '—')} files checked · ${safe(data.files_over_limit ?? '—')} above advisory size.</p><p class="body-small">Total before: ${safe(compactByteText(data.total_size_before))} · Total after: ${safe(compactByteText(data.total_size_after))}</p>${rows}`;
    }

    function compactSuccessMarkup(data) {
        const applied = data.dry_run === false;
        const files = Array.isArray(data.files) ? data.files : [];
        const finalKnown = typeof data.total_size_after === 'number' && Number.isFinite(data.total_size_after) && data.total_size_after >= 0;
        const summary = applied
            ? `<p class="body-small">${safe(data.files_total ?? '—')} files · ${safe(compactSize(data.total_size_before))} before · ${safe(compactSize(data.total_size_after))} after.</p>${finalKnown ? '' : '<p class="form-error">Final size could not be verified.</p>'}`
            : `<p class="body-small">${safe(data.files_over_limit ?? '—')} of ${safe(data.files_total ?? '—')} files exceed the advisory size. <strong>No changes made.</strong></p><p class="form-hint">This check lists eligible files; it does not generate rewritten content.</p>`;
        const rows = files.filter(file => file && typeof file === 'object').map(file => {
            let outcome = file.over_limit === false ? 'Within advisory size' : file.over_limit === true ? 'Eligible' : 'Eligibility unknown';
            if (applied && file.over_limit !== false) outcome = typeof file.reduction_pct === 'number' ? `Reduced ${file.reduction_pct}%` : 'Result unavailable';
            if (file.error) outcome = String(file.error);
            return `<tr><td class="mono">${safe(file.filename || '')}</td><td class="num">${safe(compactSize(file.size))}</td><td class="num">${safe(compactSize(file.max_size))}</td>${applied ? `<td class="num">${safe(compactSize(file.compacted_size))}</td>` : ''}<td>${safe(outcome)}</td></tr>`;
        }).join('');
        const headers = applied ? ['File', 'Before', 'Advisory size', 'After', 'Result'] : ['File', 'Size', 'Advisory size', 'Result'];
        const table = rows ? `<div class="table-scroll"><table class="data-table"><thead><tr>${headers.map(header => `<th scope="col">${safe(header)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>` : stateEmpty({ title: 'No bank files' });
        return summary + table + `<details class="compaction-details"><summary>Technical details</summary>${compactEvidence(data)}</details>`;
    }

    function compactFailureMarkup(data) {
        if (data && data.status === 'conflict') {
            return `<div class="state state-degraded" role="status">${icon('alert')}<div><h3>Consolidation in progress</h3><p class="body-small">Consolidation is running for this space — retry when the lane is idle.</p>${data.message ? serverMessage(data.message) : ''}</div></div>`;
        }
        const status = safe(data && data.status || 'error');
        const recoveryRequired = data && (data.recovery_required === true || data.apply_may_have_mutated === true);
        const heading = recoveryRequired ? 'Compaction recovery required' : `Compaction refused or failed (${status})`;
        const reason = data && data.failure_reason ? `<p class="body-small"><strong>Failure reason:</strong> ${safe(data.failure_reason)}</p>` : '';
        const message = data && data.message ? serverMessage(data.message) : '';
        const remediation = data && data.remediation ? `<p class="body-small"><strong>Next step:</strong> ${safe(data.remediation)}</p>` : '';
        const finalSize = data && data.total_size_after;
        const after = typeof finalSize === 'number' && Number.isFinite(finalSize) && finalSize >= 0
            ? `<p class="body-small"><strong>Final size:</strong> ${safe(compactSize(finalSize))}</p>`
            : '<p class="form-error">Final size could not be verified.</p>';
        const applied = data && typeof data.files_applied_before_failure === 'number'
            ? `<p class="body-small"><strong>Files changed before failure:</strong> ${safe(data.files_applied_before_failure)}</p>` : '';
        const phase = data && data.failed_phase ? `<p class="body-small"><strong>Failed during:</strong> ${safe(data.failed_phase)}</p>` : '';
        const rollback = data && data.rollback_outcome ? `<p class="body-small"><strong>Rollback result:</strong> ${safe(data.rollback_outcome)}</p>` : '';
        const mutation = data && data.apply_may_have_mutated === true
            ? '<p class="form-error"><strong>Bank content may have changed.</strong></p>' : '';
        const failures = data && Array.isArray(data.failures) ? data.failures : [];
        const rows = compactFailureRows(failures);
        const failureTable = rows ? `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">File</th><th scope="col">Safe failure</th><th scope="col">Target resolution</th></tr></thead><tbody>${rows}</tbody></table></div>` : '';
        const seenFailures = new Set();
        const fileReports = data && Array.isArray(data.files) ? data.files : [];
        const visibleFailures = [...failures, ...fileReports].filter(file => {
            if (!file || typeof file.filename !== 'string' || typeof file.error !== 'string') return false;
            const key = JSON.stringify([file.filename, file.error]);
            if (seenFailures.has(key)) return false;
            seenFailures.add(key);
            return true;
        }).map(file => `<li><strong>${safe(file.filename)}</strong>: ${safe(file.error)}</li>`).join('');
        const fileSummary = visibleFailures ? `<ul class="compaction-failures">${visibleFailures}</ul>` : '';
        const details = `<details class="compaction-details"><summary>Technical details</summary>${failureTable}${compactEvidence(data || {})}</details>`;
        const recovery = recoveryRequired ? '<p class="form-hint">Recovery is required. No automatic retry was attempted.</p>' : '';
        return `<div class="state ${recoveryRequired ? 'state-degraded' : 'state-error'}" role="${recoveryRequired ? 'status' : 'alert'}">${icon('alert')}<div><h3>${heading}</h3>${reason}${after}${phase}${rollback}${applied}${mutation}${message}${remediation}${fileSummary}${recovery}${details}</div></div>`;
    }

    function renderCompactionAction(view) {
        if (!hasPermission(view, 'manage')) return '';
        const disabled = view.compacting || view.midLoading;
        const label = view.compacting ? (view.compactApplying ? 'Compacting files…' : 'Checking bank files…') : 'Check files for compaction';
        const title = view.midLoading ? 'Wait for the Memory Bank to finish loading' : 'Check file sizes without changing bank content';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-compact-dry"${disabled ? ' disabled' : ''} title="${safe(title)}">${icon('maintenance')}${safe(label)}</button>`;
    }

    function renderCompactionReport(view) {
        const data = view.compactResult;
        if (!data) return '';
        if (data.status === 'loading') {
            return `<div id="sdCompactionResults" class="sd-compaction-results">${panel(stateLoading(view.compactApplying ? 'Compacting files…' : 'Checking bank files…'))}</div>`;
        }
        const applied = data.status === 'ok' && data.dry_run === false;
        const ready = data.status === 'ok' && data.dry_run !== false && view.compactDry === data && !view.compacting;
        const heading = applied ? 'Compaction applied' : data.status === 'ok' ? 'Files eligible for compaction' : 'Compaction result';
        const apply = ready
            ? `<button type="button" class="btn btn-primary btn-sm" data-action="sd-confirm-compact">Compact files…</button>`
            : '';
        const body = data.status === 'ok' ? compactSuccessMarkup(data) : compactFailureMarkup(data);
        return `<div id="sdCompactionResults" class="sd-compaction-results"><div class="sd-compaction-header"><div><span class="micro-label">Manual maintenance</span><h3>${heading}</h3></div>${apply}</div>${body}</div>`;
    }

    function graphStatsSection(graphStats, reachable) {
        const prototype = graphStats && typeof graphStats === 'object' ? Object.getPrototypeOf(graphStats) : undefined;
        const isRecord = prototype === null || (prototype && Object.getPrototypeOf(prototype) === null);
        if (reachable !== true || !isRecord) return stateUnavailable('Long statistics are unavailable.');
        const stats = graphStats;
        const metric = value => Number.isSafeInteger(value) && value >= 0 ? safe(value) : 'Unavailable';
        const typeCounts = stats.entity_types && typeof stats.entity_types === 'object' && !Array.isArray(stats.entity_types)
            ? Object.entries(stats.entity_types)
            : null;
        const distribution = typeCounts === null
            ? '<p class="form-hint">Entity type distribution is not reported.</p>'
            : typeCounts.length === 0
                ? '<p class="form-hint">No entity type counts reported.</p>'
                : `<ul class="sd-entity-types">${typeCounts.map(([name, count]) => `<li><span>${safe(name)}</span><strong>${Number.isSafeInteger(count) && count >= 0 ? safe(count) : 'Unavailable'}</strong></li>`).join('')}</ul>`;
        return `<section class="sd-long-volume" aria-label="LONG graph volumes and entity types">
            <div class="metric-grid sd-long-metrics">
                <div class="metric-card"><span class="micro-label">Graph documents</span><div class="metric-value">${metric(stats.document_count)}</div></div>
                <div class="metric-card"><span class="micro-label">Entities</span><div class="metric-value">${metric(stats.entity_count)}</div></div>
                <div class="metric-card"><span class="micro-label">Relations</span><div class="metric-value">${metric(stats.relation_count)}</div></div>
            </div>
            <div class="sd-long-entity-types"><h3>Entities by type</h3>${distribution}</div>
        </section>`;
    }

    function renderGraphViewer(graph) {
        if (!graph) return stateLoading('Loading graph…');
        if (graph.status !== 'ok') return stateUnavailable(graph.message || 'Graph data is unavailable.');
        const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
        const edges = Array.isArray(graph.edges) ? graph.edges : [];
        if (!nodes.length) return `<section class="sd-graph sd-graph-empty">${stateEmpty({
            title: 'No nodes in this graph view',
            hint: 'Explore your documents or check LONG status in Overview.',
            actionHtml: '<div class="sd-graph-controls"><button type="button" class="btn btn-secondary" data-action="sd-long-panel" data-panel="documents">Open Documents</button><button type="button" class="btn btn-secondary" data-action="sd-long-panel" data-panel="overview">Open Overview</button></div>',
        })}</section>`;
        const total = value => Number.isSafeInteger(value) && value >= 0 ? safe(value) : 'unknown';
        return `<section class="sd-graph" aria-labelledby="sdGraphTitle">
            <div class="sd-graph-toolbar"><div><h3 id="sdGraphTitle">Graph explorer</h3><p class="sd-graph-counts mono-data">${nodes.length} / ${total(graph.total_node_count)} nodes · ${edges.length} / ${total(graph.total_edge_count)} relations</p></div>
                <div class="sd-graph-controls"><button type="button" class="btn btn-secondary" id="sdGraphAll">Show all nodes</button><button type="button" class="btn btn-secondary" id="sdGraphFit">Fit graph</button><button type="button" class="btn btn-secondary" id="sdGraphZoomOut" aria-label="Zoom out">−</button><button type="button" class="btn btn-secondary" id="sdGraphZoomIn" aria-label="Zoom in">+</button></div></div>
            <p class="form-hint">${graph.truncated ? 'Bounded preview. More nodes or relations exist outside this snapshot.' : 'Received graph snapshot.'} Relations and sources below describe only the received projection.</p>
            <label for="sdGraphSearch">Find a node in this snapshot</label><input id="sdGraphSearch" class="form-input" type="search" placeholder="Search by name or type…" aria-controls="sdGraphResults">
            <div id="sdGraphResults" class="sd-graph-results" aria-live="polite"></div>
            <div class="sd-graph-legend"><span><i class="sd-graph-key is-entity"></i>Entity</span><span><i class="sd-graph-key is-document"></i>Document</span><span>Dashed line: MENTIONS</span><span id="sdGraphMode">All received nodes</span></div>
            <div class="sd-graph-stage"><svg id="sdGraphCanvas" role="group" aria-label="Knowledge graph; search to inspect nodes"><g id="sdGraphViewport"></g></svg><aside id="sdGraphDetails" class="sd-graph-details" aria-label="Node details"><h4 tabindex="-1">Node details</h4><p>Find or select a node to inspect its relations and sources.</p></aside></div>
            <p class="form-hint">Drag to pan. Use zoom controls, then Fit graph. Dense views can have overlapping nodes and fewer labels. Search or select a node to read its full name and relations.</p>
        </section>`;
    }

    function mountLongGraph(view) {
        view.graphCleanup?.();
        const graph = view.longData && view.longData.graph_view;
        const svg = document.getElementById('sdGraphCanvas');
        const viewport = document.getElementById('sdGraphViewport');
        if (!svg || !viewport || !graph || graph.status !== 'ok') return;
        const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
        const edges = Array.isArray(graph.edges) ? graph.edges : [];
        const byId = new Map(nodes.map(node => [node.id, node]));
        const svgNs = 'http://www.w3.org/2000/svg';
        const labelLink = document.createElementNS(svgNs, 'line');
        labelLink.setAttribute('class', 'sd-graph-label-link'); labelLink.setAttribute('aria-hidden', 'true');
        labelLink.style.display = 'none'; svg.insertBefore(labelLink, viewport);
        const selectedCaption = document.createElementNS(svgNs, 'rect');
        selectedCaption.setAttribute('class', 'sd-graph-selected-caption'); selectedCaption.setAttribute('aria-hidden', 'true');
        selectedCaption.setAttribute('rx', '4');
        selectedCaption.style.display = 'none'; svg.insertBefore(selectedCaption, viewport);
        const owned = () => longOwned(view) && svg.isConnected && view.longData.graph_view === graph;
        const name = node => String(node.filename || node.label || 'Untitled');
        const incident = id => edges.filter(edge => edge.from === id || edge.to === id);
        const groups = new Map(), labelWidths = new Map(), labelNames = new Map(), lines = [];
        const salient = new Set([...nodes].sort((a, b) => Number(b.mentions || 0) - Number(a.mentions || 0)).slice(0, 3).map(node => node.id));
        let positions = new Map(), visible = new Set(byId.keys()), selected = null, hovered = null;
        const history = []; // Synthetic IDs belong only to this projection/mount.
        let transform = { x: 0, y: 0, scale: 1 }, width = 1, height = 1;

        // Deterministic bounded force layout: edges attract related nodes, all nodes repel.
        const overview = nodes.map((node, i) => {
            const angle = i * 2.3999632297, radius = 70 * Math.sqrt(i + 1);
            return { id: node.id, x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
        });
        const pointById = new Map(overview.map(point => [point.id, point]));
        for (let step = 0; step < 100; step++) {
            const force = new Map(overview.map(point => [point.id, { x: 0, y: 0 }]));
            for (let i = 0; i < overview.length; i++) for (let j = 0; j < i; j++) {
                const a = overview[i], b = overview[j], dx = a.x - b.x, dy = a.y - b.y;
                const distance = Math.max(1, Math.hypot(dx, dy)), push = Math.min(16, 40000 / (distance * distance));
                const x = dx / distance * push, y = dy / distance * push;
                force.get(a.id).x += x; force.get(a.id).y += y;
                force.get(b.id).x -= x; force.get(b.id).y -= y;
            }
            edges.forEach(edge => {
                const a = pointById.get(edge.from), b = pointById.get(edge.to);
                if (!a || !b) return;
                const dx = b.x - a.x, dy = b.y - a.y, distance = Math.max(1, Math.hypot(dx, dy));
                const pull = (distance - 150) * .012;
                force.get(a.id).x += dx / distance * pull; force.get(a.id).y += dy / distance * pull;
                force.get(b.id).x -= dx / distance * pull; force.get(b.id).y -= dy / distance * pull;
            });
            overview.forEach(point => {
                point.x += Math.max(-20, Math.min(20, force.get(point.id).x - point.x * .006));
                point.y += Math.max(-20, Math.min(20, force.get(point.id).y - point.y * .006));
            });
        }

        function layout() {
            positions = new Map(overview.map(point => [point.id, { x: point.x, y: point.y }]));
            visible = new Set(byId.keys());
            if (selected !== null) {
                visible = new Set([selected]);
                incident(selected).forEach(edge => { if (byId.has(edge.from)) visible.add(edge.from); if (byId.has(edge.to)) visible.add(edge.to); });
                positions.set(selected, { x: 0, y: 0 });
                const neighbors = [...visible].filter(id => id !== selected);
                const radius = Math.max(220, neighbors.length * 180 / (2 * Math.PI));
                neighbors.forEach((id, i) => {
                    const angle = -Math.PI / 2 + i * 2 * Math.PI / neighbors.length;
                    positions.set(id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
                });
            }
            groups.forEach((group, id) => {
                const point = positions.get(id);
                group.setAttribute('transform', `translate(${point.x} ${point.y})`);
                group.classList.toggle('is-hidden', !visible.has(id));
                group.classList.toggle('is-selected', id === selected);
                group.setAttribute('aria-pressed', String(id === selected));
                group.setAttribute('tabindex', visible.has(id) ? '0' : '-1');
            });
            lines.forEach(({ line, edge }) => {
                const a = positions.get(edge.from), b = positions.get(edge.to);
                const shown = a && b && visible.has(edge.from) && visible.has(edge.to);
                line.classList.toggle('is-hidden', !shown);
                if (!shown) return;
                line.setAttribute('x1', a.x); line.setAttribute('y1', a.y);
                line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
            });
            document.getElementById('sdGraphMode').textContent = selected === null ? 'All received nodes' : `Neighborhood · ${visible.size} received nodes`;
            // Raise primary groups only on selection/layout, never mid-pointer gesture.
            const primary = selected === null ? [...salient].reverse() : [selected];
            primary.forEach(id => viewport.appendChild(groups.get(id)));
        }

        function measureLabels() {
            if (!owned()) return;
            // Measure at 13px outside the scaled/hidden node viewport, including
            // the optional late-font pass. Never change live label geometry here.
            const metrics = document.createElementNS(svgNs, 'g');
            metrics.setAttribute('opacity', '0'); metrics.setAttribute('aria-hidden', 'true');
            const samples = [...groups].map(([id, group]) => {
                const label = group.querySelector('text').cloneNode(true);
                label.textContent = labelNames.get(id);
                label.style.display = 'block'; label.setAttribute('font-size', '13');
                label.setAttribute('x', '0'); label.setAttribute('y', '0');
                metrics.appendChild(label); return [id, label];
            });
            svg.appendChild(metrics);
            samples.forEach(([id, label]) => labelWidths.set(id, label.getComputedTextLength() + 8));
            metrics.remove();
        }

        function labels() {
            labelLink.style.display = 'none'; labelLink.dataset.node = '';
            selectedCaption.style.display = 'none'; selectedCaption.dataset.node = '';
            const occupied = [], scale = transform.scale;
            const markers = [...visible].map(id => {
                const point = positions.get(id);
                return { id, x: point.x * scale + transform.x - 10, y: point.y * scale + transform.y - 12, w: 20, h: 24 };
            });
            const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
            const ranked = [...groups].sort(([a], [b]) => {
                const priority = id => id === selected ? 4 : id === hovered ? 3 : 0;
                return priority(b) - priority(a) || Number(byId.get(b).mentions || 0) - Number(byId.get(a).mentions || 0);
            });
            let count = 0;
            ranked.forEach(([id, group]) => {
                const label = group.querySelector('text'), point = positions.get(id);
                label.style.display = 'none';
                if (label.textContent !== labelNames.get(id)) label.textContent = labelNames.get(id);
                if (!visible.has(id) || (selected === null && count >= 12 && id !== hovered)) return;
                const x = point.x * scale + transform.x, y = point.y * scale + transform.y;
                // A floating caption cannot name a node outside the canvas.
                if (x < 10 || x > width - 10 || y < 12 || y > height - 12) return;
                const w = labelWidths.get(id), h = 20;
                const primary = id === selected || id === hovered || (selected === null && salient.has(id));
                const candidates = [{ x: x + 24, y: y - 11, w, h }];
                if (primary) {
                    candidates.push({ x: x - 24 - w, y: y - 11, w, h }, { x: x - w / 2, y: y - 36, w, h }, { x: x - w / 2, y: y + 24, w, h });
                }
                if (id === selected) {
                    // Only selected text may move away, with an explicit visual link.
                    // Eight ASCII letters at 13px, with a full em reserved per letter.
                    const captionWidth = 104, captionHeight = 20;
                    candidates.push(...candidates.map(box => ({ linked: true, w: captionWidth, h: captionHeight, x: Math.max(4, Math.min(width - captionWidth - 4, box.x)), y: Math.max(4, Math.min(height - captionHeight - 4, box.y)) })));
                    for (const edgeY of [4, height - captionHeight - 4]) {
                        for (const edgeX of [(width - captionWidth) / 2, 4, width - captionWidth - 4]) candidates.push({ x: edgeX, y: edgeY, w: captionWidth, h: captionHeight, linked: true });
                    }
                }
                const box = candidates.find(box => box.x >= 0 && box.y >= 0 && box.x + box.w <= width && box.y + box.h <= height &&
                    !occupied.some(b => overlaps(box, b)) && !markers.some(marker => overlaps(box, marker)));
                if (!box) return;
                label.setAttribute('font-size', String(13 / scale));
                label.setAttribute('x', String((box.x + (box.linked ? 8 : 0) - x) / scale)); label.setAttribute('y', String((box.y + 15 - y) / scale));
                label.style.display = 'block'; occupied.push(box); count++;
                if (box.linked) {
                    selectedCaption.dataset.node = id;
                    selectedCaption.setAttribute('x', box.x); selectedCaption.setAttribute('y', box.y);
                    selectedCaption.setAttribute('width', box.w); selectedCaption.setAttribute('height', box.h);
                    label.textContent = 'Selected';
                    selectedCaption.style.display = 'block'; labelLink.dataset.node = id;
                    labelLink.setAttribute('x1', x); labelLink.setAttribute('y1', y);
                    labelLink.setAttribute('x2', Math.max(box.x + 8, Math.min(box.x + 72, x)));
                    labelLink.setAttribute('y2', Math.max(box.y + 2, Math.min(box.y + 18, y)));
                    labelLink.style.display = 'block';
                }
            });
        }

        function applyTransform() {
            viewport.setAttribute('transform', `translate(${transform.x} ${transform.y}) scale(${transform.scale})`);
            // Node markers/hit targets stay usable even at overview scale.
            groups.forEach(group => {
                group.querySelector('.sd-graph-marker').setAttribute('transform', `scale(${1 / transform.scale})`);
            });
            labels();
        }
        function fit() {
            if (!owned()) return;
            const points = [...visible].map(id => positions.get(id));
            const xs = points.map(point => point.x), ys = points.map(point => point.y);
            const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
            // Pixel margin reserves space for labels; the scale fits actual visible positions.
            const scale = Math.max(.02, Math.min(2, (width - Math.min(220, width * .4)) / Math.max(1, maxX - minX), (height - 90) / Math.max(1, maxY - minY)));
            transform = { x: width / 2 - (minX + maxX) / 2 * scale, y: height / 2 - (minY + maxY) / 2 * scale, scale };
            applyTransform();
        }
        function zoom(factor) {
            if (!owned()) return;
            const next = Math.max(.02, Math.min(5, transform.scale * factor)), ratio = next / transform.scale;
            transform = { x: width / 2 + (transform.x - width / 2) * ratio, y: height / 2 + (transform.y - height / 2) * ratio, scale: next };
            applyTransform();
        }

        function showDetails() {
            const details = document.getElementById('sdGraphDetails'), node = byId.get(selected);
            if (!node) {
                details.innerHTML = '<h4 tabindex="-1">Node details</h4><p>Find or select a node to inspect its relations and sources.</p>';
                return;
            }
            const related = incident(selected);
            const relationRows = (list, direction) => list.map(edge => {
                const neighbor = byId.get(direction === 'incoming' ? edge.from : edge.to);
                return `<li><span class="mono-data">${safe(edge.type || 'Relation')}</span>${neighbor ? `<button type="button" class="btn btn-ghost sd-graph-neighbor" data-node="${safe(neighbor.id)}" aria-label="Inspect ${safe(name(neighbor))}">${safe(name(neighbor))}</button>` : '<span>Neighbor not in this snapshot</span>'}</li>`;
            }).join('') || '<li>No relations received in this direction.</li>';
            const incoming = related.filter(edge => edge.to === selected), outgoing = related.filter(edge => edge.from === selected);
            const sources = [...new Set(related.flatMap(edge => [edge.from, edge.to]))].map(id => byId.get(id)).filter(candidate => candidate && candidate.id !== selected && candidate.node_type === 'document');
            details.innerHTML = `<div class="sd-graph-controls"><button type="button" class="btn btn-secondary" id="sdGraphBack" ${history.length ? '' : 'disabled'}>Back</button><button type="button" class="btn btn-secondary" id="sdGraphClose">Close</button></div>
                <p class="mono-data">${safe(node.node_type === 'document' ? 'DOCUMENT' : node.type || 'ENTITY')}</p><h4 tabindex="-1">${safe(name(node))}</h4><p>${safe(node.description || 'No description returned.')}</p><span class="count-pill">${safe(node.mentions ?? 'unknown')} mentions</span>
                <h5>Outgoing relations · ${outgoing.length}</h5><ul class="sd-graph-relations">${relationRows(outgoing, 'outgoing')}</ul>
                <h5>Incoming relations · ${incoming.length}</h5><ul class="sd-graph-relations">${relationRows(incoming, 'incoming')}</ul>
                <h5>Source documents · ${sources.length}</h5><ul id="sdGraphSources" class="sd-graph-relations">${sources.map(source => `<li><button type="button" class="btn btn-ghost sd-graph-neighbor" data-node="${safe(source.id)}">${safe(name(source))}</button></li>`).join('') || '<li>No source document received for this node.</li>'}</ul>
                ${node.node_type === 'document' ? '<p class="form-hint">Document reader identity is not returned in this projection. This snapshot ID cannot open the Documents reader.</p>' : ''}
                <p class="form-hint">Relations within this snapshot.${graph.truncated ? ' This preview is truncated; the lists are not exhaustive.' : ''}</p>`;
            details.querySelectorAll('[data-node]').forEach(button => button.addEventListener('click', () => select(button.dataset.node)));
            details.querySelector('#sdGraphBack').addEventListener('click', () => {
                if (!owned() || !history.length) return;
                selected = history.pop(); layout(); fit(); showDetails(); focusDetails();
            });
            details.querySelector('#sdGraphClose').addEventListener('click', clear);
        }
        function focusDetails() { document.querySelector('#sdGraphDetails h4')?.focus(); }
        function select(id) {
            if (!owned() || !byId.has(id)) return;
            if (selected !== null && selected !== id) history.push(selected);
            selected = id; hovered = null; layout(); fit(); showDetails(); focusDetails();
        }
        function clear() {
            if (!owned()) return;
            selected = null; history.length = 0; hovered = null;
            layout(); fit(); showDetails(); document.getElementById('sdGraphSearch').focus();
        }

        edges.forEach(edge => {
            const line = document.createElementNS(svgNs, 'line');
            line.setAttribute('class', `sd-graph-edge${edge.type === 'MENTIONS' ? ' is-mention' : ''}`);
            const title = document.createElementNS(svgNs, 'title'); title.textContent = edge.type || 'Relation';
            line.appendChild(title); viewport.appendChild(line); lines.push({ line, edge });
        });
        nodes.forEach(node => {
            const group = document.createElementNS(svgNs, 'g');
            group.setAttribute('class', `sd-graph-node is-${node.node_type === 'document' ? 'document' : 'entity'}`);
            group.setAttribute('role', 'button'); group.setAttribute('aria-label', `Inspect ${name(node)}`);
            group.dataset.node = node.id;
            const marker = document.createElementNS(svgNs, 'g'); marker.setAttribute('class', 'sd-graph-marker');
            const hit = document.createElementNS(svgNs, 'circle'); hit.setAttribute('r', '22'); hit.setAttribute('class', 'sd-graph-hit');
            marker.appendChild(hit);
            const shape = document.createElementNS(svgNs, node.node_type === 'document' ? 'rect' : 'circle');
            shape.setAttribute('class', 'sd-graph-shape');
            if (node.node_type === 'document') {
                shape.setAttribute('x', '-8'); shape.setAttribute('y', '-10'); shape.setAttribute('width', '16'); shape.setAttribute('height', '20'); shape.setAttribute('rx', '2');
            } else shape.setAttribute('r', '8');
            marker.appendChild(shape); group.appendChild(marker);
            const label = document.createElementNS(svgNs, 'text'); label.setAttribute('class', 'sd-graph-label');
            const fullName = name(node); label.textContent = fullName.length > 26 ? fullName.slice(0, 25) + '…' : fullName;
            labelNames.set(node.id, label.textContent);
            const title = document.createElementNS(svgNs, 'title'); title.textContent = fullName;
            group.append(label, title);
            group.addEventListener('click', () => select(node.id));
            group.addEventListener('keydown', event => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault(); select(node.id);
            });
            group.addEventListener('mouseenter', () => { if (owned()) { hovered = node.id; labels(); } });
            group.addEventListener('mouseleave', () => { if (owned()) { hovered = null; labels(); } });
            group.addEventListener('focus', () => { if (owned()) { hovered = node.id; labels(); } });
            group.addEventListener('blur', () => { if (owned()) { hovered = null; labels(); } });
            viewport.appendChild(group); groups.set(node.id, group);
        });
        document.getElementById('sdGraphAll').addEventListener('click', clear);
        document.getElementById('sdGraphFit').addEventListener('click', fit);
        document.getElementById('sdGraphZoomIn').addEventListener('click', () => zoom(1.35));
        document.getElementById('sdGraphZoomOut').addEventListener('click', () => zoom(1 / 1.35));
        svg.addEventListener('wheel', event => { if (!owned()) return; event.preventDefault(); zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15); }, { passive: false });
        let drag = null;
        svg.addEventListener('pointerdown', event => {
            if (!owned() || event.target.closest?.('.sd-graph-node') || !event.isPrimary) return;
            drag = { x: event.clientX, y: event.clientY, ox: transform.x, oy: transform.y };
            svg.setPointerCapture(event.pointerId);
        });
        svg.addEventListener('pointermove', event => {
            if (!owned() || !drag) return;
            const rect = svg.getBoundingClientRect();
            transform.x = drag.ox + (event.clientX - drag.x) * width / rect.width;
            transform.y = drag.oy + (event.clientY - drag.y) * height / rect.height; applyTransform();
        });
        ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => svg.addEventListener(type, () => { drag = null; }));
        const search = document.getElementById('sdGraphSearch'), results = document.getElementById('sdGraphResults');
        function searchResults() {
            if (!owned()) return;
            const query = search.value.trim().toLowerCase();
            const matches = query ? nodes.filter(node => `${name(node)} ${node.type || ''}`.toLowerCase().includes(query)) : [];
            if (query && !matches.length) {
                clear();
                results.innerHTML = stateEmpty({ compact: true,
                    title: 'No matching nodes in this graph view',
                    hint: 'Try another name or type, or clear the search.',
                    actionHtml: '<button type="button" class="btn btn-secondary" id="sdGraphClearSearch">Clear search</button>',
                });
                results.querySelector('#sdGraphClearSearch').addEventListener('click', () => {
                    if (!owned()) return;
                    search.value = ''; searchResults(); search.focus();
                });
                return;
            }
            results.innerHTML = query ? `<p class="form-hint">${matches.length} matches in this snapshot${matches.length > 20 ? ' · first 20 shown, refine your search' : ''}</p>${matches.slice(0, 20).map(node => `<button type="button" class="btn btn-secondary" data-node="${safe(node.id)}">${safe(name(node))}</button>`).join('')}` : '';
            results.querySelectorAll('[data-node]').forEach(button => button.addEventListener('click', () => select(button.dataset.node)));
        }
        search.addEventListener('input', searchResults);
        search.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); results.querySelector('[data-node]')?.click(); } });
        function resize() {
            if (!owned()) { cleanup(); return; }
            width = svg.clientWidth || 1; height = svg.clientHeight || 1;
            svg.setAttribute('viewBox', `0 0 ${width} ${height}`); fit();
        }
        const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
        function cleanup() { observer?.disconnect(); window.removeEventListener('hashchange', cleanup); }
        view.graphCleanup = cleanup;
        window.addEventListener('hashchange', cleanup, { once: true });
        measureLabels();
        if (document.fonts?.status === 'loading') document.fonts.ready.then(() => {
            if (!owned()) return;
            measureLabels(); applyTransform();
        });
        layout(); resize(); observer?.observe(svg);
    }

    function renderLong(view) {
        const names = { overview: 'Overview', ontology: 'Ontology', documents: 'Documents', jobs: 'Ingestion jobs', graph: 'Graph' };
        const tabs = Object.entries(names).map(([key, label]) => `<button type="button" class="sd-tier-tab${view.longPanel === key ? ' active' : ''}" role="tab" id="sdLongTab-${key}" aria-controls="sdLongBody" aria-selected="${view.longPanel === key}" tabindex="${view.longPanel === key ? 0 : -1}" data-action="sd-long-panel" data-panel="${key}">${label}</button>`).join('');
        let body = '<div id="sdLongStatus"></div><div id="sdLongSnapshot"></div>';
        if (view.longPanel === 'ontology') body = `<div id="sdLongStatus"></div><div id="sdOntologyConfig"></div><div id="sdOntologyFreshness"></div><div class="sd-long-controls"><label for="sdOntologySelect">Available definitions</label><select id="sdOntologySelect" class="form-input"><option value="">Choose an ontology</option></select><button class="btn btn-secondary" data-action="sd-ontology-load">Load definition</button></div><div id="sdOntologyDefinitionFreshness"></div><div id="sdOntologyDefinition" class="sd-reader" tabindex="0"></div><button class="btn btn-secondary" data-action="sd-ontology-validate">Validate loaded YAML</button><div id="sdOntologyValidation"></div>`;
        if (view.longPanel === 'documents') body = `<div class="sd-long-controls sd-document-controls"><label for="sdDocumentQuery">Filename or source path<input id="sdDocumentQuery" class="form-input" type="search"></label><label for="sdDocumentStatus">Status<select id="sdDocumentStatus" class="form-input"><option value="">Any status</option>${['running', 'succeeded', 'deprecated', 'cleanup_pending'].map(status => `<option value="${status}">${documentStatusLabel(status)}</option>`).join('')}</select></label><button class="btn btn-secondary" data-action="sd-documents-apply">Apply filters</button></div><div id="sdDocumentsFreshness"></div><div id="sdDocumentsWorkspace" class="sd-documents-workspace"><div class="sd-documents-catalog"><div id="sdDocumentsList" class="sd-documents-list"></div><div id="sdDocumentsPagination" class="sd-long-controls sd-document-controls sd-document-pagination" hidden><button class="btn btn-secondary" data-action="sd-documents-page" data-step="-1" disabled>Previous</button><span id="sdDocumentsPage"></span><button class="btn btn-secondary" data-action="sd-documents-page" data-step="1" disabled>Next</button></div></div><div id="sdDocumentHost"></div></div>`;
        if (view.longPanel === 'jobs') body = `<div class="sd-ingest-scope" role="group" aria-label="Ingestion source">${[['documents', 'Space documents'], ['archive', 'MID capture archive']].map(([scope, label]) => `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-ingest-scope" data-scope="${scope}" aria-pressed="${(scope === 'archive') === view.ingestions.archive}">${label}</button>`).join('')}</div><p id="sdIngestScopeHint" class="form-hint"></p><div class="sd-long-controls"><label for="sdIngestStatus">Status<select id="sdIngestStatus" class="form-input"><option value="">Any status</option>${['queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped'].map(status => `<option value="${status}">${safe(ingestStatusLabel(status))}</option>`).join('')}</select></label><label for="sdIngestBatch">Batch<input id="sdIngestBatch" class="form-input"></label><button class="btn btn-secondary" data-action="sd-ingest-apply">Apply filters</button></div><div id="sdIngestFreshness"></div><div id="sdIngestCancelResult" role="status"></div><div id="sdIngestWorkspace" class="sd-ingest-workspace"><div class="sd-ingest-history"><div id="sdIngestList"></div><div id="sdIngestPager" class="sd-long-controls" aria-label="Ingestion pages" hidden><button id="sdIngestPrevious" class="btn btn-secondary" data-action="sd-ingest-page" data-step="-1" disabled>Previous</button><span id="sdIngestPage"></span><button id="sdIngestNext" class="btn btn-secondary" data-action="sd-ingest-page" data-step="1" disabled>Next</button></div></div><article id="sdIngestDetail" class="sd-ingest-detail" tabindex="-1" aria-label="Selected ingestion job" hidden></article></div>`;
        return `<div class="item-card tier-long sd-tier-card sd-long-tier-card"><div class="panel-header"><h2>Long memory</h2><button class="btn btn-secondary" data-action="sd-long-refresh">Refresh</button></div><div id="sdLongTabs" class="sd-tier-tabs" role="tablist" aria-label="Long memory panels">${tabs}</div><div id="sdLongBody" role="tabpanel" tabindex="0" aria-labelledby="sdLongTab-${view.longPanel}">${body}</div></div>`;
    }

    function longOwned(view) {
        return guarded(view, view.ctx.epoch) && sessionGenerationIsCurrent(view.ctx.sessionGeneration);
    }

    function paintLongRegion(id, html) {
        const node = document.getElementById(id);
        if (!node || node.longMarkup === html) return;
        const active = document.activeElement, action = node.contains(active) ? active?.dataset.action : null;
        const identity = action ? JSON.stringify(active.dataset) : null;
        const top = node.scrollTop, left = node.scrollLeft;
        node.innerHTML = html; node.longMarkup = html;
        if (action) [...node.querySelectorAll('[data-action]')].find(item => JSON.stringify(item.dataset) === identity)?.focus({ preventScroll: true });
        node.scrollTop = top; node.scrollLeft = left;
    }

    function paintLong(view) {
        const p = view.longPanel;
        if (p === 'overview' || p === 'graph' || p === 'ontology') memoryFreshness('sdLongStatus', view.longSuccess, view.longError);
        if (p === 'overview' || p === 'graph') {
            const data = view.longData;
            if (!data) paintLongRegion('sdLongSnapshot', view.longFailure ? renderLongConnection(view, view.longFailure) : stateLoading('Loading long memory…'));
            else if (p === 'overview') paintLongRegion('sdLongSnapshot', renderLongData(view, data));
            else if (view.longGraphPainted !== data) {
                const target = document.getElementById('sdLongSnapshot');
                if (target) target.innerHTML = data.status === 'ok' && data.connected && data.reachable !== false ? renderGraphViewer(data.graph_view) : renderLongConnection(view, data);
                mountLongGraph(view); view.longGraphPainted = data;
            }
        } else if (p === 'ontology') {
            const state = view.ontology;
            const configured = view.longData?.config?.ontology;
            paintLongRegion('sdOntologyConfig', !view.longData ? (view.longFailure ? renderLongConnection(view, view.longFailure) : stateLoading('Loading long status…')) : view.longData.status !== 'ok' ? renderLongConnection(view, view.longData) : configured ? `<p>Configured ontology: <code>${safe(configured)}</code></p><p class="form-hint">Available definitions below are a catalog, not a proof of the effective graph schema.</p>` : 'Configured ontology is not reported.');
            memoryFreshness('sdOntologyFreshness', state.success, state.error);
            const select = document.getElementById('sdOntologySelect');
            if (select && state.catalog && state.catalog !== state.paintedCatalog) {
                const selected = select.value;
                select.innerHTML = '<option value="">Choose an ontology</option>' + state.catalog.ontologies.map(item => `<option value="${safe(item.name)}">${safe(item.name)}</option>`).join('');
                if (state.catalog.ontologies.some(item => item.name === selected)) select.value = selected;
                state.paintedCatalog = state.catalog;
            }
            memoryFreshness('sdOntologyDefinitionFreshness', state.definitionSuccess, state.definitionError);
            const definition = state.definition;
            paintLongRegion('sdOntologyDefinition', definition ? `<h3>${safe(definition.name)}</h3><p>${safe(definition.description)}</p><p>Version ${safe(definition.version)} · ${safe(definition.entity_types_count ?? '—')} entity types · ${safe(definition.relation_types_count ?? '—')} relation types</p><pre class="mono-block">${safe(definition.content)}</pre>` : 'Select and load an available definition.');
            paintLongRegion('sdOntologyValidation', state.validation ? `<p role="status">${state.validation.valid === true ? 'Valid structure' : 'Validation did not confirm a valid structure'} · ${renderTimestamp(state.validationAt)}</p>${serverMessage(state.validation.message || '')}${(Array.isArray(state.validation.errors) ? state.validation.errors : []).map(error => `<p>${safe(error)}</p>`).join('')}<p class="form-hint">Structure validation does not measure classification quality.</p>` : '');
        } else if (p === 'documents') paintDocuments(view);
        else paintIngestions(view);
    }

    function documentKey(item) { return item.document_id ? `id:${item.document_id}` : `source:${item.source_path || ''}`; }
    function longFields(item, fields) {
        return fields.filter(([key]) => item[key] !== null && item[key] !== undefined && item[key] !== '').map(([key, label, timestamp]) => `<div class="sd-meta-row"><strong>${safe(label)}</strong><span>${timestamp ? renderTimestamp(item[key]) : safe(item[key])}</span></div>`).join('');
    }

    function longPagination(offset, count, total) {
        if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(count) || count < 0 || !Number.isSafeInteger(offset + count)) return safe('Pagination unavailable');
        return safe(`${count ? offset + 1 : 0}–${count ? offset + count : 0} / ${Number.isSafeInteger(total) && total >= 0 ? total : '—'}`);
    }

    function documentStatusLabel(status) {
        const labels = { running: 'Running', succeeded: 'Succeeded', deprecated: 'Deprecated', cleanup_pending: 'Cleanup pending', unknown: 'Unknown' };
        return Object.hasOwn(labels, status) ? labels[status] : status || 'Status unavailable';
    }

    function documentStatus(status) {
        const severity = status === 'succeeded' ? 'ok' : ['running', 'cleanup_pending'].includes(status) ? 'warn' : 'neutral';
        return statusDot(severity, documentStatusLabel(status));
    }

    function documentPageUsable(data) {
        return !!data && Number.isSafeInteger(data.offset) && data.offset >= 0
            && Number.isSafeInteger(data.count) && data.count > 0 && data.count <= 50
            && Number.isSafeInteger(data.limit) && data.limit === 50
            && Array.isArray(data.documents) && data.documents.length === data.count
            && Number.isSafeInteger(data.total_count) && data.total_count >= data.offset + data.count
            && Number.isSafeInteger(data.offset + data.count) && Number.isSafeInteger(data.offset + 50);
    }

    function documentFirstPageAvailable(data) {
        return !!data && Number.isSafeInteger(data.offset) && data.offset > 0
            && data.count === 0 && data.limit === 50
            && Array.isArray(data.documents) && data.documents.length === 0
            && Number.isSafeInteger(data.total_count) && data.total_count >= 0 && data.total_count <= data.offset;
    }

    function documentSize(value, formatted = false) {
        return Number.isFinite(value) && value >= 0 ? formatted ? fmtSize(value) : value : 'Size unavailable';
    }

    function documentInspectLabel(item) {
        const source = item.source_path || item.repo_path || item.document_id;
        return `Inspect ${item.filename || 'document'}${source ? `, source ${source}` : ''}${item.sha256 ? `, version ${item.sha256}` : ''}`;
    }

    function documentWarnings(payload, label) {
        return `${payload?.partial ? `<p class="state-degraded" role="status">Partial ${label} response.</p>` : ''}${(Array.isArray(payload?.warnings) ? payload.warnings : []).map(warning => `<p>${safe(typeof warning === 'string' ? warning : warning?.message || 'Some document data is unavailable.')}</p>`).join('')}`;
    }

    function documentEmpty(view) {
        const state = view.documents, data = state.list, filters = state.listFilters || {};
        const filtered = !!(filters.query || filters.status);
        const canClear = !!(state.filters.query || state.filters.status);
        const completeEmpty = !data.partial && data.offset === 0 && data.count === 0 && data.total_count === 0 && data.limit === 50;
        const firstPage = documentFirstPageAvailable(data);
        const title = state.error ? 'Previous result retained' : completeEmpty ? filtered ? 'No documents match these filters' : 'No documents in this catalog' : 'No documents returned';
        const hint = state.error ? 'The latest catalog read failed. This empty result is from the last successful read.'
            : firstPage ? `${data.partial ? 'Some documents may be unavailable. ' : ''}Return to the first page to check this catalog.`
            : data.partial ? 'Some documents may be unavailable. Try refreshing this view.'
            : completeEmpty ? filtered ? 'Try another filename, source path or status, or clear the filters.' : 'View the overview for this space’s long memory status.'
            : 'No documents on this page. Refresh this view to try again.';
        const actionHtml = `<div class="sd-document-empty-actions">${firstPage ? `<button type="button" class="btn btn-secondary" data-action="sd-documents-first" ${state.loading ? 'disabled' : ''}>Back to first page</button>` : ''}${canClear ? '<button type="button" class="btn btn-secondary" data-action="sd-documents-clear">Clear filters</button>' : ''}<a class="btn btn-secondary" href="#/spaces/${safe(encodeURIComponent(view.spaceId))}/long/overview">View long memory overview</a></div>`;
        return stateEmpty({ title, hint, actionHtml });
    }

    function documentReference(value, head = 32, tail = 20) {
        const text = String(value);
        const label = text.length > head + tail + 1 ? `${text.slice(0, head)}…${text.slice(-tail)}` : text;
        return `<span title="${safe(text)}">${copyable(text, label)}</span>`;
    }

    function documentInspectorShell() {
        return `<section id="sdDocumentInspector" class="sd-document-inspector" aria-labelledby="sdDocumentInspectorTitle"><div class="sd-document-heading"><h3 id="sdDocumentInspectorTitle" tabindex="-1">Document details</h3><button type="button" class="btn btn-secondary" data-action="sd-document-close" aria-label="Close document inspector">${icon('close')}</button></div><div id="sdDocumentSelectionNotice"></div><div id="sdDocumentFreshness"></div><article id="sdDocumentDetail" class="sd-document-detail" tabindex="0"></article><div id="sdDocumentContentFreshness"></div></section>`;
    }

    function closeDocument(view) {
        const state = view.documents, key = state.selected && documentKey(state.selected);
        if (state.modal) {
            closeModal();
            const modal = document.getElementById('adminModal');
            if (modal) modal.innerHTML = ''; // No hidden duplicate inspector IDs on the next open.
        }
        state.modal = false; state.selected = state.detail = state.content = null;
        state.detailSeq++; state.detailLoading = state.contentLoading = false;
        paintDocuments(view);
        if (key) ([...document.querySelectorAll('[data-action="sd-document-inspect"]')].find(button => button.dataset.key === key)
            || document.getElementById('sdDocumentQuery'))?.focus({ preventScroll: true });
    }

    function discardDocumentModal(view) {
        if (!view?.documents.modal) return;
        const modal = document.getElementById('adminModal');
        if (modal?.querySelector('#sdDocumentInspector')) {
            closeModal(); modal.innerHTML = '';
        }
        view.documents.modal = false; view.documents.selected = null; view.documents.detailSeq++;
    }

    function mountDocumentInspector(view) {
        const state = view.documents, host = document.getElementById('sdDocumentHost');
        const narrow = typeof window !== 'undefined' && window.matchMedia('(max-width: 1199px)').matches;
        const returningToDesktop = state.modal && !narrow;
        if (returningToDesktop) {
            closeModal();
            const modal = document.getElementById('adminModal');
            if (modal) modal.innerHTML = '';
            state.modal = false;
        }
        if (state.selected && narrow && !state.modal) {
            if (host) { host.innerHTML = ''; host.longMarkup = ''; }
            showModal('Document details', documentInspectorShell());
            state.modal = true;
            // Escape in the shared shell clicks this same close control.
            const close = document.getElementById('adminModal')?.querySelector('[data-action="close-modal"]');
            close?.addEventListener('click', event => {
                event.stopPropagation();
                if (longOwned(view)) closeDocument(view);
                else discardDocumentModal(view);
            });
        } else if (!state.modal) paintLongRegion('sdDocumentHost', state.selected ? documentInspectorShell() : '');
        document.getElementById('sdDocumentsWorkspace')?.classList.toggle('has-selection', !!state.selected && !state.modal);
        if (returningToDesktop) document.getElementById('sdDocumentInspectorTitle')?.focus({ preventScroll: true });
    }

    function paintDocuments(view) {
        const state = view.documents, data = state.list;
        memoryFreshness('sdDocumentsFreshness', state.success, state.error);
        const rows = data?.documents || [];
        const table = rows.length ? `<table class="data-table sd-documents-table"><caption class="sr-only">Document catalog — source and version metadata</caption><thead><tr>${['File / source path', 'Status', 'Version / ingested', 'Size / chunks', ''].map(label => `<th scope="col">${label}</th>`).join('')}</tr></thead><tbody>${rows.map(item => {
            const selected = !!state.selected && documentKey(state.selected) === documentKey(item);
            const source = item.source_path || item.repo_path || item.document_id;
            return `<tr class="${selected ? 'is-selected' : ''}" data-document-key="${safe(documentKey(item))}"><td><strong class="mono-data">${safe(item.filename || 'Unnamed document')}</strong>${source ? `<div class="sd-document-path mono-data">${documentReference(source)}</div>` : ''}</td><td>${documentStatus(item.ingestion_status)}</td><td><span class="sr-only">SHA-256 </span>${item.sha256 ? documentReference(item.sha256, 6, 4) : '<span class="text-faint">SHA-256 unavailable</span>'}${item.ingested_at ? `<div class="sd-document-date">${renderTimestamp(item.ingested_at)}</div>` : ''}</td><td>${item.size_bytes != null ? `<span>${safe(documentSize(item.size_bytes, true))}</span>` : ''}${item.chunk_count != null ? `<div>${safe(item.chunk_count)} chunks</div>` : ''}</td><td><button type="button" class="btn btn-secondary btn-sm" data-action="sd-document-inspect" data-key="${safe(documentKey(item))}" aria-label="${safe(documentInspectLabel(item))}" aria-expanded="${selected}" aria-controls="${selected ? 'sdDocumentInspector' : 'sdDocumentHost'}" ${!item.document_id && !item.source_path ? 'disabled' : ''}>${selected ? 'Selected' : 'Inspect'}</button></td></tr>`;
        }).join('')}</tbody></table>` : data ? documentEmpty(view) : '';
        paintLongRegion('sdDocumentsList', data ? `${table}${documentWarnings(data, 'catalog')}` : state.error ? '' : stateLoading('Loading documents…'));
        paintLongRegion('sdDocumentsPage', data ? longPagination(data.offset, data.count, data.total_count) : '');
        const usable = documentPageUsable(data) && !state.loading;
        const pager = document.getElementById('sdDocumentsPagination');
        if (pager) pager.hidden = !documentPageUsable(data) || data.total_count <= 50;
        document.querySelectorAll('[data-action="sd-documents-page"]').forEach(button => {
            button.disabled = !usable || (button.dataset.step === '-1' ? data.offset < 50 : data.offset + 50 >= data.total_count);
        });
        mountDocumentInspector(view);
        if (!state.selected) return;
        const outsidePage = rows.length && !rows.some(item => documentKey(item) === documentKey(state.selected));
        paintLongRegion('sdDocumentSelectionNotice', outsidePage ? '<p class="form-hint" role="status">Selected document is not in the shown catalog page. Its detail is kept for your reading.</p>' : '');
        if (state.detailSuccess || state.detailError) memoryFreshness('sdDocumentFreshness', state.detailSuccess, state.detailError);
        else paintLongRegion('sdDocumentFreshness', '');
        if (state.contentSuccess || state.contentError) memoryFreshness('sdDocumentContentFreshness', state.contentSuccess, state.contentError);
        else paintLongRegion('sdDocumentContentFreshness', '');
        const detail = state.detail?.document;
        let html = detail ? `<h4>${safe(detail.filename || 'Document')}</h4>${documentStatus(detail.ingestion_status)}${longFields({ ...detail, size_bytes: detail.size_bytes != null ? documentSize(detail.size_bytes) : detail.size_bytes }, [['document_id', 'Document'], ['source_path', 'Source'], ['repo_path', 'Repository path'], ['source_modified_at', 'Source modified', true], ['ingested_at', 'Ingested', true], ['size_bytes', 'Bytes'], ['chunk_count', 'Chunks'], ['text_length', 'Text length'], ['content_type', 'Type'], ['sha256', 'SHA-256'], ['last_ingest_job_id', 'Last ingestion job']])}${documentWarnings(state.detail, 'metadata')}<button type="button" class="btn btn-secondary" data-action="sd-document-content" ${state.contentLoading ? 'disabled' : ''}>Load content</button><p class="form-hint">Content is fetched only on request.</p>` : state.detailError ? `<button type="button" class="btn btn-secondary" data-action="sd-document-retry">Retry metadata</button>` : stateLoading('Loading document metadata…');
        if (state.contentLoading) html += stateLoading('Loading document content…');
        if (state.content) {
            const payload = state.content, text = payload.content ?? payload.document?.content;
            const raw = payload.content_format === 'raw' || payload.document?.content_format === 'raw' || payload.content_base64 != null || payload.document?.content_base64 != null;
            html += raw ? '<p>Text preview is unavailable for this binary content.</p>' : typeof text === 'string' ? `<pre class="mono-block sd-long-content">${safe(text)}</pre>` : '<p>No text content was returned.</p>';
            const note = payload.content_note || payload.document?.content_note;
            if (note) html += `<p>${safe(note)}</p>`;
            html += documentWarnings(payload, 'content');
        }
        paintLongRegion('sdDocumentDetail', html);
    }

    function ingestActive(job) { return job && ['queued', 'running'].includes(job.status); }

    function ingestJob(view, jobId) {
        return view.ingestions.list?.jobs.find(job => job?.job_id === jobId) || (view.ingestions.selected === jobId ? view.ingestions.detail : null);
    }

    function ingestStatusLabel(status) {
        const labels = { queued: 'Queued', running: 'Running', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled', skipped: 'Skipped', changed_skipped: 'Changed · skipped' };
        return Object.hasOwn(labels, status) ? labels[status] : status || 'Status unavailable';
    }

    function ingestStatus(job) {
        const severity = { queued: 'warn', running: 'warn', succeeded: 'ok', failed: 'error' };
        return statusDot(Object.hasOwn(severity, job.status) ? severity[job.status] : 'neutral', ingestStatusLabel(job.status));
    }

    function ingestProgress(job) {
        const stages = { queued: 'Waiting in queue', s3_upload: 'Storing source', text_extract: 'Extracting text', llm_extract: 'Extracting entities and relations', chunking: 'Preparing chunks', embedding: 'Creating embeddings', ontology_construction: 'Building ontology', graph_write: 'Writing graph', vector_store: 'Storing vectors', replace: 'Preparing replacement', promote: 'Activating replacement', purge_old: 'Cleaning previous version', done: 'Indexing complete', failed: 'Failed', cancelled: 'Cancelled', skipped: 'Skipped', changed_skipped: 'Changed · skipped' };
        const step = job.current_step;
        const stage = Object.hasOwn(stages, step) ? stages[step] : step;
        const percent = job.progress_percent;
        return `${stage && stage !== ingestStatusLabel(job.status) ? `<span>${safe(stage)}</span>` : ''}${Number.isFinite(percent) && percent >= 0 && percent <= 100 ? `<progress max="100" value="${percent}" aria-label="Reported ingestion progress"></progress><span class="form-hint">${safe(percent)}% reported</span>` : ''}`;
    }

    function ingestCount(value) { return Number.isSafeInteger(value) && value >= 0; }
    function ingestDate(value) { return typeof value === 'string' && value.trim() && Number.isFinite(Date.parse(value)); }

    function ingestPage(data, pastEnd = false) {
        return Boolean(data && data.status === 'ok' && !data.partial && Array.isArray(data.jobs) && data.jobs.every(job => job && typeof job === 'object' && !Array.isArray(job)) && Number.isSafeInteger(data.offset) && data.offset >= 0 && data.offset % 50 === 0 && ingestCount(data.count) && data.count === data.jobs.length && data.count <= 50 && ingestCount(data.total) && Number.isSafeInteger(data.offset + data.count) && (pastEnd ? data.count === 0 && data.offset > 0 && data.offset >= data.total : data.offset + data.count <= data.total));
    }

    function ingestDiagnostics(value, depth = 0) {
        if (value === null || value === undefined) return '';
        if (typeof value !== 'object') return safe(value);
        if (depth >= 5) return `<pre class="mono-block">${safe(JSON.stringify(value, null, 2))}</pre>`;
        if (Array.isArray(value)) return value.length ? `<ul>${value.filter(item => item !== null && item !== undefined).map(item => `<li>${ingestDiagnostics(item, depth + 1)}</li>`).join('')}</ul>` : '';
        const labels = { entity_other: 'Entities classified as Other', relation_other: 'Relations classified as OTHER', relation_related_to: 'Relations classified as RELATED_TO', count: 'Count', denominator: 'Total', rate: 'Rate' };
        const entries = Object.entries(value).map(([key, item]) => {
            const rendered = key === 'rate' && Number.isFinite(item) && item >= 0 && item <= 1 ? safe(`${+(item * 100).toFixed(2)}%`) : ingestDiagnostics(item, depth + 1);
            return rendered ? `<div><dt>${safe(Object.hasOwn(labels, key) ? labels[key] : key.replaceAll('_', ' '))}</dt><dd>${rendered}</dd></div>` : '';
        }).join('');
        return entries ? `<dl class="sd-ingest-diagnostics">${entries}</dl>` : '';
    }

    function ingestCancelFeedback(result) {
        if (!result) return '';
        const labels = { cancelling: 'Cancellation requested · pending cooperative stop', cancelled: 'Queued job cancelled', not_found: 'Job not found in available history', noop: 'Job already completed', error: 'Cancellation refused', unknown: 'Cancellation not confirmed' };
        const checked = Boolean(result.checked_at);
        const label = checked ? (result.job_status ? `Job status: ${ingestStatusLabel(result.job_status)}` : 'Cancellation result not confirmed') : (result.message || labels[result.status] || 'Cancellation not confirmed');
        const check = result.job_status && !['queued', 'running'].includes(result.job_status) ? '' : '<button class="btn btn-secondary btn-sm" data-action="sd-ingest-cancel-check">Check cancellation result</button>';
        return `<p>${safe(result.job_id || '')}: ${safe(label)}</p><p class="form-hint">Updated ${renderTimestamp(result.checked_at || result.requested_at)}</p>${checked && result.check_message ? serverMessage(result.check_message) : ''}${checked && result.message ? `<details><summary>Cancellation request response</summary>${serverMessage(result.message)}</details>` : ''}${check}`;
    }

    function applyIngestCancelOutcome(view, result) {
        const state = view.ingestions, feedback = state.cancelResult;
        const known = result?.job_id === feedback.job_id && ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped'].includes(result.status);
        feedback.checked_at = new Date().toISOString();
        feedback.job_status = known ? result.status : null;
        feedback.check_message = known ? '' : result?.message || 'Ingestion status is unavailable.';
        state.cancelPending = known && ingestActive(result);
        if (known && state.selected === feedback.job_id) {
            state.detail = result; state.detailSuccess = feedback.checked_at; state.detailError = '';
        }
        paintLong(view);
    }

    async function readIngestCancelOutcome(view, isCurrent) {
        const state = view.ingestions, feedback = state.cancelResult;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('long_ingest_status', { space_id: view.spaceId, job_id: feedback.job_id }); }
        catch { result = { status: 'error', message: 'Ingestion status request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!isCurrent() || state.cancelResult !== feedback) return;
        applyIngestCancelOutcome(view, result);
    }

    function paintIngestions(view) {
        const state = view.ingestions, data = state.list, archive = state.archive === true;
        document.querySelectorAll('[data-action="sd-ingest-scope"]').forEach(button => button.setAttribute('aria-pressed', String((button.dataset.scope === 'archive') === archive)));
        paintLongRegion('sdIngestScopeHint', archive
            ? 'Jobs indexing retained MID captures in the separate archive memory. Read-only here: capture jobs are not cancelled from the Portal.'
            : 'Jobs indexing this space’s documents. MID capture jobs are listed separately.');
        memoryFreshness('sdIngestFreshness', state.success, state.error);
        const rows = (Array.isArray(data?.jobs) ? data.jobs : []).filter(job => job && typeof job === 'object');
        const validPage = ingestPage(data);
        const complete = validPage && !state.error && (data.count > 0 || (data.total === 0 && data.offset === 0));
        const filtered = Boolean(state.listFilters?.status || state.listFilters?.batch_id);
        const pastEnd = !state.error && ingestPage(data, true);
        const overview = `<a class="btn btn-secondary" href="#/spaces/${encodeURIComponent(view.spaceId)}/long/overview">View capture backlog</a>`;
        const empty = pastEnd ? stateEmpty({ title: 'This page no longer has jobs', hint: 'The available history may have changed.', actionHtml: '<button class="btn btn-secondary" data-action="sd-ingest-first">Back to first page</button>' }) : complete ? stateEmpty({
            title: filtered ? 'No ingestion jobs match these filters' : archive ? 'No capture indexing jobs in the available history' : 'No ingestion history available',
            hint: filtered ? 'Try another status or batch, or clear the applied filters.' : archive ? 'Job history is held in memory. Pending captures are reported in the Long memory overview.' : 'Browse the document catalog to see available documents.',
            actionHtml: filtered ? '<button class="btn btn-secondary" data-action="sd-ingest-clear">Clear filters</button>' : archive ? overview : `<a class="btn btn-secondary" href="#/spaces/${encodeURIComponent(view.spaceId)}/long/documents">Open document catalog</a>`,
        }) : '';
        const incomplete = data && !complete && !pastEnd && !state.error ? stateUnavailable('Ingestion history is incomplete or its pagination is unavailable. Refresh to try again.') : '';
        const warnings = (Array.isArray(data?.warnings) ? data.warnings : []).map(warning => serverMessage(typeof warning === 'string' ? warning : warning?.message || 'Some ingestion history is unavailable.')).join('');
        const history = rows.length ? `<div class="table-scroll sd-ingest-table-scroll"><table class="data-table sd-ingest-table"><thead><tr>${['File / job', 'Status', 'Stage / progress', 'Results', 'Queue / updated', 'Actions'].map(label => `<th scope="col">${label}</th>`).join('')}</tr></thead><tbody>${rows.map(job => {
            const selected = state.selected === job.job_id;
            const eligible = typeof job.job_id === 'string' && Boolean(job.job_id);
            const results = [['created_entities', 'entities'], ['created_relations', 'relations']].filter(([key]) => ingestCount(job[key])).map(([key, label]) => `<span>${safe(job[key])} ${label}</span>`).join('');
            return `<tr data-job-id="${safe(job.job_id)}" class="${selected ? 'is-selected' : ''}"><td data-label="File / job"><strong>${safe(job.filename || job.job_id || 'Unnamed job')}</strong>${job.filename && job.job_id ? `<span class="sd-ingest-id">${safe(job.job_id)}</span>` : ''}</td><td data-label="Status">${ingestStatus(job)}</td><td data-label="Stage / progress">${ingestProgress(job)}</td><td data-label="Results">${results}</td><td data-label="Queue / updated">${ingestCount(job.queue_position) ? `<span>${job.queue_position === 0 ? 'Not queued' : job.queue_position === 1 && job.status === 'running' ? 'Active (position 1)' : `Position ${safe(job.queue_position)}`}</span>` : ''}${ingestDate(job.updated_at) ? `<span class="form-hint">${renderTimestamp(job.updated_at)}</span>` : ''}</td><td data-label="Actions"><div class="sd-ingest-actions"><button class="btn btn-secondary btn-sm" data-action="sd-ingest-inspect" data-job-id="${safe(job.job_id)}" aria-label="Inspect ingestion ${safe(job.filename || job.job_id)}" aria-controls="sdIngestDetail" aria-expanded="${selected}"${eligible ? '' : ' disabled'}>Inspect</button>${!archive && eligible && ingestActive(job) && hasPermission(view, 'write') ? `<button class="btn btn-secondary btn-sm" data-action="sd-ingest-cancel" aria-label="Cancel ingestion ${safe(job.filename || job.job_id)}" data-job-id="${safe(job.job_id)}"${state.cancelBusy ? ' disabled' : ''}>Cancel…</button>` : ''}</div></td></tr>`;
        }).join('')}</tbody></table></div>` : empty;
        // archive_not_configured is the normal first-use wait, not an outage (#645).
        const waiting = archive && state.notConfigured && !data && !state.error ? stateEmpty({ title: 'No MID capture archive yet', hint: 'The first retained MID capture creates the archive. No capture indexing job exists yet.', actionHtml: overview }) : '';
        paintLongRegion('sdIngestList', data ? `${history}${incomplete}${warnings}<p class="form-hint">This history may be incomplete.</p>` : waiting || (state.error ? '' : stateLoading('Loading ingestion jobs…')));
        paintLongRegion('sdIngestPage', data ? longPagination(data.offset, data.count, data.total) : '');
        const valid = complete && data.count > 0;
        const pager = document.getElementById('sdIngestPager');
        if (pager) pager.hidden = !valid || data.total <= 50;
        const previous = document.getElementById('sdIngestPrevious'), next = document.getElementById('sdIngestNext');
        if (previous) previous.disabled = !valid || data.offset === 0;
        if (next) next.disabled = !valid || data.offset + 50 >= data.total;
        paintLongRegion('sdIngestCancelResult', archive ? '' : ingestCancelFeedback(state.cancelResult));
        const job = state.detail;
        const inspector = document.getElementById('sdIngestDetail');
        if (inspector) inspector.hidden = !job;
        document.getElementById('sdIngestWorkspace')?.classList.toggle('has-selection', Boolean(job));
        let html = '';
        if (job) {
            const fields = { ...job };
            for (const key of ['created_entities', 'created_relations', 'queue_position']) if (!ingestCount(fields[key])) delete fields[key];
            for (const key of ['created_at', 'started_at', 'updated_at', 'finished_at']) if (!ingestDate(fields[key])) delete fields[key];
            html = `<button class="btn btn-ghost btn-sm" data-action="sd-ingest-close">Close job inspector</button><h3>${safe(job.filename || state.selected)}</h3>${archive ? '<p class="micro-label">MID capture archive</p>' : ''}<div id="sdIngestDetailFreshness"></div>`;
            if (archive && state.detailOutsidePage) html += '<p class="form-hint" role="status">This job is not in the listed archive page. Its last read is kept; this does not establish its result.</p>';
            html += job.status === 'not_found' ? '<p>Job is no longer in the available history. This does not establish the ingestion result.</p>' : `${ingestStatus(job)}<div class="sd-ingest-stage">${ingestProgress(job)}</div>${longFields(fields, [['job_id', 'Job'], ['source_path', 'Source'], ['batch_id', 'Batch'], ['created_entities', 'Entities created'], ['created_relations', 'Relations created'], ['queue_position', 'Queue position'], ['created_at', 'Created', true], ['started_at', 'Started', true], ['updated_at', 'Updated', true], ['finished_at', 'Finished', true]])}${job.error ? serverMessage(job.error) : ''}`;
            const diagnostics = ingestDiagnostics(job.ontology_diagnostics);
            if (diagnostics) html += `<section aria-label="Ontology diagnostics"><h4>Ontology diagnostics</h4>${diagnostics}</section>`;
            html += `<div class="sd-ingest-actions"><button class="btn btn-secondary" data-action="sd-ingest-check">Check selected job</button>${!archive && ingestActive(job) && hasPermission(view, 'write') ? `<button class="btn btn-secondary" data-action="sd-ingest-cancel" data-job-id="${safe(state.selected)}"${state.cancelBusy ? ' disabled' : ''}>Cancel ingestion…</button>` : ''}</div>`;
            html += archive ? '<p class="form-hint">Archive captures are not part of the document catalog.</p>' : `<p><a href="#/spaces/${encodeURIComponent(view.spaceId)}/long/documents">Open document catalog</a></p>`;
        }
        paintLongRegion('sdIngestDetail', html);
        memoryFreshness('sdIngestDetailFreshness', state.detailSuccess, state.detailError);
        if (state.focusDetail && job && inspector) {
            state.focusDetail = false;
            inspector.style.scrollMarginTop = `${(document.getElementById('portalTopbar')?.getBoundingClientRect().height || 0) + 12}px`;
            inspector.focus({ preventScroll: true }); inspector.scrollIntoView?.({ block: 'start' });
        }
    }

    function renderLongData(view, data) {
        const health = renderLongHealth(data);
        const stats = data.status === 'ok' && data.connected === true
            ? graphStatsSection(data.graph_stats, data.reachable)
            : '';
        return `<div class="sd-long-overview">${health}${stats}</div>
            ${renderMidAutomation(data)}
            ${renderLongOperational(view, data)}`;
    }

    function renderMidAutomation(data) {
        const policy = data && data.mid_automation;
        if (!policy || typeof policy !== 'object') return '';
        const mode = value => value === true ? 'Enabled' : value === false ? 'Disabled by configuration' : 'Unknown';
        const backlog = data.mid_archive_projection || {};
        const pending = Number.isSafeInteger(backlog.pending) && backlog.pending >= 0 ? backlog.pending : null;
        return `<section class="sd-long-stack sd-automation" aria-label="MID to LONG automation"><h3>MID → LONG automation</h3>
            <div class="sd-meta-row">${keyValue('Automatic compaction', mode(policy.compaction_enabled))}${keyValue('Automatic transfer to LONG', mode(policy.archive_enabled))}</div>
            <p class="body-small">When enabled, compaction follows a successful consolidation in local spaces and only processes oversized MID files. Originals are retained and queued for LONG; disabling transfer keeps them pending.</p>
            <div class="sd-meta-row">${keyValue('Captures pending indexing', pending)}${keyValue('Oldest capture', backlog.oldest_at, { timestamp: true })}</div>
            ${backlog.error ? attentionBanner('LONG indexing needs attention', String(backlog.error)) : ''}
            <p class="form-hint">A zero backlog does not mean every current MID file is indexed. </p>
        </section>`;
    }

    function renderLongConnection(view, data) {
        if (data.status === 'not_found') return stateEmpty({ title: 'Space not found' });
        if (data.status !== 'ok') return `${unavailableOrError(data, 'sd-retry-long')}${renderLongActions(view)}`;
        const stats = data.connected === true ? graphStatsSection(data.graph_stats, data.reachable) : '';
        return `${renderLongHealth(data)}${stats}${renderLongOperational(view, data)}`;
    }

    function renderLongHealth(data) {
        if (data.status === 'not_found') return stateEmpty({ title: 'Space not found' });
        if (data.status !== 'ok') return unavailableOrError(data, 'sd-retry-long');

        const unbound = data.connected === false && data.embedded === true && data.bound === false;
        let health;
        if (data.connected === false) {
            health = unbound
                ? `<div class="sd-long-health-state sd-long-health-state--banner"><span class="micro-label">LONG connection</span><p class="sd-long-state sd-long-state--unknown">Unbound</p>${attentionBanner('Waiting for the first ingestion', 'The first MID archive or documentary ingestion connects this space automatically.')}</div>`
                : `<div class="sd-long-health-state sd-long-health-state--banner">${failClosedBanner('Long runtime unavailable', 'The required embedded long runtime is not configured for this space.')}${data.message ? serverMessage(data.message) : ''}</div>`;
        } else if (data.connected !== true) {
            health = `<div class="sd-long-health-state"><span class="micro-label">LONG connection</span><p class="sd-long-state sd-long-state--unknown">Connection status unknown</p></div>`;
        } else if (data.reachable === false) {
            const outage = data.binding === 'embedded'
                ? ['Embedded long runtime unreachable', 'Embedded long runtime unreachable — this deployment is out of contract (ADR-0019).']
                : data.binding === 'explicit'
                    ? ['Explicit long runtime unreachable', 'The explicitly configured Graph Memory runtime cannot be reached. Check its URL and credentials.']
                    : ['Long runtime unreachable — binding unknown', 'The bound runtime is unreachable and its binding classification is unknown. Treat this state as unsafe until verified.'];
            health = `<div class="sd-long-health-state sd-long-health-state--banner">${failClosedBanner(outage[0], outage[1])}${data.error ? serverMessage(data.error) : ''}</div>`;
        } else if (data.reachable === true && ['embedded', 'explicit'].includes(data.binding)) {
            health = `<div class="sd-long-health-state"><span class="micro-label">LONG connection</span><p class="sd-long-state sd-long-state--healthy">Connected and reachable</p></div>`;
        } else if (data.reachable === true) {
            health = `<div class="sd-long-health-state sd-long-health-state--banner">${failClosedBanner('Unknown long binding — fail-closed', 'The bound runtime did not report a recognized binding classification.')}</div>`;
        } else {
            health = `<div class="sd-long-health-state"><span class="micro-label">LONG connection</span><p class="sd-long-state sd-long-state--unknown">Connected · reachability unknown</p></div>`;
        }

        const binding = unbound
            ? '<div class="sd-long-fact"><span class="micro-label">Binding</span><span>Unbound</span></div>'
            : `<div class="sd-long-fact"><span class="micro-label">Binding</span><span>${safe(data.connected === false ? 'Not configured' : data.binding === 'embedded' ? 'Embedded long runtime' : data.binding === 'explicit' ? 'Explicit Graph Memory runtime' : 'Unknown')}</span></div>`;
        const explicitConfig = data.binding === 'explicit' && data.config && typeof data.config === 'object'
            ? [['URL', data.config.url], ['Memory ID', data.config.memory_id]].filter(([, value]) => value !== null && value !== undefined && value !== '')
                .map(([label, value]) => `<div class="sd-long-fact"><span class="micro-label">${label}</span><span class="mono-data">${label === 'URL' ? safeLongEndpoint(value) : safe(value)}</span></div>`).join('')
            : '';
        const ontology = data.config && typeof data.config.ontology === 'string' && data.config.ontology
            ? safe(data.config.ontology)
            : 'Configured ontology is not reported';
        return `<section class="sd-long-health" aria-label="LONG connection and configuration">
            ${health}${binding}${explicitConfig}<div class="sd-long-fact"><span class="micro-label">Configured ontology</span><span>${ontology}</span></div>
            <p class="form-hint sd-long-ontology-note">Declared configuration does not establish the schema used by all existing graph data.</p>
        </section>`;
    }

    function renderLongOperational(view, data) {
        if (data.status !== 'ok') return renderLongActions(view);
        return `<section class="sd-long-operational" aria-label="LONG operational details">
            ${renderWatermark(data.watermark)}
            <div class="sd-meta-row">${keyValue('Last push', data.last_push, { timestamp: true })}${keyValue('Push count', data.push_count)}${keyValue('Files pushed', data.files_pushed)}</div>
            ${renderLongActions(view)}
        </section>`;
    }

    function renderWatermark(watermark) {
        if (!watermark) return '';
        const flagged = watermark.flagged === true
            ? attentionBanner('High-water mark preserved', 'Push observed a bank_version regression (possible rollback/split-brain) — high-water mark preserved.') : '';
        return `<section class="sd-watermark"><h3>Derived watermark</h3>${flagged}<div class="sd-meta-row">
            ${keyValue('Bank version', watermark.bank_version)}${keyValue('Commit ID', watermark.commit_id)}${keyValue('Term', watermark.term)}${keyValue('Provenance', watermark.provenance)}${keyValue('Recorded', watermark.recorded_at, { timestamp: true })}${keyValue('Flagged', watermark.flagged === true ? 'yes' : 'no')}
        </div><p class="form-hint">This watermark reports projection history only; it never decides mesh state.</p></section>`;
    }

    function renderLongActions(view) {
        if (!hasPermission(view, 'write')) return '';
        return `<div class="sd-actions-row">${renderGraphPushAction(view)}</div>`;
    }

    function renderConsolidateAction(view) {
        const disabled = view.consolidating || !hasPermission(view, 'write');
        const title = hasPermission(view, 'write') ? '' : ' title="Write permission is required"';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-confirm-consolidate"${disabled ? ' disabled' : ''}${title}>${icon('consolidation')}Start a consolidation</button>`;
    }

    function renderGraphPushAction(view) {
        const disabled = view.longMutating || !hasPermission(view, 'write');
        const title = hasPermission(view, 'write') ? '' : ' title="Write permission is required"';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-confirm-graph-push"${disabled ? ' disabled' : ''}${title}>${icon('push')}Index current MID files</button>`;
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

    // ───────────── Active work (#651) ─────────────
    // A read-only summary of the existing job sources. Each row links to the
    // existing detail; there is no aggregate API, extra scheduler or storage.
    const CONSOLIDATION_PHASES = { queued: 'Waiting in queue', planned: 'Planning batches', batch_running: 'Consolidating notes', batch_retry_wait: 'Waiting to retry a batch', batch_done: 'Batch saved', compacting: 'Compacting MID files', done: 'Done', failed: 'Failed' };
    // Same outcome labels as the consolidation inspector (renderAutoCompaction).
    const COMPACTION_OUTCOMES = { ok: ['ok', 'Files compacted'], not_needed: ['neutral', 'No oversized files'], disabled: ['neutral', 'Disabled by configuration'], not_applicable: ['neutral', 'Space not eligible'], error: ['error', 'Compaction failed'], partial: ['error', 'Compaction incomplete'], cancelled: ['error', 'Compaction interrupted'] };

    function activitySource() { return { value: null, at: null, error: '' }; }
    function activityApply(source, value) { source.value = value; source.at = new Date().toISOString(); source.error = ''; }
    function activityFail(source, result, fallback) { source.error = String(result?.message || result?.reason || fallback); }
    function activityObject(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : null; }

    function activityLaneJobs(lane) {
        const jobs = value => Array.isArray(value) ? value.filter(activityObject) : [];
        return { running: activityObject(lane?.running_job), queued: jobs(lane?.queued_jobs), latest: jobs(lane?.latest_jobs) };
    }

    // Only jobs reported as queued/running count. The three list reads are
    // independent snapshots: a job that became active before the newest-job
    // read joins the active set instead of being discarded (#651 R2).
    function activityJobs(value) {
        if (!value || value.notConfigured) return [];
        const seen = new Set();
        return [...value.running.jobs, ...value.queued.jobs, ...(value.latest?.jobs || [])].filter(job => {
            if (!ingestActive(job)) return false;
            const key = typeof job.job_id === 'string' ? job.job_id : '';
            if (key && seen.has(key)) return false;
            if (key) seen.add(key);
            return true;
        });
    }

    function activityActive(view) {
        const state = view.activity, lane = state.lane.value, { running, queued } = activityLaneJobs(lane);
        const queuedCount = ingestCount(lane?.queued_count) ? lane.queued_count : queued.length;
        return !!running || lane?.manual_compaction?.status === 'running' || queuedCount > 0 || activityJobs(state.docs.value).length > 0 || activityJobs(state.archive.value).length > 0;
    }

    // Returns null once the view no longer owns the route.
    async function readActivityTool(view, tool, args) {
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool(tool, args); }
        catch { result = { status: 'error', message: 'Request failed.' }; }
        if (!guarded(view, epochAtCall)) return null;
        return activityObject(result) || { status: 'error', message: 'No response.' };
    }

    function activityListOk(result) {
        return result?.status === 'ok' && Array.isArray(result.jobs) && result.jobs.every(activityObject);
    }

    // Same reads and gates as the CLI `space status` (#638/#645). The archive
    // scope is explicit, and its first-use wait state is not an outage.
    async function readActivityJobs(view, archive) {
        const base = { space_id: view.spaceId, limit: ACTIVITY_JOB_LIMIT, ...(archive ? { archive: true } : {}) };
        const running = await readActivityTool(view, 'long_ingest_list', { ...base, status: 'running' });
        if (!running) return { skipped: true };
        if (archive && running.status === 'error' && running.reason === 'archive_not_configured') return { value: { notConfigured: true } };
        if (!activityListOk(running)) return { failure: running };
        const queued = await readActivityTool(view, 'long_ingest_list', { ...base, status: 'queued' });
        if (!queued) return { skipped: true };
        if (!activityListOk(queued)) return { failure: queued };
        if (activityJobs({ running, queued }).length) return { value: { running, queued } };
        // Nothing active: one newest-job read shows the latest outcome, so a
        // job that just failed is reported instead of looking idle (#651 R1 F1).
        const latest = await readActivityTool(view, 'long_ingest_list', { ...base, limit: 1 });
        if (!latest) return { skipped: true };
        if (!activityListOk(latest)) return { failure: latest };
        return { value: { running, queued, latest } };
    }

    function activityDocsReadable(graph) {
        return graph?.status === 'ok' && graph.connected === true && graph.reachable !== false;
    }

    function activityArchiveReadable(graph) {
        const projection = activityObject(graph?.mid_archive_projection);
        return graph?.status === 'ok' && !!projection && graph.reachable !== false && (projection.pending !== 0 || !!projection.error);
    }

    async function refreshActivity(view, { automatic, isCurrent }) {
        const state = view.activity;
        const current = () => isCurrent() && guarded(view, view.ctx.epoch) && sessionGenerationIsCurrent(view.ctx.sessionGeneration) && view.tab === 'activity';
        if (!current()) return { skipped: true };
        const entry = !state.started; state.started = true;
        // Entry reuses the space_info lane read for the header; later cycles
        // re-read that same Space Detail source. graph_status is never
        // timer-polled (§17.2.1): automatic cycles follow the job lists
        // selected by the last explicit LONG status read.
        const [info, graph] = await Promise.all([
            entry && state.lane.value ? null : readActivityTool(view, 'space_info', { space_id: view.spaceId }),
            automatic ? null : readActivityTool(view, 'graph_status', { space_id: view.spaceId, include_graph: false }),
        ]);
        if (!current()) return { skipped: true };
        let failed = false;
        if (info) {
            const lane = info.status === 'ok' ? activityObject(info.consolidation_queue) : null;
            if (lane) activityApply(state.lane, lane);
            else { failed = true; activityFail(state.lane, info.status === 'ok' ? { message: 'Consolidation state is not reported for this space.' } : info, 'Consolidation state is unavailable.'); }
        }
        if (graph) {
            if (graph.status === 'ok') activityApply(state.graph, graph);
            else { failed = true; activityFail(state.graph, graph, 'LONG status is unavailable.'); }
        }
        const gate = automatic ? state.graph.value : graph?.status === 'ok' ? graph : null;
        const reads = [];
        for (const [key, archive, readable] of [['docs', false, activityDocsReadable], ['archive', true, activityArchiveReadable]]) {
            if (gate && readable(gate)) reads.push(readActivityJobs(view, archive).then(result => [key, result]));
            else if (gate) state[key] = activitySource(); // Nothing of this kind to follow now.
        }
        paintActivity(view);
        const results = await Promise.all(reads);
        if (!current()) return { skipped: true };
        for (const [key, result] of results) {
            if (result.skipped) return { skipped: true };
            if (result.failure) { failed = true; activityFail(state[key], result.failure, 'Ingestion jobs are unavailable.'); }
            else activityApply(state[key], result.value);
        }
        if (!failed) state.success = new Date().toISOString();
        paintActivity(view);
        if (failed) throw new Error('Active work refresh failed');
        return { follow: activityActive(view) };
    }

    function startActivity(view) {
        const lane = activityObject(view.info?.consolidation_queue);
        if (lane) activityApply(view.activity.lane, lane);
        PortalRefresh.register({ refresh: options => refreshActivity(view, options), canAuto: () => view.tab === 'activity' && activityActive(view) }, view.ctx);
        if (!PortalRefresh.state().available) { view.activity.unavailable = 'Refresh unavailable. Sign out and sign in again.'; paintActivity(view); return; }
        paintActivity(view);
        void PortalRefresh.refresh().catch(() => {});
    }

    function renderActivityShell() {
        return `<section class="panel sd-section sd-activity" aria-labelledby="sdActivityTitle"><div class="panel-header"><h2 id="sdActivityTitle">Active work</h2><button type="button" class="btn btn-secondary btn-sm" data-action="sd-activity-refresh" aria-label="Refresh active work">${icon('refresh')}<span>Refresh</span></button></div>
            <p class="body-small">What is working in this space, at which step, and whether it is blocked. Each row opens its existing detail.</p>
            <div id="sdActivityFreshness" class="body-small text-muted"></div>
            <ul id="sdActivityList" class="sd-activity-list" aria-label="Active work in this space"></ul>
            <p class="form-hint">Job histories are held in server memory and are best-effort. Manual compaction is tracked while its original request runs; no percentage is reported.</p>
        </section>`;
    }

    function activityRow(key, title, severity, label, body, actions = '') {
        return `<li class="sd-activity-row" data-activity="${key}"><div class="sd-activity-head"><h3>${safe(title)}</h3>${statusDot(severity, label)}</div><div class="sd-activity-body">${body}</div>${actions ? `<div class="sd-activity-actions">${actions}</div>` : ''}</li>`;
    }

    function activityOpen(kind, jobId, label, text = 'Open job') {
        if (jobId !== '' && (typeof jobId !== 'string' || !jobId)) return '';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="sd-activity-open" data-kind="${kind}" data-job-id="${safe(jobId)}" aria-label="${safe(label)}">${safe(text)}</button>`;
    }

    function activityStale(source) {
        if (!source.error) return '';
        return `<p class="state-degraded" role="status">${source.value ? `Refresh failed; showing the read from ${renderTimestamp(source.at)}.` : 'Read failed.'}</p>${serverMessage(source.error)}`;
    }

    function activityFacts(items) {
        return items.length ? `<ul class="sd-activity-facts">${items.map(item => `<li>${item}</li>`).join('')}</ul>` : '';
    }

    function consolidationFacts(job) {
        const progress = activityObject(job.progress) || {}, facts = [];
        const phase = typeof progress.phase === 'string' && progress.phase ? (Object.hasOwn(CONSOLIDATION_PHASES, progress.phase) ? CONSOLIDATION_PHASES[progress.phase] : progress.phase) : '';
        if (phase) facts.push(`Phase: ${safe(phase)}`);
        if (ingestCount(progress.notes_done) && ingestCount(progress.notes_total)) facts.push(`${safe(progress.notes_done)}/${safe(progress.notes_total)} notes`);
        if (ingestCount(progress.batches_done) && ingestCount(progress.batches_total)) facts.push(`${safe(progress.batches_done)}/${safe(progress.batches_total)} batches`);
        if (job.scope_label) facts.push(safe(job.scope_label));
        const when = job.status === 'running' ? job.started_at : job.queued_at || job.requested_at;
        if (ingestDate(when)) facts.push(`${job.status === 'running' ? 'Started' : 'Queued'} ${renderTimestamp(when)}`);
        return facts;
    }

    function laneUnavailableRow(key, title, source) {
        return activityRow(key, title, source.error ? 'error' : 'neutral', source.error ? 'Unavailable' : 'Checking', source.error ? serverMessage(source.error) : stateLoading('Reading consolidation state…'));
    }

    function renderConsolidationActivity(view) {
        const source = view.activity.lane, lane = source.value, title = 'MID consolidation';
        if (!lane) return laneUnavailableRow('consolidation', title, source);
        const { running, queued, latest } = activityLaneJobs(lane), stale = activityStale(source);
        const queuedCount = ingestCount(lane.queued_count) ? lane.queued_count : queued.length;
        const open = job => activityOpen('consolidation', job.job_id, `Open consolidation job ${job.job_id}`);
        if (running) return activityRow('consolidation', title, 'warn', 'Running', `${activityFacts([...consolidationFacts(running), ...(queuedCount > 0 ? [`${safe(queuedCount)} more queued`] : [])])}${stale}`, open(running));
        if (queuedCount > 0) {
            const next = queued[0];
            return activityRow('consolidation', title, 'warn', 'Queued', `<p>${safe(queuedCount)} job${queuedCount === 1 ? '' : 's'} queued.</p>${next ? activityFacts(consolidationFacts(next)) : '<p>Queued job details are unavailable.</p>'}${stale}`, next ? open(next) : '');
        }
        const terminal = latest.find(job => ['succeeded', 'failed'].includes(job.status));
        if (terminal) {
            const failed = terminal.status === 'failed';
            const when = ingestDate(terminal.finished_at) ? ` ${renderTimestamp(terminal.finished_at)}` : '. Completion time unavailable';
            return activityRow('consolidation', title, failed ? 'error' : 'ok', failed ? 'Last job failed' : 'Last job completed', `<p>No job is running or queued. The last job ${failed ? 'failed' : 'completed'}${when}.</p>${failed && terminal.error ? serverMessage(terminal.error) : ''}${stale}`, open(terminal));
        }
        return activityRow('consolidation', title, 'neutral', 'Idle', `<p>No consolidation job in this server’s recent history.</p>${stale}`);
    }

    function renderCompactionActivity(view) {
        const source = view.activity.lane, lane = source.value, title = 'Automatic compaction';
        if (!lane) return laneUnavailableRow('compaction', title, source);
        const policy = activityObject(view.activity.graph.value?.mid_automation);
        const disabled = policy?.compaction_enabled === false ? '<p>Automatic compaction is disabled by configuration.</p>' : '';
        const { running, latest } = activityLaneJobs(lane), stale = activityStale(source);
        if (running && activityObject(running.progress)?.phase === 'compacting') {
            return activityRow('compaction', title, 'warn', 'In progress', `<p>Running inside the current consolidation job. No percentage is reported.</p>${disabled}${stale}`, activityOpen('consolidation', running.job_id, `Open consolidation job ${running.job_id} for its compaction`));
        }
        const job = latest.find(item => ['succeeded', 'failed'].includes(item.status) && activityObject(activityObject(item.result)?.auto_compaction));
        if (!job) return activityRow('compaction', title, 'neutral', 'No recent result', `<p>No automatic compaction result in this server’s recent job history.</p>${disabled}${stale}`);
        const outcome = job.result.auto_compaction;
        const [severity, label] = Object.hasOwn(COMPACTION_OUTCOMES, outcome.status) ? COMPACTION_OUTCOMES[outcome.status] : ['neutral', 'Unknown outcome'];
        const recovery = outcome.recovery_required === true;
        const when = ingestDate(outcome.finished_at) ? outcome.finished_at : ingestDate(job.finished_at) ? job.finished_at : null;
        const reason = outcome.failure_reason || outcome.reason;
        return activityRow('compaction', title, recovery ? 'error' : severity, recovery ? `${label} · recovery required` : label,
            `<p>Latest automatic result${when ? `, ${renderTimestamp(when)}` : ''}. It is separate from the consolidation result.</p>${recovery ? '<p>Recovery must be checked before retrying.</p>' : ''}${typeof reason === 'string' && reason ? serverMessage(reason) : ''}${disabled}${stale}`,
            activityOpen('consolidation', job.job_id, `Open consolidation job ${job.job_id} for its compaction result`));
    }

    function renderManualCompactionActivity(view) {
        const source = view.activity.lane, lane = source.value, title = 'Manual compaction';
        if (!lane) return laneUnavailableRow('manual-compaction', title, source);
        const stale = activityStale(source);
        if (!Object.hasOwn(lane, 'manual_compaction')) return activityRow('manual-compaction', title, 'neutral', 'Tracking unavailable', `<p>This server does not report manual compaction activity.</p>${stale}`);
        const job = activityObject(lane.manual_compaction);
        if (lane.manual_compaction === null) return activityRow('manual-compaction', title, 'neutral', 'Idle', `<p>No manual compaction in this server’s recent history.</p>${stale}`);
        if (!job) return activityRow('manual-compaction', title, 'error', 'Activity unavailable', `<p>The manual compaction snapshot is invalid.</p>${stale}`);
        const running = job.status === 'running', failed = job.status === 'failed';
        const recovery = job.result?.recovery_required === true;
        const label = running ? 'In progress' : failed ? 'Last compaction failed' : job.status === 'succeeded' ? 'Last compaction completed' : 'Unknown status';
        const when = running ? job.started_at : job.finished_at;
        return activityRow('manual-compaction', title, failed || recovery ? 'error' : running ? 'warn' : job.status === 'succeeded' ? 'ok' : 'neutral', label,
            `<p>${running ? 'Compacting MID files. No percentage is reported.' : 'Latest manual compaction result.'}${ingestDate(when) ? ` ${running ? 'Started' : 'Finished'} ${renderTimestamp(when)}.` : ''}</p>${recovery ? '<p>Recovery must be checked before retrying.</p>' : ''}${job.error ? serverMessage(job.error) : ''}${stale}`,
            job.job_id ? activityOpen('consolidation', job.job_id, `Open manual compaction job ${job.job_id}`) : '');
    }

    function activityGraphRow(view, key, title) {
        const source = view.activity.graph;
        if (source.value) return '';
        return activityRow(key, title, source.error ? 'error' : 'neutral', source.error ? 'LONG status unavailable' : 'Checking', source.error ? serverMessage(source.error) : stateLoading('Reading LONG status…'));
    }

    function activityJobItem(job, kind) {
        const name = job.filename || job.job_id || 'Unnamed job';
        const position = job.status === 'queued' && ingestCount(job.queue_position) && job.queue_position > 0 ? `<span class="form-hint">Position ${safe(job.queue_position)}</span>` : '';
        const updated = ingestDate(job.updated_at) ? `<span class="form-hint">Updated ${renderTimestamp(job.updated_at)}</span>` : '';
        return `<li class="sd-activity-job"><div class="sd-activity-job-main"><strong class="mono-data">${safe(name)}</strong>${ingestStatus(job)}<div class="sd-ingest-stage">${ingestProgress(job)}</div>${position}${updated}</div>${activityOpen(kind, job.job_id, `Open ${kind === 'archive' ? 'capture indexing' : 'document ingestion'} job ${name}`)}</li>`;
    }

    // Latest terminal ingestion outcome; a failure is always shown as a failure.
    const INGEST_OUTCOMES = { failed: ['error', 'Last job failed'], cancelled: ['neutral', 'Last job cancelled'], succeeded: ['ok', 'Last job completed'], skipped: ['ok', 'Last job skipped'], changed_skipped: ['ok', 'Last job skipped'] };

    function activityJobsRow(key, title, source, { context = '', after = '', idle, problem = '' }) {
        const value = source.value, stale = activityStale(source);
        if (!value) return activityRow(key, title, source.error || problem ? 'error' : 'neutral', source.error ? 'Jobs unavailable' : problem ? 'Needs attention' : 'Checking', `${context}${source.error ? serverMessage(source.error) : stateLoading('Reading ingestion jobs…')}${after}`);
        const jobs = activityJobs(value);
        const kind = key === 'archive' ? 'archive' : 'documents';
        if (!jobs.length) {
            const latest = activityObject(value.latest?.jobs?.[0]);
            const outcome = latest && !ingestActive(latest) ? (Object.hasOwn(INGEST_OUTCOMES, latest.status) ? INGEST_OUTCOMES[latest.status] : ['neutral', 'Last job reported']) : null;
            // A pending backlog keeps its waiting label unless the latest job failed.
            const [severity, label] = problem ? ['error', 'Needs attention'] : outcome && (outcome[0] === 'error' || !idle.keep) ? outcome : [idle.severity, idle.label];
            const last = outcome ? `<p>Latest job:</p><ul class="sd-activity-jobs">${activityJobItem(latest, kind)}</ul>${latest.error ? serverMessage(latest.error) : ''}` : '';
            return activityRow(key, title, severity, label, `${context}${idle.body}${last}${stale}${after}`);
        }
        const count = list => ingestCount(list.total) ? list.total : list.jobs.filter(ingestActive).length;
        const listed = new Set([...value.running.jobs, ...value.queued.jobs].map(job => job.job_id));
        const late = jobs.filter(job => !listed.has(job.job_id));
        const running = count(value.running) + late.filter(job => job.status === 'running').length;
        const queued = count(value.queued) + late.filter(job => job.status === 'queued').length;
        const shown = jobs.slice(0, ACTIVITY_JOBS_SHOWN);
        const more = running + queued > shown.length ? `<p class="form-hint">Showing ${safe(shown.length)} of ${safe(running + queued)} active jobs.</p>` : '';
        return activityRow(key, title, problem ? 'error' : 'warn', problem ? 'Needs attention' : running > 0 ? 'Running' : 'Queued',
            `${context}<p>${safe(running)} running · ${safe(queued)} queued</p><ul class="sd-activity-jobs">${shown.map(job => activityJobItem(job, kind)).join('')}</ul>${more}${stale}${after}`,
            activityOpen(kind, '', `View all ${kind === 'archive' ? 'capture indexing' : 'document ingestion'} jobs`, 'View all jobs'));
    }

    function renderDocumentActivity(view) {
        const title = 'Document ingestion', early = activityGraphRow(view, 'documents', title);
        if (early) return early;
        const graph = view.activity.graph.value, graphStale = activityStale(view.activity.graph);
        if (graph.connected === false) {
            const unbound = graph.embedded === true && graph.bound === false;
            return activityRow('documents', title, unbound ? 'neutral' : 'error', unbound ? 'Not bound yet' : 'LONG not configured', `<p>${unbound ? 'The first document or MID capture ingestion connects this space automatically.' : 'The required LONG runtime is not configured for this space.'}</p>${graph.message ? serverMessage(graph.message) : ''}${graphStale}`);
        }
        if (graph.reachable === false) return activityRow('documents', title, 'error', 'Graph unavailable', `<p>Graph cannot be reached, so its job list was not read.</p>${graph.error ? serverMessage(graph.error) : ''}${graphStale}`);
        if (graph.connected !== true) return activityRow('documents', title, 'neutral', 'Status unknown', `<p>The LONG connection status is not reported.</p>${graphStale}`);
        return activityJobsRow('documents', title, view.activity.docs, { after: graphStale, idle: { severity: 'neutral', label: 'No active job', body: '<p>No document ingestion is running or queued.</p>' } });
    }

    function renderArchiveActivity(view) {
        const title = 'MID capture indexing', early = activityGraphRow(view, 'archive', title);
        if (early) return early;
        const graphSource = view.activity.graph, graph = graphSource.value, graphStale = activityStale(graphSource);
        const projection = activityObject(graph.mid_archive_projection), policy = activityObject(graph.mid_automation) || {};
        const transfer = policy.archive_enabled === false ? '<p>Automatic transfer to LONG is disabled by configuration; captures stay pending.</p>' : '';
        if (!projection) return activityRow('archive', title, 'neutral', 'Not reported', `<p>This server does not report the MID capture backlog.</p>${transfer}${graphStale}`);
        const pending = ingestCount(projection.pending) ? projection.pending : null;
        // Every projection error is a failure, including an unreadable backlog (#651 R1 F2).
        const problem = typeof projection.error === 'string' ? projection.error : '';
        const known = pending !== null && problem !== 'projection_unavailable';
        const backlog = `<p>${known ? `${safe(pending)} capture${pending === 1 ? '' : 's'} pending indexing` : 'Capture backlog unknown'}${graphSource.at ? ` · checked ${renderTimestamp(graphSource.at)}` : ''}</p>${known && pending > 0 && ingestDate(projection.oldest_at) ? `<p>Oldest capture: ${renderTimestamp(projection.oldest_at)}</p>` : ''}${problem ? `<p>Indexing problem: <code>${safe(problem)}</code>${problem === 'projection_unavailable' ? ' — the capture backlog could not be read.' : ''}</p>` : ''}${transfer}`;
        if (graph.reachable === false) return activityRow('archive', title, 'error', 'Graph unavailable', `${backlog}<p>Graph cannot be reached, so capture indexing jobs were not read.</p>${graph.error ? serverMessage(graph.error) : ''}${graphStale}`);
        if (pending === 0 && !problem) return activityRow('archive', title, 'neutral', 'No capture pending', `${backlog}<p>A zero backlog does not prove that every current MID file is indexed.</p>${graphStale}`);
        const source = view.activity.archive;
        if (source.value?.notConfigured) return activityRow('archive', title, problem ? 'error' : 'warn', problem ? 'Needs attention' : 'Archive not yet created', `${backlog}<p>The first retained capture creates the MID archive; no indexing job exists yet.${problem ? '' : ' This is a normal wait, not an outage.'}</p>${activityStale(source)}${graphStale}`);
        return activityJobsRow('archive', title, source, { context: backlog, after: graphStale, problem, idle: {
            severity: pending > 0 ? 'warn' : 'neutral', label: pending > 0 ? 'Capture pending' : 'Backlog unknown', keep: pending > 0,
            body: '<p>No capture indexing job is running or queued.</p>',
        } });
    }

    function paintActivity(view) {
        if (currentView !== view || view.tab !== 'activity') return;
        const state = view.activity;
        const failed = [state.lane, state.graph, state.docs, state.archive].some(source => source.error);
        memoryFreshness('sdActivityFreshness', state.success, state.unavailable || (failed ? 'Some reads failed. Each row keeps its last successful content.' : ''));
        paintLongRegion('sdActivityList', [renderConsolidationActivity(view), renderCompactionActivity(view), renderManualCompactionActivity(view), renderDocumentActivity(view), renderArchiveActivity(view)].join(''));
    }

    function renderAuxiliary(view) {
        const target = document.getElementById('sdAuxiliary');
        if (!target || currentView !== view) return;
        if (view.tab === 'rules') target.innerHTML = `<div class="panel sd-section"><div class="panel-header"><h2>Rules</h2></div><div id="sdRulesPanel">${renderRules(view)}</div></div>`;
        else if (view.tab === 'activity') {
            if (view.activityMounted) return;
            view.activityMounted = true;
            target.innerHTML = renderActivityShell();
            paintActivity(view);
        }
        else if (view.tab === 'consolidation') {
            if (view.consolidationMounted) return;
            view.consolidationMounted = true;
            target.innerHTML = `<div id="sdConsolidationJobs"></div>${renderConsolidationTools(view)}`;
            const renderConsolidation = AdminViews.get('consolidation');
            const inspectJobId = view.consolidationHandoff; view.consolidationHandoff = null;
            if (renderConsolidation) renderConsolidation(document.getElementById('sdConsolidationJobs'), {
                spaceId: view.spaceId, embedded: true, initialLane: view.info.consolidation_queue, ...(inspectJobId ? { inspectJobId } : {}),
            }, view.ctx);
        }
        else if (view.tab === 'access' && hasPermission(view, 'manage')) target.innerHTML = `<div class="panel sd-section"><div class="panel-header"><h2>Access</h2><a class="btn btn-secondary" href="#/access">Open global access</a></div><div id="sdAccessPanel">${renderAccess(view)}</div></div>`;
        else if (view.tab === 'backups' && hasPermission(view, 'write')) {
            if (view.backupsMounted) return;
            view.backupsMounted = true;
            const renderOperator = AdminViews.get('operator');
            if (renderOperator) renderOperator(target, { tab: 'backups', spaceId: view.spaceId }, view.ctx);
        }
        else if (view.tab === 'maintenance' && hasPermission(view, 'manage')) {
            if (view.maintenanceMounted) return;
            view.maintenanceMounted = true;
            target.innerHTML = `<div id="sdMaintenanceTools"></div><div class="panel sd-section">${renderDeleteAction(view)}</div>`;
            const renderOperator = AdminViews.get('operator');
            if (renderOperator) renderOperator(document.getElementById('sdMaintenanceTools'), { tab: 'maintenance', spaceId: view.spaceId }, view.ctx);
        } else if (!['memory', 'long'].includes(view.tab)) target.innerHTML = stateUnavailable('This section requires additional permission.');
    }

    function renderConsolidationTools(view) {
        const maintenance = `#/spaces/${encodeURIComponent(view.spaceId)}/maintenance`;
        const link = (label, permission, href) => hasPermission(view, permission)
            ? `<a class="btn btn-secondary" href="${safe(href)}">${label}</a>`
            : `<span><button type="button" class="btn btn-secondary" disabled>${label}</button><span class="form-hint">Requires ${permission} permission.</span></span>`;
        return `<section class="panel sd-section" aria-label="For this space"><h2>For this space</h2>
            <p class="body-small">Consolidation saves notes into MID. File compaction and transfer to LONG are separate stages with their own results.</p>
            <div class="sd-actions-row"><span id="sdConsolidationCheck">${hasPermission(view, 'manage') ? renderCompactionAction(view) : '<button type="button" class="btn btn-secondary" disabled>Check files for compaction</button><span class="form-hint">Requires manage permission.</span>'}</span>
                ${link('Repair bank', 'manage', maintenance)}
                ${link('Restore a backup', 'write', `#/spaces/${encodeURIComponent(view.spaceId)}/backups`)}
                ${link('Garbage-collect notes', 'admin', maintenance)}
            </div>
            <p class="form-hint">For large exports, use the CLI: <code>python scripts/mcp_cli.py space export ${safe(view.spaceId)}</code>.</p>
            <div id="sdConsolidationCompaction">${renderCompactionReport(view)}</div>
        </section>`;
    }

    function renderDeleteAction(view) {
        return hasPermission(view, 'manage') ? `<div class="sd-danger-zone"><h3>Delete space</h3><p>Permanently removes this space and its stored data, then removes the space from every token allowlist. Reusing the same ID never restores previous access.</p><button type="button" class="btn btn-danger" data-action="sd-confirm-space-delete">${icon('trash')}Delete space</button></div>` : '';
    }

    function renderLoadedView(view) {
        view.maintenanceMounted = false;
        view.activityMounted = false;
        view.consolidationMounted = false;
        view.backupsMounted = false;
        const panelLabel = spaceSections(view).some(([tab]) => tab === view.tab)
            ? `aria-labelledby="sdSpaceTab-${safe(view.tab)}"` : 'aria-label="Unavailable space section"';
        view.contentEl.innerHTML = `<div class="page sd-page${view.tab === 'long' ? ' sd-page-long' : ''}">
            ${pageHeader('Space', '<a class="btn btn-secondary" href="#/spaces">Back to spaces</a>', `Space ${view.spaceId}`)}
            ${renderHeader(view)}
            ${spaceTabs(view)}
            <section id="sdSpacePanel" role="tabpanel" tabindex="0" ${panelLabel}>
                ${['memory', 'long'].includes(view.tab) ? `<section class="sd-section">${view.tab === 'memory' ? tierButtons(view) : ''}<div id="sdTierPanel"${view.tab === 'memory' ? ` role="tabpanel" tabindex="0" aria-labelledby="sdTierTab-${view.tier}"` : ''}></div></section>` : ''}
                <div id="sdAuxiliary"></div>
            </section>
        </div>`;
        renderTier(view);
        renderAuxiliary(view);
        if (view.tab === 'long' && nextLongFocus) { document.getElementById('sdLongTab-' + nextLongFocus)?.focus(); nextLongFocus = ''; }
        if (view.focusTab) {
            document.getElementById('sdSpaceTab-' + view.focusTab)?.focus();
            view.focusTab = '';
        }
    }

    function preparePreload(view) {
        if (view.preloadStarted) return false;
        view.preloadStarted = true;
        view.shortLoading = view.tab === 'memory' && view.tier === 'short';
        view.midLoading = view.tab === 'memory' && view.tier === 'mid';
        view.longLoading = view.tab === 'long';
        view.rulesLoading = view.tab === 'rules';
        view.accessLoading = view.tab === 'access' && hasPermission(view, 'admin');
        view.meshReadinessLoading = meshReadinessAvailable(view);
        return true;
    }

    function startPreload(view) {
        // Only the visible section reads its data. Consolidation and Maintenance
        // already have their overview/target in space_info.
        if (view.tab === 'memory') {
            startMemory(view);
        } else if (view.tab === 'activity') startActivity(view);
        else if (view.tab === 'long') startLong(view);
        else if (view.tab === 'rules') void loadRules(view);
        else if (view.tab === 'access' && hasPermission(view, 'admin')) void loadAccess(view);
        startMeshReadiness(view);
    }

    function startMeshReadiness(view) {
        if (!view.info || view.meshReadinessStarted || !meshReadinessAvailable(view)) return;
        view.meshReadinessStarted = true;
        void loadMeshReadiness(view);
    }

    async function loadSpace(view) {
        if (view.info) invalidateCompaction(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await callTool('space_info', { space_id: view.spaceId });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return;
        if (!sessionGenerationIsCurrent(view.ctx.sessionGeneration)) return;
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

    async function loadShort(view, isCurrent = () => true) {
        const seqAtCall = ++view.shortSeq;
        view.shortLoading = true;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('live_read', { space_id: view.spaceId, ...view.shortFilters }); }
        catch { result = { status: 'error', message: 'Request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.shortSeq) return;
        if (!isCurrent()) return;
        view.shortLoading = false;
        if (!result || result.status !== 'ok' || !Array.isArray(result.notes)) {
            view.shortError = result?.message || 'Could not read notes. Previous results are retained.';
            renderTier(view); throw new Error(view.shortError);
        }
        const previous = view.shortNewestTimestamp;
        view.shortNewKeys = new Set();
        result.notes.forEach((note, index) => {
            const timestamp = Date.parse(note.timestamp);
            if (!Number.isFinite(timestamp)) return;
            if (previous !== null && timestamp > previous) view.shortNewKeys.add(String(note.filename || note.note_id || index));
            view.shortNewestTimestamp = Math.max(view.shortNewestTimestamp ?? timestamp, timestamp);
        });
        view.shortData = result; view.shortError = ''; view.shortSuccess = new Date().toISOString();
        renderTier(view);
    }

    async function loadMid(view, options = {}) {
        if (!options.preserveCompaction) invalidateCompaction(view);
        const seqAtCall = ++view.midSeq;
        view.midReadSeq += 1; view.midLoading = !view.midData;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('bank_list', { space_id: view.spaceId }); }
        catch { result = { status: 'error', message: 'Request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.midSeq || (options.isCurrent && !options.isCurrent())) return;
        view.midLoading = false;
        if (!result || result.status !== 'ok' || !Array.isArray(result.files)) {
            view.midListError = result?.message || 'Could not read the file list.';
            renderTier(view); throw new Error(view.midListError);
        }
        view.midData = result; view.midListError = ''; view.midListSuccess = new Date().toISOString();
        if (view.midSelectedFilename === null && result.files.length) view.midSelectedFilename = result.files[0].filename;
        if (view.midSelectedFilename && !result.files.some(file => file.filename === view.midSelectedFilename)) {
            view.midFileError = `Selected file ${view.midSelectedFilename} is no longer in the list. Previous content is retained; select another file.`;
            renderTier(view); return;
        }
        renderTier(view);
        if (view.midSelectedFilename) await readBankFile(view, view.midSelectedFilename, options.isCurrent);
    }

    async function readMemoryMetadata(view, isCurrent) {
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('space_info', { space_id: view.spaceId }); }
        catch { result = { status: 'error', message: 'Request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!isCurrent()) return;
        if (!result || result.status !== 'ok') {
            view.midMetadataError = result?.message || 'Could not refresh the space metadata.';
            renderTier(view); throw new Error(view.midMetadataError);
        }
        view.info = result; view.midMetadataError = ''; view.midMetadataSuccess = new Date().toISOString();
        renderTier(view);
    }

    async function refreshMemory(view, { isCurrent }) {
        const revision = view.memoryRevision, tier = view.tier;
        const current = () => isCurrent() && guarded(view, view.ctx.epoch) && view.memoryRevision === revision && view.tier === tier;
        if (!current()) return { skipped: true };
        view.memoryReadRevision = revision;
        const initial = view.initialMemoryRead; view.initialMemoryRead = false;
        try {
            if (tier === 'short') await loadShort(view, current);
            else {
                const reads = [loadMid(view, { preserveCompaction: true, isCurrent: current })];
                if (!initial) reads.push(readMemoryMetadata(view, current));
                const results = await Promise.allSettled(reads);
                const failure = results.find(result => result.status === 'rejected');
                if (failure) throw failure.reason;
            }
        } catch (error) {
            if (!current()) return { skipped: true };
            throw error;
        }
        return current() ? { follow: true } : { skipped: true };
    }

    function requestMemoryRefresh(view) {
        if (!guarded(view, view.ctx.epoch) || !sessionGenerationIsCurrent(view.ctx.sessionGeneration) || !PortalRefresh.state().available) return;
        const requested = ++view.memoryRequested;
        if (PortalRefresh.state().busy) {
            void PortalRefresh.refresh().catch(() => {}).then(() => {
                if (guarded(view, view.ctx.epoch) && sessionGenerationIsCurrent(view.ctx.sessionGeneration) && requested === view.memoryRequested && view.memoryReadRevision !== view.memoryRevision && PortalRefresh.state().available) void PortalRefresh.refresh().catch(() => {});
            });
        } else void PortalRefresh.refresh().catch(() => {});
    }

    function startMemory(view) {
        view.midMetadataSuccess = new Date().toISOString();
        PortalRefresh.register({ refresh: options => refreshMemory(view, options), canAuto: () => view.tab === 'memory' && guarded(view, view.ctx.epoch) }, view.ctx);
        if (!PortalRefresh.state().available) {
            view.shortError = view.midListError = 'Refresh unavailable. Sign out and sign in again.';
            view.shortLoading = view.midLoading = false; renderTier(view); return;
        }
        requestMemoryRefresh(view);
    }

    function captureShortDraft(view) {
        for (const [name, id] of [['limit', 'sdShortLimit'], ['category', 'sdShortCategory'], ['agent', 'sdShortAgent'], ['since', 'sdShortSince']]) {
            const input = document.getElementById(id); if (input) view.shortDraft[name] = input.value;
        }
    }

    function applyShortFilters(view) {
        captureShortDraft(view);
        const draft = view.shortDraft, date = draft.since ? new Date(draft.since) : null;
        if (date && !Number.isFinite(date.getTime())) {
            memoryHtml(document.getElementById('sdShortFilterError'), 'Enter a valid local date and time.'); return;
        }
        const applied = { limit: Math.min(500, Math.max(1, Math.floor(Number(draft.limit) || 50))), category: CATEGORIES.includes(draft.category) ? draft.category : '', agent: String(draft.agent).trim(), since: date ? date.toISOString() : '' };
        if (JSON.stringify(applied) !== JSON.stringify(view.shortFilters)) { view.shortNewestTimestamp = null; view.shortNewKeys = new Set(); }
        view.shortFilters = applied; view.memoryRevision++; view.shortSeq++;
        memoryHtml(document.getElementById('sdShortFilterError'), ''); requestMemoryRefresh(view);
    }

    function invalidateCompaction(view) {
        view.compactSeq += 1;
        if (view.compactApplying) return;
        view.compactDry = null;
        view.compactResult = null;
        view.compacting = false;
    }

    async function runCompaction(view, dryRun, epochAtCallOverride) {
        if (!hasPermission(view, 'manage') || view.compacting) return false;
        if (!dryRun && (!view.compactDry || view.compactDry.status !== 'ok' || view.compactResult !== view.compactDry)) return false;
        const laneSeq = dryRun ? ++view.compactSeq : ++view.compactApplySeq;
        view.compacting = true;
        view.compactApplying = !dryRun;
        view.compactDry = null;
        view.compactResult = { status: 'loading' };
        renderTier(view);
        const epochAtCall = epochAtCallOverride ?? view.ctx.epoch;
        let result;
        try {
            result = await callTool('bank_compact', { space_id: view.spaceId, dry_run: dryRun });
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall)) return false;
        if ((dryRun && laneSeq !== view.compactSeq) || (!dryRun && laneSeq !== view.compactApplySeq)) return false;
        if (result && result.status === 'ok' && result.space_id !== view.spaceId) {
            result = { status: 'error', message: 'Compaction target did not match this space.' };
        }
        view.compacting = false;
        view.compactApplying = false;
        view.compactResult = result;
        if (dryRun && result && result.status === 'ok') view.compactDry = result;
        renderTier(view);
        if (!dryRun && result && (result.status === 'ok' || result.status === 'partial')) {
            if (result.status === 'ok') showToast('ok', 'Compaction applied.');
            try { await loadMid(view, { preserveCompaction: true }); }
            catch {
                // The reader retains its error; a read failure cannot undo the applied result.
            }
            if (!guarded(view, epochAtCall)) return false;
        }
        return true;
    }

    function confirmCompact(view) {
        if (!hasPermission(view, 'manage') || view.compacting) return;
        if (!view.compactDry || view.compactDry.status !== 'ok' || view.compactResult !== view.compactDry) {
            showToast('error', 'Run a successful compaction dry run first.');
            return;
        }
        const captured = view.spaceId;
        const epochAtOpen = view.ctx.epoch;
        showModal(
            'Compact files',
            `<p class="body-small">Rewrite oversized Memory Bank files in <code>${safe(captured)}</code> using the configured language model. Older material is summarized; secondary detail may be lost.</p><p class="body-small">A preimage is saved before changes are applied. Shared Project Mesh spaces cannot be compacted.</p>`,
            'Compact files',
            async () => {
                if (!guarded(view, epochAtOpen) || view.spaceId !== captured) return false;
                return runCompaction(view, false, epochAtOpen);
            },
        );
    }

    async function loadLong(view, includeGraph = false) {
        const seqAtCall = ++view.longSeq;
        view.longLoading = true; renderTier(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('graph_status', { space_id: view.spaceId, include_graph: includeGraph }); }
        catch { result = { status: 'error', message: 'Long status request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view) || seqAtCall !== view.longSeq) return;
        view.longLoading = false;
        if (!result || result.status !== 'ok') {
            view.longFailure = result || { status: 'error', message: 'Long status is unavailable.' };
            view.longError = view.longFailure.message || 'Long status is unavailable.';
            if ((!view.longData || view.longData.status !== 'ok') && result && typeof result === 'object' && (Object.hasOwn(result, 'mid_automation') || Object.hasOwn(result, 'mid_archive_projection'))) view.longData = result;
        }
        else { view.longData = result; view.longSuccess = new Date().toISOString(); view.longError = ''; view.longFailure = null; }
        renderTier(view);
    }

    async function loadOntologyCatalog(view) {
        const state = view.ontology, seq = ++state.seq;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('ontology_list', { space_id: view.spaceId }); }
        catch { result = { status: 'error', message: 'Ontology catalog request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view) || seq !== state.seq) return;
        if (result?.status !== 'ok' || !Array.isArray(result.ontologies)) state.error = result?.message || 'Ontology catalog is unavailable.';
        else { state.catalog = result; state.success = new Date().toISOString(); state.error = ''; }
        paintLong(view);
    }

    async function loadOntologyDefinition(view) {
        const state = view.ontology, name = document.getElementById('sdOntologySelect')?.value;
        if (!state.catalog?.ontologies.some(item => item.name === name)) return;
        const seq = ++state.definitionSeq; state.validation = null;
        if (state.definition?.name !== name) { state.definition = null; state.definitionSuccess = null; }
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('ontology_get', { space_id: view.spaceId, name }); }
        catch { result = { status: 'error', message: 'Ontology definition request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view) || seq !== state.definitionSeq) return;
        if (result?.status !== 'ok' || typeof result.content !== 'string') state.definitionError = result?.message || 'Ontology definition is unavailable.';
        else { state.definition = result; state.definitionSuccess = new Date().toISOString(); state.definitionError = ''; }
        paintLong(view);
    }

    async function validateOntology(view) {
        const state = view.ontology, definition = state.definition;
        if (!definition || state.validating) return;
        state.validating = true;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('ontology_validate', { space_id: view.spaceId, content_yaml: definition.content }); }
        catch { result = { status: 'error', message: 'Ontology validation request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view)) return;
        state.validating = false;
        if (state.definition !== definition) return;
        state.validation = result || { status: 'error', message: 'No validation result returned.' }; state.validationAt = new Date().toISOString(); paintLong(view);
    }

    async function loadDocuments(view) {
        const state = view.documents, seq = ++state.seq;
        const epochAtCall = view.ctx.epoch;
        const filters = { ...state.filters };
        state.loading = true; paintDocuments(view);
        let result;
        try { result = await callTool('long_document_list', { space_id: view.spaceId, limit: 50, offset: state.offset, ...filters }); }
        catch { result = { status: 'error', message: 'Document catalog request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view) || seq !== state.seq) return;
        state.loading = false;
        if (result?.status !== 'ok' || !Array.isArray(result.documents)) state.error = result?.message || 'Document catalog is unavailable.';
        else {
            state.list = result; state.listFilters = filters; state.success = new Date().toISOString(); state.error = '';
            if (!result.documents.length && state.selected) closeDocument(view);
        }
        paintLong(view);
    }

    async function loadDocument(view, includeContent = false) {
        const state = view.documents, selected = state.selected;
        if (!selected || (includeContent ? state.contentLoading : state.detailLoading)) return;
        const seq = ++state.detailSeq;
        const args = { space_id: view.spaceId, include_content: includeContent, ...(selected.document_id ? { document_id: selected.document_id } : { source_path: selected.source_path }) };
        const epochAtCall = view.ctx.epoch;
        if (includeContent) state.contentLoading = true;
        else state.detailLoading = true;
        paintDocuments(view);
        let result;
        try { result = await callTool('long_document_get', args); }
        catch { result = { status: 'error', message: 'Document request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!longOwned(view) || seq !== state.detailSeq || state.selected !== selected) return;
        state.detailLoading = state.contentLoading = false;
        const error = result?.status !== 'ok' || !result.document;
        if (includeContent) {
            state.contentError = error ? result?.message || 'Document content is unavailable.' : '';
            if (!error) { state.content = result; state.contentSuccess = new Date().toISOString(); }
        } else {
            state.detailError = error ? result?.message || 'Document metadata is unavailable.' : '';
            if (!error) { state.detail = result; state.detailSuccess = new Date().toISOString(); }
        }
        paintLong(view);
    }

    async function readIngestDetail(view, isCurrent) {
        const state = view.ingestions, jobId = state.selected;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('long_ingest_status', { space_id: view.spaceId, job_id: jobId }); }
        catch { result = { status: 'error', message: 'Ingestion status request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (!isCurrent() || state.selected !== jobId) return;
        if (!result || result.status === 'error' || (result.status !== 'not_found' && result.job_id !== jobId)) {
            state.detailError = result?.message || 'Ingestion status is unavailable.'; paintLong(view); throw new Error(state.detailError);
        }
        state.detail = result; state.detailSuccess = new Date().toISOString(); state.detailError = ''; paintLong(view);
    }

    async function refreshIngestions(view, { isCurrent }) {
        const state = view.ingestions, revision = state.revision;
        state.readRevision = revision;
        const current = () => isCurrent() && longOwned(view) && state.revision === revision;
        const explicitDetail = state.checkDetail; state.checkDetail = false;
        // The archive scope is always explicit; the default stays the primary memory.
        const archive = state.archive === true;
        const args = { space_id: view.spaceId, limit: 50, offset: state.offset, ...state.filters, ...(archive ? { archive: true } : {}) };
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('long_ingest_list', args); }
        catch { result = { status: 'error', message: 'Ingestion list request failed.' }; }
        if (!guarded(view, epochAtCall)) return { skipped: true };
        if (!current()) return { skipped: true };
        if (archive && result?.status === 'error' && result.reason === 'archive_not_configured') {
            state.list = null; state.notConfigured = true; state.error = ''; state.success = new Date().toISOString();
            state.selected = state.detail = null; state.detailError = ''; state.detailSuccess = null; state.detailOutsidePage = false;
            paintLong(view);
            return current() ? { follow: false } : { skipped: true };
        }
        state.notConfigured = false;
        if (result?.status !== 'ok' || !Array.isArray(result.jobs)) {
            state.error = result?.message || 'Ingestion list is unavailable.'; paintLong(view); throw new Error(state.error);
        }
        state.list = result; state.listFilters = { ...state.filters }; state.success = new Date().toISOString(); state.error = '';
        if (archive && state.detail && ingestPage(result) && result.count === 0 && result.total === 0 && result.offset === 0) {
            // An archive job has no status read: keep its last read, flagged,
            // and stop following it instead of discarding it (#651 R1 F3).
            state.detailOutsidePage = true; paintLong(view);
            return current() ? { follow: false } : { skipped: true };
        }
        if (ingestPage(result) && result.count === 0 && result.total === 0 && result.offset === 0) {
            const inspectorHadFocus = document.getElementById('sdIngestDetail')?.contains(document.activeElement);
            state.selected = state.detail = null;
            state.checkDetail = false; state.detailError = ''; state.detailSuccess = null;
            state.revision++; state.readRevision = state.revision;
            paintLong(view);
            if (inspectorHadFocus) document.getElementById('sdIngestStatus')?.focus();
            const emptyRevision = state.revision;
            const emptyCurrent = () => isCurrent() && longOwned(view) && state.revision === emptyRevision;
            if (!archive && state.cancelPending && state.cancelResult) await readIngestCancelOutcome(view, emptyCurrent);
            return emptyCurrent() ? { follow: false } : { skipped: true };
        }
        const selected = result.jobs.find(job => job?.job_id === state.selected);
        if (selected) { state.detail = selected; state.detailSuccess = state.success; state.detailError = ''; }
        // Status and cancellation tools address the primary memory only: an
        // archive job is read solely through its archive-scoped list, so only
        // active jobs on the listed page are followed.
        state.detailOutsidePage = archive && !!state.selected && !selected;
        paintLong(view);
        if (archive) return current() ? { follow: result.jobs.some(ingestActive) } : { skipped: true };
        let cancelStatusRead = false;
        if (state.cancelPending && state.cancelResult) {
            const cancelledJob = result.jobs.find(job => job?.job_id === state.cancelResult.job_id);
            if (cancelledJob) applyIngestCancelOutcome(view, cancelledJob);
            else { cancelStatusRead = true; await readIngestCancelOutcome(view, current); }
        }
        if (!cancelStatusRead && current() && state.selected && (explicitDetail || (!selected && ingestActive(state.detail)))) await readIngestDetail(view, current);
        return current() ? { follow: true } : { skipped: true };
    }

    function requestIngestions(view) {
        if (!longOwned(view) || !PortalRefresh.state().available) return;
        const state = view.ingestions;
        if (PortalRefresh.state().busy) {
            void PortalRefresh.refresh().catch(() => {}).then(() => {
                if (longOwned(view) && state.readRevision !== state.revision && PortalRefresh.state().available) void PortalRefresh.refresh().catch(() => {});
            });
        } else void PortalRefresh.refresh().catch(() => {});
    }

    function startLong(view) {
        if (view.longPanel === 'jobs') {
            PortalRefresh.register({ refresh: options => refreshIngestions(view, options), canAuto: () => ingestActive(view.ingestions.detail) || !!view.ingestions.list?.jobs.some(ingestActive) }, view.ctx);
            if (!PortalRefresh.state().available) { view.ingestions.error = 'Refresh unavailable. Sign out and sign in again.'; paintLong(view); return; }
            requestIngestions(view);
        } else refreshLongPanel(view);
    }

    function refreshLongPanel(view) {
        if (!longOwned(view)) return;
        if (view.longPanel === 'jobs') requestIngestions(view);
        else if (view.longPanel === 'documents') void loadDocuments(view);
        else {
            void loadLong(view, view.longPanel === 'graph');
            if (view.longPanel === 'ontology') void loadOntologyCatalog(view);
        }
    }

    function confirmIngestCancel(view, requestedJobId) {
        const state = view.ingestions, jobId = requestedJobId || state.selected;
        if (state.archive) return; // Cancellation addresses the primary memory only, never an archive job.
        if (!hasPermission(view, 'write') || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return;
        showModal('Cancel ingestion', `<p>Request cancellation of job <code>${safe(jobId)}</code> in space <code>${safe(view.spaceId)}</code>?</p><p>A running job stops cooperatively. This is not an immediate rollback guarantee.</p>`, 'Request cancellation', async () => {
            if (!longOwned(view) || !hasPermission(view, 'write') || !ingestActive(ingestJob(view, jobId)) || state.cancelBusy) return false;
            state.cancelBusy = true; paintLong(view);
            const epochAtCall = view.ctx.epoch;
            let result;
            try { result = await callTool('long_ingest_cancel', { space_id: view.spaceId, job_id: jobId }); }
            catch { result = { status: 'unknown', message: 'Cancellation was not confirmed. Check status.' }; }
            if (!guarded(view, epochAtCall)) return false;
            if (!longOwned(view)) return false;
            state.cancelBusy = false;
            state.cancelResult = { ...(result || { status: 'unknown', message: 'Cancellation was not confirmed.' }), job_id: jobId, requested_at: new Date().toISOString() };
            state.cancelPending = ['cancelling', 'unknown'].includes(state.cancelResult.status); paintLong(view);
            state.revision++; requestIngestions(view);
            return true;
        });
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

    async function loadMeshReadiness(view) {
        if (!meshReadinessAvailable(view)) return;
        const seqAtCall = ++view.meshReadinessSeq;
        view.meshReadinessLoading = true;
        view.meshReadinessError = '';
        updateMeshReadiness(view);
        const epochAtCall = view.ctx.epoch;
        let result;
        try {
            result = await meshAdminSourceReadiness(view.spaceId);
        } catch {
            result = { status: 'error', message: 'Request failed.' };
        }
        if (!guarded(view, epochAtCall) || !meshReadinessAvailable(view)) return;
        if (seqAtCall !== view.meshReadinessSeq) return;
        view.meshReadinessLoading = false;
        const source = authoritativeMeshSource(view, result);
        view.meshReadiness = source;
        view.meshReadinessError = source
            ? ''
            : String(result && result.message || 'Project Mesh readiness is unavailable.');
        updateMeshReadiness(view);
    }

    async function readBankFile(view, filename, isCurrent = () => true) {
        const files = view.midData && Array.isArray(view.midData.files) ? view.midData.files : [];
        const file = files.find(item => item.filename === filename);
        if (!file) return;
        const seqAtCall = ++view.midReadSeq;
        const epochAtCall = view.ctx.epoch;
        let result;
        try { result = await callTool('bank_read', { space_id: view.spaceId, filename: file.filename }); }
        catch { result = { status: 'error', message: 'Request failed.' }; }
        if (!guarded(view, epochAtCall)) return;
        if (seqAtCall !== view.midReadSeq || view.midSelectedFilename !== filename || !isCurrent()) return;
        if (!result || result.status !== 'ok' || result.filename !== filename) {
            view.midFileError = result?.message || 'Could not read the selected file. Previous content is retained.';
            renderTier(view); throw new Error(view.midFileError);
        }
        const large = Number(result.size) > 409600 ? attentionBanner('Large file', 'This preview exceeds 400 KB and may be slow to inspect.') : '';
        const note = result.note ? `${serverMessage(result.note)}<a class="sd-link" href="#/operator/maintenance">Run Repair (dry-run)</a>` : '';
        view.midPreviewHtml = `${large}${note}${renderMarkdown(result.content)}`;
        view.midFileData = { ...result, last_modified: file.last_modified }; view.midFileError = ''; view.midFileSuccess = new Date().toISOString();
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
            showToast(result.errors > 0 ? 'error' : 'ok', result.errors > 0
                ? `Indexing incomplete: ${pushed} file(s) indexed, ${result.errors} error(s).`
                : `Current MID indexing finished: ${pushed} file(s) indexed.`);
            if (view.tab === 'long') await loadLong(view, view.longPanel === 'graph');
            else renderTier(view);
            if (!guarded(view, epochAtCall)) return false;
            return true;
        } else {
            showModal('Current MID indexing refused', panel(serverMessage(result.message) || stateError({ title: 'The server refused or failed this operation.' })));
            renderTier(view);
            return false;
        }
    }

    function confirmGraphPush(view) {
        if (!hasPermission(view, 'write') || view.longMutating) return;
        const epochAtOpen = view.ctx.epoch;
        showModal(
            'Index current MID files',
            '<p class="body-small">Index the current MID files in the documentary graph. This action is separate from automatic archiving of originals retained during compaction.</p><p class="body-small">Volatile bank files are not included.</p>',
            'Index current MID files',
            async () => {
                if (!guarded(view, epochAtOpen)) return false;
                return graphPush(view);
            },
        );
    }

    function confirmConsolidate(view) {
        if (!hasPermission(view, 'write') || view.consolidating) return;
        openConsolidationLauncher({
            spaces: [view.info],
            lanes: [{ ...view.info.consolidation_queue, space_id: view.spaceId }],
            spaceId: view.spaceId, ctx: view.ctx,
            onSubmitted: () => {
                if (guarded(view, view.ctx.epoch)) AdminRouter.go(`/spaces/${encodeURIComponent(view.spaceId)}/consolidation`);
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
        const accessPending = Object.hasOwn(result, 'access_grants_pending')
            ? keyValue(
                'Access grants pending',
                result.access_grants_pending === null ? 'null' : result.access_grants_pending,
            )
            : '';
        const accessRecoveryBoundary = String(recovery.action || '').includes('recover_access_grants=True')
            ? '<p class="form-hint">Grant-recovery retry is MCP/CLI-only. This console never sends recover_access_grants and will not retry automatically.</p>'
            : '';
        return `<div class="sd-delete-recovery" data-recovery-required="true">
            <div class="sd-banner sd-banner--error" role="alert">${icon('alert')}<div>
                <strong>Space deletion incomplete — recovery required (not successful)</strong>
                <p>${safe(result.message)}</p>
            </div></div>
            <div class="sd-meta-row">
                ${keyValue('Files total', result.files_total)}
                ${keyValue('Files deleted', result.files_deleted)}
                ${keyValue('Marker preserved', markerPreserved)}
                ${accessPending}
                ${keyValue('Recovery retry safe', retrySafe)}
            </div>
            <div class="form-group"><span class="micro-label">Failed keys</span>${failedKeysHtml}</div>
            <div class="form-group"><span class="micro-label">Recovery action</span><p>${safe(recovery.action)}</p></div>
            ${accessRecoveryBoundary}
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
            bodyHtml: `${warning}<p>Permanently deletes this space and its stored data, then removes the space from every token allowlist. Reusing the same ID never restores previous access.</p>`,
            onConfirm: async () => {
                const epochAtCall = view.ctx.epoch;
                const result = await callTool('space_delete', { space_id: view.spaceId, confirm: true });
                if (!guarded(view, epochAtCall)) return false;
                if (result.status === 'partial' && result.recovery_required === true) {
                    showSpaceDeleteRecovery(result);
                    return false;
                }
                if (result.status === 'deleted' || result.status === 'ok') {
                    const filesDeleted = Number(result.files_deleted || 0);
                    const grantsRemoved = Number(result.access_grants_removed || 0);
                    showToast('ok', `Space deleted (${filesDeleted} files, ${grantsRemoved} token grants removed).`);
                    AdminRouter.go('/spaces');
                    return true;
                }
                if (result.status === 'grants_cleaned') {
                    const grantsRemoved = Number(result.access_grants_removed || 0);
                    showToast('ok', `Access grants cleaned (${grantsRemoved} token grants). The space was not deleted by this result.`);
                    return false;
                }
                if (result.status === 'not_found') {
                    showToast('error', (result.message || 'Space not found.') + ' If grant recovery is required, it is MCP/CLI-only; this console never sends recover_access_grants.');
                    return false;
                }
                showToast('error', result.message || 'Space deletion failed.');
                return false;
            },
        });
    }

    function getView() {
        return currentView && currentView.info ? currentView : null;
    }

    registerAction('sd-long-panel', data => {
        const view = getView();
        if (!view || !['overview', 'ontology', 'documents', 'jobs', 'graph'].includes(data.panel)) return;
        nextLongFocus = data.panel;
        AdminRouter.go(`/spaces/${encodeURIComponent(view.spaceId)}/long/${data.panel}`);
    });
    registerAction('sd-long-refresh', () => { const view = getView(); if (view?.tab === 'long') refreshLongPanel(view); });
    registerAction('sd-ontology-load', () => { const view = getView(); if (view?.longPanel === 'ontology') void loadOntologyDefinition(view); });
    registerAction('sd-ontology-validate', () => { const view = getView(); if (view?.longPanel === 'ontology') void validateOntology(view); });
    registerAction('sd-documents-apply', () => {
        const view = getView(); if (view?.longPanel !== 'documents' || !longOwned(view)) return;
        view.documents.filters = { query: document.getElementById('sdDocumentQuery').value.trim(), status: document.getElementById('sdDocumentStatus').value.trim() }; view.documents.offset = 0; void loadDocuments(view);
    });
    registerAction('sd-documents-clear', () => {
        const view = getView(); if (view?.longPanel !== 'documents' || !longOwned(view)) return;
        view.documents.filters = { query: '', status: '' }; view.documents.offset = 0;
        const query = document.getElementById('sdDocumentQuery'), status = document.getElementById('sdDocumentStatus');
        if (query) { query.value = ''; query.focus({ preventScroll: true }); }
        if (status) status.value = '';
        void loadDocuments(view);
    });
    registerAction('sd-documents-page', data => {
        const view = getView(); if (view?.longPanel !== 'documents' || !view.documents.list || !longOwned(view)) return;
        const state = view.documents;
        if (state.loading || !documentPageUsable(state.list) || !['-1', '1'].includes(data.step)) return;
        const offset = state.list.offset + (data.step === '-1' ? -50 : 50);
        if (offset < 0 || offset >= state.list.total_count) return;
        state.offset = offset; void loadDocuments(view);
    });
    registerAction('sd-documents-first', () => {
        const view = getView(); if (view?.longPanel !== 'documents' || !longOwned(view)) return;
        const state = view.documents;
        if (state.loading || !documentFirstPageAvailable(state.list)) return;
        state.offset = 0;
        document.getElementById('sdDocumentQuery')?.focus({ preventScroll: true });
        void loadDocuments(view);
    });
    registerAction('sd-document-inspect', data => {
        const view = getView(); if (view?.longPanel !== 'documents') return;
        const state = view.documents, selected = state.list?.documents.find(item => documentKey(item) === data.key);
        if (!selected || (!selected.document_id && !selected.source_path)) return;
        state.selected = selected; state.detail = state.content = null;
        state.detailSuccess = state.contentSuccess = null; state.detailError = state.contentError = '';
        state.detailLoading = state.contentLoading = false;
        paintLong(view); void loadDocument(view);
        if (!state.modal) document.getElementById('sdDocumentInspectorTitle')?.focus({ preventScroll: true });
    });
    registerAction('sd-document-close', () => { const view = getView(); if (view?.longPanel === 'documents') closeDocument(view); });
    registerAction('sd-document-retry', () => { const view = getView(); if (view?.longPanel === 'documents') void loadDocument(view); });
    registerAction('sd-document-content', () => { const view = getView(); if (view?.longPanel === 'documents' && view.documents.detail) void loadDocument(view, true); });
    registerAction('sd-ingest-apply', () => {
        const view = getView(); if (view?.longPanel !== 'jobs') return;
        const state = view.ingestions; state.filters = { status: document.getElementById('sdIngestStatus').value, batch_id: document.getElementById('sdIngestBatch').value.trim() }; state.offset = 0; state.selected = null; state.detail = null; state.checkDetail = false; state.revision++; requestIngestions(view);
        paintLong(view);
    });
    registerAction('sd-ingest-clear', () => {
        const view = getView(); if (view?.longPanel !== 'jobs') return;
        document.getElementById('sdIngestStatus').value = ''; document.getElementById('sdIngestBatch').value = '';
        const state = view.ingestions; state.filters = { status: '', batch_id: '' }; state.offset = 0; state.selected = null; state.detail = null; state.checkDetail = false; state.revision++; requestIngestions(view);
        paintLong(view); document.getElementById('sdIngestStatus').focus();
    });
    registerAction('sd-ingest-first', () => {
        const view = getView(); if (view?.longPanel !== 'jobs') return;
        const state = view.ingestions;
        if (state.error || !ingestPage(state.list, true)) return;
        state.offset = 0; state.revision++; requestIngestions(view);
        document.getElementById('sdIngestStatus')?.focus();
    });
    registerAction('sd-ingest-cancel-check', () => {
        const view = getView(); if (view?.longPanel !== 'jobs' || !view.ingestions.cancelResult || view.ingestions.cancelBusy) return;
        view.ingestions.cancelPending = true; view.ingestions.revision++; requestIngestions(view);
    });
    registerAction('sd-ingest-page', data => {
        const view = getView(); if (view?.longPanel !== 'jobs' || !view.ingestions.list) return;
        const state = view.ingestions;
        if (state.error || !ingestPage(state.list) || state.list.count === 0 || !['-1', '1'].includes(data.step)) return;
        const offset = state.list.offset + (data.step === '-1' ? -50 : 50);
        if (offset < 0 || offset >= state.list.total) return;
        state.offset = offset; state.revision++; requestIngestions(view);
    });
    registerAction('sd-ingest-inspect', data => {
        const view = getView(); if (view?.longPanel !== 'jobs') return;
        const state = view.ingestions, job = state.list?.jobs.find(item => item?.job_id === data.jobId);
        if (!job || typeof job.job_id !== 'string' || !job.job_id) return;
        state.selected = job.job_id; state.detail = job; state.detailSuccess = state.success; state.detailError = ''; state.detailOutsidePage = false; state.revision++; paintLong(view);
        const inspector = document.getElementById('sdIngestDetail');
        if (inspector?.style) inspector.style.scrollMarginTop = `${(document.getElementById('portalTopbar')?.getBoundingClientRect().height || 0) + 12}px`;
        inspector?.focus({ preventScroll: true }); inspector?.scrollIntoView?.({ block: 'start' });
    });
    registerAction('sd-ingest-close', () => {
        const view = getView(); if (view?.longPanel !== 'jobs') return;
        const state = view.ingestions, jobId = state.selected;
        state.selected = state.detail = null; state.checkDetail = false; state.detailError = ''; state.detailSuccess = null; state.detailOutsidePage = false; state.revision++; paintLong(view);
        const list = document.getElementById('sdIngestList');
        [...(list?.querySelectorAll('[data-action="sd-ingest-inspect"]') || [])].find(button => button.dataset.jobId === jobId)?.focus();
    });
    registerAction('sd-ingest-scope', data => {
        const view = getView(); if (view?.longPanel !== 'jobs' || !longOwned(view) || !['documents', 'archive'].includes(data.scope)) return;
        const state = view.ingestions, archive = data.scope === 'archive';
        if (state.archive === archive) return;
        // Never show one memory's jobs under the other scope while the next read runs.
        Object.assign(state, { archive, offset: 0, selected: null, detail: null, checkDetail: false, detailError: '', detailSuccess: null, detailOutsidePage: false, list: null, listFilters: null, success: null, error: '', notConfigured: false });
        state.revision++; paintLong(view); requestIngestions(view);
    });
    registerAction('sd-ingest-check', () => { const view = getView(); if (view?.longPanel === 'jobs' && view.ingestions.selected) { view.ingestions.checkDetail = true; view.ingestions.revision++; requestIngestions(view); } });
    registerAction('sd-ingest-cancel', data => { const view = getView(); if (view?.longPanel === 'jobs') confirmIngestCancel(view, data?.jobId); });

    registerAction('sd-activity-refresh', () => {
        const view = getView();
        if (view?.tab === 'activity' && longOwned(view) && PortalRefresh.state().available) void PortalRefresh.refresh().catch(() => {});
    });
    registerAction('sd-activity-open', data => {
        const view = getView();
        if (!view || view.tab !== 'activity' || !longOwned(view)) return;
        const owner = { spaceId: view.spaceId, sessionGeneration: view.ctx.sessionGeneration };
        if (data.kind === 'consolidation') {
            const { running, queued, latest } = activityLaneJobs(view.activity.lane.value);
            if (!data.jobId || ![running, view.activity.lane.value?.manual_compaction, ...queued, ...latest].some(job => job?.job_id === data.jobId)) return;
            pendingHandoff = { ...owner, kind: 'consolidation', jobId: data.jobId };
            AdminRouter.go(`/spaces/${encodeURIComponent(view.spaceId)}/consolidation`);
        } else if (data.kind === 'documents' || data.kind === 'archive') {
            const source = data.kind === 'archive' ? view.activity.archive : view.activity.docs;
            const shown = [...activityJobs(source.value), activityObject(source.value?.latest?.jobs?.[0])];
            const job = data.jobId ? shown.find(item => item?.job_id === data.jobId) : null;
            if (data.jobId && !job) return;
            pendingHandoff = { ...owner, kind: 'ingest', archive: data.kind === 'archive', jobId: job ? job.job_id : null, job, at: source.at };
            AdminRouter.go(`/spaces/${encodeURIComponent(view.spaceId)}/long/jobs`);
        }
    });

    registerAction('sd-select-tier', data => {
        const view = getView();
        if (!view || view.tab !== 'memory' || !['short', 'mid'].includes(data.tier)) return;
        if (data.tier === view.tier) return;
        if (view.tier === 'short') captureShortDraft(view);
        view.memoryRevision++; view.shortSeq++; view.midSeq++; view.midReadSeq++;
        view.tier = data.tier;
        history.replaceState(null, '', `#/spaces/${encodeURIComponent(view.spaceId)}/memory/${data.tier}`);
        document.querySelectorAll('.sd-tier-tab').forEach(tab => {
            const selected = tab.dataset.tier === view.tier;
            tab.setAttribute('aria-selected', String(selected));
            tab.classList.toggle('active', selected);
            tab.tabIndex = selected ? 0 : -1;
        });
        const tierPanel = document.getElementById('sdTierPanel');
        if (tierPanel) tierPanel.setAttribute('aria-labelledby', `sdTierTab-${view.tier}`);
        renderTier(view);
        requestMemoryRefresh(view);
    });
    registerAction('sd-select-tab', data => {
        const view = getView();
        if (!view || !['memory', 'activity', 'consolidation', 'long', 'rules', 'access', 'backups', 'maintenance'].includes(data.tab)) return;
        nextTabFocus = data.tab;
        const suffix = data.tab === 'memory' ? 'memory/short' : data.tab === 'long' ? 'long/overview' : data.tab;
        AdminRouter.go(`/spaces/${encodeURIComponent(view.spaceId)}/${suffix}`);
    });
    registerAction('sd-refresh-space', () => { const view = currentView; if (view?.tab === 'memory' && view.info) requestMemoryRefresh(view); else if (view?.tab === 'long' && view.info) refreshLongPanel(view); else if (view) loadSpace(view); });
    registerAction('sd-apply-short-filters', () => { const view = getView(); if (view?.tier === 'short') applyShortFilters(view); });
    registerAction('sd-retry-short', () => { const view = getView(); if (view) requestMemoryRefresh(view); });
    registerAction('sd-retry-mid', () => { const view = getView(); if (view) requestMemoryRefresh(view); });
    registerAction('sd-retry-long', () => { const view = getView(); if (view) refreshLongPanel(view); });
    registerAction('sd-retry-rules', () => { const view = getView(); if (view) loadRules(view); });
    registerAction('sd-retry-access', () => { const view = getView(); if (view) loadAccess(view); });
    registerAction('sd-retry-mesh-readiness', () => { const view = getView(); if (view) loadMeshReadiness(view); });
    registerAction('sd-read-bank', data => {
        const view = getView();
        if (!view || view.tier !== 'mid' || !view.midData?.files.some(file => file.filename === data.filename)) return;
        view.midSelectedFilename = data.filename; view.midReadSeq++; view.memoryRevision++;
        if (view.midFileData?.filename !== data.filename) {
            view.midFileData = null; view.midPreviewHtml = ''; view.midFileSuccess = null;
            view.midRenderedContent = null; view.midRenderedFilename = null;
            view.midFileError = '';
            memoryHtml(document.getElementById('sdBankMarkdown'), '');
        }
        renderTier(view); requestMemoryRefresh(view);
    });
    registerAction('sd-edit-rules', () => { const view = getView(); if (view) openRulesEditor(view); });
    registerAction('sd-confirm-consolidate', () => { const view = getView(); if (view) confirmConsolidate(view); });
    registerAction('sd-compact-dry', () => { const view = getView(); if (view) void runCompaction(view, true); });
    registerAction('sd-confirm-compact', () => { const view = getView(); if (view) confirmCompact(view); });
    registerAction('sd-confirm-graph-push', () => { const view = getView(); if (view) confirmGraphPush(view); });
    registerAction('sd-confirm-space-delete', () => { const view = getView(); if (view) confirmSpaceDelete(view); });

    document.addEventListener('input', event => {
        if (event.target.id !== 'sdRulesInput') return;
        const counter = document.getElementById('sdRulesCount');
        if (counter) counter.textContent = String(event.target.value.length);
        event.target.setAttribute('aria-invalid', event.target.value.length > RULES_LIMIT ? 'true' : 'false');
    });

    document.addEventListener('keydown', event => {
        const longTab = event.target.closest && event.target.closest('#sdLongTabs [role=tab]');
        if (longTab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            const tabs = [...document.querySelectorAll('#sdLongTabs [role=tab]')], index = tabs.indexOf(longTab);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault(); tabs.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; }); tabs[next].focus(); return;
        }
        const spaceTab = event.target.closest && event.target.closest('.sd-space-tab');
        if (spaceTab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            const tabs = [...document.querySelectorAll('.sd-space-tab')];
            const index = tabs.indexOf(spaceTab);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault();
            tabs.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
            tabs[next].focus();
            return;
        }
        const tab = event.target.closest && event.target.closest('.sd-tier-tab');
        if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            const tabs = [...document.querySelectorAll('.sd-tier-tab')];
            const index = tabs.indexOf(tab);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
                : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            event.preventDefault();
            tabs.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
            tabs[next].focus();
            return; // Manual activation: only Enter/Space/click may load Long.
        }
        const file = event.target.closest && event.target.closest('.sd-file-tab');
        if (!file || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const files = [...document.querySelectorAll('.sd-file-tab')];
        const index = files.indexOf(file);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? files.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + files.length) % files.length;
        event.preventDefault(); files.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; }); files[next].focus();
    });

    document.addEventListener('admin:mesh-availability', event => {
        if (!event.detail || event.detail.available !== true) return;
        const view = currentView;
        if (view) startMeshReadiness(view);
    });

    document.addEventListener('keydown', event => {
        const view = getView();
        if (event.key === 'Escape' && view?.longPanel === 'documents' && view.documents.selected && !view.documents.modal
            && document.getElementById('sdDocumentInspector')?.contains(event.target)) {
            event.preventDefault(); closeDocument(view);
        }
    });
    if (typeof window !== 'undefined') {
        window.addEventListener('resize', () => {
            const view = getView();
            if (view?.longPanel === 'documents' && view.documents.selected && longOwned(view)) paintDocuments(view);
        });
        window.addEventListener('hashchange', () => {
            if (currentView && !longOwned(currentView)) discardDocumentModal(currentView);
        });
    }

    function render(contentEl, params, ctx) {
        discardDocumentModal(currentView);
        const handoffTarget = pendingHandoff; pendingHandoff = null;
        const spaceId = params && typeof params.spaceId === 'string' ? params.spaceId : '';
        if (!SPACE_ID_RE.test(spaceId)) {
            currentView = null;
            contentEl.innerHTML = `<div class="page">${pageHeader('Invalid space id')}<div class="panel">${stateError({ title: 'Invalid space id' })}<a class="btn btn-secondary" href="#/spaces">Back to spaces</a></div></div>`;
            return;
        }
        const tier = params?.tab === 'long' ? 'long' : params && TIERS.has(params.tier) ? params.tier : 'short';
        const sessionGeneration = ctx.sessionGeneration ?? currentSessionGeneration();
        const handoff = handoffTarget?.spaceId === spaceId && handoffTarget.sessionGeneration === sessionGeneration ? handoffTarget : null;
        const view = {
            contentEl, ctx: { ...ctx, sessionGeneration }, spaceId, tier, tab: params.tab || (tier === 'long' ? 'long' : 'memory'), info: null,
            focusTab: nextTabFocus,
            shortFilters: { limit: 50, category: '', agent: '', since: '' },
            shortDraft: { limit: 50, category: '', agent: '', since: '' },
            shortRows: new Map(), shortGroups: new Map(), shortNewestTimestamp: null, shortNewKeys: new Set(),
            shortSuccess: null, shortError: '',
            memoryRevision: 0, memoryReadRevision: null, memoryRequested: 0, initialMemoryRead: true, memoryMounted: null,
            shortData: null, shortLoading: false, shortSeq: 0,
            midData: null, midLoading: false, midSeq: 0, midReadSeq: 0,
            midSelectedFilename: null, midPreviewHtml: '', midFileData: null, midTabs: new Map(),
            midListSuccess: null, midFileSuccess: null, midMetadataSuccess: null,
            midListError: '', midFileError: '', midMetadataError: '',
            longPanel: ['overview', 'ontology', 'documents', 'jobs', 'graph'].includes(params.panel) ? params.panel : 'overview', longMounted: null,
            longSuccess: null, longError: '', longFailure: null, longGraphPainted: null,
            ontology: { seq: 0, definitionSeq: 0 },
            documents: { seq: 0, detailSeq: 0, offset: 0, filters: {} },
            ingestions: { revision: 0, readRevision: null, offset: 0, filters: {}, archive: false },
            activity: { lane: activitySource(), graph: activitySource(), docs: activitySource(), archive: activitySource(), success: null, started: false, unavailable: '' },
            longData: null, longLoading: false, longMutating: false,
            longSeq: 0,
            rulesData: null, rulesLoading: false,
            accessData: null, accessLoading: false,
            meshReadiness: null, meshReadinessLoading: false,
            meshReadinessError: '', meshReadinessStarted: false, meshReadinessSeq: 0,
            preloadStarted: false, consolidating: false,
            compacting: false, compactApplying: false, compactSeq: 0, compactApplySeq: 0,
            compactDry: null, compactResult: null,
            consolidationHandoff: handoff?.kind === 'consolidation' && params.tab === 'consolidation' ? handoff.jobId : null,
        };
        if (handoff?.kind === 'ingest' && view.tab === 'long' && view.longPanel === 'jobs') {
            view.ingestions.archive = handoff.archive === true;
            if (handoff.job) Object.assign(view.ingestions, { selected: handoff.jobId, detail: handoff.job, detailSuccess: handoff.at, focusDetail: true });
        }
        nextTabFocus = '';
        currentView = view;
        contentEl.innerHTML = `<div class="page">${pageHeader(`Space: ${spaceId}`, '<a class="btn btn-secondary" href="#/spaces">Back to spaces</a>')}${panel(stateLoading('Loading space…'))}</div>`;
        loadSpace(view).catch(() => {
            if (!guarded(view, ctx.epoch)) return;
            contentEl.innerHTML = `<div class="page">${pageHeader(`Space: ${spaceId}`)}${panel(stateError({ title: "Couldn't load this space", retryAction: 'sd-refresh-space' }))}</div>`;
        });
    }

    AdminViews.register('space-detail', render);
})();
