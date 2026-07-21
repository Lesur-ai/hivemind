/**
 * Mesh view (P10-4, issue #192) — routes #/mesh and #/mesh/<space-id>.
 *
 * Contract: DESIGN/hivemind/P10_DESIGN_PACK.md §3-§8, DESIGN/hivemind/PROJECT_MESH.md,
 * DESIGN/hivemind/P10_THREAT_MODEL.md (T5, T15), docs/adr/0024. Consumes ONLY the
 * `/api/admin/mesh/*` admin control plane (a distinct REST surface from `/api/tool`
 * — mesh_admin.py's own docstring: "never an MCP mesh_* tool"), never a `mesh_*`
 * MCP tool.
 *
 * Reconciliation note (documented deviation from the design pack's §6 aspirational
 * data-source matrix, since the shipped P10-3 backend has a narrower/different
 * shape — see CHANGELOG.md): `capabilities{}` is not a server field (reaching this
 * view at all already requires admin+enabled, so every mutating action is
 * inherently available — rendered as a plain statement, never a fabricated
 * per-action toggle); `eligible_spaces[]` is not precomputed — the space picker
 * lists local spaces and `create_invitation()`'s own fail-closed validation is the
 * single source of truth for eligibility, surfaced verbatim on refusal;
 * `pending_actions[]` is derived client-side from the real `pairings[]` state via
 * the state→action matrix below, never fabricated; the signed policy export
 * (`GET /spaces/<id>/policy-export`) has zero backend implementation anywhere and
 * ships as an explanatory absence, never a disabled button that would fire a
 * request (T15).
 *
 * The normal successful pairing is three actions across two administrators
 * (create → paste "invitation code" + accept → approve); bootstrap transfer and
 * the final acknowledgement are internal continuations of the accept step, driven
 * by the "Complete enrollment" affordance shown only in the CLAIMED/AWAITING_ACKS
 * target states and re-entrant on failure (never a hidden retry the operator
 * cannot see or drive).
 *
 * The invitation code bundles the four separate values create_invitation()
 * returns (secret, source_endpoint, invitation bytes, source_fingerprint is
 * display-only) into ONE copyable opaque string so accept stays a single paste —
 * no form ever asks the target administrator to type/edit an endpoint. It carries
 * the one-time secret and follows the exact same display-once/teardown discipline
 * as views-access.js's token secret (closure-only storage, zeroed on every exit
 * path, navigation-locked while displayed) — see _navLock* / _copySecret below.
 *
 * Escaping (mirrors admin-app.js's shell contract): every dynamic value passes
 * through esc() at its interpolation site; server messages render only via
 * serverMessage(), verbatim, never parsed as HTML.
 */
