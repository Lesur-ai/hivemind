/**
 * Graph Memory Admin.
 * CSP-safe: all interactions are handled through event delegation.
 */

const esc = value => String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');

const fmtDate = iso => {
    if (!iso) return '';
    try { return new Date(iso).toLocaleString('en-US', { dateStyle: 'short', timeStyle: 'short' }); }
    catch { return iso; }
};

const fmtSize = bytes => {
    const n = Number(bytes || 0);
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
};

const fmtInt = value => {
    const n = Number(value || 0);
    return Number.isFinite(n) ? n.toLocaleString('en-US') : '0';
};

const attrJson = obj => esc(JSON.stringify(obj));
const gv = id => (document.getElementById(id)?.value || '').trim();
const rawValue = id => document.getElementById(id)?.value || '';
const checked = id => Boolean(document.getElementById(id)?.checked);
const isSuccess = result => ['ok', 'created', 'deleted', 'restored'].includes(result?.status);
const resultStore = new Map();
let resultSeq = 0;

function storeResult(title, result, options = {}) {
    resultSeq += 1;
    const id = `result-${resultSeq}`;
    resultStore.set(id, { title, result, options });
    return id;
}

function summarizeValue(value) {
    if (value === null || value === undefined || value === '') return '';
    if (Array.isArray(value)) return `${value.length} item${value.length > 1 ? 's' : ''}`;
    if (typeof value === 'object') {
        const count = Object.keys(value).length;
        return `${count} field${count > 1 ? 's' : ''}`;
    }
    return String(value);
}

function resultSummaryRows(result) {
    const keys = [
        'status', 'message', 'mode', 'scope', 'memory_id', 'memory_name', 'document_id',
        'backup_id', 'name', 'filename', 'count', 'created_count', 'error_count',
        'requested_count', 'elapsed_seconds', 'size_bytes',
    ];
    return keys
        .filter(key => Object.prototype.hasOwnProperty.call(result || {}, key))
        .map(key => `<tr><th>${esc(key)}</th><td>${esc(summarizeValue(result[key]))}</td></tr>`)
        .join('');
}

function querySummaryHtml(result) {
    // Rendu dédié des résultats memory_query : expose source_path / repo_path
    // (chemin Git canonique) pour aligner /admin sur le CLI mcp_cli.
    if (!result || (!Array.isArray(result.rag_chunks) && !Array.isArray(result.source_documents))) return '';
    const canonicalPath = (o) => o && (o.repo_path || o.source_path) || '';
    let html = '';

    const docs = Array.isArray(result.source_documents) ? result.source_documents : [];
    if (docs.length) {
        const rows = docs.map(d => {
            const path = canonicalPath(d);
            const status = d.ingestion_status && d.ingestion_status !== 'unknown' ? d.ingestion_status : '';
            return `<tr><td>${esc(d.filename || '?')}</td><td class="mono">${esc(path || '—')}</td><td>${esc(status)}</td></tr>`;
        }).join('');
        html += `<h4 class="result-section-title">📄 Source documents (${docs.length})</h4>`
            + `<table class="result-summary-table"><thead><tr><th>File</th><th>source_path / repo_path</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    const chunks = Array.isArray(result.rag_chunks) ? result.rag_chunks : [];
    if (chunks.length) {
        const rows = chunks.map((c, i) => {
            const section = c.section_title || c.article_number || '—';
            const path = canonicalPath(c);
            const scoreNum = (c.score === null || c.score === undefined) ? NaN : Number(c.score);
            const score = Number.isFinite(scoreNum) ? scoreNum.toFixed(4) : '—';
            return `<tr><td>${i + 1}</td><td>${esc(score)}</td>`
                + `<td>${esc(section)}</td><td>${esc(c.filename || '?')}</td><td class="mono">${esc(path || '—')}</td></tr>`;
        }).join('');
        html += `<h4 class="result-section-title">📎 Chunks RAG (${chunks.length})</h4>`
            + `<table class="result-summary-table"><thead><tr><th>#</th><th>Score</th><th>Section</th><th>Document</th><th>source_path / repo_path</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    return html;
}

function resultSummaryHtml(result) {
    const rows = resultSummaryRows(result);
    const query = querySummaryHtml(result);
    const base = rows ? `<table class="result-summary-table"><tbody>${rows}</tbody></table>` : '';
    if (base || query) return base + query;
    return '<div class="empty compact">No compact summary available.</div>';
}

function resultCard(title, result, options = {}) {
    const id = storeResult(title, result, options);
    const status = result?.status || 'result';
    const detail = result?.message || result?.backup_id || result?.document_id || result?.memory_id || result?.name || '';
    const badgeClass = isSuccess(result) ? 'green' : status === 'error' ? 'red' : 'blue';
    return `
        <div class="result-card compact">
            <div class="result-card-main">
                <span class="badge ${badgeClass}">${esc(status)}</span>
                <div>
                    <strong>${esc(title)}</strong>
                    ${detail ? `<span class="text-muted">${esc(detail)}</span>` : ''}
                </div>
            </div>
            <button class="btn-sm blue" data-action="view-result" data-result-id="${esc(id)}">View</button>
        </div>`;
}

function showInlineResult(elementId, result, title = 'Result', options = {}) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = resultCard(title, result, options);
    if (options.modal !== false) showResultModal(title, result, options);
}

function showInlineBusy(elementId, message) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.innerHTML = `<div class="page-loading compact">${esc(message)}</div>`;
}

