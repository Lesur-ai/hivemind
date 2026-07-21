/**
 * Live Memory - API REST avec auth par cookie HttpOnly
 *
 * LM2-04 fix : le token n'est plus stocké côté JS (localStorage exfiltrable
 * par XSS). Le serveur émet un cookie `livemem_auth` HttpOnly via /api/login,
 * automatiquement attaché aux requêtes /api/* par le navigateur. JS ne peut
 * jamais lire le token — un éventuel XSS ne peut donc pas l'exfiltrer.
 *
 * `credentials: 'same-origin'` est le défaut, mais on l'explicite pour
 * la clarté et la robustesse face aux futures évolutions de la spec fetch.
 */

const AUTH_TOKEN_KEY = 'livemem_auth_token';  // ancien storage, à purger

/**
 * Purge l'ancien localStorage hérité (migration LM2-04).
 * Appelé au premier chargement pour nettoyer les tokens encore stockés
 * en clair côté client.
 */
function purgeLegacyTokenStorage() {
    try {
        if (localStorage.getItem(AUTH_TOKEN_KEY)) {
            localStorage.removeItem(AUTH_TOKEN_KEY);
            console.info('[auth] Ancien token localStorage purgé (migration LM2-04).');
        }
    } catch (_) {
        // localStorage indisponible (mode privé strict) : best-effort.
    }
}

/**
 * Authentifie l'utilisateur via le cookie HttpOnly (LM2-04 fix).
 * Le token brut quitte le navigateur uniquement pour ce POST initial,
 * puis est conservé exclusivement côté serveur dans le cookie.
 */
async function loginWithToken(token) {
    const response = await fetch('/api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
    });
    if (response.status === 401) {
        return { status: 'error', message: 'Invalid token' };
    }
    try {
        return await response.json();
    } catch {
        return { status: 'error', message: 'Invalid server response' };
    }
}

/**
 * Destroys the current session (clears the HttpOnly cookie server-side).
 */
async function logout() {
    try {
        await fetch('/api/logout', {
            method: 'POST',
            credentials: 'same-origin',
        });
    } catch (_) {
        // Best-effort : si /api/logout est inaccessible, le cookie expirera
        // de toute façon à la fermeture du navigateur (cookie de session).
    }
}

/**
 * Fetch avec gestion auto du 401 et parsing JSON robuste.
 * Le cookie d'auth est attaché automatiquement par le navigateur
 * (same-origin), pas besoin d'ajouter un header Authorization.
 */
async function authFetch(url, options = {}) {
    options.credentials = options.credentials || 'same-origin';

    let response;
    try {
        response = await fetch(url, options);
    } catch (e) {
        console.error(`[API] Network error: ${url}`, e);
        return { status: 'error', message: 'Network error' };
    }

    if (response.status === 401) {
        showLogin('Session expired.');
        throw new Error('Unauthorized');
    }

    // Parser le JSON de manière robuste (gère les réponses vides/tronquées)
    try {
        const text = await response.text();
        if (!text) return { status: 'error', message: 'Empty server response' };
        return JSON.parse(text);
    } catch (e) {
        console.error(`[API] JSON parse error: ${url}`, e);
        return { status: 'error', message: 'Invalid server response' };
    }
}

// ═══════════════ ENDPOINTS ═══════════════

async function apiLoadSpaces() {
    return await authFetch('/api/spaces');
}

async function apiLoadSpaceInfo(spaceId) {
    return await authFetch(`/api/space/${encodeURIComponent(spaceId)}`);
}

async function apiLoadNotes(spaceId, params = {}) {
    const qs = new URLSearchParams();
    if (params.limit) qs.set('limit', params.limit);
    if (params.agent) qs.set('agent', params.agent);
    if (params.category) qs.set('category', params.category);
    const qsStr = qs.toString();
    return await authFetch(`/api/live/${encodeURIComponent(spaceId)}${qsStr ? '?' + qsStr : ''}`);
}

async function apiLoadBankList(spaceId) {
    return await authFetch(`/api/bank/${encodeURIComponent(spaceId)}`);
}

async function apiLoadBankFile(spaceId, filename) {
    return await authFetch(`/api/bank/${encodeURIComponent(spaceId)}/${encodeURIComponent(filename)}`);
}

async function apiHealth() {
    // /health is public (no auth needed), returns version + service status
    try {
        const r = await fetch('/health');
        return await r.json();
    } catch (_) { return {}; }
}
