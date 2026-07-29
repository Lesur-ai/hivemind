/**
 * Consolidation view (P8-4, issue #142) — route #/consolidation.
 *
 * Absorbs the inherited consolidation-lanes dashboard AND the Stale Banks view
 * (contract §4.8 K1–K6, §5.5). Real data only: every widget consumes exactly
 * the fields the real tools return (bank_consolidation_queues / _status /
 * bank_consolidate / bank_stale_spaces). No polling (D8): the only refresh
 * triggers are load, manual Refresh, and after-action. Progress bars are
 * snapshots labeled "as of last refresh".
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
    };

    function hasManage() {
        const perms = state.identity && Array.isArray(state.identity.permissions)
            ? state.identity.permissions : [];
        return perms.includes('manage') || perms.includes('admin');
    }

    // Modal-instance token: bumped each time this view opens a modal, so a slow
    // or out-of-order continuation drops instead of overwriting or closing a
    // NEWER modal opened on the same route (Codex same-route race). Checked
    // alongside the navigation epoch before any modal/close effect.
    let _modalOp = 0;
    function beginModalOp() { return ++_modalOp; }
    function modalOpCurrent(t) { return _modalOp === t; }

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
        const staleLabel = state.staleMode ? 'Hide stale banks' : 'Stale banks';
        return `
            <button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-toggle">${esc(staleLabel)}</button>
            <button type="button" class="btn btn-secondary btn-sm" data-action="consol-refresh">${icon('refresh')}<span>Refresh</span></button>`;
    }

    function subtitle(data) {
        // §8.2 trap: the sanctioned mental model is one worker per space;
        // never compose the banned two-word phrase describing that scheduling.
        // Every token below is read from the real bank_consolidation_queues
        // response (§5.5) — nothing is hardcoded and presented as server data.
        const model = data && data.parallelism_model
            ? `<span class="mono micro-label consol-model">${esc(String(data.parallelism_model))}</span>` : '';
        const lanes = (data && Array.isArray(data.lanes)) ? data.lanes : [];
        const guarantee = lanes.map(l => l && l.guarantee).find(Boolean);
        const guaranteeBadge = guarantee
            ? `<span class="pill pill-neutral consol-guarantee" title="Job state lives in server memory: it does not survive a restart and history is trimmed.">${esc(String(guarantee))}</span>` : '';
        const batch = data && data.service_config && typeof data.service_config.batch_size === 'number'
            ? `<span class="mono micro-label consol-batch">batch size ${esc(String(data.service_config.batch_size))}</span>` : '';
        return `<p class="consol-subtitle body-small">One worker per space · lanes are isolated per space. ${model}${guaranteeBadge}${batch}</p>`;
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
        state.owner = owner;
        state.identity = identity;
        const epoch = ctx ? ctx.epoch : AdminRouter.epoch;
        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Consolidation', headerActions())}
            <div id="consolSubtitle"></div>
            <div id="consolStale"></div>
            <div id="consolLanes">${panel(stateLoading('Loading consolidation lanes…'))}</div>
        </div>`;
        if (state.staleMode) renderStalePanel(epoch);
        loadLanes(epoch);
    }

    // ───────────────────────── lanes ─────────────────────────

    async function loadLanes(epoch) {
        let data;
        try {
            data = await callTool('bank_consolidation_queues', { space_ids: '' });
        } catch (e) {
            if (AdminRouter.epoch !== epoch) return;
            paintLanesError({ status: 'error', message: '' });
            return;
        }
        if (AdminRouter.epoch !== epoch) return;

        const sub = document.getElementById('consolSubtitle');
        if (sub) sub.innerHTML = subtitle(data);

        if (!data || data.status !== 'ok') {
            paintLanesError(data || {});
            return;
        }
        paintLanes(data);
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
            title: "Couldn't load consolidation lanes",
            message: data && data.message,
            retryAction: 'consol-refresh',
        }));
    }

    function metricCards(d) {
        const cards = [
            ['Total spaces', d.total_spaces],
            ['Active', d.active_spaces],
            ['Running', d.running_spaces],
            ['Queued jobs', d.queued_jobs],
            ['Failed recent', d.failed_recent],
        ];
        const html = cards.map(([label, value]) => {
            const shown = (typeof value === 'number') ? String(value) : '—';
            return `<div class="metric-card"><span class="micro-label">${esc(label)}</span><span class="metric-value">${esc(shown)}</span></div>`;
        }).join('');
        return `<div class="metric-grid consol-metrics">${html}</div>`;
    }

    function laneStateDot(laneState) {
        if (laneState === 'running') return statusDot('warn', 'Running');
        if (laneState === 'queued') return statusDot('warn', 'Queued');
        // §5(d): `failed` is idle-with-most-recent-failure — an attention chip,
        // NOT a currently-running failure. Frame it as idle + last run failed.
        if (laneState === 'failed') return statusDot('error', 'Idle · last run failed');
        return statusDot('neutral', 'Idle');
    }

    function progressBar(progress) {
        if (!progress) return '';
        const nd = Number(progress.notes_done || 0);
        const nt = progress.notes_total;
        const bd = Number(progress.batches_done || 0);
        const bt = progress.batches_total;
        // Keep phase/label RAW; escape once at each sink (the aria-label and the
        // visible meta) — never pre-escape, or the aria-label double-escapes.
        const phase = progress.phase ? String(progress.phase) : '';
        let pct = null;
        let label = '';
        if (typeof nt === 'number' && nt > 0) {
            pct = Math.min(100, Math.round((nd / nt) * 100));
            label = `${nd}/${nt} notes`;
        } else if (typeof bt === 'number' && bt > 0) {
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

    function jobCell(job) {
        if (!job) return '<span class="text-faint">—</span>';
        const jid = String(job.job_id || '');
        const scope = esc(String(job.scope_label || ''));
        const idChip = jid
            ? `<button type="button" class="btn btn-ghost btn-sm consol-job-link" data-action="consol-job" data-job-id="${esc(jid)}" title="${esc(jid)}">${esc(truncateMiddle(jid, 10, 6))}</button>`
            : '';
        return `<div class="consol-jobcell"><span class="consol-scope mono-data">${scope}</span> ${idChip}${progressBar(job.progress)}</div>`;
    }

    function queuedCell(lane) {
        const n = Number(lane.queued_count || 0);
        if (!n) return '<span class="text-faint">0</span>';
        // Render the FIFO queue payload (queued_jobs[], insertion order) as
        // inspectable position chips — each opens the job inspector (§5.5).
        const jobs = Array.isArray(lane.queued_jobs) ? lane.queued_jobs : [];
        if (!jobs.length) {
            return `<span class="count-pill mono">${esc(String(n))}</span> <span class="body-small">in queue</span>`;
        }
        const chips = jobs.map(j => {
            const jid = String(j.job_id || '');
            const pos = Number(j.queue_position);
            const scope = String(j.scope_label || '');
            // Position is the visible label; the real scope_label rides in the
            // title/aria so the chip carries the job's actual scope (§5.5).
            const label = (pos >= 2 ? `#${pos}` : 'queued') + (scope ? ` · ${scope}` : '');
            const aria = `Inspect queued job ${truncateMiddle(jid, 10, 6)} at position ${pos}${scope ? ' — ' + scope : ''}`;
            return jid
                ? `<button type="button" class="btn btn-ghost btn-sm consol-qchip" data-action="consol-job" data-job-id="${esc(jid)}" title="${esc(scope || jid)}" aria-label="${esc(aria)}">${esc(label)}</button>`
                : `<span class="count-pill mono">${esc(label)}</span>`;
        }).join(' ');
        return `<span class="consol-queued">${chips}</span>`;
    }

    function latestCell(lane) {
        const jobs = Array.isArray(lane.latest_jobs) ? lane.latest_jobs : [];
        if (!jobs.length) return '<span class="text-faint">—</span>';
        const j = jobs[0];
        const sev = j.status === 'succeeded' ? 'ok' : j.status === 'failed' ? 'error' : 'warn';
        const when = j.finished_at ? renderTimestamp(j.finished_at) : '';
        const jid = String(j.job_id || '');
        const link = jid
            ? `<button type="button" class="btn btn-ghost btn-sm" data-action="consol-job" data-job-id="${esc(jid)}" aria-label="Inspect job ${esc(truncateMiddle(jid, 10, 6))}">Details</button>`
            : '';
        return `<div class="consol-latest">${statusDot(sev, j.status || 'unknown')} ${when} ${link}</div>`;
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

    function laneRow(lane) {
        const sid = String(lane.space_id || '');
        const href = '#/spaces/' + encodeURIComponent(sid);
        return `<tr>
            <td><a class="mono" href="${esc(href)}">${esc(sid)}</a></td>
            <td>${laneStateDot(lane.lane_state)}</td>
            <td>${jobCell(lane.running_job)}</td>
            <td class="num">${queuedCell(lane)}</td>
            <td>${latestCell(lane)}</td>
            <td class="actions">${laneActions(lane)}</td>
        </tr>`;
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
        const lanes = Array.isArray(d.lanes) ? d.lanes : [];
        let body;
        if (!lanes.length) {
            body = stateEmpty({ title: 'No spaces visible', hint: 'This token cannot see any consolidation lanes.' });
        } else {
            const rows = lanes.map(laneRow).join('');
            const table = dataTable(['Space', 'Lane', 'Running job', 'Queued', 'Latest', 'Actions'], rows);
            body = `<div class="panel-header"><h2>Lanes</h2><span class="count-pill mono">${esc(String(lanes.length))}</span></div>${table}`;
        }
        el.innerHTML = metricCards(d) + panel(body) + deniedFooter(d.denied_spaces);
    }

    // ───────────────────────── job inspector ─────────────────────────

    function renderResultMetrics(result) {
        if (!result || typeof result !== 'object') return '';
        // Zero-notes short form: only status/notes_processed/message.
        if (Number(result.notes_processed) === 0 && result.message) {
            return `<div class="consol-nothing"><span class="micro-label">NOTHING TO DO</span>${serverMessage(result.message)}</div>`;
        }
        const rows = [
            ['Notes processed', result.notes_processed],
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
        if (job.status === 'succeeded') statusBlock = renderResultMetrics(job.result);
        else if (job.status === 'failed') statusBlock = `<div class="state-error" role="alert">${icon('alert')}<div><div class="micro-label">FAILED</div>${serverMessage(job.error)}</div></div>`;
        else if (job.message) statusBlock = serverMessage(job.message);
        return `<div class="consol-jobinspect">
            ${progressBar(job.progress)}
            <div class="consol-jobmeta">${meta.join('')}${posLine ? `<div class="consol-jobmeta-row">${posLine}</div>` : ''}</div>
            ${statusBlock}
        </div>`;
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
        showModal('Consolidation job', renderJob(data));
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
            <div class="panel-header"><h2>Stale banks</h2></div>
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
        el.innerHTML = panel(controls);
        if (state.staleData) paintStale(state.staleData);
    }

    function staleRow(sp) {
        const sid = String(sp.space_id || '');
        const href = '#/spaces/' + encodeURIComponent(sid);
        const age = (typeof sp.oldest_note_age_days === 'number') ? String(sp.oldest_note_age_days) : '—';
        return `<tr>
            <td><a class="mono" href="${esc(href)}">${esc(sid)}</a></td>
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
        const stale = Array.isArray(d.spaces) ? d.spaces : [];
        const scanned = (typeof d.total_spaces === 'number') ? d.total_spaces : (Array.isArray(d.scanned) ? d.scanned.length : 0);
        const scanLine = `<p class="body-small consol-scan-line">Scanned ${esc(String(scanned))} space(s) at thresholds ≥ ${esc(String(d.min_notes))} notes, ≥ ${esc(String(d.min_age_days))} days.</p>`;
        let body;
        if (!stale.length) {
            body = scanLine + stateEmpty({ title: 'No stale banks', hint: `No stale banks at the current thresholds (≥ ${d.min_notes} notes, ≥ ${d.min_age_days} days).` });
        } else {
            const rows = stale.map(staleRow).join('');
            const table = dataTable(['Space', 'Notes', 'Oldest (days)', 'Oldest note', 'Actions'], rows);
            const allBtn = `<button type="button" class="btn btn-secondary btn-sm" data-action="consol-stale-all">Consolidate all stale</button>`;
            body = scanLine + `<div class="panel-header"><h3>${esc(String(d.total_stale))} stale</h3><div class="page-header-actions">${allBtn}</div></div>` + table;
        }
        el.innerHTML = body + deniedFooter(d.denied_spaces);
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
        // Drop out-of-order scans: only the latest scan may paint / set staleData.
        const stale = () => AdminRouter.epoch !== epoch || gen !== _staleGen || !sessionActive();
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
        const captured = Array.isArray(scan.spaces)
            ? scan.spaces.map(s => String(s.space_id || '')).filter(Boolean) : [];
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
        AdminRouter.refresh();
        if (activating) scanStale();
    });
    registerAction('consol-stale-scan', () => scanStale());
    registerAction('consol-stale-row', (d) => { if (d.space) confirmStaleRow(d.space); });
    registerAction('consol-stale-all', () => startConsolidateAllStale());

    AdminViews.register('consolidation', render);
})();
