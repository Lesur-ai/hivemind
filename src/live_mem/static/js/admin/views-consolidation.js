/**
 * Consolidation view (P8-4, issue #142) — route #/consolidation.
 *
 * Jobs-first operation view with on-demand notes-to-consolidate scan
 * (contract §4.8 K1–K6, §5.5). Real data only: every widget consumes exactly
 * the fields the real tools return (bank_consolidation_queues / _status /
 * bank_consolidate / bank_stale_spaces). Refresh triggers are load, manual
 * Refresh, after-action, and — the one bounded exception to D8 (§5.5.1) —
 * a live refresh every LIVE_REFRESH_MS while a
 * space shows a running or queued job or while the open job
 * inspector shows a running/queued job. It stops by itself when no job is
 * active, when the route epoch or the session changes, when the modal is
 * closed, and it skips network calls while the tab is hidden. The live tick
 * re-reads only the lanes painted by the last full load (explicit space_ids:
 * an in-memory registry read, never the storage-backed space scan of the
 * full load); a space created or deleted meanwhile appears at the next full
 * load. Progress bars stay snapshots labeled "as of last refresh".
 *
 * Escaping (contract §7.3.3 R1–R6): every dynamic value passes through the
 * shell esc() at its interpolation site; dataset values re-escaped when reused
 * in HTML; data-* JSON payloads are JSON.stringify then esc; server text
 * renders only through serverMessage()/textContent, never parsed. All actions
 * flow through the shell [data-action] delegate (no inline handlers, CSP-safe).
 */
