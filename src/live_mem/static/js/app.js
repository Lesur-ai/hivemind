/**
 * Live Memory - Orchestrateur principal
 * Login → sélection espace → chargement → auto-refresh intelligent
 */

// ═══════════════ AUTH ═══════════════

function showLogin(msg='') {
    document.getElementById('loginOverlay').classList.remove('hidden');
    document.getElementById('loginError').textContent = msg ? `❌ ${msg}` : '';
    document.getElementById('loginToken').focus();
}
function hideLogin() { document.getElementById('loginOverlay').classList.add('hidden'); }

// LM2-04 fix : authentification via cookie HttpOnly émis par /api/login.
// Le token brut n'est jamais stocké côté JS — un XSS ne peut donc plus
// l'exfiltrer (contrairement à l'ancienne approche localStorage).
async function doLogin() {
    const input = document.getElementById('loginToken');
    const btn = document.getElementById('loginBtn');
    const err = document.getElementById('loginError');
    const token = input.value.trim();
    if (!token) { err.textContent = '❌ Token required.'; return; }

    btn.disabled = true; btn.textContent = 'Signing in…'; err.textContent = '';
    try {
        // POST /api/login → émet le cookie HttpOnly côté serveur.
        const loginResult = await loginWithToken(token);
        if (loginResult.status !== 'ok') {
            err.textContent = `❌ ${loginResult.message || 'Token invalide'}`;
            return;
        }

        // Charger la liste des spaces (le cookie est déjà attaché par le navigateur)
        const data = await apiLoadSpaces();
        if (data.status !== 'ok') {
            err.textContent = `❌ ${data.message || 'Error'}`;
            return;
        }

        hideLogin();
        input.value = '';  // efface le token du DOM dès qu'il n'est plus utile
        fillSpaceSelect(data.spaces || []);
        // If the URL carries a valid space, applySpaceFromUrl loads it and
        // arms the refresh via loadSpace; otherwise arm it here once.
        if (!(await applySpaceFromUrl())) startRefresh();
    } catch {
        err.textContent = '❌ Server unreachable.';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign in';
    }
}

async function doLogout() {
    await logout();  // efface le cookie HttpOnly côté serveur
    setSpaceInUrl('');
    stopRefresh();
    app.spaceId = null; app.info = null; app.notes = []; app.bankFiles = [];
    app.currentBankFile = null; app.agentColors = {};
    document.getElementById('panelLeft').style.display = 'none';
    document.getElementById('panelRight').style.display = 'none';
    document.getElementById('placeholder').style.display = 'flex';
    document.getElementById('spaceSelect').innerHTML = '<option value="">-- Space --</option>';
    showLogin();
}

async function checkToken() {
    // Le cookie HttpOnly est invisible côté JS — on tente directement
    // un appel API : si le cookie est présent et valide, on enchaîne ;
    // sinon on affiche le formulaire de login.
    try {
        const r = await apiLoadSpaces();
        if (r.status === 'ok') {
            hideLogin();
            fillSpaceSelect(r.spaces || []);
            // Same as doLogin: avoid arming a second refresh when the URL
            // already auto-loaded a space.
            if (!(await applySpaceFromUrl())) startRefresh();
        } else {
            showLogin();
        }
    } catch (e) {
        if (e.message === 'Unauthorized') {
            // showLogin déjà appelé par authFetch sur 401
            return;
        }
        showLogin('Server unreachable.');
    }
}

// ═══════════════ URL STATE ═══════════════
// Persist the selected space in the `?space=<id>` query string so reloads,
// new tabs and shared links all land on the same space.

function getSpaceFromUrl() {
    try { return new URLSearchParams(window.location.search).get('space') || ''; }
    catch { return ''; }
}

function setSpaceInUrl(spaceId) {
    try {
        const url = new URL(window.location.href);
        if (spaceId) url.searchParams.set('space', spaceId);
        else url.searchParams.delete('space');
        history.replaceState(null, '', url.toString());
    } catch { /* best-effort */ }
}

async function applySpaceFromUrl() {
    // Returns true if a space from the URL was applied and loaded, so callers
    // can skip arming a second refresh cycle (loadSpace already armed one).
    const sel = document.getElementById('spaceSelect');
    const wanted = getSpaceFromUrl();
    if (!wanted) return false;
    if ([...sel.options].some(o => o.value === wanted)) {
        sel.value = wanted;
        await loadSpace(wanted);
        return true;
    }
    return false;
}

