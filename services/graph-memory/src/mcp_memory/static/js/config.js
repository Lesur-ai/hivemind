/**
 * MCP Memory - Configuration et constantes
 * Couleurs par type d'entité et de relation, paramètres par défaut,
 * état de filtrage global.
 */

// ═══════════════ COULEURS ═══════════════

// Couleurs des nœuds par type d'entité
const TYPE_COLORS = {
    Organization: '#3498db', Person: '#2ecc71', Party: '#2980b9',
    LegalRepresentative: '#27ae60', Clause: '#1abc9c', Obligation: '#16a085',
    ContractType: '#8e44ad', Annex: '#6c3483', Reference: '#5b2c6f',
    Amount: '#f39c12', Duration: '#e67e22', Date: '#d35400',
    SLA: '#e74c3c', Metric: '#c0392b', Certification: '#9b59b6',
    Regulation: '#8e44ad', Location: '#1abc9c', Jurisdiction: '#17a589',
    Concept: '#3498db', Product: '#2e86c1', Service: '#2874a6',
    Document: '#e74c3c', Other: '#95a5a6', Unknown: '#7f8c8d'
};

// Couleurs des arêtes par type de relation
const EDGE_COLORS = {
    RELATED_TO: '#667eea', DEFINES: '#e74c3c', SIGNED_BY: '#2ecc71',
    REPRESENTS: '#27ae60', MENTIONS: '#555', PARTY_TO: '#3498db',
    INCLUDES_ANNEX: '#8e44ad', HAS_DURATION: '#e67e22', HAS_AMOUNT: '#f39c12',
    HAS_SLA: '#e74c3c', HAS_CERTIFICATION: '#9b59b6', GUARANTEES: '#9b59b6',
    GOVERNED_BY: '#1abc9c', OBLIGATES: '#c0392b', REQUIRES_CERTIFICATION: '#8e44ad',
    AMENDS: '#d35400', SUPERSEDES: '#c0392b', LOCATED_AT: '#1abc9c',
    JURISDICTION: '#17a589', EFFECTIVE_DATE: '#d35400', CERTIFIES: '#9b59b6',
    REFERENCES: '#95a5a6', BELONGS_TO: '#2980b9', CREATED_BY: '#2ecc71',
    CONTAINS: '#34495e', HAS_VALUE: '#f39c12'
};

// ═══════════════ PARAMÈTRES D'AFFICHAGE ═══════════════

const DEFAULT_PARAMS = { springLength: 400, gravity: 15000, nodeSize: 20, fontSize: 11 };
let currentParams = { ...DEFAULT_PARAMS };

// ═══════════════ ÉTAT GLOBAL ═══════════════

const appState = {
    network: null,       // Instance vis-network
    currentData: null,   // Données brutes du graphe chargé (non filtrées)
    currentMemory: null  // ID de la mémoire sélectionnée
};

// ═══════════════ OPTIONS D'AFFICHAGE (toggles UI) ═══════════════

const displayOptions = {
    showEdgeLabels: true,   // Labels texte sur les arêtes
    showShadows: true,      // Ombres sur les nœuds
    showSmooth: true,       // Arêtes courbes (smooth) vs lignes droites
    showMentions: true,     // Liens Document ↔ Entité (MENTIONS) + nœuds Document
    layout: 'barnesHut'     // 'barnesHut' | 'forceAtlas2' | 'hierarchicalUD' | 'hierarchicalLR'
};

// ═══════════════ ÉTAT DE FILTRAGE ═══════════════

const filterState = {
    // Sets de types/IDs visibles (tout visible par défaut après chargement)
    visibleEntityTypes: new Set(),   // ex: {"Organization", "Clause", "Person"}
    visibleEdgeTypes: new Set(),     // ex: {"DEFINES", "OBLIGATES", "MENTIONS"}
    visibleDocuments: new Set(),     // ex: {"doc_id_1", "doc_id_2"}

    // Mode isolation (pour ASK "Isoler le sujet")
    // null = pas d'isolation (graphe complet filtré), Set = seuls ces nœuds sont montrés
    isolatedNodes: null,

    // Flag : les filtres ont-ils été initialisés après un chargement de graphe ?
    initialized: false
};

