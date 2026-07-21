/**
 * Graph Memory Admin API layer.
 * The token is submitted once to /api/login, then stored server-side in
 * an HttpOnly cookie. Tool calls use the cookie with same-origin credentials.
 */

async function adminLogin(token) {
    const response = await fetch('/api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
    });
    if (response.status === 401) return { status: 'error', message: 'Invalid token' };
    try { return await response.json(); } catch { return { status: 'error', message: 'Bad response' }; }
}

async function adminLogout() {
    try { await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); } catch {}
}

async function adminHealth() {
    try {
        const response = await fetch('/health', { credentials: 'same-origin' });
        return await response.json();
    } catch {
        return { status: 'error', message: 'Server unreachable' };
    }
}

async function callTool(toolName, args = {}) {
    const response = await fetch('/api/tool', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: toolName, arguments: args }),
    });
    if (response.status === 401) {
        showLogin('Session expired.');
        throw new Error('Unauthorized');
    }
    const text = await response.text();
    if (!text) return { status: 'error', message: 'Empty response' };
    try { return JSON.parse(text); } catch (e) { return { status: 'error', message: 'Invalid JSON: ' + e.message }; }
}

async function checkSession() {
    try {
        const result = await callTool('system_whoami', {});
        return result.status === 'ok' ? result : null;
    } catch {
        return null;
    }
}
