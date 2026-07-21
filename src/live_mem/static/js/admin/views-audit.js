/**
 * Audit view (P8-6, issue #144).
 *
 * The server payload is retained only in a WeakMap entry keyed by the current
 * .audit-view root. Navigating away therefore leaves no module-level strong
 * reference to audit data. Refresh is deliberately manual: one request on
 * initial admin render, then one request for each explicit refresh action.
 */
(function () {
    const viewState = new WeakMap();
    const sentinelStatuses = Object.freeze(['read_only', 'rate_limited', 'truncated']);

    function _scopeBanner() {
        return `<section class="audit-scope-banner" aria-labelledby="auditScopeTitle">
            <span class="audit-scope-icon" aria-hidden="true">${icon('shield')}</span>
            <div>
                <span class="micro-label" id="auditScopeTitle">In-memory audit scope</span>
                <p>This instance, since restart — console and auth events only, best-effort.</p>
                <p>MCP (<code>/mcp</code>) tool calls are not individually audited here.</p>
                <p>Argument values, including space identifiers, are deliberately not stored; labels may be clipped or redacted.</p>
            </div>
        </section>`;
    }

    function _refreshButton() {
        return `<button type="button" class="btn btn-secondary" data-audit-action="refresh">
            ${icon('refresh')}<span>Refresh</span>
        </button>`;
    }

    function _filters() {
        return `<div class="audit-filters" aria-label="Audit event filters">
            <div class="audit-filter-field">
                <label class="form-label" for="auditEventFilter">Event type</label>
                <select class="form-input" id="auditEventFilter" data-audit-filter="event">
                    <option value="">All event types</option>
                    <option value="admin_tool_call">admin_tool_call</option>
                    <option value="login_success">login_success</option>
                    <option value="login_failed">login_failed</option>
                    <option value="auth_rejected">auth_rejected</option>
                </select>
            </div>
            <div class="audit-filter-field">
                <label class="form-label" for="auditToolFilter">Requested tool</label>
                <input class="form-input mono" id="auditToolFilter" data-audit-filter="tool" placeholder="Literal tool text" autocomplete="off">
            </div>
            <div class="audit-filter-field">
                <label class="form-label" for="auditClientFilter">Client</label>
                <input class="form-input mono" id="auditClientFilter" data-audit-filter="client" placeholder="Literal client text" autocomplete="off">
            </div>
        </div>`;
    }

    function _identityKind(identity) {
        if (!identity || typeof identity !== 'object' || !identity.client_name) {
            return 'missing';
        }
        const permissions = Array.isArray(identity.permissions) ? identity.permissions : [];
        return permissions.includes('admin') ? 'admin' : 'non-admin';
    }

    function _resultSlot(root) {
        return root.querySelector('[data-audit-results]');
    }

    function _refreshControl(root) {
        return root.querySelector('[data-audit-action="refresh"]');
    }

    function _setResult(root, html) {
        const slot = _resultSlot(root);
        if (slot) slot.innerHTML = html;
    }

    function _setLoading(root, loading) {
        const button = _refreshControl(root);
        if (button) button.disabled = loading;
        if (loading) _setResult(root, stateLoading('Loading audit events…'));
    }

    function _literalIncludes(value, query) {
        if (!query) return true;
        return String(value ?? '').toLowerCase().includes(String(query).toLowerCase());
    }

    function _filteredEntries(state) {
        return state.entries.filter(entry => {
            const eventMatches = !state.filters.event || entry.event === state.filters.event;
            const toolMatches = _literalIncludes(entry.tool, state.filters.tool);
            const clientMatches = _literalIncludes(entry.client, state.filters.client);
            return eventMatches && toolMatches && clientMatches;
        });
    }

    function _eventPill(event) {
        const value = String(event ?? '');
        if (value === 'login_success') return pill('ok', value);
        if (value === 'login_failed' || value === 'auth_rejected') return pill('error', value);
        if (value === 'admin_tool_call') return pill('neutral', value);
        return pill('warn', value || 'unknown');
    }

    function _nullableMono(value, className) {
        if (value === null || value === undefined || value === '') {
            return '<span class="audit-null" aria-label="Not recorded">—</span>';
        }
        return `<span class="${esc(className)} mono">${esc(String(value))}</span>`;
    }

    function _isOverflowMarker(value) {
        if (!value.startsWith('+') || !value.endsWith(' more')) return false;
        const countText = value.slice(1, -5);
        if (!countText || countText.trim() !== countText) return false;
        const count = Number(countText);
        return Number.isInteger(count) && count > 0 && String(count) === countText;
    }

    function _argumentKeyChips(argumentKeys) {
        if (argumentKeys === null || argumentKeys === undefined) {
            return '<span class="audit-null" aria-label="Not recorded">—</span>';
        }
        if (!Array.isArray(argumentKeys) || argumentKeys.length === 0) {
            return '<span class="audit-null">No keys</span>';
        }
        return `<div class="audit-key-list">${argumentKeys.map((key, index) => {
            const value = String(key);
            const isOverflow = index === argumentKeys.length - 1 && _isOverflowMarker(value);
            const className = isOverflow ? 'audit-key-chip audit-key-chip--overflow' : 'audit-key-chip';
            return `<span class="${esc(className)}">${esc(value)}</span>`;
        }).join('')}</div>`;
    }

    function _entryRow(entry) {
        return `<tr>
            <td class="audit-time">${renderTimestamp(entry.ts)}</td>
            <td>${_eventPill(entry.event)}</td>
            <td>${_nullableMono(entry.tool, 'audit-tool')}</td>
            <td>${_argumentKeyChips(entry.arguments_keys)}</td>
            <td>${_nullableMono(entry.client, 'audit-client')}</td>
            <td>${_nullableMono(entry.auth_type, 'audit-auth-type')}</td>
        </tr>`;
    }

    function _renderEntries(root, state) {
        const returned = esc(String(state.total));
        const capacity = esc(String(state.capacity));
        const fetched = esc(String(state.entries.length));
        const scopeNote = esc(String(state.scopeNote));
        const meta = visible => `<div class="audit-result-meta">
            <span><strong>${esc(String(visible))}</strong> visible of ${fetched} fetched</span>
            <span>Server returned ${returned}; ring capacity ${capacity}</span>
        </div>
        <div class="audit-server-scope">
            <span class="micro-label">Server scope note</span>
            <p>${scopeNote}</p>
        </div>`;

        if (state.entries.length === 0) {
            _setResult(root, `${meta(0)}${stateEmpty({
                title: 'No events recorded since last restart',
                hint: 'A manual refresh reads this instance again.',
            })}`);
            return;
        }

        const entries = _filteredEntries(state);
        if (entries.length === 0) {
            _setResult(root, `${meta(0)}${stateEmpty({
                title: 'No events match these filters',
                hint: 'Change the event, requested tool, or client filter.',
            })}`);
            return;
        }

        const rows = entries.map(_entryRow).join('');
        const table = dataTable(
            ['Time', 'Event', 'Requested tool', 'Argument keys', 'Client', 'Auth type'],
            rows,
        );

        _setResult(root, `${meta(entries.length)}${table}`);
    }

    function _renderResponseError(root, response) {
        const message = response && response.message
            ? String(response.message)
            : 'The audit tool returned an unexpected response.';
        _setResult(root, stateError({
            title: "Couldn't load audit events",
            message,
        }));
    }

    function _clearPayload(state) {
        state.entries = [];
        state.total = '—';
        state.capacity = '—';
        state.scopeNote = '';
    }

    function _finishFailedLoad(root, state) {
        _clearPayload(state);
        state.phase = 'error';
        _setLoading(root, false);
    }

    async function _load(root) {
        const state = viewState.get(root);
        if (!state || state.phase === 'loading') return;
        state.phase = 'loading';
        _setLoading(root, true);

        try {
            const response = await callTool('admin_audit_recent', { limit: 500 });
            if (!root.isConnected || state.epoch !== AdminRouter.epoch) return;

            if (response && sentinelStatuses.includes(response.status)) {
                _finishFailedLoad(root, state);
                _setResult(root, stateUnavailable(String(response.message || 'Audit data is unavailable.')));
                return;
            }
            if (!response || response.status !== 'ok') {
                _finishFailedLoad(root, state);
                _renderResponseError(root, response);
                return;
            }
            if (!Array.isArray(response.entries)) {
                _finishFailedLoad(root, state);
                _setResult(root, stateError({
                    title: "Couldn't load audit events",
                    message: 'The audit response did not include an entries list.',
                }));
                return;
            }

            state.entries = response.entries;
            state.total = response.total === null || response.total === undefined
                ? '—'
                : String(response.total);
            state.capacity = response.capacity === null || response.capacity === undefined
                ? '—'
                : String(response.capacity);
            state.scopeNote = response.scope_note === null || response.scope_note === undefined
                ? ''
                : String(response.scope_note);
            state.phase = 'loaded';
            _setLoading(root, false);
            _renderEntries(root, state);
        } catch (error) {
            if (!root.isConnected || state.epoch !== AdminRouter.epoch) return;
            _finishFailedLoad(root, state);
            _setResult(root, stateError({
                title: "Couldn't load audit events",
                message: error && error.message ? String(error.message) : 'The audit request failed.',
            }));
        }
    }

    function _wire(root) {
        root.addEventListener('click', event => {
            const button = event.target.closest('[data-audit-action="refresh"]');
            if (!button || !root.contains(button)) return;
            event.preventDefault();
            _load(root);
        });

        root.addEventListener('input', event => {
            const input = event.target.closest('[data-audit-filter]');
            if (!input || !root.contains(input)) return;
            const state = viewState.get(root);
            if (!state) return;
            const filter = input.dataset.auditFilter;
            if (filter !== 'event' && filter !== 'tool' && filter !== 'client') return;
            state.filters[filter] = input.value;
            if (state.phase !== 'loaded') return;
            _renderEntries(root, state);
        });
    }

    function render(contentEl, _params, ctx) {
        const identityKind = _identityKind(ctx && ctx.identity);
        const adminControls = identityKind === 'admin'
            ? `${pageHeader('Audit', _refreshButton())}${panel(`${_filters()}<div class="audit-results" data-audit-results aria-live="polite">${stateLoading('Loading audit events…')}</div>`)}`
            : `${pageHeader('Audit')}${panel(`<div class="audit-results" data-audit-results></div>`)}`;

        contentEl.innerHTML = `<div class="page audit-view">
            ${_scopeBanner()}
            ${adminControls}
        </div>`;

        const root = contentEl.querySelector('.audit-view');
        if (!root) return;
        const state = {
            epoch: ctx ? ctx.epoch : AdminRouter.epoch,
            phase: 'idle',
            entries: [],
            total: '—',
            capacity: '—',
            scopeNote: '',
            filters: { event: '', tool: '', client: '' },
        };
        viewState.set(root, state);

        if (identityKind === 'missing') {
            _setResult(root, stateUnavailable('Identity unavailable.'));
            return;
        }
        if (identityKind === 'non-admin') {
            _setResult(root, stateUnavailable('Requires admin permission'));
            return;
        }

        _wire(root);
        _load(root);
    }

    AdminViews.register('audit', render);
})();
