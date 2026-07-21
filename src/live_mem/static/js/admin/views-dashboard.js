/**
 * Dashboard view (P8-2, issue #140).
 * Contract: DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §4.2 (parity), §5.2
 * (data matrix). Route entry issues exactly 3 `/api/tool` calls for a
 * non-admin identity (`system_health`, `space_list`,
 * `bank_consolidation_queues`) and 4 for an admin identity (adds
 * `admin_list_tokens`); identity is read from the shell-cached
 * `_ctx().identity` — zero extra request, `system_whoami` is never called
 * directly from this file. No polling anywhere: load, manual refresh, and
 * after-action are the only triggers (D8).
 */
(function () {
    const BEST_EFFORT_TOOLTIP = 'Job state lives in server memory: it does not survive a restart and history is trimmed.';

    let _epoch = -1;
    let _lastHealth = null;
    let _healthSeq = 0;

    function _isAdmin(identity) {
        return !!(identity && Array.isArray(identity.permissions) && identity.permissions.includes('admin'));
    }

    function _hasManage(identity) {
        if (!identity || !Array.isArray(identity.permissions)) return false;
        return identity.auth_type === 'bootstrap' || identity.permissions.includes('manage') || _isAdmin(identity);
    }

    function _svcSeverity(status) {
        if (status === 'ok') return 'ok';
        if (status === 'warning') return 'warn';
        if (status === 'error') return 'error';
        return 'neutral';
    }

    function _fmtUptime(seconds) {
        if (typeof seconds !== 'number' || Number.isNaN(seconds)) return '';
        const totalMin = Math.floor(seconds / 60);
        const days = Math.floor(totalMin / 1440);
        const hours = Math.floor((totalMin % 1440) / 60);
        const mins = totalMin % 60;
        if (days > 0) return `${days}d ${hours}h`;
        if (hours > 0) return `${hours}h ${mins}m`;
        return `${mins}m`;
    }

    // ═══════════════ Identity card (zero request — ctx.identity is cached shell state) ═══════════════

    function _identityCardBody(identity) {
        if (!identity || !identity.client_name) {
            return `<span class="micro-label">Identity</span>${stateUnavailable('Identity unavailable.')}`;
        }
        const perms = (identity.permissions || []).map(p => pill('neutral', String(p))).join('');
        // §5.1/§5.2 D7: same fields as the sidebar identity block, including
        // the conditional expiry chip — this card is fed from the same
        // cached identity, zero extra request either way.
        const expiresChip = identity.expires_at
            ? `<span title="${esc(fmtTimestamp(identity.expires_at).title)}">${pill('neutral', `expires ${fmtTimestamp(identity.expires_at).text} UTC`)}</span>`
            : '';
        return `<div class="dash-card-body">
            <span class="micro-label">Identity</span>
            <span class="dash-identity-name" title="${esc(identity.client_name)}">${esc(identity.client_name)}</span>
            <div class="dash-identity-chips">${pill('neutral', identity.auth_type || 'unknown')}${perms}${expiresChip}</div>
        </div>`;
    }

    // ═══════════════ Health card + drill-down (system_health) ═══════════════

    // Success state: the whole card body is a single full-bleed <button>
    // that opens the drill-down. The card container itself is a plain <div>
    // (see render()), so this button is never nested inside another
    // interactive control — and the error state (its own Retry button) never
    // renders a button-in-a-button (§2.8 accessibility).
    function _healthCardBody(health) {
        const sev = health.status === 'healthy' ? 'ok' : 'warn';
        const label = health.status === 'healthy' ? 'Healthy' : 'Degraded';
        const spacesText = (health.spaces_count === -1 || health.spaces_count === undefined)
            ? 'spaces unavailable'
            : `${health.spaces_count} spaces`;
        const uptimeText = _fmtUptime(health.uptime_seconds);
        const s3 = (health.services && health.services.s3) || {};
        const llm = (health.services && health.services.llmaas) || {};
        return `<button type="button" class="dash-card-btn" data-action="dash-open-health" aria-label="System health — open details">
            <span class="micro-label">System health</span>
            ${statusDot(sev, label)}
            <span class="metric-value">v${esc(String(health.version || '?'))}</span>
            <span class="body-small dash-meta">${esc([uptimeText, spacesText].filter(Boolean).join(' · '))}</span>
            <div class="dash-health-services">
                ${statusDot(_svcSeverity(s3.status), 'S3')}
                ${statusDot(_svcSeverity(llm.status), 'LLMaaS')}
            </div>
        </button>`;
    }

    function _healthCardError(resp) {
        return `<div class="dash-card-body">
            <span class="micro-label">System health</span>
            ${stateError({ title: "Couldn't load system health", message: resp && resp.message, retryAction: 'dash-refresh-health' })}
        </div>`;
    }

    function _healthModalBody(health) {
        const s3 = (health.services && health.services.s3) || {};
        const llm = (health.services && health.services.llmaas) || {};
        const sev = health.status === 'healthy' ? 'ok' : 'warn';
        const label = health.status === 'healthy' ? 'Healthy' : 'Degraded';
        const spacesText = (health.spaces_count === -1 || health.spaces_count === undefined) ? 'unavailable' : String(health.spaces_count);
        return `<div class="dash-health-modal">
            ${statusDot(sev, label)}
            <dl class="dash-health-modal-grid">
                <dt>Service</dt><dd>${esc(String(health.service_name || '—'))}</dd>
                <dt>Version</dt><dd>${esc(String(health.version || '—'))}</dd>
                <dt>Uptime</dt><dd>${esc(_fmtUptime(health.uptime_seconds) || '—')}</dd>
                <dt>Spaces</dt><dd>${esc(spacesText)}</dd>
            </dl>
            <div class="dash-health-modal-service">
                <h3>S3</h3>
                ${statusDot(_svcSeverity(s3.status), s3.status || 'unknown')}
                ${s3.bucket ? `<p class="body-small">Bucket: ${esc(String(s3.bucket))}</p>` : ''}
                ${typeof s3.latency_ms === 'number' ? `<p class="body-small">Latency: ${esc(String(s3.latency_ms))} ms</p>` : ''}
                ${s3.message ? serverMessage(s3.message) : ''}
            </div>
            <div class="dash-health-modal-service">
                <h3>LLMaaS</h3>
                ${statusDot(_svcSeverity(llm.status), llm.status || 'unknown')}
                ${llm.model ? `<p class="body-small">Model: ${esc(String(llm.model))}</p>` : ''}
                ${typeof llm.latency_ms === 'number' ? `<p class="body-small">Latency: ${esc(String(llm.latency_ms))} ms</p>` : ''}
                ${llm.message ? serverMessage(llm.message) : ''}
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="dashHealthModalRefreshBtn" data-action="dash-refresh-health-modal">${icon('refresh')} Refresh</button>
        </div>`;
    }

    function _setHealthRefreshButton(inFlight) {
        const btn = document.getElementById('dashHealthRefreshBtn');
        if (!btn) return;
        btn.disabled = inFlight;
        btn.innerHTML = inFlight ? 'Checking…' : `${icon('refresh')} Refresh health`;
    }

    function _setHealthModalRefreshButton(inFlight) {
        const btn = document.getElementById('dashHealthModalRefreshBtn');
        if (!btn) return;
        btn.disabled = inFlight;
        btn.innerHTML = inFlight ? 'Checking…' : `${icon('refresh')} Refresh`;
    }

    // Load/refresh system_health (D8: load + manual refresh only, never polled,
    // never on tab focus). Shared by the card's own refresh button and the
    // drill-down modal's refresh button — both keep the card and (if open)
    // the modal in sync. Guarded by both the router epoch (dropped if the
    // operator navigated away, §3.3.2 rule 3) and a local monotonic sequence
    // number: two overlapping calls issued in the SAME epoch (e.g. a
    // route-entry load racing a fast manual refresh click) can resolve out
    // of order, so only the result of the most recently *issued* call is
    // ever applied — an older, still-in-flight response is dropped even if
    // it happens to resolve last.
    async function _loadHealth(epochAtCall) {
        const seq = ++_healthSeq;
        _setHealthRefreshButton(true);
        _setHealthModalRefreshButton(true);
        let resp;
        try {
            resp = await callTool('system_health', {});
        } catch {
            resp = { status: 'error', message: 'Request failed' };
        }
        if (seq !== _healthSeq || AdminRouter.epoch !== epochAtCall) return;
        _setHealthRefreshButton(false);
        const card = document.getElementById('dashHealthCard');
        const modalOpen = !!document.getElementById('dashHealthModalRefreshBtn');
        if (resp && (resp.status === 'healthy' || resp.status === 'degraded')) {
            _lastHealth = resp;
            if (card) card.innerHTML = _healthCardBody(resp);
            if (modalOpen) {
                const modalBody = document.querySelector('#adminModal .modal-body');
                if (modalBody) modalBody.innerHTML = _healthModalBody(resp);
            }
        } else {
            _lastHealth = null;
            if (card) card.innerHTML = _healthCardError(resp);
            if (modalOpen) {
                _setHealthModalRefreshButton(false);
                showToast('error', (resp && resp.message) || 'Refresh failed');
            }
        }
    }

    registerAction('dash-refresh-health', () => {
        _loadHealth(AdminRouter.epoch);
    });

    registerAction('dash-open-health', () => {
        if (!_lastHealth) {
            showModal('System health', stateUnavailable('Health data is not available yet.'));
            return;
        }
        showModal('System health', _healthModalBody(_lastHealth));
    });

    registerAction('dash-refresh-health-modal', () => {
        _loadHealth(AdminRouter.epoch);
    });

    // ═══════════════ Spaces summary tile (space_list) ═══════════════

    function _spacesTileBody(resp, canManage) {
        if (!resp) return stateLoading('');
        if (resp.status !== 'ok') {
            return `<span class="micro-label">Spaces</span>${stateError({ title: "Couldn't load spaces", message: resp.message, retryAction: 'dash-refresh-rest' })}`;
        }
        const spaces = resp.spaces || [];
        const total = resp.total ?? spaces.length;
        if (total === 0) {
            return `<span class="micro-label">Spaces</span>${stateEmpty({
                title: 'No spaces yet',
                hint: canManage ? 'Create your first space to get started.' : 'A manager can create the first space.',
                actionHtml: canManage
                    ? '<a class="btn btn-primary btn-sm" href="#/spaces">Create space</a>'
                    : '',
            })}`;
        }
        // §5.2 / #140 plan: total plus the client-side sums of each space's
        // live_notes_count (Short) and bank_files_count (Mid). A space whose
        // count field is missing is excluded from that sum rather than
        // counted as 0 — if every space is missing a field, the aggregate is
        // rendered as unavailable ('—'), never a fabricated 0 (§5.0/§2.7).
        const _sum = (key) => {
            const present = spaces.filter(s => typeof s[key] === 'number');
            return present.length ? present.reduce((n, s) => n + s[key], 0) : null;
        };
        const shortSum = _sum('live_notes_count');
        const midSum = _sum('bank_files_count');
        return `<a class="dash-tile-link" href="#/spaces">
            <span class="micro-label">Spaces</span>
            <span class="metric-value">${esc(String(total))}</span>
            <span class="body-small dash-meta">${esc(String(shortSum ?? '—'))} short · ${esc(String(midSum ?? '—'))} mid</span>
        </a>`;
    }

    // ═══════════════ Tokens tile (admin_list_tokens, admin-gated) ═══════════════

    function _tokensTileBody(admin, resp) {
        if (!admin) {
            return `<span class="micro-label">Tokens</span>${stateUnavailable('Admin permission required.')}`;
        }
        if (!resp) return stateLoading('');
        if (resp.status !== 'ok') {
            return `<span class="micro-label">Tokens</span>${stateError({ title: "Couldn't load tokens", message: resp.message, retryAction: 'dash-refresh-rest' })}`;
        }
        const list = resp.tokens || [];
        const total = resp.total ?? list.length;
        const revoked = list.filter(t => t.revoked).length;
        const now = Date.now();
        const active = list.filter(t => !t.revoked && (!t.expires_at || Date.parse(t.expires_at) > now)).length;
        return `<div class="dash-card-body">
            <span class="micro-label">Tokens</span>
            <span class="metric-value">${esc(String(active))}</span>
            <span class="body-small dash-meta">${esc(String(total))} tokens · ${esc(String(revoked))} revoked</span>
        </div>`;
    }

    // ═══════════════ Consolidation lanes summary (bank_consolidation_queues) ═══════════════

    function _metricBlock(label, value) {
        return `<div class="dash-lane-metric"><span class="micro-label">${esc(label)}</span><span class="metric-value">${esc(String(value ?? '—'))}</span></div>`;
    }

    function _lanesSummaryBody(resp) {
        if (!resp) return stateLoading('');
        if (resp.status !== 'ok') {
            return stateError({ title: "Couldn't load consolidation summary", message: resp.message, retryAction: 'dash-refresh-rest' });
        }
        const denied = resp.denied_spaces || [];
        const batchSize = (resp.service_config || {}).batch_size;
        return `
            <div class="dash-lanes-metrics">
                ${_metricBlock('Active', resp.active_spaces)}
                ${_metricBlock('Running', resp.running_spaces)}
                ${_metricBlock('Queued', resp.queued_jobs)}
                ${_metricBlock('Failed recent', resp.failed_recent)}
            </div>
            <p class="body-small dash-meta">${esc(String(resp.total_spaces ?? '—'))} spaces total · model: ${esc(String(resp.parallelism_model || '—'))}${batchSize !== undefined ? ` · batch ${esc(String(batchSize))}` : ''}</p>
            ${denied.length ? `<div class="dash-denied">${denied.map(d => serverMessage(`${d.space_id}: ${d.message}`)).join('')}</div>` : ''}
            <a class="btn btn-ghost btn-sm" href="#/consolidation">View consolidation</a>
        `;
    }

    // ═══════════════ Recent memory activity (derived client-side, zero extra request) ═══════════════

    function _recentActivityBody(resp) {
        if (!resp) return stateLoading('');
        if (resp.status !== 'ok') return stateUnavailable('Recent activity is not available.');
        const jobs = [];
        (resp.lanes || []).forEach(lane => {
            (lane.latest_jobs || []).forEach(job => {
                if (job && job.finished_at) jobs.push(job);
            });
        });
        jobs.sort((a, b) => (a.finished_at < b.finished_at ? 1 : a.finished_at > b.finished_at ? -1 : 0));
        const top = jobs.slice(0, 10);
        if (!top.length) {
            return stateEmpty({ title: 'No consolidation activity recorded since last restart' });
        }
        const rows = top.map(job => {
            const sev = job.status === 'succeeded' ? 'ok' : job.status === 'failed' ? 'error' : 'neutral';
            const ts = fmtTimestamp(job.finished_at);
            const href = esc('#/spaces/' + encodeURIComponent(job.space_id));
            return `<div class="dash-activity-row">
                ${statusDot(sev, job.status || 'unknown')}
                <a href="${href}" class="dash-activity-space">${esc(job.space_id)}</a>
                <span class="body-small dash-meta">${esc(job.scope_label || '')}</span>
                <span class="mono-data" title="${esc(ts.title)}">${esc(ts.text)}</span>
                ${job.job_id ? `<span title="${esc(job.job_id)}">${copyable(job.job_id, truncateMiddle(job.job_id, 10, 6))}</span>` : ''}
            </div>`;
        }).join('');
        return `<div class="dash-activity-list">${rows}</div>
            <p class="body-small dash-meta"><span title="${esc(BEST_EFFORT_TOOLTIP)}">${pill('neutral', 'in_memory_best_effort')}</span> job history — since restart, last 10 per space</p>`;
    }

    // ═══════════════ Route-entry loads ═══════════════

    function _applySpaces(resp, canManage) {
        if (resp && resp.status === 'ok') cache.spaces = resp.spaces || [];
        const tile = document.getElementById('dashSpacesTile');
        if (tile) tile.innerHTML = _spacesTileBody(resp, canManage);
    }

    function _applyTokens(admin, resp) {
        if (resp && resp.status === 'ok') cache.tokens = resp.tokens || [];
        const tile = document.getElementById('dashTokensTile');
        if (tile) tile.innerHTML = _tokensTileBody(admin, resp);
    }

    function _applyQueues(resp) {
        const lanesPanel = document.getElementById('dashLanesPanel');
        if (lanesPanel) lanesPanel.innerHTML = _lanesSummaryBody(resp);
        const activityPanel = document.getElementById('dashActivityPanel');
        if (activityPanel) activityPanel.innerHTML = _recentActivityBody(resp);
    }

    let _restSeq = 0;

    // Route-entry (and manual-refresh) load of everything except health:
    // 2 calls for non-admin identities, 3 for admin (adds admin_list_tokens).
    // Sequence-guarded like _loadHealth: a route-entry load racing a fast
    // manual-refresh click (or two refresh clicks) can resolve out of
    // order — only the most recently issued call is ever applied.
    async function _loadRest(epochAtCall, identity) {
        const seq = ++_restSeq;
        const admin = _isAdmin(identity);
        const calls = [
            callTool('space_list', {}).catch(() => ({ status: 'error', message: 'Request failed' })),
            callTool('bank_consolidation_queues', { space_ids: '' }).catch(() => ({ status: 'error', message: 'Request failed' })),
        ];
        if (admin) {
            calls.push(callTool('admin_list_tokens', { include_revoked: true }).catch(() => ({ status: 'error', message: 'Request failed' })));
        }
        const [spacesResp, queuesResp, tokensResp] = await Promise.all(calls);
        if (seq !== _restSeq || AdminRouter.epoch !== epochAtCall) return;
        _applySpaces(spacesResp, _hasManage(identity));
        _applyQueues(queuesResp);
        if (admin) _applyTokens(true, tokensResp);
    }

    registerAction('dash-refresh-rest', () => {
        const btn = document.getElementById('dashRestRefreshBtn');
        if (btn) btn.disabled = true;
        _loadRest(AdminRouter.epoch, _ctx().identity).finally(() => {
            if (btn && btn.isConnected) btn.disabled = false;
        });
    });

    function render(contentEl, params, ctx) {
        _epoch = ctx.epoch;
        _lastHealth = null;
        const identity = ctx.identity || {};
        const admin = _isAdmin(identity);

        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Dashboard', `
                <button type="button" class="btn btn-secondary btn-sm" id="dashHealthRefreshBtn" data-action="dash-refresh-health" title="Runs a live LLM probe">${icon('refresh')} Refresh health</button>
                <button type="button" class="btn btn-secondary btn-sm" id="dashRestRefreshBtn" data-action="dash-refresh-rest">${icon('refresh')} Refresh</button>
            `)}
            <div class="metric-grid">
                <div class="metric-card" id="dashHealthCard">${stateLoading('')}</div>
                <div class="metric-card" id="dashIdentityCard">${_identityCardBody(identity)}</div>
                <div class="metric-card" id="dashSpacesTile">${stateLoading('')}</div>
                <a class="metric-card" href="#/access" id="dashTokensTile">${admin ? stateLoading('') : _tokensTileBody(false, null)}</a>
            </div>
            <div class="grid-2">
                ${panel(`<div class="panel-header"><h2>Consolidation</h2></div><div id="dashLanesPanel">${stateLoading('')}</div>`)}
                ${panel(`<div class="panel-header"><h2>Recent memory activity</h2></div><div id="dashActivityPanel">${stateLoading('')}</div>`)}
            </div>
        </div>`;

        _loadHealth(_epoch);
        _loadRest(_epoch, identity);
    }

    AdminViews.register('dashboard', render);
})();