function highlightYaml(yaml) {
    return String(yaml || '').split('\n').map(line => {
        const escaped = esc(line);
        if (/^\s*#/.test(line)) return `<span class="yaml-comment">${escaped}</span>`;
        const match = escaped.match(/^(\s*)(-\s*)?([^:#]+)(:)(.*)$/);
        if (!match) return escaped;
        const [, indent, dash = '', key, colon, rest] = match;
        return `${indent}${dash ? `<span class="yaml-dash">${dash}</span>` : ''}<span class="yaml-key">${key.trim()}</span><span class="yaml-colon">${colon}</span><span class="yaml-value">${rest}</span>`;
    }).join('\n');
}

function highlightJson(value) {
    const json = JSON.stringify(value || {}, null, 2);
    return esc(json).replace(/(&quot;(?:\\.|[^\\])*?&quot;)(\s*:)?|\b(true|false)\b|\bnull\b|-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/gi, (match, stringToken, colon) => {
        if (stringToken) {
            const cls = colon ? 'json-key' : 'json-string';
            return `<span class="${cls}">${stringToken}</span>${colon || ''}`;
        }
        if (match === 'true' || match === 'false') return `<span class="json-bool">${match}</span>`;
        if (match === 'null') return '<span class="json-null">null</span>';
        return `<span class="json-number">${match}</span>`;
    });
}

function resultTabsHtml(tabs, initialTab = '') {
    const active = initialTab || tabs[0]?.id || '';
    return `<div class="modal-tabs">${
        tabs.map(tab => `<button class="tab-button${tab.id === active ? ' active' : ''}" data-action="result-tab" data-tab="${esc(tab.id)}">${esc(tab.label)}</button>`).join('')
    }</div><div class="tab-panels">${
        tabs.map(tab => `<section class="tab-panel${tab.id === active ? ' active' : ''}" data-panel="${esc(tab.id)}">${tab.body}</section>`).join('')
    }</div>`;
}

function showResultModal(title, result, options = {}) {
    const tabs = [
        { id: 'summary', label: 'Summary', body: resultSummaryHtml(result) },
        { id: 'json', label: 'JSON', body: `<pre class="pretty-code json-pretty">${highlightJson(result)}</pre>` },
    ];
    if (options.yaml !== undefined) {
        tabs.unshift({
            id: 'yaml',
            label: 'YAML',
            body: `<pre class="pretty-code yaml-code">${highlightYaml(options.yaml)}</pre>`,
        });
    }
    showModal(title, resultTabsHtml(tabs, options.initialTab), 'Close', () => true, { size: options.size || 'wide' });
}

function viewStoredResult(resultId) {
    const stored = resultStore.get(resultId);
    if (!stored) return;
    showResultModal(stored.title, stored.result, stored.options || {});
}

function showResultTab(tabName) {
    const modal = document.getElementById('modalOverlay');
    if (!modal) return;
    modal.querySelectorAll('.tab-button').forEach(button => button.classList.toggle('active', button.dataset.tab === tabName));
    modal.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.dataset.panel === tabName));
}

const cache = { memories: [], ontologies: [], tokens: [], backups: [], identity: {}, health: {}, about: {} };
const CATS = {
    dashboard: { icon: '📊', label: 'Dashboard' },
    ontologies: { icon: '🧬', label: 'Ontologies' },
    memories: { icon: '🧠', label: 'Memories' },
    documents: { icon: '📄', label: 'Documents' },
    jobs: { icon: '⚡', label: 'Ingest Jobs' },
    search: { icon: '🔎', label: 'Ask & Query' },
    tokens: { icon: '🔑', label: 'Tokens' },
    backups: { icon: '💾', label: 'Backups' },
    storage: { icon: '🧹', label: 'Storage' },
};
let activeCat = 'dashboard';

function showLogin(message = '') {
    document.getElementById('loginOverlay').classList.remove('hidden');
    document.getElementById('loginError').textContent = message;
    document.getElementById('loginToken').focus();
}

function hideLogin() {
    document.getElementById('loginOverlay').classList.add('hidden');
}

async function doLogin() {
    const input = document.getElementById('loginToken');
    const button = document.getElementById('loginBtn');
    const error = document.getElementById('loginError');
    const token = input.value.trim();
    if (!token) {
        error.textContent = 'Token required.';
        return;
    }
    button.disabled = true;
    button.textContent = 'Signing in...';
    error.textContent = '';
    try {
        const result = await adminLogin(token);
        if (result.status !== 'ok') {
            error.textContent = result.message || 'Invalid token';
            return;
        }
        cache.identity = result;
        document.getElementById('headerUser').textContent = result.client_name || '';
        input.value = '';
        hideLogin();
        await warmCache();
        buildSidebar();
        showCategory('dashboard');
    } catch {
        error.textContent = 'Server unreachable.';
    } finally {
        button.disabled = false;
        button.textContent = 'Sign in';
    }
}

async function doLogout() {
    await adminLogout();
    document.getElementById('headerUser').textContent = '';
    showLogin();
}

async function warmCache() {
    const [health, whoami, memories, ontologies] = await Promise.all([
        adminHealth(),
        callTool('system_whoami', {}).catch(() => ({})),
        callTool('memory_list', {}).catch(() => ({ memories: [] })),
        callTool('ontology_list', {}).catch(() => ({ ontologies: [] })),
    ]);
    cache.health = health || {};
    cache.identity = whoami?.status === 'ok' ? whoami : cache.identity;
    cache.memories = memories.memories || [];
    cache.ontologies = ontologies.ontologies || [];
    document.getElementById('headerVersion').textContent = health.version ? 'v' + health.version : '';
    document.getElementById('headerUser').textContent = cache.identity.client_name || '';
}

async function loadMemories() {
    const result = await callTool('memory_list', {});
    cache.memories = result.memories || [];
    return cache.memories;
}

async function loadOntologies() {
    const result = await callTool('ontology_list', {});
    cache.ontologies = result.ontologies || [];
    return cache.ontologies;
}

async function loadTokens() {
    const result = await callTool('admin_list_tokens', {});
    cache.tokens = result.tokens || [];
    return cache.tokens;
}

function buildSidebar() {
    const nav = document.getElementById('sidebarNav');
    nav.innerHTML = Object.entries(CATS).map(([key, item]) =>
        `<button class="sidebar-btn${key === activeCat ? ' active' : ''}" data-action="nav" data-cat="${esc(key)}">
            <span class="sidebar-icon">${esc(item.icon)}</span>${esc(item.label)}
        </button>`
    ).join('');
}

function showCategory(cat) {
    stopJobsPolling();  // arrêter le polling si on quitte la page Jobs
    activeCat = cat;
    document.querySelectorAll('.sidebar-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.cat === cat));
    const content = document.getElementById('content');
    content.innerHTML = '<div class="page-loading">Loading...</div>';
    const renderers = {
        dashboard: renderDashboard,
        memories: renderMemories,
        documents: renderDocuments,
        jobs: renderIngestJobs,
        search: renderSearch,
        tokens: renderTokens,
        backups: renderBackups,
        storage: renderStorage,
        ontologies: renderOntologies,
    };
    (renderers[cat] || renderDashboard)();
}

function memorySelect(id, includeAll = false) {
    const opts = cache.memories.map(m =>
        `<option value="${esc(m.id)}">${esc(m.id)}${m.name ? ' - ' + esc(m.name) : ''}</option>`
    ).join('');
    const first = includeAll ? '<option value="">all memories</option>' : '<option value="">choose a memory</option>';
    return `<select class="form-input" id="${esc(id)}">${first}${opts}</select>`;
}

function ontologySelect(id) {
    const opts = cache.ontologies.map(o =>
        `<option value="${esc(o.name)}">${esc(o.name)}${o.description ? ' - ' + esc(o.description.slice(0, 50)) : ''}</option>`
    ).join('');
    return `<select class="form-input" id="${esc(id)}"><option value="">choose an ontology</option>${opts}</select>`;
}

function checkedValues(name) {
    return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(input => input.value);
}

function permissionPicker(name, selected = []) {
    const values = new Set(selected);
    return `<div class="choice-grid compact">${
        ['read', 'write', 'admin'].map(permission => `
            <label class="choice-pill">
                <input type="checkbox" name="${esc(name)}" value="${esc(permission)}" ${values.has(permission) ? 'checked' : ''}>
                <span>${esc(permission)}</span>
            </label>
        `).join('')
    }</div>`;
}

function memoryCheckboxGrid(name, selected = []) {
    const values = new Set(selected);
    if (!cache.memories.length) return '<div class="empty compact">No memory available.</div>';
    return `<div class="memory-choice-grid">${
        cache.memories.map(memory => `
            <label class="choice-pill memory-choice">
                <input type="checkbox" name="${esc(name)}" value="${esc(memory.id)}" ${values.has(memory.id) ? 'checked' : ''}>
                <span><strong>${esc(memory.id)}</strong>${memory.name ? `<em>${esc(memory.name)}</em>` : ''}</span>
            </label>
        `).join('')
    }</div>`;
}

function dashboardStat(icon, label, value, detail, cat = '') {
    const action = cat ? ` clickable" data-action="nav" data-cat="${esc(cat)}` : '';
    return `<div class="dash-card${action}">
        <div class="dash-card-icon">${esc(icon)}</div>
        <div class="dash-card-body">
            <div class="dash-card-value">${value}</div>
            <div class="dash-card-label">${esc(label)}</div>
            <div class="dash-card-detail">${detail}</div>
        </div>
    </div>`;
}

function metricTile(label, value, detail = '') {
    return `<div class="metric-tile">
        <span>${esc(label)}</span>
        <strong>${esc(value)}</strong>
        ${detail ? `<em>${esc(detail)}</em>` : ''}
    </div>`;
}

function serviceStatusValue(service) {
    return typeof service === 'object' && service !== null ? service.status || '?' : service || '?';
}

function graphTotals(memories) {
    return (memories || []).reduce((acc, memory) => {
        acc.documents += Number(memory.documents || 0);
        acc.entities += Number(memory.entities || 0);
        acc.relations += Number(memory.relations || 0);
        return acc;
    }, { documents: 0, entities: 0, relations: 0 });
}

function ontologyUsageHtml(memories) {
    const counts = new Map();
    memories.forEach(memory => {
        const name = memory.ontology || 'none';
        counts.set(name, (counts.get(name) || 0) + 1);
    });
    const entries = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    if (!entries.length) return '<div class="empty compact">No memory yet.</div>';
    const max = Math.max(...entries.map(([, count]) => count), 1);
    return entries.map(([name, count]) => `
        <div class="usage-row">
            <div class="usage-row-head"><strong>${esc(name)}</strong><span>${count}</span></div>
            <div class="usage-bar"><span style="width:${Math.max(8, Math.round((count / max) * 100))}%"></span></div>
        </div>
    `).join('');
}

function graphStatsHtml(memories) {
    const totals = graphTotals(memories);
    const active = (memories || []).filter(memory =>
        Number(memory.documents || 0) + Number(memory.entities || 0) + Number(memory.relations || 0) > 0
    ).length;
    const density = totals.entities ? (totals.relations / totals.entities).toFixed(2) : '0.00';
    const top = [...(memories || [])].sort((a, b) =>
        (Number(b.documents || 0) + Number(b.entities || 0) + Number(b.relations || 0)) -
        (Number(a.documents || 0) + Number(a.entities || 0) + Number(a.relations || 0))
    )[0];
    return `<div class="metric-grid">
        ${metricTile('Documents', fmtInt(totals.documents), 'ingested')}
        ${metricTile('Entities', fmtInt(totals.entities), 'graph nodes')}
        ${metricTile('Relations', fmtInt(totals.relations), 'graph edges')}
        ${metricTile('Density', density, 'relations / entity')}
        ${metricTile('Active memories', `${active}/${(memories || []).length}`, 'with graph data')}
        ${metricTile('Top memory', top?.id || 'none', top ? `${fmtInt(top.documents)} docs` : '')}
    </div>`;
}

function capabilityHtml(capabilities) {
    const categories = capabilities?.categories || {};
    const formats = capabilities?.supported_formats || [];
    const categoryRows = Object.entries(categories).map(([name, count]) =>
        `<div class="service-row"><span>${esc(name)}</span><strong>${esc(count)}</strong></div>`
    ).join('');
    return `<div class="identity-lines">
        <div><span>MCP tools</span><strong>${esc(capabilities?.total_tools || '?')}</strong></div>
        <div><span>Formats</span><strong>${formats.map(fmt => esc(fmt)).join(', ') || '?'}</strong></div>
    </div>
    <div class="capability-list">${categoryRows || '<div class="empty compact">No capability detail.</div>'}</div>`;
}

document.addEventListener('click', event => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    event.preventDefault();
    const action = target.dataset.action;
    const data = target.dataset;

    if (action === 'nav') return showCategory(data.cat);
    if (action === 'close-modal') return closeModal();
    if (action === 'view-result') return viewStoredResult(data.resultId);
    if (action === 'result-tab') return showResultTab(data.tab);
    if (action === 'run') return runAndShow(data.tool, JSON.parse(data.args || '{}'));
    if (action === 'confirm-run') return runConfirmedAction(data);
    if (action === 'create-memory') return showCreateMemory();
    if (action === 'update-memory') return showUpdateMemory(data.memory);
    if (action === 'memory-stats') return runAndShow('memory_stats', { memory_id: data.memory });
    if (action === 'show-ingest') return showIngestDocument();
    if (action === 'ingest-document') return ingestDocument();
    if (action === 'load-documents') return loadDocuments();
    if (action === 'read-document') return readDocument(data.memory, data.document);
    if (action === 'load-jobs') return loadIngestJobs();
    if (action === 'toggle-jobs-autorefresh') return toggleJobsAutoRefresh();
    if (action === 'view-job') return viewIngestJob(data.job);
    if (action === 'cancel-job') return cancelIngestJob(data.job);
    if (action === 'show-ingest-async') return showIngestAsync();
    if (action === 'ingest-async-submit') return ingestAsyncSubmit();
    if (action === 'ask') return doAsk();
    if (action === 'query') return doQuery();
    if (action === 'create-token') return showCreateToken();
    if (action === 'update-token') return showUpdateToken(data.hash);
    if (action === 'create-backup') return showCreateBackup();
    if (action === 'restore-backup-archive') return showRestoreBackupArchive();
    if (action === 'load-backups') return loadBackups();
    if (action === 'download-backup') return downloadBackup(data.backup);
    if (action === 'storage-check') return storageCheck();
    if (action === 'storage-cleanup') return storageCleanup();
    if (action === 'import-ontology') return showImportOntology();
    if (action === 'view-ontology') return viewOntology(data.name);
    if (action === 'edit-ontology') return editOntology(data.name);
    if (action === 'export-ontology') return exportOntology(data.name);
    if (action === 'delete-ontology') return deleteOntology(data.name);
});

document.addEventListener('change', event => {
    if (event.target.id === 'documentsMemory') loadDocuments();
    if (event.target.id === 'backupMemory') loadBackups();
    if (event.target.id === 'jobsMemory' || event.target.id === 'jobsStatus') loadIngestJobs();
    if (event.target.name === 'ut_memory_mode') syncTokenMemoryMode();
});

document.getElementById('loginBtn').addEventListener('click', doLogin);
document.getElementById('loginToken').addEventListener('keydown', event => {
    if (event.key === 'Enter') doLogin();
});
document.getElementById('logoutBtn').addEventListener('click', doLogout);

async function renderDashboard() {
    await warmCache();
    const content = document.getElementById('content');
    const [tokensResult, backupsResult, aboutResult] = await Promise.all([
        callTool('admin_list_tokens', {}).catch(e => ({ status: 'error', message: e.message })),
        callTool('backup_list', {}).catch(e => ({ status: 'error', message: e.message })),
        callTool('system_about', {}).catch(e => ({ status: 'error', message: e.message })),
    ]);
    cache.about = aboutResult.status === 'ok' ? aboutResult : {};
    const healthStatus = cache.health.status || '?';
    const healthClass = ['healthy', 'ok'].includes(healthStatus) ? 'green' : healthStatus === 'degraded' ? 'orange' : 'red';
    const services = cache.health.services || {};
    const serviceEntries = Object.entries(services);
    const serviceOk = serviceEntries.filter(([, svc]) => ['ok', 'healthy'].includes(serviceStatusValue(svc))).length;
    const serviceBits = serviceEntries.map(([name, svc]) => {
        const status = serviceStatusValue(svc);
        return `<span class="badge ${status === 'ok' || status === 'healthy' ? 'green' : status === 'warning' ? 'orange' : 'red'}">${esc(name)} ${esc(status)}</span>`;
    }).join(' ');
    const perms = (cache.identity.permissions || []).map(p => `<span class="badge purple">${esc(p)}</span>`).join(' ');
    const tokenCount = tokensResult.status === 'ok' ? (tokensResult.tokens || []).length : 'n/a';
    const backupCount = backupsResult.status === 'ok' ? (backupsResult.count ?? (backupsResult.backups || []).length) : 'n/a';
    const latestBackup = backupsResult.status === 'ok' && (backupsResult.backups || [])[0]
        ? fmtDate(backupsResult.backups[0].created_at)
        : 'no recent backup';
    const memoryScope = (cache.identity.memory_ids || []).length ? cache.identity.memory_ids.join(', ') : 'all memories';
    const ontologyNames = cache.ontologies.map(o => o.name).slice(0, 6).join(', ') || 'none';
    const graphMemories = cache.about.memories || [];
    const totals = graphTotals(graphMemories);
    const capabilities = cache.about.capabilities || {};
    content.innerHTML = `
        <div class="page">
            <div class="dashboard-hero">
                <div>
                    <h2 class="page-title">📊 Dashboard</h2>
                    <p>Graph Memory overview for the current admin session.</p>
                </div>
                <div class="dashboard-hero-meta">
                    <span class="badge ${healthClass}">${esc(healthStatus)}</span>
                    <span class="badge blue">v${esc(cache.health.version || '?')}</span>
                </div>
            </div>
            <div class="dash-cards">
                ${dashboardStat('⚙️', 'Services', `${serviceOk}/${serviceEntries.length || 0}`, serviceBits || 'no service detail')}
                ${dashboardStat('🧠', 'Memories', cache.memories.length, 'Namespaces visible to this token', 'memories')}
                ${dashboardStat('📄', 'Documents', fmtInt(totals.documents), 'Indexed graph documents', 'documents')}
                ${dashboardStat('🔗', 'Entities', fmtInt(totals.entities), 'Knowledge graph nodes')}
                ${dashboardStat('↔️', 'Relations', fmtInt(totals.relations), 'Knowledge graph edges')}
                ${dashboardStat('🧬', 'Ontologies', cache.ontologies.length, esc(ontologyNames), 'ontologies')}
                ${dashboardStat('💾', 'Backups', backupCount, esc(latestBackup), 'backups')}
                ${dashboardStat('🔑', 'Tokens', tokenCount, tokensResult.status === 'ok' ? 'Active tokens' : 'Admin permission required', 'tokens')}
            </div>
            <div class="dashboard-grid">
                <section class="dashboard-panel wide">
                    <div class="panel-title"><span>Knowledge Graph</span><strong>${fmtInt(totals.documents + totals.entities + totals.relations)}</strong></div>
                    ${graphStatsHtml(graphMemories)}
                </section>
                <section class="dashboard-panel">
                    <div class="panel-title"><span>Service Status</span><strong>${serviceOk}/${serviceEntries.length || 0}</strong></div>
                    <div class="service-list">${
                        serviceEntries.length ? serviceEntries.map(([name, svc]) => {
                            const status = serviceStatusValue(svc);
                            const cls = status === 'ok' || status === 'healthy' ? 'green' : status === 'warning' ? 'orange' : 'red';
                            return `<div class="service-row"><span>${esc(name)}</span><span class="badge ${cls}">${esc(status)}</span></div>`;
                        }).join('') : '<div class="empty compact">No service detail.</div>'
                    }</div>
                </section>
                <section class="dashboard-panel">
                    <div class="panel-title"><span>Ontology Usage</span><strong>${cache.memories.length}</strong></div>
                    ${ontologyUsageHtml(cache.memories)}
                </section>
                <section class="dashboard-panel">
                    <div class="panel-title"><span>Capabilities</span><strong>${esc(capabilities.total_tools || '?')}</strong></div>
                    ${capabilityHtml(capabilities)}
                </section>
                <section class="dashboard-panel">
                    <div class="panel-title"><span>Current Access</span><strong>${esc(cache.identity.auth_type || '?')}</strong></div>
                    <div class="identity-lines">
                        <div><span>Client</span><strong>${esc(cache.identity.client_name || '?')}</strong></div>
                        <div><span>Scope</span><strong>${esc(memoryScope)}</strong></div>
                        <div><span>Permissions</span><strong>${perms || '<span class="text-muted">none</span>'}</strong></div>
                    </div>
                </section>
            </div>
        </div>`;
}

async function renderMemories() {
    const content = document.getElementById('content');
    content.innerHTML = '<div class="page"><div class="page-header"><h2 class="page-title">🧠 Memories</h2><button class="btn-action green" data-action="create-memory">Create Memory</button></div><div id="memoriesContent" class="page-loading">Loading...</div></div>';
    const [memories] = await Promise.all([loadMemories(), loadOntologies()]);
    const el = document.getElementById('memoriesContent');
    if (!memories.length) {
        el.innerHTML = '<div class="empty">No memory found.</div>';
        return;
    }
    el.innerHTML = `<table class="data-table"><thead><tr><th>ID</th><th>Name</th><th>Ontology</th><th>Created</th><th>Actions</th></tr></thead><tbody>${
        memories.map(memory => {
            const id = esc(memory.id);
            return `<tr>
                <td><strong>${id}</strong><br><span class="text-muted">${esc(memory.description || '')}</span></td>
                <td>${esc(memory.name || '')}</td>
                <td><span class="badge blue">${esc(memory.ontology || 'none')}</span></td>
                <td>${esc(fmtDate(memory.created_at))}</td>
                <td class="actions-cell">
                    <button class="btn-sm blue" data-action="memory-stats" data-memory="${id}">Stats</button>
                    <button class="btn-sm" data-action="update-memory" data-memory="${id}">Edit</button>
                    <a class="btn-sm" href="/graph?memory=${id}">Graph</a>
                    <button class="btn-sm red" data-action="confirm-run" data-tool="memory_delete" data-args='${attrJson({ memory_id: memory.id })}' data-message="Delete memory ${id} and all its data?">Delete</button>
                </td>
            </tr>`;
        }).join('')
    }</tbody></table>`;
}

function showCreateMemory() {
    showModal('Create Memory', `
        <div class="form-group"><label class="form-label">Memory ID</label><input class="form-input" id="m_memory_id" data-1p-ignore></div>
        <div class="form-group"><label class="form-label">Name</label><input class="form-input" id="m_name" data-1p-ignore></div>
        <div class="form-group"><label class="form-label">Description</label><input class="form-input" id="m_description" data-1p-ignore></div>
        <div class="form-group"><label class="form-label">Ontology</label>${ontologySelect('m_ontology')}</div>
    `, 'Create', async () => {
        const memoryId = gv('m_memory_id');
        const ontology = gv('m_ontology');
        if (!memoryId || !ontology) return false;
        const result = await callTool('memory_create', {
            memory_id: memoryId,
            name: gv('m_name') || memoryId,
            description: gv('m_description'),
            ontology,
        });
        if (result.status === 'created') {
            closeModal();
            renderMemories();
            return false;
        }
        alert(result.message || 'Error');
        return false;
    });
}

function showUpdateMemory(memoryId) {
    const memory = cache.memories.find(m => m.id === memoryId) || {};
    showModal('Edit Memory', `
        <div class="form-group"><label class="form-label">Name</label><input class="form-input" id="m_name" value="${esc(memory.name || '')}" data-1p-ignore></div>
        <div class="form-group"><label class="form-label">Description</label><input class="form-input" id="m_description" value="${esc(memory.description || '')}" data-1p-ignore></div>
    `, 'Save', async () => {
        const args = { memory_id: memoryId };
        if (gv('m_name')) args.name = gv('m_name');
        args.description = gv('m_description');
        const result = await callTool('memory_update', args);
        if (result.status === 'ok') {
            closeModal();
            renderMemories();
            return false;
        }
        alert(result.message || 'Error');
        return false;
    });
}

async function renderDocuments() {
    const content = document.getElementById('content');
    await loadMemories();
    content.innerHTML = `
        <div class="page">
            <div class="page-header"><h2 class="page-title">📄 Documents</h2><div class="toolbar">${memorySelect('documentsMemory')}<button class="btn-action green" data-action="show-ingest">Ingest File</button></div></div>
            <div id="documentsContent" class="empty">Choose a memory.</div>
        </div>`;
}

function showIngestDocument() {
    showModal('Ingest File', `
        <div class="form-group"><label class="form-label">Memory</label>${memorySelect('ingestMemory')}</div>
        <div class="form-group"><label class="form-label">File</label><input class="form-input" id="ingestFile" type="file"></div>
        <div class="form-group"><label class="form-label">Source path</label><input class="form-input" id="ingestSourcePath" placeholder="optional; defaults to file name" data-1p-ignore></div>
        <label class="form-check"><input type="checkbox" id="ingestForce"> force re-ingestion</label>
        <div id="ingestStatus" class="text-muted"></div>
    `, 'Ingest', async () => {
        await ingestDocument();
        return false;
    });
}

function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
}

async function ingestDocument() {
    const fileInput = document.getElementById('ingestFile');
    const status = document.getElementById('ingestStatus');
    const memoryId = gv('ingestMemory');
    const file = fileInput?.files?.[0];
    if (!memoryId || !file) {
        if (status) status.textContent = 'Choose a memory and a file.';
        return;
    }
    if (status) status.textContent = `Reading ${file.name} (${fmtSize(file.size)})...`;
    const contentBase64 = arrayBufferToBase64(await file.arrayBuffer());
    if (status) status.textContent = 'Ingestion in progress...';
    const sourcePath = gv('ingestSourcePath') || file.name;
    const result = await callTool('memory_ingest', {
        memory_id: memoryId,
        content_base64: contentBase64,
        filename: file.name,
        force: checked('ingestForce'),
        source_path: sourcePath,
        source_modified_at: new Date(file.lastModified || Date.now()).toISOString(),
    });
    if (result.status === 'ok' || result.status === 'already_exists') {
        if (status) status.textContent = result.status === 'ok' ? 'Ingestion complete.' : 'Already ingested.';
        showResultModal('Ingestion Result', result);
        await renderDocuments();
        return;
    }
    if (status) status.textContent = result.message || 'Ingestion failed.';
}

async function loadDocuments() {
    const memoryId = gv('documentsMemory');
    const el = document.getElementById('documentsContent');
    if (!memoryId || !el) {
        if (el) el.innerHTML = '<div class="empty">Choose a memory.</div>';
        return;
    }
    el.innerHTML = '<div class="page-loading">Loading documents...</div>';
    const result = await callTool('document_list', { memory_id: memoryId });
    const docs = result.documents || [];
    if (!docs.length) {
        el.innerHTML = '<div class="empty">No document found.</div>';
        return;
    }
    el.innerHTML = `<table class="data-table"><thead><tr><th>Document</th><th>Size</th><th>Hash</th><th>Actions</th></tr></thead><tbody>${
        docs.map(doc => {
            const id = esc(doc.id || doc.document_id || '');
            return `<tr>
                <td><strong>${esc(doc.filename || id)}</strong><br><span class="text-muted mono">${id}</span></td>
                <td>${esc(fmtSize(doc.size_bytes))}</td>
                <td><span class="mono text-muted">${esc((doc.hash || '').slice(0, 16))}</span></td>
                <td class="actions-cell">
                    <button class="btn-sm blue" data-action="read-document" data-memory="${esc(memoryId)}" data-document="${id}">Read</button>
                    <button class="btn-sm red" data-action="confirm-run" data-tool="document_delete" data-args='${attrJson({ memory_id: memoryId, document_id: doc.id || doc.document_id })}' data-message="Delete document ${esc(doc.filename || id)}?">Delete</button>
                </td>
            </tr>`;
        }).join('')
    }</tbody></table>`;
}

async function readDocument(memoryId, documentId) {
    const result = await callTool('document_get', { memory_id: memoryId, document_id: documentId, include_content: true, content_format: 'text' });
    if (result.status === 'error') {
        alert(result.message || 'Error');
        return;
    }
    if (!result.content && !result.content_note) {
        showResultModal('Document Preview', result);
        return;
    }
    const content = result.content || result.content_note;
    showModal('Document Preview', `<pre class="pretty-code">${esc(content)}</pre>`, 'Close', () => true, { size: 'wide' });
}

// =============================================================================
// Ingest Jobs (ingestion asynchrone) — v3.1.0
// =============================================================================

const JOB_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'skipped', 'changed_skipped']);
const JOB_STATUS_CLASS = {
    succeeded: 'green', running: 'blue', queued: 'amber', failed: 'red',
    cancelled: 'grey', cancelling: 'amber', skipped: 'grey', changed_skipped: 'amber',
};
let jobsPollTimer = null;

