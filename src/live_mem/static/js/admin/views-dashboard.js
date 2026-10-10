/** Dashboard: recent consolidated spaces from lightweight metadata; explicit
 * visible-card queues and <=3 selected note feeds share PortalRefresh. */
(function () {
    const BEST_EFFORT_TOOLTIP = 'Job state lives in server memory: it does not survive a restart and history is trimmed.';

    let _epoch = -1;
    let _lastHealth = null;
    let _viewCtx = null;
    let _spaces = [];
    let _lanes = [];

    function _isAdmin(identity) {
        return !!(identity && Array.isArray(identity.permissions) && identity.permissions.includes('admin'));
    }

    function _hasManage(identity) {
        if (!identity || !Array.isArray(identity.permissions)) return false;
        return identity.auth_type === 'bootstrap' || identity.permissions.includes('manage') || _isAdmin(identity);
    }

    function _canConsolidate(identity) {
        const permissions = identity && Array.isArray(identity.permissions) ? identity.permissions : [];
        return permissions.some(permission => ['write', 'manage', 'admin'].includes(permission));
    }

    function _current(ctx) {
        return !!ctx && AdminRouter.epoch === ctx.epoch && sessionGenerationIsCurrent(ctx.sessionGeneration);
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
            <button type="button" class="btn btn-secondary btn-sm" id="dashHealthModalRefreshBtn" data-action="dash-refresh-health-modal">${icon('refresh')} Check services</button>
        </div>`;
    }

    function _setHealthModalRefreshButton(inFlight) {
        const btn = document.getElementById('dashHealthModalRefreshBtn');
        if (!btn) return;
        btn.disabled = inFlight;
        btn.innerHTML = inFlight ? 'Checking…' : `${icon('refresh')} Check services`;
    }

    // The shell owns one session-bound probe; all visible copies share its snapshot.
    function _applyHealth() {
        if (AdminRouter.epoch !== _epoch) return;
        _setHealthModalRefreshButton(!!_portalHealthFlight);
        const resp = _dashHealth;
        if (!resp || _portalHealthFlight) return;
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
        void checkPortalServices();
    });

    registerAction('dash-open-health', () => {
        if (!_lastHealth) {
            showModal('System health', stateUnavailable('Health data is not available yet.'));
            return;
        }
        showModal('System health', _healthModalBody(_lastHealth));
    });

    registerAction('dash-refresh-health-modal', () => {
        void checkPortalServices();
    });
    document.addEventListener('portal:services-change', _applyHealth);

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
        return `<a class="dash-tile-link" href="#/spaces">
            <span class="micro-label">Accessible spaces</span>
            <span class="metric-value">${esc(String(total))}</span>
            <span class="body-small dash-meta">Explore all spaces →</span>
        </a>`;
    }

    // Home owns one bounded activity cycle; the shared controller owns scheduling.
    let _home = null;
    let _selectionSession = null;
    let _selectedSpaces = [];
    const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;

    function _html(el, html) {
        if (el && el.innerHTML !== html) el.innerHTML = html;
    }

    function _freshness(el, timestamp, error) {
        _html(el, `${timestamp ? `Updated ${renderTimestamp(timestamp)}` : 'Not updated yet'}${error ? `<p class="state-degraded" role="status">${esc(error)}</p>` : ''}`);
    }

    function _consolidationTime(value) {
        if (typeof value !== 'string') return null;
        const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(value);
        if (!match) return null;
        const [, year, month, day, hour, minute, second, zoneHour = '0', zoneMinute = '0'] = match;
        if (+month < 1 || +month > 12 || +day < 1 || +day > new Date(Date.UTC(+year, +month, 0)).getUTCDate()
            || +hour > 23 || +minute > 59 || +second > 59 || +zoneHour > 23 || +zoneMinute > 59) return null;
        const timestamp = Date.parse(value);
        return Number.isFinite(timestamp) ? timestamp : null;
    }

    function _cardLayout() {
        const width = document.getElementById('dashRecentSpaces')?.clientWidth || 320;
        const columns = width >= 900 ? 3 : width >= 560 ? 2 : 1;
        const height = window.innerHeight;
        const rows = Math.max(height >= 760 ? 2 : 1, Math.min(4, Math.floor((height - 360) / 240)));
        return { columns, limit: Math.min(12, columns * rows) };
    }

    function _visibleIds(view) {
        return view.recent.slice(0, view.cardLimit).map(space => space.space_id);
    }

    function _paintSpaces(view) {
        const parent = document.getElementById('dashRecentSpaces');
        const layout = _cardLayout();
        view.cardLimit = layout.limit;
        parent.style.setProperty('--dash-columns', String(layout.columns));
        const visible = view.recent.slice(0, view.cardLimit);
        const latest = view.recent[0];
        const invalidDates = _spaces.filter(space => _consolidationTime(space.last_consolidation) === null
            && ((space.last_consolidation != null && space.last_consolidation !== '')
                || [space.consolidation_count, space.total_notes_processed].some(value => Number.isFinite(value) && value > 0))).length;
        _html(document.getElementById('dashLatestSignal'), latest
            ? `<span>Latest consolidation</span><a href="${esc('#/spaces/' + encodeURIComponent(latest.space_id) + '/consolidation')}">${esc(latest.space_id)}</a>${renderTimestamp(latest.last_consolidation)}`
            : invalidDates ? 'Consolidation dates unavailable.' : _spaces.length ? 'Your first consolidation will appear here.' : 'Start a shared memory space to see its activity.');
        const retained = new Set();
        const counter = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'Unavailable';
        visible.forEach((space, index) => {
            retained.add(space.space_id);
            let card = view.cards.get(space.space_id);
            if (!card) {
                card = document.createElement('article'); card.className = 'dash-space-card';
                card.dataset.space = space.space_id; view.cards.set(space.space_id, card);
            }
            const focused = document.activeElement;
            const focusHref = focused && card.contains(focused) ? focused.getAttribute('href') : null;
            _html(card, `<div class="dash-space-card-heading"><span class="micro-label">${index === 0 ? 'Latest consolidation' : 'Last consolidated'}</span>${renderTimestamp(space.last_consolidation)}</div>
                <h3><a href="${esc('#/spaces/' + encodeURIComponent(space.space_id))}">${esc(space.space_id)}</a></h3>
                <p class="dash-space-description">${esc(space.description || 'Shared project memory')}</p>
                <div class="dash-space-results"><span class="micro-label">Lifetime totals</span><dl>
                    <div><dt>Consolidations</dt><dd>${esc(counter(space.consolidation_count))}</dd></div>
                    <div><dt>Notes processed</dt><dd>${esc(counter(space.total_notes_processed))}</dd></div>
                </dl></div><a class="dash-space-action" href="${esc('#/spaces/' + encodeURIComponent(space.space_id) + '/consolidation')}">View consolidation →</a>`);
            if (parent.children[index] !== card) parent.insertBefore(card, parent.children[index] || null);
            if (focusHref && document.activeElement !== focused) Array.from(card.querySelectorAll('a')).find(link => link.getAttribute('href') === focusHref)?.focus({ preventScroll: true });
        });
        for (const [id, card] of view.cards) if (!retained.has(id)) { card.remove(); view.cards.delete(id); }
        _html(document.getElementById('dashSpacesEmpty'), visible.length ? '' : stateEmpty({
            title: invalidDates ? 'Consolidation dates unavailable' : _spaces.length ? 'No consolidations yet' : 'No spaces available',
            hint: invalidDates ? 'Some consolidation dates could not be read. Explore Spaces for details.' : _spaces.length ? 'Consolidated spaces will appear here as notes become shared memory.'
                : _hasManage(view.ctx.identity) ? 'Create a space to start building shared memory.' : 'Ask a manager for access to a memory space.',
            actionHtml: _spaces.length ? '<a class="btn btn-secondary" href="#/spaces">Explore spaces</a>'
                : _hasManage(view.ctx.identity) ? '<a class="btn btn-primary" href="#/spaces" data-action="dash-create-space">Create a space</a>' : '',
        }));
        const label = document.getElementById('dashCardScope');
        if (label) label.textContent = visible.length ? `Latest ${visible.length} of ${view.recent.length} consolidated spaces · jobs below cover these cards only.${invalidDates ? ` ${invalidDates} consolidation date(s) unavailable.` : ''}`
            : 'Recent consolidations across your accessible spaces.';
    }

    function _resetQueues(view) {
        view.data = null; view.lastSuccess = null; _lanes = [];
        _paintQueues(view, { lanes: [] });
        _freshness(document.getElementById('dashLanesFreshness'), null, 'Loading visible activity…');
    }

    function _resizeCards() {
        const view = _home;
        if (!view || !_current(view.ctx) || !view.inventorySuccess || document.hidden || !PortalRefresh.state().available) return;
        if (PortalRefresh.state().busy) { view.resizePending = true; return; }
        view.resizePending = false;
        const before = _visibleIds(view).join(',');
        _paintSpaces(view);
        if (before !== _visibleIds(view).join(',')) { _resetQueues(view); _visibleRefresh(); }
    }
    window.addEventListener('resize', _resizeCards);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) _resizeCards(); });

    function _controls() {
        if (!_home || !_current(_home.ctx)) return;
        const busy = PortalRefresh.state().busy;
        const set = (id, disabled) => { const el = document.getElementById(id); if (el) { el.disabled = disabled; el.setAttribute('aria-disabled', String(busy || disabled)); } };
        set('dashNoteSpace', !_spaces.length || _selectedSpaces.length >= 3);
        set('dashAddNotes', !_spaces.length || _selectedSpaces.length >= 3);
        set('dashStartConsolidationBtn', !_spaces.length || !_canConsolidate(_home.ctx.identity));
        if (!busy && _home.resizePending) _resizeCards();
        for (const card of _home.notes.values()) card.node.querySelector('button').setAttribute('aria-disabled', String(busy));
    }
    document.addEventListener('portal:refresh-change', _controls);

    function _applySpaces(resp, canManage) {
        const previousIds = _visibleIds(_home).join(',');
        _spaces = resp.spaces.filter(space => space && typeof space.space_id === 'string' && SPACE_ID_RE.test(space.space_id))
            .filter((space, index, all) => all.findIndex(other => other.space_id === space.space_id) === index);
        _home.recent = _spaces.filter(space => _consolidationTime(space.last_consolidation) !== null)
            .sort((a, b) => _consolidationTime(b.last_consolidation) - _consolidationTime(a.last_consolidation)
                || (a.space_id < b.space_id ? -1 : a.space_id > b.space_id ? 1 : 0));
        _selectedSpaces = _selectedSpaces.filter(id => _spaces.some(space => space.space_id === id));
        _html(document.getElementById('dashSpacesTile'), _spacesTileBody(resp, canManage));
        const select = document.getElementById('dashNoteSpace');
        if (select) {
            const previous = select.value;
            _html(select, '<option value="">Select a space</option>' + _spaces.map(space => `<option value="${esc(space.space_id)}">${esc(space.space_id)}</option>`).join(''));
            if (_spaces.some(space => space.space_id === previous)) select.value = previous;
        }
        _syncNoteCards(_home);
        _paintSpaces(_home);
        if (previousIds !== _visibleIds(_home).join(',')) {
            _resetQueues(_home);
        }
    }

    function _jobRow(job, view) {
        const key = `${job.space_id}/${job.job_id}`;
        let row = view.rows.get(key);
        if (!row) {
            row = document.createElement('article');
            row.className = 'consol-job-row';
            row.dataset.jobId = String(job.job_id || ''); row.dataset.space = job.space_id;
            row.innerHTML = `<div class="consol-job-heading"><a class="consol-space-name" href="${esc('#/spaces/' + encodeURIComponent(job.space_id))}">${esc(job.space_id)}</a><span data-part="status"></span>${job.job_id ? `<button type="button" class="btn btn-secondary btn-sm" data-action="dash-job" data-job-id="${esc(job.job_id)}" data-space="${esc(job.space_id)}">Details</button>` : ''}</div><p class="consol-job-meta body-small" data-part="meta"></p><div data-part="progress"></div><div data-part="outcome"></div><div data-part="id"></div>`;
            view.rows.set(key, row);
        }
        const labels = { running: 'Running', queued: 'Queued', succeeded: 'Completed', failed: 'Failed' };
        const active = ['running', 'queued'].includes(job.status);
        const partial = job.result && (job.result.status === 'partial' || (job.result.status !== 'error'
            && (job.result.partial === true || (Number.isSafeInteger(job.result.batches_completed)
                && Number.isSafeInteger(job.result.batches_total) && job.result.batches_completed > 0
                && job.result.batches_completed < job.result.batches_total))));
        const label = Object.hasOwn(labels, job.status) ? labels[job.status] : 'Unknown';
        _html(row.querySelector('[data-part="status"]'), statusDot(job.status === 'failed' ? 'error' : active || partial ? 'warn' : job.status === 'succeeded' ? 'ok' : 'neutral', label));
        const stamp = active ? job.started_at || job.queued_at || job.requested_at : job.finished_at;
        const elapsed = job.status === 'running' && Number.isFinite(Date.parse(job.started_at)) ? `${Math.max(0, Math.floor((Date.now() - Date.parse(job.started_at)) / 1000))} s elapsed at refresh` : '';
        const position = job.status === 'queued' && Number.isSafeInteger(job.queue_position) && job.queue_position >= 2 ? ` · Position ${job.queue_position}` : '';
        _html(row.querySelector('[data-part="meta"]'), _activityMeta(job, position, stamp, elapsed));
        _html(row.querySelector('[data-part="progress"]'), active ? PortalJobView.progressBar(job.progress) : '');
        _html(row.querySelector('[data-part="outcome"]'), _activityOutcome(job, partial, label));
        _html(row.querySelector('[data-part="id"]'), job.job_id ? `<span title="${esc(job.job_id)}">${copyable(job.job_id, truncateMiddle(job.job_id, 10, 6))}</span>` : '');
        return row;
    }

    function _activityMeta(job, position, stamp, elapsed) {
        const scope = typeof job.scope_label === 'string' && job.scope_label ? job.scope_label : 'Scope unavailable';
        const agent = typeof job.agent === 'string' ? job.agent : '';
        const requester = typeof job.requested_by === 'string' ? job.requested_by : '';
        const scopeNamesAgent = !!agent && (scope === `Agent: ${agent}` || scope === `Agent ${agent}`);
        const actors = [];
        if (agent && !scopeNamesAgent && agent !== requester) actors.push(`Agent ${agent}`);
        if (requester && requester !== agent) actors.push(`Requested by ${requester}`);
        else if (requester && !scopeNamesAgent) actors.push(`Requested by ${requester}`);
        const time = stamp && Number.isFinite(Date.parse(stamp)) ? renderTimestamp(stamp) : 'Time unavailable';
        return [esc(scope) + esc(position), ...actors.map(esc), time, elapsed].filter(Boolean).join(' · ');
    }

    function _activityOutcome(job, partial, label) {
        const result = job.result && typeof job.result === 'object' ? job.result : null;
        const state = job.kind === 'manual_compaction'
            ? (job.status === 'running' ? 'Manual compaction in progress'
                : job.status === 'failed' ? 'Manual compaction failed'
                    : job.status === 'succeeded' ? 'Manual compaction completed' : 'Manual compaction status unavailable')
            : job.status === 'failed' ? 'Consolidation failed'
            : job.status === 'queued' ? 'Waiting for consolidation to start'
                : job.status === 'running' ? 'Consolidation in progress'
                    : job.status === 'succeeded' ? (partial ? 'Consolidation result incomplete' : 'Consolidation completed')
                        : 'Consolidation status unavailable';
        const summary = [`<p class="dash-job-summary${job.status === 'failed' ? ' state-error' : ''}">${esc(state)}</p>`];
        // A partial result may represent durable ambiguity, even with no completed
        // batch. Preserve the terminal failure and report only the server's counts.
        if (partial) {
            summary.push('<p class="dash-job-partial body-small">Partial result reported</p>');
            if (result && Number.isSafeInteger(result.batches_completed) && Number.isSafeInteger(result.batches_total)
                && result.batches_completed >= 0 && result.batches_total >= result.batches_completed) {
                summary.push(`<p class="dash-job-result body-small">${result.batches_completed} of ${result.batches_total} batches reported completed</p>`);
            }
        }
        if (job.kind === 'manual_compaction' && result?.recovery_required === true) summary.push(`<p class="body-small">${statusDot('error', 'Recovery required')}</p>`);
        if (job.status === 'succeeded' && !partial && result && Number.isFinite(result.notes_processed) && Number.isFinite(result.notes_total)) {
            const notes = result.notes_total === 0 ? 'No notes to process'
                : `${result.notes_processed} of ${result.notes_total} notes processed`;
            summary.push(`<p class="dash-job-result body-small">${esc(notes)}</p>`);
        }
        if (label === 'Unknown') summary.push(`<p class="body-small">Status code: ${esc(job.status || 'Not reported')}</p>`);

        const compaction = result && result.auto_compaction && typeof result.auto_compaction === 'object'
            ? result.auto_compaction : null;
        const compactionLabels = {
            ok: 'Files compacted', not_needed: 'No oversized files', disabled: 'Disabled by configuration',
            not_applicable: 'Space not eligible', error: 'Compaction failed', partial: 'Compaction incomplete',
            cancelled: 'Compaction interrupted',
        };
        let maintenance = '';
        if (compaction) {
            const compactionLabel = Object.hasOwn(compactionLabels, compaction.status)
                ? compactionLabels[compaction.status] : 'Unknown compaction outcome';
            const severity = compaction.status === 'error' || compaction.recovery_required === true ? 'error'
                : ['partial', 'cancelled'].includes(compaction.status) ? 'warn'
                    : compaction.status === 'ok' ? 'ok' : 'neutral';
            maintenance = `<p class="dash-maintenance-summary body-small">${statusDot(severity, `Automatic compaction: ${compactionLabel}`)}${compaction.recovery_required === true ? ` ${statusDot('error', 'Recovery required')}` : ''}</p>`;
            summary.push(maintenance);
        }

        const jobDetails = [job.error, job.message].some(value => typeof value === 'string' && value.length > 0)
            ? `${job.error ? serverMessage(job.error) : ''}${job.message ? serverMessage(job.message) : ''}` : '';
        const compactionDetails = compaction ? renderAutoCompaction(result) : '';
        const details = jobDetails || compactionDetails
            ? `<details class="dash-job-details"><summary>Server and maintenance details</summary>${jobDetails}${compactionDetails}</details>`
            : '';
        return summary.join('') + details;
    }

    function _paintQueues(view, data) {
        const focused = document.activeElement;
        const restoreFocus = focused && [...view.rows.values()].some(row => row.contains(focused));
        const jobs = PortalJobView.collectJobs(data.lanes);
        const active = [...jobs.active.filter(job => job.status === 'running'), ...jobs.active.filter(job => job.status === 'queued'), ...jobs.unreported];
        const recent = jobs.history.slice(0, 10);
        const noScope = !view.recent.length && !!view.inventorySuccess;
        const denied = data.denied_spaces || [];
        const missing = view.data ? _visibleIds(view).filter(id => !data.lanes.some(lane => lane.space_id === id) && !denied.some(item => item.space_id === id)) : [];
        const unknown = data.lanes.filter(lane => !['idle', 'running', 'queued', 'failed'].includes(lane.lane_state) || (lane.queued_count > 0 && !(lane.queued_jobs || []).length));
        const complete = !missing.length && !denied.length && !unknown.length;
        const retained = new Set();
        for (const [id, items] of [['dashRunningJobs', active], ['dashRecentJobs', recent]]) {
            const parent = document.getElementById(id);
            if (!parent) continue;
            items.forEach((job, index) => {
                retained.add(`${job.space_id}/${job.job_id}`);
                const row = _jobRow(job, view);
                if (parent.children[index] !== row) parent.insertBefore(row, parent.children[index] || null);
            });
        }
        for (const [key, row] of view.rows) if (!retained.has(key)) { row.remove(); view.rows.delete(key); }
        if (restoreFocus && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
        _html(document.getElementById('dashActivityEmpty'), active.length ? '' : stateEmpty(noScope
            ? { title: 'No recent spaces to follow', hint: 'Jobs for all accessible spaces are available in Consolidation.' } : view.data
            ? { title: data.lanes.length && complete ? 'No jobs in progress' : 'Activity unavailable for some cards', hint: data.lanes.length && complete ? 'Use Auto-refresh to discover work started by agents.' : 'No accessible activity was returned. Open Consolidation for the full view.' }
            : { title: 'Activity not loaded for these cards', hint: 'See the activity status above.' }));
        _html(document.getElementById('dashRecentEmpty'), recent.length ? '' : stateEmpty(noScope
            ? { title: 'Explore Consolidation for job history', hint: 'Recent consolidated spaces will appear above when available.' } : view.data
            ? { title: complete ? 'No completed jobs available' : 'Recent history unavailable for some cards', hint: complete ? 'History may have been cleared by a restart or trimmed.' : 'Open Consolidation to inspect all accessible activity.' }
            : { title: 'Recent jobs not loaded', hint: 'See the activity status above.' }));
        _html(document.getElementById('dashQueueWarnings'), missing.map(id => serverMessage(`${id}: Activity unavailable`)).join('') + denied.map(item => serverMessage(`${item.space_id}: ${item.message || 'Not accessible'}`)).join('') + unknown.map(lane => serverMessage(`${lane.space_id}: ${lane.queued_count > 0 ? 'Queued jobs; details unavailable' : 'Consolidation state unavailable'}`)).join(''));
        const guarantees = [...new Set(data.lanes.map(lane => lane.guarantee).filter(value => typeof value === 'string' && value))];
        _html(document.getElementById('dashHistoryGuarantee'), guarantees.map(value => `<p>History guarantee: <code>${esc(value)}</code></p>`).join(''));
        _updateInspector(view);
    }

    function _inspectorCurrent(view) {
        const inspector = view.inspector;
        return inspector && _home === view && _current(view.ctx) && inspector.node.isConnected
            && document.getElementById('dashJobSnapshot') === inspector.node
            && document.getElementById('adminModal')?.style.display === 'flex';
    }

    function _updateInspector(view, error = '') {
        if (!_inspectorCurrent(view)) return;
        const inspector = view.inspector;
        if (!['running', 'queued'].includes(inspector.job.status)) return;
        const match = !error && view.data && view.data.lanes.flatMap(lane => [lane.running_job, lane.manual_compaction, ...(lane.queued_jobs || []), ...(lane.latest_jobs || [])].filter(Boolean).map(job => ({ ...job, space_id: lane.space_id })))
            .find(job => job.job_id === inspector.job.job_id && job.space_id === inspector.job.space_id);
        if (match) {
            inspector.job = match; inspector.lastSuccess = view.lastSuccess;
            const focused = document.activeElement;
            const restoreFocus = focused && inspector.node.contains(focused) && focused.dataset;
            _html(inspector.node, PortalJobView.renderJob(match));
            if (restoreFocus) Array.from(inspector.node.querySelectorAll('[data-action]')).find(button =>
                button.dataset.action === restoreFocus.action && button.dataset.value === restoreFocus.value)?.focus();
        }
        _freshness(document.getElementById('dashJobFreshness'), inspector.lastSuccess, error || (!match ? 'This job is no longer returned on these visible cards. The last snapshot is retained; open Consolidation to inspect it.' : ''));
    }

    registerAction('dash-job', data => {
        const view = _home;
        if (!view || !_current(view.ctx) || !view.data) return;
        const jobs = PortalJobView.collectJobs(view.data.lanes);
        const job = [...jobs.active, ...jobs.history, ...jobs.unreported].find(item => item.job_id === data.jobId && item.space_id === data.space);
        if (!job) return;
        showModal('Consolidation job', `<div id="dashJobFreshness"></div><div id="dashJobSnapshot"></div><p><a href="${esc('#/spaces/' + encodeURIComponent(job.space_id) + '/consolidation')}" data-action="portal-open-jobs" data-space-id="${esc(job.space_id)}">Open Consolidation</a></p>`);
        view.inspector = { job, lastSuccess: view.lastSuccess, node: document.getElementById('dashJobSnapshot') };
        _html(view.inspector.node, PortalJobView.renderJob(job));
        _freshness(document.getElementById('dashJobFreshness'), view.lastSuccess, '');
    });

    function _syncNoteCards(view) {
        const parent = document.getElementById('dashNotesPanel');
        for (const [id, card] of view.notes) if (!_selectedSpaces.includes(id)) { card.node.remove(); view.notes.delete(id); }
        for (const id of _selectedSpaces) if (!view.notes.has(id)) {
            const node = document.createElement('section'); node.className = 'panel'; node.dataset.space = id;
            node.innerHTML = `<div class="panel-header"><h3>${esc(id)}</h3><button type="button" class="btn btn-ghost btn-sm" data-action="dash-remove-notes" data-space="${esc(id)}" aria-label="Remove notes for ${esc(id)}">Remove</button></div><div data-part="freshness" class="body-small text-muted"></div><div data-part="list"></div><p data-part="bound" class="body-small"></p>`;
            parent.appendChild(node); view.notes.set(id, { node, rows: new Map(), lastSuccess: null });
        }
        _html(document.getElementById('dashNoteMessage'), !_selectedSpaces.length ? '<p class="body-small">Choose up to three spaces to read their latest notes. No notes are read until selected.</p>' : `<p class="body-small">${_selectedSpaces.length} of 3 selected. Showing at most 20 notes per space. Each refresh reads all notes in these spaces.</p>`);
    }

    function _paintNotes(card, data) {
        const parent = card.node.querySelector('[data-part="list"]');
        const retained = new Set();
        data.notes.slice(0, 20).forEach((note, index) => {
            const key = String(note.filename || note.note_id || index); retained.add(key);
            let row = card.rows.get(key);
            if (!row) { row = document.createElement('article'); row.className = 'consol-job-row'; row.dataset.noteId = key; row.innerHTML = '<p class="body-small" data-part="meta"></p><p data-part="content"></p>'; card.rows.set(key, row); }
            _html(row.querySelector('[data-part="meta"]'), `${esc(note.agent || 'Agent unavailable')} · ${esc(note.category || 'Category unavailable')} · ${note.timestamp ? renderTimestamp(note.timestamp) : 'Time unavailable'}`);
            const content = row.querySelector('[data-part="content"]');
            const text = String(note.content ?? '');
            if (content.textContent !== text) content.textContent = text;
            if (parent.children[index] !== row) parent.insertBefore(row, parent.children[index] || null);
        });
        for (const [key, row] of card.rows) if (!retained.has(key)) { row.remove(); card.rows.delete(key); }
        card.node.querySelector('[data-part="bound"]').textContent = data.has_more === true ? 'More notes are available in Short memory.' : data.notes.length ? '' : 'No notes returned.';
    }

    async function _readQueues(view, ids, current) {
        if (!ids.length) {
            view.data = null; view.lastSuccess = null; _lanes = []; _paintQueues(view, { lanes: [] });
            _html(document.getElementById('dashLanesFreshness'), view.inventorySuccess ? 'No consolidated spaces available for activity.' : 'Activity awaits a successful space inventory.');
            return;
        }
        try {
            const data = await callTool('bank_consolidation_queues', { space_ids: ids.join(',') });
            if (!current()) return;
            if (!data || data.status !== 'ok' || !Array.isArray(data.lanes)) throw new Error(data?.message || 'Activity read failed');
            view.data = { ...data, lanes: data.lanes.filter(lane => lane && ids.includes(lane.space_id)), denied_spaces: (data.denied_spaces || []).filter(item => item && ids.includes(item.space_id)) };
            _lanes = view.data.lanes; view.lastSuccess = new Date().toISOString();
            _paintQueues(view, view.data); _freshness(document.getElementById('dashLanesFreshness'), view.lastSuccess, '');
        } catch (error) {
            if (current()) { _freshness(document.getElementById('dashLanesFreshness'), view.lastSuccess, error.message || 'Activity read failed'); _updateInspector(view, 'Activity read failed. Last snapshot retained.'); }
            throw error;
        }
    }

    async function _readNotes(view, id, current) {
        const card = view.notes.get(id);
        try {
            const data = await callTool('live_read', { space_id: id, limit: 20 });
            if (!current()) return;
            if (!data || data.status !== 'ok' || !Array.isArray(data.notes)) throw new Error(data?.message || 'Notes read failed');
            _paintNotes(card, data); card.lastSuccess = new Date().toISOString();
            _freshness(card.node.querySelector('[data-part="freshness"]'), card.lastSuccess, '');
        } catch (error) {
            if (current()) _freshness(card.node.querySelector('[data-part="freshness"]'), card.lastSuccess, error.message || 'Notes read failed');
            throw error;
        }
    }

    async function _refreshHome({ automatic, isCurrent }) {
        const view = _home;
        const current = () => _home === view && _current(view.ctx) && isCurrent();
        if (!current()) return;
        const inventory = !view.visibleOnly; view.visibleOnly = false;
        let failure = null;
        if (inventory) {
            try {
                const spacesResp = await callTool('space_list', { include_counts: false });
                if (!current()) return;
                if (!spacesResp || spacesResp.status !== 'ok' || !Array.isArray(spacesResp.spaces)) throw new Error(spacesResp?.message || 'Space inventory read failed');
                _applySpaces(spacesResp, _hasManage(view.ctx.identity)); view.inventorySuccess = new Date().toISOString();
                _freshness(document.getElementById('dashInventoryFreshness'), view.inventorySuccess, '');
            } catch (error) {
                if (!current()) return;
                failure = error; _freshness(document.getElementById('dashInventoryFreshness'), view.inventorySuccess, error.message || 'Space inventory read failed');
                if (!view.inventorySuccess) {
                    _html(document.getElementById('dashSpacesTile'), _spacesTileBody({ status: 'error', message: error.message }, _hasManage(view.ctx.identity)));
                    _html(document.getElementById('dashSpacesEmpty'), stateUnavailable('Recent consolidations unavailable. Retry the overview.'));
                    _html(document.getElementById('dashLatestSignal'), 'Recent consolidation data unavailable.');
                }
            }
        }
        if (!current()) return;
        const results = await Promise.allSettled([_readQueues(view, _visibleIds(view), current), ..._selectedSpaces.map(id => _readNotes(view, id, current))]);
        if (!current()) return;
        failure ||= results.find(result => result.status === 'rejected')?.reason;
        if (failure) throw failure;
        return { follow: true };
    }

    function _visibleRefresh() {
        _home.visibleOnly = true;
        void PortalRefresh.refresh().catch(() => {});
    }
    function _canChangeScope() { return _home && _current(_home.ctx) && PortalRefresh.state().available && !PortalRefresh.state().busy; }
    registerAction('dash-add-notes', () => {
        if (!_canChangeScope() || _selectedSpaces.length >= 3) return;
        const id = document.getElementById('dashNoteSpace')?.value;
        if (!_spaces.some(space => space.space_id === id) || _selectedSpaces.includes(id)) return;
        _selectedSpaces.push(id); _syncNoteCards(_home); _visibleRefresh();
    });
    registerAction('dash-remove-notes', data => {
        if (!_canChangeScope()) return;
        _selectedSpaces = _selectedSpaces.filter(id => id !== data.space); _syncNoteCards(_home); _controls();
    });
    registerAction('dash-refresh-rest', () => { if (_home && _current(_home.ctx)) void PortalRefresh.refresh().catch(() => {}); });
    registerAction('dash-start-consolidation', () => {
        if (!_current(_viewCtx) || !_canConsolidate(_viewCtx.identity) || !_spaces.length) return;
        const ctx = _viewCtx, spaces = _spaces;
        openConsolidationLauncher({ spaces, lanes: _lanes, ctx, onSubmitted: result => {
            if (!_current(ctx) || !result || !spaces.some(space => space.space_id === result.space_id)) return;
            AdminRouter.go(`/spaces/${encodeURIComponent(result.space_id)}/consolidation`);
        } });
    });

    function render(contentEl, params, ctx) {
        _epoch = ctx.epoch; _lastHealth = null;
        const identity = ctx.identity || {};
        _viewCtx = { epoch: _epoch, sessionGeneration: ctx.sessionGeneration ?? currentSessionGeneration(), identity };
        if (_selectionSession !== _viewCtx.sessionGeneration) _selectedSpaces = [];
        _selectionSession = _viewCtx.sessionGeneration;
        _spaces = []; _lanes = [];
        _home = { ctx: _viewCtx, recent: [], cards: new Map(), cardLimit: _cardLayout().limit, resizePending: false, visibleOnly: false, data: null, lastSuccess: null, inventorySuccess: null, rows: new Map(), notes: new Map(), inspector: null };
        contentEl.innerHTML = `<div class="page dash-page">
            ${pageHeader('Dashboard', '')}
            <section class="dash-hero" aria-label="Shared memory overview"><div><p class="micro-label">Collective memory</p><h2>Memory in motion.</h2><div id="dashLatestSignal" class="dash-latest-signal">Loading recent consolidations…</div></div><img src="/static/img/hivemind-mark-dark.svg" alt="" width="150" height="150"></section>
            <section class="dash-consolidated" aria-labelledby="dashRecentTitle"><div class="dash-section-heading"><div><h2 id="dashRecentTitle">Recently consolidated</h2><p id="dashCardScope" class="body-small text-muted">Recent consolidations across your accessible spaces.</p></div><a class="btn btn-ghost btn-sm" href="#/spaces">All spaces →</a></div>
            <div id="dashInventoryFreshness" class="body-small text-muted"></div><div id="dashRecentSpaces" class="dash-space-grid"></div><div id="dashSpacesEmpty">${stateLoading('Loading recent consolidations…')}</div></section>
            <details class="dash-operations" open><summary>Live jobs and recent results</summary>
            ${_canConsolidate(identity) ? '<button type="button" class="btn btn-primary btn-sm" id="dashStartConsolidationBtn" data-action="dash-start-consolidation" disabled>Start a consolidation</button>' : ''}<p class="body-small text-muted">Activity for the spaces shown above. Open a space’s Consolidation view for its full queue.</p>
            <div id="dashLanesFreshness" class="body-small text-muted"></div>
            ${panel('<div class="panel-header"><h2>In progress</h2></div><div id="dashLanesPanel"><div id="dashRunningJobs"></div><div id="dashActivityEmpty">Loading activity…</div><div id="dashQueueWarnings"></div></div>')}
            ${panel(`<div class="panel-header"><h2>Recent jobs</h2></div><div id="dashActivityPanel"><div id="dashRecentJobs"></div><div id="dashRecentEmpty"></div><details class="diagnostic-details body-small"><summary>History details</summary><p>Up to 10 recent jobs for these cards.</p><div id="dashHistoryGuarantee"></div><p>${esc(BEST_EFFORT_TOOLTIP)}</p></details></div>`)}
            </details><details class="dash-notes"><summary>Read latest notes</summary>
            ${panel('<div class="panel-header"><h2>Latest notes</h2></div><div class="consol-stale-controls"><div class="form-group"><label class="form-label" for="dashNoteSpace">Read notes from a space</label><select class="form-input" id="dashNoteSpace" disabled><option value="">Select a space</option></select></div><button type="button" class="btn btn-secondary" id="dashAddNotes" data-action="dash-add-notes" disabled>Show notes</button></div><div id="dashNoteMessage"></div><div id="dashNotesPanel"></div>')}
            </details><details class="dash-diagnostics"><summary>Instance overview</summary><div class="metric-grid"><div class="metric-card" id="dashSpacesTile">${stateLoading('Loading spaces…')}</div><div class="metric-card" id="dashHealthCard"><div class="dash-card-body"><span class="micro-label">System health</span><p>Services not checked.</p></div></div></div></details>
        </div>`;
        _syncNoteCards(_home); _applyHealth();
        document.getElementById('dashRecentSpaces').style.setProperty('--dash-columns', String(_cardLayout().columns));
        PortalRefresh.register({ refresh: _refreshHome, canAuto: () => _current(_viewCtx) }, _viewCtx);
        if (!PortalRefresh.state().available) {
            _selectedSpaces = []; _syncNoteCards(_home);
            const message = 'Refresh unavailable. Sign out and sign in again.';
            _html(document.getElementById('dashInventoryFreshness'), stateUnavailable(message));
            _html(document.getElementById('dashSpacesTile'), stateUnavailable(message));
            _html(document.getElementById('dashActivityEmpty'), stateUnavailable(message));
            _html(document.getElementById('dashSpacesEmpty'), stateUnavailable(message));
            _html(document.getElementById('dashLatestSignal'), 'Recent consolidation data unavailable.');
            return;
        }
        void PortalRefresh.refresh().catch(() => {});
    }

    AdminViews.register('dashboard', render);
})();