function fillSpaceSelect(spaces) {
    const sel = document.getElementById('spaceSelect');
    sel.innerHTML = '<option value="">-- Space --</option>';
    // Issue #8: native <option> elements don't support text-overflow:ellipsis,
    // so we truncate descriptions in JS to prevent the dropdown from overflowing.
    const MAX_DESC = 70;
    spaces.forEach(s => {
        const o = document.createElement('option');
        o.value = s.space_id;
        let desc = '';
        if (s.description) {
            desc = s.description.length > MAX_DESC
                ? s.description.slice(0, MAX_DESC).trimEnd() + '…'
                : s.description;
            // Description complète accessible en tooltip natif
            o.title = s.description;
        }
        o.textContent = s.space_id + (desc ? ' — ' + desc : '');
        sel.appendChild(o);
    });
}

// ═══════════════ CHARGEMENT ESPACE ═══════════════

async function loadSpace(spaceId) {
    if (!spaceId) {
        app.spaceId = null;
        document.getElementById('panelLeft').style.display = 'none';
        document.getElementById('panelRight').style.display = 'none';
        document.getElementById('placeholder').style.display = 'flex';
        // Don't stop refresh — keep refreshing space list even without selection
        return;
    }

    app.spaceId = spaceId;
    app.currentBankFile = null;
    app._noteHash = '';
    app._bankHash = '';

    document.getElementById('placeholder').style.display = 'none';
    document.getElementById('panelLeft').style.display = 'flex';
    document.getElementById('panelRight').style.display = 'flex';

    // Chargement initial complet
    await refresh(true);
    startRefresh();
}

// ═══════════════ REFRESH INTELLIGENT ═══════════════

async function refreshSpaceList() {
    // Refresh the space dropdown without losing the current selection.
    try {
        const r = await apiLoadSpaces();
        if (r.status !== 'ok') return;
        const sel = document.getElementById('spaceSelect');
        const current = sel.value;
        fillSpaceSelect(r.spaces || []);
        // Restore selection if still exists
        if (current && [...sel.options].some(o => o.value === current)) {
            sel.value = current;
        }
    } catch (_) { /* best-effort */ }
}

async function refresh(force = false) {
    // Always refresh space list + health status
    await refreshSpaceList();
    await refreshHealthStatus();

    // Capture the target space up-front: if the user switches space (or the
    // URL auto-loads one) while the calls below are in flight, a late response
    // must not be rendered into the now-current space's UI.
    const requestedSpaceId = app.spaceId;
    if (!requestedSpaceId) return;

    try {
        const [notesR, bankR, infoR] = await Promise.all([
            apiLoadNotes(requestedSpaceId),
            apiLoadBankList(requestedSpaceId),
            apiLoadSpaceInfo(requestedSpaceId),
        ]);

        // Stale-response guard — drop results if the space changed meanwhile.
        if (app.spaceId !== requestedSpaceId) return;

        // Détection changement notes
        const newNotes = notesR.status === 'ok' ? (notesR.notes || []) : app.notes;
        const noteHash = newNotes.length + ':' + (newNotes[0]?.timestamp || '');
        const notesChanged = noteHash !== app._noteHash;

        // Détection changement bank
        const newBank = bankR.status === 'ok' ? (bankR.files || []) : app.bankFiles;
        const bankHash = newBank.map(f => f.filename).join(',');
        const bankChanged = bankHash !== app._bankHash;

        // Info (toujours mettre à jour)
        app.info = infoR.status === 'ok' ? infoR : app.info;

        // Mettre à jour seulement ce qui a changé
        if (notesChanged || force) {
            app.notes = newNotes;
            app._noteHash = noteHash;
            renderLive();
        }

        if (bankChanged || force) {
            app.bankFiles = newBank;
            app._bankHash = bankHash;
            renderBankTabs();
        }

        // Dashboard toujours mis à jour (léger)
        renderDashboard();

        updateStatus('ok');
    } catch (e) {
        if (e.message !== 'Unauthorized') {
            console.error('Refresh:', e);
            updateStatus('error');
        }
    }
}

