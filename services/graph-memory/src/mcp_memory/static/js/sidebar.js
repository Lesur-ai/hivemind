/**
 * MCP Memory - Sidebar : stats, filtres interactifs, liste d'entités
 *
 * Trois panneaux de filtrage pliables :
 *   1. Types d'entités (checkboxes avec pastilles couleur)
 *   2. Types de relations (checkboxes avec barres couleur)
 *   3. Documents (checkboxes par document)
 *
 * Chaque changement de filtre appelle applyFilters() (défini dans config.js).
 *
 * CSP-compliant : aucun inline handler (onclick/onchange) ni inline style.
 * Les couleurs dynamiques sont appliquées via element.style après innerHTML.
 * Les événements utilisent la délégation (listeners sur les conteneurs parents).
 */

// ═══════════════ COULEURS DYNAMIQUES (CSP-safe) ═══════════════

/**
 * Applique les couleurs stockées dans data-color sur les éléments d'un conteneur.
 * Utilise element.style.xxx (API DOM, autorisé par CSP) au lieu de style="".
 */
function applyDynamicColors(container) {
    container.querySelectorAll('[data-color]').forEach(el => {
        el.style.background = el.dataset.color;
    });
    container.querySelectorAll('[data-border-color]').forEach(el => {
        el.style.borderLeftColor = el.dataset.borderColor;
    });
}

// ═══════════════ STATS ═══════════════

function updateStats(nodeCount, edgeCount) {
    document.getElementById('nodeCount').textContent = nodeCount;
    document.getElementById('edgeCount').textContent = edgeCount;
}

// ═══════════════ SECTIONS PLIABLES ═══════════════

/** Toggle une section de filtre (plier/déplier) */
function toggleFilterSection(sectionId) {
    const body = document.getElementById('body-' + sectionId);
    const chevron = document.getElementById('chevron-' + sectionId);
    const actions = document.getElementById('actions-' + sectionId);

    if (body.classList.contains('collapsed')) {
        body.classList.remove('collapsed');
        chevron.classList.remove('collapsed');
        if (actions) actions.classList.remove('hidden');
    } else {
        body.classList.add('collapsed');
        chevron.classList.add('collapsed');
        if (actions) actions.classList.add('hidden');
    }
}

// ═══════════════ FILTRE : TYPES D'ENTITÉS ═══════════════

/** Génère les checkboxes pour les types d'entités */
function buildEntityTypeFilters(nodes) {
    const body = document.getElementById('body-entityTypes');

    // Compter les nœuds par type (exclure les documents)
    const typeCounts = {};
    nodes.forEach(n => {
        if (n.node_type === 'document') return;
        typeCounts[n.type] = (typeCounts[n.type] || 0) + 1;
    });

    const types = Object.keys(typeCounts).sort();
    if (types.length === 0) {
        body.innerHTML = '<div class="filter-empty">Aucune entité</div>';
        return;
    }

    body.innerHTML = types.map(type => {
        const color = TYPE_COLORS[type] || TYPE_COLORS.Unknown;
        const checked = filterState.visibleEntityTypes.has(type) ? 'checked' : '';
        const dimmed = checked ? '' : 'dimmed';
        return `
            <label class="filter-item" title="${type} (${typeCounts[type]})">
                <input type="checkbox" ${checked}
                       data-action="toggle-entity-type" data-type="${type}">
                <div class="filter-color" data-color="${color}"></div>
                <span class="filter-label ${dimmed}" id="label-etype-${type}">${type}</span>
                <span class="filter-count">${typeCounts[type]}</span>
            </label>`;
    }).join('');

    // Appliquer les couleurs dynamiques (CSP-safe)
    applyDynamicColors(body);
}

/** Toggle un type d'entité */
function toggleEntityType(type, visible) {
    if (visible) {
        filterState.visibleEntityTypes.add(type);
    } else {
        filterState.visibleEntityTypes.delete(type);
    }
    // Mettre à jour le style du label
    const label = document.getElementById('label-etype-' + type);
    if (label) label.classList.toggle('dimmed', !visible);

    applyFilters();
}

