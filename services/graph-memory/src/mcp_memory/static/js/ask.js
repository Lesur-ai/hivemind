/**
 * MCP Memory - Panneau ASK (question/réponse) avec highlight graphe,
 * mode "Focus Question" (isolation du sous-graphe pertinent),
 * redimensionnement par drag, et export HTML de la réponse.
 *
 * CSP-compliant : aucun inline handler (onclick) ni inline style.
 * Les événements dynamiques utilisent la délégation sur #askBody.
 * Les classes CSS remplacent les styles inline.
 */

// Stocke les entités de la dernière réponse (pour le bouton Isoler)
let lastAnswerEntities = [];
// Stocke le dernier résultat complet (pour l'export HTML)
let lastAnswerResult = null;
// Stocke la dernière question posée (pour l'export HTML)
let lastQuestion = '';

// ═══════════════ PANNEAU ASK ═══════════════

/** Ouvre le panneau ASK */
function showAskPanel() {
    document.getElementById('askPanel').classList.add('visible');
    document.getElementById('askInput').focus();
}

/** Ferme le panneau ASK et efface la surbrillance */
function hideAskPanel() {
    document.getElementById('askPanel').classList.remove('visible');
    clearHighlight();
}

/** Soumet une question */
async function submitQuestion() {
    const input = document.getElementById('askInput');
    const question = input.value.trim();
    if (!question || !appState.currentMemory) return;

    // Sortir du mode Focus si actif, pour que la réponse s'affiche
    // sur le graphe complet (pas filtré par l'isolation précédente)
    if (filterState.isolatedNodes !== null) {
        exitIsolation();
    }

    const body = document.getElementById('askBody');
    const btn = document.getElementById('askSubmitBtn');

    // Afficher le loading (classes CSS au lieu de styles inline)
    body.innerHTML = '<div class="ask-loading"><div class="spinner ask-spinner-sm"></div>Réflexion en cours…</div>';
    btn.disabled = true;
    lastAnswerEntities = [];
    lastAnswerResult = null;
    lastQuestion = question;

    try {
        const result = await apiAsk(appState.currentMemory, question);

        if (result.status === 'ok') {
            lastAnswerResult = result;
            displayAnswer(result);
            // Mettre en évidence les entités dans le graphe
            if (result.entities && result.entities.length > 0) {
                lastAnswerEntities = result.entities;
                highlightEntities(result.entities);
            }
        } else {
            body.innerHTML = `<div class="text-error">❌ ${result.message || 'Erreur'}</div>`;
        }
    } catch (err) {
        body.innerHTML = `<div class="text-error">❌ Erreur réseau: ${err.message}</div>`;
    } finally {
        btn.disabled = false;
    }
}

/** Affiche la réponse dans le panneau */
function displayAnswer(result) {
    const body = document.getElementById('askBody');

    // Convertir le markdown en HTML avec marked.js
    const answerHtml = marked.parse(result.answer || '', { breaks: true, gfm: true });

    let html = `<div class="ask-answer">${answerHtml}</div>`;

    // Entités trouvées (cliquables → focus dans le graphe)
    if (result.entities && result.entities.length > 0) {
        html += '<div class="ask-entities"><span class="ask-entities-label">🔗 Entités:</span>';
        result.entities.forEach(name => {
            html += `<span class="ask-entity-tag" data-action="focus-node" data-node-id="${escapeHtml(name)}" title="Voir dans le graphe">${escapeHtml(name)}</span>`;
        });
        html += '</div>';
    }

    // Documents sources (affichage en liste)
    if (result.source_documents && result.source_documents.length > 0) {
        html += `<div class="ask-sources">
            <div class="ask-sources-label">📄 Sources (${result.source_documents.length}):</div>`;
        result.source_documents.forEach(doc => {
            const name = doc.filename || doc.id || '?';
            html += `<div class="ask-source-item">📄 ${escapeHtml(name)}</div>`;
        });
        html += '</div>';
    }

    // Barre d'actions (Isoler + Export HTML)
    html += '<div class="ask-actions">';

    if (result.entities && result.entities.length > 0) {
        html += `<button class="ask-isolate-btn" data-action="isolate-from-ask" title="Afficher uniquement le sous-graphe lié à cette question">
            🔬 Isoler le sujet
        </button>`;
    }

    // Bouton Export HTML (toujours visible quand il y a une réponse)
    html += `<button class="ask-export-btn" data-action="export-answer-html" title="Télécharger la réponse en HTML">
        📥 Export HTML
    </button>`;

    html += '</div>';

    body.innerHTML = html;
}

/** Déclenche l'isolation du sous-graphe depuis le bouton dans le panneau ASK */
function isolateFromAsk() {
    if (lastAnswerEntities.length > 0) {
        isolateSubgraph(lastAnswerEntities);
    }
}

