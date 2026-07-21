/**
 * Live Memory - Bank (panneau bas-droit, onglets de fichiers)
 */

function renderBankTabs() {
    const tabsEl = document.getElementById('bankTabs');
    const countEl = document.getElementById('bankCount');
    const files = app.bankFiles;

    countEl.textContent = files.length > 0 ? `(${files.length})` : '';

    if (files.length === 0) {
        tabsEl.innerHTML = '';
        document.getElementById('bankContent').innerHTML = '<div class="empty-state">📘 No consolidated bank files</div>';
        return;
    }

    // LM2-01 fix : le `${name}` final dans innerHTML était NON ÉCHAPPÉ —
    // un nom de fichier malicieux (`<img src=x onerror=...>`) injecté par
    // un opérateur compromis (ou un LLM dérivant) exécutait du JS arbitraire
    // dans le navigateur de chaque admin ouvrant /live. Échappement systématique
    // + le serveur refuse maintenant les caractères dangereux (LM2-12 fix).
    // CSP fix : inline onclick="..." interdit par script-src 'self'.
    // On utilise addEventListener via data-filename + délégation.
    tabsEl.innerHTML = files.map(f => {
        const name = f.filename || f;
        const safeName = esc(name);
        const active = app.currentBankFile === name ? 'active' : '';
        return `<div class="bank-tab ${active}" data-filename="${safeName}">${safeName}</div>`;
    }).join('');

    // Attach click handlers (CSP-safe, no inline scripts)
    tabsEl.querySelectorAll('.bank-tab').forEach(tab => {
        tab.addEventListener('click', () => selectBank(tab.dataset.filename));
    });

    // Si aucun fichier sélectionné, sélectionner le premier
    if (!app.currentBankFile && files.length > 0) {
        selectBank(files[0].filename || files[0]);
    }
}

async function selectBank(filename) {
    app.currentBankFile = filename;

    // Mettre à jour les onglets actifs
    document.querySelectorAll('.bank-tab').forEach(t => {
        t.classList.toggle('active', t.textContent === filename);
    });

    const el = document.getElementById('bankContent');
    el.innerHTML = '<div class="empty-state">Loading…</div>';

    try {
        const r = await apiLoadBankFile(app.spaceId, filename);
        if (r.status === 'ok' && r.content) {
            el.innerHTML = `<div class="md-content">${md(r.content)}</div>`;
        } else {
            el.innerHTML = `<div class="empty-state">❌ ${esc(r.message||'Error')}</div>`;
        }
    } catch (e) {
        if (e.message !== 'Unauthorized') {
            el.innerHTML = `<div class="empty-state">❌ ${esc(e.message)}</div>`;
        }
    }
}
