/**
 * Live Memory Admin — API layer (cookie HttpOnly auth, same as /live)
 *
 * Global client rules (contract §5.0): callTool() is the single funnel for
 * every /api/tool call. Before any caller reads `status`, it applies, in
 * order: (1) the 512 KB truncation guard, (2) the 429 rate-limit guard,
 * (3) the read-only 403 guard. The 401 and response paths are also bound to
 * the browser-session generation that started the request.
 */

async function adminLogin(token) {
    const r = await fetch('/api/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
    });
    if (r.status === 401) return { status: 'error', message: 'Invalid token' };
    try { return await r.json(); } catch { return { status: 'error', message: 'Bad response' }; }
}

async function adminLogout() {
    try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); } catch {}
}

async function adminHealth() {
    try { const r = await fetch('/health'); return await r.json(); } catch { return {}; }
}

/**
 * Call an MCP tool via POST /api/tool.
 * Auth cookie is attached automatically by the browser.
 */
async function callTool(toolName, args = {}) {
    // Bind this request to the exact authenticated browser session that
    // launched it. A response can arrive after logout + re-login, when the
    // HttpOnly cookie already belongs to a different operator. In particular,
    // a late 401 from the old request must not wipe the newer session.
    const requestSessionGeneration = currentSessionGeneration();
    let r;
    try {
        r = await fetch('/api/tool', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tool: toolName, arguments: args }),
        });
    } catch (e) {
        if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
            throw new Error('Stale session');
        }
        throw e;
    }

    if (r.status === 401) {
        if (sessionGenerationIsCurrent(requestSessionGeneration)) {
            showLogin('Session expired.');
        }
        throw new Error('Unauthorized');
    }

    // A successful or typed-error response from an older cookie owner is just
    // as stale as an old 401. Drop it at the shared funnel so even a view that
    // forgets its own post-await guard cannot consume cross-session data.
    if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
        throw new Error('Stale session');
    }

    // §5.0 truncation guard — checked before any status/JSON handling.
    // ResponseLimitMiddleware injects either the header or (when it still
    // manages to emit a JSON body) the _truncated flag.
    if (r.headers.get('X-Response-Truncated') === 'true') {
        return {
            status: 'truncated',
            message: "Response exceeded the console's 512 KB limit — use an MCP client for this operation.",
        };
    }

    // §5.0 rate-limit guard — no automatic retry, ever.
    if (r.status === 429) {
        return {
            status: 'rate_limited',
            message: 'Rate limited by the gateway — retry in a moment',
        };
    }

    let text;
    try {
        text = await r.text();
    } catch (e) {
        if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
            throw new Error('Stale session');
        }
        return { status: 'error', message: 'Invalid JSON: ' + e.message };
    }
    if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
        throw new Error('Stale session');
    }
    if (!text) return { status: 'error', message: 'Empty response' };

    let body;
    try {
        body = JSON.parse(text);
    } catch (e) {
        return { status: 'error', message: 'Invalid JSON: ' + e.message };
    }

    // §5.0 truncation guard, body-flag variant (belt-and-suspenders with
    // the header check above — either signal alone must trip this).
    if (body && body._truncated === true) {
        return {
            status: 'truncated',
            message: "Response exceeded the console's 512 KB limit — use an MCP client for this operation.",
        };
    }

    // §5.0 read-only guard — ADM-06 shape: 403 + "write" in the message.
    if (r.status === 403 && typeof body.message === 'string' && body.message.toLowerCase().includes('write')) {
        return {
            status: 'read_only',
            message: 'This token is read-only. The admin console requires write permission — use /live for read-only viewing.',
        };
    }

    return body;
}

/**
 * Check if current session is valid (cookie present & working).
 */
async function checkSession() {
    try {
        const r = await fetch('/api/spaces', { credentials: 'same-origin' });
        if (r.status === 401) return null;
        const data = await r.json();
        return data.status === 'ok' ? data : null;
    } catch { return null; }
}

/**
 * Project Mesh admin control plane — /api/admin/mesh/* (P10-4). A distinct
 * REST surface from /api/tool: mesh_admin.py's own docstring is explicit
 * that the console consumes this "never an MCP mesh_* tool" (no ADM-06
 * read-only-token shape — mesh_admin.py has no such concept), but it is
 * bound to the browser-session generation exactly like callTool(), and
 * ResponseLimitMiddleware wraps every route including /api/admin/mesh/*
 * (src/live_mem/server.py: applied after MeshAdminMiddleware in the stack,
 * so it is the outermost layer), so the same truncation guard callTool()
 * applies is required here too — a large pairing/members list can be cut by
 * the shared 512 KB limit exactly like any other response.
 */
async function _meshFetch(path, opts = {}) {
    // Bind this request to the exact authenticated browser session that
    // launched it — same race callTool() guards against: a Mesh request from
    // session A can resolve with 401 after logout + re-login to session B,
    // and a bare showLogin() here would wipe B for A's stale failure.
    const requestSessionGeneration = currentSessionGeneration();
    let r;
    try {
        r = await fetch('/api/admin/mesh/' + path, { credentials: 'same-origin', ...opts });
    } catch {
        if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
            return { status: 'error', message: 'Stale session' };
        }
        return { status: 'error', message: 'Server unreachable' };
    }
    if (r.status === 401) {
        if (sessionGenerationIsCurrent(requestSessionGeneration)) {
            showLogin('Session expired.');
        }
        return { status: 'error', message: 'Unauthorized' };
    }
    // A successful or typed-error response from an older cookie owner is just
    // as stale as an old 401 — drop it here so a caller cannot consume
    // cross-session data even if it forgets its own post-await guard.
    if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
        return { status: 'error', message: 'Stale session' };
    }
    // §5.0 truncation guard (header) — checked before any status/JSON handling,
    // mirroring callTool().
    if (r.headers.get('X-Response-Truncated') === 'true') {
        return {
            status: 'truncated',
            message: "Response exceeded the console's 512 KB limit — use an MCP client for this operation.",
        };
    }
    let body;
    try {
        body = await r.json();
    } catch {
        return { status: 'error', message: 'Invalid response' };
    }
    if (!sessionGenerationIsCurrent(requestSessionGeneration)) {
        return { status: 'error', message: 'Stale session' };
    }
    // §5.0 truncation guard (body-flag variant, belt-and-suspenders).
    if (body && body._truncated === true) {
        return {
            status: 'truncated',
            message: "Response exceeded the console's 512 KB limit — use an MCP client for this operation.",
        };
    }
    return body;
}

/** GET /api/admin/mesh/status. Returns null on any failure (network error,
 * non-JSON body, or a non-'ok' status) — callers use this as the single
 * "is Mesh reachable and enabled for this session" signal, never a
 * fabricated {enabled:false} shape (the endpoint itself never emits one:
 * disabled Mesh means the route doesn't exist at all). */
async function meshAdminStatus() {
    const body = await _meshFetch('status');
    return body && body.status === 'ok' ? body : null;
}

/** GET /api/admin/mesh/members/<spaceId>. */
async function meshAdminMembers(spaceId) {
    return _meshFetch('members/' + encodeURIComponent(spaceId));
}

/** POST /api/admin/mesh/<action> with an explicit confirm:true (every
 * mutating Mesh admin action requires it server-side). */
async function meshAdminAction(action, args = {}) {
    return _meshFetch(action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true, ...args }),
    });
}