function stopJobsPolling() {
    if (jobsPollTimer) { clearInterval(jobsPollTimer); jobsPollTimer = null; }
}

function jobStatusBadge(status) {
    const cls = JOB_STATUS_CLASS[status] || 'grey';
    return `<span class="job-badge ${cls}">${esc(status || '?')}</span>`;
}

function progressBar(percent) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    return `<div class="job-progress"><div class="job-progress-fill" data-pct="${p}"></div><span class="job-progress-label">${p}%</span></div>`;
}

async function renderIngestJobs() {
    const content = document.getElementById('content');
    await loadMemories();
    content.innerHTML = `
        <div class="page">
            <div class="page-header">
                <h2 class="page-title">⚡ Ingest Jobs</h2>
                <div class="toolbar">
                    ${memorySelect('jobsMemory', true)}
                    <select class="form-input" id="jobsStatus">
                        <option value="">all statuses</option>
                        <option value="queued">queued</option>
                        <option value="running">running</option>
                        <option value="succeeded">succeeded</option>
                        <option value="failed">failed</option>
                        <option value="cancelled">cancelled</option>
                        <option value="skipped">skipped</option>
                        <option value="changed_skipped">changed_skipped</option>
                    </select>
                    <button class="btn-action" data-action="load-jobs">Refresh</button>
                    <button class="btn-action" id="jobsAutoBtn" data-action="toggle-jobs-autorefresh">▶ Auto</button>
                    <button class="btn-action green" data-action="show-ingest-async">Ingest (async)</button>
                </div>
            </div>
            <div id="jobsContent" class="empty">Choose a memory to list its ingestion jobs.</div>
        </div>`;
}

