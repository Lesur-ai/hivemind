/**
 * Hivemind Admin Console — shell entry point
 *
 * Owns: hash router (AdminRouter), view registry (AdminViews), sidebar
 * build/active state, identity block, login/logout, the single delegated
 * [data-action] click switchboard, esc(), and every shared shell component
 * helper listed in contract §2.3.4 (DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md).
 *
 * Escaping contract (binding on every renderer in this file and in every
 * js/admin/views-*.js module):
 *   R1 — every dynamic value interpolated into an innerHTML/template-literal
 *        HTML string passes through esc() at the interpolation site (coerce
 *        non-strings via esc(String(v))). Constant literals are exempt.
 *   R2 — attribute values are esc()'d and the attribute is quoted; data-args
 *        JSON payloads are JSON.stringify(...) then esc(...).
 *   R3 — values read back from element.dataset are ALREADY DECODED: if
 *        reused in HTML they must be re-escaped at the read site.
 *   R4 — server messages render only via serverMessage(), verbatim, never
 *        parsed as HTML/markdown. User-authored Rules and Mid content may use
 *        renderMarkdown(), which requires the vendored Marked + DOMPurify pair.
 *   R5 — prefer textContent for single-value sinks (labels, counters).
 *   R6 — forbidden sinks: document.write, insertAdjacentHTML with
 *        unescaped input, and script-executing URL schemes in
 *        href/src attributes.
 * showModal's bodyHTML parameter is caller-owned HTML (callers must
 * pre-escape any dynamic fragment they embed in it); its title is always
 * escaped by the shell (see showModal below — this is the XSS fix).
 */

// Pinned one-liner (ADM-01) — kept byte-identical, do not rename/split.
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#x27;');

// Trusted rendering boundary for user-authored Markdown only. If either local
// dependency is unavailable, fail closed to escaped plain text instead of
// emitting partially sanitized HTML. Server messages never use this helper.
function renderMarkdown(value) {
    const text = String(value ?? '');
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        return `<pre class="mono-block">${esc(text)}</pre>`;
    }
    try {
        const raw = marked.parse(text, { breaks: true, gfm: true });
        return DOMPurify.sanitize(raw, {
            ALLOWED_TAGS: ['a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'ol', 'p', 'pre', 'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul'],
            ALLOWED_ATTR: ['href', 'title'],
        });
    } catch {
        return `<pre class="mono-block">${esc(text)}</pre>`;
    }
}

// ═══════════════ SHARED FORMATTERS ═══════════════

function fmtSize(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '';
    const n = Number(bytes);
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
}

function _pad2(n) { return String(n).padStart(2, '0'); }

// Mono UTC "YYYY-MM-DD HH:mm" + full-precision title tooltip. On parse
// failure, shows the raw server string — never blank (contract §2.2.2).
function fmtTimestamp(iso) {
    if (!iso) return { text: '', title: '' };
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return { text: String(iso), title: String(iso) };
    const text = `${d.getUTCFullYear()}-${_pad2(d.getUTCMonth() + 1)}-${_pad2(d.getUTCDate())} ${_pad2(d.getUTCHours())}:${_pad2(d.getUTCMinutes())}`;
    return { text, title: String(iso) };
}

// Renders fmtTimestamp() as an HTML fragment with the mono "UTC" unit label
// and the full-precision tooltip. Callers needing the raw parts (rare) can
// call fmtTimestamp() directly instead.
function renderTimestamp(iso) {
    const t = fmtTimestamp(iso);
    if (!t.text) return '<span class="text-faint">—</span>';
    return `<span class="mono-data" title="${esc(t.title)}">${esc(t.text)} <span class="unit-utc">UTC</span></span>`;
}

function truncateMiddle(str, head = 10, tail = 6) {
    const s = String(str || '');
    if (s.length <= head + tail + 1) return s;
    return s.slice(0, head) + '…' + s.slice(s.length - tail);
}

// ═══════════════ ICONOGRAPHY (contract §2.5 — 25 glyphs, static strings) ═══════════════
// Every value below is a constant SVG string: no interpolation of dynamic
// data into markup, so this map is innerHTML-safe by construction.