(function () {
    'use strict';

    const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
    const DETAIL_TABS = ['overview', 'members', 'invitations'];

    // ─────────────── modal generation & staleness (mirrors views-access.js) ───────────────
    let _modalGen = 0;

    function _openModal(title, body, verb, onConfirm) {
        _modalGen += 1;
        showModal(title, body, verb, onConfirm);
    }

    function _openDestructive(opts) {
        _modalGen += 1;
        showDestructiveModal(opts);
    }

    function _sessionIdentity() {
        return (typeof _ctx === 'function') ? _ctx().identity : null;
    }

    function _sessionEnded(sessionAtCall) {
        const overlay = document.getElementById('loginOverlay');
        if (overlay && !overlay.classList.contains('hidden')) return true;
        if (sessionAtCall === undefined) return false;
        return typeof _ctx === 'function' && _ctx().identity !== sessionAtCall;
    }

    function _isStale(epochAtCall, genAtCall, sessionAtCall) {
        return AdminRouter.epoch !== epochAtCall
            || _modalGen !== genAtCall
            || _sessionEnded(sessionAtCall);
    }

    // Navigation lock for the one-time invitation-code display (mirrors
    // views-access.js's one-time-secret nav lock): while the code is on screen,
    // Back/Forward and hash edits are reverted to this route so the secret is
    // never left rendered over a route the operator navigated to. Self-heals on
    // a session wipe via the captured session identity.
    let _navLock = null;

    function _navLockAcquire() {
        const lock = { hash: location.hash, session: _sessionIdentity() };
        _navLock = lock;
        return lock;
    }

    function _navLockRelease(lock) {
        if (_navLock === lock) _navLock = null;
    }

    function _navLockSessionCurrent(lock) {
        const overlay = document.getElementById('loginOverlay');
        if (overlay && !overlay.classList.contains('hidden')) return false;
        return _sessionIdentity() === lock.session;
    }

    window.addEventListener('hashchange', () => {
        if (!_navLock || location.hash === _navLock.hash) return;
        if (!_navLockSessionCurrent(_navLock)) {
            _navLock = null;
            return;
        }
        location.hash = _navLock.hash;
    });

    function _copySecret(holder, epochAtCopy, genAtCopy, sessionAtCopy, copiedLabel) {
        function stale() {
            return !holder.value || _isStale(epochAtCopy, genAtCopy, sessionAtCopy);
        }
        function finish(ok) {
            if (stale()) return;
            const successLabel = copiedLabel || 'Copied';
            if (ok) { showToast('ok', successLabel); return; }
            const ta = document.createElement('textarea');
            ta.value = holder.value;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            let copied = false;
            try { copied = document.execCommand('copy'); } catch { copied = false; }
            document.body.removeChild(ta);
            showToast(copied ? 'ok' : 'warn',
                copied ? successLabel : 'Copy failed — select the value and copy manually');
        }
        const value = holder.value;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(() => finish(true)).catch(() => finish(false));
        } else {
            finish(false);
        }
    }

    // ─────────────────────────── permission helpers ───────────────────────────

    function isAdmin(identity) {
        return !!(identity && Array.isArray(identity.permissions) && identity.permissions.includes('admin'));
    }

    // ─────────────────────── state → action matrix (PLAN §4) ───────────────────────

    const SOURCE_ACTIONS = {
        issued: ['cancel'],
        claimed: ['approve', 'cancel'],
        approved: ['cancel', 'evict'],
        transferring: ['evict'],
        awaiting_acks: ['resume', 'evict'],
        active: [],
        expired: [], cancelled: [], refused: [],
    };
    const TARGET_ACTIONS = {
        claimed: ['enroll', 'abandon'],
        transferring: ['resync', 'abandon'],
        awaiting_acks: ['enroll', 'abandon'],
        blocked_recovery: ['resync', 'abandon'],
        active: [],
        expired: [], cancelled: [], refused: [],
    };

    // The middleware's process-lock check is unconditional and precedes even
    // GET /status (mesh_admin.py __call__), so healthy:false in a successful
    // response can only happen if the lock was lost between that entry check
    // and _status()'s own read of it — a narrow but real race. Treat it
    // exactly like an unreachable Mesh: mutations would 503, so no control
    // that would fire one may render (§3, T15).
    function meshAvailable(status) {
        return !!(status && status.healthy === true);
    }

    function actionsFor(pairing, available) {
        if (!available) return [];
        if (pairing.role === 'source') {
            if (pairing.state === 'blocked_recovery') {
                if (pairing.next_action === 'resume') return ['resume'];
                if (pairing.next_action === 'evict') return ['evict'];
                // Recorded evidence is unavailable/unverifiable — offer both
                // recovery actions rather than guessing (never fabricate which
                // one is correct; the backend re-verifies before acting either way).
                return ['resume', 'evict'];
            }
            return SOURCE_ACTIONS[pairing.state] || [];
        }
        return TARGET_ACTIONS[pairing.state] || [];
    }

    const ACTION_META = {
        approve: {
            label: 'Approve', tier: 'plain', verb: 'Approve',
            body: () => '<p class="body-small">Approves the pending pairing claim, admits the target as a pending member, and starts the automated bootstrap transfer.</p>',
        },
        cancel: {
            label: 'Cancel', tier: 'plain', verb: 'Cancel invitation',
            body: () => '<p class="body-small">Cancels this pairing before it mutated shared membership. No member is added or removed.</p>',
        },
        resume: {
            label: 'Resume', tier: 'plain', verb: 'Resume',
            body: () => '<p class="body-small">Idempotently re-delivers the pending membership activation to the target. Safe to retry.</p>',
        },
        enroll: {
            label: 'Complete enrollment', tier: 'plain', verb: 'Continue',
            body: () => '<p class="body-small">Fetches the signed approval and bootstrap snapshot from the source, imports it, and sends the final acknowledgement. If the other administrator has not approved yet, this reports that clearly — retry once they have.</p>',
        },
        evict: {
            label: 'Evict', tier: 'destructive', verb: 'Evict', reason: true,
            typed: p => p.space_id,
            body: p => `<p class="body-small">Removes the admitted candidate for space <strong>${esc(p.space_id)}</strong> from shared membership and releases the reservation. Epoch-advancing and audited.</p>`,
        },
        resync: {
            label: 'Resync', tier: 'destructive', verb: 'Resync',
            typed: p => p.space_id,
            body: p => `<p class="body-small">Tears this target's copy of <strong>${esc(p.space_id)}</strong> back to blank and re-imports a fresh signed snapshot from the source. Use only after a failed or corrupted import.</p>`,
        },
        abandon: {
            label: 'Abandon', tier: 'destructive', verb: 'Abandon',
            typed: p => p.space_id,
            body: () => '<p class="body-small">Gives up this pairing after verifying the other administrator already gave up too, and tears this target back to blank so it can be reused. Refused while the pairing could still converge.</p>',
        },
        'force-evict-member': {
            label: 'Force-evict member', tier: 'destructive', verb: 'Force-evict', reason: true,
            typed: p => p.space_id,
            body: p => `<p class="body-small"><strong>Danger — only if the peer is confirmed dead.</strong> Force-removes the active member for <strong>${esc(p.space_id)}</strong> through ordinary epoch-advancing membership eviction. Removing a node that is actually alive causes a split-brain.</p>`,
        },
    };

    function actionButton(pairing, actionKey) {
        const meta = ACTION_META[actionKey];
        if (!meta) return '';
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-run-action" data-pair-id="${esc(pairing.pair_id)}" data-mesh-action="${esc(actionKey)}">${esc(meta.label)}</button>`;
    }

    function confirmMeshAction(pairId, actionKey) {
        const pairing = findPairing(pairId);
        if (!pairing) { showToast('error', 'Pairing not found — refresh and try again.'); return; }
        const meta = ACTION_META[actionKey];
        if (!meta) return;
        let bodyHtml = meta.body(pairing);
        if (meta.reason) {
            bodyHtml += '<div class="form-group"><label class="form-label" for="meshReasonInput">Reason (for the audit trail, optional)</label>' +
                '<textarea class="form-input" id="meshReasonInput" rows="2"></textarea></div>';
        }

        async function onConfirm() {
            // Click-time re-check (defense in depth beyond the render-time
            // gate): the modal can stay open across a background Refresh
            // that flips Mesh unhealthy/unavailable while the operator was
            // reading the confirmation — never fire a mutation the process-
            // lock middleware is guaranteed to 503 (§3, T15).
            if (!meshAvailableNow()) {
                showModal('Action refused', panel(stateUnavailable('Mesh became unavailable while this dialog was open — refresh and try again.')));
                return false;
            }
            const epochAtCall = AdminRouter.epoch, genAtCall = _modalGen, sessionAtCall = _sessionIdentity();
            const args = { pair_id: pairing.pair_id };
            if (meta.reason) {
                const identity = _sessionIdentity();
                args.operator = (identity && identity.client_name) || 'admin';
                const reasonEl = document.getElementById('meshReasonInput');
                args.reason = reasonEl ? reasonEl.value.trim() : '';
            }
            let res;
            try { res = await meshAdminAction(actionKey, args); }
            catch { res = { status: 'error', message: 'Request failed' }; }
            if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;
            if (res && res.status === 'ok') {
                showToast('ok', meta.label + ' succeeded');
                AdminRouter.refresh();
                return true;
            }
            showModal('Action failed', panel(serverMessage(res && res.message) || stateError({ title: 'The server refused or failed this operation.' })));
            return false;
        }

        if (meta.tier === 'destructive') {
            _openDestructive({ title: meta.label, bodyHtml, verb: meta.verb, typedConfirmation: meta.typed(pairing), onConfirm });
        } else {
            _openModal(meta.label, bodyHtml, meta.verb, onConfirm);
        }
    }

    // ─────────────────────────── rendering helpers ───────────────────────────

    function statePill(pairingState) {
        const kind = pairingState === 'active' ? 'ok'
            : (pairingState === 'blocked_recovery' || pairingState === 'refused') ? 'error'
            : (pairingState === 'expired' || pairingState === 'cancelled') ? 'neutral'
            : 'warn';
        return pill(kind, String(pairingState || '').replace(/_/g, ' '));
    }

    function renderTimestampMs(ms) {
        if (typeof ms !== 'number') return '<span class="text-faint">—</span>';
        return renderTimestamp(new Date(ms).toISOString());
    }

    function renderDiagnostics(pairing) {
        const rows = [
            ['pair_id', pairing.pair_id],
            ['base epoch', pairing.base_epoch],
            ['source fingerprint', pairing.source_fingerprint],
            ['source endpoint', pairing.source_endpoint],
            ['target fingerprint', pairing.target_fingerprint],
            ['target endpoint', pairing.target_endpoint],
            ['granted scopes', (pairing.granted_scopes || []).join(', ')],
            ['invitation digest', pairing.invitation_digest],
            ['claim digest', pairing.claim_digest],
            ['approval digest', pairing.approval_digest],
            ['bootstrap manifest digest', pairing.bootstrap_manifest_digest],
            ['activation event id', pairing.activation_event_id],
            ['last error', pairing.last_error],
        ];
        if (pairing.state === 'blocked_recovery') {
            rows.push(['recorded next action', pairing.next_action || 'unavailable']);
            rows.push(['blocked phase', pairing.phase || 'unavailable']);
        }
        const body = rows.map(([label, value]) =>
            `<div class="sd-kv"><span class="micro-label">${esc(label)}</span>${value ? `<span class="mono-data">${esc(String(value))}</span>` : '<span class="text-faint">—</span>'}</div>`
        ).join('');
        return `<details class="sd-rules mesh-diagnostics"><summary>Diagnostics</summary><div class="sd-meta-row">${body}</div></details>`;
    }

    function renderPairingRow(pairing, opts) {
        opts = opts || {};
        const actionsHtml = actionsFor(pairing, !!opts.available).map(a => actionButton(pairing, a)).join(' ') || '<span class="text-faint">—</span>';
        const spaceCell = opts.hideSpace ? '' : `<td><a class="sd-link" href="#/mesh/${encodeURIComponent(pairing.space_id)}">${esc(pairing.space_id)}</a></td>`;
        return `<tr>
            ${spaceCell}
            <td>${esc(pairing.role)}</td>
            <td>${statePill(pairing.state)}</td>
            <td>${renderTimestampMs(pairing.updated_at_ms)}</td>
            <td class="mesh-row-actions">${actionsHtml}</td>
        </tr>
        <tr class="mesh-diag-row"><td colspan="${opts.hideSpace ? 4 : 5}">${renderDiagnostics(pairing)}</td></tr>`;
    }

    // ─────────────────────────── module state ───────────────────────────

    const state = {
        identity: {},
        sessionGeneration: null,
        status: null,
        statusEpoch: null, // AdminRouter.epoch as of the last loadStatus() write — see meshAvailableNow()
        members: {}, // spaceId -> last meshAdminMembers() payload, or {error}
        detailTab: 'overview',
    };

    function findPairing(pairId) {
        const s = state.status;
        if (!s || !Array.isArray(s.pairings)) return null;
        return s.pairings.find(p => p.pair_id === pairId) || null;
    }

    async function loadStatus(epoch) {
        let res;
        try { res = await meshAdminStatus(); } catch { res = null; }
        if (epoch !== AdminRouter.epoch) return;
        state.status = res || null;
        state.statusEpoch = epoch;
    }

    // Click-time-only re-check (used by the three mutation-modal onConfirm
    // handlers, never at render time — a render is always epoch-synchronized
    // already by construction). state.status survives an ordinary route
    // change; only loadStatus() overwrites it, asynchronously. A modal held
    // open across a navigation (none of the three mutation modals hold a
    // nav lock) can therefore reach confirm while AdminRouter.epoch has
    // already advanced past state.statusEpoch — the in-flight reload has not
    // resolved yet, so state.status is a stale answer to a question about a
    // route that no longer exists. Fail closed: treat "unknown/pending" the
    // same as "unavailable", never assume a stale healthy reading still holds.
    function meshAvailableNow() {
        return state.statusEpoch === AdminRouter.epoch && meshAvailable(state.status);
    }

    async function loadMembers(spaceId, epoch) {
        let res;
        try { res = await meshAdminMembers(spaceId); } catch { res = null; }
        if (epoch !== AdminRouter.epoch) return;
        // Positive match on 'ok' (not a negative match on 'error'): a
        // 'truncated' response (§5.0) has neither members[] nor
        // membership_epoch and must render as an honest error, never a
        // fabricated "no members" empty state.
        state.members[spaceId] = (res && res.status === 'ok') ? res : { error: (res && res.message) || 'Request failed' };
    }

    function ensureSpaces() {
        return callTool('space_list', {}).then(resp => {
            if (resp && resp.status === 'ok') cache.spaces = resp.spaces || [];
            return resp;
        }).catch(() => ({ status: 'error' }));
    }

    // ─────────────────────────── overview (#/mesh) ───────────────────────────

    function renderOverviewShell(contentEl, epoch) {
        if (!isAdmin(state.identity)) {
            contentEl.innerHTML = `<div class="page">${pageHeader('Project Mesh')}${panel(stateUnavailable('Requires admin permission.'))}</div>`;
            return;
        }
        contentEl.innerHTML = `<div class="page">${pageHeader('Project Mesh')}${panel(stateLoading('Loading Mesh status…'))}</div>`;
        loadStatus(epoch).then(() => { if (epoch === AdminRouter.epoch) paintOverview(contentEl, epoch); });
    }

    // Create/Accept are omitted (not disabled) unless Mesh is confirmed
    // available — a control that would just POST into a 404/unreachable
    // control plane is exactly the "action expected to fail when clicked"
    // the design pack forbids (§3, T15). Refresh is always safe (GET-only,
    // lets the operator retry).
    function overviewActions(available) {
        const refresh = `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-refresh">${icon('refresh')}<span>Refresh</span></button>`;
        if (!available) return refresh;
        return refresh +
            `<button type="button" class="btn btn-primary btn-sm" data-action="mesh-create-invitation">${icon('plus')}<span>Create invitation</span></button>
            <button type="button" class="btn btn-secondary btn-sm" data-action="mesh-accept-invitation">Accept invitation</button>`;
    }

    function instanceCard(s, available) {
        const note = available
            ? 'Signed in as an admin session — every Mesh action below is available.'
            : 'This instance reports unhealthy — mutating Mesh actions are unavailable until it recovers.';
        return `<div class="panel-header"><h2>This instance</h2>${statusDot(s.healthy ? 'ok' : 'error', s.healthy ? 'healthy' : 'unhealthy')}</div>
            <div class="sd-meta-row">
                <div class="sd-kv"><span class="micro-label">DISPLAY NAME</span><span class="mono-data">${esc(s.display_name || '—')}</span></div>
                <div class="sd-kv"><span class="micro-label">FINGERPRINT</span>${copyable(s.fingerprint || '')}</div>
                <div class="sd-kv"><span class="micro-label">PUBLIC URL</span><span class="mono-data">${esc(s.public_url || '—')}</span></div>
            </div>
            <p class="body-small">${esc(note)}</p>`;
    }

    function attentionSection(attention, available) {
        const header = '<div class="panel-header"><h2>Needs your attention</h2></div>';
        if (!attention.length) {
            return header + stateEmpty({ title: 'Nothing needs attention', hint: 'Every pairing on this instance is settled or has no pending operator action.' });
        }
        return header + dataTable(['Space', 'Role', 'State', 'Updated', 'Action'], attention.map(p => renderPairingRow(p, { available })).join(''));
    }

    function pairingsSection(pairings, available) {
        const header = '<div class="panel-header"><h2>All pairings</h2></div>';
        if (!pairings.length) {
            return header + stateEmpty({ title: 'No pairings yet', hint: 'Create an invitation to pair a space with another Hivemind instance.' });
        }
        return header + dataTable(['Space', 'Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { available })).join(''));
    }

    function paintOverview(contentEl, epoch) {
        if (epoch !== AdminRouter.epoch) return;
        if (!state.status) {
            contentEl.innerHTML = `<div class="page">${pageHeader('Project Mesh', overviewActions(false))}${panel(stateUnavailable('Mesh is not available on this instance or for this session.'))}</div>`;
            return;
        }
        const s = state.status;
        const available = meshAvailable(s);
        const pairings = s.pairings || [];
        const attention = pairings.filter(p => actionsFor(p, available).length > 0);
        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Project Mesh', overviewActions(available))}
            ${panel(instanceCard(s, available))}
            ${panel(attentionSection(attention, available))}
            ${panel(pairingsSection(pairings, available))}
        </div>`;
    }

    // ───────────────────── space detail (#/mesh/<space-id>) ─────────────────────

    function renderDetail(contentEl, rawSpaceId, epoch) {
        if (!isAdmin(state.identity)) {
            contentEl.innerHTML = `<div class="page">${pageHeader('Mesh space')}${panel(stateUnavailable('Requires admin permission.'))}</div>`;
            return;
        }
        if (!SPACE_ID_RE.test(rawSpaceId)) {
            contentEl.innerHTML = `<div class="page">${pageHeader('Mesh space')}${panel(stateError({ title: 'Invalid space id' }))}</div>`;
            return;
        }
        contentEl.innerHTML = `<div class="page">${pageHeader(rawSpaceId)}${panel(stateLoading('Loading Mesh status…'))}</div>`;
        // Sequenced, not parallel: if Mesh turns out to be unavailable,
        // members/<space_id> is guaranteed to fail the same way — firing it
        // anyway would be a wasted request against a control plane already
        // known to be unreachable.
        loadStatus(epoch).then(() => {
            if (epoch !== AdminRouter.epoch) return;
            if (!meshAvailable(state.status)) {
                paintDetail(contentEl, rawSpaceId, epoch);
                return;
            }
            loadMembers(rawSpaceId, epoch).then(() => {
                if (epoch === AdminRouter.epoch) paintDetail(contentEl, rawSpaceId, epoch);
            });
        });
    }

    function detailOverviewPanel(spaceId, pairings, members, available) {
        const epochKnown = members && !members.error && members.membership_epoch != null;
        const memberCount = members && !members.error ? (members.members || []).length : null;
        return `<div class="panel-header"><h2>Overview</h2></div>
            <div class="sd-meta-row">
                <div class="sd-kv"><span class="micro-label">MEMBERSHIP EPOCH</span><span class="mono-data">${epochKnown ? esc(String(members.membership_epoch)) : '—'}</span></div>
                <div class="sd-kv"><span class="micro-label">ACTIVE MEMBERS</span><span class="mono-data">${memberCount === null ? '—' : esc(String(memberCount))}</span></div>
                <div class="sd-kv"><span class="micro-label">PAIRING SESSIONS</span><span class="mono-data">${esc(String(pairings.length))}</span></div>
            </div>
            ${pairings.length
                ? dataTable(['Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { hideSpace: true, available })).join(''))
                : stateEmpty({ title: 'No pairing sessions for this space' })}`;
    }

    function membersPanel(members, pairings, available) {
        const header = '<div class="panel-header"><h2>Members</h2></div>';
        if (!members) return header + stateLoading('');
        if (members.error) return header + stateError({ title: "Couldn't load members", message: members.error });
        const rows = members.members || [];
        if (!rows.length) return header + stateEmpty({ title: 'No active members' });
        const body = rows.map(m => {
            const owner = (pairings || []).find(p => p.role === 'source' && p.target_fingerprint
                && p.target_fingerprint === m.fingerprint
                && (p.state === 'active' || p.state === 'blocked_recovery'));
            const action = (available && owner) ? actionButton(owner, 'force-evict-member') : '<span class="text-faint">—</span>';
            return `<tr>
                <td>${m.display_name ? `<span class="mono-data">${esc(m.display_name)}</span>` : '<span class="text-faint" title="No display name set for this peer">—</span>'}</td>
                <td>${m.fingerprint ? copyable(m.fingerprint, truncateMiddle(m.fingerprint)) : '<span class="text-faint">—</span>'}</td>
                <td>${m.endpoint ? `<span class="mono-data">${esc(m.endpoint)}</span>` : '<span class="text-faint">—</span>'}</td>
                <td>${(m.scopes || []).map(sc => pill('neutral', sc)).join(' ') || '<span class="text-faint">full (unrestricted)</span>'}</td>
                <td>${action}</td>
            </tr>`;
        }).join('');
        return header + dataTable(['Display name', 'Fingerprint', 'Endpoint', 'Scopes', 'Evict'], body);
    }

    function invitationsPanel(spaceId, pairings, available) {
        const createAccept = available
            ? `<button type="button" class="btn btn-primary btn-sm" data-action="mesh-create-invitation" data-space-id="${esc(spaceId)}">${icon('plus')}<span>Create invitation</span></button>
            <button type="button" class="btn btn-secondary btn-sm" data-action="mesh-accept-invitation" data-space-id="${esc(spaceId)}">Accept invitation</button>`
            : '';
        const header = `<div class="panel-header"><h2>Invitations & policy</h2><div class="page-header-actions">${createAccept}</div></div>`;
        const list = pairings.length
            ? dataTable(['Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { hideSpace: true, available })).join(''))
            : stateEmpty({ title: 'No invitations or pairing sessions for this space' });
        const policyPanel = panel(`<div class="panel-header"><h2>Signed policy export</h2></div>
            <p class="body-small">Not available yet in this build. The Mesh protocol reserves an optional signed policy export for an external Git mirror; there is no server-side implementation to call yet.</p>`);
        return panel(header + list) + policyPanel;
    }

    function detailTabsHtml(spaceId) {
        return `<div class="sd-tier-tabs" role="tablist" aria-label="Mesh panels">
            ${DETAIL_TABS.map(t => `<button type="button" class="sd-tier-tab${state.detailTab === t ? ' active' : ''}" role="tab" aria-selected="${state.detailTab === t ? 'true' : 'false'}" data-action="mesh-detail-tab" data-tab="${t}">${t === 'invitations' ? 'Invitations &amp; policy' : esc(t.charAt(0).toUpperCase() + t.slice(1))}</button>`).join('')}
        </div>`;
    }

    function paintDetail(contentEl, spaceId, epoch) {
        if (epoch !== AdminRouter.epoch) return;
        const available = meshAvailable(state.status);
        if (!available) {
            // Same rule as the overview: a direct #/mesh/<space-id> visit
            // (deep link/bookmark) can reach this route even though the
            // nav item is absent, and a status response can be unhealthy
            // (process-lock lost) without being null. Never render
            // Create/Accept/evict-style controls that would just POST into
            // a control plane that would 503 (§3, T15) — show the honest
            // unavailable state instead, with a Refresh to retry
            // (GET-only, always safe).
            const actions = `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-refresh">${icon('refresh')}<span>Refresh</span></button>`;
            contentEl.innerHTML = `<div class="page">${pageHeader(spaceId, actions)}${panel(stateUnavailable('Mesh is not available on this instance or for this session.'))}</div>`;
            return;
        }
        const pairings = ((state.status && state.status.pairings) || []).filter(p => p.space_id === spaceId);
        const members = state.members[spaceId];
        let panelsHtml;
        if (state.detailTab === 'members') panelsHtml = panel(membersPanel(members, pairings, available));
        else if (state.detailTab === 'invitations') panelsHtml = invitationsPanel(spaceId, pairings, available);
        else panelsHtml = panel(detailOverviewPanel(spaceId, pairings, members, available));
        const actions = `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-refresh">${icon('refresh')}<span>Refresh</span></button>`;
        contentEl.innerHTML = `<div class="page">${pageHeader(spaceId, actions)}${detailTabsHtml(spaceId)}${panelsHtml}</div>`;
    }

    // ─────────────────────────── create / accept invitation ───────────────────────────

    function encodeInvitationCode(res) {
        const json = JSON.stringify({
            v: 1, secret: res.secret, source_endpoint: res.source_endpoint, invitation: res.invitation,
        });
        const b64 = btoa(json);
        return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function decodeInvitationCode(code) {
        let padded = String(code || '').trim().replace(/-/g, '+').replace(/_/g, '/');
        padded += '='.repeat((4 - (padded.length % 4)) % 4);
        const obj = JSON.parse(atob(padded));
        if (!obj || obj.v !== 1 || typeof obj.secret !== 'string' || !obj.secret
            || typeof obj.source_endpoint !== 'string' || !obj.source_endpoint
            || typeof obj.invitation !== 'string' || !obj.invitation) {
            throw new Error('bad invitation code');
        }
        return obj;
    }

    function spaceOptionsHtml(selectedSpaceId) {
        const spaces = cache.spaces || [];
        return spaces.map(s => `<option value="${esc(s.space_id)}"${s.space_id === selectedSpaceId ? ' selected' : ''}>${esc(s.space_id)}</option>`).join('');
    }

    function openCreateInvitation(prefillSpaceId) {
        if (!isAdmin(_sessionIdentity())) { showToast('error', 'This action requires admin permission.'); return; }
        ensureSpaces().then(() => renderCreateInvitationForm(prefillSpaceId));
    }

    function renderCreateInvitationForm(prefillSpaceId) {
        const options = spaceOptionsHtml(prefillSpaceId);
        const body =
            '<div class="form-group"><label class="form-label" for="meshInvSpace">Space</label>' +
            (options ? `<select class="form-input" id="meshInvSpace">${options}</select>` : '<div class="form-hint">No local spaces found.</div>') +
            '</div>' +
            '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshInvCommit"> Grant commit scope (in addition to read)</label></div>' +
            '<p class="form-hint">Creates a one-time invitation valid for 1 hour. The other administrator pastes it to accept.</p>' +
            '<div class="form-error" id="meshInvErr" hidden></div>';
        _openModal('Create invitation', body, 'Create invitation', onCreateInvitationConfirm);
    }

    async function onCreateInvitationConfirm() {
        const errEl = document.getElementById('meshInvErr');
        if (!meshAvailableNow()) {
            if (errEl) { errEl.textContent = 'Mesh became unavailable while this dialog was open — refresh and try again.'; errEl.hidden = false; }
            return false;
        }
        const select = document.getElementById('meshInvSpace');
        const spaceId = select ? select.value : '';
        if (!spaceId) {
            if (errEl) { errEl.textContent = 'Select a space.'; errEl.hidden = false; }
            return false;
        }
        const commit = document.getElementById('meshInvCommit');
        const scopes = commit && commit.checked ? ['read', 'commit'] : ['read'];
        const epochAtCall = AdminRouter.epoch, genAtCall = _modalGen, sessionAtCall = _sessionIdentity();
        let res;
        try { res = await meshAdminAction('invitation', { space_id: spaceId, scopes }); }
        catch { res = { status: 'error', message: 'Request failed' }; }
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;
        if (res && res.status === 'ok' && res.secret && res.invitation) {
            showInvitationCode(res);
            return false; // the secret step owns the modal now
        }
        if (errEl) {
            errEl.textContent = (res && res.message) ? String(res.message) : 'The server refused or failed this operation.';
            errEl.hidden = false;
        }
        return false;
    }

    // One-time invitation-code display (T5). Mirrors views-access.js's one-time
    // token pattern: the code lives ONLY in the `holder` closure, destroyed —
    // DOM node emptied AND closure zeroed — on every exit path.
    function showInvitationCode(res) {
        const navLock = _navLockAcquire();
        const holder = { value: encodeInvitationCode(res) };
        const body =
            '<p class="secret-warning">' + icon('alert') +
            ' This code contains a one-time secret. Copy it now and send it to the other administrator through a trusted channel — it is shown once and can never be retrieved again.</p>' +
            '<p class="micro-label">Invitation code</p>' +
            `<div class="mono-block secret" id="meshInvCode">${esc(holder.value)}</div>` +
            '<div class="secret-actions"><button type="button" class="btn btn-secondary btn-sm" id="meshInvCopyBtn">' + icon('copy') + '<span>Copy invitation code</span></button></div>' +
            `<div class="token-meta"><span class="micro-label">Source fingerprint</span> ${esc(res.source_fingerprint || '')}</div>` +
            '<p class="form-hint">Expires in 1 hour if unused.</p>';

        function destroySecret() {
            holder.value = '';
            const el = document.getElementById('meshInvCode');
            if (el) el.textContent = '';
            const btn = document.getElementById('meshInvCopyBtn');
            if (btn) btn.disabled = true;
            _navLockRelease(navLock);
        }

        _openModal('Invitation created — save the code now', body, 'I have saved it', async () => {
            destroySecret();
            AdminRouter.refresh();
            return true;
        });

        const modalEl = document.getElementById('adminModal');
        if (modalEl) {
            modalEl.querySelectorAll('[data-action="close-modal"]').forEach(c => {
                c.addEventListener('click', destroySecret);
            });
        }
        const copyBtn = document.getElementById('meshInvCopyBtn');
        if (copyBtn) {
            copyBtn.addEventListener('click', () => {
                if (!holder.value) { showToast('warn', 'Code already cleared — create a new invitation'); return; }
                _copySecret(holder, AdminRouter.epoch, _modalGen, _sessionIdentity(), 'Invitation code copied');
            });
        }
    }

    function openAcceptInvitation(prefillSpaceId) {
        if (!isAdmin(_sessionIdentity())) { showToast('error', 'This action requires admin permission.'); return; }
        ensureSpaces().then(() => renderAcceptForm(prefillSpaceId));
    }

    function renderAcceptForm(prefillSpaceId) {
        const options = spaceOptionsHtml(prefillSpaceId);
        const body =
            '<div class="form-group"><label class="form-label" for="meshAccCode">Invitation code</label>' +
            '<textarea class="form-input mono" id="meshAccCode" rows="4" placeholder="Paste the invitation code from the other administrator"></textarea></div>' +
            '<div class="form-group"><label class="form-label" for="meshAccSpace">Target space (must be blank)</label>' +
            (options ? `<select class="form-input" id="meshAccSpace">${options}</select>` : '<div class="form-hint">No local spaces found — create one first.</div>') +
            '</div>' +
            '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshAccCommit"> Request commit scope (in addition to read)</label></div>' +
            '<div class="form-error" id="meshAccErr" hidden></div>';
        _openModal('Accept invitation', body, 'Accept', onAcceptConfirm);
    }

    async function onAcceptConfirm() {
        const errEl = document.getElementById('meshAccErr');
        if (!meshAvailableNow()) {
            if (errEl) { errEl.textContent = 'Mesh became unavailable while this dialog was open — refresh and try again.'; errEl.hidden = false; }
            return false;
        }
        const codeEl = document.getElementById('meshAccCode');
        let parsed;
        try { parsed = decodeInvitationCode(codeEl ? codeEl.value : ''); }
        catch {
            if (errEl) { errEl.textContent = 'That does not look like a valid invitation code.'; errEl.hidden = false; }
            return false;
        }
        const spaceSel = document.getElementById('meshAccSpace');
        const targetSpaceId = spaceSel ? spaceSel.value : '';
        if (!targetSpaceId) {
            if (errEl) { errEl.textContent = 'Select a target space.'; errEl.hidden = false; }
            return false;
        }
        const commit = document.getElementById('meshAccCommit');
        const scopes = commit && commit.checked ? ['read', 'commit'] : ['read'];
        const epochAtCall = AdminRouter.epoch, genAtCall = _modalGen, sessionAtCall = _sessionIdentity();
        let res;
        try {
            res = await meshAdminAction('accept', {
                invitation: parsed.invitation, secret: parsed.secret,
                source_endpoint: parsed.source_endpoint, target_space_id: targetSpaceId, scopes,
            });
        } catch { res = { status: 'error', message: 'Request failed' }; }
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;
        if (res && res.status === 'ok') {
            showToast('ok', 'Invitation accepted — waiting for the other administrator to approve.');
            AdminRouter.refresh();
            return true;
        }
        if (errEl) {
            errEl.textContent = (res && res.message) ? String(res.message) : 'The server refused or failed this operation.';
            errEl.hidden = false;
        }
        return false;
    }

    // ─────────────────────────── render entry ───────────────────────────

    function render(contentEl, params, ctx) {
        const nextSessionGeneration = (ctx && Number.isInteger(ctx.sessionGeneration)) ? ctx.sessionGeneration : null;
        if (state.sessionGeneration !== nextSessionGeneration) {
            state.status = null;
            state.members = {};
            state.detailTab = 'overview';
            _modalGen += 1;
        }
        state.sessionGeneration = nextSessionGeneration;
        state.identity = (ctx && ctx.identity) || {};
        const epoch = ctx ? ctx.epoch : AdminRouter.epoch;
        if (params && params.spaceId !== undefined) {
            renderDetail(contentEl, params.spaceId, epoch);
        } else {
            renderOverviewShell(contentEl, epoch);
        }
    }

    // ─────────────────────────── action registration ───────────────────────────

    registerAction('mesh-refresh', () => AdminRouter.refresh());
    registerAction('mesh-run-action', d => confirmMeshAction(d.pairId, d.meshAction));
    registerAction('mesh-create-invitation', d => openCreateInvitation(d.spaceId));
    registerAction('mesh-accept-invitation', d => openAcceptInvitation(d.spaceId));
    registerAction('mesh-detail-tab', d => {
        if (!DETAIL_TABS.includes(d.tab)) return;
        state.detailTab = d.tab;
        const contentEl = document.getElementById('content');
        const current = AdminRouter.current();
        if (contentEl && current.view === 'mesh-detail') paintDetail(contentEl, current.params.spaceId, AdminRouter.epoch);
    });

    AdminViews.register('mesh', render);
    AdminViews.register('mesh-detail', render);
})();
