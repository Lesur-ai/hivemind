/**
 * MCP Memory - Orchestration et initialisation
 *
 * Point d'entrée : authentification, chargement des mémoires,
 * connexion des événements, gestion du graphe, modale paramètres.
 */

// ═══════════════ AUTHENTIFICATION ═══════════════

/** Affiche l'écran de login avec un message d'erreur optionnel */
function showLoginScreen(errorMsg = '') {
    const overlay = document.getElementById('loginOverlay');
    overlay.classList.remove('hidden');
    const errorEl = document.getElementById('loginError');
    errorEl.textContent = errorMsg ? `❌ ${errorMsg}` : '';
    document.getElementById('loginToken').focus();
}

/** Masque l'écran de login */
function hideLoginScreen() {
    document.getElementById('loginOverlay').classList.add('hidden');
}

/** Tente de se connecter avec le token saisi */
async function attemptLogin() {
    const input = document.getElementById('loginToken');
    const btn = document.getElementById('loginBtn');
    const errorEl = document.getElementById('loginError');
    const token = input.value.trim();

    if (!token) {
        errorEl.textContent = '❌ Veuillez saisir un token.';
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Connexion…';
    errorEl.textContent = '';

    try {
        // Tester le token en appelant /api/memories
        const response = await fetch('/api/memories', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.status === 401) {
            errorEl.textContent = '❌ Token invalide ou expiré.';
            return;
        }

        if (!response.ok) {
            errorEl.textContent = `❌ Erreur serveur (${response.status}).`;
            return;
        }

        const result = await response.json();
        if (result.status !== 'ok') {
            errorEl.textContent = `❌ ${result.message || 'Erreur inconnue.'}`;
            return;
        }

        // Token valide ! Stocker et continuer
        setAuthToken(token);
        hideLoginScreen();
        input.value = '';

        // Charger les mémoires dans le select
        populateMemories(result);

    } catch (e) {
        errorEl.textContent = `❌ Impossible de contacter le serveur.`;
        console.error('Login error:', e);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Se connecter';
    }
}

/** Déconnexion : efface le token et affiche le login */
function logout() {
    clearAuthToken();
    // Réinitialiser l'état
    appState.currentData = null;
    appState.currentMemory = null;
    appState.network = null;
    document.getElementById('memorySelect').innerHTML = '<option value="">-- Mémoire --</option>';
    document.getElementById('askBtn').disabled = true;
    document.getElementById('loadBtn').disabled = true;
    showLoginScreen();
}

/** Vérifie si un token existe et est valide au chargement */
async function checkExistingToken() {
    const token = getAuthToken();

    if (!token) {
        showLoginScreen();
        return;
    }

    try {
        const result = await apiLoadMemories();
        if (result.status === 'ok') {
            hideLoginScreen();
            populateMemories(result);
        } else {
            showLoginScreen('Token invalide.');
        }
    } catch (e) {
        // Si c'est un 401, showLoginScreen est déjà appelé par authFetch
        if (e.message !== 'Unauthorized') {
            showLoginScreen('Impossible de contacter le serveur.');
        }
    }
}

/** Remplit le select des mémoires à partir d'un résultat API */
function populateMemories(result) {
    const select = document.getElementById('memorySelect');
    // Vider (garder l'option par défaut)
    select.innerHTML = '<option value="">-- Mémoire --</option>';
    if (result.memories) {
        result.memories.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = `${m.id} — ${m.name}`;
            select.appendChild(opt);
        });
    }
}

// ═══════════════ SETUP LOGIN ═══════════════

function setupLogin() {
    // Bouton Se connecter
    document.getElementById('loginBtn').addEventListener('click', attemptLogin);

    // Entrée dans le champ token → submit
    document.getElementById('loginToken').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') attemptLogin();
    });

    // Bouton logout
    document.getElementById('logoutBtn').addEventListener('click', logout);
}

// ═══════════════ CHARGEMENT DU GRAPHE ═══════════════

/** Charge le graphe de la mémoire sélectionnée */
async function loadSelectedGraph() {
    const memoryId = document.getElementById('memorySelect').value;
    if (!memoryId) return;

    const loading = document.getElementById('loading');
    loading.classList.remove('hidden');

    try {
        const result = await apiLoadGraph(memoryId);
        if (result.status !== 'ok') throw new Error(result.message);

        // Stocker les données brutes
        appState.currentData = result;
        appState.currentMemory = memoryId;

        // Initialiser l'état de filtrage (tout visible)
        initFilterState(result);

        // Construire les panneaux de filtrage dans la sidebar
        buildAllFilters(result);

        // Appliquer les filtres (= rendu initial complet)
        applyFilters();

        // Activer le bouton ASK
        document.getElementById('askBtn').disabled = false;

        // Quitter le mode isolation s'il était actif
        exitIsolation();

    } catch (e) {
        if (e.message !== 'Unauthorized') {
            console.error('Erreur:', e);
            alert('Erreur: ' + e.message);
        }
    } finally {
        loading.classList.add('hidden');
    }
}