const ICONS = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="7" r="1.6"/><circle cx="17" cy="7" r="1.6"/><circle cx="7" cy="17" r="1.6"/><circle cx="17" cy="17" r="1.6"/><path d="M8.6 7h6.8M8.6 17h6.8M7 8.6v6.8M17 8.6v6.8"/></svg>',
    spaces: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="12" cy="9.5" r="1.4"/><circle cx="8" cy="14.5" r="1.4"/><circle cx="16" cy="14.5" r="1.4"/><path d="M12 10.9 8 13.4M12 10.9l4 2.5"/></svg>',
    consolidation: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="1.8"/><path d="M4 5.5 10.3 10.8M20 5.5 13.7 10.8M4 18.5 10.3 13.2M20 18.5 13.7 13.2"/></svg>',
    audit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 5h9M6 9.5h9M6 14h6"/><circle cx="17.5" cy="16.5" r="3"/><path d="m16 16.6 1.1 1.1 2-2.1"/></svg>',
    access: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8.5" cy="8.5" r="4"/><path d="M11.2 11.2 19 19M15.5 15.5l1.8-1.8M17.8 17.8l1.8-1.8"/></svg>',
    backups: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="16" height="4.5" rx="1.2"/><path d="M5 8.5v9.5a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 18V8.5"/><path d="M12 12v4.2M9.8 14.3 12 16.5l2.2-2.2"/></svg>',
    maintenance: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="16" r="1.6"/><path d="M9.2 14.8 16 8a2.2 2.2 0 0 0 0-3.1 2.2 2.2 0 0 0-3.1 0l-6.8 6.8"/><path d="m13.8 6.6 3.6 3.6"/></svg>',
    live: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/><path d="M6.6 12h3l1.6-4.5 2.8 9 1.6-4.5h3"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 4.5H6.5A1.5 1.5 0 0 0 5 6v12a1.5 1.5 0 0 0 1.5 1.5H10"/><path d="M15 8l4 4-4 4M19 12H10"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 7h14M5 12h14M5 17h14"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="7" r="1.3"/><circle cx="17" cy="17" r="1.3"/><path d="m8.4 8.4 7.2 7.2M15.6 8.4l-7.2 7.2"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="1.5"/><path d="M18.4 9a6.5 6.5 0 0 0-11-3.2L5 8.2M5.6 15a6.5 6.5 0 0 0 11 3.2l2.4-2.4"/><path d="M5 5v3.4h3.4M19 19v-3.4h-3.4"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
    play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4.5v15l13-7.5z"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 7h14M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/><path d="M7 7l1 12.2A1.5 1.5 0 0 0 9.5 20.5h5a1.5 1.5 0 0 0 1.5-1.3L17 7"/></svg>',
    restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="1.5"/><path d="M6 9.6A6.5 6.5 0 0 1 17.6 7.2l1.4 1.4"/><path d="M19 5v3.6h-3.6"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="7.5" width="10" height="12" rx="1.6"/><path d="M8.5 7.5V6a1.5 1.5 0 0 1 1.5-1.5h8A1.5 1.5 0 0 1 19 6v9.5a1.5 1.5 0 0 1-1.5 1.5H16"/></svg>',
    upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V5M8.2 8.8 12 5l3.8 3.8"/><path d="M5 15.5V18a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 18v-2.5"/></svg>',
    push: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="16.5" r="1.4"/><circle cx="6.5" cy="18.5" r="1.4"/><circle cx="17.5" cy="18.5" r="1.4"/><path d="M12 15.1V6M8.5 9 12 5.5 15.5 9"/><path d="M10.6 15.7 7.6 17.3M13.4 15.7l3 1.6"/></svg>',
    unlink: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 14.5 7 17a3 3 0 0 1-4.2-4.2l2.5-2.5"/><path d="M14.5 9.5 17 7a3 3 0 0 0-4.2-4.2l-2.5 2.5"/><path d="m4 4 16 16"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4 3.5 19.5h17z"/><path d="M12 10.5v4M12 17.3v.1"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5 19 6.5v5.4c0 4.2-2.9 7.3-7 8.6-4.1-1.3-7-4.4-7-8.6V6.5z"/><circle cx="12" cy="11" r="1.4"/></svg>',
    mesh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="1.7"/><circle cx="5" cy="18.5" r="1.7"/><circle cx="19" cy="18.5" r="1.7"/><path d="M12 6.7 6.3 16.9M12 6.7l5.7 10.2M6.7 18.5h10.6"/></svg>',
};

function icon(name) {
    return Object.prototype.hasOwnProperty.call(ICONS, name) ? ICONS[name] : '';
}

// ═══════════════ DATA CACHE / SHARED CONTEXT ═══════════════

const cache = { spaces: [], tokens: [], backups: [], bankFiles: {}, agents: {} };
let _dashHealth = {};
let _currentIdentity = {};
let _epoch = 0;
// Monotonic browser-session ownership token. It is deliberately independent
// from the route epoch: logout / 401 preserves the hash, while every login
// attempt, restored cookie session, logout initiation, and current-session 401
// must still invalidate all continuations and view-local authority caches from
// the previous cookie owner.
let _sessionGeneration = 0;
// Login and logout both mutate the shared HttpOnly cookie. Keep exactly one
// such request in flight: generation guards can discard stale JavaScript
// continuations, but they cannot undo a late HTTP Set-Cookie header.
let _sessionCookieMutationPending = false;

/**
 * Registry-derived UI hint for whether the current permission profile can
 * render an affordance. This never authorizes or suppresses callTool(): the
 * server handler remains authoritative for scopes and conditional guards.
 */
function toolCapabilityHint(toolName, identity = _currentIdentity) {
    const metadata = HIVEMIND_TOOL_CAPABILITIES[toolName];
    if (!metadata) return { known: false, available: false };
    const rank = { public: 0, read: 1, write: 2, manage: 3, admin: 4 };
    const permissions = identity && Array.isArray(identity.permissions)
        ? identity.permissions : [];
    const effective = permissions.reduce((highest, permission) => {
        const candidate = Object.prototype.hasOwnProperty.call(rank, permission)
            ? rank[permission] : -1;
        return Math.max(highest, candidate);
    }, -1);
    return {
        ...metadata,
        known: true,
        available: effective >= rank[metadata.minimum_permission],
    };
}

function currentSessionGeneration() {
    return _sessionGeneration;
}

function sessionGenerationIsCurrent(generation) {
    return generation === _sessionGeneration;
}

function _beginSessionGeneration() {
    _sessionGeneration += 1;
    return _sessionGeneration;
}

function _invalidateSessionGeneration() {
    const generation = _beginSessionGeneration();
    // Existing views already guard post-await effects with the route epoch.
    // Bumping it here extends that protection to every view on logout/401,
    // including modules that do not yet consume ctx.sessionGeneration.
    _epoch += 1;
    return generation;
}

