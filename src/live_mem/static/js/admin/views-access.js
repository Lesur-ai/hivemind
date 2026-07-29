/**
 * Access view (P8-5, issue #143) — token & space-access management.
 *
 * Contract: DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §4.4, §5.0, §5.7, §6.4,
 * §6.5, §7.1, §7.4, §8. Replaces the current Tokens page while preserving the
 * REAL Hivemind token model: `permissions` (read/write/manage/admin, inclusive)
 * plus a `space_ids` ALLOWLIST — never per-tier rights, never tenancy.
 *
 * Data honesty (D7/D8, §5.7): Access has two capability modes. Admin/bootstrap
 * keeps the historical global token CRUD and loads `admin_list_tokens` once.
 * A non-admin manager never probes an `admin_*` tool: it may only create an
 * initially unscoped token (`token_create`) and add it to one visible space
 * (`space_invite_token`) after a fresh `space_list` read.
 * The current-session badge and bootstrap panel read the cached `system_whoami`
 * (`ctx.identity`) — no request. The token last-used timestamp is NEVER shown
 * (D-lastused, §6.5): it is dead data server-side. There is no rotate backend
 * (D-rotate,
 * §6.4): rotation is documented as create → verify → revoke, never a control.
 *
 * Escaping (§7.3.3): every dynamic value passes through the shell `esc()` at its
 * interpolation site (R1); attribute values are quoted + esc()'d (R2); values
 * read back from `dataset` are re-escaped at the new sink (R3); server French
 * strings render verbatim via `serverMessage()` only (R4). The raw plaintext of
 * a newly created token is shown once, held only in a JS closure — never in a
 * `data-*` attribute, never in browser storage, never in a log (§7.1.6).
 *
 * All shell helpers below (esc, icon, pageHeader, panel, dataTable, pill,
 * copyable, monoBlock, stateEmpty/Error/Unavailable, serverMessage, fmtTimestamp,
 * showModal, closeModal, showDestructiveModal, showToast, registerAction,
 * AdminRouter, callTool, cache) are frozen shell globals from admin-app.js /
 * admin-api.js (§2.3.4, §3.3.1).
 */
