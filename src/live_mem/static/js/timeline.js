/**
 * Live Memory - Timeline des notes live (panneau haut-droit)
 */

function renderLive() {
    const el = document.getElementById('liveContent');
    const countEl = document.getElementById('liveCount');
    const notes = app.notes;

    countEl.textContent = notes.length > 0 ? `(${notes.length})` : '';

    if (notes.length === 0) {
        el.innerHTML = '<div class="empty-state">🔴 No live notes</div>';
        return;
    }

    // Trier décroissant
    const sorted = [...notes].sort((a,b) => (b.timestamp||'').localeCompare(a.timestamp||''));

    // Grouper par date
    const groups = {};
    sorted.forEach(n => {
        const k = (n.timestamp||'').substring(0,10);
        if (!groups[k]) groups[k] = [];
        groups[k].push(n);
    });

    let h = '';
    Object.entries(groups).forEach(([dateKey, dayNotes]) => {
        let label = dateKey;
        try {
            const d = new Date(dateKey + 'T00:00:00');
            const today = new Date().toISOString().substring(0,10);
            const yest = new Date(Date.now()-86400000).toISOString().substring(0,10);
            if (dateKey === today) label = 'Today';
            else if (dateKey === yest) label = 'Yesterday';
            else label = d.toLocaleDateString('en-US',{weekday:'short',day:'numeric',month:'long'});
        } catch {}

        h += `<div class="date-sep">
            <span class="date-sep-line"></span>
            <span class="date-sep-label">📅 ${label} · ${dayNotes.length}</span>
            <span class="date-sep-line"></span>
        </div>`;

        dayNotes.forEach(n => { h += noteCard(n); });
    });

    el.innerHTML = h;
}

function noteCard(n) {
    const col = n.agent ? getAgentColor(n.agent) : '#666';
    const cs = getCatStyle(n.category);
    const ci = getCatIcon(n.category);
    const t = fmtTime(n.timestamp);
    const body = md(n.content);

    return `<div class="note-card" style="border-left-color:${col}">
        <div class="note-header">
            <span class="note-time">${t}</span>
            ${n.agent ? `<span class="note-agent" style="background:${col}">${esc(n.agent)}</span>` : ''}
            ${n.category ? `<span class="note-cat" style="background:${cs.bg};color:${cs.text};border:1px solid ${cs.border}">${ci} ${n.category}</span>` : ''}
        </div>
        <div class="note-body">${body}</div>
    </div>`;
}