/**
 * Initialise filterState à partir des données chargées.
 * Met tous les types/documents à "visible".
 */
function initFilterState(data) {
    // Collecter tous les types d'entités
    const entityTypes = new Set(data.nodes.map(n => n.type));
    filterState.visibleEntityTypes = entityTypes;

    // Collecter tous les types de relations
    const edgeTypes = new Set(data.edges.map(e => e.type));
    filterState.visibleEdgeTypes = edgeTypes;

    // Collecter tous les IDs de documents
    const docIds = new Set((data.documents || []).map(d => d.id));
    filterState.visibleDocuments = docIds;

    // Pas d'isolation
    filterState.isolatedNodes = null;

    filterState.initialized = true;
}

/**
 * Applique les filtres et re-rend le graphe.
 * C'est LA fonction centrale de filtrage — appelée à chaque changement de filtre.
 */
function applyFilters() {
    if (!appState.currentData || !filterState.initialized) return;

    const data = appState.currentData;

    // Étape 1 : Filtrer les nœuds par type d'entité visible
    //           En mode isolation, les nœuds dans isolatedNodes sont toujours éligibles
    const isIsolated = filterState.isolatedNodes !== null;

    let filteredNodes = data.nodes.filter(n => {
        // Les documents sont masqués si le toggle Documents est OFF (prioritaire sur tout, y compris l'isolation)
        if (n.node_type === 'document' && !displayOptions.showMentions) return false;

        // En mode isolation, les nœuds dans le set sont toujours visibles
        if (isIsolated && filterState.isolatedNodes.has(n.id)) return true;

        // Les documents sont visibles si leur ID est dans visibleDocuments
        if (n.node_type === 'document') {
            const rawId = n.id.startsWith('doc:') ? n.id.substring(4) : n.id;
            return filterState.visibleDocuments.has(rawId);
        }
        // Les entités sont visibles si leur type est dans visibleEntityTypes
        return filterState.visibleEntityTypes.has(n.type);
    });

    // Étape 2 : Si des documents sont masqués, masquer aussi les entités
    //           qui n'apparaissent QUE dans ces documents masqués
    const hiddenDocIds = new Set(
        (data.documents || [])
            .filter(d => !filterState.visibleDocuments.has(d.id))
            .map(d => d.id)
    );
    if (hiddenDocIds.size > 0 && data.documents && data.documents.length > 0) {
        filteredNodes = filteredNodes.filter(n => {
            if (n.node_type === 'document') return true; // déjà filtré ci-dessus
            // Si l'entité a des source_docs, vérifier qu'au moins un est visible
            if (n.source_docs && n.source_docs.length > 0) {
                return n.source_docs.some(docId => filterState.visibleDocuments.has(docId));
            }
            return true; // pas de source_docs = toujours visible
        });
    }

    // Étape 3 : Mode isolation (ASK "Isoler le sujet")
    if (filterState.isolatedNodes !== null) {
        filteredNodes = filteredNodes.filter(n => filterState.isolatedNodes.has(n.id));
    }

    // Étape 4 : Filtrer les arêtes
    const visibleNodeIds = new Set(filteredNodes.map(n => n.id));
    let filteredEdges = data.edges.filter(e => {
        // L'arête doit connecter deux nœuds visibles
        if (!visibleNodeIds.has(e.from) || !visibleNodeIds.has(e.to)) return false;
        // Si le toggle MENTIONS est OFF, masquer les arêtes MENTIONS
        if (!displayOptions.showMentions && e.type === 'MENTIONS') return false;
        // Le type de relation doit être visible
        return filterState.visibleEdgeTypes.has(e.type);
    });

    // Rendre le graphe filtré
    renderGraph(filteredNodes, filteredEdges);

    // Mettre à jour la liste d'entités (exclure les documents)
    updateEntityList(filteredNodes.filter(n => n.node_type !== 'document'));

    // Mettre à jour les stats
    updateStats(
        filteredNodes.filter(n => n.node_type !== 'document').length,
        filteredEdges.filter(e => e.type !== 'MENTIONS').length
    );
}