// ═══════════════ EXPORT HTML ═══════════════

/**
 * Génère un fichier HTML autonome avec la réponse formatée et le télécharge.
 * Le HTML contient le CSS inline pour un beau rendu même hors du site.
 * Note: le fichier téléchargé n'est pas affecté par la CSP de la page.
 */
function exportAnswerHtml() {
    if (!lastAnswerResult) return;

    const result = lastAnswerResult;
    const answerHtml = marked.parse(result.answer || '', { breaks: true, gfm: true });
    const now = new Date();
    const dateStr = now.toLocaleDateString('fr-FR', { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const memoryId = appState.currentMemory || 'inconnu';

    // Construire les sections optionnelles
    let entitiesSection = '';
    if (result.entities && result.entities.length > 0) {
        const tags = result.entities.map(e => `<span class="entity-tag">${escapeHtml(e)}</span>`).join(' ');
        entitiesSection = `<div class="section"><h3>🔗 Entités identifiées</h3><div class="entities">${tags}</div></div>`;
    }

    let sourcesSection = '';
    if (result.source_documents && result.source_documents.length > 0) {
        const items = result.source_documents.map(doc => {
            const name = doc.filename || doc.id || '?';
            return `<div class="source-item">📄 ${escapeHtml(name)}</div>`;
        }).join('');
        sourcesSection = `<div class="section"><h3>📄 Documents sources</h3>${items}</div>`;
    }

    const htmlContent = `<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Graph Memory — Réponse</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            background: #f5f7fa; color: #2d3748; line-height: 1.6;
            padding: 2rem;
        }
        .container {
            max-width: 900px; margin: 0 auto;
            background: #fff; border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            overflow: hidden;
        }
        .header-bar {
            background: linear-gradient(135deg, #2d3748 0%, #1a202c 100%);
            padding: 1.5rem 2rem;
            border-bottom: 3px solid #41a890;
            display: flex; align-items: center; justify-content: space-between;
        }
        .header-bar h1 {
            color: #fff; font-size: 1.2rem; font-weight: 500;
        }
        .header-bar .brand {
            color: #41a890; font-size: 0.85rem; font-weight: 600;
        }
        .meta {
            padding: 1rem 2rem;
            background: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
            font-size: 0.85rem; color: #718096;
            display: flex; gap: 2rem; flex-wrap: wrap;
        }
        .meta span { display: flex; align-items: center; gap: 0.3rem; }
        .question-box {
            margin: 1.5rem 2rem; padding: 1rem 1.2rem;
            background: linear-gradient(135deg, #ebf4ff 0%, #e8f5f0 100%);
            border-left: 4px solid #41a890;
            border-radius: 0 8px 8px 0;
            font-size: 1.05rem; font-weight: 500; color: #2d3748;
        }
        .answer {
            padding: 1.5rem 2rem;
            font-size: 0.95rem; line-height: 1.7; color: #2d3748;
        }
        .answer h1, .answer h2, .answer h3 { color: #1a365d; margin: 1rem 0 0.5rem; }
        .answer h1 { font-size: 1.4rem; }
        .answer h2 { font-size: 1.2rem; }
        .answer h3 { font-size: 1.05rem; }
        .answer ul, .answer ol { padding-left: 1.8rem; margin: 0.5rem 0; }
        .answer li { margin: 0.3rem 0; }
        .answer code {
            background: #edf2f7; padding: 0.15rem 0.4rem;
            border-radius: 4px; font-size: 0.88rem; color: #e53e3e;
        }
        .answer pre {
            background: #2d3748; color: #e2e8f0;
            padding: 1rem; border-radius: 8px; overflow-x: auto;
            margin: 0.8rem 0; font-size: 0.85rem;
        }
        .answer pre code { background: none; color: inherit; padding: 0; }
        .answer strong { color: #1a202c; }
        .answer table {
            border-collapse: collapse; width: 100%; margin: 0.8rem 0;
        }
        .answer th, .answer td {
            border: 1px solid #e2e8f0; padding: 0.5rem 0.8rem; text-align: left;
        }
        .answer th { background: #f7fafc; color: #2d3748; font-weight: 600; }
        .answer tr:nth-child(even) { background: #f8fafc; }
        .answer blockquote {
            border-left: 4px solid #41a890; padding: 0.5rem 1rem; margin: 0.8rem 0;
            color: #718096; font-style: italic; background: #f8fafc;
            border-radius: 0 6px 6px 0;
        }
        .answer hr { border: none; border-top: 1px solid #e2e8f0; margin: 1rem 0; }
        .section {
            padding: 1rem 2rem; border-top: 1px solid #e2e8f0;
        }
        .section h3 {
            font-size: 0.9rem; color: #4a5568; margin-bottom: 0.5rem;
        }
        .entities { display: flex; flex-wrap: wrap; gap: 0.4rem; }
        .entity-tag {
            padding: 0.25rem 0.6rem; border-radius: 6px; font-size: 0.8rem;
            background: #e6fffa; color: #234e52; border: 1px solid #b2f5ea;
            font-weight: 500;
        }
        .source-item {
            padding: 0.4rem 0.6rem; margin: 0.2rem 0; border-radius: 6px;
            font-size: 0.85rem; color: #c53030;
            background: #fff5f5; border-left: 3px solid #fc8181;
        }
        .footer {
            padding: 1rem 2rem; background: #f8fafc;
            border-top: 1px solid #e2e8f0;
            font-size: 0.75rem; color: #a0aec0; text-align: center;
        }
        @media print {
            body { padding: 0; background: #fff; }
            .container { box-shadow: none; border-radius: 0; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-bar">
            <h1>Graph Memory — Réponse</h1>
            <span class="brand">Cloud Temple</span>
        </div>
        <div class="meta">
            <span>📅 ${escapeHtml(dateStr)}</span>
            <span>🧠 Mémoire : <strong>${escapeHtml(memoryId)}</strong></span>
        </div>
        <div class="question-box">💬 ${escapeHtml(lastQuestion)}</div>
        <div class="answer">${answerHtml}</div>
        ${entitiesSection}
        ${sourcesSection}
        <div class="footer">
            Généré par Graph Memory — Cloud Temple — ${escapeHtml(dateStr)}
        </div>
    </div>
</body>
</html>`;

    // Créer le blob et déclencher le téléchargement
    const blob = new Blob([htmlContent], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    // Nom de fichier : graph-memory-YYYY-MM-DD-HHmm.html
    const ts = now.toISOString().replace(/[T:]/g, '-').substring(0, 16);
    a.download = `graph-memory-${ts}.html`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// ═══════════════ REDIMENSIONNEMENT PAR DRAG ═══════════════

/**
 * Initialise le drag-to-resize sur la poignée du panneau ASK.
 * Tirer vers le haut = panneau plus grand (graphe plus petit).
 * Tirer vers le bas = panneau plus petit (graphe plus grand).
 */
function setupAskResize() {
    const handle = document.getElementById('askResizeHandle');
    const panel = document.getElementById('askPanel');
    const graphContainer = panel.parentElement; // .graph-container

    let isDragging = false;
    let startY = 0;
    let startHeight = 0;

    handle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        isDragging = true;
        startY = e.clientY;
        startHeight = panel.offsetHeight;
        handle.classList.add('dragging');
        document.body.style.cursor = 'ns-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        // Delta vers le haut = négatif = panel grandit
        const delta = startY - e.clientY;
        const containerHeight = graphContainer.offsetHeight;
        const minH = 100;
        const maxH = containerHeight * 0.8;
        const newHeight = Math.min(maxH, Math.max(minH, startHeight + delta));
        panel.style.height = newHeight + 'px';
    });

    document.addEventListener('mouseup', () => {
        if (!isDragging) return;
        isDragging = false;
        handle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        // Forcer vis-network à se recalculer si présent
        if (appState.network) {
            appState.network.redraw();
        }
    });
}

// ═══════════════ UTILITAIRES ═══════════════

/** Échappe le HTML pour éviter les injections */
function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

// ═══════════════ SETUP ═══════════════

/** Setup des événements ASK */
function setupAsk() {
    // Bouton Ask dans le header
    document.getElementById('askBtn').addEventListener('click', showAskPanel);

    // Bouton submit
    document.getElementById('askSubmitBtn').addEventListener('click', submitQuestion);

    // Fermer
    document.getElementById('askCloseBtn').addEventListener('click', hideAskPanel);

    // Enter pour soumettre
    document.getElementById('askInput').addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submitQuestion();
        }
    });

    // Initialiser le drag-to-resize
    setupAskResize();

    // --- Event delegation sur le body ASK (CSP-safe) ---
    // Les boutons et tags sont générés dynamiquement dans displayAnswer()
    document.getElementById('askBody').addEventListener('click', (e) => {
        // Entités cliquables → focus dans le graphe
        const entityTag = e.target.closest('[data-action="focus-node"]');
        if (entityTag) {
            focusNode(entityTag.dataset.nodeId);
            return;
        }
        // Bouton Isoler
        if (e.target.closest('[data-action="isolate-from-ask"]')) {
            isolateFromAsk();
            return;
        }
        // Bouton Export HTML
        if (e.target.closest('[data-action="export-answer-html"]')) {
            exportAnswerHtml();
            return;
        }
    });
}