// ═══════════════ SETUP CONTRÔLES ═══════════════

/** Setup des contrôles header */
function setupHeaderControls() {
    document.getElementById('memorySelect').addEventListener('change', function () {
        document.getElementById('loadBtn').disabled = !this.value;
    });
    document.getElementById('loadBtn').addEventListener('click', loadSelectedGraph);

    // Zoom to fit (sans animation pour les graphes denses — beaucoup plus stable)
    document.getElementById('fitBtn').addEventListener('click', () => {
        if (appState.network) appState.network.fit({ animation: false });
    });

    // Bouton "Graphe complet" (sortir du mode isolation)
    document.getElementById('exitIsolationBtn').addEventListener('click', exitIsolation);

    // Sélecteur de layout
    document.getElementById('layoutSelect').addEventListener('change', function () {
        displayOptions.layout = this.value;
        if (appState.currentData && filterState.initialized) {
            applyFilters();
        }
    });

    // Toggles d'affichage (labels arêtes, ombres, courbes, mentions)
    setupDisplayToggle('toggleEdgeLabels', 'showEdgeLabels');
    setupDisplayToggle('toggleShadows', 'showShadows');
    setupDisplayToggle('toggleSmooth', 'showSmooth');
    setupDisplayToggle('toggleMentions', 'showMentions');
}

/** Connecte un bouton toggle à une option d'affichage et re-rend le graphe */
function setupDisplayToggle(btnId, optionKey) {
    document.getElementById(btnId).addEventListener('click', function () {
        // Inverser l'option
        displayOptions[optionKey] = !displayOptions[optionKey];
        // Mettre à jour le style du bouton
        this.classList.toggle('active', displayOptions[optionKey]);
        // Re-rendre le graphe avec les nouvelles options (si un graphe est chargé)
        if (appState.currentData && filterState.initialized) {
            applyFilters();
        }
    });
}

/** Setup modale paramètres */
function setupSettingsModal() {
    const modal = document.getElementById('settingsModal');
    document.getElementById('settingsBtn').addEventListener('click', () => modal.classList.add('visible'));
    modal.addEventListener('click', e => { if (e.target === modal) modal.classList.remove('visible'); });

    const sliders = [
        { id: 'paramSpringLength', valId: 'valSpringLength', prefix: '' },
        { id: 'paramGravity', valId: 'valGravity', prefix: '-' },
        { id: 'paramNodeSize', valId: 'valNodeSize', prefix: '' },
        { id: 'paramFontSize', valId: 'valFontSize', prefix: '' }
    ];
    sliders.forEach(s => {
        document.getElementById(s.id).addEventListener('input', function () {
            document.getElementById(s.valId).textContent = s.prefix + this.value;
        });
    });

    document.getElementById('resetParams').addEventListener('click', () => {
        document.getElementById('paramSpringLength').value = DEFAULT_PARAMS.springLength;
        document.getElementById('paramGravity').value = DEFAULT_PARAMS.gravity;
        document.getElementById('paramNodeSize').value = DEFAULT_PARAMS.nodeSize;
        document.getElementById('paramFontSize').value = DEFAULT_PARAMS.fontSize;
        document.getElementById('valSpringLength').textContent = DEFAULT_PARAMS.springLength;
        document.getElementById('valGravity').textContent = '-' + DEFAULT_PARAMS.gravity;
        document.getElementById('valNodeSize').textContent = DEFAULT_PARAMS.nodeSize;
        document.getElementById('valFontSize').textContent = DEFAULT_PARAMS.fontSize;
    });

    document.getElementById('applyParams').addEventListener('click', () => {
        currentParams.springLength = parseInt(document.getElementById('paramSpringLength').value);
        currentParams.gravity = parseInt(document.getElementById('paramGravity').value);
        currentParams.nodeSize = parseInt(document.getElementById('paramNodeSize').value);
        currentParams.fontSize = parseInt(document.getElementById('paramFontSize').value);
        modal.classList.remove('visible');
        applyFilters();
    });
}

// ═══════════════ INITIALISATION ═══════════════

document.addEventListener('DOMContentLoaded', () => {
    setupLogin();
    setupHeaderControls();
    setupSettingsModal();
    setupSearchFilter();
    setupSidebarEvents();  // Event delegation CSP-safe (sidebar + filtres)
    setupAsk();

    // Vérifier le token existant (ou afficher le login)
    checkExistingToken();
});