function _beginSessionCookieMutation() {
    if (_sessionCookieMutationPending) return false;
    _sessionCookieMutationPending = true;
    return true;
}

function _endSessionCookieMutation() {
    _sessionCookieMutationPending = false;
    const btn = document.getElementById('loginBtn');
    if (btn) {
        btn.disabled = false;
        btn.textContent = 'Sign in';
    }
}

function _resetCaches() {
    cache.spaces = [];
    cache.tokens = [];
    cache.backups = [];
    cache.bankFiles = {};
    cache.agents = {};
    _dashHealth = {};
    _currentIdentity = {};
}

function _ctx() {
    return {
        epoch: _epoch,
        sessionGeneration: _sessionGeneration,
        identity: _currentIdentity,
        caches: cache,
    };
}

// ═══════════════ VIEW REGISTRY (contract §3.3.1 — names frozen) ═══════════════

const AdminViews = (() => {
    const registry = Object.create(null);
    return {
        register(name, renderFn) {
            registry[name] = renderFn;
        },
        get(name) {
            return Object.prototype.hasOwnProperty.call(registry, name) ? registry[name] : null;
        },
    };
})();

// ═══════════════ HASH ROUTER (contract §3.1.1/§3.3.1 — names frozen) ═══════════════

const SPACE_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const TIERS = new Set(['short', 'mid', 'long']);

function _matchRoute(hash) {
    // hash includes the leading '#'. Strip it; require a leading '/'.
    let raw = hash.startsWith('#') ? hash.slice(1) : hash;
    if (!raw.startsWith('/')) return { view: null, params: {}, raw };

    // Split before decoding (§3.1.2 step 2) so a malformed percent-sequence
    // in one segment cannot corrupt sibling segments.
    const segments = raw.slice(1).split('/');

    if (raw === '/dashboard') return { view: 'dashboard', params: {}, raw };
    if (raw === '/spaces') return { view: 'spaces', params: {}, raw };
    if (raw === '/consolidation') return { view: 'consolidation', params: {}, raw };
    if (raw === '/audit') return { view: 'audit', params: {}, raw };
    if (raw === '/access') return { view: 'access', params: {}, raw };
    if (raw === '/operator/backups') return { view: 'operator', params: { tab: 'backups' }, raw };
    if (raw === '/operator/maintenance') return { view: 'operator', params: { tab: 'maintenance' }, raw };
    if (raw === '/operator') return { view: '__normalize-operator', params: {}, raw };

    if (segments.length === 2 && segments[0] === 'spaces' && segments[1] !== '') {
        return _matchSpaceDetail(segments[1], null, raw);
    }
    if (segments.length === 3 && segments[0] === 'spaces' && segments[1] !== '') {
        return _matchSpaceDetail(segments[1], segments[2], raw);
    }
    if (raw === '/mesh') return { view: 'mesh', params: {}, raw };
    if (segments.length === 2 && segments[0] === 'mesh' && segments[1] !== '') {
        return _matchMeshDetail(segments[1], raw);
    }

    return { view: null, params: {}, raw };
}

function _matchMeshDetail(encodedId, raw) {
    let spaceId;
    try {
        spaceId = decodeURIComponent(encodedId);
    } catch {
        // Same rule as _matchSpaceDetail: only a malformed percent-escape
        // makes the whole route unknown; SPACE_ID_RE validation happens in
        // the Mesh view module, not here.
        return { view: null, params: {}, raw };
    }
    return { view: 'mesh-detail', params: { spaceId }, raw };
}

function _matchSpaceDetail(encodedId, tierSegment, raw) {
    let spaceId;
    try {
        spaceId = decodeURIComponent(encodedId);
    } catch {
        // Only a malformed percent-escape makes the whole route unknown
        // (§3.1.2 step 2) — a decodable-but-invalid id still reaches the
        // space-detail view below, per step 3.
        return { view: null, params: {}, raw };
    }

    // §3.1.2 step 3: SPACE_ID_RE validation happens in the Space Detail
    // module, not here — the router's job is routing, not rejecting.
    // A regex-invalid (but decodable) id still dispatches to
    // 'space-detail' with the raw decoded value in params.spaceId; the
    // view renders its own "invalid space id" state and never calls a
    // tool with it. SPACE_ID_RE/TIERS are exposed at script scope for the
    // view module to reuse.
    if (tierSegment === null) {
        return { view: 'space-detail', params: { spaceId }, raw };
    }
    if (!TIERS.has(tierSegment)) return { view: null, params: {}, raw };
    return { view: 'space-detail', params: { spaceId, tier: tierSegment }, raw };
}

const AdminRouter = (() => {
    let current = { view: null, params: {}, raw: '' };

    function dispatch() {
        const hash = location.hash || '#/dashboard';
        let matched = _matchRoute(hash);

        if (matched.view === '__normalize-operator') {
            location.replace('#/operator/backups');
            return;
        }
        if (!matched.view) {
            if (hash === '#/dashboard' || hash === '' || hash === '#') {
                matched = { view: 'dashboard', params: {}, raw: '/dashboard' };
            } else {
                location.replace('#/dashboard');
                return;
            }
        }

        current = matched;
        _epoch += 1;
        _setActiveNav(matched.view);

        const renderFn = AdminViews.get(matched.view);
        const contentEl = document.getElementById('content');
        if (!contentEl) return;
        if (!renderFn) {
            contentEl.innerHTML = stateUnavailable('This view is not available in this build.');
            return;
        }
        contentEl.focus({ preventScroll: true });
        try {
            renderFn(contentEl, matched.params, _ctx());
        } catch {
            contentEl.innerHTML = stateError({ title: "Couldn't load this view" });
        }
    }

    return {
        go(path) {
            location.hash = '#' + path;
        },
        refresh() {
            dispatch();
        },
        current() {
            return current;
        },
        // §3.3.2 rule 3: views compare a captured ctx.epoch against the
        // *current* AdminRouter.epoch before touching the DOM after an
        // await, so this must read live state, not a dispatch-time snapshot.
        get epoch() {
            return _epoch;
        },
        _dispatch: dispatch,
    };
})();