async function loadIngestJobs() {
    const memoryId = gv('jobsMemory');
    const el = document.getElementById('jobsContent');
    if (!el) return;
    if (!memoryId) {
        stopJobsPolling();
        el.innerHTML = '<div class="empty">Choose a memory to list its ingestion jobs.</div>';
        return;
    }
    const status = gv('jobsStatus');
    const args = { memory_id: memoryId };
    if (status) args.status = status;
    const result = await callTool('ingest_job_list', args);
    if (result.status !== 'ok') {
        el.innerHTML = `<div class="empty">${esc(result.message || 'Unable to list jobs.')}</div>`;
        return;
    }
    const jobs = result.jobs || [];
    const running = jobs.filter(j => !JOB_TERMINAL.has(j.status)).length;
    if (!jobs.length) {
        el.innerHTML = '<div class="empty">No ingestion job for this memory.</div>';
        return;
    }
    el.innerHTML = `
        <div class="job-summary">${jobs.length} job(s) — <strong>${running}</strong> running/queued
            <span class="text-muted">· guarantee: ${esc(result.guarantee || 'in_memory_best_effort')}</span></div>
        <table class="data-table"><thead><tr>
            <th>Status</th><th>Step</th><th>Progress</th><th>source_path / file</th>
            <th>E / R</th><th>Updated</th><th>Actions</th>
        </tr></thead><tbody>${
            jobs.map(j => {
                const term = JOB_TERMINAL.has(j.status);
                const label = esc(j.source_path || j.filename || j.job_id);
                return `<tr>
                    <td>${jobStatusBadge(j.status)}</td>
                    <td class="mono text-muted">${esc(j.current_step || '')}</td>
                    <td>${progressBar(j.progress_percent)}</td>
                    <td><strong>${label}</strong><br><span class="text-muted mono">${esc(j.job_id)}</span></td>
                    <td>${fmtInt(j.created_entities)} / ${fmtInt(j.created_relations)}</td>
                    <td class="text-muted">${esc(fmtDate(j.updated_at))}</td>
                    <td class="actions-cell">
                        <button class="btn-sm blue" data-action="view-job" data-job="${esc(j.job_id)}">View</button>
                        ${term ? '' : `<button class="btn-sm red" data-action="cancel-job" data-job="${esc(j.job_id)}">Cancel</button>`}
                    </td>
                </tr>`;
            }).join('')
        }</tbody></table>`;
    // Appliquer la largeur des barres de progression (CSP-safe : pas de style inline en HTML)
    el.querySelectorAll('.job-progress-fill').forEach(bar => { bar.style.width = (bar.dataset.pct || 0) + '%'; });
}

