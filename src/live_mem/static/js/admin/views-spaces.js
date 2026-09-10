/**
 * Spaces inventory (§5.3, v1.5.2): one aggregate inventory and queues load.
 * Consolidation activity refreshes only for painted, known-active spaces.
 * Creation/recovery contracts and real-anchor navigation remain unchanged.
 */
(function () {
    const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
    const MAX_DESCRIPTION = 500;
    const MAX_RULES = 50000;
    const LIVE_REFRESH_MS = 60000;
    const BEST_EFFORT_TOOLTIP = 'Job state lives in server memory: it does not survive a restart and history is trimmed.';
    let _epoch = -1;
    let _identity = {};
    let _sessionGeneration = -1;
    let _spacesData = null;
    let _lanesById = null;
    let _query = '';
    let _liveTimer = null;
    let _pending = null;
    let _tableSeq = 0;
    let _updatedAt = null;
    let _queueError = false;
    let _liveStopped = false;
    let _queueDenied = [];

    function _isAdmin(identity) {
        return !!(identity && Array.isArray(identity.permissions) && identity.permissions.includes('admin'));
    }

    function _hasManage(identity) {
        if (!identity || !Array.isArray(identity.permissions)) return false;
        return identity.auth_type === 'bootstrap' || identity.permissions.includes('manage') || _isAdmin(identity);
    }

    function _liveIdentity() {
        return (typeof _ctx === 'function' && _ctx().identity) || {};
    }

    function _current(epoch, generation = _sessionGeneration) {
        return AdminRouter.epoch === epoch && sessionGenerationIsCurrent(generation);
    }

    function _clearLive() {
        if (_liveTimer !== null) clearTimeout(_liveTimer);
        _liveTimer = null;
    }

    function _laneActive(lane) {
        return !!(lane && (lane.running_job || (typeof lane.queued_count === 'number' && lane.queued_count > 0)));
    }

    function _scheduleLive(epoch) {
        if (_liveStopped) { _clearLive(); return; }
        const rows = _computeRows();
        if (!_current(epoch) || !rows.some(s => _laneActive(_lanesById && _lanesById[s.space_id]))) {
            _clearLive();
            return;
        }
        // Search changes the IDs read at the tick, not its existing deadline.
        if (_liveTimer !== null) return;
        const generation = _sessionGeneration;
        _liveTimer = setTimeout(() => {
            _liveTimer = null;
            if (!_current(epoch, generation)) return;
            if (document.hidden) { _scheduleLive(epoch); return; }
            _refreshActivity(epoch);
        }, LIVE_REFRESH_MS);
    }

    function _idCellHtml(id) {
        const href = `#/spaces/${encodeURIComponent(id)}`;
        const payload = esc(JSON.stringify(id));
        return `<a href="${esc(href)}" class="spaces-id-link">${esc(id)}</a>
            <button type="button" class="copy-btn" data-action="copy-value" data-value="${payload}" aria-label="Copy ${esc(id)}">${icon('copy')}</button>`;
    }

    function _terminalJobs(lane) {
        const jobs = lane && Array.isArray(lane.latest_jobs) ? lane.latest_jobs : [];
        const activeIds = new Set([
            lane && lane.running_job && lane.running_job.job_id,
            ...(lane && Array.isArray(lane.queued_jobs) ? lane.queued_jobs.map(j => j && j.job_id) : []),
            ...(lane && Array.isArray(lane.queued_job_ids) ? lane.queued_job_ids : []),
        ].filter(Boolean));
        return jobs.filter(j => j && ['succeeded', 'failed'].includes(j.status) && !activeIds.has(j.job_id));
    }

    function _latestFinished(lane) {
        const seen = new Set();
        return _terminalJobs(lane).filter(j => typeof j.finished_at === 'string' && Number.isFinite(Date.parse(j.finished_at)))
            .slice().sort((a, b) => Date.parse(b.finished_at) - Date.parse(a.finished_at))
            .filter(j => { if (j.job_id && seen.has(j.job_id)) return false; if (j.job_id) seen.add(j.job_id); return true; })[0] || null;
    }

    function _number(value) {
        return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
    }

    function _progressText(progress) {
        if (!progress) return '';
        const done = _number(progress.notes_done);
        const total = _number(progress.notes_total);
        if (done !== null && total !== null) return `${done} / ${total} notes`;
        if (done !== null) return `${done} notes processed`;
        const batches = _number(progress.batches_done);
        const batchTotal = _number(progress.batches_total);
        if (batches !== null && batchTotal !== null) return `${batches} / ${batchTotal} batches`;
        return progress.phase ? String(progress.phase) : '';
    }

    function _partialResult(job) {
        const result = job && job.result;
        return !!(result && (result.status === 'partial' || result.partial === true
            || (Number.isFinite(result.batches_completed) && Number.isFinite(result.batches_total)
                && result.batches_completed < result.batches_total)));
    }

    function _activityHtml(lane) {
        if (!lane) return statusDot('neutral', 'Unavailable');
        const queue = _number(lane.queued_count);
        const job = lane.running_job;
        if (job || (queue !== null && queue > 0)) {
            const label = job ? (job.status === 'running' ? 'Running' : 'Status unavailable') : 'Queued';
            const progress = job ? _progressText(job.progress) : '';
            const firstQueued = Array.isArray(lane.queued_jobs) ? lane.queued_jobs[0] : null;
            const scope = (job || firstQueued || {}).scope_label;
            const meta = [scope, queue === null ? 'Queue unavailable' : queue > 0 ? `${queue} queued` : ''].filter(Boolean);
            return `${statusDot('warn', label)}${progress ? ` <span>${esc(progress)}</span>` : ''}
                <span class="spaces-activity-meta">${meta.map(esc).join(' · ')}</span>`;
        }
        const latest = _latestFinished(lane);
        if (latest) {
            const partial = _partialResult(latest);
            const label = partial ? 'Partial' : latest.status === 'succeeded' ? 'Completed' : 'Failed';
            return `${statusDot(partial ? 'warn' : latest.status === 'succeeded' ? 'ok' : 'error', label)}
                <span class="spaces-activity-meta">${renderTimestamp(latest.finished_at)}</span>`;
        }
        const undated = _terminalJobs(lane)[0];
        if (undated) {
            const partial = _partialResult(undated);
            const label = partial ? 'Partial result' : undated.status === 'failed' ? 'Failed result' : 'Completed result';
            return `${statusDot(partial ? 'warn' : undated.status === 'failed' ? 'error' : 'neutral', label)}<span class="spaces-activity-meta">Result date unavailable</span>`;
        }
        if (lane.lane_state === 'failed') return statusDot('error', 'Last run failed');
        return statusDot('neutral', lane.lane_state === 'idle' && queue === 0 ? 'No recent history' : 'Unavailable');
    }

    function _tableRowsHtml(rows) {
        return rows.map(space => `<tr>
            <td data-label="Space"><div class="spaces-identity">${_idCellHtml(space.space_id)}</div>
                <p class="spaces-description" title="${esc(space.description || '')}">${esc(space.description || 'No description')}</p>
                <p class="spaces-owner">${esc(space.owner || 'Owner not specified')}</p></td>
            <td data-label="Memory"><div class="spaces-memory">
                <span class="spaces-memory-line">${esc(String(_number(space.live_notes_count) ?? '—'))} notes <span class="text-faint">SHORT</span></span>
                <span class="spaces-memory-line">${esc(String(_number(space.bank_files_count) ?? '—'))} bank files <span class="text-faint">MID</span></span></div></td>
            <td data-label="Consolidation" class="spaces-activity-cell" data-space="${esc(space.space_id)}">
                <a class="spaces-activity-link" href="${esc('#/consolidation/' + encodeURIComponent(space.space_id))}" title="${esc(BEST_EFFORT_TOOLTIP)}">${_activityHtml(_lanesById && _lanesById[space.space_id])}</a></td>
        </tr>`).join('');
    }

    function _computeRows() {
        const spaces = _spacesData && Array.isArray(_spacesData.spaces) ? _spacesData.spaces : [];
        const query = _query.trim().toLocaleLowerCase();
        return spaces.filter(s => [s.space_id, s.description, s.owner].some(v => String(v || '').toLocaleLowerCase().includes(query)))
            .slice().sort((a, b) => String(a.space_id).localeCompare(String(b.space_id), 'en'));
    }

    function _renderToolbar() {
        const el = document.getElementById('spacesToolbar');
        if (!el) return;
        el.innerHTML = `<div class="spaces-search"><label for="spacesSearch" class="form-label">Search spaces</label>
            <input type="search" id="spacesSearch" class="form-input" placeholder="Name, description or owner" value="${esc(_query)}"></div>`;
        const input = document.getElementById('spacesSearch');
        if (input) input.oninput = () => {
            _query = input.value;
            _renderBody();
            _scheduleLive(_epoch);
        };
    }

    function _renderFreshness() {
        const el = document.getElementById('spacesFreshness');
        if (el) el.dataset.stale = String(_queueError);
        if (el) el.innerHTML = _updatedAt
            ? `Consolidation updated ${renderTimestamp(_updatedAt)}. ${_queueError ? 'Update failed — showing last successful data.' : 'Refreshes every 60 s while shown jobs are active.'}${_liveStopped ? ' Automatic refresh stopped; use Refresh to try again.' : ''}`
            : (_queueError ? 'Consolidation data unavailable. Refresh to try again.' : 'Loading consolidation data…');
        const warnings = document.getElementById('spacesQueueWarnings');
        if (warnings) warnings.innerHTML = _queueDenied.map(d => serverMessage(`${d.space_id}: ${d.message || 'Access denied'}`)).join('');
    }

    function _renderBody() {
        const wrap = document.getElementById('spacesTableWrap');
        if (!wrap) return;
        if (!_spacesData) { wrap.innerHTML = stateLoading('Loading spaces…'); return; }
        if (_spacesData.status !== 'ok') {
            wrap.innerHTML = stateError({ title: "Couldn't load spaces", message: _spacesData.message, retryAction: 'spaces-refresh' });
            return;
        }
        const rows = _computeRows();
        if (!rows.length) {
            const anySpaces = Array.isArray(_spacesData.spaces) && _spacesData.spaces.length > 0;
            const canCreate = _hasManage(_identity);
            wrap.innerHTML = stateEmpty({
                title: anySpaces ? 'No spaces match your search' : 'No spaces yet',
                hint: anySpaces ? 'Try a different name, description or owner.' : canCreate ? 'Create your first space to get started.' : 'A manager can create the first space.',
                actionHtml: !anySpaces && canCreate ? '<button type="button" class="btn btn-primary btn-sm" data-action="spaces-open-create">Create space</button>' : '',
            });
            return;
        }
        wrap.innerHTML = dataTable(['Space', 'Memory', 'Consolidation'], _tableRowsHtml(rows));
    }

    function _applyQueues(resp, requestedIds) {
        if (resp && resp.status === 'ok' && Array.isArray(resp.lanes)) {
            if (!Array.isArray(requestedIds) || _lanesById === null) _lanesById = Object.create(null);
            if (Array.isArray(requestedIds)) requestedIds.forEach(id => { delete _lanesById[id]; });
            resp.lanes.forEach(lane => { if (lane && lane.space_id) _lanesById[lane.space_id] = lane; });
            const retainedDenied = Array.isArray(requestedIds)
                ? _queueDenied.filter(d => !requestedIds.includes(d.space_id)) : [];
            _queueDenied = retainedDenied.concat(Array.isArray(resp.denied_spaces) ? resp.denied_spaces : []);
            _updatedAt = new Date().toISOString();
            _queueError = false;
            _liveStopped = false;
        } else {
            // A failed read cannot turn a known running job into an idle one.
            _queueError = true;
            if (resp && ['rate_limited', 'truncated', 'read_only'].includes(resp.status)) _liveStopped = true;
        }
        _renderFreshness();
    }

    function _setRefreshing(value) {
        const btn = document.getElementById('spacesRefreshBtn');
        if (btn) btn.disabled = value;
    }

    async function _loadTable(epochAtCall) {
        if (_pending || !_current(epochAtCall)) return;
        _clearLive();
        const seq = ++_tableSeq;
        const generation = _sessionGeneration;
        const request = {};
        _pending = request;
        _setRefreshing(true);
        const [spacesResp, queuesResp] = await Promise.all([
            callTool('space_list', {}).catch(() => ({ status: 'error', message: 'Request failed' })),
            callTool('bank_consolidation_queues', { space_ids: '' }).catch(() => ({ status: 'error' })),
        ]);
        if (_pending === request) _pending = null;
        if (seq !== _tableSeq || !_current(epochAtCall, generation)) return;
        _spacesData = spacesResp;
        if (spacesResp && spacesResp.status === 'ok') cache.spaces = spacesResp.spaces || [];
        _applyQueues(queuesResp);
        _renderBody();
        _setRefreshing(false);
        _scheduleLive(epochAtCall);
    }

    async function _refreshActivity(epochAtCall) {
        if (_pending || !_current(epochAtCall) || document.hidden) return;
        const ids = _computeRows().map(s => s.space_id);
        if (!ids.length || !ids.some(id => _laneActive(_lanesById && _lanesById[id]))) return;
        const generation = _sessionGeneration;
        const request = {};
        _pending = request;
        _setRefreshing(true);
        const resp = await callTool('bank_consolidation_queues', { space_ids: ids.join(',') }).catch(() => ({ status: 'error' }));
        if (_pending === request) _pending = null;
        if (!_current(epochAtCall, generation)) return;
        _applyQueues(resp, ids);
        // Preserve the table, search input and the focused anchor. Only its
        // non-interactive children change, so live updates never steal focus.
        document.querySelectorAll('#spacesTableWrap .spaces-activity-cell').forEach(cell => {
            const link = cell.querySelector('.spaces-activity-link');
            if (link) link.innerHTML = _activityHtml(_lanesById && _lanesById[cell.dataset.space]);
        });
        _setRefreshing(false);
        _scheduleLive(epochAtCall);
    }

    registerAction('spaces-refresh', () => {
        _loadTable(AdminRouter.epoch);
    });

    // ═══════════════ Create-space form ═══════════════

    function _createSpaceFormHtml() {
        return `
            <div class="form-group">
                <label class="form-label" for="csSpaceId">Space ID <span class="req">*</span></label>
                <input type="text" id="csSpaceId" class="form-input mono" autocomplete="off" maxlength="64" aria-describedby="csSpaceIdHint csSpaceIdError">
                <p class="form-hint" id="csSpaceIdHint">Alphanumeric, hyphens and underscores, 1–64 chars. Space access is a space allowlist, not a tenant boundary.</p>
                <p class="form-error" id="csSpaceIdError" role="alert" hidden></p>
            </div>
            <div class="form-group">
                <label class="form-label" for="csDescription">Description</label>
                <input type="text" id="csDescription" class="form-input" maxlength="${MAX_DESCRIPTION}">
            </div>
            <div class="form-group">
                <label class="form-label" for="csOwner">Owner</label>
                <input type="text" id="csOwner" class="form-input" list="csOwnerList" autocomplete="off">
                <datalist id="csOwnerList"></datalist>
            </div>
            <div class="form-group">
                <label class="form-label" for="csRules">Rules (Markdown, optional — default template used if left empty)</label>
                <textarea id="csRules" class="form-input mono" rows="6" maxlength="${MAX_RULES}"></textarea>
                <p class="form-hint" id="csRulesCount">0 / ${MAX_RULES} chars</p>
            </div>
            <p class="form-error" id="csFormError" hidden></p>
        `;
    }

    async function _populateOwnerDatalist(identity, epochAtOpen) {
        if (!_isAdmin(identity)) return;
        let tokens = cache.tokens;
        if (!tokens || !tokens.length) {
            let resp;
            try {
                resp = await callTool('admin_list_tokens', { include_revoked: true });
            } catch {
                return;
            }
            if (AdminRouter.epoch !== epochAtOpen) return;
            if (!resp || resp.status !== 'ok') return;
            cache.tokens = resp.tokens || [];
            tokens = cache.tokens;
        }
        if (AdminRouter.epoch !== epochAtOpen) return;
        const list = document.getElementById('csOwnerList');
        if (!list) return;
        const names = Array.from(new Set((tokens || []).filter(t => !t.revoked && t.name).map(t => t.name)));
        list.innerHTML = names.map(n => `<option value="${esc(n)}"></option>`).join('');
    }

    function _wireCreateSpaceForm(epochAtOpen) {
        const rulesInput = document.getElementById('csRules');
        const rulesCount = document.getElementById('csRulesCount');
        if (rulesInput && rulesCount) {
            rulesInput.addEventListener('input', () => {
                rulesCount.textContent = `${rulesInput.value.length} / ${MAX_RULES} chars`;
            });
        }
        _populateOwnerDatalist(_identity, epochAtOpen);
    }

    function _lockCreateRetryForAdminRecovery() {
        const confirmButton = document.getElementById('modalConfirmBtn');
        if (!confirmButton) return;
        // showModal keeps a reference to the original button and restores it
        // in its async finally block. Replace that node so the shell cannot
        // accidentally re-enable an unsafe retry after this callback returns.
        const lockedButton = confirmButton.cloneNode(true);
        lockedButton.disabled = true;
        lockedButton.textContent = 'Admin recovery required';
        lockedButton.setAttribute('aria-disabled', 'true');
        confirmButton.replaceWith(lockedButton);
    }

    async function _submitCreateSpace() {
        const idInput = document.getElementById('csSpaceId');
        const descInput = document.getElementById('csDescription');
        const ownerInput = document.getElementById('csOwner');
        const rulesInput = document.getElementById('csRules');
        const idError = document.getElementById('csSpaceIdError');
        const formError = document.getElementById('csFormError');
        const spaceId = ((idInput && idInput.value) || '').trim();
        const description = ((descInput && descInput.value) || '').trim();
        const owner = ((ownerInput && ownerInput.value) || '').trim();
        const rules = (rulesInput && rulesInput.value) || '';

        if (idError) { idError.hidden = true; idError.textContent = ''; }
        if (formError) {
            formError.hidden = true;
            formError.textContent = '';
            formError.removeAttribute('data-recovery-required');
        }

        if (!_hasManage(_liveIdentity())) {
            if (formError) {
                formError.hidden = false;
                formError.textContent = 'Creating a space requires manage permission.';
            }
            return false;
        }

        if (!SPACE_ID_RE.test(spaceId)) {
            if (idError) {
                idError.hidden = false;
                idError.innerHTML = `${icon('alert')} Invalid space id: alphanumeric, hyphens and underscores, 1-64 chars.`;
            }
            return false;
        }
        if (description.length > MAX_DESCRIPTION) {
            if (formError) { formError.hidden = false; formError.textContent = `Description too long (max ${MAX_DESCRIPTION} chars).`; }
            return false;
        }
        if (rules.length > MAX_RULES) {
            if (formError) { formError.hidden = false; formError.textContent = `Rules too long (max ${MAX_RULES} chars).`; }
            return false;
        }

        const epochAtSubmit = AdminRouter.epoch;
        const sessionAtSubmit = _liveIdentity();
        let resp;
        try {
            resp = await callTool('space_create', { space_id: spaceId, description, owner, rules });
        } catch {
            resp = { status: 'error', message: 'Request failed' };
        }
        // §3.3.2 rule 3: if the operator navigated away while this was in
        // flight, drop the continuation silently. Return FALSE, never true —
        // the shared confirm handler (admin-app.js) calls closeModal() on any
        // truthy result, and #adminModal is a single global overlay, so a
        // stale `true` here would close whatever *different* modal the
        // operator has since opened (e.g. the health drill-down).
        if (AdminRouter.epoch !== epochAtSubmit) return false;
        if (_liveIdentity() !== sessionAtSubmit || !_hasManage(_liveIdentity())) return false;

        if (resp && resp.status === 'created') {
            AdminRouter.refresh();
            if (resp.token_message) {
                // §5.3: token_message is shown verbatim in the server-message
                // slot, not a toast. showModal's single-modal architecture
                // (§2.4.6) supports this as a multi-step flow: replace the
                // body with a message-only view (no confirm button) instead
                // of auto-closing. Returning false leaves the already-
                // replaced content in place (the original confirm button no
                // longer exists in the DOM, so its post-click cleanup is a
                // harmless no-op).
                showModal(
                    'Space created',
                    `<p class="body-small">Space <code class="mono-data">${esc(resp.space_id)}</code> created.</p>${serverMessage(resp.token_message)}`,
                );
                return false;
            }
            showToast('ok', 'Space created');
            return true;
        }
        if (resp && resp.status === 'already_exists') {
            if (idError) { idError.hidden = false; idError.innerHTML = `${icon('alert')} ${esc(resp.message || 'This space id already exists.')}`; }
            return false;
        }
        if (resp && resp.status === 'partial' && resp.recovery_required === true) {
            // Keep the form and its exact attempted values in place: a matching
            // retry may be safe, while an incompatible prefix must never be
            // auto-cleaned. Surface the server's typed recovery contract rather
            // than collapsing it to the generic message string.
            const recovery = resp.recovery || {};
            const retrySafe = String(recovery.retry_safe);
            const recoveryAction = String(recovery.action ?? '');
            const retryHelp = recovery.retry_safe === true
                ? '<strong>Identical manual retry is permitted; no automatic retry was made.</strong> '
                : '<strong>Admin recovery required. Retry is disabled in this form; follow recovery.action.</strong> ';
            const accessRecoveryBoundary = recoveryAction.includes('recover_access_grants=True')
                ? '<strong>Grant-recovery retry is MCP/CLI-only. This console never sends recover_access_grants.</strong> '
                : '';
            if (formError) {
                formError.hidden = false;
                formError.setAttribute('data-recovery-required', 'true');
                formError.innerHTML = `${icon('alert')} <strong>Recovery required.</strong> ` +
                    `${esc(resp.message || 'Space creation is incomplete.')} ` +
                    retryHelp +
                    `<strong>recovery.retry_safe:</strong> <code>${esc(retrySafe)}</code> ` +
                    `<strong>recovery.action:</strong> ${esc(recoveryAction)} ` +
                    accessRecoveryBoundary +
                    '<strong>No automatic cleanup or rollback was performed.</strong>';
            }
            if (recovery.retry_safe !== true) _lockCreateRetryForAdminRecovery();
            return false;
        }
        if (formError) { formError.hidden = false; formError.textContent = (resp && resp.message) || 'Request failed.'; }
        return false;
    }

    registerAction('spaces-open-create', () => {
        if (!_hasManage(_liveIdentity())) {
            showToast('error', 'Creating a space requires manage permission.');
            return;
        }
        const epochAtOpen = AdminRouter.epoch;
        showModal('Create space', _createSpaceFormHtml(), 'Create', () => _submitCreateSpace());
        _wireCreateSpaceForm(epochAtOpen);
    });

    function render(contentEl, params, ctx) {
        _clearLive();
        _epoch = ctx.epoch;
        _identity = ctx.identity || {};
        _sessionGeneration = ctx.sessionGeneration ?? currentSessionGeneration();
        _pending = null;
        _spacesData = null;
        _lanesById = null;
        _query = '';
        _updatedAt = null;
        _queueError = false;
        _liveStopped = false;
        _queueDenied = [];
        const createAction = _hasManage(_identity)
            ? `<button type="button" class="btn btn-primary btn-sm" data-action="spaces-open-create">${icon('plus')} Create space</button>` : '';
        contentEl.innerHTML = `<div class="page spaces-index">
            ${pageHeader('Spaces', `
                <button type="button" class="btn btn-secondary btn-sm" id="spacesRefreshBtn" data-action="spaces-refresh">${icon('refresh')} Refresh</button>
                ${createAction}
            `)}
            <div class="panel">
                <div id="spacesToolbar"></div>
                <div id="spacesTableWrap" class="spaces-table">${stateLoading('Loading spaces…')}</div>
            </div>
            <p id="spacesFreshness" class="spaces-freshness body-small"></p>
            <div id="spacesQueueWarnings"></div>
        </div>`;
        _renderToolbar();
        _loadTable(_epoch);
    }

    AdminViews.register('spaces', render);
})();