async function refreshHealthStatus() {
    // Dot color reflects /health status, clock always shows current time
    const el = document.getElementById('globalStatus');
    if (!el) return;
    const dot = el.querySelector('.dot');
    const txt = el.querySelector('.status-text');
    const tooltip = document.getElementById('healthTooltip');
    txt.textContent = fmtTime(new Date().toISOString());
    try {
        const h = await apiHealth();
        if (h.status === 'healthy') {
            dot.className = 'dot'; dot.style.background = '#4CAF50';
        } else if (h.status === 'degraded') {
            dot.className = 'dot'; dot.style.background = '#f39c12';
        } else {
            dot.className = 'dot'; dot.style.background = '#e74c3c';
        }
        // Build tooltip content from health details
        if (tooltip && h.services) {
            const statusIcon = (s) => s === 'ok' ? '🟢' : s === 'degraded' ? '🟠' : '🔴';
            const cls = (s) => s === 'ok' ? 'ht-ok' : s === 'degraded' ? 'ht-warn' : 'ht-err';
            let rows = `<div class="ht-title">Health — ${esc(h.status || '?')}</div>`;
            for (const [name, svc] of Object.entries(h.services)) {
                const st = svc.status || '?';
                const lat = svc.latency_ms ? `${Math.round(svc.latency_ms)}ms` : '';
                const extra = svc.bucket || svc.model || svc.url || '';
                rows += `<div class="ht-row">
                    <span class="ht-label">${statusIcon(st)} ${esc(name)}</span>
                    <span class="ht-val ${cls(st)}">${esc(st)}${lat ? ' · ' + lat : ''}${extra ? ' · ' + esc(extra) : ''}</span>
                </div>`;
            }
            rows += `<div class="ht-row" style="margin-top:4px;border-top:1px solid #333;padding-top:4px;">
                <span class="ht-label">Version</span>
                <span class="ht-val ht-ok">${esc(h.version || '?')}</span>
            </div>`;
            tooltip.innerHTML = rows;
        }
    } catch (_) {
        dot.className = 'dot'; dot.style.background = '#e74c3c';
        if (tooltip) tooltip.innerHTML = '<div class="ht-title">🔴 Server unreachable</div>';
    }
}

function updateStatus(s) {
    // Legacy — only used for error fallback in catch blocks
    const el = document.getElementById('globalStatus');
    if (!el) return;
    const dot = el.querySelector('.dot');
    if (s === 'error') {
        dot.className = 'dot'; dot.style.background = '#e74c3c';
    }
}

// ═══════════════ AUTO-REFRESH ═══════════════

function startRefresh() {
    stopRefresh();
    if (app.refreshInterval <= 0) return;
    app.refreshTimer = setInterval(() => refresh(), app.refreshInterval * 1000);
}

function stopRefresh() {
    if (app.refreshTimer) { clearInterval(app.refreshTimer); app.refreshTimer = null; }
}

// ═══════════════ RESIZER ═══════════════

function setupResizer() {
    const resizer = document.getElementById('resizer');
    const livePanel = document.getElementById('livePanel');
    const bankPanel = document.getElementById('bankPanel');
    const panelRight = document.getElementById('panelRight');
    let dragging = false, startY = 0, startLiveH = 0;

    resizer.addEventListener('mousedown', e => {
        e.preventDefault(); dragging = true; startY = e.clientY;
        startLiveH = livePanel.offsetHeight;
        resizer.classList.add('dragging');
        document.body.style.cursor = 'row-resize';
        document.body.style.userSelect = 'none';
    });
    document.addEventListener('mousemove', e => {
        if (!dragging) return;
        const delta = e.clientY - startY;
        const totalH = panelRight.offsetHeight - resizer.offsetHeight;
        const newLiveH = Math.min(totalH - 150, Math.max(200, startLiveH + delta));
        livePanel.style.flex = 'none';
        livePanel.style.height = newLiveH + 'px';
        bankPanel.style.flex = '1';
    });
    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    });
}

// ═══════════════ INIT ═══════════════

document.addEventListener('DOMContentLoaded', () => {
    // LM2-04 migration : purger l'ancien token stocké en localStorage.
    purgeLegacyTokenStorage();

    // Login
    document.getElementById('loginBtn').addEventListener('click', doLogin);
    document.getElementById('loginToken').addEventListener('keydown', e => { if (e.key==='Enter') doLogin(); });
    document.getElementById('logoutBtn').addEventListener('click', doLogout);

    // Sélection espace → chargement auto
    document.getElementById('spaceSelect').addEventListener('change', function() {
        setSpaceInUrl(this.value);
        loadSpace(this.value);
    });

    // Refresh interval
    document.getElementById('refreshInterval').addEventListener('change', function() {
        app.refreshInterval = parseInt(this.value);
        startRefresh();
        const dot = document.querySelector('#globalStatus .dot');
        if (dot) dot.className = app.refreshInterval > 0 ? 'dot' : 'dot paused';
    });

    // Resizer
    setupResizer();

    // Load version from /health (public, no auth needed)
    apiHealth().then(h => {
        const el = document.getElementById('headerVersion');
        if (el && h.version) el.textContent = 'v' + h.version;
    });

    // Go
    checkToken();
});