/** Tous les types d'entités visibles */
function selectAllEntityTypes() {
    if (!appState.currentData) return;
    const types = new Set(appState.currentData.nodes.filter(n => n.node_type !== 'document').map(n => n.type));
    filterState.visibleEntityTypes = types;
    // Mettre à jour les checkboxes
    document.querySelectorAll('#body-entityTypes input[type="checkbox"]').forEach(cb => {
        cb.checked = true;
    });
    document.querySelectorAll('#body-entityTypes .filter-label').forEach(l => {
        l.classList.remove('dimmed');
    });
    applyFilters();
}

/** Aucun type d'entité visible */
function selectNoEntityTypes() {
    filterState.visibleEntityTypes.clear();
    document.querySelectorAll('#body-entityTypes input[type="checkbox"]').forEach(cb => {
        cb.checked = false;
    });
    document.querySelectorAll('#body-entityTypes .filter-label').forEach(l => {
        l.classList.add('dimmed');
    });
    applyFilters();
}

/** Inverser la sélection des types d'entités */
function invertEntityTypes() {
    if (!appState.currentData) return;
    const allTypes = new Set(appState.currentData.nodes.filter(n => n.node_type !== 'document').map(n => n.type));
    const newVisible = new Set();
    allTypes.forEach(t => {
        if (!filterState.visibleEntityTypes.has(t)) newVisible.add(t);
    });
    filterState.visibleEntityTypes = newVisible;
    // Mettre à jour les checkboxes
    document.querySelectorAll('#body-entityTypes input[type="checkbox"]').forEach(cb => {
        const type = cb.closest('.filter-item').querySelector('.filter-label').textContent.trim();
        const visible = filterState.visibleEntityTypes.has(type);
        cb.checked = visible;
        cb.closest('.filter-item').querySelector('.filter-label').classList.toggle('dimmed', !visible);
    });
    applyFilters();
}

// ═══════════════ FILTRE : TYPES DE RELATIONS ═══════════════

/** Génère les checkboxes pour les types de relations */
function buildEdgeTypeFilters(edges) {
    const body = document.getElementById('body-edgeTypes');

    // Compter les arêtes par type
    const typeCounts = {};
    edges.forEach(e => {
        typeCounts[e.type] = (typeCounts[e.type] || 0) + 1;
    });

    const types = Object.keys(typeCounts).sort();
    if (types.length === 0) {
        body.innerHTML = '<div class="filter-empty">Aucune relation</div>';
        return;
    }

    body.innerHTML = types.map(type => {
        const color = EDGE_COLORS[type] || '#556';
        const checked = filterState.visibleEdgeTypes.has(type) ? 'checked' : '';
        const dimmed = checked ? '' : 'dimmed';
        const displayName = type.replace(/_/g, ' ');
        return `
            <label class="filter-item" title="${displayName} (${typeCounts[type]})">
                <input type="checkbox" ${checked}
                       data-action="toggle-edge-type" data-type="${type}">
                <div class="filter-edge-color" data-color="${color}"></div>
                <span class="filter-label ${dimmed}" id="label-etype-edge-${type}">${displayName}</span>
                <span class="filter-count">${typeCounts[type]}</span>
            </label>`;
    }).join('');

    // Appliquer les couleurs dynamiques (CSP-safe)
    applyDynamicColors(body);
}

/** Toggle un type de relation */
function toggleEdgeType(type, visible) {
    if (visible) {
        filterState.visibleEdgeTypes.add(type);
    } else {
        filterState.visibleEdgeTypes.delete(type);
    }
    const label = document.getElementById('label-etype-edge-' + type);
    if (label) label.classList.toggle('dimmed', !visible);
    applyFilters();
}

/** Tous les types de relations visibles */
function selectAllEdgeTypes() {
    if (!appState.currentData) return;
    const types = new Set(appState.currentData.edges.map(e => e.type));
    filterState.visibleEdgeTypes = types;
    document.querySelectorAll('#body-edgeTypes input[type="checkbox"]').forEach(cb => {
        cb.checked = true;
    });
    document.querySelectorAll('#body-edgeTypes .filter-label').forEach(l => {
        l.classList.remove('dimmed');
    });
    applyFilters();
}