function toggleJobsAutoRefresh() {
    const btn = document.getElementById('jobsAutoBtn');
    if (jobsPollTimer) {
        stopJobsPolling();
        if (btn) { btn.textContent = '▶ Auto'; btn.classList.remove('active'); }
    } else {
        if (!gv('jobsMemory')) { alert('Choose a memory first.'); return; }
        loadIngestJobs();
        jobsPollTimer = setInterval(() => { if (activeCat === 'jobs') loadIngestJobs(); else stopJobsPolling(); }, 3000);
        if (btn) { btn.textContent = '⏸ Auto (3s)'; btn.classList.add('active'); }
    }
}

async function viewIngestJob(jobId) {
    const result = await callTool('ingest_job_status', { job_id: jobId });
    showResultModal('Ingest Job', result);
}

async function cancelIngestJob(jobId) {
    if (!confirm(`Cancel job ${jobId}? (best effort, without corrupting the graph)`)) return;
    const result = await callTool('ingest_job_cancel', { job_id: jobId });
    if (result.status === 'error') alert(result.message || 'Cancel failed.');
    await loadIngestJobs();
}

async function sha256Hex(buffer) {
    const digest = await crypto.subtle.digest('SHA-256', buffer);
    return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
}

function showIngestAsync() {
    showModal('Ingest (async)', `
        <div class="form-group"><label class="form-label">Memory</label>${memorySelect('iaMemory')}</div>
        <div class="form-group"><label class="form-label">File</label><input class="form-input" id="iaFile" type="file"></div>
        <div class="form-group"><label class="form-label">Source path (stable business key)</label><input class="form-input" id="iaSourcePath" placeholder="for example, docs/product/iaas.md — defaults to the filename" data-1p-ignore></div>
        <label class="form-check"><input type="checkbox" id="iaReplace"> replace_existing (replace when the checksum changed)</label>
        <div id="iaStatus" class="text-muted"></div>
    `, 'Submit', async () => { await ingestAsyncSubmit(); return false; });
}

async function ingestAsyncSubmit() {
    const status = document.getElementById('iaStatus');
    const memoryId = gv('iaMemory');
    const file = document.getElementById('iaFile')?.files?.[0];
    if (!memoryId || !file) { if (status) status.textContent = 'Choose a memory and a file.'; return; }
    if (status) status.textContent = `Reading ${file.name} (${fmtSize(file.size)})...`;
    const buffer = await file.arrayBuffer();
    const contentBase64 = arrayBufferToBase64(buffer);
    const sha256 = await sha256Hex(buffer);
    if (status) status.textContent = 'Submitting job...';
    const result = await callTool('memory_ingest_async', {
        memory_id: memoryId,
        content_base64: contentBase64,
        filename: file.name,
        source_path: gv('iaSourcePath') || file.name,
        sha256,
        source_modified_at: new Date(file.lastModified || Date.now()).toISOString(),
        replace_existing: checked('iaReplace'),
    });
    if (status) status.textContent = result.message || result.status || '';
    closeModal();
    showResultModal('Asynchronous submission', result);
    // Basculer sur la page Jobs de cette mémoire et lancer le suivi auto
    if (activeCat !== 'jobs') showCategory('jobs');
    const sel = document.getElementById('jobsMemory');
    if (sel) sel.value = memoryId;
    await loadIngestJobs();
}