function _setActiveNav(view) {
    const activeName = view === 'space-detail' ? 'spaces'
        : view === 'mesh-detail' ? 'mesh'
        : view === 'operator' ? (AdminRouter.current().params.tab === 'maintenance' ? 'maintenance' : 'backups')
        : view;
    document.querySelectorAll('.sidebar a[data-nav]').forEach(a => {
        const isActive = a.dataset.nav === activeName;
        a.classList.toggle('active', isActive);
        if (isActive) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
    });
}

// ═══════════════ SHELL STATE-PATTERN COMPONENTS (contract §2.7) ═══════════════

function stateEmpty(opts = {}) {
    const safeTitle = esc(opts.title || 'Nothing here yet');
    const hint = opts.hint ? `<p class="state-hint">${esc(opts.hint)}</p>` : '';
    const action = opts.actionHtml || '';
    return `<div class="state state-empty">${icon('spaces')}<h3>${safeTitle}</h3>${hint}${action}</div>`;
}

function stateLoading(label) {
    const text = esc(label || 'Loading…');
    return `<div class="state state-loading"><span class="spinner" aria-hidden="true"></span><span>${text}</span></div>`;
}

function stateError(opts = {}) {
    const safeTitle = esc(opts.title || "Couldn't load this data");
    const msg = opts.message ? serverMessage(opts.message) : '';
    const retryAttr = opts.retryAction ? ` data-action="${esc(opts.retryAction)}"` : '';
    const retry = opts.retryAction ? `<button class="btn btn-secondary btn-sm"${retryAttr}>Retry</button>` : '';
    return `<div class="state state-error" role="alert">${icon('alert')}<h3>${safeTitle}</h3>${msg}${retry}</div>`;
}

function stateUnavailable(reason) {
    const text = esc(reason || 'This data is not available.');
    return `<div class="state state-unavailable"><span class="micro-label">NOT AVAILABLE</span><p>${text}</p></div>`;
}

// Verbatim server text — never parsed, never rendered as
// HTML/markdown (R4). msg is escaped once here at the sink.
function serverMessage(msg) {
    if (!msg) return '';
    return `<div class="server-msg"><span class="server-msg-label">SERVER MESSAGE</span><span class="server-msg-text" lang="en">${esc(String(msg))}</span></div>`;
}

// ═══════════════ SHELL LAYOUT COMPONENTS (contract §2.3.4) ═══════════════

function pageHeader(title, actionsHtml = '') {
    return `<div class="page-header"><h1>${esc(title)}</h1><div class="page-header-actions">${actionsHtml}</div></div>`;
}

function panel(bodyHtml) {
    return `<div class="panel">${bodyHtml}</div>`;
}

function dataTable(headers, rowsHtml) {
    const thead = headers.map(h => `<th scope="col">${esc(h)}</th>`).join('');
    return `<div class="table-scroll"><table class="data-table"><thead><tr>${thead}</tr></thead><tbody>${rowsHtml}</tbody></table></div>`;
}

function statusDot(severity, label) {
    const sev = ['ok', 'warn', 'error', 'neutral'].includes(severity) ? severity : 'neutral';
    return `<span class="status-dot-wrap"><span class="status-dot dot-${sev}"></span><span class="status-dot-label">${esc(String(label ?? ''))}</span></span>`;
}

function pill(kind, label) {
    const k = ['ok', 'warn', 'error', 'neutral'].includes(kind) ? kind : 'neutral';
    return `<span class="pill pill-${k}">${esc(String(label ?? ''))}</span>`;
}

function copyable(value, display) {
    const full = String(value ?? '');
    const shown = display !== undefined ? String(display) : full;
    const payload = esc(JSON.stringify(full));
    return `<span class="mono-chip"><span class="mono-chip-value">${esc(shown)}</span><button type="button" class="copy-btn" data-action="copy-value" data-value="${payload}" aria-label="Copy ${esc(shown)}">${icon('copy')}</button></span>`;
}

function monoBlock(text) {
    return `<div class="mono-block">${esc(String(text ?? ''))}</div>`;
}

// ═══════════════ ACTION DELEGATION EXTENSION POINT (contract §2.3.4) ═══════════════

const _actionHandlers = Object.create(null);

function registerAction(name, handler) {
    _actionHandlers[name] = handler;
}

registerAction('close-modal', () => closeModal());
registerAction('copy-value', (data) => {
    let value = '';
    try { value = JSON.parse(data.value || '""'); } catch { value = data.value || ''; }
    _copyText(value);
});

// ═══════════════ GENERIC RUN ACTION (contract §4 row S2 — kept, restyled
// per §2; same POST /api/tool path via callTool(), same TOOL_TITLES
// mechanism) ═══════════════

