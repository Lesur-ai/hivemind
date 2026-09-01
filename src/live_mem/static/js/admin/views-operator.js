/**
 * Operator tools view (P8-4, issue #142) — routes #/operator/backups and
 * #/operator/maintenance, both dispatched here via params.tab.
 *
 * Backups (§4.6 B1–B5, §5.8.1): global inventory, single- and all-spaces
 * backup create, typed-confirmation restore/delete. Maintenance (§4.8 M1–M6,
 * §5.8.2): per-space compact / repair (dry-run default, two-step bound to the
 * reviewed target) and all three GC orphan-note modes. GC deletion is bound to
 * the exact opaque eligible-set token returned by a successful dry run for the
 * current target, threshold, and browser-session generation. Token purge is NOT
 * rendered here — Access owns that destructive control (§4.8 M6); this view
 * only cross-links to it.
 *
 * Real data only; no polling (D8). Escaping per §7.3.3 R1–R6: every dynamic
 * value through esc() at the sink; server text only via serverMessage();
 * all actions via the shell [data-action] delegate (CSP-safe). Permission gates
 * read the cached identity (never a fresh probe); the server stays authoritative.
 *
 * Timestamp note: backup_list.timestamp and admin_gc_notes `oldest` are compact
 * non-ISO folder/filename forms — rendered as raw mono, never through the shared
 * UTC timestamp helper (which only handles ISO-8601).
 */