(function () {
    'use strict';

    // The reserved embedded-long-runtime credential (§7.4.2). Mirrors
    // INTERNAL_LONG_TOKEN_NAME (src/live_mem/core/models.py:30).
    var INTERNAL_LONG = 'internal-long';

    // The four valid permission chains (inclusive model). Manager-mode token
    // creation deliberately slices off the admin preset; only the historical
    // admin_create_token flow may mint an admin credential.
    var PERMISSION_PRESETS = [
        { value: 'read', label: 'Read only' },
        { value: 'read,write', label: 'Read + Write' },
        { value: 'read,write,manage', label: 'Read + Write + Manage' },
        { value: 'read,write,manage,admin', label: 'Read + Write + Manage + Admin' },
    ];

    var ALLOWLIST_NOTE = 'Tokens can only access spaces listed in their allowlist.';
    // Documented consequence of the /api/tool write floor (§7.1.4).
    var READ_ONLY_NOTE =
        'Read-only tokens cannot use the admin console; use the /live viewer for read-only access.';

    // ─────────────── modal generation & staleness (Codex R2 finding 3) ───────────────
    // The console has a SINGLE shared #adminModal. A stale async continuation
    // that resolves after the operator dismissed its modal and opened a
    // different one must not manipulate (close/replace) that newer modal. Route
    // epoch alone cannot detect a same-view modal swap (opening a modal does not
    // navigate), so every modal this view opens bumps `_modalGen`; a continuation
    // is stale if either the route epoch OR the modal generation changed since it
    // opened its modal. All of this view's modal opens go through _openModal /
    // _openDestructive so the counter can never drift from reality.
    var _modalGen = 0;
    // The explicit header-level edit entry point selects from the latest token
    // list. Keep its freshness separate from the cache itself so an operator
    // cannot select a row left over from a previous render while a new list is
    // still loading.
    var _tokenListEpoch = -1;

    function _openModal(title, body, verb, onConfirm) {
        _modalGen += 1;
        showModal(title, body, verb, onConfirm);
    }

    function _openDestructive(opts) {
        _modalGen += 1;
        showDestructiveModal(opts);
    }

    // The live session-identity object from the shell (`_ctx()`): wipeSession()
    // rebinds it to a fresh {} and login rebinds it to a new whoami, so its
    // reference changes on any logout / expiry / re-login but is stable across
    // in-session navigation. Captured before an await; compared after.
    function _sessionIdentity() {
        return (typeof _ctx === 'function') ? _ctx().identity : null;
    }

    // A continuation belongs to a DEAD session if the login overlay is currently
    // showing (logged out now) or the identity reference changed (logout+re-login
    // or expiry). wipeSession() changes neither route epoch nor modal generation,
    // so this is the ONLY signal that a session wipe happened while a request was
    // in flight — every post-wipe modal/toast/refresh effect must be dropped so no
    // privileged state survives the wipe (§3.1.4, Codex R4).
    function _sessionEnded(sessionAtCall) {
        var overlay = document.getElementById('loginOverlay');
        if (overlay && !overlay.classList.contains('hidden')) return true;
        if (sessionAtCall === undefined) return false; // caller opted out of identity check
        return typeof _ctx === 'function' && _ctx().identity !== sessionAtCall;
    }

    // Full staleness: the route changed, a newer modal replaced this one, OR the
    // auth session ended/changed. Used by every awaiting modal/mutation
    // continuation so a stale response can never touch a newer modal or repaint a
    // dead session's state.
    function _isStale(epochAtCall, genAtCall, sessionAtCall) {
        return AdminRouter.epoch !== epochAtCall
            || _modalGen !== genAtCall
            || _sessionEnded(sessionAtCall);
    }

    // Session-aware copy for the ONE-TIME SECRET (Codex R5 f2 + Terra-R1 f1).
    // The shell _copyText() is fire-and-forget: its async Clipboard `.then`/
    // `.catch` would fire a "Copied" toast — or build the plaintext fallback
    // textarea — even after the operator dismissed the secret, navigated, opened
    // another modal, or the session was wiped. This takes the `holder` (not a
    // captured string) plus the epoch/generation/session at click time, and
    // gates EVERY completion effect on: (a) the secret still being live
    // (holder.value non-empty — destroySecret() zeroes it on dismiss), and
    // (b) full staleness (route + modal generation + session). The execCommand
    // fallback (§2.4.7) reads the live holder value and builds/removes its
    // textarea synchronously, only when neither guard trips.
    function _copySecret(holder, epochAtCopy, genAtCopy, sessionAtCopy, copiedLabel) {
        function stale() {
            return !holder.value || _isStale(epochAtCopy, genAtCopy, sessionAtCopy);
        }
        function finish(ok) {
            if (stale()) return; // dismissed / navigated / modal-swapped / wiped: no effect
            var successLabel = copiedLabel || 'Plaintext token copied';
            if (ok) { showToast('ok', successLabel); return; }
            var ta = document.createElement('textarea');
            ta.value = holder.value;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            var copied = false;
            try { copied = document.execCommand('copy'); } catch (e) { copied = false; }
            document.body.removeChild(ta);
            showToast(copied ? 'ok' : 'warn',
                copied ? successLabel : 'Copy failed — select the value and copy manually');
        }
        var value = holder.value;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(function () { finish(true); }).catch(function () { finish(false); });
        } else {
            finish(false);
        }
    }

    // Lock/unlock the shell modal's dismissal controls (× and Cancel). Making
    // the Create flow EXCLUSIVE and non-dismissible while the request is in
    // flight (Terra-R1 f2) is what lets `created` safely surface the one-time
    // secret without ever replacing newer UI: the confirm button is already
    // disabled by the shell, the modal overlay blocks clicks on the content
    // behind it, and with dismissal disabled the operator cannot close this
    // modal to open another — so no newer modal can exist when the secret
    // arrives. Never-orphan is preserved (the response is shown, never dropped).
    function _setModalDismissible(on) {
        var controls = document.querySelectorAll('#adminModal [data-action="close-modal"]');
        for (var i = 0; i < controls.length; i++) {
            controls[i].disabled = !on;
            controls[i].style.pointerEvents = on ? '' : 'none';
            if (on) controls[i].removeAttribute('aria-disabled');
            else controls[i].setAttribute('aria-disabled', 'true');
        }
    }

    // Navigation lock for the exclusive create/secret flow (Terra-R2 f2).
    // Disabling the modal's ×/Cancel does NOT stop browser Back/Forward or
    // address-bar hash edits — the router would dispatch those, then a late
    // `created` would open the plaintext secret over the new route. While a lock
    // is held, any hash change is reverted to the locked route, so the one-time
    // secret is always delivered in the context that requested it (never over a
    // new route, never orphaned). The lock is an OWNERSHIP TOKEN: only its
    // holder releases it, so a stale cross-session continuation can never
    // release a newer request's lock (companion to the Terra-R2 f1 ordering).
    //
    // Terra-R3: the lock also captures the SESSION that owns it. The frozen
    // shell's wipeSession() removes the create/secret modal WITHOUT running its
    // teardown, so the owning continuation may never release this view-local
    // lock (e.g. a 401 expiry mid-secret). Keying the revert on the captured
    // session makes a stale lock self-heal: the first navigation in any other
    // session (logged out, or a different logged-in session after re-login)
    // drops it instead of pinning that session on the dead flow's route.
    var _navLock = null;

    function _navLockAcquire() {
        var lock = { hash: location.hash, session: _sessionIdentity() };
        _navLock = lock;
        return lock;
    }

    function _navLockRelease(lock) {
        if (_navLock === lock) _navLock = null;
    }

    // The lock's owning session is current iff we are logged in (no overlay) AND
    // the live identity reference still matches the one captured at acquire time.
    function _navLockSessionCurrent(lock) {
        var overlay = document.getElementById('loginOverlay');
        if (overlay && !overlay.classList.contains('hidden')) return false;
        return _sessionIdentity() === lock.session;
    }

    window.addEventListener('hashchange', function () {
        if (!_navLock || location.hash === _navLock.hash) return;
        if (!_navLockSessionCurrent(_navLock)) {
            // Orphaned by a session wipe: drop it and let navigation proceed —
            // never pin a dead flow's route onto a new (or logged-out) session.
            _navLock = null;
            return;
        }
        location.hash = _navLock.hash; // revert; the shell re-dispatches back
    });

    // ─────────────────────────── small helpers ───────────────────────────

    function isAdmin(identity) {
        return !!(identity && Array.isArray(identity.permissions) &&
            identity.permissions.indexOf('admin') !== -1);
    }

    function isBootstrap(identity) {
        return !!(identity && identity.auth_type === 'bootstrap');
    }

    function hasManage(identity) {
        if (!identity || !Array.isArray(identity.permissions)) return false;
        return isBootstrap(identity) || identity.permissions.indexOf('manage') !== -1 || isAdmin(identity);
    }

    function hasGlobalAdmin(identity) {
        return isBootstrap(identity) || isAdmin(identity);
    }

    function clearAdminTokenCache() {
        if (typeof cache !== 'undefined' && cache) {
            cache.tokens = [];
            cache.spaces = [];
        }
        _tokenListEpoch = -1;
    }

    function dropKnownLocalPrivileges(identity) {
        if (!identity || identity.auth_type === 'bootstrap') return;
        identity.permissions = [];
        clearAdminTokenCache();
        if (typeof renderIdentityBlock === 'function') renderIdentityBlock(identity);
    }

    // Fail closed at the action boundary as well as at render time. This keeps
    // stale/forged data-action elements from turning manager mode into an
    // `admin_*` probe, and purges any token payload cached by a prior admin view.
    function requireGlobalAdmin() {
        if (hasGlobalAdmin(_sessionIdentity())) return true;
        clearAdminTokenCache();
        showToast('error', 'This action requires admin permission.');
        return false;
    }

    // Client-side "expired" derivation of the real `expires_at` field (§5.7,
    // Appendix B). Returns true only for a parseable past ISO date.
    function isExpired(expiresAt) {
        if (!expiresAt) return false;
        var t = Date.parse(expiresAt);
        return !Number.isNaN(t) && t < Date.now();
    }

    // Full hash displayed as `sha256:` + first 16 hex, with a copy-full-hash
    // affordance (§5.7). 16 hex is the server's minimum mutation prefix, so the
    // visible fragment is always a valid target; mutations always send the full
    // hash. copyable(full, shown) escapes both and stores the full value in a
    // JSON-encoded, esc()'d data-value attribute (shell helper).
    function hashCell(fullHash) {
        var h = String(fullHash || '');
        var body = h.indexOf('sha256:') === 0 ? h.slice('sha256:'.length) : h;
        var shown = 'sha256:' + body.slice(0, 16);
        return copyable(h, shown);
    }

    function permsChips(perms) {
        if (!Array.isArray(perms) || !perms.length) {
            return '<span class="text-faint">—</span>';
        }
        return perms.map(function (p) {
            return '<span class="chip">' + esc(String(p).toUpperCase()) + '</span>';
        }).join(' ');
    }

    // Space allowlist cell. Keep dense rows on one line: the complete list is
    // available in the overflow tooltip and in Edit, without forcing a tall row.
    function spacesCell(token) {
        if (isAdminToken(token)) {
            return '<span class="access-all">all spaces (admin)</span>';
        }
        var ids = Array.isArray(token.space_ids) ? token.space_ids : [];
        if (!ids.length) {
            return '<span class="text-faint">— no space access</span>';
        }
        var visible = ids.slice(0, 3);
        var hidden = ids.slice(3);
        var overflow = hidden.length
            ? '<span class="chip chip-space chip-overflow" title="' +
              esc(hidden.map(String).join(', ')) + '">+' + hidden.length + ' more</span>'
            : '';
        return '<span class="space-chips">' + visible.map(function (sid) {
            return '<span class="chip chip-space">' + esc(String(sid)) + '</span>';
        }).join(' ') + overflow + '</span>';
    }

    function isAdminToken(token) {
        return Array.isArray(token.permissions) &&
            token.permissions.indexOf('admin') !== -1;
    }

    function isInternalLong(token) {
        return token && token.name === INTERNAL_LONG;
    }

    function statusPill(token) {
        if (token.revoked) return pill('neutral', 'Revoked');
        if (isExpired(token.expires_at)) return pill('warn', 'Expired');
        return pill('ok', 'Active');
    }

    // Expiry sub-line under the status pill (real field only; "never" when null).
    function expiryLine(token) {
        if (!token.expires_at) {
            return '<div class="cell-sub">never expires</div>';
        }
        var t = fmtTimestamp(token.expires_at);
        var word = isExpired(token.expires_at) ? 'expired' : 'expires';
        return '<div class="cell-sub" title="' + esc(t.title) + '">' +
            esc(word + ' ' + t.text) + ' <span class="unit-utc">UTC</span></div>';
    }

    // ─────────────────────────── render ───────────────────────────

    function render(contentEl, params, ctx) {
        var identity = (ctx && ctx.identity) || {};
        var epochAtRender = ctx ? ctx.epoch : (AdminRouter ? AdminRouter.epoch : 0);

        if (!hasManage(identity)) {
            clearAdminTokenCache();
            contentEl.innerHTML =
                '<div class="page">' +
                pageHeader('Access') +
                panel(
                    stateUnavailable('Requires manage permission to create tokens or grant space access.') +
                    accessHelp(false)
                ) +
                '</div>';
            return;
        }

        // A manager gets only the additive, scoped delegation surface. Do not
        // retain or list global token metadata from a previous admin session.
        if (!hasGlobalAdmin(identity)) {
            clearAdminTokenCache();
            contentEl.innerHTML =
                '<div class="page">' +
                pageHeader('Access', managerHeaderActions()) +
                '<div id="accessBanners"></div>' +
                '<div id="accessMsg"></div>' +
                panel(
                    '<div class="panel-header"><div class="panel-title"><h2>Scoped delegation</h2></div></div>' +
                    '<p>Create a token with no space access, then invite its full hash to one of your spaces.</p>' +
                    '<p class="text-faint">Managers cannot list, edit, revoke, delete, or purge tokens.</p>'
                ) +
                accessHelp(false) +
                '</div>';
            renderBootstrapBanner(identity);
            return;
        }

        _tokenListEpoch = -1;
        contentEl.innerHTML =
            '<div class="page">' +
            pageHeader('Access', adminHeaderActions()) +
            '<div id="accessBanners"></div>' +
            '<div id="accessMsg"></div>' +
            panel(
                '<div class="panel-header">' +
                '<div class="panel-title"><h2>Tokens</h2><span id="accessCount" class="count-pill">…</span></div>' +
                '<div class="panel-header-actions">' +
                '<button class="btn btn-secondary btn-sm" data-action="access-purge" data-mode="revoked">Purge revoked…</button>' +
                '<button class="btn btn-danger btn-sm" data-action="access-purge" data-mode="all">Purge all…</button>' +
                '</div>' +
                '</div>' +
                '<div id="accessTable">' + stateLoading('Loading tokens…') + '</div>'
            ) +
            accessHelp(true) +
            '</div>';

        renderBootstrapBanner(identity);
        loadTokens(contentEl, ctx, epochAtRender);
    }

    function adminHeaderActions() {
        return '' +
            '<button class="btn btn-secondary" data-action="access-refresh">' +
            icon('refresh') + '<span>Refresh</span></button>' +
            '<button class="btn btn-secondary" data-action="access-open-edit">' +
            icon('access') + '<span>Edit token</span></button>' +
            '<button class="btn btn-primary" data-action="access-create">' +
            icon('plus') + '<span>Create token</span></button>';
    }

    function managerHeaderActions() {
        return '' +
            '<button class="btn btn-primary" data-action="access-create">' +
            icon('plus') + '<span>Create token</span></button>' +
            '<button class="btn btn-secondary" data-action="access-invite">' +
            icon('access') + '<span>Invite token</span></button>';
    }

    // Concise operator guidance. Destructive purge dialogs still carry their
    // dedicated safety warning, where the information is actionable.
    function accessHelp() {
        return '' +
            '<div class="access-help">' +
            '<p>' + esc(ALLOWLIST_NOTE) + '</p>' +
            '<p>' + esc(READ_ONLY_NOTE) + '</p>' +
            '</div>';
    }

    function renderBootstrapBanner(identity) {
        var el = document.getElementById('accessBanners');
        if (!el) return;
        var html = '';
        // Bootstrap sessions (auth_type "bootstrap", no token_hash) do not appear
        // in the token list and cannot be revoked here (§5.7).
        if (identity.auth_type === 'bootstrap' || !identity.token_hash) {
            html += '<div class="access-banner">' + icon('shield') +
                '<span>You are using the bootstrap key — it does not appear ' +
                'in this list and cannot be revoked here.</span></div>';
        }
        el.innerHTML = html;
    }

    async function loadTokens(contentEl, ctx, epochAtRender) {
        var sessionAtRender = _sessionIdentity();
        if (!hasGlobalAdmin(sessionAtRender)) {
            clearAdminTokenCache();
            return;
        }
        var res;
        try {
            res = await callTool('admin_list_tokens', { include_revoked: true });
        } catch (e) {
            res = { status: 'error' };
        }
        // Drop the response only if the operator navigated away (§3.3.2 rule 3) OR
        // the session was wiped (§3.1.4 — rendering the prior session's list
        // behind the login overlay). Do NOT drop on a modal opening: the token
        // table is content that a modal merely overlays, so opening a modal while
        // the list loads must not permanently discard it (Codex R5) — the modal
        // generation is deliberately NOT part of this content-load guard.
        if (AdminRouter.epoch !== epochAtRender || _sessionEnded(sessionAtRender) ||
            !hasGlobalAdmin(_sessionIdentity())) {
            clearAdminTokenCache();
            return;
        }

        var tableEl = document.getElementById('accessTable');
        var countEl = document.getElementById('accessCount');
        if (!tableEl) return;

        if (!res || res.status !== 'ok') {
            clearAdminTokenCache();
            tableEl.innerHTML = renderNonOk(res);
            if (countEl) countEl.textContent = '—';
            return;
        }

        var tokens = (Array.isArray(res.tokens) ? res.tokens : []).filter(function (token) {
            return !isInternalLong(token);
        });
        cache.tokens = tokens;
        _tokenListEpoch = epochAtRender;
        if (countEl) countEl.textContent = String(tokens.length);

        if (!tokens.length) {
            tableEl.innerHTML = stateEmpty({
                title: 'No tokens — the bootstrap key still works',
                hint: 'Create an agent token to grant scoped access to spaces.',
            });
            return;
        }

        var identity = (ctx && ctx.identity) || {};
        var headers = ['Name', 'Hash', 'Status', 'Permissions', 'Space allowlist', 'Email / owner', ''];
        var rows = tokens.map(function (t) { return renderRow(t, identity); }).join('');
        tableEl.innerHTML = dataTable(headers, rows);
    }

    // §5.0 status handling: sentinels + error render honest states; server text
    // is passed through verbatim, never parsed.
    function renderNonOk(res) {
        res = res || {};
        if (res.status === 'read_only' || res.status === 'rate_limited' ||
            res.status === 'truncated') {
            return stateUnavailable(res.message || 'Not available.');
        }
        return stateError({ title: 'Could not load tokens', message: res.message });
    }

    function renderRow(t, identity) {
        var internal = isInternalLong(t);
        var current = identity && identity.token_hash && t.hash === identity.token_hash;
        var rowClass = t.revoked ? ' class="row-muted"' : '';

        var nameCell = '<div class="token-name">' + esc(String(t.name || '')) + '</div>';
        if (internal) {
            nameCell += '<span class="badge-reserved">' + icon('shield') +
                'System — embedded long runtime</span>';
        }
        if (current) {
            nameCell += '<span class="chip chip-current">current session</span>';
        }

        var adminCell = isAdminToken(t) ? pill('warn', 'admin') : '<span class="text-faint">—</span>';

        // Row actions. Edit is absent for internal-long (never widen/narrow the
        // reserved token, §7.4.2). Revoke is hidden once already revoked. Delete
        // is offered for revoked rows only.
        var actions = '';
        if (!internal && !t.revoked) {
            actions += '<button class="btn btn-secondary btn-sm" data-action="access-edit"' +
                ' data-hash="' + esc(t.hash) + '"' +
                ' data-name="' + esc(String(t.name || '')) + '"' +
                ' data-perms="' + esc((t.permissions || []).join(',')) + '"' +
                ' data-spaces="' + esc((t.space_ids || []).join(',')) + '"' +
                ' data-email="' + esc(String(t.email || '')) + '">Edit</button>';
        }
        if (!t.revoked) {
            actions += '<button class="btn btn-danger btn-sm" data-action="access-revoke"' +
                ' data-hash="' + esc(t.hash) + '"' +
                ' data-name="' + esc(String(t.name || '')) + '"' +
                ' data-internal="' + (internal ? '1' : '0') + '">Revoke</button>';
        }
        if (t.revoked) {
            actions += '<button class="btn btn-danger btn-sm" data-action="access-delete"' +
                ' data-hash="' + esc(t.hash) + '"' +
                ' data-name="' + esc(String(t.name || '')) + '">Delete</button>';
        }

        return '<tr' + rowClass + '>' +
            '<td>' + nameCell + '</td>' +
            '<td>' + hashCell(t.hash) + '</td>' +
            '<td>' + statusPill(t) + expiryLine(t) + '</td>' +
            '<td>' + permsChips(t.permissions) + '</td>' +
            '<td>' + spacesCell(t) + '</td>' +
            '<td>' + (t.email ? esc(String(t.email)) : '<span class="text-faint">—</span>') + '</td>' +
            '<td class="cell-actions">' + actions + '</td>' +
            '</tr>';
    }

    // ─────────────────────────── create ───────────────────────────

    function openCreateModal() {
        var identityAtOpen = _sessionIdentity();
        if (!hasManage(identityAtOpen)) {
            clearAdminTokenCache();
            showToast('error', 'This action requires manage permission.');
            return;
        }
        var adminMode = hasGlobalAdmin(identityAtOpen);
        var presets = adminMode ? PERMISSION_PRESETS : PERMISSION_PRESETS.slice(0, 3);
        var permOptions = presets.map(function (p, i) {
            return '<option value="' + esc(p.value) + '"' + (i === 1 ? ' selected' : '') +
                '>' + esc(p.label) + '</option>';
        }).join('');

        var spacesField = adminMode ?
            '<div class="form-group">' +
            '<label class="form-label" for="ctSpaces">Space allowlist</label>' +
            '<input class="form-input mono" id="ctSpaces" autocomplete="off" placeholder="space-a, space-b">' +
            '<div class="form-hint" id="ctSpacesHint">Empty grants no space access. Use ' +
            '<code>*</code> or <code>all</code> to grant access to all existing spaces. ' +
            'New spaces are not added automatically. ' +
            'Admin profiles are global and persist an empty allowlist.</div>' +
            '</div>' :
            '<div class="access-banner">New tokens start with no space access. ' +
            'Save the Token ID, then use <strong>Invite token</strong> to grant one of your spaces.</div>';

        var body =
            '<div class="form-group">' +
            '<label class="form-label" for="ctName">Name <span class="req">*</span></label>' +
            '<input class="form-input mono" id="ctName" autocomplete="off" data-1p-ignore data-lpignore="true" placeholder="agent-cline">' +
            '<div class="form-error" id="ctNameErr" hidden></div>' +
            '<div class="form-hint" id="ctNameHint">Names are labels, not identifiers — they need not be unique.</div>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label" for="ctPerms">Permissions <span class="req">*</span></label>' +
            '<select class="form-input" id="ctPerms">' + permOptions + '</select>' +
            '<div class="form-hint">Inclusive model: each preset includes the ones before it.</div>' +
            '</div>' +
            spacesField +
            '<div class="form-group">' +
            '<label class="form-label" for="ctExpires">Expires in days</label>' +
            '<input class="form-input mono" id="ctExpires" type="number" min="0" value="0">' +
            '<div class="form-hint">0 = never expires.</div>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label" for="ctEmail">Email / owner</label>' +
            '<input class="form-input" id="ctEmail" type="email" autocomplete="off" placeholder="optional">' +
            '</div>' +
            // Timer-free recovery for an indefinitely pending request (Terra-R3):
            // callTool has no abort/timeout and timer-based coordination is
            // banned (§3.3.2 r5), so a stalled create could otherwise pin this modal and
            // navigation until a full reload. This escape is hidden until the
            // request is in flight; it lets the operator stop waiting WITHOUT
            // abandoning the live continuation at the network layer.
            '<div class="pending-escape" id="ctPendingEscape" hidden>' +
            '<span class="form-hint">Taking longer than expected?</span>' +
            '<button type="button" class="btn btn-ghost btn-sm" id="ctStopWaiting">Stop waiting</button>' +
            '</div>';

        _openModal('Create token', body, 'Create token', function () {
            return onCreateConfirm(adminMode, identityAtOpen);
        });

        // Token-store v2 forbids dormant admin allowlists. Keep any typed value
        // in the DOM when toggling presets, but disable and omit it while the
        // selected target profile contains admin.
        var permissionSelect = document.getElementById('ctPerms');
        var scopeInput = document.getElementById('ctSpaces');
        var scopeHint = document.getElementById('ctSpacesHint');
        function syncAdminScopeInvariant() {
            if (!permissionSelect || !scopeInput) return;
            var targetAdmin = permissionSelect.value.split(',').indexOf('admin') !== -1;
            scopeInput.disabled = targetAdmin;
            scopeInput.setAttribute('aria-disabled', targetAdmin ? 'true' : 'false');
            if (scopeHint) {
                scopeHint.textContent = targetAdmin
                    ? 'Admin access is global. V2 stores space_ids: [] so a later downgrade cannot activate a dormant allowlist.'
                    : 'Empty grants no space access. Use * or all to grant access to all existing spaces. New spaces are not added automatically.';
            }
        }
        if (permissionSelect && scopeInput) {
            permissionSelect.addEventListener('change', syncAdminScopeInvariant);
            syncAdminScopeInvariant();
        }

        // Live warning if the operator types the reserved name (not blocked —
        // the server has no reserved-name guard; the embedded runtime auto-
        // revokes collisions via the never-orphan rule, §B3.8).
        var nameInput = document.getElementById('ctName');
        var hint = document.getElementById('ctNameHint');
        if (nameInput && hint) {
            nameInput.addEventListener('input', function () {
                if (nameInput.value.trim() === INTERNAL_LONG) {
                    hint.textContent = adminMode
                        ? 'Reserved name: internal-long is the embedded long runtime credential. ' +
                          'Creating it here is not recommended — the runtime will rotate a colliding token out.'
                        : 'Reserved name: managers cannot create the internal-long runtime credential.';
                    hint.classList.add('form-warn');
                } else {
                    hint.textContent = 'Names are labels, not identifiers — they need not be unique.';
                    hint.classList.remove('form-warn');
                }
            });
        }
    }

    async function onCreateConfirm(adminMode, identityAtOpen) {
        var nameEl = document.getElementById('ctName');
        var name = (nameEl ? nameEl.value : '').trim();
        var errEl = document.getElementById('ctNameErr');
        if (!name) {
            if (errEl) { errEl.textContent = 'Name is required.'; errEl.hidden = false; }
            return false;
        }
        if (errEl) errEl.hidden = true;

        if (_sessionEnded(identityAtOpen) || !hasManage(_sessionIdentity()) ||
            (adminMode && !hasGlobalAdmin(_sessionIdentity()))) {
            clearAdminTokenCache();
            if (errEl) {
                errEl.textContent = 'Your session permissions changed. Reopen this form.';
                errEl.hidden = false;
            }
            return false;
        }
        if (!adminMode && name === INTERNAL_LONG) {
            if (errEl) {
                errEl.textContent = 'The reserved internal-long name cannot be created by a manager.';
                errEl.hidden = false;
            }
            return false;
        }

        var selectedPermissions = document.getElementById('ctPerms').value;
        var targetIsAdmin = selectedPermissions.split(',').indexOf('admin') !== -1;
        var safePresetValues = PERMISSION_PRESETS.slice(0, 3).map(function (p) { return p.value; });
        if (!adminMode && safePresetValues.indexOf(selectedPermissions) === -1) {
            if (errEl) {
                errEl.textContent = 'Managers may create only read, read+write, or read+write+manage tokens.';
                errEl.hidden = false;
            }
            return false;
        }
        var args = {
            name: name,
            permissions: selectedPermissions,
        };
        var spacesEl = document.getElementById('ctSpaces');
        var spaces = spacesEl ? spacesEl.value.trim() : '';
        if (adminMode && spaces && !targetIsAdmin) args.space_ids = spaces;
        var days = parseInt(document.getElementById('ctExpires').value, 10);
        if (Number.isFinite(days) && days > 0) args.expires_in_days = days;
        var email = document.getElementById('ctEmail').value.trim();
        if (email) args.email = email;

        var genAtCall = _modalGen;
        // Captured to detect a session change while the create request is in
        // flight (see _sessionEnded / _sessionIdentity).
        var sessionAtCall = _sessionIdentity();
        // Route epoch matters only AFTER the operator stops waiting (below): the
        // pending nav+modal locks otherwise pin the route, so while they hold it
        // cannot change. Once released via the escape it can, so the abandoned
        // path uses full staleness (epoch+gen+session) to stay in-context.
        var epochAtCall = AdminRouter.epoch;

        // Terra-R1 f2: make the create modal an EXCLUSIVE, non-dismissible
        // pending flow while the request is in flight. With dismissal disabled
        // (and the shell already disabling the confirm button + the overlay
        // blocking content clicks) no newer modal can be opened before the
        // response, so a `created` response can never replace newer UI.
        // Terra-R2 f2: disabling the modal's ×/Cancel does NOT stop Back/Forward
        // or address-bar hash edits, which the shell router WOULD dispatch — so
        // also take the navigation lock, pinning the route until the handoff
        // completes. The lock is an ownership TOKEN so a stale cross-session
        // continuation can only ever release its OWN lock (never a newer one).
        var navLock = _navLockAcquire();
        _setModalDismissible(false);

        // Terra-R3: bound recovery for an indefinitely pending request. callTool
        // cannot be aborted and timer-based coordination is banned (§3.3.2 r5), so
        // without this a stalled create pins the modal + navigation until reload.
        // The "Stop waiting" escape (revealed only now, while in flight) releases
        // both locks and re-enables dismissal WITHOUT abandoning the live promise:
        // the continuation below still delivers a `created` secret if it arrives
        // IN-CONTEXT, and only drops it (with the operator forewarned) if they
        // have since navigated or opened another modal. It is NOT auto-dismissal
        // (×/Cancel stay disabled — R1 exclusivity holds) and NOT a client
        // timeout that could orphan a secret a moment's patience would deliver.
        var abandoned = false;
        var escape = document.getElementById('ctPendingEscape');
        if (escape) escape.hidden = false;
        var stopBtn = document.getElementById('ctStopWaiting');
        if (stopBtn) {
            // Assigned (not addEventListener) so a retry-after-error on the SAME
            // modal replaces this invocation's handler rather than stacking one.
            stopBtn.onclick = function () {
                if (abandoned) return;
                abandoned = true;
                _navLockRelease(navLock);   // free navigation
                _setModalDismissible(true); // let the operator close / retry
                if (escape) escape.hidden = true;
                var e = document.getElementById('ctNameErr');
                if (e) {
                    e.textContent = 'Stopped waiting. Stay on this dialog to still ' +
                        'receive any returned credential, including an uncertain partial result. ' +
                        'If you leave, an unrecoverable token may remain — ask an admin to inspect it.';
                    e.hidden = false;
                }
            };
        }

        var res;
        try {
            res = adminMode
                ? await callTool('admin_create_token', args)
                : await callTool('token_create', args);
        } catch (e) {
            res = { status: 'error' };
        }

        // Codex R1/R2/R3 finding 1 (HIGH): a returned credential must NEVER be
        // orphaned across NAVIGATION (the server already persisted the token and
        // returned its one-time plaintext), but it must ALSO NOT repaint
        // privileged DOM once the SESSION has ended OR CHANGED. Two orderings:
        //  - resolves while logged out → the login overlay is visible (§3.1.4);
        //  - resolves after logout+re-login → the overlay is hidden again, but in
        //    a DIFFERENT session — the identity reference has changed, so showing
        //    the prior session's secret would be a cross-session leak.
        // Route changes alone change neither signal, so the secret still shows.
        var returnedCredential = res && res.token &&
            (res.status === 'created' ||
             (res.status === 'partial' && res.recovery_required === true && res.token_hash));
        if (returnedCredential) {
            if (abandoned) {
                // Locks were released and the modal is dismissible again, so
                // deliver the secret ONLY while the create dialog is still open
                // in-context. Terra-R4: a ×/Cancel dismissal only HIDES the modal
                // (closeModal sets display:none) — it changes neither epoch nor
                // _modalGen — so the staleness gate alone would let a late
                // credential response REOPEN the secret after the operator explicitly
                // closed the dialog, exposing a one-time token the warning said
                // was unrecoverable. Gate on live modal visibility too: any hide
                // (dismiss), route change, modal swap, or session change drops it.
                var modalEl = document.getElementById('adminModal');
                var dialogOpen = modalEl && modalEl.style.display !== 'none';
                if (!dialogOpen || _isStale(epochAtCall, genAtCall, sessionAtCall)) return false;
            } else if (_sessionEnded(sessionAtCall)) {
                // Show the secret on success unless the SESSION ended/changed
                // (logout / expiry / re-login) — route changes alone still show it
                // (never-orphan). _sessionEnded covers both the "logged out now"
                // and "logged out+back in" orderings.
                _navLockRelease(navLock);
                return false; // never recreate secret DOM in a dead/other session
            }
            _navLockRelease(navLock);  // end the pending-phase lock…
            showTokenSecret(res);      // …the secret step takes its own nav lock
            return false;              // the secret step owns the modal + navigation now
        }

        // Non-created (error / validation). If the operator stopped waiting they
        // already have the recovery notice — drop silently. Otherwise Terra-R2
        // f1: check OWNERSHIP before mutating the modal — a stale cross-session
        // continuation must never re-enable a NEWER session's still-locked modal.
        // Under the modal + nav locks the only way we can lose the modal is a
        // session change: gen is held (no new modal can open while dismissal is
        // disabled AND navigation is pinned), so `_modalGen` advancing or
        // `_sessionEnded` both mean we no longer own this modal — bail without
        // touching it. Epoch is deliberately excluded: the nav lock's hash-revert
        // churns it (Back → shell dispatch → revert → shell dispatch) while the
        // create modal legitimately remains ours, so keying on epoch here would
        // strand a still-owned modal in its locked state.
        if (abandoned) { _navLockRelease(navLock); return false; }
        if (_modalGen !== genAtCall || _sessionEnded(sessionAtCall)) { _navLockRelease(navLock); return false; }
        _setModalDismissible(true);
        _navLockRelease(navLock); // create flow is over; free navigation for retry/cancel
        showCreateError(res);
        return false;
    }

    function showCreateError(res) {
        var errEl = document.getElementById('ctNameErr');
        if (errEl) {
            errEl.textContent = (res && res.message) ? String(res.message) : 'The server refused or failed this operation.';
            errEl.hidden = false;
        }
    }

    // One-time secret display (§7.1.6). The raw token lives ONLY in the `holder`
    // object below — never a data-* attribute, browser storage, or a log. It is
    // destroyed — the DOM node emptied AND the closure value zeroed so the Copy
    // button can no longer recover it — on EVERY exit path (acknowledge, Cancel,
    // the × close), which closes the teardown gaps of Codex R1 finding 2 and is
    // stronger than the §7.1.6/B3.7 accepted-residual floor (no shell change).
    function showTokenSecret(res) {
        // The secret display takes its OWN nav lock (Terra-R2 f2 / R3): while the
        // one-time plaintext is on screen, Back/Forward and hash edits are pinned
        // to this route so the secret can never be left rendered over a route the
        // operator navigated to. It is a fresh lock (not the create's), so the
        // display is protected even when the create's pending lock was released
        // early by the "Stop waiting" escape. destroySecret() releases it on
        // every teardown path; a session wipe self-heals it via _navLockAcquire's
        // captured session (the frozen wipeSession removes the modal without
        // running destroySecret).
        var navLock = _navLockAcquire();
        var holder = { value: String(res.token || '') };
        var hashHolder = { value: String(res.token_hash || '') };
        var extra = '';
        var uncertain = res.status === 'partial' && res.recovery_required === true;
        if (uncertain) {
            extra += '<div class="destructive-note">' + icon('alert') +
                '<span><strong>Creation state is uncertain.</strong> Do not discard either value. ' +
                'Do not assume the token is active or absent. Ask an admin to inspect and validate ' +
                'the token registry before retrying, revoking, or granting access.</span></div>';
            if (res.message) extra += serverMessage(res.message);
        }
        if (!uncertain && res.warning_no_access) {
            extra += '<div class="access-banner"><span><strong>No space access yet.</strong> ' +
                'A manager can grant access with the Token ID below.</span></div>';
        }
        if (!uncertain && res.snapshot_taken) {
            var grantedCount = Array.isArray(res.space_ids) ? res.space_ids.length : 0;
            extra += '<div class="access-banner"><span><strong>Access granted to ' +
                esc(String(grantedCount)) + ' existing space' + (grantedCount === 1 ? '' : 's') + '.</strong> ' +
                'New spaces will not be added automatically.</span></div>';
        }
        if (!uncertain && res.scope_normalized) {
            extra += '<div class="access-banner"><span>Admin access is global; the space allowlist was ignored.</span></div>';
        }

        var hashBlock = hashHolder.value
            ? '<p class="micro-label">Token ID (full hash)</p>' +
              '<p class="form-hint">A manager needs this ID to grant the token access to a space. It is not the secret token.</p>' +
              '<div class="mono-block secret" id="ctTokenHash">' + esc(hashHolder.value) + '</div>' +
              '<div class="secret-actions">' +
              '<button type="button" class="btn btn-secondary btn-sm" id="ctCopyHashBtn">' +
              icon('copy') + '<span>Copy Token ID</span></button></div>'
            : '<p class="form-hint">The Token ID will be visible in the admin token list after this dialog.</p>';

        var body =
            '<p class="secret-warning">' + icon('alert') +
            ' Copy the plaintext token now — it is shown once and can never be retrieved again.</p>' +
            '<p class="micro-label">Plaintext token</p>' +
            '<div class="mono-block secret" id="ctSecret">' + esc(holder.value) + '</div>' +
            '<div class="secret-actions">' +
            '<button type="button" class="btn btn-secondary btn-sm" id="ctCopyBtn">' +
            icon('copy') + '<span>Copy plaintext</span></button>' +
            '</div>' +
            hashBlock +
            '<div class="token-meta">' +
            '<span class="micro-label">Name</span> ' + esc(String(res.name || '')) + ' · ' +
            '<span class="micro-label">Permissions</span> ' + permsChips(res.permissions) +
            '</div>' +
            extra;

        // Zero the plaintext everywhere the UI could recover it from, and
        // release the navigation lock (Terra-R2 f2): the one-time-secret handoff
        // is complete, so the route is free again. Runs on EVERY exit path
        // (acknowledge, Cancel, ×), so navigation is never left pinned. The
        // release is owner-scoped, so if a newer flow has since taken the lock
        // this is a harmless no-op.
        function destroySecret() {
            holder.value = '';
            hashHolder.value = '';
            var secret = document.getElementById('ctSecret');
            if (secret) secret.textContent = '';
            var tokenHash = document.getElementById('ctTokenHash');
            if (tokenHash) tokenHash.textContent = '';
            var btn = document.getElementById('ctCopyBtn');
            if (btn) btn.disabled = true;
            var hashBtn = document.getElementById('ctCopyHashBtn');
            if (hashBtn) hashBtn.disabled = true;
            _navLockRelease(navLock);
        }

        _openModal(
            uncertain ? 'Token creation uncertain — preserve both values' : 'Token created — save it now',
            body,
            uncertain ? 'I saved both values' : 'I have saved it',
            async function () {
            destroySecret();
            AdminRouter.refresh();
            return true;
            }
        );

        // The shell's Cancel and × controls only hide the modal (closeModal sets
        // display:none), leaving its DOM — and this closure's Copy listener — in
        // place until the next modal replaces it. Attach the teardown to both so
        // dismissing also destroys the plaintext. The element-level listener
        // fires before the document-level [data-action="close-modal"] delegation.
        var modalEl = document.getElementById('adminModal');
        if (modalEl) {
            modalEl.querySelectorAll('[data-action="close-modal"]').forEach(function (c) {
                c.addEventListener('click', destroySecret);
            });
        }

        var copyBtn = document.getElementById('ctCopyBtn');
        if (copyBtn) {
            copyBtn.addEventListener('click', function () {
                if (!holder.value) { showToast('warn', 'Token already cleared — create a new one'); return; }
                // Session-aware copy: async Clipboard API first, then a
                // synchronously-removed hidden-textarea execCommand fallback for
                // non-secure / LAN contexts (§2.4.7, Codex R1 finding 4). Every
                // completion effect is gated on the live holder value AND the
                // route/modal/session captured now, so a post-dismissal,
                // post-navigation, post-modal-swap, or post-wipe fulfilment
                // cannot toast or rebuild the plaintext (Codex R5 f2 + Terra f1).
                _copySecret(holder, AdminRouter.epoch, _modalGen, _sessionIdentity());
            });
        }

        var copyHashBtn = document.getElementById('ctCopyHashBtn');
        if (copyHashBtn) {
            copyHashBtn.addEventListener('click', function () {
                if (!hashHolder.value) { showToast('warn', 'Token ID is no longer available in this dialog'); return; }
                _copySecret(hashHolder, AdminRouter.epoch, _modalGen, _sessionIdentity(), 'Token ID copied');
            });
        }
    }

    // ─────────────────────────── manager invite ───────────────────────────

    async function openInviteModal() {
        var sessionAtList = _sessionIdentity();
        if (!hasManage(sessionAtList) || hasGlobalAdmin(sessionAtList)) {
            if (!hasGlobalAdmin(sessionAtList)) clearAdminTokenCache();
            showToast('error', 'Scoped invitation is available to manager tokens.');
            return;
        }

        // Deliberately bypass the shared spaces cache: an invitation must be
        // chosen from a fresh authoritative visibility read, never prior state.
        var epochAtList = AdminRouter.epoch;
        var genAtList = _modalGen;
        var res;
        try { res = await callTool('space_list', {}); } catch (e) { res = { status: 'error' }; }
        if (_isStale(epochAtList, genAtList, sessionAtList)) return;
        if (!hasManage(_sessionIdentity()) || hasGlobalAdmin(_sessionIdentity())) {
            clearAdminTokenCache();
            return;
        }
        if (!res || res.status !== 'ok') {
            _openModal(
                'Invite token',
                stateError({ title: 'Could not load your spaces', message: res && res.message }),
            );
            return;
        }

        var spaces = (Array.isArray(res.spaces) ? res.spaces : [])
            .map(function (s) { return s && s.space_id ? String(s.space_id) : ''; })
            .filter(function (sid) { return sid; });
        if (!spaces.length) {
            _openModal(
                'Invite token',
                stateEmpty({ title: 'No visible space', hint: 'Create a space first, then retry the invitation.' }),
            );
            return;
        }

        var options = spaces.map(function (sid) {
            return '<option value="' + esc(sid) + '">' + esc(sid) + '</option>';
        }).join('');
        var body =
            '<div class="form-group">' +
            '<label class="form-label" for="itSpace">Space <span class="req">*</span></label>' +
            '<select class="form-input mono" id="itSpace">' + options + '</select>' +
            '<div class="form-hint">This list was refreshed when the dialog opened.</div>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label" for="itHash">Token ID (full hash) <span class="req">*</span></label>' +
            '<input class="form-input mono" id="itHash" autocomplete="off" spellcheck="false" ' +
            'placeholder="sha256:64 hexadecimal characters">' +
            '<div class="form-hint">Use the complete hash shown after token creation. Prefix shortcuts are not accepted.</div>' +
            '</div>' +
            '<div class="form-error" id="itErr" role="alert" hidden></div>';

        _openModal('Invite token to a space', body, 'Invite token', function () {
            return onInviteConfirm(sessionAtList, spaces);
        });
    }

    async function onInviteConfirm(sessionAtOpen, listedSpaces) {
        var errEl = document.getElementById('itErr');
        var spaceEl = document.getElementById('itSpace');
        var hashEl = document.getElementById('itHash');
        var spaceId = spaceEl ? spaceEl.value : '';
        var tokenHash = hashEl ? hashEl.value.trim() : '';
        if (errEl) { errEl.hidden = true; errEl.textContent = ''; }

        if (_sessionEnded(sessionAtOpen) || !hasManage(_sessionIdentity()) ||
            hasGlobalAdmin(_sessionIdentity())) {
            clearAdminTokenCache();
            if (errEl) { errEl.textContent = 'Your session permissions changed. Reopen this form.'; errEl.hidden = false; }
            return false;
        }
        if (listedSpaces.indexOf(spaceId) === -1) {
            if (errEl) { errEl.textContent = 'Choose a space from the refreshed list.'; errEl.hidden = false; }
            return false;
        }
        if (!/^sha256:[0-9a-f]{64}$/.test(tokenHash)) {
            if (errEl) { errEl.textContent = 'Enter the canonical full hash: sha256: followed by 64 lowercase hexadecimal characters.'; errEl.hidden = false; }
            return false;
        }

        var epochAtCall = AdminRouter.epoch;
        var genAtCall = _modalGen;
        var sessionAtCall = _sessionIdentity();
        var res;
        try {
            res = await callTool('space_invite_token', { space_id: spaceId, token_hash: tokenHash });
        } catch (e) {
            res = { status: 'error' };
        }
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;
        if (res && res.status === 'ok') {
            showToast('ok', res.added === false ? 'Token already had access to this space' : 'Token invited to space');
            return true;
        }
        if (errEl) {
            errEl.textContent = (res && res.message) ? String(res.message) : 'The server refused or failed this invitation.';
            errEl.hidden = false;
        }
        return false;
    }

    // ─────────────────────────── edit (delta mode only) ───────────────────────────

    function editDataFromToken(token) {
        return {
            hash: String(token.hash || ''),
            name: String(token.name || ''),
            perms: Array.isArray(token.permissions) ? token.permissions.join(',') : '',
            spaces: Array.isArray(token.space_ids) ? token.space_ids.join(',') : '',
            email: String(token.email || ''),
        };
    }

    // A clear page-level entry point complements the per-row Edit button. The
    // full hash is the option value and part of its label, so duplicate names
    // can never select the wrong credential.
    function openEditPickerModal() {
        if (_tokenListEpoch !== AdminRouter.epoch) {
            showToast('warn', 'Wait for the token list to finish loading before selecting a token.');
            return;
        }
        var editable = (cache && Array.isArray(cache.tokens) ? cache.tokens : []).filter(function (token) {
            return !isInternalLong(token) && !token.revoked;
        });
        if (!editable.length) {
            showToast('warn', 'There are no active tokens available to edit.');
            return;
        }

        var options = editable.map(function (token) {
            var label = token.name ? String(token.name) : 'Unnamed token';
            return '<option value="' + esc(String(token.hash || '')) + '">' +
                esc(label + ' — ' + String(token.hash || '')) + '</option>';
        }).join('');
        var body =
            '<div class="form-group">' +
            '<label class="form-label" for="editTokenPicker">Token</label>' +
            '<select class="form-input" id="editTokenPicker">' + options + '</select>' +
            '<div class="form-hint">Select by full token ID. Only active, non-system tokens can be edited.</div>' +
            '</div>' +
            '<div id="editTokenPickerErr"></div>';

        _openModal('Choose a token to edit', body, 'Edit token', function () {
            if (!requireGlobalAdmin()) return false;
            if (_tokenListEpoch !== AdminRouter.epoch) {
                var staleEl = document.getElementById('editTokenPickerErr');
                if (staleEl) staleEl.innerHTML = serverMessage('The token list changed. Close this dialog and select a token again.');
                return false;
            }
            var picker = document.getElementById('editTokenPicker');
            var selectedHash = picker ? picker.value : '';
            var selected = null;
            for (var i = 0; i < editable.length; i++) {
                if (editable[i].hash === selectedHash) {
                    selected = editable[i];
                    break;
                }
            }
            if (!selected) {
                var errorEl = document.getElementById('editTokenPickerErr');
                if (errorEl) errorEl.innerHTML = serverMessage('Select an active token from this list.');
                return false;
            }
            openEditModal(editDataFromToken(selected));
            return false;
        });
    }

    async function openEditModal(data) {
        // R3: dataset values are decoded — re-escape at every HTML sink below.
        var hash = data.hash || '';
        var name = data.name || '';
        var currentPerms = data.perms || '';
        var beforeSpaces = (data.spaces || '').split(',').map(function (s) { return s.trim(); })
            .filter(function (s) { return s; });
        var email = data.email || '';

        // Space chip universe (§5.7): union(space_list ids, token's space_ids).
        // One call, only when the shell cache is empty. On error, fall back to
        // the token's own space_ids.
        var allSpaces = [];
        var cached = (cache && Array.isArray(cache.spaces)) ? cache.spaces : [];
        if (cached.length) {
            allSpaces = cached.map(function (s) { return s.space_id; }).filter(Boolean);
        } else {
            var epochAtCall = AdminRouter.epoch;
            var genAtCall = _modalGen;
            var sessionAtCall = _sessionIdentity();
            var listRes;
            try { listRes = await callTool('space_list', {}); } catch (e) { listRes = null; }
            // Codex R3 finding 2 + R4 finding 1: this await precedes opening the
            // edit modal, so drop it if the operator navigated away, opened another
            // modal, OR the session was wiped (wipeSession changes neither epoch
            // nor generation) — never pop a stale edit modal, least of all one
            // holding a prior session's token data, over the login overlay.
            if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return;
            if (listRes && listRes.status === 'ok' && Array.isArray(listRes.spaces)) {
                cache.spaces = listRes.spaces;
                allSpaces = listRes.spaces.map(function (s) { return s.space_id; }).filter(Boolean);
            }
        }

        // Union preserving order: known spaces first, then orphaned grants.
        var universe = [];
        var seen = Object.create(null);
        allSpaces.forEach(function (sid) {
            if (!seen[sid]) { seen[sid] = true; universe.push({ id: sid, orphan: false }); }
        });
        beforeSpaces.forEach(function (sid) {
            if (!seen[sid]) { seen[sid] = true; universe.push({ id: sid, orphan: true }); }
        });

        var permOptions = '<option value="">— no change —</option>' +
            PERMISSION_PRESETS.map(function (p) {
                var sel = (p.value === currentPerms) ? ' selected' : '';
                return '<option value="' + esc(p.value) + '"' + sel + '>' + esc(p.label) + '</option>';
            }).join('');

        var chips = universe.length
            ? universe.map(function (s) {
                var checked = beforeSpaces.indexOf(s.id) !== -1 ? ' checked' : '';
                var orphan = s.orphan
                    ? ' <span class="chip-orphan">(not found)</span>' : '';
                return '<label class="space-check"><input type="checkbox" class="et-space" value="' +
                    esc(s.id) + '"' + checked + '> <span class="mono">' + esc(s.id) + '</span>' + orphan + '</label>';
            }).join('')
            : '<p class="form-hint">No spaces available. This token keeps its current allowlist.</p>';

        var body =
            '<div class="form-group">' +
            '<label class="form-label">Token</label>' +
            '<div class="mono-block">' + esc(name) + '</div>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label" for="etPerms">Permissions</label>' +
            '<select class="form-input" id="etPerms">' + permOptions + '</select>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label">Space allowlist</label>' +
            '<div class="space-checks" id="etSpaceChecks">' + chips + '</div>' +
            '<div class="form-hint" id="etSpacesHint">' + esc(ALLOWLIST_NOTE) +
            ' Changes apply as an additive delta (add/remove) — the full list is never ' +
            'replaced, so no grant is silently dropped.</div>' +
            '</div>' +
            '<div class="form-group">' +
            '<label class="form-label" for="etEmail">Email / owner</label>' +
            '<input class="form-input" id="etEmail" type="email" autocomplete="off" value="' + esc(email) + '">' +
            '</div>' +
            '<div id="etErr"></div>';

        _openModal('Edit token', body, 'Save changes', function () {
            return onEditConfirm(hash, currentPerms, beforeSpaces, email);
        });

        var editPerms = document.getElementById('etPerms');
        var editHint = document.getElementById('etSpacesHint');
        function syncEditAdminScopeInvariant() {
            if (!editPerms) return;
            var effective = editPerms.value || currentPerms;
            var targetAdmin = effective.split(',').indexOf('admin') !== -1;
            var editBoxes = document.querySelectorAll('.et-space');
            for (var i = 0; i < editBoxes.length; i++) {
                editBoxes[i].disabled = targetAdmin;
            }
            if (editHint) {
                editHint.textContent = targetAdmin
                    ? 'Admin access is global. Saving an admin profile clears space_ids under token-store v2.'
                    : ALLOWLIST_NOTE + ' A downgrade from admin starts empty; checked spaces are explicit new grants.';
            }
        }
        if (editPerms) {
            editPerms.addEventListener('change', syncEditAdminScopeInvariant);
            syncEditAdminScopeInvariant();
        }
    }

    async function onEditConfirm(hash, currentPerms, beforeSpaces, beforeEmail) {
        var args = { token_hash: hash };

        var newPerms = document.getElementById('etPerms').value;
        if (newPerms && newPerms !== currentPerms) args.permissions = newPerms;
        var effectivePerms = newPerms || currentPerms;
        var targetIsAdmin = effectivePerms.split(',').indexOf('admin') !== -1;

        var newEmail = document.getElementById('etEmail').value.trim();
        if (newEmail && newEmail !== beforeEmail) args.email = newEmail;

        // Compute the additive delta from checkbox state (delta mode ONLY —
        // replacement mode is never offered, §5.7).
        if (!targetIsAdmin) {
            var checked = [];
            var boxes = document.querySelectorAll('.et-space');
            for (var i = 0; i < boxes.length; i++) {
                if (boxes[i].checked) checked.push(boxes[i].value);
            }
            var toAdd = checked.filter(function (s) { return beforeSpaces.indexOf(s) === -1; });
            var toRemove = beforeSpaces.filter(function (s) { return checked.indexOf(s) === -1; });
            if (toAdd.length) args.space_ids_add = toAdd.join(',');
            if (toRemove.length) args.space_ids_remove = toRemove.join(',');
        }

        if (!args.permissions && !args.email && !args.space_ids_add && !args.space_ids_remove) {
            var noEl = document.getElementById('etErr');
            if (noEl) noEl.innerHTML = serverMessage('No changes to apply.');
            return false;
        }

        var epochAtCall = AdminRouter.epoch;
        var genAtCall = _modalGen;
        var sessionAtCall = _sessionIdentity();
        var res;
        try { res = await callTool('admin_update_token', args); } catch (e) { res = { status: 'error' }; }
        // Stale continuation (route changed, a newer modal replaced this one, OR
        // the session was wiped): drop without closing a newer modal or repainting
        // a dead session's state (R2 finding 3 + R4 finding 1).
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;

        if (res && res.status === 'ok') {
            var msg = res.message || 'Token updated';
            if (res.info) msg = res.info;
            if (res.warning_no_access) msg = res.warning_no_access;
            // The cached whoami is intentionally monotonic-downward here: when
            // an admin edits its own token to remove admin, apply that known
            // successful reduction immediately. This prevents a post-downgrade
            // refresh/action from reading cached admin data or probing admin
            // tools before the next login refreshes whoami.
            if (args.permissions && sessionAtCall && hash === sessionAtCall.token_hash &&
                args.permissions.split(',').indexOf('admin') === -1) {
                sessionAtCall.permissions = args.permissions.split(',').filter(function (p) { return p; });
                clearAdminTokenCache();
                if (typeof renderIdentityBlock === 'function') renderIdentityBlock(sessionAtCall);
            }
            showToast(res.warning_no_access ? 'warn' : 'ok', String(msg));
            AdminRouter.refresh();
            return true;
        }
        var errEl = document.getElementById('etErr');
        if (errEl) errEl.innerHTML = serverMessage((res && res.message) || 'The server refused or failed this operation.');
        return false;
    }

    // ─────────────────────────── revoke / delete (non-typed, red) ───────────────────────────

    function openRevokeModal(data) {
        var hash = data.hash || '';
        var name = data.name || '';
        var internal = data.internal === '1';

        var warn = '<p>This soft-revokes <strong>' + esc(name) + '</strong>. There is no ' +
            'un-revoke: a revoked token can be deleted but never re-activated.</p>';
        if (internal) {
            warn += '<div class="destructive-note">' + icon('alert') +
                '<span>Revoking <code>internal-long</code> fails the embedded long tier ' +
                'closed: a revoked entry is never re-activated automatically, and ' +
                '<code>long_push</code> / <code>long_status</code> will fail until the ' +
                'credential is re-provisioned by an operator.</span></div>';
        }
        openConfirmDanger('Revoke token', warn, 'Revoke', 'access-revoke-do', hash);
    }

    function openDeleteModal(data) {
        var hash = data.hash || '';
        var name = data.name || '';
        var warn = '<p>This permanently deletes the revoked token <strong>' + esc(name) +
            '</strong> from the registry. This is irreversible.</p>';
        openConfirmDanger('Delete token', warn, 'Delete', 'access-delete-do', hash);
    }

    // Non-typed destructive confirm (§7.4.1 "Medium-impact") with a Critical-Red
    // confirm button (§7.4.4). The button is body-owned and wired through the
    // shell's data-action delegation — no reach into modal internals, no typed
    // challenge (the unratified typed-revoke/delete proposal, B3.5, is a panel
    // decision left to the frozen baseline).
    function openConfirmDanger(title, warnHtml, verb, doAction, hash) {
        var body = '<div class="destructive-summary">' + warnHtml + '</div>' +
            '<div id="dangerErr"></div>' +
            '<div class="modal-footer inline-footer">' +
            '<button type="button" class="btn btn-secondary" data-action="close-modal">Cancel</button>' +
            '<button type="button" class="btn btn-danger" data-action="' + esc(doAction) +
            '" data-hash="' + esc(hash) + '">' + esc(verb) + '</button>' +
            '</div>';
        _openModal(title, body);
    }

    async function runMutation(tool, hash, btn, okKind, okText) {
        if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
        var epochAtCall = AdminRouter.epoch;
        var genAtCall = _modalGen;
        var sessionAtCall = _sessionIdentity();
        var res;
        try { res = await callTool(tool, { token_hash: hash }); } catch (e) { res = { status: 'error' }; }
        // Stale (route changed, a newer modal replaced this one, OR the session
        // was wiped): drop WITHOUT closeModal()/toast/refresh, which would hide a
        // newer modal or repaint a dead session's message (R2 finding 3 + R4
        // finding 1). The mutation still committed server-side; the list reloads
        // fresh on the next Access visit.
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return;

        if (res && (res.status === 'ok' || res.status === 'deleted')) {
            if (sessionAtCall && sessionAtCall.token_hash === hash) {
                dropKnownLocalPrivileges(sessionAtCall);
            }
            showToast(okKind, res.message ? String(res.message) : okText);
            closeModal();
            AdminRouter.refresh();
            return;
        }
        if (res && res.status === 'not_found') {
            showToast('warn', res.message ? String(res.message) : 'Token not found — list refreshed.');
            closeModal();
            AdminRouter.refresh();
            return;
        }
        // Error: keep the modal open, show the verbatim server message (via the
        // shell serverMessage() builder, which esc()'s its argument — R4).
        var errSlot = document.getElementById('dangerErr');
        if (errSlot) {
            errSlot.innerHTML = serverMessage((res && res.message) || 'The server refused or failed this operation.');
        }
        if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
    }

    // ─────────────────────────── purge (typed) ───────────────────────────

    function openPurgeModal(mode) {
        if (mode === 'all') {
            var allWarn =
                '<div class="destructive-note">' + icon('alert') +
                '<span>This deletes ALL tokens, including the <code>internal-long</code> ' +
                'credential of the embedded long runtime. Embedded long-tier calls will fail ' +
                'until the next embedded bind (<code>long_push</code> path) re-registers the ' +
                'credential (<code>src/live_mem/core/graph_bridge.py:497</code>); a token that ' +
                'was revoked rather than deleted stays fail-closed.</span></div>' +
                '<div class="destructive-note">' + icon('alert') +
                '<span>Every agent token is deleted. The server remains reachable only via the ' +
                'bootstrap key until new tokens are created.</span></div>';
            _openDestructive({
                title: 'Purge ALL tokens',
                bodyHtml: allWarn,
                verb: 'Purge all tokens',
                typedConfirmation: 'purge all',
                onConfirm: function () {
                    return runPurge({ revoked_only: false, confirm: true });
                },
            });
            return;
        }
        // revoked-only
        _openDestructive({
            title: 'Purge revoked tokens',
            bodyHtml: '<p>Physically deletes all revoked tokens. Active tokens are not affected.</p>',
            verb: 'Purge revoked',
            typedConfirmation: 'purge revoked',
            onConfirm: function () {
                return runPurge({ revoked_only: true });
            },
        });
    }

    async function runPurge(args) {
        var epochAtCall = AdminRouter.epoch;
        var genAtCall = _modalGen;
        var sessionAtCall = _sessionIdentity();
        var res;
        try { res = await callTool('admin_purge_tokens', args); } catch (e) { res = { status: 'error' }; }
        // Stale continuation (route/modal change OR session wipe): drop without
        // closing a newer modal or repainting a dead session (R2 f3 + R4 f1).
        if (_isStale(epochAtCall, genAtCall, sessionAtCall)) return false;

        if (res && res.status === 'ok') {
            var n = (res.deleted != null) ? res.deleted : 0;
            if (args.revoked_only === false) dropKnownLocalPrivileges(sessionAtCall);
            showToast('ok', res.message ? String(res.message) : (n + ' token(s) purged'));
            AdminRouter.refresh();
            return true;
        }
        showToast('error', (res && res.message) ? String(res.message) : 'Purge failed.');
        return false;
    }

    // ─────────────────────────── action registrations ───────────────────────────

    registerAction('access-refresh', function () {
        if (hasGlobalAdmin(_sessionIdentity())) AdminRouter.refresh();
        else clearAdminTokenCache();
    });
    registerAction('access-create', function () { openCreateModal(); });
    registerAction('access-invite', function () { openInviteModal(); });
    registerAction('access-open-edit', function () { if (requireGlobalAdmin()) openEditPickerModal(); });
    registerAction('access-edit', function (data) { if (requireGlobalAdmin()) openEditModal(data); });
    registerAction('access-revoke', function (data) { if (requireGlobalAdmin()) openRevokeModal(data); });
    registerAction('access-delete', function (data) { if (requireGlobalAdmin()) openDeleteModal(data); });
    registerAction('access-purge', function (data) { if (requireGlobalAdmin()) openPurgeModal(data.mode); });
    registerAction('access-revoke-do', function (data, btn) {
        if (!requireGlobalAdmin()) return;
        runMutation('admin_revoke_token', data.hash, btn, 'ok', 'Token revoked');
    });
    registerAction('access-delete-do', function (data, btn) {
        if (!requireGlobalAdmin()) return;
        runMutation('admin_delete_token', data.hash, btn, 'ok', 'Token deleted');
    });

    AdminViews.register('access', render);
})();