const TOOL_TITLES = {
    space_list: 'Spaces',
    space_info: 'Space info',
    space_create: 'Space created',
    space_delete: 'Space deleted',
    space_rules: 'Space rules',
    space_summary: 'Space summary',
    space_update_rules: 'Rules updated',
    admin_list_tokens: 'Tokens',
    admin_create_token: 'Token created',
    admin_update_token: 'Token updated',
    admin_revoke_token: 'Token revoked',
    admin_delete_token: 'Token deleted',
    admin_purge_tokens: 'Tokens purged',
    admin_gc_notes: 'Garbage collection',
    admin_audit_recent: 'Recent audit entries',
    live_read: 'Live notes',
    bank_list: 'Bank files',
    bank_read: 'Bank file',
    bank_consolidate: 'Consolidation',
    bank_consolidation_status: 'Consolidation status',
    bank_consolidation_queues: 'Consolidation queues',
    bank_compact: 'Compact result',
    bank_repair: 'Repair result',
    bank_stale_spaces: 'Stale spaces',
    backup_create: 'Backup created',
    backup_list: 'Backups',
    backup_restore: 'Backup restored',
    backup_delete: 'Backup deleted',
    graph_status: 'Long tier status',
    graph_push: 'Long tier push',
    graph_disconnect: 'Long tier disconnected',
    system_health: 'System health',
    system_whoami: 'Identity',
};

// Generic result renderer for the shared "run" action — restyled with the
// §2 component set. Views with a dedicated surface (e.g. Space Detail,
// Consolidation) render their own tool results directly; this generic path
// covers the rest, same role as the inherited renderPretty()/renderJSON().
function renderToolResult(data) {
    if (data && typeof data.message === 'string' && data.message) {
        return serverMessage(data.message);
    }
    if (data && Array.isArray(data.items) && data.items.length) {
        const keys = Object.keys(data.items[0]);
        const rows = data.items
            .map(item => `<tr>${keys.map(k => `<td>${monoBlock(typeof item[k] === 'string' ? item[k] : JSON.stringify(item[k]))}</td>`).join('')}</tr>`)
            .join('');
        return dataTable(keys, rows);
    }
    return monoBlock(JSON.stringify(data, null, 2));
}

function runAndShow(tool, args) {
    const title = TOOL_TITLES[tool] || tool;
    // §3.3.2 rule 3: capture the epoch before the await and drop both the
    // success and error continuations if the operator has navigated away
    // in the meantime — a stale result must never paint over a new view.
    const epochAtCall = AdminRouter.epoch;
    showModal(title, stateLoading(''));
    callTool(tool, args || {})
        .then(result => {
            if (AdminRouter.epoch !== epochAtCall) return;
            showModal(title, renderToolResult(result));
        })
        .catch(() => {
            if (AdminRouter.epoch !== epochAtCall) return;
            showModal(title, stateError({ title: 'Request failed' }));
        });
}

registerAction('run', (data) => {
    let args = {};
    try { args = JSON.parse(data.args || '{}'); } catch { args = {}; }
    runAndShow(data.tool, args);
});

function _copyText(text) {
    const done = () => showToast('ok', 'Copied');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => _copyFallback(text, done));
    } else {
        _copyFallback(text, done);
    }
}

function _copyFallback(text, done) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch { /* no-op */ }
    document.body.removeChild(ta);
}

// ═══════════════ GLOBAL EVENT DELEGATION (CSP-safe, data-action switchboard) ═══════════════

document.addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const handler = Object.prototype.hasOwnProperty.call(_actionHandlers, action) ? _actionHandlers[action] : null;
    if (!handler) return;
    e.preventDefault();
    handler(btn.dataset, btn);
});

// ═══════════════ MODAL (single #adminModal architecture, contract §2.4.6) ═══════════════

// showModal(title, bodyHTML, btnLabel, onConfirm)
//   title    — plain text, ALWAYS escaped by the shell (this is the XSS fix).
//   bodyHTML — caller-owned HTML. Callers must esc() any dynamic fragment
//              they interpolate into it before passing it here.
//   btnLabel — plain text, escaped by the shell.
//   onConfirm — async () => boolean; truthy return closes the modal.
//
// `_modalReturnFocus` is intentionally owned by the shell instead of a view:
// views are free to replace one modal body with another (for example, the
// Mesh invitation form with its one-time-code acknowledgement), but closing
// the eventual dialog must return a keyboard user to the control that opened
// the flow.  Capture it only when a previously closed dialog is opened.
let _modalReturnFocus = null;