(function () {
    'use strict';

    // View-local, in-memory state (contract §3.1.1 — not URL state). Persists
    // for the session like the inherited threshold behavior; never persisted.
    const state = {
        staleMode: false,
        thresholds: { minNotes: 5, minAgeDays: 5 },
        staleData: null,        // last bank_stale_spaces payload while in stale mode
        identity: {},           // cached ctx.identity (never a fresh probe)
        owner: null,            // unique session marker (token_hash); null = unproven → cache never retained
        scope: '',              // optional route spaceId; does not widen a request
        data: null, lastSuccess: null,
        sessionGeneration: null,
        request: null, pendingLoad: null, renderSeq: 0,
        liveTimer: null,        // pending lanes live-refresh timer (§5.5.1); one at a time
    };

    // §5.5.1 — live refresh period while a job is running or queued.
    // Period fixed at one minute by the owner (arbitration 2026-09-05, PR #489).
    const LIVE_REFRESH_MS = 60000;
    const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;

    function laneActive(lane) {
        return !!(lane && (lane.running_job || (typeof lane.queued_count === 'number' && lane.queued_count > 0)
            || (Array.isArray(lane.queued_jobs) && lane.queued_jobs.length)));
    }

    function anyLaneActive(data) {
        return !!(data && Array.isArray(data.lanes) && data.lanes.some(laneActive));
    }

    // Space ids painted by the last full load: the live tick re-reads exactly
    // these lanes by explicit space_ids, which the server answers from the
    // token's access rules and the in-memory queue registry (§5.5.1: no space
    // scan; authentication's own token-registry read applies as to any request).
    function laneIds(data) {
        return scopedItems(data && data.lanes).map(l => l.space_id).filter(Boolean);
    }

    function clearLive() {
        if (state.liveTimer !== null) {
            clearTimeout(state.liveTimer);
            state.liveTimer = null;
        }
    }

    // Schedule ONE lanes reload while a job is active. The tick re-checks the
    // route epoch and the session (§3.1.4), and only re-arms — without a
    // network call — while the tab is hidden.
    function scheduleLive(epoch, data) {
        clearLive();
        if (!anyLaneActive(data)) return;
        const generation = state.sessionGeneration;
        const seq = state.renderSeq;
        state.liveTimer = setTimeout(() => {
            state.liveTimer = null;
            if (AdminRouter.epoch !== epoch || !sessionActive() || !sessionGenerationIsCurrent(generation) || seq !== state.renderSeq) return;
            if (document.hidden) { scheduleLive(epoch, data); return; }
            loadLanes(epoch, laneIds(data));
        }, LIVE_REFRESH_MS);
    }

    function hasManage() {
        const perms = state.identity && Array.isArray(state.identity.permissions)
            ? state.identity.permissions : [];
        return perms.includes('manage') || perms.includes('admin');
    }

    // Modal-instance token: bumped each time this view opens a modal, so a slow
    // or out-of-order continuation drops instead of overwriting or closing a
    // NEWER modal opened on the same route. Checked
    // alongside the navigation epoch before any modal/close effect.
    let _modalOp = 0;
    let _modalSession = null;
    function beginModalOp() { _modalSession = currentSessionGeneration(); return ++_modalOp; }
    function modalOpCurrent(t) { return _modalOp === t && sessionGenerationIsCurrent(_modalSession); }

    // Stale-scan generation: a later scan (e.g. different thresholds) bumps this,
    // so an earlier scan resolving out of order drops instead of overwriting the
    // newer result / state.staleData.
    let _staleGen = 0;

    // Session-ownership boundary (§3.1.4): logout / 401 wipes the shell but does
    // not bump the route epoch, so a continuation must ALSO verify the session is
    // still active before any DOM/toast/modal effect or further batch request —
    // otherwise it could repaint privileged UI behind the login overlay, or a
    // batch could keep mutating (even under a NEW login). `livemem_auth` is
    // HttpOnly, so we read the shell's own login-overlay visibility as the signal.
    function sessionActive() {
        const ov = document.getElementById('loginOverlay');
        return !ov || ov.classList.contains('hidden');
    }

    // ───────────────────────── header / layout ─────────────────────────

    function headerActions() {
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="consol-refresh">${icon('refresh')}<span>Refresh</span></button>
            <button type="button" class="btn btn-primary btn-sm" data-action="consol-picker">${icon('plus')}<span>Consolidate notes</span></button>`;
    }

    function scopedItems(items) {
        return (Array.isArray(items) ? items : []).filter(item => item && (!state.scope || item.space_id === state.scope));
    }

    function subtitle(data) {
        const live = anyLaneActive(data)
            ? `<span class="consol-live">Live · refreshes every ${esc(String(LIVE_REFRESH_MS / 1000))} s while a job runs</span>` : '';
        const rawModel = data && data.parallelism_model;
        const model = rawModel === 'one_worker_per_space' ? '1 worker per space' : 'Worker configuration unavailable';
        const guarantee = scopedItems(data && data.lanes).map(lane => lane.guarantee).find(Boolean);
        const guaranteeBadge = guarantee ? `<span class="pill pill-neutral consol-guarantee" title="Job state lives in server memory: it does not survive a restart and history is trimmed.">${esc(String(guarantee))}</span>` : '';
        const batchSize = data && data.service_config && data.service_config.batch_size;
        const batch = Number.isFinite(batchSize) ? `<span class="consol-batch">batch size: <span class="mono-data">${esc(String(batchSize))}</span> notes</span>` : '';
        return `<p class="consol-subtitle body-small">${live}</p><details class="diagnostic-details body-small"><summary>Technical details</summary>
            <p>${model} ${guaranteeBadge} ${batch}</p><p>Worker configuration code: <code>${esc(String(rawModel ?? 'Not reported'))}</code></p></details>`;
    }

    function paintFreshness(error) {
        const el = document.getElementById('consolFreshness');
        if (!el) return;
        const updated = state.lastSuccess ? `Last updated ${renderTimestamp(state.lastSuccess)}` : '';
        el.innerHTML = error
            ? `<div class="state-degraded body-small" role="status">${statusDot('warn', 'Consolidation data is stale')} ${updated}${serverMessage(error.message || '')}</div>`
            : `<p class="body-small text-muted">${updated}</p>`;
    }

    // ───────────────────────── render entry ─────────────────────────

    function render(contentEl, params, ctx) {
        const identity = (ctx && ctx.identity) || {};
        // Confidentiality (§3.1.4) — FAIL CLOSED. The stale-scan cache may persist
        // across renders ONLY when we can positively prove the same session. The
        // one unique session identity is the authenticated token_hash; client_name
        // is explicitly NON-unique (two distinct tokens can share a name), so it
        // must never stand in as the owner marker. When token_hash is absent
        // (non-token auth, or a degraded whoami) we cannot prove sameness, so we
        // drop the cache unconditionally rather than risk repainting a prior
        // token's rows for a different operator. Server now returns token_hash for
        // token auth independent of best-effort enrichment; this stays as
        // defense-in-depth for every path that still lacks it. See PR #159 / #164.
        const owner = (identity && typeof identity.token_hash === 'string' && identity.token_hash)
            ? identity.token_hash : null;
        // The SENSITIVE state is the cached bank_stale_spaces rows (staleData):
        // drop them whenever the owner is unprovable (null) OR has changed, so
        // renderStalePanel can never repaint one operator's rows under another.
        // The staleMode toggle itself is not sensitive; only reset it on a real
        // owner change, otherwise a null-owner session (non-token auth) could
        // never keep the panel open — its own activation refresh would cancel it.
        if (owner === null || owner !== state.owner) state.staleData = null;
        if (owner !== state.owner) state.staleMode = false;
        const generation = currentSessionGeneration();
        const scope = params && typeof params.spaceId === 'string' ? params.spaceId : '';
        if (owner === null || owner !== state.owner || generation !== state.sessionGeneration || scope !== state.scope) {
            state.data = null; state.lastSuccess = null; state.staleData = null;
        }
        state.scope = scope;
        state.sessionGeneration = generation;
        state.renderSeq += 1;
        state.owner = owner;
        state.identity = identity;
        clearLive();
        const epoch = ctx ? ctx.epoch : AdminRouter.epoch;
        if (scope && !SPACE_ID_RE.test(scope)) {
            contentEl.innerHTML = `<div class="page consolidation-page">${pageHeader('Consolidation')}${panel(stateError({ title: 'Invalid space id' }))}<a href="#/consolidation">All spaces</a></div>`;
            return;
        }
        contentEl.innerHTML = `<div class="page consolidation-page">
            ${pageHeader('Consolidation', headerActions())}
            ${scope ? `<p class="consol-scope-filter body-small">Space: <strong>${esc(scope)}</strong> · <a href="#/consolidation">All spaces</a></p>` : ''}
            <div id="consolSubtitle"></div><div id="consolFreshness" class="consol-freshness"></div>
            <div id="consolLanes">${panel(stateLoading('Loading consolidation jobs…'))}</div>
            <div id="consolStale"></div>
        </div>`;
        renderStalePanel(epoch);
        if (state.data) { paintLanes(state.data); paintFreshness(); }
        loadLanes(epoch);
    }

    // ───────────────────────── lanes ─────────────────────────

    // Full load (liveIds absent — load, manual Refresh, after-action): space_ids
    // "" lets the server resolve the visible spaces (storage-backed scan).
    // Live tick (liveIds present): the painted ids only — in-memory read.
    async function loadLanes(epoch, liveIds) {
        const generation = state.sessionGeneration;
        const seq = state.renderSeq;
        const current = () => AdminRouter.epoch === epoch && sessionActive()
            && sessionGenerationIsCurrent(generation) && seq === state.renderSeq;
        if (!current()) return;
        if (state.request) { state.pendingLoad = { epoch, liveIds, generation, seq }; return; }
        clearLive();
        const request = {};
        state.request = request;
        const spaceIds = Array.isArray(liveIds) ? liveIds.join(',') : state.scope;
        let data;
        try {
            try { data = await callTool('bank_consolidation_queues', { space_ids: spaceIds }); }
            catch (e) { data = { status: 'error', message: '' }; }
            if (!current()) return;
            if (!data || data.status !== 'ok' || !Array.isArray(data.lanes)) {
                if (state.data) {
                    paintFreshness(data || {});
                    if (!['truncated', 'rate_limited', 'read_only'].includes(data && data.status)) scheduleLive(epoch, state.data);
                } else paintLanesError(data || {});
                return;
            }
            const retainedDenials = Array.isArray(liveIds) && state.data
                ? scopedItems(state.data.denied_spaces).filter(item => !liveIds.includes(item.space_id)) : [];
            data = { ...data, lanes: scopedItems(data.lanes), denied_spaces: retainedDenials.concat(scopedItems(data.denied_spaces)) };
            state.data = data;
            state.lastSuccess = new Date().toISOString();
            const sub = document.getElementById('consolSubtitle');
            if (sub) sub.innerHTML = subtitle(data);
            paintLanes(data);
            paintFreshness();
            scheduleLive(epoch, data);
        } finally {
            if (state.request === request) state.request = null;
            const pending = state.pendingLoad;
            state.pendingLoad = null;
            if (pending && pending.epoch === AdminRouter.epoch && pending.seq === state.renderSeq
                && sessionGenerationIsCurrent(pending.generation) && sessionActive()) loadLanes(pending.epoch, pending.liveIds);
        }
    }

    function paintLanesError(data) {
        const el = document.getElementById('consolLanes');
        if (!el) return;
        // §5.0 sentinels (truncated / rate_limited / read_only) are not retryable
        // errors — render the dedicated NOT-AVAILABLE state with their message.
        if (data && (data.status === 'truncated' || data.status === 'rate_limited' || data.status === 'read_only')) {
            el.innerHTML = panel(stateUnavailable(data.message));
            return;
        }
        el.innerHTML = panel(stateError({
            title: "Couldn't load consolidation jobs",
            message: data && data.message,
            retryAction: 'consol-refresh',
        }));
    }

    function progressBar(progress) {
        if (!progress) return '';
        const nd = progress.notes_done;
        const nt = progress.notes_total;
        const bd = progress.batches_done;
        const bt = progress.batches_total;
        // Keep phase/label RAW; escape once at each sink (the aria-label and the
        // visible meta) — never pre-escape, or the aria-label double-escapes.
        const phase = progress.phase ? String(progress.phase) : '';
        let pct = null;
        let label = '';
        if (Number.isFinite(nt) && nt > 0 && Number.isFinite(nd) && nd >= 0) {
            pct = Math.min(100, Math.round((nd / nt) * 100));
            label = `${nd}/${nt} notes`;
        } else if (Number.isFinite(bt) && bt > 0 && Number.isFinite(bd) && bd >= 0) {
            pct = Math.min(100, Math.round((bd / bt) * 100));
            label = `${bd}/${bt} batches`;
        }
        const barInner = pct === null
            ? `<span class="consol-bar-fill consol-bar-indeterminate"></span>`
            : `<span class="consol-bar-fill" style="width:${pct}%"></span>`;
        const meta = [phase, label].filter(Boolean).map(esc).join(' · ');
        return `<div class="consol-progress" role="img" aria-label="Progress as of last refresh: ${esc(label || phase || 'in progress')}">
            <div class="consol-bar">${barInner}</div>
            <div class="consol-progress-meta body-small">${meta}<span class="consol-asof"> · as of last refresh</span></div>
        </div>`;
    }

    function collectJobs(lanes) {
        const active = [], history = [], undated = [], unreported = [], seen = new Set();
        const add = (job, sid, fromQueue) => {
            if (!job || typeof job !== 'object') return;
            const id = typeof job.job_id === 'string' ? job.job_id : '';
            const key = sid + '/' + id;
            if (id && seen.has(key)) return;
            if (id) seen.add(key);
            const item = { ...job, space_id: sid };
            if (job.status === 'running' || job.status === 'queued') active.push(item);
            else if (!fromQueue && ['succeeded', 'failed'].includes(job.status)) {
                if (typeof job.finished_at === 'string' && Number.isFinite(Date.parse(job.finished_at))) history.push(item);
                else undated.push(item);
            }
            else unreported.push(item);
        };
        // Active registry entries win over duplicate/inconsistent history.
        lanes.forEach(lane => {
            add(lane.running_job, lane.space_id, true);
            (Array.isArray(lane.queued_jobs) ? lane.queued_jobs : []).forEach(job => add(job, lane.space_id, true));
        });
        lanes.forEach(lane => (Array.isArray(lane.latest_jobs) ? lane.latest_jobs : []).forEach(job => add(job, lane.space_id, false)));
        history.sort((a, b) => Date.parse(b.finished_at) - Date.parse(a.finished_at));
        return { active, history: history.concat(undated), unreported };
    }

    function jobRow(job) {
        const jid = String(job.job_id || '');
        const sid = String(job.space_id || '');
        const partial = job.result && (job.result.status === 'partial' || job.result.partial === true
            || (Number.isFinite(job.result.batches_completed) && Number.isFinite(job.result.batches_total)
                && job.result.batches_completed < job.result.batches_total));
        const labels = { running: 'Running', queued: 'Queued', succeeded: 'Completed', failed: 'Failed' };
        const status = Object.hasOwn(labels, job.status) ? labels[job.status] : 'Unknown';
        const severity = job.status === 'failed' ? 'error' : partial || ['running', 'queued'].includes(job.status) ? 'warn' : job.status === 'succeeded' ? 'ok' : 'neutral';
        const phase = statusDot(severity, status);
        const when = ['succeeded', 'failed'].includes(job.status) ? job.finished_at : job.started_at || job.queued_at || job.requested_at;
        const time = typeof when === 'string' && Number.isFinite(Date.parse(when)) ? renderTimestamp(when)
            : ['succeeded', 'failed'].includes(job.status) ? 'Completion time unavailable' : 'Time unavailable';
        const position = job.status === 'queued' && Number.isSafeInteger(job.queue_position) && job.queue_position >= 2
            ? ` · Position ${esc(String(job.queue_position))}` : '';
        const inspect = jid ? `<button type="button" class="btn btn-secondary btn-sm" data-action="consol-job" data-job-id="${esc(jid)}" aria-label="Inspect job ${esc(jid)}">Details</button>` : '';
        return `<article class="consol-job-row">
            <div class="consol-job-heading"><a class="consol-space-name" href="${esc('#/spaces/' + encodeURIComponent(sid))}">${esc(sid)}</a>${phase}${inspect}</div>
            <p class="consol-job-meta body-small">${esc(String(job.scope_label || 'Scope unavailable'))}${position} · ${time}</p>
            ${['running', 'queued'].includes(job.status) ? progressBar(job.progress) : ''}
            ${partial ? '<p class="consol-partial body-small">Partial completion</p>' : ''}
            ${job.error ? serverMessage(job.error) : ''}
            ${status === 'Unknown' ? `<p class="body-small">Status code: <code>${esc(String(job.status || 'Not reported'))}</code></p>` : ''}
        </article>`;
    }

    function laneActions(lane) {
        const sid = String(lane.space_id || '');
        const enc = esc(sid);
        const parts = [];
        // "My notes" (scope mine) — ALWAYS sends agent (§4.5 E4). Only offered
        // when the cached identity has a client_name to send as the agent.
        if (state.identity && state.identity.client_name) {
            parts.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="consol-mine" data-space="${enc}">Consolidate my notes</button>`);
        }
        // "All notes" (scope all agents, §4.5 E3) — stays VISIBLE but disabled
        // with a manage/admin hint for non-managers (client gate on the cached
        // identity; the server stays authoritative and the handler re-checks).
        if (hasManage()) {
            parts.push(`<button type="button" class="btn btn-secondary btn-sm" data-action="consol-all" data-space="${enc}">Consolidate all notes</button>`);
        } else {
            parts.push(`<button type="button" class="btn btn-secondary btn-sm" disabled title="Requires manage or admin permission" aria-label="Consolidate all notes (requires manage or admin permission)">Consolidate all notes</button>`);
        }
        return parts.join(' ');
    }

    function openPicker() {
        beginModalOp();
        const lanes = scopedItems(state.data && state.data.lanes);
        if (!lanes.length) { showModal('Consolidate notes', stateUnavailable('No accessible spaces loaded. Refresh to try again.')); return; }
        const options = lanes.map(lane => `<option value="${esc(lane.space_id)}">${esc(lane.space_id)}</option>`).join('');
        showModal('Consolidate notes', `<div class="consol-picker form-group"><label class="form-label" for="consolPickSpace">Space</label>
            <select id="consolPickSpace" class="form-input"><option value="">Select a space</option>${options}</select></div>
            <div id="consolPickerActions"></div>`);
        const select = document.getElementById('consolPickSpace');
        if (!select) return;
        const update = () => {
            const el = document.getElementById('consolPickerActions');
            const lane = lanes.find(item => item.space_id === select.value);
            if (el) el.innerHTML = lane ? laneActions(lane) : '';
        };
        select.onchange = update;
        if (state.scope) select.value = state.scope;
        update();
    }

    function deniedFooter(denied) {
        if (!Array.isArray(denied) || !denied.length) return '';
        const rows = denied.map(d => `<li>${copyable(String(d.space_id || ''))}${serverMessage(d.message)}</li>`).join('');
        return `<div class="consol-denied state-degraded" role="status">
            <div class="micro-label">${esc(String(denied.length))} space(s) not accessible</div>
            <ul class="consol-denied-list">${rows}</ul>
        </div>`;
    }

    function paintLanes(d) {
        const el = document.getElementById('consolLanes');
        if (!el) return;
        const focusedJob = document.activeElement && el.contains && el.contains(document.activeElement)
            ? document.activeElement.dataset && document.activeElement.dataset.jobId : null;
        const lanes = scopedItems(d.lanes);
        const denied = scopedItems(d.denied_spaces);
        if (!lanes.length && denied.length) {
            el.innerHTML = panel(stateUnavailable('Consolidation data is not accessible.')) + deniedFooter(denied);
            return;
        }
        if (!lanes.length) {
            el.innerHTML = panel(stateEmpty({ title: 'No spaces visible', hint: 'This token cannot see any consolidation lanes.' }));
            return;
        }
        const jobs = collectJobs(lanes);
        const section = (title, items, empty, hint) => panel(`<div class="panel-header"><h2>${title}</h2><span class="count-pill">${esc(String(items.length))}</span></div>
            <div class="consol-job-list">${items.length ? items.map(jobRow).join('') : stateEmpty({ title: empty, hint })}</div>`);
        const diagnostics = lanes.filter(lane => !['idle', 'running', 'queued', 'failed'].includes(lane.lane_state)
            || (lane.queued_count > 0 && !(Array.isArray(lane.queued_jobs) && lane.queued_jobs.length)))
            .map(lane => `<p class="body-small">${esc(lane.space_id)} · ${lane.queued_count > 0 ? `${esc(String(lane.queued_count))} queued; job details unavailable` : 'Consolidation state unavailable'}</p>`).join('');
        el.innerHTML = section('In progress', jobs.active, 'No jobs in progress', 'Refresh to check for newly submitted jobs.')
            + (diagnostics ? panel(diagnostics) : '')
            + section('Recent history', jobs.history, 'No completed jobs available', 'History may have been cleared by a server restart or trimmed.')
            + `<p class="consol-history-note body-small text-muted">History is held in server memory and is bounded. It may be cleared by a restart.</p>`
            + (jobs.unreported.length ? panel(`<div class="state-degraded body-small">Some jobs have an unknown state.</div>${jobs.unreported.map(jobRow).join('')}`) : '')
            + deniedFooter(denied);
        if (focusedJob && el.querySelectorAll) {
            const target = Array.from(el.querySelectorAll('[data-action="consol-job"]')).find(button => button.dataset.jobId === focusedJob);
            if (target) target.focus();
        }
    }

    // ───────────────────────── job inspector ─────────────────────────

    // Compaction is a human decision (bank_compact): a consolidation
    // never runs it. Oversized bank files are only REPORTED, as an advisory.
    function renderBankSizeAdvisory(result) {
        const items = result && Array.isArray(result.bank_size_advisory) ? result.bank_size_advisory : [];
        const rows = items.map(item => {
            if (!item || typeof item !== 'object' || typeof item.filename !== 'string'
                || !Number.isSafeInteger(item.utf8_bytes) || !Number.isSafeInteger(item.max_size)) return '';
            return `<tr><td class="mono-data">${esc(item.filename)}</td><td class="num mono-data">${esc(String(item.utf8_bytes))}</td><td class="num mono-data">${esc(String(item.max_size))}</td></tr>`;
        }).join('');
        return rows
            ? `<div class="consol-size-advisory">${statusDot('warn', 'Some bank files exceed the advisory size')}<p class="body-small">Compaction is optional. Open the space’s Memory Bank to check files for compaction.</p>${dataTable(['File', 'UTF-8 bytes', 'Advisory threshold'], rows)}</div>`
            : '';
    }

    function renderResultMetrics(result) {
        if (!result || typeof result !== 'object') return '';
        // Zero-notes short form: only when the run had zero notes.
        // A stop at the first batch leaves notes_processed at 0 with notes_total > 0
        // and must render its counters, never "nothing to do".
        if (Number(result.notes_total) === 0 && result.message) {
            return `<div class="consol-nothing"><span class="micro-label">Nothing to do</span>${serverMessage(result.message)}</div>`;
        }
        const rows = [
            ['Notes total', result.notes_total],
            ['Notes processed', result.notes_processed],
            ['Notes declared useless', result.notes_discarded_count],
            ['Notes deleted', result.notes_deleted],
            ['Notes remaining', result.notes_remaining],
            ['Failed batch', result.failed_batch],
            ['Failure reason', result.failure_reason],
            ['Bank files updated', result.bank_files_updated],
            ['Bank files created', result.bank_files_created],
            ['Bank files unchanged', result.bank_files_unchanged],
            ['Operations applied', result.operations_applied],
            ['Operations failed', result.operations_failed],
            ['Synthesis size', typeof result.synthesis_size === 'number' ? result.synthesis_size : undefined],
            ['LLM tokens used', result.llm_tokens_used],
            ['Batches', (typeof result.batches_completed === 'number' && typeof result.batches_total === 'number')
                ? `${result.batches_completed}/${result.batches_total}` : undefined],
            ['Duration (s)', result.duration_seconds],
        ].filter(([, v]) => v !== undefined && v !== null);
        const cells = rows.map(([k, v]) => `<tr><th scope="row">${esc(k)}</th><td class="num mono">${esc(String(v))}</td></tr>`).join('');
        let partial = '';
        if (typeof result.batches_completed === 'number' && typeof result.batches_total === 'number'
            && result.batches_completed < result.batches_total) {
            partial = `<p class="consol-partial body-small">Partial completion: ${esc(String(result.batches_completed))} of ${esc(String(result.batches_total))} batches.</p>`;
        }
        return `<div class="table-scroll"><table class="data-table"><tbody>${cells}</tbody></table></div>${partial}`;
    }

    function renderJob(job) {
        if (!job || typeof job !== 'object') return monoBlock('No job payload.');
        if (job.status === 'not_found') {
            // §5.5 / §5(d): never an error — restart/trim copy, neutral.
            return `<div class="consol-job-notfound">${stateUnavailable('Job unknown — the server restarted or trimmed its history (100-job cap). Job history is in-memory and best-effort.')}</div>`;
        }
        const meta = [];
        meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">Space</span>${copyable(String(job.space_id || ''))}</div>`);
        meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">Scope</span><span>${esc(String(job.scope_label || ''))}</span></div>`);
        if (job.job_id) meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">Job</span>${copyable(String(job.job_id))}</div>`);
        // Full job payload per §5.5: provenance, guarantee, and lifecycle stamps.
        if (job.requested_by) meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">Requested by</span><span class="mono-data">${esc(String(job.requested_by))}</span></div>`);
        if (job.guarantee) meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">Guarantee</span><span class="pill pill-neutral" title="Job state lives in server memory: it does not survive a restart and history is trimmed.">${esc(String(job.guarantee))}</span></div>`);
        [['Requested', job.requested_at], ['Queued', job.queued_at], ['Started', job.started_at], ['Finished', job.finished_at]].forEach(pair => {
            if (pair[1]) meta.push(`<div class="consol-jobmeta-row"><span class="micro-label">${esc(pair[0])}</span>${renderTimestamp(pair[1])}</div>`);
        });
        const qp = Number(job.queue_position);
        let posLine = '';
        if (qp === 1) posLine = statusDot('warn', 'Running');
        else if (qp >= 2) posLine = statusDot('warn', `Position ${qp} in queue`);
        let statusBlock = '';
        if (job.status === 'succeeded') statusBlock = renderResultMetrics(job.result) + renderBankSizeAdvisory(job.result);
        else if (job.status === 'failed') statusBlock = `<div class="state-error" role="alert">${icon('alert')}<div><div class="micro-label">Failed</div>${serverMessage(job.error)}</div></div>${renderResultMetrics(job.result)}${renderBankSizeAdvisory(job.result)}`;
        else if (job.message) statusBlock = serverMessage(job.message);
        return `<div class="consol-jobinspect">
            ${progressBar(job.progress)}
            <div class="consol-jobmeta">${meta.join('')}${posLine ? `<div class="consol-jobmeta-row">${posLine}</div>` : ''}</div>
            ${statusBlock}
        </div>`;
    }

    let jobLastSuccess = null;
    function jobSnapshot(job) {
        jobLastSuccess = new Date().toISOString();
        return `<div id="consolJobFreshness" class="body-small text-muted">Last updated ${renderTimestamp(jobLastSuccess)}</div>${renderJob(job)}`;
    }

    function paintJobStale(error) {
        const el = document.getElementById('consolJobFreshness');
        if (el) el.innerHTML = `${statusDot('warn', 'Job data is stale')} Last updated ${renderTimestamp(jobLastSuccess)}${serverMessage(error && error.message || '')}`;
    }

    async function inspectJob(jobId) {
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showModal('Consolidation job', stateLoading('Loading job status…'));
        let data;
        try {
            data = await callTool('bank_consolidation_status', { job_id: jobId });
        } catch (e) {
            if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return;
            showModal('Consolidation job', stateError({ title: 'Request failed' }));
            return;
        }
        // Drop if navigated away OR a newer job/modal replaced this one (so two
        // inspections resolving out of order can't overwrite the latest).
        if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return;
        // Sentinel guards (truncated/rate_limited/read_only) carry only a message.
        if (data && (data.status === 'truncated' || data.status === 'rate_limited' || data.status === 'read_only')) {
            showModal('Consolidation job', panel(stateUnavailable(data.message)));
            return;
        }
        showModal('Consolidation job', jobSnapshot(data));
        scheduleJobLive(jobId, op, epoch, data);
    }

    function modalOpen() {
        const m = document.getElementById('adminModal');
        return !!(m && m.style && m.style.display === 'flex');
    }

    // §5.5.1 — while the inspected job is running or queued, re-read its status
    // every LIVE_REFRESH_MS and repaint the SAME modal instance (no loading
    // flash). Stops on a terminal job, a closed or replaced modal, a route or
    // session change; skips the network call while the tab is hidden.
    function scheduleJobLive(jobId, op, epoch, data) {
        const st = data && data.status;
        if (st !== 'running' && st !== 'queued') return;
        setTimeout(async () => {
            if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive() || !modalOpen()) return;
            if (document.hidden) { scheduleJobLive(jobId, op, epoch, data); return; }
            let next;
            try {
                next = await callTool('bank_consolidation_status', { job_id: jobId });
            } catch (e) {
                // Transport failure: keep the last snapshot on screen and re-arm.
                if (AdminRouter.epoch === epoch && modalOpCurrent(op) && sessionActive() && modalOpen()) { paintJobStale(); scheduleJobLive(jobId, op, epoch, data); }
                return;
            }
            if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive() || !modalOpen()) return;
            const nextStatus = next && next.status;
            // §5.0 sentinels end the loop without repainting. Any other non-job
            // payload (typed `error`, unknown shape) keeps the last good snapshot
            // and re-arms: a running job must never be replaced on screen by an
            // error state because one status read failed.
            if (nextStatus === 'truncated' || nextStatus === 'rate_limited' || nextStatus === 'read_only') { paintJobStale(next); return; }
            if (!['running', 'queued', 'succeeded', 'failed', 'not_found'].includes(nextStatus)) {
                paintJobStale(next);
                scheduleJobLive(jobId, op, epoch, data);
                return;
            }
            showModal('Consolidation job', jobSnapshot(next));
            scheduleJobLive(jobId, op, epoch, next);
        }, LIVE_REFRESH_MS);
    }

    // ───────────────────────── enqueue ─────────────────────────

    // Renders an enqueue-ack from inside a confirm modal's onConfirm. Success →
    // toast + refresh + close the modal (return true). Error/refusal/sentinel →
    // REPLACE the modal with the verbatim server text and keep it open (return
    // false): returning true would let the shell confirm-wrapper closeModal()
    // the very error modal we just showed.
    function handleEnqueueResult(data, epoch, op) {
        // Drop before any effect if navigated away OR a newer modal replaced this
        // confirm (returning true would closeModal() the newer one).
        if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
        if (!data || typeof data !== 'object') { showToast('error', 'No response'); return true; }
        const qp = Number(data.queue_position);
        if (data.status === 'running' || data.status === 'queued' || qp >= 1) {
            showToast('ok', (data.status === 'running' || qp === 1)
                ? 'Consolidation running'
                : `Consolidation queued (position ${qp || '?'})`);
            AdminRouter.refresh();
            return true;
        }
        showModal('Consolidation refused', panel(serverMessage(data && data.message) || stateError({ title: 'The server refused or failed this operation.' })));
        return false;
    }

    async function enqueue(spaceId, scope) {
        // scope 'mine' MUST always send a NON-EMPTY agent (§4.5 E4 — load-bearing).
        // A missing client_name must HARD-REFUSE. scope 'all' sends the
        // historical empty-string sentinel explicitly; omission now means the
        // caller's own notes for every permission level.
        const args = { space_id: spaceId };
        if (scope === 'mine') {
            const agent = state.identity && state.identity.client_name;
            if (!agent) {
                showToast('error', 'Cannot determine your agent identity — reload and sign in again.');
                return null;
            }
            args.agent = String(agent);
        } else if (scope === 'all') {
            args.agent = '';
        }
        try {
            return await callTool('bank_consolidate', args);
        } catch (e) {
            return { status: 'error', message: '' };
        }
    }

    function confirmEnqueue(spaceId, scope) {
        // Guard the mine scope up front so we never show a confirm we cannot
        // fulfil (defence in depth with the hard refuse inside enqueue()).
        if (scope === 'mine' && !(state.identity && state.identity.client_name)) {
            showToast('error', 'Cannot determine your agent identity — reload and sign in again.');
            return;
        }
        const scopeCopy = scope === 'mine'
            ? `Consolidates only your own live notes (agent <code>${esc(String(state.identity.client_name || ''))}</code>) in space <code>${esc(spaceId)}</code>.`
            : `Consolidates <strong>all agents'</strong> live notes in space <code>${esc(spaceId)}</code> (requires manage/admin — the server enforces this).`;
        // Capture the epoch + modal token at open: the shared modal can outlive
        // a route change or be replaced by a newer modal, so a stale confirm
        // must drop before touching the DOM.
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showModal('Consolidate', `<p class="body-small">${scopeCopy}</p><p class="body-small">Consolidation is asynchronous and runs one worker per space.</p>`,
            'Consolidate', async () => {
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                const data = await enqueue(spaceId, scope);
                if (!data) return true; // guard already toasted (missing identity)
                return handleEnqueueResult(data, epoch, op);
            });
    }

    // ───────────────────────── stale banks ─────────────────────────

    function renderStalePanel(epoch) {
        const el = document.getElementById('consolStale');
        if (!el) return;
        const t = state.thresholds;
        const controls = `
            <div class="panel-header"><h2>Notes to consolidate</h2>
                <button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-toggle" aria-expanded="true">Hide notes</button></div>
            <div class="consol-stale-controls">
                <div class="form-group">
                    <label class="form-label" for="consolStaleMinNotes">Min notes</label>
                    <input class="form-input mono" id="consolStaleMinNotes" type="number" min="1" step="1" value="${esc(String(t.minNotes))}">
                </div>
                <div class="form-group">
                    <label class="form-label" for="consolStaleMinAge">Min age (days)</label>
                    <input class="form-input mono" id="consolStaleMinAge" type="number" min="0" step="1" value="${esc(String(t.minAgeDays))}">
                </div>
                <button type="button" class="btn btn-primary" data-action="consol-stale-scan">Scan</button>
            </div>
            <div id="consolStaleResults"></div>`;
        el.innerHTML = state.staleMode ? panel(controls) : panel(`<div class="panel-header"><h2>Notes to consolidate</h2>
            <button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-toggle" aria-expanded="false">Find notes</button></div>
            <p class="body-small">Check for accumulated notes using minimum note count and age. This scan runs on demand.</p>`);
        if (state.staleMode && state.staleData) paintStale(state.staleData);
    }

    function staleRow(sp) {
        const sid = String(sp.space_id || '');
        const href = '#/spaces/' + encodeURIComponent(sid);
        const age = (typeof sp.oldest_note_age_days === 'number') ? String(sp.oldest_note_age_days) : '—';
        return `<tr>
            <td><a class="consol-space-name" href="${esc(href)}">${esc(sid)}</a></td>
            <td class="num mono">${esc(String(sp.live_notes_count ?? '—'))}</td>
            <td class="num mono">${esc(age)}</td>
            <td>${sp.oldest_note_timestamp ? renderTimestamp(sp.oldest_note_timestamp) : '<span class="text-faint">—</span>'}</td>
            <td class="actions"><button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-row" data-space="${esc(sid)}">Consolidate</button></td>
        </tr>`;
    }

    function paintStale(d) {
        const el = document.getElementById('consolStaleResults');
        if (!el) return;
        if (!d || d.status !== 'ok') {
            el.innerHTML = stateError({ title: "Couldn't scan stale banks", message: d && d.message, retryAction: 'consol-stale-scan' });
            return;
        }
        const stale = scopedItems(d.spaces);
        const scanned = (typeof d.total_spaces === 'number') ? d.total_spaces : (Array.isArray(d.scanned) ? d.scanned.length : '—');
        const scanLine = `<p class="body-small consol-scan-line">Scanned ${esc(String(scanned))} accessible space(s)${state.scope ? '; showing this space only' : ''} at thresholds ≥ ${esc(String(d.min_notes))} notes, ≥ ${esc(String(d.min_age_days))} days.</p>`;
        let body;
        if (!stale.length) {
            body = scanLine + stateEmpty({ title: 'No stale banks', hint: `No stale banks at the current thresholds (≥ ${d.min_notes} notes, ≥ ${d.min_age_days} days).` });
        } else {
            const rows = stale.map(staleRow).join('');
            const table = dataTable(['Space', 'Notes', 'Oldest (days)', 'Oldest note', 'Actions'], rows);
            const allBtn = `<button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-all">Consolidate all stale</button>`;
            body = scanLine + `<div class="panel-header"><h3>${esc(String(stale.length))} matching space(s)</h3><div class="page-header-actions">${allBtn}</div></div>` + table;
        }
        el.innerHTML = body + deniedFooter(scopedItems(d.denied_spaces));
    }

    async function scanStale() {
        const notesEl = document.getElementById('consolStaleMinNotes');
        const ageEl = document.getElementById('consolStaleMinAge');
        let minNotes = parseInt(notesEl && notesEl.value, 10);
        let minAge = parseInt(ageEl && ageEl.value, 10);
        if (!Number.isFinite(minNotes) || minNotes < 1) minNotes = 1;
        if (!Number.isFinite(minAge) || minAge < 0) minAge = 0;
        state.thresholds = { minNotes, minAgeDays: minAge };
        const resultsEl = document.getElementById('consolStaleResults');
        if (resultsEl) resultsEl.innerHTML = stateLoading('Scanning stale banks…');
        const epoch = AdminRouter.epoch;
        const gen = ++_staleGen;
        const generation = currentSessionGeneration();
        // Drop out-of-order scans: only the latest scan may paint / set staleData.
        const stale = () => AdminRouter.epoch !== epoch || gen !== _staleGen || !sessionActive() || !sessionGenerationIsCurrent(generation);
        let data;
        try {
            data = await callTool('bank_stale_spaces', { min_notes: minNotes, min_age_days: minAge });
        } catch (e) {
            if (stale()) return;
            if (resultsEl) resultsEl.innerHTML = stateError({ title: 'Scan failed', retryAction: 'consol-stale-scan' });
            return;
        }
        if (stale()) return;
        if (data && (data.status === 'truncated' || data.status === 'rate_limited' || data.status === 'read_only')) {
            if (resultsEl) resultsEl.innerHTML = stateUnavailable(data.message);
            return;
        }
        state.staleData = data;
        paintStale(data);
    }

    // Direct per-space stale consolidation (§4.8 K4). Staleness is a whole-space
    // property, so a confirmed manage/admin action explicitly sends the global
    // sentinel; write tokens omit it and remain caller-scoped.
    function confirmStaleRow(spaceId) {
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showModal('Consolidate stale bank', `<p class="body-small">Submit a consolidation for space <code>${esc(spaceId)}</code>? This clears accumulated live notes into the mid bank.</p><p class="body-small">Scope depends on your permission and is enforced by the server: manage/admin consolidates <strong>all agents'</strong> notes; a write-only token consolidates <strong>only your own</strong> notes (the note count above counts all agents).</p>`,
            'Consolidate', async () => {
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                let data;
                const args = { space_id: spaceId };
                if (hasManage()) args.agent = '';
                try { data = await callTool('bank_consolidate', args); }
                catch (e) { data = { status: 'error', message: '' }; }
                return handleEnqueueResult(data, epoch, op);
            });
    }

    // Scan-BEFORE-confirm all-stale (§4.8 K5): re-scan, show the exact current
    // stale set in the confirmation, then submit ONLY that captured set — a
    // space that becomes stale after the operator confirmed is never swept in.
    async function startConsolidateAllStale() {
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        // Drop if navigated away OR a newer modal replaced this all-stale flow.
        const stale = () => AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive();
        showModal('Consolidate all stale', stateLoading('Re-scanning stale banks…'));
        let scan;
        try { scan = await callTool('bank_stale_spaces', { min_notes: state.thresholds.minNotes, min_age_days: state.thresholds.minAgeDays }); }
        catch (e) { if (stale()) return; showModal('Consolidate all stale', panel(stateError({ title: 'Re-scan failed' }))); return; }
        if (stale()) return;
        // §5.0: branch on the typed status BEFORE deriving the captured set — a
        // sentinel or error scan must not fall through to a false "No stale banks".
        if (scan && (scan.status === 'truncated' || scan.status === 'rate_limited' || scan.status === 'read_only')) {
            showModal('Consolidate all stale', panel(stateUnavailable(scan.message)));
            return;
        }
        if (!scan || scan.status !== 'ok') {
            showModal('Consolidate all stale', panel(stateError({ title: "Couldn't re-scan stale banks", message: scan && scan.message })));
            return;
        }
        state.staleData = scan;
        paintStale(scan);
        const captured = scopedItems(scan.spaces).map(s => String(s.space_id || '')).filter(Boolean);
        if (!captured.length) { showModal('Consolidate all stale', panel(stateEmpty({ title: 'No stale banks to consolidate' }))); return; }
        const list = captured.map(s => `<li>${copyable(s)}</li>`).join('');
        const scopeCopy = hasManage()
            ? "All agents' live notes in every listed space will be consolidated."
            : 'Only your own live notes in every listed space will be consolidated.';
        // Opening the confirm modal advances the token; submitAllStale captures
        // the NEW token so its summary can't be closed by a stale continuation.
        const confirmOp = beginModalOp();
        showModal('Consolidate all stale banks',
            `<p class="body-small">${esc(String(captured.length))} stale space(s) will be consolidated, one at a time (sequential — one worker per space). ${scopeCopy}</p><ul class="consol-confirm-list">${list}</ul>`,
            // submitAllStale replaces this modal with the summary, so return false.
            'Consolidate all', async () => { await submitAllStale(captured, epoch, confirmOp); return false; });
    }

    // Sequential submission over the CAPTURED set — one lane per space, never
    // parallelized, never re-scanned; a 429 aborts with an honest partial
    // summary and no retry.
    async function submitAllStale(spaces, epoch, op) {
        const stale = () => AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive();
        const results = [];
        let aborted = false;
        for (const sid of spaces) {
            if (stale()) return;
            let data;
            const args = { space_id: sid };
            if (hasManage()) args.agent = '';
            try { data = await callTool('bank_consolidate', args); }
            catch (e) { results.push({ space_id: sid, status: 'error', message: 'request failed' }); continue; }
            if (data && data.status === 'rate_limited') {
                results.push({ space_id: sid, status: 'rate_limited', message: data.message });
                aborted = true;
                break;
            }
            results.push({ space_id: sid, status: data && data.status, message: data && data.message });
        }
        if (stale()) return;
        showAllStaleSummary(results, aborted, spaces.length);
        AdminRouter.refresh();
    }

    function showAllStaleSummary(results, aborted, totalPlanned) {
        const rows = results.map(r => {
            const st = String(r.status || 'unknown');
            const sev = (st === 'running' || st === 'queued') ? 'ok' : (st === 'rate_limited' || st === 'error') ? 'error' : 'warn';
            // Render the verbatim server message per row (§5.0/§5(a)) — a
            // fail-closed refusal or 429 must show its real cause, not a bare row.
            const msg = r.message ? serverMessage(r.message) : '';
            return `<tr><td>${copyable(String(r.space_id || ''))}</td><td>${statusDot(sev, st)}</td><td>${msg}</td></tr>`;
        }).join('');
        const note = aborted
            ? `<p class="consol-partial body-small">Rate limited by the gateway — the batch was stopped after ${esc(String(results.length))} of ${esc(String(totalPlanned))} spaces. No automatic retry.</p>`
            : `<p class="body-small">Submitted ${esc(String(results.length))} consolidation(s). "running"/"queued" mean the job was accepted.</p>`;
        showModal('Consolidate all stale', `${note}<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Space</th><th scope="col">Status</th><th scope="col">Message</th></tr></thead><tbody>${rows}</tbody></table></div>`);
    }

    // ───────────────────────── action registration ─────────────────────────

    registerAction('consol-picker', () => { if (sessionActive() && sessionGenerationIsCurrent(state.sessionGeneration)) openPicker(); });
    registerAction('consol-refresh', () => AdminRouter.refresh());
    registerAction('consol-job', (d) => { if (d.jobId) inspectJob(d.jobId); });
    registerAction('consol-mine', (d) => { if (d.space) confirmEnqueue(d.space, 'mine'); });
    registerAction('consol-all', (d) => { if (d.space && hasManage()) confirmEnqueue(d.space, 'all'); });
    registerAction('consol-stale-toggle', () => {
        const activating = !state.staleMode;
        state.staleMode = activating;
        // §5.5: never carry a prior activation's results into a fresh one — drop
        // the cache so renderStalePanel shows an empty panel, then scan with a
        // visible loading phase. Deactivating just hides the panel.
        if (activating) state.staleData = null;
        else _staleGen += 1; // A late scan must not repaint or refill the collapsed panel.
        renderStalePanel(AdminRouter.epoch);
        if (activating) scanStale();
    });
    registerAction('consol-stale-scan', () => scanStale());
    registerAction('consol-stale-row', (d) => { if (d.space) confirmStaleRow(d.space); });
    registerAction('consol-stale-all', () => startConsolidateAllStale());

    AdminViews.register('consolidation', render);
})();