function renderSearch() {
    const content = document.getElementById('content');
    content.innerHTML = `
        <div class="page">
            <h2 class="page-title">🔎 Ask & Query</h2>
            <div class="sys-grid">
                <section class="sys-card">
                    <h3>Ask</h3>
                    <div class="form-group">${memorySelect('askMemory')}</div>
                    <div class="form-group"><textarea class="form-input" id="askQuestion" rows="4" placeholder="Question..."></textarea></div>
                    <div class="form-row"><input class="form-input short" id="askLimit" type="number" value="10"><button class="btn-action blue" data-action="ask">Ask</button></div>
                    <div id="askResult"></div>
                </section>
                <section class="sys-card">
                    <h3>Query</h3>
                    <div class="form-group">${memorySelect('queryMemory')}</div>
                    <div class="form-group"><textarea class="form-input" id="queryText" rows="4" placeholder="Structured query..."></textarea></div>
                    <div class="form-row"><input class="form-input short" id="queryLimit" type="number" value="10"><button class="btn-action blue" data-action="query">Query</button></div>
                    <div id="queryResult"></div>
                </section>
            </div>
        </div>`;
}

async function doAsk() {
    const resultEl = document.getElementById('askResult');
    resultEl.innerHTML = '<div class="page-loading compact">Searching...</div>';
    const result = await callTool('question_answer', {
        memory_id: gv('askMemory'),
        question: gv('askQuestion'),
        limit: Number(gv('askLimit') || 10),
    });
    showInlineResult('askResult', result, 'Ask Result');
}

async function doQuery() {
    const resultEl = document.getElementById('queryResult');
    resultEl.innerHTML = '<div class="page-loading compact">Searching...</div>';
    const result = await callTool('memory_query', {
        memory_id: gv('queryMemory'),
        query: gv('queryText'),
        limit: Number(gv('queryLimit') || 10),
    });
    showInlineResult('queryResult', result, 'Query Result');
}

async function renderTokens() {
    const content = document.getElementById('content');
    content.innerHTML = '<div class="page"><div class="page-header"><h2 class="page-title">🔑 Tokens</h2><button class="btn-action green" data-action="create-token">Create Token</button></div><div id="tokensContent" class="page-loading">Loading...</div><div id="tokensResult"></div></div>';
    let tokens = [];
    try { tokens = await loadTokens(); } catch (e) {
        document.getElementById('tokensContent').innerHTML = `<div class="empty">${esc(e.message || 'Admin permission required.')}</div>`;
        return;
    }
    await loadMemories().catch(() => []);
    const el = document.getElementById('tokensContent');
    if (!tokens.length) {
        el.innerHTML = '<div class="empty">No token found.</div>';
        return;
    }
    el.innerHTML = `<table class="data-table"><thead><tr><th>Client</th><th>Permissions</th><th>Memories</th><th>Expires</th><th>Actions</th></tr></thead><tbody>${
        tokens.map(token => {
            const hash = esc(token.token_hash || '');
            return `<tr>
                <td><strong>${esc(token.client_name || '')}</strong><br><span class="text-muted">${esc(token.email || '')}</span><br><span class="mono text-muted">${hash.slice(0, 28)}...</span></td>
                <td>${(token.permissions || []).map(p => `<span class="badge purple">${esc(p)}</span>`).join(' ')}</td>
                <td>${(token.memory_ids || []).length ? esc(token.memory_ids.join(', ')) : '<span class="text-muted">all</span>'}</td>
                <td>${esc(fmtDate(token.expires_at)) || '<span class="text-muted">never</span>'}</td>
                <td class="actions-cell">
                    <button class="btn-sm" data-action="update-token" data-hash="${hash}">Update</button>
                    <button class="btn-sm red" data-action="confirm-run" data-tool="admin_revoke_token" data-args='${attrJson({ token_hash_prefix: token.token_hash })}' data-message="Revoke token ${esc(token.client_name)}?">Revoke</button>
                </td>
            </tr>`;
        }).join('')
    }</tbody></table>`;
}

function showCreateToken() {
    showModal('Create Token', `
        <div class="form-group"><label class="form-label">Client name</label><input class="form-input" id="t_client" data-1p-ignore></div>
        <section class="form-section">
            <div class="form-section-head"><strong>Permissions</strong><span>Choose explicit capabilities.</span></div>
            ${permissionPicker('t_permissions', ['read', 'write'])}
        </section>
        <section class="form-section">
            <div class="form-section-head"><strong>Memory access</strong><span>Empty selection means all memories.</span></div>
            ${memoryCheckboxGrid('t_memories')}
        </section>
        <div class="form-group"><label class="form-label">Email</label><input class="form-input" id="t_email" data-1p-ignore></div>
        <div class="form-group"><label class="form-label">Expires in days</label><input class="form-input" id="t_expires" type="number" min="0" placeholder="empty = never"></div>
    `, 'Create', async () => {
        const args = {
            client_name: gv('t_client'),
            permissions: checkedValues('t_permissions'),
            memory_ids: checkedValues('t_memories'),
        };
        if (!args.permissions.length) {
            alert('Choose at least one permission.');
            return false;
        }
        if (gv('t_email')) args.email = gv('t_email');
        if (gv('t_expires')) args.expires_in_days = Number(gv('t_expires'));
        const result = await callTool('admin_create_token', args);
        if (result.token) {
            showModal('Token Created', `<p class="warning">This token will not be shown again.</p><pre class="pretty-code">${esc(result.token)}</pre>`, 'Close', () => { renderTokens(); return true; });
            return false;
        }
        alert(result.message || 'Error');
        return false;
    }, { size: 'token-modal' });
}

function showUpdateToken(hash) {
    const token = cache.tokens.find(item => item.token_hash === hash || (item.token_hash || '').startsWith(hash)) || {};
    const currentPermissions = token.permissions || [];
    const currentMemories = token.memory_ids || [];
    const hasMemoryRestriction = currentMemories.length > 0;
    showModal('Update Token', `
        <div class="token-summary">
            <div><span>Client</span><strong>${esc(token.client_name || 'unknown')}</strong></div>
            <div><span>Hash</span><strong class="mono">${esc((token.token_hash || hash).slice(0, 20))}...</strong></div>
            <div><span>Current scope</span><strong>${hasMemoryRestriction ? esc(currentMemories.join(', ')) : 'all memories'}</strong></div>
        </div>
        <section class="form-section">
            <div class="form-section-head"><strong>Permissions</strong><span>Replaces the current permission list.</span></div>
            ${permissionPicker('ut_permissions', currentPermissions)}
        </section>
        <section class="form-section">
            <div class="form-section-head"><strong>Memory access</strong><span>No CSV. Choose the intended scope.</span></div>
            <div class="choice-stack">
                <label class="choice-row">
                    <input type="radio" name="ut_memory_mode" value="keep" checked>
                    <span><strong>Keep current access</strong><em>No change to memory scope.</em></span>
                </label>
                <label class="choice-row">
                    <input type="radio" name="ut_memory_mode" value="all">
                    <span><strong>All memories</strong><em>Equivalent to an empty memory list.</em></span>
                </label>
                <label class="choice-row">
                    <input type="radio" name="ut_memory_mode" value="selected">
                    <span><strong>Selected memories</strong><em>Replace access with the selection below.</em></span>
                </label>
            </div>
            <div id="utMemorySelection" class="disabled-block">${memoryCheckboxGrid('ut_memories', currentMemories)}</div>
        </section>
        <div class="form-group"><label class="form-label">Email</label><input class="form-input" id="ut_email" value="${esc(token.email || '')}" data-1p-ignore></div>
        <div id="ut_status" class="text-muted"></div>
    `, 'Update', async () => {
        const args = { token_hash_prefix: hash };
        const permissions = checkedValues('ut_permissions');
        if (!permissions.length) {
            document.getElementById('ut_status').textContent = 'Choose at least one permission.';
            return false;
        }
        args.set_permissions = permissions;
        const memoryMode = document.querySelector('input[name="ut_memory_mode"]:checked')?.value || 'keep';
        if (memoryMode === 'all') args.set_memories = [];
        if (memoryMode === 'selected') {
            const selected = checkedValues('ut_memories');
            if (!selected.length) {
                document.getElementById('ut_status').textContent = 'Choose at least one memory or select all memories.';
                return false;
            }
            args.set_memories = selected;
        }
        const emailValue = rawValue('ut_email').trim();
        if (emailValue !== (token.email || '')) args.set_email = emailValue;
        const result = await callTool('admin_update_token', args);
        if (result.status === 'ok') {
            closeModal();
            await renderTokens();
            showInlineResult('tokensResult', result, 'Update Token');
            return false;
        }
        document.getElementById('ut_status').textContent = result.message || 'Update failed.';
        return false;
    }, { size: 'token-modal' });
    syncTokenMemoryMode();
}