function showModal(title, bodyHTML, btnLabel, onConfirm) {
    let m = document.getElementById('adminModal');
    if (!m) {
        m = document.createElement('div');
        m.id = 'adminModal';
        m.className = 'modal-overlay';
        document.body.appendChild(m);
    }
    if (m.style.display !== 'flex') {
        _modalReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    }
    const footer = btnLabel
        ? `<div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal">Cancel</button><button class="btn btn-primary" id="modalConfirmBtn">${esc(btnLabel)}</button></div>`
        : '';
    m.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
        <div class="modal-header"><h3 id="modalTitle">${esc(title)}</h3><button class="modal-close" type="button" data-action="close-modal" aria-label="Close">${icon('close')}</button></div>
        <div class="modal-body">${bodyHTML}</div>
        ${footer}
    </div>`;
    m.style.display = 'flex';
    const firstFocusable = m.querySelector('input, textarea, select, button');
    if (firstFocusable) firstFocusable.focus();

    // The close action is the single cleanup path.  In particular, Mesh's
    // one-time invitation display attaches its secret-destruction handler to
    // this control, so Escape must invoke the control rather than hiding the
    // overlay directly.
    m.onkeydown = event => {
        if (event.key !== 'Escape') return;
        const close = m.querySelector('[data-action="close-modal"]');
        if (!close) return;
        event.preventDefault();
        close.click();
    };

    if (btnLabel && onConfirm) {
        const confirmBtn = document.getElementById('modalConfirmBtn');
        confirmBtn.addEventListener('click', async () => {
            confirmBtn.disabled = true;
            const original = confirmBtn.textContent;
            confirmBtn.textContent = 'Working…';
            try {
                const ok = await onConfirm();
                if (ok) closeModal();
            } finally {
                if (confirmBtn.isConnected) {
                    confirmBtn.disabled = false;
                    confirmBtn.textContent = original;
                }
            }
        });
    }
}

function closeModal() {
    const m = document.getElementById('adminModal');
    if (m) m.style.display = 'none';
    const returnFocus = _modalReturnFocus;
    _modalReturnFocus = null;
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
}

// showDestructiveModal({title, bodyHtml, verb, danger, typedConfirmation})
// Typed-confirmation destructive variant (contract §2.4.6/D10): the Danger
// confirm button stays disabled until the input strictly (case-sensitive)
// equals typedConfirmation.
function showDestructiveModal(opts) {
    const { title, bodyHtml, verb, typedConfirmation, onConfirm } = opts;
    const challenge = esc(String(typedConfirmation ?? ''));
    const body = `
        <div class="destructive-summary">${bodyHtml || ''}</div>
        <div class="form-group">
            <label class="form-label" for="destructiveConfirmInput">Type <code class="typed-challenge">&quot;${challenge}&quot;</code> to confirm</label>
            <input class="form-input mono" id="destructiveConfirmInput" autocomplete="off" data-1p-ignore data-lpignore="true">
        </div>`;
    showModal(title, body, verb || 'Delete', async () => onConfirm && onConfirm());

    // showModal()'s 4-arg signature is frozen (§2.3.4) and always renders a
    // neutral primary confirm button — the destructive variant (§2.4.6,
    // §7.4.4: Critical Red family, alert-icon header) is applied here as a
    // post-render adjustment, without touching showModal itself.
    const header = document.querySelector('#adminModal .modal-header');
    const headerTitle = document.getElementById('modalTitle');
    if (header && headerTitle && !header.querySelector('.modal-header-icon')) {
        header.classList.add('modal-header--danger');
        const iconSpan = document.createElement('span');
        iconSpan.className = 'modal-header-icon';
        iconSpan.setAttribute('aria-hidden', 'true');
        iconSpan.innerHTML = icon('alert');
        header.insertBefore(iconSpan, headerTitle);
    }

    const input = document.getElementById('destructiveConfirmInput');
    const confirmBtn = document.getElementById('modalConfirmBtn');
    if (!input || !confirmBtn) return;
    confirmBtn.classList.remove('btn-primary');
    confirmBtn.classList.add('btn-danger');
    confirmBtn.disabled = true;
    input.addEventListener('input', () => {
        confirmBtn.disabled = input.value !== String(typedConfirmation ?? '');
    });
}

// ═══════════════ TOASTS (contract §2.4.8) ═══════════════

function showToast(kind, text) {
    const stack = document.getElementById('toastStack');
    if (!stack) return;
    const sev = ['ok', 'warn', 'error'].includes(kind) ? kind : 'ok';
    const el = document.createElement('div');
    el.className = `toast toast-${sev}`;
    if (sev === 'error') el.setAttribute('role', 'alert');
    el.innerHTML = `${icon(sev === 'error' ? 'alert' : 'check')}<span class="toast-text">${esc(String(text ?? ''))}</span><button type="button" class="toast-close" aria-label="Dismiss">${icon('close')}</button>`;
    el.querySelector('.toast-close').addEventListener('click', () => el.remove());
    stack.appendChild(el);
    while (stack.children.length > 3) stack.removeChild(stack.firstElementChild);
    if (sev !== 'error') {
        setTimeout(() => { if (el.isConnected) el.remove(); }, 5000);
    }
}

// ═══════════════ LOGIN / LOGOUT / SESSION WIPE ═══════════════

function showLogin(msg = '') {
    // Invalidate synchronously, before any DOM work. This is also called by the
    // current-session 401 path in admin-api.js.
    _invalidateSessionGeneration();
    wipeSession();
    const overlay = document.getElementById('loginOverlay');
    const err = document.getElementById('loginError');
    overlay.classList.remove('hidden');
    err.textContent = msg || '';
    document.getElementById('loginToken').focus();
    const btn = document.getElementById('loginBtn');
    if (btn) {
        // A 401 can show the overlay while a login response is still pending.
        // Do not permit another login until its Set-Cookie opportunity ends.
        btn.disabled = _sessionCookieMutationPending;
        if (!_sessionCookieMutationPending) btn.textContent = 'Sign in';
    }
}

function hideLogin() {
    document.getElementById('loginOverlay').classList.add('hidden');
}

// Logout / session-expiry content-wipe rule (contract §3.1.4, exact 5-item
// list). Triggered from showLogin(), which runs on explicit logout AND on
// any current-generation 401 from /api/tool. The hash is deliberately NOT
// touched here; stale 401 responses reject only their original caller.
function wipeSession() {
    const content = document.getElementById('content');
    if (content) content.innerHTML = stateLoading('');

    _resetCaches();
    _setMeshNavVisible(false);

    const identityBlock = document.getElementById('identityBlock');
    if (identityBlock) identityBlock.replaceChildren();

    const modal = document.getElementById('adminModal');
    if (modal) modal.remove();

    const toastStack = document.getElementById('toastStack');
    if (toastStack) toastStack.replaceChildren();
}

async function doLogin() {
    const input = document.getElementById('loginToken');
    const btn = document.getElementById('loginBtn');
    const err = document.getElementById('loginError');
    // The Enter-key handler calls doLogin() directly, so the disabled button
    // must also be enforced here while login/logout owns the session cookie.
    if (btn.disabled) return;
    const token = input.value.trim();
    if (!token) { err.textContent = 'Token required.'; return; }
    if (!_beginSessionCookieMutation()) return;
    // A login attempt is a new ownership generation even when the operator
    // authenticates as the same client_name. Object identity and display name
    // are not session boundaries.
    const sessionGeneration = _beginSessionGeneration();
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    err.textContent = '';
    try {
        const r = await adminLogin(token);
        if (!sessionGenerationIsCurrent(sessionGeneration)) return;
        if (r.status !== 'ok') {
            err.textContent = r.message || 'Invalid token.';
            return;
        }
        hideLogin();
        input.value = '';
        await _bootAuthenticated();
    } catch {
        if (!sessionGenerationIsCurrent(sessionGeneration)) return;
        err.textContent = 'Server unreachable.';
    } finally {
        // The cookie-mutation lock prevents a newer login/logout request from
        // existing here, so releasing it is safe even if this generation was
        // superseded by a 401 while the login response was pending.
        _endSessionCookieMutation();
    }
}

async function doLogout() {
    if (!_beginSessionCookieMutation()) return;
    // Invalidate and wipe BEFORE awaiting the network request. No continuation
    // can cross the logout boundary while POST /api/logout is in flight.
    showLogin();
    const btn = document.getElementById('loginBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Signing out…';
    }
    try {
        await adminLogout();
    } finally {
        // Keep login disabled until the old cookie has been cleared, preventing
        // a late logout response from racing a new login cookie into oblivion.
        _endSessionCookieMutation();
    }
}

// ═══════════════ SIDEBAR / IDENTITY ═══════════════

const NAV_PRIMARY = [
    { name: 'dashboard', label: 'Dashboard', href: '#/dashboard' },
    { name: 'spaces', label: 'Spaces', href: '#/spaces' },
    { name: 'consolidation', label: 'Consolidation', href: '#/consolidation' },
    { name: 'audit', label: 'Audit', href: '#/audit' },
    { name: 'access', label: 'Access', href: '#/access' },
];

// Net-new P10-4 nav entry. Rendered ONLY when the session probes admin AND
// GET /api/admin/mesh/status succeeds (see _refreshMeshNav) — per the design
// pack, Mesh navigation is absent, never a disabled item that would fire a
// request the session/instance cannot serve.
const NAV_MESH = { name: 'mesh', label: 'Mesh', href: '#/mesh' };

const NAV_OPERATOR = [
    { name: 'backups', label: 'Backups', href: '#/operator/backups' },
    { name: 'maintenance', label: 'Maintenance', href: '#/operator/maintenance' },
];

function _navItemHtml(item) {
    return `<li><a href="${esc(item.href)}" data-nav="${esc(item.name)}" class="nav-item">${icon(item.name)}<span class="nav-label">${esc(item.label)}</span></a></li>`;
}

let _meshNavVisible = false;

function buildSidebar() {
    const nav = document.getElementById('sidebarNav');
    if (nav) nav.innerHTML = NAV_PRIMARY.concat(_meshNavVisible ? [NAV_MESH] : []).map(_navItemHtml).join('');
    const navOp = document.getElementById('sidebarNavOperator');
    if (navOp) navOp.innerHTML = NAV_OPERATOR.map(_navItemHtml).join('');
}

// Exposed for view modules (e.g. views-space-detail.js's Mesh link) that
// need to know, synchronously at render time, whether this boot's Mesh
// capability probe succeeded — never re-derived or cached per-view.
function meshIsAvailable() {
    return _meshNavVisible;
}

// Rebuilds the whole primary nav list from NAV_PRIMARY (+ NAV_MESH when
// visible) rather than mutating the DOM incrementally — same rebuild-from-
// source-of-truth idiom as buildSidebar() itself, and avoids the forbidden
// insertAdjacentHTML sink (contract R6).
function _setMeshNavVisible(visible) {
    if (_meshNavVisible === visible) return;
    _meshNavVisible = visible;
    buildSidebar();
    _setActiveNav(AdminRouter.current().view);
}

// Load-time-only capability probe (contract §5.0 no-automatic-polling rule:
// load / manual / after-action). Re-runs every _bootAuthenticated() call,
// i.e. once per login, never on a timer.
async function _refreshMeshNav(sessionGeneration) {
    const isAdmin = (_currentIdentity.permissions || []).includes('admin');
    if (!isAdmin) {
        _setMeshNavVisible(false);
        return;
    }
    const status = await meshAdminStatus();
    if (!sessionGenerationIsCurrent(sessionGeneration)) return;
    _setMeshNavVisible(!!status);
}

function renderIdentityBlock(identity) {
    const el = document.getElementById('identityBlock');
    if (!el) return;
    if (!identity || !identity.client_name) {
        el.innerHTML = `<div class="identity-unavailable">${stateUnavailable('Identity unavailable.')}<button type="button" class="icon-btn" id="logoutBtn" aria-label="Sign out">${icon('logout')}</button></div>`;
        _wireLogoutButton();
        return;
    }
    const perms = (identity.permissions || []).map(p => `<span class="chip">${esc(String(p))}</span>`).join('');
    const authType = esc(identity.auth_type || 'unknown');
    const expiresChip = identity.expires_at
        ? `<span class="chip chip-expiry" title="${esc(fmtTimestamp(identity.expires_at).title)}">expires ${esc(fmtTimestamp(identity.expires_at).text)} UTC</span>`
        : '';
    el.innerHTML = `
        <div class="identity-row">
            <span class="identity-name" title="${esc(identity.client_name)}">${esc(identity.client_name)}</span>
            <button type="button" class="icon-btn" id="logoutBtn" aria-label="Sign out">${icon('logout')}</button>
        </div>
        <div class="identity-chips">
            <span class="chip chip-auth">${authType}</span>
            ${perms}
            ${expiresChip}
        </div>`;
    _wireLogoutButton();
}

function _wireLogoutButton() {
    const btn = document.getElementById('logoutBtn');
    if (btn) btn.addEventListener('click', doLogout);
}

// ═══════════════ NARROW DRAWER (contract §2.6) ═══════════════

function _wireDrawer() {
    const toggle = document.getElementById('sidebarMenuToggle');
    const sidebar = document.getElementById('sidebar');
    const scrim = document.getElementById('sidebarScrim');
    if (!toggle || !sidebar || !scrim) return;

    function openDrawer() {
        sidebar.classList.add('sidebar--drawer-open');
        scrim.classList.add('visible');
        toggle.setAttribute('aria-expanded', 'true');
        const firstLink = sidebar.querySelector('a.nav-item');
        if (firstLink) firstLink.focus();
    }
    function closeDrawer() {
        const wasOpen = sidebar.classList.contains('sidebar--drawer-open');
        sidebar.classList.remove('sidebar--drawer-open');
        scrim.classList.remove('visible');
        toggle.setAttribute('aria-expanded', 'false');
        if (wasOpen) toggle.focus();
    }

    toggle.addEventListener('click', () => {
        if (sidebar.classList.contains('sidebar--drawer-open')) closeDrawer();
        else openDrawer();
    });
    scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && sidebar.classList.contains('sidebar--drawer-open')) closeDrawer();
    });
    window.addEventListener('hashchange', closeDrawer);
}

// ═══════════════ BOOT SEQUENCE ═══════════════

// §5.0 global sentinel statuses callTool() can return instead of a real
// tool payload — the shell must show their specific, mandated copy rather
// than falling through to the generic "Identity unavailable" state.
const CALLTOOL_SENTINEL_STATUSES = new Set(['read_only', 'rate_limited', 'truncated']);

async function _bootAuthenticated() {
    const sessionGeneration = currentSessionGeneration();
    buildSidebar();
    let whoami = {};
    try {
        whoami = await callTool('system_whoami', {});
    } catch {
        whoami = {};
    }

    // system_whoami may resolve after logout, a current-session 401, or a new
    // login. Never restore identity/DOM or dispatch a route across that edge.
    if (!sessionGenerationIsCurrent(sessionGeneration)) return;

    if (whoami && CALLTOOL_SENTINEL_STATUSES.has(whoami.status)) {
        // §7.1.4 read-only tokens (and, defensively, a rate-limited/truncated
        // boot response): render the exact server-facing message and do not
        // dispatch a route — "No repeated probing" (§5.0).
        _currentIdentity = {};
        renderIdentityBlock(_currentIdentity);
        const contentEl = document.getElementById('content');
        if (contentEl) {
            contentEl.innerHTML = `<div class="page">${pageHeader('Access blocked')}${panel(stateUnavailable(whoami.message))}</div>`;
        }
        return;
    }

    _currentIdentity = whoami && whoami.client_name ? whoami : {};
    renderIdentityBlock(_currentIdentity);
    // Fire-and-forget: the nav item appears as soon as the probe resolves,
    // never blocking the first route dispatch.
    _refreshMeshNav(sessionGeneration);
    AdminRouter._dispatch();
}

window.addEventListener('hashchange', () => AdminRouter._dispatch());

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('loginBtn').addEventListener('click', doLogin);
    document.getElementById('loginToken').addEventListener('keydown', e => {
        if (e.key === 'Enter') doLogin();
    });
    _wireDrawer();

    adminHealth().then(h => {
        if (!h.version) return;
        const footerEl = document.getElementById('headerVersion');
        if (footerEl) footerEl.textContent = 'v' + h.version;
        const loginEl = document.getElementById('loginVersion');
        if (loginEl) loginEl.textContent = 'v' + h.version;
    });

    const menuToggle = document.getElementById('sidebarMenuToggle');
    if (menuToggle) {
        const slot = menuToggle.querySelector('.menu-icon-slot');
        if (slot) slot.innerHTML = icon('menu');
    }
    const liveIconSlot = document.querySelector('.nav-live-icon-slot');
    if (liveIconSlot) liveIconSlot.innerHTML = icon('live');

    const probeSessionGeneration = currentSessionGeneration();
    checkSession().then(async data => {
        // A manual login can start while the initial cookie probe is pending.
        // The stale probe must not hide or re-show the overlay afterward.
        if (!sessionGenerationIsCurrent(probeSessionGeneration)) return;
        if (data) {
            _beginSessionGeneration();
            hideLogin();
            await _bootAuthenticated();
        } else {
            showLogin();
        }
    });
});
