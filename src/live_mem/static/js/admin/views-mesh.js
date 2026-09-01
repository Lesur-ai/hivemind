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
 * per-action toggle); source readiness comes only from the server-owned
 * `source_readiness[]` predicate and its derived `eligible_spaces[]` projection —
 * the Create picker never guesses from `space_list`; `pending_actions[]` is
 * derived client-side from the real `pairings[]` state via
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
    const SOURCE_STATE_TOKEN_RE = /^[0-9a-f]{64}$/;
    const SOURCE_READINESS_STATES = new Set([
        'local_only_can_prepare', 'preparing', 'prepare_recovery_required',
        'ready', 'busy', 'pairing_in_flight', 'mutation_in_progress',
        'insufficient_scope', 'multi_member', 'identity_mismatch', 'unavailable', 'unsafe',
        'resync_required', 'not_a_space',
    ]);
    const DETAIL_TABS = ['overview', 'members', 'invitations'];

    // ─────────────── modal generation & staleness (mirrors views-access.js) ───────────────
    let _modalGen = 0;

    function _openModal(title, body, verb, onConfirm) {
        _modalGen += 1;
        const generation = _modalGen;
        showModal(title, body, verb, onConfirm);
        const modal = document.getElementById('adminModal');
        if (modal) {
            // The shell owns closeModal(), so a close does not otherwise
            // replace this view's modal generation.  Bind explicit dismissal
            // to the generation that rendered these controls: late awaited
            // continuations must not write into a closed/reused overlay.
            modal.querySelectorAll('[data-action="close-modal"]').forEach(control => {
                control.addEventListener('click', () => {
                    if (_modalGen === generation) _modalGen += 1;
                });
            });
        }
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
    let _invitationRequest = null;

    function _navLockAcquire() {
        const lock = {
            hash: location.hash,
            session: _sessionIdentity(),
            sessionGeneration: state.sessionGeneration,
        };
        _navLock = lock;
        return lock;
    }

    function _navLockRelease(lock) {
        if (_navLock === lock) _navLock = null;
    }

    function _navLockSessionCurrent(lock) {
        const overlay = document.getElementById('loginOverlay');
        if (overlay && !overlay.classList.contains('hidden')) return false;
        return _sessionIdentity() === lock.session
            && state.sessionGeneration === lock.sessionGeneration;
    }

    window.addEventListener('hashchange', () => {
        if (!_navLock || location.hash === _navLock.hash) return;
        if (!_navLockSessionCurrent(_navLock)) {
            _navLock = null;
            return;
        }
        location.hash = _navLock.hash;
    });

    function _setModalDismissEnabled(enabled) {
        const modal = document.getElementById('adminModal');
        if (!modal) return;
        modal.querySelectorAll('[data-action="close-modal"]').forEach(control => {
            control.disabled = !enabled;
            if (enabled) control.removeAttribute('aria-disabled');
            else control.setAttribute('aria-disabled', 'true');
        });
    }

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

    // Source readiness is server-owned. These helpers validate/project the
    // documented response shape but never infer eligibility from hive labels,
    // local space metadata, or pairing state in the browser.
    function sourceReadinessUnavailable(status) {
        return !!(status && status.source_readiness_unavailable === true);
    }

    function sourceReadinessUnavailableNotice(status) {
        // Map only the closed server contract to fixed operator copy. Never
        // render source_readiness_unavailable_reason: it is diagnostic input,
        // not trusted display text.
        const inventoryLimitExceeded = !!(status
            && (status.source_readiness_truncated === true
                || status.source_readiness_unavailable_reason === 'mesh_status_inventory_too_large'));
        const detail = inventoryLimitExceeded
            ? 'The bounded source inventory limit was exceeded, so no partial source list is shown. Pairing history and recovery controls remain available.'
            : 'Source readiness could not be loaded. Pairing history and recovery controls remain available.';
        return `<div class="sd-banner sd-banner--warn" role="status">${icon('alert')}<div>
            <strong>Source readiness is unavailable</strong>
            <p>${esc(detail)} Refresh to retry; source preparation and invitation creation stay hidden until the inventory is authoritative.</p>
        </div></div>`;
    }

    function sourceReadinessIsCoherent(entry) {
        if (entry.reason_code !== entry.state) return false;
        if (entry.can_create_invitation === true
            && entry.source_ready !== true) return false;
        if (entry.source_ready === true
            && entry.state !== 'ready' && entry.state !== 'pairing_in_flight') return false;
        if (entry.source_initializable === true
            && entry.state !== 'local_only_can_prepare' && entry.state !== 'preparing') return false;
        if (entry.resumable === true && entry.state !== 'preparing') return false;
        if (entry.state === 'local_only_can_prepare') {
            return entry.source_ready === false
                && entry.source_initializable === true
                && entry.can_create_invitation === false
                && entry.resumable === false;
        }
        if (entry.state === 'preparing') {
            return entry.source_ready === false
                && entry.source_initializable === true
                && entry.can_create_invitation === false
                && entry.resumable === true;
        }
        if (entry.state === 'ready') {
            return entry.source_ready === true
                && entry.source_initializable === false
                && entry.can_create_invitation === true
                && entry.resumable === false;
        }
        if (entry.state !== 'pairing_in_flight') {
            return entry.source_ready === false
                && entry.source_initializable === false
                && entry.can_create_invitation === false
                && entry.resumable === false;
        }
        return entry.source_initializable === false && entry.resumable === false;
    }

    function sourceReadinessEntries(status) {
        if (sourceReadinessUnavailable(status)) return [];
        if (!status || !Array.isArray(status.source_readiness)) return [];
        const entries = status.source_readiness.filter(entry => entry
            && typeof entry.space_id === 'string'
            && SPACE_ID_RE.test(entry.space_id)
            && SOURCE_READINESS_STATES.has(entry.state)
            && typeof entry.source_ready === 'boolean'
            && typeof entry.source_initializable === 'boolean'
            && typeof entry.can_create_invitation === 'boolean'
            && typeof entry.resumable === 'boolean'
            && typeof entry.reason_code === 'string'
            && typeof entry.message === 'string'
            && typeof entry.state_token === 'string'
            && SOURCE_STATE_TOKEN_RE.test(entry.state_token)
            && sourceReadinessIsCoherent(entry));
        const counts = new Map();
        entries.forEach(entry => counts.set(entry.space_id, (counts.get(entry.space_id) || 0) + 1));
        return entries.filter(entry => counts.get(entry.space_id) === 1);
    }

    function sourceReadinessFor(spaceId, status) {
        return sourceReadinessEntries(status || state.status)
            .find(entry => entry.space_id === spaceId) || null;
    }

    function eligibleSourceEntries(status) {
        if (!status || !Array.isArray(status.eligible_spaces)) return [];
        const readiness = sourceReadinessEntries(status);
        const byId = new Map(readiness.map(entry => [entry.space_id, entry]));
        const seen = new Set();
        const eligible = [];
        status.eligible_spaces.forEach(item => {
            if (typeof item !== 'string' || !SPACE_ID_RE.test(item)) return;
            const spaceId = item;
            const entry = byId.get(spaceId);
            if (!entry || seen.has(spaceId) || entry.can_create_invitation !== true) return;
            seen.add(spaceId);
            eligible.push(entry);
        });
        return eligible;
    }

    function sourceCanPrepare(entry) {
        if (!entry || typeof entry.state_token !== 'string'
            || !SOURCE_STATE_TOKEN_RE.test(entry.state_token)) return false;
        if (entry.state === 'preparing') {
            return entry.source_initializable === true && entry.resumable === true;
        }
        return entry.state === 'local_only_can_prepare' && entry.source_initializable === true;
    }

    function sourceStatePill(entry) {
        const value = entry && entry.state ? entry.state : 'unavailable';
        const kind = value === 'ready' ? 'ok'
            : (value === 'local_only_can_prepare' || value === 'preparing' || value === 'pairing_in_flight') ? 'warn'
            : 'error';
        return pill(kind, String(value).replace(/_/g, ' '));
    }

    function sourceActionButton(entry) {
        if (!entry) return '';
        if (entry.can_create_invitation === true) {
            return `<button type="button" class="btn btn-primary btn-sm" data-action="mesh-create-invitation" data-space-id="${esc(entry.space_id)}">${icon('plus')}<span>Create invitation</span></button>`;
        }
        if (sourceCanPrepare(entry)) {
            const label = entry.state === 'preparing' ? 'Resume preparation' : 'Prepare for Project Mesh';
            const ariaLabel = `${label} for ${entry.space_id}`;
            return `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-prepare-source" data-space-id="${esc(entry.space_id)}" aria-label="${esc(ariaLabel)}">${esc(label)}</button>`;
        }
        return '';
    }

    function sourceReasonHtml(entry) {
        if (!entry) return stateUnavailable('Source readiness is unavailable. Refresh before taking action.');
        const reason = entry.reason_code
            ? `<span class="mono-data">${esc(String(entry.reason_code))}</span>`
            : '<span class="text-faint">No reason code</span>';
        const message = entry.message ? serverMessage(entry.message) : '';
        return `<div class="body-small">${reason}${message}</div>`;
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
        statusGeneration: 0, // increments whenever authoritative readiness/status is replaced
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
        state.statusGeneration += 1;
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

    // Target selection remains a distinct predicate. Only Accept uses the
    // ordinary local-space list; source selection above never calls it.
    function ensureSpaces() {
        return callTool('space_list', {}).then(resp => {
            if (!resp || resp.status !== 'ok' || !Array.isArray(resp.spaces)) {
                cache.spaces = [];
                return {
                    status: 'error',
                    message: (resp && resp.message) || 'Local spaces are unavailable.',
                };
            }
            const spaces = resp.spaces.filter(space => space
                && typeof space.space_id === 'string'
                && SPACE_ID_RE.test(space.space_id));
            cache.spaces = spaces;
            return { ...resp, spaces };
        }).catch(() => {
            cache.spaces = [];
            return { status: 'error', message: 'Local spaces are unavailable.' };
        });
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
    function overviewActions(available, readinessUnavailable) {
        const refresh = `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-refresh">${icon('refresh')}<span>Refresh</span></button>`;
        if (!available) return refresh;
        const create = readinessUnavailable
            ? ''
            : `<button type="button" class="btn btn-primary btn-sm" data-action="mesh-create-invitation">${icon('plus')}<span>Create invitation</span></button>`;
        return refresh + create
            + '<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-accept-invitation">Accept invitation</button>';
    }

    function instanceCard(s, available) {
        const note = !available
            ? 'This instance reports unhealthy — mutating Mesh actions are unavailable until it recovers.'
            : sourceReadinessUnavailable(s)
                ? 'Pairing and recovery actions remain available; source actions are hidden until readiness can be loaded.'
                : 'Signed in as an admin session — every Mesh action below is available.';
        return `<div class="panel-header"><h2>This instance</h2>${statusDot(s.healthy ? 'ok' : 'error', s.healthy ? 'healthy' : 'unhealthy')}</div>
            <div class="sd-meta-row">
                <div class="sd-kv"><span class="micro-label">DISPLAY NAME</span><span class="mono-data">${esc(s.display_name || '—')}</span></div>
                <div class="sd-kv"><span class="micro-label">FINGERPRINT</span>${copyable(s.fingerprint || '')}</div>
                <div class="sd-kv"><span class="micro-label">PUBLIC URL</span><span class="mono-data">${esc(s.public_url || '—')}</span></div>
            </div>
            <p class="body-small">${esc(note)}</p>`;
    }

    function pairingHistoryTruncatedBanner() {
        return `<div class="sd-banner sd-banner--warn" role="status">${icon('alert')}<div>
            <strong>Pairing history is truncated</strong>
            <p>Only a bounded diagnostic slice is shown. An absent session or action in this view is not authoritative.</p>
        </div></div>`;
    }

    function pairingMetadataTruncatedBanner() {
        return `<div class="sd-banner sd-banner--warn" role="status">${icon('alert')}<div>
            <strong>Pairing metadata is truncated</strong>
            <p>Membership is authoritative, but fingerprints, endpoints, scopes, and pairing-derived actions may be missing from this bounded diagnostic slice.</p>
        </div></div>`;
    }

    function attentionSection(attention, available, historyTruncated) {
        const header = '<div class="panel-header"><h2>Needs your attention</h2></div>';
        if (!attention.length) {
            const hint = historyTruncated
                ? 'No actionable session appears in the loaded slice; omitted history may still require attention.'
                : 'Every pairing on this instance is settled or has no pending operator action.';
            return header + stateEmpty({ title: historyTruncated ? 'No action in the loaded slice' : 'Nothing needs attention', hint });
        }
        return header + dataTable(['Space', 'Role', 'State', 'Updated', 'Action'], attention.map(p => renderPairingRow(p, { available })).join(''));
    }

    function pairingsSection(pairings, available, historyTruncated) {
        const header = '<div class="panel-header"><h2>All pairings</h2></div>';
        if (!pairings.length) {
            const hint = historyTruncated
                ? 'No session appears in the loaded slice; omitted history may contain pairings.'
                : 'Create an invitation to pair a space with another Hivemind instance.';
            return header + stateEmpty({ title: historyTruncated ? 'No pairing in the loaded slice' : 'No pairings yet', hint });
        }
        return header + dataTable(['Space', 'Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { available })).join(''));
    }

    function sourceReadinessSection(status, available) {
        const header = '<div class="panel-header"><h2>Invitation sources</h2></div>';
        if (sourceReadinessUnavailable(status)) {
            return header + sourceReadinessUnavailableNotice(status);
        }
        const entries = sourceReadinessEntries(status);
        if (!entries.length) {
            return header + stateEmpty({ title: 'No committed spaces reported', hint: 'Refresh after creating a local space.' });
        }
        const rows = entries.map(entry => {
            // Ready sources are selected through the single page-level Create
            // flow; only preparation/resume needs an inline affordance here.
            const action = available && sourceCanPrepare(entry) ? sourceActionButton(entry) : '';
            return `<tr>
                <td><a class="sd-link" href="#/mesh/${encodeURIComponent(entry.space_id)}">${esc(entry.space_id)}</a></td>
                <td>${sourceStatePill(entry)}</td>
                <td>${sourceReasonHtml(entry)}</td>
                <td class="mesh-row-actions">${action || '<span class="text-faint">—</span>'}</td>
            </tr>`;
        }).join('');
        return header + dataTable(['Space', 'Readiness', 'Reason', 'Action'], rows);
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
        const historyTruncated = s.pairings_truncated === true;
        const readinessUnavailable = sourceReadinessUnavailable(s);
        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Project Mesh', overviewActions(available, readinessUnavailable))}
            ${panel(instanceCard(s, available))}
            ${historyTruncated ? pairingHistoryTruncatedBanner() : ''}
            ${panel(sourceReadinessSection(s, available))}
            ${panel(attentionSection(attention, available, historyTruncated))}
            ${panel(pairingsSection(pairings, available, historyTruncated))}
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

    function detailOverviewPanel(spaceId, pairings, members, available, historyTruncated) {
        const epochKnown = members && !members.error && members.membership_epoch != null;
        const memberCount = members && !members.error ? (members.members || []).length : null;
        return `<div class="panel-header"><h2>Overview</h2></div>
            ${historyTruncated ? pairingHistoryTruncatedBanner() : ''}
            <div class="sd-meta-row">
                <div class="sd-kv"><span class="micro-label">MEMBERSHIP EPOCH</span><span class="mono-data">${epochKnown ? esc(String(members.membership_epoch)) : '—'}</span></div>
                <div class="sd-kv"><span class="micro-label">ACTIVE MEMBERS</span><span class="mono-data">${memberCount === null ? '—' : esc(String(memberCount))}</span></div>
                <div class="sd-kv"><span class="micro-label">${historyTruncated ? 'LOADED PAIRING SESSIONS' : 'PAIRING SESSIONS'}</span><span class="mono-data">${esc(String(pairings.length))}</span></div>
            </div>
            ${pairings.length
                ? dataTable(['Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { hideSpace: true, available })).join(''))
                : stateEmpty({ title: historyTruncated ? 'No session for this space in the loaded slice' : 'No pairing sessions for this space' })}`;
    }

    function membersPanel(members, pairings, available) {
        const header = '<div class="panel-header"><h2>Members</h2></div>';
        if (!members) return header + stateLoading('');
        if (members.error) return header + stateError({ title: "Couldn't load members", message: members.error });
        const truncation = members.pairing_metadata_truncated === true ? pairingMetadataTruncatedBanner() : '';
        const rows = members.members || [];
        if (!rows.length) return header + truncation + stateEmpty({ title: 'No active members' });
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
        return header + truncation + dataTable(['Display name', 'Fingerprint', 'Endpoint', 'Scopes', 'Evict'], body);
    }

    function invitationsPanel(spaceId, pairings, available, historyTruncated) {
        const readinessUnavailable = sourceReadinessUnavailable(state.status);
        const readiness = readinessUnavailable ? null : sourceReadinessFor(spaceId, state.status);
        const sourceAction = available && !readinessUnavailable ? sourceActionButton(readiness) : '';
        const acceptAction = available
            ? `<button type="button" class="btn btn-secondary btn-sm" data-action="mesh-accept-invitation" data-space-id="${esc(spaceId)}">Accept invitation</button>`
            : '';
        const header = `<div class="panel-header"><h2>Invitations & policy</h2><div class="page-header-actions">${sourceAction}${acceptAction}</div></div>`;
        const readinessPanel = readinessUnavailable
            ? `<div class="item-card"><div class="panel-header"><h3>Invitation source readiness</h3></div>${sourceReadinessUnavailableNotice(state.status)}</div>`
            : `<div class="item-card">
                <div class="panel-header"><h3>Invitation source readiness</h3>${readiness ? sourceStatePill(readiness) : ''}</div>
                ${sourceReasonHtml(readiness)}
            </div>`;
        const list = pairings.length
            ? dataTable(['Role', 'State', 'Updated', 'Action'], pairings.map(p => renderPairingRow(p, { hideSpace: true, available })).join(''))
            : stateEmpty({ title: historyTruncated ? 'No invitation or session for this space in the loaded slice' : 'No invitations or pairing sessions for this space' });
        const policyPanel = panel(`<div class="panel-header"><h2>Signed policy export</h2></div>
            <p class="body-small">Not available yet in this build. The Mesh protocol reserves an optional signed policy export for an external Git mirror; there is no server-side implementation to call yet.</p>`);
        return panel(header + (historyTruncated ? pairingHistoryTruncatedBanner() : '') + readinessPanel + list) + policyPanel;
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
        const historyTruncated = state.status && state.status.pairings_truncated === true;
        let panelsHtml;
        if (state.detailTab === 'members') panelsHtml = panel(membersPanel(members, pairings, available));
        else if (state.detailTab === 'invitations') panelsHtml = invitationsPanel(spaceId, pairings, available, historyTruncated);
        else panelsHtml = panel(detailOverviewPanel(spaceId, pairings, members, available, historyTruncated));
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

    function eligibleSourceOptionsHtml(selectedSpaceId) {
        return eligibleSourceEntries(state.status).map(entry =>
            `<option value="${esc(entry.space_id)}"${entry.space_id === selectedSpaceId ? ' selected' : ''}>${esc(entry.space_id)}</option>`
        ).join('');
    }

    function sourcePickerReadinessHtml() {
        const entries = sourceReadinessEntries(state.status);
        if (!entries.length) return stateUnavailable('No authoritative source readiness was returned. Refresh and try again.');
        return entries.map(entry => {
            const action = sourceCanPrepare(entry) ? sourceActionButton(entry) : '';
            return `<div class="item-card">
                <div class="panel-header"><strong>${esc(entry.space_id)}</strong>${sourceStatePill(entry)}</div>
                ${sourceReasonHtml(entry)}
                ${action ? `<div class="actions">${action}</div>` : ''}
            </div>`;
        }).join('');
    }

    function openCreateInvitation(prefillSpaceId) {
        if (!isAdmin(_sessionIdentity())) { showToast('error', 'This action requires admin permission.'); return; }
        if (!meshAvailableNow()) { showToast('error', 'Mesh status is stale or unavailable — refresh and try again.'); return; }
        renderCreateInvitationForm(prefillSpaceId);
    }

    function renderCreateInvitationForm(prefillSpaceId) {
        const options = eligibleSourceOptionsHtml(prefillSpaceId);
        const body =
            '<div class="form-group"><label class="form-label" for="meshInvSpace">Ready source space</label>' +
            (options ? `<select class="form-input" id="meshInvSpace">${options}</select>` : '<div class="form-hint">No space is currently ready to create an invitation.</div>') +
            '</div>' +
            (options ? '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshInvCommit"> Grant commit scope (in addition to read)</label></div>' : '') +
            (options ? '<p class="form-hint">Creates a one-time invitation valid for 1 hour. The other administrator pastes it to accept.</p>' : '') +
            '<div class="panel-header"><h3>Source readiness</h3></div>' + sourcePickerReadinessHtml() +
            '<div class="form-error" id="meshInvErr" hidden></div>';
        _openModal('Create invitation', body, options ? 'Create invitation' : '', options ? onCreateInvitationConfirm : null);
    }

    async function onCreateInvitationConfirm() {
        const errEl = document.getElementById('meshInvErr');
        if (_invitationRequest) {
            // Refuse a duplicate from the same live session/modal.  A request
            // abandoned by a session wipe must not permanently poison a new
            // login if its network response never arrives.
            if (_navLockSessionCurrent(_invitationRequest.navLock)) return false;
            _navLockRelease(_invitationRequest.navLock);
            _invitationRequest = null;
        }
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
        const readiness = sourceReadinessFor(spaceId, state.status);
        const eligible = eligibleSourceEntries(state.status).some(entry => entry.space_id === spaceId);
        if (!readiness || readiness.can_create_invitation !== true || !eligible) {
            if (errEl) { errEl.textContent = 'This source is no longer invitation-ready — refresh and try again.'; errEl.hidden = false; }
            return false;
        }
        const commit = document.getElementById('meshInvCommit');
        const scopes = commit && commit.checked ? ['read', 'commit'] : ['read'];
        const navLock = _navLockAcquire();
        const ctx = {
            modalGeneration: _modalGen,
            sessionGeneration: state.sessionGeneration,
            sessionIdentity: _sessionIdentity(),
            navLock,
        };
        _invitationRequest = ctx;
        _setModalDismissEnabled(false);
        let res;
        try { res = await meshAdminAction('invitation', { space_id: spaceId, scopes }); }
        catch { res = { status: 'error', message: 'Request failed' }; }
        finally {
            if (_invitationRequest === ctx) _invitationRequest = null;
        }

        const stale = _modalGen !== ctx.modalGeneration
            || _navLock !== navLock
            || state.sessionGeneration !== ctx.sessionGeneration
            || _sessionIdentity() !== ctx.sessionIdentity
            || !_navLockSessionCurrent(navLock);
        if (stale) {
            _navLockRelease(navLock);
            return false;
        }
        if (res && res.status === 'ok'
            && typeof res.secret === 'string' && res.secret
            && typeof res.invitation === 'string' && res.invitation
            && typeof res.source_endpoint === 'string' && res.source_endpoint) {
            showInvitationCode(res, navLock);
            return false; // the secret step owns the modal now
        }
        _navLockRelease(navLock);
        _setModalDismissEnabled(true);
        if (errEl) {
            errEl.textContent = (res && res.message) ? String(res.message) : 'The server refused or failed this operation.';
            errEl.hidden = false;
        }
        return false;
    }

    // Source preparation is an explicit, one-way maintenance action. It is
    // intentionally separate from invitation creation: success refreshes
    // readiness and never chains into an invitation POST.
    function prepareContextIsStale(ctx) {
        return _isStale(ctx.epoch, ctx.modalGeneration, ctx.sessionIdentity)
            || state.sessionGeneration !== ctx.sessionGeneration
            || state.statusGeneration !== ctx.statusGeneration
            || !meshAvailableNow();
    }

    function setPrepareError(message) {
        const errEl = document.getElementById('meshPrepareErr');
        if (!errEl) return;
        errEl.textContent = String(message || 'The server refused or failed this operation.');
        errEl.hidden = false;
    }

    function openPrepareSource(spaceId) {
        if (!isAdmin(_sessionIdentity())) { showToast('error', 'This action requires admin permission.'); return; }
        if (!meshAvailableNow()) { showToast('error', 'Mesh status is stale or unavailable — refresh and try again.'); return; }
        const readiness = sourceReadinessFor(spaceId, state.status);
        if (!sourceCanPrepare(readiness)) {
            showToast('error', 'This space cannot be prepared from the current authoritative state. Refresh and inspect its reason.');
            return;
        }

        const label = readiness.state === 'preparing' ? 'Resume preparation' : 'Prepare for Project Mesh';
        const ctx = {
            spaceId,
            stateToken: readiness.state_token,
            epoch: AdminRouter.epoch,
            modalGeneration: _modalGen + 1,
            sessionGeneration: state.sessionGeneration,
            sessionIdentity: _sessionIdentity(),
            statusGeneration: state.statusGeneration,
            inFlight: false,
        };
        const body =
            '<p class="body-small"><strong>One-way transition in v1.4.1.</strong> This preserves the existing space content and adds Project Mesh coordination state. It cannot be reverted to local-only.</p>' +
            '<p class="body-small">Stop every agent, consolidation, restore, repair, garbage collection, and other writer for this space before continuing. Preparation does not claim that every shared write is currently serviceable.</p>' +
            '<div class="form-group"><label class="form-label" for="meshPrepareConfirmInput">Type <code class="typed-challenge">&quot;' + esc(spaceId) + '&quot;</code> to confirm</label>' +
            '<input class="form-input mono" id="meshPrepareConfirmInput" autocomplete="off" data-1p-ignore data-lpignore="true"></div>' +
            '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshPrepareQuiesced"> I confirm that every same-space writer and maintenance job is quiesced.</label></div>' +
            '<div class="form-error" id="meshPrepareErr" hidden></div>';

        _openModal(label, body, label, () => onPrepareSourceConfirm(ctx));

        const input = document.getElementById('meshPrepareConfirmInput');
        const checkbox = document.getElementById('meshPrepareQuiesced');
        const confirmBtn = document.getElementById('modalConfirmBtn');
        if (!input || !checkbox || !confirmBtn) return;
        const syncConfirm = () => {
            confirmBtn.disabled = input.value !== spaceId || checkbox.checked !== true || ctx.inFlight;
        };
        confirmBtn.disabled = true;
        input.addEventListener('input', syncConfirm);
        checkbox.addEventListener('change', syncConfirm);
    }

    async function onPrepareSourceConfirm(ctx) {
        // This check is deliberately BEFORE the fetch. A stale overlay must
        // cause zero mutation, not merely discard a late response.
        if (prepareContextIsStale(ctx) || ctx.inFlight) return false;
        const input = document.getElementById('meshPrepareConfirmInput');
        const checkbox = document.getElementById('meshPrepareQuiesced');
        if (!input || input.value !== ctx.spaceId) {
            setPrepareError('Type the exact space id to confirm.');
            return false;
        }
        if (!checkbox || checkbox.checked !== true) {
            setPrepareError('Confirm that all same-space writers are quiesced.');
            return false;
        }
        const current = sourceReadinessFor(ctx.spaceId, state.status);
        if (!sourceCanPrepare(current) || current.state_token !== ctx.stateToken) {
            setPrepareError('Source readiness changed while this dialog was open — refresh and try again.');
            return false;
        }

        ctx.inFlight = true;
        let res;
        try {
            res = await meshAdminAction('prepare-source', {
                space_id: ctx.spaceId,
                quiesced: true,
                expected_state_token: ctx.stateToken,
            });
        } catch {
            res = { status: 'error', message: 'Request failed' };
        } finally {
            ctx.inFlight = false;
        }
        if (prepareContextIsStale(ctx)) return false;
        if (res && res.status === 'ok'
            && (res.result === 'prepared' || res.result === 'already_ready')) {
            showToast('ok', res.result === 'already_ready'
                ? 'This space is already ready for Project Mesh.'
                : 'Space prepared for Project Mesh.');
            AdminRouter.refresh();
            return true;
        }
        if (res && res.code === 'source_state_changed') {
            setPrepareError('Source state changed. Refresh before trying again; preparation was not retried automatically.');
            return false;
        }
        setPrepareError((res && res.message) || 'The server refused or failed this operation.');
        return false;
    }

    // One-time invitation-code display (T5). Mirrors views-access.js's one-time
    // token pattern: the code lives ONLY in the `holder` closure, destroyed —
    // DOM node emptied AND closure zeroed — on every exit path.
    function showInvitationCode(res, navLock) {
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

    function acceptContextIsStale(ctx) {
        return _isStale(ctx.epoch, ctx.modalGeneration, ctx.sessionIdentity)
            || state.sessionGeneration !== ctx.sessionGeneration
            || state.statusGeneration !== ctx.statusGeneration
            || !meshAvailableNow();
    }

    async function openAcceptInvitation(prefillSpaceId) {
        if (!isAdmin(_sessionIdentity())) { showToast('error', 'This action requires admin permission.'); return; }
        if (!meshAvailableNow()) { showToast('error', 'Mesh status is stale or unavailable — refresh and try again.'); return; }
        const loadCtx = {
            epoch: AdminRouter.epoch,
            modalGeneration: _modalGen,
            sessionGeneration: state.sessionGeneration,
            sessionIdentity: _sessionIdentity(),
            statusGeneration: state.statusGeneration,
        };
        if (acceptContextIsStale(loadCtx)) return;
        const result = await ensureSpaces();
        if (acceptContextIsStale(loadCtx)) return;
        renderAcceptForm(prefillSpaceId, result);
    }

    function renderAcceptForm(prefillSpaceId, result) {
        if (!result || result.status !== 'ok' || !Array.isArray(result.spaces)) {
            _openModal(
                'Accept invitation',
                stateUnavailable((result && result.message) || 'Local spaces are unavailable.'),
                '',
                null,
            );
            return;
        }
        const options = spaceOptionsHtml(prefillSpaceId);
        if (!options) {
            _openModal(
                'Accept invitation',
                stateUnavailable('No local spaces found — create a blank target space first.'),
                '',
                null,
            );
            return;
        }
        const ctx = {
            epoch: AdminRouter.epoch,
            modalGeneration: _modalGen + 1,
            sessionGeneration: state.sessionGeneration,
            sessionIdentity: _sessionIdentity(),
            statusGeneration: state.statusGeneration,
            inFlight: false,
        };
        const body =
            '<div class="form-group"><label class="form-label" for="meshAccCode">Invitation code</label>' +
            '<textarea class="form-input mono" id="meshAccCode" rows="4" placeholder="Paste the invitation code from the other administrator"></textarea></div>' +
            '<div class="form-group"><label class="form-label" for="meshAccSpace">Target space (must be blank)</label>' +
            `<select class="form-input" id="meshAccSpace">${options}</select>` +
            '</div>' +
            '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshAccCommit"> Request commit scope (in addition to read)</label></div>' +
            '<p class="body-small">Before accepting, stop every same-space agent, direct writer, consolidation, repair, restore, garbage-collection, and maintenance job. This attestation is an operational precondition; it does not coordinate another running process.</p>' +
            '<div class="form-group"><label class="space-check"><input type="checkbox" id="meshAccQuiesced"> I confirm that every same-space writer and maintenance job is quiesced.</label></div>' +
            '<div class="form-error" id="meshAccErr" hidden></div>';
        _openModal('Accept invitation', body, 'Accept', () => onAcceptConfirm(ctx));

        const checkbox = document.getElementById('meshAccQuiesced');
        const confirmBtn = document.getElementById('modalConfirmBtn');
        if (!checkbox || !confirmBtn) return;
        const syncConfirm = () => {
            confirmBtn.disabled = checkbox.checked !== true || ctx.inFlight;
        };
        confirmBtn.disabled = true;
        checkbox.addEventListener('change', syncConfirm);
    }

    async function onAcceptConfirm(ctx) {
        const errEl = document.getElementById('meshAccErr');
        if (acceptContextIsStale(ctx) || ctx.inFlight) {
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
        const quiesced = document.getElementById('meshAccQuiesced');
        if (!quiesced || quiesced.checked !== true) {
            if (errEl) { errEl.textContent = 'Confirm that all same-space writers and maintenance jobs are quiesced.'; errEl.hidden = false; }
            return false;
        }
        const commit = document.getElementById('meshAccCommit');
        const scopes = commit && commit.checked ? ['read', 'commit'] : ['read'];
        ctx.inFlight = true;
        let res;
        try {
            res = await meshAdminAction('accept', {
                invitation: parsed.invitation, secret: parsed.secret,
                source_endpoint: parsed.source_endpoint, target_space_id: targetSpaceId, scopes,
                quiesced: true,
            });
        } catch { res = { status: 'error', message: 'Request failed' }; }
        finally { ctx.inFlight = false; }
        if (acceptContextIsStale(ctx)) return false;
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
            state.statusEpoch = null;
            state.statusGeneration += 1;
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
    registerAction('mesh-prepare-source', d => openPrepareSource(d.spaceId));
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
