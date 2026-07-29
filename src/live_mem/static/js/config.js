/**
 * Live Memory - Configuration, état global, helpers
 */

const AGENT_COLORS = [
    '#3498db','#2ecc71','#e74c3c','#9b59b6','#f39c12',
    '#1abc9c','#e67e22','#2980b9','#c0392b','#8e44ad'
];
const CATEGORY_COLORS = {
    observation: {bg:'rgba(52,152,219,0.2)',border:'#3498db',text:'#3498db'},
    decision:    {bg:'rgba(46,204,113,0.2)',border:'#2ecc71',text:'#2ecc71'},
    todo:        {bg:'rgba(241,196,15,0.2)',border:'#f1c40f',text:'#f1c40f'},
    insight:     {bg:'rgba(155,89,182,0.2)',border:'#9b59b6',text:'#9b59b6'},
    question:    {bg:'rgba(230,126,34,0.2)',border:'#e67e22',text:'#e67e22'},
    progress:    {bg:'rgba(26,188,156,0.2)',border:'#1abc9c',text:'#1abc9c'},
    issue:       {bg:'rgba(231,76,60,0.2)', border:'#e74c3c',text:'#e74c3c'},
};
const CATEGORY_ICONS = {
    observation:'👁️',decision:'✅',todo:'📋',insight:'💡',
    question:'❓',progress:'📈',issue:'⚠️',
};

// État global — 1 seul espace à la fois
const app = {
    spaceId: null,
    info: null,
    notes: [],
    bankFiles: [],
    currentBankFile: null,
    agentColors: {},
    refreshTimer: null,
    refreshInterval: 5,
    _noteHash: '',
    _bankHash: '',
    _bankRequestGeneration: 0,
};

function getAgentColor(name) {
    if (!app.agentColors[name]) {
        app.agentColors[name] = AGENT_COLORS[Object.keys(app.agentColors).length % AGENT_COLORS.length];
    }
    return app.agentColors[name];
}
function getCatStyle(c) { return CATEGORY_COLORS[c] || {bg:'rgba(255,255,255,0.1)',border:'#666',text:'#888'}; }
function getCatIcon(c) { return CATEGORY_ICONS[c] || '📝'; }
// HM-15 fix : échappe aussi " et ' (aligné sur admin-app.js esc()). esc() est
// utilisé dans des contextes d'attribut HTML (ex: bank.js data-filename="${...}"),
// où un guillemet non échappé permettrait une injection d'attribut.
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function fmtTime(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
    catch { return iso; }
}
function fmtDate(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleDateString('en-US',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); }
    catch { return iso; }
}
function fmtSize(b) {
    if (!b) return '';
    if (b<1024) return b+' B';
    if (b<1048576) return (b/1024).toFixed(1)+' KB';
    return (b/1048576).toFixed(1)+' MB';
}
// LM2-19 fix : sanitisation systématique du HTML produit par marked.
// Marked ≥4 ne supporte plus l'option `sanitize` — un contenu Markdown
// malicieux (ex: une image avec gestionnaire d'erreur inline ou un lien
// `javascript:`) injecté
// dans une note live ou un fichier bank exécutait sinon du JS arbitraire
// dans le navigateur de l'admin (vol du token bearer via XSS).
// DOMPurify est chargé localement (LM2-06 fix) — voir static/vendor/README.md.
function md(text) {
    try {
        const raw = marked.parse(text || '', { breaks: true, gfm: true });
        // Fallback defense in depth : si DOMPurify n'est pas chargé pour
        // une raison quelconque, on échappe brutalement plutôt que de
        // servir du HTML non sanitisé.
        if (typeof DOMPurify === 'undefined') {
            console.warn('[md] DOMPurify non disponible — fallback esc()');
            return '<p>' + esc(text) + '</p>';
        }
        return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
    } catch {
        return '<p>' + esc(text) + '</p>';
    }
}
