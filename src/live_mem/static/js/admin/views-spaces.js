/**
 * Spaces index view (P8-2, issue #140).
 * Contract: DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §4.3 (parity), §5.3
 * (data matrix). One `space_list` call + one `bank_consolidation_queues`
 * call on load (no per-row N+1); the Attention filter's `bank_stale_spaces`
 * call is on-demand only, never on load. Row navigation is a real anchor
 * (§3.3.2 rule 1) — no `data-action`, no `AdminRouter.go()` for that case.
 */
(function () {
    const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
    const MAX_DESCRIPTION = 500;
    const MAX_RULES = 50000;
    const BEST_EFFORT_TOOLTIP = 'Job state lives in server memory: it does not survive a restart and history is trimmed.';

    let _epoch = -1;
    let _identity = {};
    let _spacesData = null;
    let _lanesById = null; // null = queues call failed/unavailable
    let _activeFilter = 'all'; // 'all' | 'consolidating' | 'attention'
    let _staleData = null;
    let _staleLoading = false;
    let _staleError = null;
    let _staleSeq = 0;
    let _tableSeq = 0;
    let _lastMinNotes = 5;
    let _lastMinAge = 5;

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

    // ═══════════════ Table rows ═══════════════

    function _idCellHtml(id) {
        const href = `#/spaces/${encodeURIComponent(id)}`;
        const payload = esc(JSON.stringify(id));
        return `<a href="${esc(href)}" class="mono-data spaces-id-link" title="${esc(id)}">${esc(truncateMiddle(id, 10, 6))}</a>
            <button type="button" class="copy-btn" data-action="copy-value" data-value="${payload}" aria-label="Copy ${esc(id)}">${icon('copy')}</button>`;
    }

    function _laneChip(lane) {
        // §5(d): the in_memory_best_effort guarantee is surfaced verbatim as
        // a tooltip on every job-bearing widget, explicitly including
        // "Spaces lane chips' tooltip".
        if (_lanesById === null) return `<span title="${esc(BEST_EFFORT_TOOLTIP)}">${statusDot('neutral', 'unknown')}</span>`;
        const dot = !lane ? statusDot('neutral', 'idle')
            : lane.lane_state === 'failed' ? statusDot('error', 'failed')
            : (lane.lane_state === 'running' || lane.lane_state === 'queued') ? statusDot('warn', lane.lane_state)
            : statusDot('neutral', 'idle');
        return `<span title="${esc(BEST_EFFORT_TOOLTIP)}">${dot}</span>`;
    }

    // staleById: when set (Attention filter active with data), an extra
    // "Oldest note" column renders per-space `oldest_note_age_days` from
    // bank_stale_spaces (§5.3) — the full metadata, not just membership.
    function _tableRowsHtml(rows, staleById) {
        return rows.map(space => {
            const lane = _lanesById && _lanesById[space.space_id];
            const created = fmtTimestamp(space.created_at);
            const stale = staleById && staleById[space.space_id];
            const staleCell = staleById
                ? `<td class="num" title="${stale && stale.oldest_note_timestamp ? esc(stale.oldest_note_timestamp) : ''}">${stale && typeof stale.oldest_note_age_days === 'number' ? esc(stale.oldest_note_age_days + 'd') : '—'}</td>`
                : '';
            // §5.3: when the Attention scan has its own live_notes_count for
            // this space, prefer it over the separately-fetched space_list
            // count — the scan's is_stale/age fields were derived from that
            // exact count, so showing a different (possibly older) number
            // next to them would be internally inconsistent.
            const shortCount = (stale && typeof stale.live_notes_count === 'number') ? stale.live_notes_count : space.live_notes_count;
            return `<tr>
                <td>${_idCellHtml(space.space_id)}</td>
                <td>${esc(space.description || '—')}</td>
                <td>${esc(space.owner || '—')}</td>
                <td class="mono-data" title="${esc(created.title)}">${esc(created.text || '—')}</td>
                <td class="num">${esc(String(shortCount ?? '—'))}</td>
                <td class="num">${esc(String(space.bank_files_count ?? '—'))}</td>
                <td class="num" title="Long tier state is shown in Space Detail">—</td>
                ${staleCell}
                <td>${_laneChip(lane)}</td>
            </tr>`;
        }).join('');
    }

    // ═══════════════ Filters ═══════════════

    function _computeRows() {
        const spaces = (_spacesData && _spacesData.spaces) || [];
        if (_activeFilter === 'consolidating') {
            // Degrade gracefully (§5.3): if lane data is unavailable the
            // Consolidating predicate cannot be computed — show every row
            // (usable, neutral "unknown" lane chips) rather than filtering
            // them all out. _loadTable also resets the active filter to All
            // on this transition, so this is a defensive backstop.
            if (_lanesById === null) return spaces;
            return spaces.filter(s => {
                const lane = _lanesById[s.space_id];
                return !!lane && (lane.lane_state === 'running' || (lane.queued_count || 0) > 0);
            });
        }
        if (_activeFilter === 'attention') {
            if (!_staleData) return [];
            const staleIds = new Set((_staleData.spaces || []).map(s => s.space_id));
            return spaces.filter(s => staleIds.has(s.space_id));
        }
        return spaces;
    }

    // Filter toggle group (§2.8). These are mutually-exclusive filter
    // toggles over one table, not tabs that swap panels — so they use a
    // labeled group + per-button aria-pressed (a complete, correct pattern)
    // rather than a half-implemented tab widget, which would also demand
    // roving tabindex, aria-controls, and arrow-key handling.
    function _filterBtn(filter, label, extraAttrs = '') {
        const active = _activeFilter === filter;
        return `<button type="button" class="spaces-filter-tab${active ? ' active' : ''}" aria-pressed="${active ? 'true' : 'false'}" data-action="spaces-filter-tab" data-filter="${esc(filter)}"${extraAttrs}>${esc(label)}</button>`;
    }

    function _filterTabsHtml() {
        const consolidatingAttrs = _lanesById === null ? ' disabled title="Lane data is unavailable"' : '';
        return `<div class="spaces-filter-tabs" role="group" aria-label="Filter spaces">
            ${_filterBtn('all', 'All')}
            ${_filterBtn('consolidating', 'Consolidating', consolidatingAttrs)}
            ${_filterBtn('attention', 'Attention')}
        </div>`;
    }

    function _staleControlsHtml() {
        return `<div class="spaces-stale-controls">
            <label class="form-label" for="staleMinNotes">Min notes</label>
            <input type="number" min="1" id="staleMinNotes" class="form-input mono spaces-stale-input" value="${esc(String(_lastMinNotes))}">
            <label class="form-label" for="staleMinAge">Min age (days)</label>
            <input type="number" min="0" id="staleMinAge" class="form-input mono spaces-stale-input" value="${esc(String(_lastMinAge))}">
            <button type="button" class="btn btn-secondary btn-sm" data-action="spaces-apply-stale">Apply</button>
        </div>`;
    }

    function _renderToolbar() {
        const el = document.getElementById('spacesToolbar');
        if (!el) return;
        el.innerHTML = `${_filterTabsHtml()}${_activeFilter === 'attention' ? _staleControlsHtml() : ''}`;
    }

    registerAction('spaces-filter-tab', (data) => {
        const filter = data.filter;
        if (filter === _activeFilter) return;
        if (filter === 'consolidating' && _lanesById === null) return;
        _activeFilter = filter;
        _renderToolbar();
        // §5.3: "Filter activation + manual" are the Attention refresh
        // triggers — re-scan on every transition into Attention, not only
        // the first. The _staleSeq/epoch guards in _runStaleQuery make a
        // rapid re-activation safe (only the latest scan is applied).
        if (filter === 'attention') {
            _runStaleQuery(AdminRouter.epoch, _lastMinNotes, _lastMinAge);
        } else {
            _renderBody();
        }
    });

    registerAction('spaces-apply-stale', () => {
        const notesInput = document.getElementById('staleMinNotes');
        const ageInput = document.getElementById('staleMinAge');
        _lastMinNotes = Math.max(1, parseInt((notesInput && notesInput.value) || '5', 10) || 1);
        _lastMinAge = Math.max(0, parseInt((ageInput && ageInput.value) || '5', 10) || 0);
        _runStaleQuery(AdminRouter.epoch, _lastMinNotes, _lastMinAge);
    });

    registerAction('spaces-retry-stale', () => {
        _runStaleQuery(AdminRouter.epoch, _lastMinNotes, _lastMinAge);
    });

    registerAction('spaces-refresh', () => {
        const btn = document.getElementById('spacesRefreshBtn');
        if (btn) btn.disabled = true;
        _loadTable(AdminRouter.epoch).finally(() => {
            if (btn && btn.isConnected) btn.disabled = false;
        });
    });

    // Attention filter's on-demand bank_stale_spaces call. Guarded by both
    // the router epoch (dropped if the operator navigates away — stale-
    // response proof) and a local monotonic sequence number, so re-applying
    // the filter with new thresholds before an older scan resolves can never
    // let the older, now-superseded scan overwrite the newer one.
    async function _runStaleQuery(epochAtCall, minNotes, minAgeDays) {
        const seq = ++_staleSeq;
        _staleLoading = true;
        _staleError = null;
        _renderBody();
        let resp;
        try {
            resp = await callTool('bank_stale_spaces', { min_notes: minNotes, min_age_days: minAgeDays });
        } catch {
            resp = { status: 'error', message: 'Request failed' };
        }
        if (seq !== _staleSeq || AdminRouter.epoch !== epochAtCall) return;
        _staleLoading = false;
        if (resp && resp.status === 'ok') {
            _staleData = resp;
            _staleError = null;
        } else {
            _staleData = null;
            _staleError = (resp && resp.message) || 'Request failed';
        }
        _renderBody();
    }

    function _staleById() {
        if (!_staleData) return null;
        const map = {};
        (_staleData.spaces || []).forEach(s => { map[s.space_id] = s; });
        return map;
    }

    // §5.3: the Attention widget's full response — total_stale, echoed
    // thresholds, and denied_spaces — not just space-id membership.
    function _staleSummaryHtml() {
        if (!_staleData) return '';
        const total = _staleData.total_stale ?? 0;
        const denied = _staleData.denied_spaces || [];
        const summary = `<p class="body-small spaces-meta">${esc(String(total))} stale space${total === 1 ? '' : 's'} (≥ ${esc(String(_staleData.min_notes))} notes, ≥ ${esc(String(_staleData.min_age_days))} days)</p>`;
        const deniedHtml = denied.length
            ? `<div class="spaces-stale-denied">${denied.map(d => serverMessage(`${d.space_id}: ${d.message}`)).join('')}</div>`
            : '';
        return summary + deniedHtml;
    }

    // ═══════════════ Table body render ═══════════════

    function _renderBody() {
        const wrap = document.getElementById('spacesTableWrap');
        if (!wrap) return;
        if (!_spacesData) { wrap.innerHTML = stateLoading('Loading spaces…'); return; }
        if (_spacesData.status !== 'ok') {
            wrap.innerHTML = stateError({ title: "Couldn't load spaces", message: _spacesData.message, retryAction: 'spaces-refresh' });
            return;
        }
        if (_activeFilter === 'attention' && _staleLoading) {
            wrap.innerHTML = stateLoading('Scanning for stale spaces…');
            return;
        }
        if (_activeFilter === 'attention' && _staleError) {
            wrap.innerHTML = stateError({ title: "Couldn't scan for stale spaces", message: _staleError, retryAction: 'spaces-retry-stale' });
            return;
        }
        const staleActive = _activeFilter === 'attention' && !!_staleData;
        const staleSummary = staleActive ? _staleSummaryHtml() : '';
        const staleById = staleActive ? _staleById() : null;
        const rows = _computeRows();
        if (!rows.length) {
            if (staleActive) {
                wrap.innerHTML = staleSummary + stateEmpty({ title: 'No stale banks at the current thresholds' });
                return;
            }
            if (_activeFilter === 'all') {
                const canCreate = _hasManage(_identity);
                wrap.innerHTML = stateEmpty({
                    title: 'No spaces yet',
                    hint: canCreate ? 'Create your first space to get started.' : 'A manager can create the first space.',
                    actionHtml: canCreate
                        ? '<button type="button" class="btn btn-primary btn-sm" data-action="spaces-open-create">Create space</button>'
                        : '',
                });
                return;
            }
            wrap.innerHTML = stateEmpty({ title: 'No spaces match this filter' });
            return;
        }
        const headers = ['Space', 'Description', 'Owner', 'Created', 'Short', 'Mid', 'Long']
            .concat(staleActive ? ['Oldest note'] : [])
            .concat(['Lane']);
        wrap.innerHTML = staleSummary + dataTable(headers, _tableRowsHtml(rows, staleById));
    }

    // ═══════════════ Route-entry load ═══════════════

    // Sequence-guarded like _runStaleQuery: a route-entry load racing a
    // fast manual-refresh click (or two refresh clicks) can resolve out of
    // order — only the most recently issued call is ever applied.
    async function _loadTable(epochAtCall) {
        const seq = ++_tableSeq;
        const [spacesResp, queuesResp] = await Promise.all([
            callTool('space_list', {}).catch(() => ({ status: 'error', message: 'Request failed' })),
            callTool('bank_consolidation_queues', { space_ids: '' }).catch(() => ({ status: 'error' })),
        ]);
        if (seq !== _tableSeq || AdminRouter.epoch !== epochAtCall) return;
        _spacesData = spacesResp;
        if (spacesResp && spacesResp.status === 'ok') cache.spaces = spacesResp.spaces || [];
        if (queuesResp && queuesResp.status === 'ok') {
            _lanesById = {};
            (queuesResp.lanes || []).forEach(lane => { _lanesById[lane.space_id] = lane; });
        } else {
            _lanesById = null;
            // §5.3: the Consolidating filter cannot be computed without lane
            // data. If the operator was already on it when the queues call
            // failed, fall back to All so the table stays usable (the tab is
            // also disabled by _filterTabsHtml while lanes are null).
            if (_activeFilter === 'consolidating') {
                _activeFilter = 'all';
                showToast('warn', 'Lane data unavailable — showing all spaces');
            }
        }
        _renderToolbar();
        _renderBody();
    }

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
        _epoch = ctx.epoch;
        _identity = ctx.identity || {};
        _spacesData = null;
        _lanesById = null;
        _activeFilter = 'all';
        _staleData = null;
        _staleLoading = false;
        _staleError = null;

        const createAction = _hasManage(_identity)
            ? `<button type="button" class="btn btn-primary btn-sm" data-action="spaces-open-create">${icon('plus')} Create space</button>`
            : '';

        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Spaces', `
                <button type="button" class="btn btn-secondary btn-sm" id="spacesRefreshBtn" data-action="spaces-refresh">${icon('refresh')} Refresh</button>
                ${createAction}
            `)}
            <div class="panel">
                <div id="spacesToolbar"></div>
                <div id="spacesTableWrap">${stateLoading('Loading spaces…')}</div>
            </div>
        </div>`;

        _renderToolbar();
        _loadTable(_epoch);
    }

    AdminViews.register('spaces', render);
})();