/** Aucun type de relation visible */
function selectNoEdgeTypes() {
    filterState.visibleEdgeTypes.clear();
    document.querySelectorAll('#body-edgeTypes input[type="checkbox"]').forEach(cb => {
        cb.checked = false;
    });
    document.querySelectorAll('#body-edgeTypes .filter-label').forEach(l => {
        l.classList.add('dimmed');
    });
    applyFilters();
}

/** Inverser la sélection des types de relations */
function invertEdgeTypes() {
    if (!appState.currentData) return;
    const allTypes = new Set(appState.currentData.edges.map(e => e.type));
    const newVisible = new Set();
    allTypes.forEach(t => {
        if (!filterState.visibleEdgeTypes.has(t)) newVisible.add(t);
    });
    filterState.visibleEdgeTypes = newVisible;
    document.querySelectorAll('#body-edgeTypes input[type="checkbox"]').forEach(cb => {
        const label = cb.closest('.filter-item').querySelector('.filter-label');
        // Retrouver le type original (avec underscores) depuis l'ID du label
        const labelId = label.id || '';
        const type = labelId.replace('label-etype-edge-', '');
        const visible = filterState.visibleEdgeTypes.has(type);
        cb.checked = visible;
        label.classList.toggle('dimmed', !visible);
    });
    applyFilters();
}

// ═══════════════ FILTRE : DOCUMENTS ═══════════════

/** Génère les checkboxes pour les documents */
function buildDocumentFilters(documents) {
    const body = document.getElementById('body-documents');

    if (!documents || documents.length === 0) {
        body.innerHTML = '<div class="filter-empty">Aucun document</div>';
        return;
    }

    body.innerHTML = documents.map(doc => {
        const checked = filterState.visibleDocuments.has(doc.id) ? 'checked' : '';
        const dimmed = checked ? '' : 'dimmed';
        const name = doc.filename || doc.id;
        const shortName = name.length > 30 ? name.substring(0, 28) + '…' : name;
        return `
            <label class="filter-item" title="${name}">
                <input type="checkbox" ${checked}
                       data-action="toggle-document" data-doc-id="${doc.id}">
                <div class="filter-color filter-color-doc"></div>
                <span class="filter-label ${dimmed}" id="label-doc-${doc.id}">${shortName}</span>
            </label>`;
    }).join('');
}

/** Toggle un document */
function toggleDocument(docId, visible) {
    if (visible) {
        filterState.visibleDocuments.add(docId);
    } else {
        filterState.visibleDocuments.delete(docId);
    }
    const label = document.getElementById('label-doc-' + docId);
    if (label) label.classList.toggle('dimmed', !visible);
    applyFilters();
}

/** Tous les documents visibles */
function selectAllDocuments() {
    if (!appState.currentData) return;
    const docIds = new Set((appState.currentData.documents || []).map(d => d.id));
    filterState.visibleDocuments = docIds;
    document.querySelectorAll('#body-documents input[type="checkbox"]').forEach(cb => {
        cb.checked = true;
    });
    document.querySelectorAll('#body-documents .filter-label').forEach(l => {
        l.classList.remove('dimmed');
    });
    applyFilters();
}

/** Aucun document visible */
function selectNoDocuments() {
    filterState.visibleDocuments.clear();
    document.querySelectorAll('#body-documents input[type="checkbox"]').forEach(cb => {
        cb.checked = false;
    });
    document.querySelectorAll('#body-documents .filter-label').forEach(l => {
        l.classList.add('dimmed');
    });
    applyFilters();
}

// ═══════════════ LISTE D'ENTITÉS ═══════════════

function updateEntityList(nodes) {
    const list = document.getElementById('entityList');
    const countSpan = document.getElementById('entityListCount');
    const sorted = [...nodes].sort((a, b) => (b.mentions || 0) - (a.mentions || 0));

    if (countSpan) countSpan.textContent = `(${sorted.length})`;

    list.innerHTML = sorted.slice(0, 80).map(n => {
        const color = TYPE_COLORS[n.type] || TYPE_COLORS.Unknown;
        return `
        <div class="entity-item" data-action="focus-node" data-node-id="${n.id}"
             data-border-color="${color}">
            ${n.label.substring(0, 35)}${n.label.length > 35 ? '…' : ''}
            <div class="type">${n.type}</div>
        </div>`;
    }).join('');

    if (sorted.length > 80) {
        list.innerHTML += `<div class="entity-overflow">… +${sorted.length - 80} entités</div>`;
    }

    // Appliquer les couleurs dynamiques (border-left)
    applyDynamicColors(list);
}