function syncTokenMemoryMode() {
    const mode = document.querySelector('input[name="ut_memory_mode"]:checked')?.value || 'keep';
    const block = document.getElementById('utMemorySelection');
    if (!block) return;
    const enabled = mode === 'selected';
    block.classList.toggle('disabled-block', !enabled);
    block.querySelectorAll('input').forEach(input => { input.disabled = !enabled; });
}

async function renderBackups() {
    const content = document.getElementById('content');
    await loadMemories();
    content.innerHTML = `
        <div class="page">
            <div class="page-header">
                <h2 class="page-title">💾 Backups</h2>
                <div class="toolbar">${memorySelect('backupMemory', true)}<button class="btn-action green" data-action="create-backup">Create Backup</button><button class="btn-action orange" data-action="restore-backup-archive">Restore Archive</button><button class="btn-action blue" data-action="load-backups">Refresh</button></div>
            </div>
            <div id="backupsContent" class="page-loading">Loading...</div>
            <div id="backupsResult"></div>
        </div>`;
    await loadBackups();
}

async function loadBackups() {
    const memoryId = gv('backupMemory');
    showInlineBusy('backupsContent', 'Loading backups...');
    const result = await callTool('backup_list', memoryId ? { memory_id: memoryId } : {});
    if (result.status === 'error') {
        document.getElementById('backupsContent').innerHTML = `<div class="empty">${esc(result.message || 'Unable to load backups.')}</div>`;
        return;
    }
    const backups = result.backups || [];
    const el = document.getElementById('backupsContent');
    if (!el) return;
    if (!backups.length) {
        el.innerHTML = '<div class="empty">No backup found.</div>';
        return;
    }
    el.innerHTML = `<table class="data-table"><thead><tr><th>Backup</th><th>Memory</th><th>Created</th><th>Actions</th></tr></thead><tbody>${
        backups.map(backup => {
            const id = esc(backup.backup_id || backup.id || '');
            return `<tr>
                <td><strong>${id}</strong><br><span class="text-muted">${esc(backup.description || '')}</span></td>
                <td>${esc(backup.memory_id || '')}</td>
                <td>${esc(fmtDate(backup.created_at))}</td>
                <td class="actions-cell">
                    <button class="btn-sm blue" data-action="download-backup" data-backup="${id}">Download</button>
                    <button class="btn-sm orange" data-action="confirm-run" data-tool="backup_restore" data-args='${attrJson({ backup_id: backup.backup_id || backup.id })}' data-message="Restore backup ${id}? The memory must not exist.">Restore</button>
                    <button class="btn-sm red" data-action="confirm-run" data-tool="backup_delete" data-args='${attrJson({ backup_id: backup.backup_id || backup.id })}' data-message="Delete backup ${id}?">Delete</button>
                </td>
            </tr>`;
        }).join('')
    }</tbody></table>`;
}

function showCreateBackup() {
    const selectedMemory = gv('backupMemory');
    showModal('Create Backup', `
        <div class="form-group"><label class="form-label">Memory</label>${memorySelect('bk_memory', true)}</div>
        <div class="form-group"><label class="form-label">Description</label><input class="form-input" id="bk_description" data-1p-ignore></div>
    `, 'Create', async () => {
        const memoryId = gv('bk_memory');
        const target = memoryId || 'all memories';
        const ok = confirm(`Create a backup for ${target}?`);
        if (!ok) return false;
        closeModal();
        showInlineBusy('backupsResult', `Creating backup for ${target}...`);
        const args = { description: gv('bk_description') };
        if (memoryId) args.memory_id = memoryId;
        const result = await callTool('backup_create', args);
        await loadBackups();
        showInlineResult('backupsResult', result, 'Create Backup');
        if (!isSuccess(result)) alert(result.message || 'Backup failed.');
        return false;
    });
    const select = document.getElementById('bk_memory');
    if (select) select.value = selectedMemory;
}

function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
        reader.onerror = () => reject(reader.error || new Error('Unable to read file.'));
        reader.readAsDataURL(file);
    });
}

function showRestoreBackupArchive() {
    showModal('Restore Backup Archive', `
        <div class="form-group"><label class="form-label">Archive tar.gz</label><input class="form-input" id="backupArchiveFile" type="file" accept=".tar.gz,.tgz,application/gzip,application/x-gzip"></div>
        <p class="warning">The target memory must not already exist. This restores graph data and included documents.</p>
        <div id="backupArchiveStatus" class="text-muted"></div>
    `, 'Restore', async () => {
        const file = document.getElementById('backupArchiveFile')?.files?.[0];
        if (!file) {
            document.getElementById('backupArchiveStatus').textContent = 'Choose a backup archive.';
            return false;
        }
        if (!confirm(`Restore archive ${file.name}? The target memory must not exist.`)) return false;
        closeModal();
        showInlineBusy('backupsResult', `Restoring archive ${file.name}...`);
        const archive_base64 = await fileToBase64(file);
        const result = await callTool('backup_restore_archive', { archive_base64 });
        await loadBackups();
        showInlineResult('backupsResult', result, 'Restore Backup Archive');
        if (!isSuccess(result)) alert(result.message || 'Restore failed.');
        return false;
    }, { size: 'wide' });
}

