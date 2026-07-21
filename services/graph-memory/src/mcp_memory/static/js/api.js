/**
 * MCP Memory - Appels API REST avec authentification Bearer Token.
 *
 * Toutes les requêtes injectent automatiquement le token stocké en localStorage.
 * En cas de 401, redirige vers l'écran de login.
 */

// ═══════════════ GESTION DU TOKEN ═══════════════

const AUTH_TOKEN_KEY = 'mcp_auth_token';

/** Récupère le token depuis localStorage */
function getAuthToken() {
    return localStorage.getItem(AUTH_TOKEN_KEY);
}

/** Stocke le token dans localStorage */
function setAuthToken(token) {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
}

/** Supprime le token de localStorage */
function clearAuthToken() {
    localStorage.removeItem(AUTH_TOKEN_KEY);
}

/** Construit les headers avec le Bearer token */
function authHeaders(extra = {}) {
    const token = getAuthToken();
    const headers = { ...extra };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
}

/**
 * Fetch wrapper qui gère automatiquement le 401.
 * Si la réponse est 401, efface le token et affiche le login.
 */
async function authFetch(url, options = {}) {
    options.headers = authHeaders(options.headers || {});

    const response = await fetch(url, options);

    if (response.status === 401) {
        clearAuthToken();
        showLoginScreen('Session expirée ou token invalide.');
        throw new Error('Unauthorized');
    }

    return response;
}

// ═══════════════ APPELS API ═══════════════

async function apiLoadMemories() {
    const response = await authFetch('/api/memories');
    return await response.json();
}

async function apiLoadGraph(memoryId) {
    const response = await authFetch(`/api/graph/${encodeURIComponent(memoryId)}`);
    return await response.json();
}

async function apiAsk(memoryId, question, limit = 10) {
    const response = await authFetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memory_id: memoryId, question, limit })
    });
    return await response.json();
}

async function apiQuery(memoryId, query, limit = 10) {
    const response = await authFetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memory_id: memoryId, query, limit })
    });
    return await response.json();
}