// ═══════════════ RECHERCHE LOCALE ═══════════════

/** Filtre local d'entités dans la sidebar + sélection dans le graphe */
function setupSearchFilter() {
    document.getElementById('searchInput').addEventListener('input', function () {
        const q = this.value.toLowerCase().trim();
        if (!appState.currentData || !appState.network) return;

        if (!q) {
            appState.network.unselectAll();
            // Restaurer la liste selon les filtres actifs
            const visibleNodes = appState.currentData.nodes.filter(n =>
                n.node_type !== 'document' && filterState.visibleEntityTypes.has(n.type)
            );
            updateEntityList(visibleNodes);
            return;
        }

        const matches = appState.currentData.nodes.filter(n =>
            n.node_type !== 'document' && (
                n.label.toLowerCase().includes(q) ||
                (n.description || '').toLowerCase().includes(q) ||
                (n.type || '').toLowerCase().includes(q)
            )
        );
        updateEntityList(matches);

        if (matches.length > 0 && matches.length <= 20) {
            const ids = matches.map(n => n.id);
            appState.network.selectNodes(ids);
            if (matches.length === 1) {
                appState.network.focus(ids[0], { scale: 1.5, animation: true });
            }
        }
    });
}

// ═══════════════ EVENT DELEGATION (CSP-safe) ═══════════════

/**
 * Initialise la délégation d'événements sur la sidebar.
 * Un seul listener par conteneur parent, dispatche via data-action.
 * Appelé une fois au démarrage (DOMContentLoaded).
 */
function setupSidebarEvents() {
    // --- Filter headers (toggle collapse) ---
    document.querySelectorAll('[data-action="toggle-section"]').forEach(el => {
        el.addEventListener('click', () => toggleFilterSection(el.dataset.section));
    });

    // --- Filter action buttons ---
    const actionMap = {
        'select-all-entity-types': selectAllEntityTypes,
        'select-no-entity-types': selectNoEntityTypes,
        'invert-entity-types': invertEntityTypes,
        'select-all-edge-types': selectAllEdgeTypes,
        'select-no-edge-types': selectNoEdgeTypes,
        'invert-edge-types': invertEdgeTypes,
        'select-all-documents': selectAllDocuments,
        'select-no-documents': selectNoDocuments
    };
    Object.entries(actionMap).forEach(([action, fn]) => {
        const btn = document.querySelector(`[data-action="${action}"]`);
        if (btn) btn.addEventListener('click', fn);
    });

    // --- Entity type checkboxes (event delegation) ---
    document.getElementById('body-entityTypes').addEventListener('change', (e) => {
        const input = e.target.closest('[data-action="toggle-entity-type"]');
        if (input) toggleEntityType(input.dataset.type, input.checked);
    });

    // --- Edge type checkboxes (event delegation) ---
    document.getElementById('body-edgeTypes').addEventListener('change', (e) => {
        const input = e.target.closest('[data-action="toggle-edge-type"]');
        if (input) toggleEdgeType(input.dataset.type, input.checked);
    });

    // --- Document checkboxes (event delegation) ---
    document.getElementById('body-documents').addEventListener('change', (e) => {
        const input = e.target.closest('[data-action="toggle-document"]');
        if (input) toggleDocument(input.dataset.docId, input.checked);
    });

    // --- Entity list clicks (event delegation) ---
    document.getElementById('entityList').addEventListener('click', (e) => {
        const item = e.target.closest('[data-action="focus-node"]');
        if (item) focusNode(item.dataset.nodeId);
    });

    // --- Node details close button ---
    document.getElementById('nodeDetailsClose').addEventListener('click', hideNodeDetails);
}

// ═══════════════ CONSTRUCTION COMPLÈTE DES FILTRES ═══════════════

/**
 * Construit tous les panneaux de filtrage à partir des données chargées.
 * Appelé après le chargement d'un graphe.
 */
function buildAllFilters(data) {
    buildEntityTypeFilters(data.nodes);
    buildEdgeTypeFilters(data.edges);
    buildDocumentFilters(data.documents || []);
}