async function downloadBackup(backupId) {
    showInlineBusy('backupsResult', `Preparing archive ${backupId}...`);
    const result = await callTool('backup_download', { backup_id: backupId, include_documents: true });
    if (result.status !== 'ok') {
        showInlineResult('backupsResult', result, 'Download Backup');
        alert(result.message || 'Error');
        return;
    }
    const bytes = atob(result.content_base64 || '');
    const buffer = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i += 1) buffer[i] = bytes.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([buffer], { type: 'application/gzip' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = result.filename || 'backup.tar.gz';
    link.click();
    URL.revokeObjectURL(url);
    showInlineResult('backupsResult', {
        status: 'ok',
        message: `Download started: ${result.filename || 'backup.tar.gz'}`,
        backup_id: result.backup_id,
        size_bytes: result.size_bytes,
        include_documents: result.include_documents,
    }, 'Download Backup');
}

async function renderStorage() {
    const content = document.getElementById('content');
    await loadMemories();
    content.innerHTML = `
        <div class="page">
            <h2 class="page-title">🧹 Storage</h2>
            <div class="maint-list">
                <div class="maint-row"><div class="maint-info"><strong>Check S3 / graph consistency</strong><span class="text-muted">Global check requires admin permission.</span></div><div class="maint-actions storage-check-actions">${memorySelect('storageMemory', true)}<button class="btn-action blue" data-action="storage-check">Check</button></div></div>
                <div class="maint-row"><div class="maint-info"><strong>Cleanup orphan S3 files</strong><span class="text-muted">Dry-run by default. Enable apply to delete.</span></div><div class="maint-actions"><label class="form-check"><input type="checkbox" id="cleanupApply"> apply</label><button class="btn-action orange" data-action="storage-cleanup">Cleanup</button></div></div>
            </div>
            <div id="storageResult"></div>
        </div>`;
}

async function storageCheck() {
    const args = {};
    if (gv('storageMemory')) args.memory_id = gv('storageMemory');
    showInlineBusy('storageResult', 'Checking S3 / graph consistency...');
    const result = await callTool('storage_check', args);
    showInlineResult('storageResult', result, 'Storage Check');
}

async function storageCleanup() {
    if (checked('cleanupApply') && !confirm('Delete orphan S3 files?')) return;
    showInlineBusy('storageResult', checked('cleanupApply') ? 'Deleting orphan S3 files...' : 'Running cleanup dry-run...');
    const result = await callTool('storage_cleanup', { dry_run: !checked('cleanupApply') });
    showInlineResult('storageResult', result, 'Storage Cleanup');
}

async function renderOntologies() {
    const content = document.getElementById('content');
    content.innerHTML = '<div class="page"><div class="page-header"><h2 class="page-title">🧬 Ontologies</h2><button class="btn-action green" data-action="import-ontology">Import Ontology</button></div><div id="ontologiesContent" class="page-loading">Loading...</div><div id="ontologiesResult"></div></div>';
    const ontologies = await loadOntologies();
    document.getElementById('ontologiesContent').innerHTML = `<table class="data-table"><thead><tr><th>Name</th><th>Description</th><th>Entity types</th><th>Relation types</th><th>Actions</th></tr></thead><tbody>${
        ontologies.map(o => `<tr>
            <td><strong>${esc(o.name)}</strong><br><span class="text-muted">v${esc(o.version || '?')}</span></td>
            <td>${esc(o.description || '')}</td>
            <td>${esc(o.entity_types_count ?? '')}</td>
            <td>${esc(o.relation_types_count ?? '')}</td>
            <td class="actions-cell">
                <button class="btn-sm blue" data-action="view-ontology" data-name="${esc(o.name)}">View</button>
                <button class="btn-sm" data-action="edit-ontology" data-name="${esc(o.name)}">Edit</button>
                <button class="btn-sm blue" data-action="export-ontology" data-name="${esc(o.name)}">Export</button>
                <button class="btn-sm red" data-action="delete-ontology" data-name="${esc(o.name)}">Delete</button>
            </td>
        </tr>`).join('')
    }</tbody></table>`;
}

function showImportOntology() {
    showModal('Import Ontology', `
        <div class="form-group"><label class="form-label">YAML file</label><input class="form-input" id="ontologyFile" type="file" accept=".yaml,.yml,text/yaml,text/plain"></div>
        <div class="form-group"><label class="form-label">YAML content</label><textarea class="form-input tall" id="ontologyYaml" rows="16" placeholder="Paste ontology YAML here..."></textarea></div>
        <label class="form-check"><input type="checkbox" id="ontologyOverwrite"> overwrite existing ontology</label>
        <div id="ontologyImportStatus" class="text-muted"></div>
    `, 'Import', async () => {
        let content = rawValue('ontologyYaml').trim();
        const file = document.getElementById('ontologyFile')?.files?.[0];
        if (!content && file) content = await file.text();
        if (!content) {
            document.getElementById('ontologyImportStatus').textContent = 'Paste YAML or choose a file.';
            return false;
        }
        const result = await callTool('ontology_import', { content_yaml: content, overwrite: checked('ontologyOverwrite') });
        if (isSuccess(result)) {
            closeModal();
            await renderOntologies();
            showInlineResult('ontologiesResult', result, 'Import Ontology');
            return false;
        }
        document.getElementById('ontologyImportStatus').textContent = result.message || 'Import failed.';
        return false;
    }, { size: 'wide' });

    const fileInput = document.getElementById('ontologyFile');
    fileInput?.addEventListener('change', async () => {
        const file = fileInput.files?.[0];
        if (!file) return;
        document.getElementById('ontologyYaml').value = await file.text();
    });
}

async function viewOntology(name) {
    showInlineBusy('ontologiesResult', `Loading ontology ${name}...`);
    const result = await callTool('ontology_get', { name });
    if (result.status !== 'ok') {
        showInlineResult('ontologiesResult', result, 'Load Ontology');
        return;
    }
    showInlineResult('ontologiesResult', result, 'Load Ontology', {
        modal: false,
        yaml: result.content || '',
        initialTab: 'yaml',
    });
    showResultModal(`Ontology: ${name}`, result, {
        yaml: result.content || '',
        initialTab: 'yaml',
        size: 'wide',
    });
}

async function editOntology(name) {
    const result = await callTool('ontology_get', { name });
    if (result.status !== 'ok') {
        showInlineResult('ontologiesResult', result, 'Load Ontology');
        return;
    }
    showModal(`Edit Ontology: ${name}`, `
        ${resultTabsHtml([
            {
                id: 'editor',
                label: 'Editor',
                body: '<div class="form-group"><label class="form-label">YAML content</label><textarea class="form-input tall ontology-editor" id="ontologyEditYaml" rows="28">' + esc(result.content || '') + '</textarea></div>',
            },
            {
                id: 'preview',
                label: 'Preview',
                body: '<pre class="pretty-code yaml-code" id="ontologyEditPreview">' + highlightYaml(result.content || '') + '</pre>',
            },
        ], 'editor')}
        <div id="ontologyEditStatus" class="text-muted"></div>
    `, 'Save', async () => {
        const updated = rawValue('ontologyEditYaml');
        const save = await callTool('ontology_update', { name, content_yaml: updated });
        if (isSuccess(save)) {
            closeModal();
            await renderOntologies();
            showInlineResult('ontologiesResult', save, 'Update Ontology');
            return false;
        }
        document.getElementById('ontologyEditStatus').textContent = save.message || 'Save failed.';
        return false;
    }, { size: 'wide' });
    const editor = document.getElementById('ontologyEditYaml');
    const preview = document.getElementById('ontologyEditPreview');
    editor?.addEventListener('input', () => {
        if (preview) preview.innerHTML = highlightYaml(editor.value);
    });
}

async function exportOntology(name) {
    showInlineBusy('ontologiesResult', `Exporting ontology ${name}...`);
    const result = await callTool('ontology_export', { name });
    if (result.status !== 'ok') {
        showInlineResult('ontologiesResult', result, 'Export Ontology');
        return;
    }
    downloadText(result.filename || `${name}.yaml`, result.content || '');
    showInlineResult('ontologiesResult', { status: 'ok', message: `Export started: ${result.filename || name + '.yaml'}`, name }, 'Export Ontology');
}

async function deleteOntology(name) {
    const force = confirm(`Delete ontology ${name}? If it is used by a memory, deletion will be refused unless you confirm force deletion next.`);
    if (!force) return;
    showInlineBusy('ontologiesResult', `Deleting ontology ${name}...`);
    let result = await callTool('ontology_delete', { name, force: false });
    if (result.status === 'error' && result.used_by?.length) {
        const forceDelete = confirm(`${result.message}\n\nForce delete anyway?`);
        if (forceDelete) result = await callTool('ontology_delete', { name, force: true });
    }
    await renderOntologies();
    showInlineResult('ontologiesResult', result, 'Delete Ontology');
    if (!isSuccess(result)) alert(result.message || 'Delete failed.');
}

function downloadText(filename, content) {
    const url = URL.createObjectURL(new Blob([content], { type: 'text/yaml;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
}

async function runAndShow(tool, args) {
    const result = await callTool(tool, args);
    showResultModal(tool, result);
}

async function runConfirmedAction(data) {
    if (!confirm(data.message || 'Confirm this action?')) return;
    const args = JSON.parse(data.args || '{}');
    const resultId = activeCat === 'backups' ? 'backupsResult' : activeCat === 'storage' ? 'storageResult' : '';
    if (resultId) showInlineBusy(resultId, `Running ${data.tool}...`);
    const result = await callTool(data.tool, args);
    if (resultId) showInlineResult(resultId, result, data.tool);
    else showResultModal(data.tool, result);
    if (isSuccess(result) && activeCat === 'backups') await loadBackups();
    if (!isSuccess(result)) alert(result.message || 'Action failed.');
}

function showModal(title, body, okLabel, onOk, options = {}) {
    closeModal();
    const variant = typeof options === 'string' ? options : options.size || '';
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'modalOverlay';
    overlay.innerHTML = `
        <div class="modal-card ${esc(variant)}">
            <div class="modal-header"><h3>${esc(title)}</h3><button class="modal-close" data-action="close-modal">x</button></div>
            <div class="modal-body">${body}</div>
            <div class="modal-footer"><button class="btn-action blue" id="modalOk">${esc(okLabel || 'OK')}</button></div>
        </div>`;
    document.body.appendChild(overlay);
    document.getElementById('modalOk').addEventListener('click', async () => {
        const shouldClose = onOk ? await onOk() : true;
        if (shouldClose) closeModal();
    });
}

function closeModal() {
    document.getElementById('modalOverlay')?.remove();
}

(async function init() {
    buildSidebar();
    const session = await checkSession();
    if (!session) {
        showLogin();
        return;
    }
    cache.identity = session;
    hideLogin();
    await warmCache();
    showCategory('dashboard');
})();