(function () {
    'use strict';

    const state = {
        identity: {},
        tab: 'backups',
        spaces: null,       // last space_list payload (pickers)
        compactDry: null,   // space id of the last SUCCESSFUL compact dry run
        // The verified apply response stays visible beside its mandatory
        // post-apply dry run so its readback hashes/preimage are not lost.
        compactApplyEvidence: null,
        repairDry: null,    // space id of the last SUCCESSFUL repair dry run
        sessionGeneration: null,
        gcDry: null,        // exact {spaceId,maxAgeDays,count,token,sessionGeneration}
        gcMutation: null,   // in-flight GC mutation owner (separate from proof)
    };

    function perms() {
        return (state.identity && Array.isArray(state.identity.permissions)) ? state.identity.permissions : [];
    }
    function isAdmin() {
        // Runtime harnesses can execute this view without the full shell. The
        // shipped page loads the generated mapping first; the fallback keeps
        // this standalone module defensive and is still only a UI hint.
        if (typeof toolCapabilityHint === 'function') {
            return toolCapabilityHint('admin_gc_notes', state.identity).available;
        }
        return perms().includes('admin');
    }
    function hasManage() {
        if (typeof toolCapabilityHint === 'function') {
            return toolCapabilityHint('bank_compact', state.identity).available;
        }
        return perms().includes('manage') || perms().includes('admin');
    }

    // Render a required numeric metric: a real number (incl. 0) shows as-is; an
    // ABSENT field shows "—" — never a fabricated 0 (Codex LOW / data honesty).
    function numOr(v, dash) { return (typeof v === 'number') ? String(v) : (dash || '—'); }

    // Modal-instance token (see views-consolidation.js): a slow/out-of-order
    // continuation drops instead of closing or overwriting a NEWER same-route
    // modal. Checked with the navigation epoch before any modal/close effect.
    let _modalOp = 0;
    function beginModalOp() { return ++_modalOp; }
    function modalOpCurrent(t) { return _modalOp === t; }

    // Session-ownership boundary (§3.1.4): logout / 401 wipes the shell but does
    // not bump the route epoch, so a continuation must ALSO verify the session is
    // still active before any effect or further request. `livemem_auth` is
    // HttpOnly; we read the shell's login-overlay visibility as the signal.
    function sessionActive() {
        const ov = document.getElementById('loginOverlay');
        return !ov || ov.classList.contains('hidden');
    }

    // Per-operation request generation: a newer request for the SAME maintenance
    // op (even same space, different input — e.g. GC max-age 7 then 0) bumps the
    // counter, so an earlier response arriving last drops instead of painting as
    // current. Complements the visible-target guard (picker moved). Apply
    // (mutation) uses a SEPARATE lane from dry-run reads so a manual dry run
    // during a pending Apply cannot invalidate the Apply's completion.
    const _maintReq = {
        compact: 0,
        compactApply: 0,
        repair: 0,
        repairApply: 0,
        gc: 0,
        gcConsolidate: 0,
        gcDelete: 0,
    };

    function requiresPermPanel(title, permLabel) {
        return panel(`<div class="panel-header"><h2>${esc(title)}</h2></div>${stateUnavailable('Requires ' + permLabel + ' permission.')}`);
    }

    // ───────────────────────── render entry ─────────────────────────

    function render(contentEl, params, ctx) {
        const nextSessionGeneration = (ctx && Number.isInteger(ctx.sessionGeneration))
            ? ctx.sessionGeneration
            : null;
        if (state.sessionGeneration !== nextSessionGeneration) {
            // View-module state survives a shell wipe. Never carry a reviewed
            // target or opaque delete proof into another authenticated session.
            state.spaces = null;
            state.compactDry = null;
            state.compactApplyEvidence = null;
            state.repairDry = null;
            state.gcDry = null;
            state.gcMutation = null;
            Object.keys(_maintReq).forEach(lane => { _maintReq[lane] += 1; });
            _modalOp += 1;
        }
        state.sessionGeneration = nextSessionGeneration;
        state.identity = (ctx && ctx.identity) || {};
        state.tab = (params && params.tab) === 'maintenance' ? 'maintenance' : 'backups';
        const epoch = ctx ? ctx.epoch : AdminRouter.epoch;
        if (state.tab === 'maintenance') renderMaintenance(contentEl, epoch);
        else renderBackups(contentEl, epoch);
    }

    function refreshBtn(action) {
        return `<button type="button" class="btn btn-secondary btn-sm" data-action="${esc(action)}">${icon('refresh')}<span>Refresh</span></button>`;
    }

    // Shared space picker (options from cached space_list). §5.8.1: space_list
    // error → the picker is disabled with an explicit error state.
    function spacePicker(id) {
        const sp = state.spaces;
        if (!sp || sp.status !== 'ok') {
            const msg = sp && sp.message ? serverMessage(sp.message) : '';
            return `<div class="form-group"><label class="form-label" for="${esc(id)}">Space</label>
                <select class="form-input" id="${esc(id)}" disabled><option>— spaces unavailable —</option></select>
                <p class="form-error">${icon('alert')} Couldn't load spaces.</p>${msg}</div>`;
        }
        const spaces = Array.isArray(sp.spaces) ? sp.spaces : [];
        const opts = ['<option value="">— select a space —</option>']
            .concat(spaces.map(s => `<option value="${esc(String(s.space_id))}">${esc(String(s.space_id))}</option>`))
            .join('');
        return `<div class="form-group"><label class="form-label" for="${esc(id)}">Space</label>
            <select class="form-input mono" id="${esc(id)}">${opts}</select></div>`;
    }

    // ═════════════════════════ BACKUPS TAB (§5.8.1) ═════════════════════════

    function renderBackups(contentEl, epoch) {
        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Backups', refreshBtn('op-backups-refresh'))}
            <div id="opBackupsActions" class="op-actions-bar"></div>
            <div id="opBackupsList">${panel(stateLoading('Loading backups…'))}</div>
        </div>`;
        loadBackups(epoch);
    }

    async function loadBackups(epoch) {
        let backups, spaces;
        try {
            // ≤4 concurrent (§5.0): the global list + the picker source.
            [backups, spaces] = await Promise.all([
                callTool('backup_list', {}),
                callTool('space_list', {}),
            ]);
        } catch (e) {
            if (AdminRouter.epoch !== epoch) return;
            paintBackupsList({ status: 'error' });
            return;
        }
        if (AdminRouter.epoch !== epoch) return;
        state.spaces = spaces;
        paintBackupsActions();
        paintBackupsList(backups);
    }

    function paintBackupsActions() {
        const el = document.getElementById('opBackupsActions');
        if (!el) return;
        const createBtn = `<button type="button" class="btn btn-primary btn-sm" data-action="op-backup-create">${icon('plus')}<span>New backup</span></button>`;
        // All-spaces backup is admin-only server-side (empty space_id); hide it
        // otherwise (§5.8.1) rather than showing an action that will 403.
        const allBtn = isAdmin()
            ? `<button type="button" class="btn btn-secondary btn-sm" data-action="op-backup-all">${icon('backups')}<span>Back up all spaces</span></button>`
            : '';
        el.innerHTML = createBtn + ' ' + allBtn;
    }

    function paintBackupsList(data) {
        const el = document.getElementById('opBackupsList');
        if (!el) return;
        if (!data || data.status !== 'ok') {
            // §5.0 sentinels are not retryable — dedicated NOT-AVAILABLE state.
            if (data && (data.status === 'truncated' || data.status === 'rate_limited' || data.status === 'read_only')) {
                el.innerHTML = panel(stateUnavailable(data.message));
                return;
            }
            el.innerHTML = panel(stateError({ title: "Couldn't load backups", message: data && data.message, retryAction: 'op-backups-refresh' }));
            return;
        }
        const backups = Array.isArray(data.backups) ? data.backups : [];
        const filteredBanner = data.filtered_by_token === true
            ? `<div class="op-filtered-banner state-degraded" role="status">${icon('alert')}<span>List filtered to your token's space allowlist.</span></div>`
            : '';
        let body;
        if (!backups.length) {
            body = stateEmpty({ title: 'No backups', hint: 'No backup archives are visible to this token.' });
        } else {
            const rows = backups.map(backupRow).join('');
            const table = dataTable(['Backup', 'Space', 'Timestamp', 'Files', 'Size', 'Actions'], rows);
            body = `<div class="panel-header"><h2>Backups</h2><span class="count-pill mono">${esc(String(data.total ?? backups.length))}</span></div>${table}`;
        }
        el.innerHTML = filteredBanner + panel(body);
    }

    function backupRow(b) {
        const bid = String(b.backup_id || '');
        const sid = String(b.space_id || '');
        // timestamp is the compact S3 folder form (YYYY-MM-DDTHH-MM-SS) — not
        // ISO; render as raw mono, never via renderTimestamp (would misparse).
        const ts = b.timestamp ? `<span class="mono-data">${esc(String(b.timestamp))}</span>` : '<span class="text-faint">—</span>';
        const files = (typeof b.files_count === 'number') ? String(b.files_count) : '—';
        const size = (typeof b.total_size === 'number') ? fmtSize(b.total_size) : '—';
        const desc = b.description ? `<div class="op-backup-desc body-small">${esc(String(b.description))}</div>` : '';
        const restoreBtn = `<button type="button" class="btn btn-secondary btn-sm" data-action="op-backup-restore" data-backup-id="${esc(bid)}" data-space="${esc(sid)}" aria-label="Restore backup ${esc(bid)}">${icon('restore')}<span>Restore</span></button>`;
        const deleteBtn = `<button type="button" class="btn btn-danger btn-sm" data-action="op-backup-delete" data-backup-id="${esc(bid)}" aria-label="Delete backup ${esc(bid)}">${icon('trash')}<span>Delete</span></button>`;
        return `<tr>
            <td>${copyable(bid, truncateMiddle(bid, 12, 8))}${desc}</td>
            <td><a class="mono" href="#/spaces/${esc(encodeURIComponent(sid))}">${esc(sid)}</a></td>
            <td>${ts}</td>
            <td class="num mono">${esc(files)}</td>
            <td class="num mono">${esc(size)}</td>
            <td class="actions">${restoreBtn} ${deleteBtn}</td>
        </tr>`;
    }

    // ── create single backup ──
    function openCreateBackup() {
        const body = `${spacePicker('opBackupCreateSpace')}
            <div class="form-group">
                <label class="form-label" for="opBackupCreateDesc">Description (optional)</label>
                <input class="form-input" id="opBackupCreateDesc" maxlength="500" autocomplete="off">
            </div>
            <div id="opBackupCreateErr"></div>`;
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showModal('New backup', body, 'Create backup', async () => {
            const sel = document.getElementById('opBackupCreateSpace');
            const sid = sel ? sel.value : '';
            const errEl = document.getElementById('opBackupCreateErr');
            if (!sid) { if (errEl) errEl.innerHTML = `<p class="form-error">${icon('alert')} Select a space.</p>`; return false; }
            const descEl = document.getElementById('opBackupCreateDesc');
            const desc = descEl ? descEl.value : '';
            const args = { space_id: sid };
            if (desc) args.description = desc;
            let data;
            try { data = await callTool('backup_create', args); }
            catch (e) { if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false; if (errEl) errEl.innerHTML = `<p class="form-error">Request failed.</p>`; return false; }
            // Drop if navigated away OR a newer modal replaced this one — an old
            // success returning true would closeModal() the newer confirmation.
            if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
            if (data && data.status === 'created') {
                showToast('ok', 'Backup created');
                loadBackups(epoch);
                return true;
            }
            if (errEl) errEl.innerHTML = serverMessage(data && data.message) || `<p class="form-error">The server refused or failed this operation.</p>`;
            return false;
        });
    }

    // ── all-spaces backup (admin) ──
    function backupAll() {
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showModal('Back up all spaces',
            `<p class="body-small">Creates a backup for every space you can see (admin). This may take a while and runs server-side.</p>
            <div class="form-group">
                <label class="form-label" for="opBackupAllDesc">Description (optional)</label>
                <input class="form-input" id="opBackupAllDesc" maxlength="500" autocomplete="off">
            </div>`,
            'Back up all', async () => {
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                // §4.6 B3: keep the optional fleet-backup description label.
                const descEl = document.getElementById('opBackupAllDesc');
                const desc = descEl ? descEl.value : '';
                const args = { space_id: '' };
                if (desc) args.description = desc;
                showModal('Backing up all spaces', stateLoading('Running backup for every space…'));
                let data;
                try { data = await callTool('backup_create', args); }
                catch (e) { if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false; showModal('Backup failed', panel(stateError({ title: 'Request failed' }))); return false; }
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                showModal('All-spaces backup', backupAllResult(data));
                loadBackups(epoch);
                return false;
            });
    }

    function backupAllResult(data) {
        if (!data || data.status !== 'ok') {
            return panel(serverMessage(data && data.message) || stateError({ title: 'The server refused or failed this operation.' }));
        }
        const details = Array.isArray(data.details) ? data.details : [];
        // Empty-cluster variant: spaces_total absent, only a message.
        const total = (typeof data.spaces_total === 'number') ? data.spaces_total : details.length;
        if (!details.length) {
            return panel(serverMessage(data.message) || stateEmpty({ title: 'No spaces to back up' }));
        }
        const rows = details.map(d => {
            const ok = d.status === 'created';
            const sev = ok ? 'ok' : 'error';
            const detail = ok
                ? `<span class="body-small">${esc(String(d.files ?? '—'))} files · ${esc(fmtSize(d.size))}</span>${d.backup_id ? ' ' + copyable(String(d.backup_id), truncateMiddle(String(d.backup_id), 12, 8)) : ''}`
                : serverMessage(d.message);
            return `<tr><td>${copyable(String(d.space_id || ''))}</td><td>${statusDot(sev, d.status || 'unknown')}</td><td>${detail}</td></tr>`;
        }).join('');
        const summary = `<p class="body-small">${esc(numOr(data.spaces_backed_up))} backed up · ${esc(numOr(data.spaces_failed))} failed · ${esc(String(total))} total.</p>`;
        return summary + `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Space</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }

    // ── restore (typed confirmation) ──
    function confirmRestore(backupId, spaceId) {
        if (!backupId) { showToast('error', 'Missing backup id'); return; }
        const body = `
            <p class="body-small">Restores backup <code>${esc(backupId)}</code>${spaceId ? ` into space <code>${esc(spaceId)}</code>` : ''}.</p>
            <p class="body-small">Restores over the target space. The server refuses to restore over a mesh-participating or fail-closed space (<code>hive_status_label</code> gate).</p>
            <p class="body-small"><strong>Data only:</strong> restore never restores token allowlists. A stored global admin may re-grant with <code>space_invite_token</code>; bootstrap must use <code>admin_update_token</code> or <code>admin_bulk_update_tokens</code>. Never delete and recreate the restored space to repair access.</p>`;
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showDestructiveModal({
            title: 'Restore backup', bodyHtml: body, verb: 'Restore', typedConfirmation: backupId,
            onConfirm: async () => {
                let data;
                // The unsafe-recovery flag is NEVER sent — that path is MCP-client only.
                try { data = await callTool('backup_restore', { backup_id: backupId, confirm: true }); }
                catch (e) { if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false; showModal('Restore failed', panel(stateError({ title: 'Request failed' }))); return false; }
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                if (data && data.status === 'ok') {
                    const sid = String(data.space_id || spaceId || '');
                    const link = sid ? `<p class="body-small"><a href="#/spaces/${esc(encodeURIComponent(sid))}">Open ${esc(sid)}</a></p>` : '';
                    showModal('Restore complete', `<div class="state state-success">${icon('check')}<h3>Restored</h3><p class="body-small">${esc(numOr(data.files_restored))} file(s) restored${sid ? ` to ${esc(sid)}` : ''}.</p><p class="body-small"><strong>Access was not restored.</strong> A stored global admin may use <code>space_invite_token</code>; bootstrap must use <code>admin_update_token</code> or <code>admin_bulk_update_tokens</code>. Never delete and recreate this space to repair access.</p>${link}</div>`);
                    loadBackups(epoch);
                    return false;
                }
                // not_found is a NEUTRAL state (§5(a)/§5.8.1), never a red error.
                if (data && data.status === 'not_found') {
                    showModal('Backup not found', `<div class="state">${icon('backups')}<h3>Backup not found</h3>${serverMessage(data.message) || '<p class="body-small">This backup was not found — it may have been deleted.</p>'}</div>`);
                    loadBackups(epoch);
                    return false;
                }
                // UNIFORM fail-closed for EVERY restore error (§5(a)/D7: never
                // parse the server message to pick cause-specific copy). The
                // verbatim message states the real cause; the static note is
                // general, non-causal doctrine.
                showModal('Restore failed', restoreErrorHtml(data));
                return false;
            },
        });
    }

    function restoreErrorHtml(data) {
        return `<div class="state state-error" role="alert">${icon('alert')}<div class="op-restore-error">
            <h3>Restore refused or failed</h3>
            ${serverMessage(data && data.message)}
            <p class="body-small op-restore-doctrine">Restoring over mesh-participating or fail-closed state is refused by design. If the target space still exists, it must be deleted first — this console does not chain delete-then-restore. The unsafe-recovery path is an MCP-client operation, not a console action.</p>
        </div></div>`;
    }

    // ── delete backup (typed confirmation) ──
    function confirmDeleteBackup(backupId) {
        if (!backupId) { showToast('error', 'Missing backup id'); return; }
        const body = `<p class="body-small">Permanently deletes this backup archive.</p><p class="mono-block">${esc(backupId)}</p>`;
        const epoch = AdminRouter.epoch;
        const op = beginModalOp();
        showDestructiveModal({
            title: 'Delete backup', bodyHtml: body, verb: 'Delete', typedConfirmation: backupId,
            onConfirm: async () => {
                let data;
                try { data = await callTool('backup_delete', { backup_id: backupId, confirm: true }); }
                catch (e) { if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false; showToast('error', 'Request failed'); return true; }
                if (AdminRouter.epoch !== epoch || !modalOpCurrent(op) || !sessionActive()) return false;
                if (data && data.status === 'deleted') {
                    showToast('ok', `Backup deleted (${numOr(data.files_deleted)} files)`);
                    loadBackups(epoch);
                    return true;
                }
                // not_found: the backup is already gone — neutral, not an error.
                if (data && data.status === 'not_found') {
                    showToast('ok', 'Backup already removed');
                    loadBackups(epoch);
                    return true;
                }
                showModal('Delete failed', panel(serverMessage(data && data.message) || stateError({ title: 'The server refused or failed this operation.' })));
                return false;
            },
        });
    }

    // ═════════════════════════ MAINTENANCE TAB (§5.8.2) ═════════════════════════

    function renderMaintenance(contentEl, epoch) {
        contentEl.innerHTML = `<div class="page">
            ${pageHeader('Maintenance', refreshBtn('op-maint-refresh'))}
            <p class="body-small op-maint-intro">Per-space operator tools. Compact and Repair require manage; garbage collection requires admin. Destructive actions never infer intent from an empty form.</p>
            <div id="opMaintPicker">${panel(stateLoading('Loading spaces…'))}</div>
            <div id="opMaintPanels"></div>
        </div>`;
        loadMaintenance(epoch);
    }

    async function loadMaintenance(epoch) {
        let spaces;
        try { spaces = await callTool('space_list', {}); }
        catch (e) { if (AdminRouter.epoch !== epoch) return; spaces = { status: 'error' }; }
        if (AdminRouter.epoch !== epoch) return;
        state.spaces = spaces;
        paintMaintPicker();
        paintMaintPanels();
    }

    function paintMaintPicker() {
        const el = document.getElementById('opMaintPicker');
        if (!el) return;
        el.innerHTML = panel(`<div class="panel-header"><h2>Target space</h2></div>${spacePicker('opMaintSpace')}<p class="form-hint">All actions below operate on the selected space.</p>`);
        // Clearing stale results when the target changes avoids showing one
        // space's report under another's name. CSP-safe (no inline handler).
        const sel = document.getElementById('opMaintSpace');
        if (sel) {
            sel.addEventListener('change', () => {
                ['opCompactResults', 'opRepairResults', 'opGcResults'].forEach(id => {
                    const r = document.getElementById(id);
                    if (r) r.innerHTML = '';
                });
                // Target changed -> a prior dry run no longer matches; require a
                // fresh one before Apply (§5.8.2 two-step, bound to the target).
                state.compactDry = null;
                state.compactApplyEvidence = null;
                state.repairDry = null;
                invalidateGcProof();
                // A -> B -> A must not resurrect any prior target's response,
                // including a compact apply/evidence or repair result in flight.
                Object.keys(_maintReq).forEach(lane => { _maintReq[lane] += 1; });
            });
        }
    }

    function maintSpace() {
        const el = document.getElementById('opMaintSpace');
        return el ? el.value : '';
    }

    function paintMaintPanels() {
        const el = document.getElementById('opMaintPanels');
        if (!el) return;
        el.innerHTML = compactPanel() + repairPanel() + gcPanel() + purgeXlinkPanel();
        const age = document.getElementById('opGcMaxAge');
        if (age) {
            age.addEventListener('input', () => {
                invalidateGcProof();
                // Invalidate an in-flight scan even if the value later returns
                // to its old number before that response arrives.
                _maintReq.gc += 1;
                const results = document.getElementById('opGcResults');
                if (results) results.innerHTML = '';
            });
        }
    }

    // ── compact (manage; dry-run default; two-step) ──
    function compactPanel() {
        if (!hasManage()) return requiresPermPanel('Compact', 'manage');
        return panel(`
            <div class="panel-header"><h2>Compact</h2></div>
            <p class="body-small">Compacts oversized bank files via the LLM using UTF-8 byte limits. Run a dry run first; Apply is a separate, explicit DirectLocal-only step. Shared Project Mesh routes are refused.</p>
            <div class="op-maint-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-compact-dry">Dry run</button>
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-compact-apply">Apply</button>
            </div>
            <div id="opCompactResults"></div>`);
    }

    function runCompact(dryRun, sidArg, preserveApplyEvidence) {
        // Apply passes the CAPTURED dry-run target (sidArg); dry reads the picker.
        const sid = sidArg || maintSpace();
        const el = document.getElementById('opCompactResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first.</p>`; return; }
        if (dryRun && !preserveApplyEvidence) state.compactApplyEvidence = null;
        if (!dryRun) state.compactApplyEvidence = null;
        if (el) {
            const evidence = dryRun && preserveApplyEvidence
                ? compactApplyEvidenceMarkup(state.compactApplyEvidence)
                : '';
            el.innerHTML = evidence + stateLoading(dryRun ? 'Scanning bank files…' : 'Compacting…');
        }
        // Revoke any prior Apply authorization the moment a NEW dry run starts:
        // it is only re-granted when THIS dry run succeeds (never from a pending
        // or failed newer preview). Keeps Apply bound to a current successful run.
        if (dryRun) state.compactDry = null;
        const epoch = AdminRouter.epoch;
        // Apply (mutation) has its own lane so a concurrent dry run can't make its
        // completion "stale"; the re-dry-run it schedules uses the read lane.
        const lane = dryRun ? 'compact' : 'compactApply';
        const req = ++_maintReq[lane];
        // Drop if navigated away, session lost, a NEWER request in this lane
        // started, or the visible target moved off the request's space.
        const stale = () => AdminRouter.epoch !== epoch || req !== _maintReq[lane] || maintSpace() !== sid || !sessionActive();
        callTool('bank_compact', { space_id: sid, dry_run: dryRun }).then(data => {
            if (stale()) return;
            if (dryRun) {
                if (data && data.status === 'ok') state.compactDry = sid;
                paintCompact(data);
            } else if (data && data.status === 'ok') {
                // §5.8.2: apply → after-action re-dry-run for the captured target
                // (shows the post-compaction state while retaining the verified
                // apply evidence; both remain bound to the visible target).
                state.compactApplyEvidence = data;
                showToast('ok', 'Compacted');
                runCompact(true, sid, true);
            } else {
                paintCompact(data); // conflict / error still surfaced
            }
        }).catch(() => {
            if (stale()) return;
            if (el) {
                const evidence = dryRun && preserveApplyEvidence
                    ? compactApplyEvidenceMarkup(state.compactApplyEvidence)
                    : '';
                el.innerHTML = evidence + stateError({ title: 'Compact failed' });
            }
        });
    }

    function compactByteText(value) {
        return (typeof value === 'number' && Number.isFinite(value) && value >= 0)
            ? `${value} UTF-8 bytes`
            : 'unknown / not asserted';
    }

    function compactSuccessMarkup(data) {
        const applied = data.dry_run === false;
        const files = Array.isArray(data.files) ? data.files : [];
        const after = data.total_size_after === null
            ? 'unknown / not asserted'
            : compactByteText(data.total_size_after);
        const summary = `<p class="body-small">${applied ? 'Applied' : 'Dry run'} · ${esc(numOr(data.files_total))} files, ${esc(numOr(data.files_over_limit))} over limit · ${esc(compactByteText(data.total_size_before))} → ${esc(after)}.</p>`;
        const preimage = applied && data.preimage_id
            ? `<p class="body-small"><strong>preimage_id:</strong> <code>${esc(String(data.preimage_id))}</code></p>`
            : '';
        let body;
        if (!files.length) {
            body = summary + preimage + stateEmpty({ title: 'No bank files' });
        } else {
            const headers = applied
                ? ['File', 'UTF-8 bytes', 'Max UTF-8 bytes', 'Source SHA-256', 'Result SHA-256', 'Over', 'Ratio', 'Compacted UTF-8 bytes', 'Reduction']
                : ['File', 'UTF-8 bytes', 'Max UTF-8 bytes', 'Source SHA-256', 'Over', 'Ratio'];
            const rows = files.map(f => {
                const over = f.over_limit ? statusDot('warn', 'yes') : statusDot('neutral', 'no');
                const sourceSha256 = (typeof f.source_sha256 === 'string') ? f.source_sha256 : '—';
                const resultSha256 = (typeof f.result_sha256 === 'string') ? f.result_sha256 : '—';
                const hashCells = `<td class="mono">${esc(sourceSha256)}</td>${applied ? `<td class="mono">${esc(resultSha256)}</td>` : ''}`;
                let extra = '';
                if (applied) {
                    const cs = (typeof f.compacted_size === 'number') ? `${f.compacted_size} UTF-8 bytes` : '—';
                    const rp = (typeof f.reduction_pct === 'number') ? `${f.reduction_pct}%` : (f.error ? '' : '—');
                    const errCell = f.error ? esc(String(f.error)) : esc(rp);
                    extra = `<td class="num mono">${esc(cs)}</td><td class="num mono">${errCell}</td>`;
                }
                return `<tr><td class="mono">${esc(String(f.filename || ''))}</td><td class="num mono">${esc(String(f.size ?? '—'))}</td><td class="num mono">${esc(String(f.max_size ?? '—'))}</td>${hashCells}<td>${over}</td><td class="num mono">${esc(String(f.ratio ?? '—'))}</td>${extra}</tr>`;
            }).join('');
            body = summary + preimage + `<div class="table-scroll"><table class="data-table"><thead><tr>${headers.map(h => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
        }
        return body;
    }

    function compactApplyEvidenceMarkup(data) {
        if (!data || data.status !== 'ok' || data.dry_run !== false) return '';
        return `<div class="state state-success" role="status">${icon('check')}<div><h3>Verified compaction apply evidence</h3>${compactSuccessMarkup(data)}</div></div>`;
    }

    function safeCompactionTargetDetail(failure) {
        if (!failure || typeof failure !== 'object'
            || failure.error !== 'ambiguous_or_missing_compaction_target') return '';
        const index = failure.operation_index;
        const resolution = failure.target_resolution;
        const count = failure.target_match_count;
        const sha256 = failure.target_heading_sha256;
        if (!Number.isSafeInteger(index) || index < 0
            || (resolution !== 'missing' && resolution !== 'ambiguous')
            || !Number.isSafeInteger(count) || count < 0
            || typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)
            || (resolution === 'missing' && count !== 0)
            || (resolution === 'ambiguous' && count < 2)) return '';
        return `operation_index=${index}; target_resolution=${resolution}; target_match_count=${count}; target_heading_sha256=${sha256}`;
    }

    function safeCompactionFailureRows(failures) {
        if (!Array.isArray(failures)) return '';
        return failures.map(f => {
            if (!f || typeof f !== 'object'
                || typeof f.filename !== 'string' || typeof f.error !== 'string') return '';
            const detail = safeCompactionTargetDetail(f);
            return `<tr><td class="mono">${esc(f.filename)}</td><td class="mono">${esc(f.error)}</td><td class="mono">${esc(detail || '—')}</td></tr>`;
        }).join('');
    }

    function paintCompact(data) {
        const el = document.getElementById('opCompactResults');
        if (!el) return;
        const applyEvidence = compactApplyEvidenceMarkup(state.compactApplyEvidence);
        if (data && data.status === 'conflict') {
            el.innerHTML = applyEvidence + `<div class="state state-degraded" role="status">${icon('alert')}<div><h3>Consolidation in progress</h3><p class="body-small">Consolidation is running for this space — retry when the lane is idle.</p>${serverMessage(data.message)}</div></div>`;
            return;
        }
        if (!data || data.status !== 'ok') {
            const recovery = data && data.status === 'partial' && data.recovery_required === true;
            const status = data && data.status ? esc(String(data.status)) : 'error';
            const reason = data && data.failure_reason ? esc(String(data.failure_reason)) : 'unknown';
            const after = data && data.total_size_after === null
                ? 'unknown / not asserted'
                : compactByteText(data && data.total_size_after);
            const failures = data && Array.isArray(data.failures) ? data.failures : [];
            const failureRows = safeCompactionFailureRows(failures);
            const failureTable = failureRows
                ? `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">File</th><th scope="col">Safe failure</th><th scope="col">Target resolution</th></tr></thead><tbody>${failureRows}</tbody></table></div>`
                : '';
            const message = data && data.message ? serverMessage(data.message) : '';
            const remediation = data && data.remediation
                ? `<p class="body-small"><strong>Remediation:</strong> ${esc(String(data.remediation))}</p>`
                : '';
            const preimage = data && data.preimage_id
                ? `<p class="body-small"><strong>preimage_id:</strong> <code>${esc(String(data.preimage_id))}</code></p>`
                : '';
            const applied = data && typeof data.files_applied_before_failure === 'number'
                ? `<p class="body-small"><strong>files_applied_before_failure:</strong> ${esc(String(data.files_applied_before_failure))}</p>`
                : '';
            const phase = data && data.failed_phase
                ? `<p class="body-small"><strong>failed_phase:</strong> ${esc(String(data.failed_phase))}</p>`
                : '';
            const rollback = data && data.rollback_outcome
                ? `<p class="body-small"><strong>rollback_outcome:</strong> ${esc(String(data.rollback_outcome))}</p>`
                : '';
            const mutation = data && data.apply_may_have_mutated === true
                ? '<p class="body-small"><strong>apply_may_have_mutated:</strong> true</p>'
                : '';
            const fileReports = data && Array.isArray(data.files) ? data.files : [];
            const hashRows = fileReports.map(f => {
                const source = f && f.source_sha256;
                const result = f && f.result_sha256;
                if (!source && !result) return '';
                return `<tr><td class="mono">${esc(String((f && f.filename) || ''))}</td><td class="mono">${esc(String(source || '—'))}</td><td class="mono">${esc(String(result || '—'))}</td></tr>`;
            }).join('');
            const hashTable = hashRows
                ? `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">File</th><th scope="col">Source SHA-256</th><th scope="col">Result SHA-256</th></tr></thead><tbody>${hashRows}</tbody></table></div>`
                : '';
            el.innerHTML = applyEvidence + `<div class="state ${recovery ? 'state-degraded' : 'state-error'}" role="${recovery ? 'status' : 'alert'}">${icon('alert')}<div><h3>${recovery ? 'Compaction recovery required' : 'Compaction refused or failed'}</h3><p class="body-small"><strong>status:</strong> ${status} · <strong>failure_reason:</strong> ${reason}</p><p class="body-small"><strong>total_size_after:</strong> ${esc(after)}</p>${phase}${rollback}${applied}${mutation}${preimage}${failureTable}${hashTable}${remediation}${message}<p class="body-small"><strong>No automatic retry or restore was performed.</strong></p></div></div>`;
            return;
        }
        el.innerHTML = applyEvidence + compactSuccessMarkup(data);
    }

    function confirmCompactApply() {
        const sid = maintSpace();
        const el = document.getElementById('opCompactResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first.</p>`; return; }
        // Two-step guard (§5.8.2): Apply is only offered after a successful dry
        // run for THIS space, and it targets that captured space — never the
        // (possibly changed) picker value at confirm time.
        if (state.compactDry !== sid) {
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Run a dry run for <code>${esc(sid)}</code> first — Apply is bound to the dry-run target.</p>`;
            return;
        }
        const captured = sid;
        const epoch = AdminRouter.epoch;
        showModal('Apply compaction', `<p class="body-small">Compacts oversized bank files in space <code>${esc(captured)}</code> via the LLM. This rewrites bank content only on a DirectLocal route; shared Project Mesh routes are refused.</p>`,
            'Apply', async () => {
                if (AdminRouter.epoch !== epoch) return false;
                state.compactDry = null;
                runCompact(false, captured);
                return true;
            });
    }

    // ── repair (manage; dry-run default; two-step) ──
    function repairPanel() {
        if (!hasManage()) return requiresPermPanel('Repair', 'manage');
        return panel(`
            <div class="panel-header"><h2>Repair</h2></div>
            <p class="body-small">Fixes bank filenames and removes duplicate files. Run a dry run first; Apply is a separate step.</p>
            <div class="op-maint-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-repair-dry">Dry run</button>
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-repair-apply">Apply</button>
            </div>
            <div id="opRepairResults"></div>`);
    }

    function runRepair(dryRun, sidArg) {
        // Apply passes the CAPTURED dry-run target (sidArg); dry reads the picker.
        const sid = sidArg || maintSpace();
        const el = document.getElementById('opRepairResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first.</p>`; return; }
        if (el) el.innerHTML = stateLoading(dryRun ? 'Scanning filenames…' : 'Repairing…');
        if (dryRun) state.repairDry = null; // revoke prior Apply auth (see runCompact)
        const epoch = AdminRouter.epoch;
        const lane = dryRun ? 'repair' : 'repairApply';
        const req = ++_maintReq[lane];
        const stale = () => AdminRouter.epoch !== epoch || req !== _maintReq[lane] || maintSpace() !== sid || !sessionActive();
        callTool('bank_repair', { space_id: sid, dry_run: dryRun }).then(data => {
            if (stale()) return;
            if (dryRun) {
                if (data && data.status === 'ok') state.repairDry = sid;
                paintRepair(data);
            } else if (data && data.status === 'ok') {
                // §5.8.2: apply → after-action re-dry-run for the captured target.
                showToast('ok', 'Repaired');
                runRepair(true, sid);
            } else {
                paintRepair(data);
            }
        }).catch(() => {
            if (stale()) return;
            if (el) el.innerHTML = stateError({ title: 'Repair failed' });
        });
    }

    function paintRepair(data) {
        const el = document.getElementById('opRepairResults');
        if (!el) return;
        if (!data || data.status !== 'ok') {
            el.innerHTML = stateError({ title: "Couldn't repair", message: data && data.message });
            return;
        }
        const repairs = Array.isArray(data.repairs) ? data.repairs : [];
        const dups = Array.isArray(data.duplicates) ? data.duplicates : [];
        const summary = `<p class="body-small">${esc(String(data.mode || ''))} · scanned ${esc(numOr(data.files_scanned))}, ok ${esc(numOr(data.files_ok))}, to repair ${esc(numOr(data.files_to_repair))}, duplicates ${esc(numOr(data.duplicates_found))}.</p>`;
        if (!repairs.length && !dups.length) {
            el.innerHTML = summary + stateEmpty({ title: 'Bank filenames are clean' }) + (data.message ? serverMessage(data.message) : '');
            return;
        }
        let tables = '';
        if (repairs.length) {
            const rows = repairs.map(r => `<tr><td class="mono">${esc(String(r.original_relpath || ''))}</td><td class="mono">${esc(String(r.sanitized || ''))}</td><td>${esc(String(r.action || ''))}</td><td>${esc(String(r.status || ''))}</td></tr>`).join('');
            tables += `<h3 class="op-sub">Renames</h3><div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Original</th><th scope="col">Sanitized</th><th scope="col">Action</th><th scope="col">Status</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        }
        if (dups.length) {
            const rows = dups.map(d => `<tr><td class="mono">${esc(String(d.relpath || ''))}</td><td class="mono">${esc(String(d.canonical || ''))}</td><td>${esc(String(d.action || ''))}</td><td>${esc(String(d.status || ''))}</td></tr>`).join('');
            tables += `<h3 class="op-sub">Duplicates</h3><div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Path</th><th scope="col">Canonical</th><th scope="col">Action</th><th scope="col">Status</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        }
        el.innerHTML = summary + tables + (data.message ? serverMessage(data.message) : '');
    }

    function confirmRepairApply() {
        const sid = maintSpace();
        const el = document.getElementById('opRepairResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first.</p>`; return; }
        if (state.repairDry !== sid) {
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Run a dry run for <code>${esc(sid)}</code> first — Apply is bound to the dry-run target.</p>`;
            return;
        }
        const captured = sid;
        const epoch = AdminRouter.epoch;
        showModal('Apply repair', `<p class="body-small">Moves mis-named bank files to their canonical path and deletes duplicates in space <code>${esc(captured)}</code>.</p>`,
            'Apply', async () => {
                if (AdminRouter.epoch !== epoch) return false;
                state.repairDry = null;
                runRepair(false, captured);
                return true;
            });
    }

    // ── GC orphan notes (admin; space-required; three modes) ──
    function gcPanel() {
        if (!isAdmin()) return requiresPermPanel('Garbage collection', 'admin');
        return panel(`
            <div class="panel-header"><h2>GC orphan notes</h2></div>
            <p class="body-small">Finds live notes older than the threshold that were never consolidated. A space selection is required — this console has no global GC. Dry run is always the default action.</p>
            <div class="form-group op-gc-age">
                <label class="form-label" for="opGcMaxAge">Max age (days)</label>
                <input class="form-input mono" id="opGcMaxAge" type="number" min="0" step="1" value="7">
            </div>
            <div class="op-maint-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-gc-dry">Dry run</button>
                <button type="button" class="btn btn-secondary btn-sm" data-action="op-gc-consolidate">Consolidate orphans</button>
                <button type="button" class="btn btn-danger btn-sm" data-action="op-gc-delete">Delete without consolidation</button>
            </div>
            <p class="form-hint op-gc-delete-hint">${icon('alert')}<span>Deletion requires a successful dry run for this exact space, age threshold, and browser session. Any change requires a new dry run.</span></p>
            <div id="opGcResults"></div>`);
    }

    function gcMaxAge() {
        const el = document.getElementById('opGcMaxAge');
        const raw = el ? String(el.value).trim() : '';
        if (!/^\d+$/.test(raw)) return null;
        const value = Number(raw);
        return Number.isSafeInteger(value) ? value : null;
    }

    function invalidateGcProof() {
        state.gcDry = null;
    }

    function gcFailureBlock(title, data) {
        const message = data && data.message ? serverMessage(data.message) : '';
        return `<div class="state state-error" role="alert">${icon('alert')}<div><h3>${esc(title)}</h3>${message}</div></div>`;
    }

    function gcContinuationCurrent(captured, lane, req) {
        return Number.isInteger(captured.sessionGeneration)
            && state.sessionGeneration === captured.sessionGeneration
            && currentSessionGeneration() === captured.sessionGeneration
            && sessionGenerationIsCurrent(captured.sessionGeneration)
            && AdminRouter.epoch === captured.epoch
            && (!lane || req === _maintReq[lane])
            && maintSpace() === captured.spaceId
            && gcMaxAge() === captured.maxAgeDays
            && sessionActive();
    }

    function gcProofForCurrentTarget() {
        const proof = state.gcDry;
        const sid = maintSpace();
        const maxAgeDays = gcMaxAge();
        if (!proof || state.gcMutation || !sessionActive()) return null;
        if (!Number.isInteger(state.sessionGeneration)) return null;
        if (currentSessionGeneration() !== state.sessionGeneration) return null;
        if (!sessionGenerationIsCurrent(proof.sessionGeneration)) return null;
        if (proof.spaceId !== sid || proof.maxAgeDays !== maxAgeDays) return null;
        if (proof.sessionGeneration !== state.sessionGeneration) return null;
        if (!Number.isInteger(proof.count) || proof.count < 0) return null;
        if (typeof proof.token !== 'string' || proof.token.length === 0) return null;
        return proof;
    }

    function beginGcMutation(lane, captured) {
        if (state.gcMutation) return null;
        // A mutation consumes every prior proof immediately. It also cancels a
        // pending dry response so that response cannot authorize a later delete.
        invalidateGcProof();
        _maintReq.gc += 1;
        const req = ++_maintReq[lane];
        const owner = { lane, req, sessionGeneration: captured.sessionGeneration };
        state.gcMutation = owner;
        return owner;
    }

    function finishGcMutation(owner) {
        if (state.gcMutation === owner) state.gcMutation = null;
    }

    function runGcDry() {
        const sid = maintSpace();
        const el = document.getElementById('opGcResults');
        // Starting ANY new scan revokes the prior delete proof before validation
        // or I/O. A failed, refused, or stale scan can never leave it authorized.
        invalidateGcProof();
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first (this console has no global GC).</p>`; return; }
        if (state.gcMutation) {
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} A GC mutation is already in progress.</p>`;
            return;
        }
        const maxAgeDays = gcMaxAge();
        const sessionGeneration = state.sessionGeneration;
        if (maxAgeDays === null) {
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Max age must be a whole number greater than or equal to 0.</p>`;
            return;
        }
        if (!Number.isInteger(sessionGeneration)) {
            if (el) el.innerHTML = gcFailureBlock('Authenticated session unavailable');
            return;
        }
        if (el) el.innerHTML = stateLoading('Scanning orphan notes…');
        const epoch = AdminRouter.epoch;
        const req = ++_maintReq.gc;
        const captured = { spaceId: sid, maxAgeDays, sessionGeneration, epoch };
        // Drop if navigated away, session lost, a NEWER GC scan started (same
        // space, different max-age included), or the target/session changed.
        const stale = () => !gcContinuationCurrent(captured, 'gc', req);
        callTool('admin_gc_notes', { space_id: sid, max_age_days: maxAgeDays, confirm: false }).then(data => {
            if (stale() || state.gcMutation) return;
            const count = data && data.total_old_notes;
            const token = data && data.eligible_set_token;
            if (data && data.status === 'ok'
                && Number.isInteger(count) && count >= 0
                && data.max_age_days === maxAgeDays
                && typeof token === 'string' && token.length > 0) {
                // Exact five-field proof cache. The token stays opaque and is
                // never rendered, parsed, transformed, or inferred from count.
                state.gcDry = Object.freeze({
                    spaceId: sid,
                    maxAgeDays,
                    count,
                    token,
                    sessionGeneration,
                });
            }
            paintGcDry(data);
        }).catch(() => {
            if (stale()) return;
            invalidateGcProof();
            if (el) el.innerHTML = stateError({ title: 'GC scan failed' });
        });
    }

    function paintGcDry(data) {
        const el = document.getElementById('opGcResults');
        if (!el) return;
        if (!data || data.status !== 'ok') {
            invalidateGcProof();
            el.innerHTML = gcFailureBlock("Couldn't scan orphan notes", data);
            return;
        }
        const summary = `<p class="body-small">Cutoff ${esc(String(data.cutoff_date || ''))} (≥ ${esc(String(data.max_age_days ?? '?'))} days) · ${esc(numOr(data.total_old_notes))} orphan note(s) · ${esc(fmtSize(data.total_old_size))}.</p>`;
        // §5.0/§5(a): the dry run returns a `message` for every result shape —
        // render it verbatim always, not only on the empty branch.
        const msg = data.message ? serverMessage(data.message) : '';
        const spaces = (data.spaces && typeof data.spaces === 'object') ? data.spaces : {};
        const ids = Object.keys(spaces);
        let detail = '';
        if (!ids.length) {
            detail = stateEmpty({ title: 'No orphan notes' });
        } else {
            detail = ids.map(sid => {
                const s = spaces[sid] || {};
                const byAgent = (s.by_agent && typeof s.by_agent === 'object')
                    ? Object.keys(s.by_agent).map(a => `<span class="chip">${esc(String(a))}: ${esc(String(s.by_agent[a]))}</span>`).join(' ')
                    : '';
                // `oldest` is a compact non-ISO filename form — raw mono only.
                const oldest = s.oldest ? `<span class="mono-data">${esc(String(s.oldest))}</span>` : '<span class="text-faint">—</span>';
                return `<div class="op-gc-space">
                    <div class="micro-label">${esc(sid)}</div>
                    <p class="body-small">${esc(numOr(s.old_notes))}/${esc(numOr(s.total_notes))} old · ${esc(fmtSize(s.old_notes_size))} · oldest ${oldest} · keys ${esc(numOr(s.keys_count))}</p>
                    <div class="op-gc-agents">${byAgent}</div>
                </div>`;
            }).join('');
        }
        const proofReady = gcProofForCurrentTarget() !== null;
        const proofWarning = proofReady ? '' : gcFailureBlock('Delete proof unavailable');
        el.innerHTML = summary + msg + detail + proofWarning;
    }

    function gcConsolidationDetails(data) {
        const details = (data && data.consolidation_details && typeof data.consolidation_details === 'object')
            ? data.consolidation_details
            : {};
        const rows = [];
        Object.keys(details).sort().forEach(sid => {
            const agents = (details[sid] && typeof details[sid] === 'object') ? details[sid] : {};
            Object.keys(agents).sort().forEach(agent => {
                const item = agents[agent] || {};
                const reason = item.reason ? `<code>${esc(String(item.reason))}</code>` : '<span class="text-faint">—</span>';
                const message = item.message ? serverMessage(item.message) : '';
                rows.push(`<tr>
                    <td class="mono">${esc(String(sid))}</td>
                    <td class="mono">${esc(String(agent))}</td>
                    <td>${esc(String(item.status || 'unknown'))}</td>
                    <td class="num mono">${esc(numOr(item.notes_processed))}/${esc(numOr(item.notes_requested))}</td>
                    <td>${reason}${message}</td>
                </tr>`);
            });
        });
        if (!rows.length) return '';
        return `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">Space</th><th scope="col">Agent</th><th scope="col">Status</th><th scope="col">Processed</th><th scope="col">Reason / message</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
    }

    function gcMutationResult(data, action) {
        const status = data && data.status ? String(data.status) : 'error';
        const reason = data && data.reason ? String(data.reason) : '';
        const failureReason = data && data.failure_reason ? String(data.failure_reason) : '';
        const message = data && data.message ? serverMessage(data.message) : '';
        let kind = 'error';
        let title = action === 'delete' ? 'Deletion refused or failed' : 'Consolidation refused or failed';
        let summary = action === 'delete'
            ? '<p class="body-small">No orphan notes were reported as deleted.</p>'
            : '<p class="body-small">No orphan notes were reported as consolidated.</p>';

        if (action === 'delete' && status === 'deleted') {
            kind = 'success';
            title = 'Deletion complete';
            summary = `<p class="body-small">${esc(numOr(data.deleted))} orphan note(s) deleted without consolidation.</p>`;
        } else if (action === 'delete' && status === 'partial' && reason === 'partial_delete') {
            kind = 'degraded';
            title = 'Deletion partial';
            summary = `<p class="body-small">${esc(numOr(data.deleted))}/${esc(numOr(data.delete_requested))} orphan note(s) deleted; ${esc(numOr(data.delete_failed))} not deleted. No automatic retry was attempted.</p>`;
        } else if (action === 'consolidate' && status === 'ok') {
            kind = 'success';
            title = 'Consolidation complete';
            summary = `<p class="body-small">${esc(numOr(data.consolidated))} orphan note(s) consolidated.</p>`;
        } else if (action === 'consolidate' && status === 'partial' && reason === 'partial_consolidation') {
            kind = 'degraded';
            title = 'Consolidation partial';
            summary = `<p class="body-small">${esc(numOr(data.consolidated))}/${esc(numOr(data.consolidation_requested))} orphan note(s) consolidated; ${esc(numOr(data.consolidation_failed))} not consolidated. No automatic retry was attempted.</p>`;
        } else if (status === 'conflict') {
            kind = 'degraded';
            if (reason === 'eligible_set_changed') title = 'Eligible note set changed';
            else if (reason === 'consolidation_in_progress') title = 'Consolidation in progress';
            else title = 'GC operation conflicted';
            summary = `<p class="body-small">The operation was not retried. Run a new dry run before another deletion attempt.</p>`;
        } else if (status === 'error' && reason === 'eligible_set_token_required') {
            title = 'Dry-run proof required';
        }

        const reasonHtml = reason
            ? `<p class="body-small">Reason: <code>${esc(reason)}</code></p>`
            : '';
        const failureReasonHtml = failureReason
            ? `<p class="body-small">Failure reason: <code>${esc(failureReason)}</code></p>`
            : '';
        const details = action === 'consolidate' ? gcConsolidationDetails(data) : '';
        const role = kind === 'error' ? 'alert' : 'status';
        const marker = kind === 'success' ? icon('check') : icon('alert');
        return `<div class="state state-${kind}" role="${role}">${marker}<div><h3>${esc(title)}</h3>${summary}${reasonHtml}${failureReasonHtml}${message}${details}</div></div>`;
    }

    function paintGcMutation(data, action) {
        const el = document.getElementById('opGcResults');
        if (el) el.innerHTML = gcMutationResult(data, action);
    }

    function confirmGcConsolidate() {
        const sid = maintSpace();
        const el = document.getElementById('opGcResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first (this console has no global GC).</p>`; return; }
        const maxAgeDays = gcMaxAge();
        if (maxAgeDays === null) {
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Max age must be a whole number greater than or equal to 0.</p>`;
            return;
        }
        const captured = {
            spaceId: sid,
            maxAgeDays,
            sessionGeneration: state.sessionGeneration,
            epoch: AdminRouter.epoch,
        };
        if (!Number.isInteger(captured.sessionGeneration) || !sessionActive()) {
            if (el) el.innerHTML = gcFailureBlock('Authenticated session unavailable');
            return;
        }
        const op = beginModalOp();
        showModal('Consolidate orphan notes',
            `<p class="body-small">Consolidates currently eligible orphan notes in <code>${esc(sid)}</code> (older than ${esc(String(captured.maxAgeDays))} day(s)) into the Memory Bank via the LLM. The server writes a GC notice for traceability.</p>`,
            'Consolidate', async () => {
                if (!modalOpCurrent(op) || !gcContinuationCurrent(captured)) return false;
                const owner = beginGcMutation('gcConsolidate', captured);
                if (!owner) {
                    showModal('GC already running', gcFailureBlock('A GC mutation is already in progress'));
                    return false;
                }
                if (el) el.innerHTML = stateLoading('Consolidating orphan notes…');
                let data;
                try {
                    data = await callTool('admin_gc_notes', {
                        space_id: sid,
                        max_age_days: captured.maxAgeDays,
                        confirm: true,
                        delete_only: false,
                    });
                } catch (e) {
                    finishGcMutation(owner);
                    if (!modalOpCurrent(op) || !gcContinuationCurrent(captured, 'gcConsolidate', owner.req)) return false;
                    const body = gcFailureBlock('GC consolidation request failed');
                    if (el) el.innerHTML = body;
                    showModal('GC consolidation failed', body);
                    return false;
                }
                finishGcMutation(owner);
                if (!modalOpCurrent(op) || !gcContinuationCurrent(captured, 'gcConsolidate', owner.req)) return false;
                paintGcMutation(data, 'consolidate');
                showModal('GC consolidation result', gcMutationResult(data, 'consolidate'));
                return false;
            });
    }

    function confirmGcDelete() {
        const sid = maintSpace();
        const el = document.getElementById('opGcResults');
        if (!sid) { if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Select a space first (this console has no global GC).</p>`; return; }
        if (gcMaxAge() === null) {
            invalidateGcProof();
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Max age must be a whole number greater than or equal to 0.</p>`;
            return;
        }
        const proof = gcProofForCurrentTarget();
        if (!proof) {
            invalidateGcProof();
            if (el) el.innerHTML = `<p class="form-error">${icon('alert')} Run a fresh dry run for this exact space, age threshold, and session before deleting.</p>`;
            return;
        }
        const captured = Object.freeze({
            spaceId: proof.spaceId,
            maxAgeDays: proof.maxAgeDays,
            count: proof.count,
            token: proof.token,
            sessionGeneration: proof.sessionGeneration,
            epoch: AdminRouter.epoch,
        });
        const challenge = `delete ${captured.count} notes`;
        const warning = `Deletes ${captured.count} orphan notes WITHOUT consolidating them. Their content is lost.`;
        const op = beginModalOp();
        showDestructiveModal({
            title: 'Delete orphan notes',
            bodyHtml: `<p class="body-small">${esc(warning)}</p><p class="body-small">Target <code>${esc(captured.spaceId)}</code> · older than ${esc(String(captured.maxAgeDays))} day(s).</p>`,
            verb: 'Delete without consolidation',
            typedConfirmation: challenge,
            onConfirm: async () => {
                const currentProof = gcProofForCurrentTarget();
                if (!modalOpCurrent(op) || !gcContinuationCurrent(captured)
                    || !currentProof || currentProof.token !== captured.token
                    || currentProof.count !== captured.count) {
                    if (modalOpCurrent(op) && gcContinuationCurrent(captured)) {
                        showModal('Delete proof expired', gcFailureBlock('Run a new dry run before deleting'));
                    }
                    return false;
                }
                const owner = beginGcMutation('gcDelete', captured);
                if (!owner) {
                    showModal('GC already running', gcFailureBlock('A GC mutation is already in progress'));
                    return false;
                }
                if (el) el.innerHTML = stateLoading('Deleting orphan notes…');
                let data;
                try {
                    data = await callTool('admin_gc_notes', {
                        space_id: captured.spaceId,
                        max_age_days: captured.maxAgeDays,
                        confirm: true,
                        delete_only: true,
                        expected_eligible_set_token: captured.token,
                    });
                } catch (e) {
                    finishGcMutation(owner);
                    if (!modalOpCurrent(op) || !gcContinuationCurrent(captured, 'gcDelete', owner.req)) return false;
                    const body = gcFailureBlock('GC delete request failed');
                    if (el) el.innerHTML = body;
                    showModal('GC deletion failed', body);
                    return false;
                }
                finishGcMutation(owner);
                if (!modalOpCurrent(op) || !gcContinuationCurrent(captured, 'gcDelete', owner.req)) return false;
                paintGcMutation(data, 'delete');
                showModal('GC deletion result', gcMutationResult(data, 'delete'));
                return false;
            },
        });
    }

    // ── token purge cross-link (M6 — Access owns the control) ──
    function purgeXlinkPanel() {
        return panel(`<div class="panel-header"><h2>Token purge</h2></div>
            <p class="body-small">Token purge lives in <a href="#/access">Access</a> so destructive token controls stay in one place.</p>`);
    }

    // ───────────────────────── action registration ─────────────────────────

    registerAction('op-backups-refresh', () => AdminRouter.refresh());
    registerAction('op-maint-refresh', () => AdminRouter.refresh());
    registerAction('op-backup-create', () => openCreateBackup());
    registerAction('op-backup-all', () => { if (isAdmin()) backupAll(); });
    registerAction('op-backup-restore', (d) => confirmRestore(d.backupId, d.space));
    registerAction('op-backup-delete', (d) => confirmDeleteBackup(d.backupId));
    registerAction('op-compact-dry', () => runCompact(true));
    registerAction('op-compact-apply', () => confirmCompactApply());
    registerAction('op-repair-dry', () => runRepair(true));
    registerAction('op-repair-apply', () => confirmRepairApply());
    registerAction('op-gc-dry', () => runGcDry());
    registerAction('op-gc-consolidate', () => confirmGcConsolidate());
    registerAction('op-gc-delete', () => confirmGcDelete());

    AdminViews.register('operator', render);
})();
