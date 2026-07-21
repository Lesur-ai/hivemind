# -*- coding: utf-8 -*-
"""
Service Live — Gestion des notes en temps réel.

Ce service encapsule les opérations sur les notes live :
    - write_note  : écrire une note (append-only, zéro conflit)
    - read_notes  : lire les notes avec filtres
    - search_notes : rechercher du texte dans les notes

Les notes live sont le cœur de la collaboration multi-agents.
Chaque note = 1 fichier S3 unique → aucun conflit possible entre agents.

Architecture :
    tools/live.py → LiveService (ce fichier) → StorageService (S3)

Voir S3_DATA_MODEL.md pour le format des notes (front-matter YAML + contenu).
Voir MCP_TOOLS_SPEC.md pour les catégories et le format de retour.
"""

import re
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from .storage import get_storage
from .reservation_guard import assert_space_not_reserved
from .live_note_format import (
    decode_live_note_string,
    split_live_note_front_matter,
)


# ─────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────

# VULN-07 fix : limites de taille pour les contenus
MAX_NOTE_CONTENT_SIZE = 100_000  # 100K caractères max par note
MAX_LIVE_READ_LIMIT = 500  # VULN-10 fix : limite max pour live_read

# Catégories de notes autorisées (cf. MCP_TOOLS_SPEC.md)
VALID_CATEGORIES = [
    "observation",  # Constat factuel ("Le build passe")
    "decision",  # Choix technique ("On part sur S3")
    "todo",  # Tâche à faire ("Implémenter le backup")
    "insight",  # Pattern découvert ("Le pattern X marche")
    "question",  # Question ouverte ("Supporter le CSV ?")
    "progress",  # Avancement ("Module auth : 80%")
    "issue",  # Problème, bug ("Timeout LLM > 60s")
]


class LiveService:
    """
    Service de gestion des notes live.

    Toutes les méthodes sont async et retournent un dict
    avec un champ "status" conforme à la convention MCP.
    """

    async def write_note(
        self,
        space_id: str,
        category: str,
        content: str,
        tags: str = "",
    ) -> dict:
        """
        Écrit une note live (append-only, aucun conflit possible).

        Crée un fichier Markdown avec front-matter YAML + contenu.
        Le nom de fichier est unique : {timestamp}_{agent}_{category}_{uuid8}.md

        L'identité de l'agent est TOUJOURS le client_name du token
        d'authentification (v0.8.1 — Token = Agent). Pas de paramètre
        agent pour garantir la cohérence avec le consolidateur.

        Args:
            space_id: Espace cible
            category: Type de note (observation, decision, todo, etc.)
            content: Corps de la note (texte libre)
            tags: Tags séparés par des virgules (optionnel)

        Returns:
            {"status": "created", "filename": "...", ...}
        """
        await assert_space_not_reserved(space_id)
        # VULN-07 fix : valider la taille du contenu
        if len(content) > MAX_NOTE_CONTENT_SIZE:
            return {
                "status": "error",
                "message": (
                    f"Contenu trop long ({len(content)} chars, "
                    f"max {MAX_NOTE_CONTENT_SIZE})"
                ),
            }

        # Valider la catégorie
        if category not in VALID_CATEGORIES:
            return {
                "status": "error",
                "message": (
                    f"Catégorie invalide : '{category}'. "
                    f"Valides : {', '.join(VALID_CATEGORIES)}"
                ),
            }

        storage = get_storage()

        # Vérifier que l'espace existe
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Agent = client_name du token (toujours, jamais de paramètre libre)
        from ..auth.context import get_current_agent_name

        agent = get_current_agent_name()
        if not isinstance(agent, str) or agent == "":
            return {
                "status": "error",
                "message": "Identité client_name non vide requise pour écrire une note",
            }

        # Construire le nom de fichier unique
        # Format : {YYYYMMDD}T{HHMMSS}_{agent}_{category}_{uuid8}.md
        now = datetime.now(timezone.utc)
        timestamp_str = now.strftime("%Y%m%dT%H%M%S")
        uuid8 = uuid.uuid4().hex[:8]
        # Nettoyer le nom d'agent (garder uniquement alphanum + tirets)
        safe_agent = re.sub(r"[^a-zA-Z0-9_-]", "", agent) or "agent"
        filename = f"{timestamp_str}_{safe_agent}_{category}_{uuid8}.md"

        # Parser les tags depuis la chaîne CSV
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        # Construire le front-matter YAML + contenu Markdown
        front_matter = (
            f"---\n"
            f'timestamp: "{now.isoformat()}"\n'
            f"agent: {json.dumps(agent, ensure_ascii=False)}\n"
            f'category: "{category}"\n'
            f"tags: {json.dumps(tag_list)}\n"
            f'space_id: "{space_id}"\n'
            f"---\n\n"
        )
        full_content = front_matter + content

        # Écrire sur S3 — 1 seul PUT, aucun lock nécessaire
        key = f"{space_id}/live/{filename}"
        await storage.put(key, full_content)

        return {
            "status": "created",
            "space_id": space_id,
            "filename": filename,
            "category": category,
            "agent": agent,
            "size": len(full_content.encode("utf-8")),
            "timestamp": now.isoformat(),
        }

    async def read_notes(
        self,
        space_id: str,
        limit: int = 50,
        category: str = "",
        agent: str = "",
        since: str = "",
    ) -> dict:
        """
        Lit les notes live récentes avec filtres optionnels.

        Les notes sont retournées du plus récent au plus ancien.

        Args:
            space_id: Espace cible
            limit: Nombre max de notes (défaut 50)
            category: Filtrer par catégorie (optionnel)
            agent: Filtrer par agent (optionnel)
            since: ISO datetime — notes après cette date (optionnel)

        Returns:
            {"status": "ok", "notes": [...], "total": N, "has_more": bool}
        """
        # VULN-10 fix : borner le limit
        limit = min(limit, MAX_LIVE_READ_LIMIT)

        storage = get_storage()
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Gate Hivemind unique (fail-closed : corruption propage). Le skip du
        # sidecar live/_origin/ n'est légitime QUE sur un space Hivemind confirmé
        # (P5-7) — sinon un objet legacy sous ce préfixe serait perdu sur un space
        # non-Hivemind (régression byte-for-byte).
        is_hive = await _is_confirmed_hive(storage, space_id)

        # Lire toutes les notes live depuis S3
        all_notes = await storage.list_and_get(f"{space_id}/live/")

        # Parser et filtrer les notes
        parsed = []
        for item in all_notes:
            if is_hive and _is_origin_sidecar(item["key"], space_id):
                continue  # Sidecar de provenance (live/_origin/) → pas une note
            note = _parse_note(item["key"], item["content"])
            if note is None:
                continue  # Note mal formée → skip silencieux

            # Appliquer les filtres
            if category and note["category"] != category:
                continue
            if agent and note["agent"] != agent:
                continue
            if since and note["timestamp"] < since:
                continue

            parsed.append(note)

        # Trier par timestamp décroissant (plus récent d'abord)
        parsed.sort(key=lambda n: n["timestamp"], reverse=True)

        # Appliquer la limite
        total = len(parsed)
        notes = parsed[:limit]

        # Enrichissement provenance (Hivemind-only, byte-préservant — P5-7).
        await _enrich_provenance(storage, space_id, notes, is_hive=is_hive)

        return {
            "status": "ok",
            "space_id": space_id,
            "notes": notes,
            "total": len(notes),
            "has_more": total > limit,
        }

    async def search_notes(
        self,
        space_id: str,
        query: str,
        limit: int = 20,
    ) -> dict:
        """
        Recherche texte (case-insensitive) dans les notes live.

        Args:
            space_id: Espace cible
            query: Texte à chercher
            limit: Nombre max de résultats (défaut 20)

        Returns:
            {"status": "ok", "notes": [...], "total": N, "has_more": bool}
        """
        storage = get_storage()
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Gate Hivemind unique (fail-closed) : skip du sidecar uniquement sur un
        # space Hivemind confirmé (cf. read_notes). Non-Hivemind = legacy
        # byte-for-byte, tous les objets live/* passent à _parse_note.
        is_hive = await _is_confirmed_hive(storage, space_id)

        all_notes = await storage.list_and_get(f"{space_id}/live/")
        query_lower = query.lower()

        matched = []
        for item in all_notes:
            if is_hive and _is_origin_sidecar(item["key"], space_id):
                continue  # Sidecar de provenance (live/_origin/) → pas une note
            note = _parse_note(item["key"], item["content"])
            if note is None:
                continue

            # Recherche case-insensitive dans le contenu
            if query_lower in note["content"].lower():
                matched.append(note)

        # Trier par pertinence (plus récent d'abord)
        matched.sort(key=lambda n: n["timestamp"], reverse=True)

        total = len(matched)
        notes = matched[:limit]

        # Enrichissement provenance (Hivemind-only, byte-préservant — P5-7).
        await _enrich_provenance(storage, space_id, notes, is_hive=is_hive)

        return {
            "status": "ok",
            "space_id": space_id,
            "query": query,
            "notes": notes,
            "total": len(notes),
            "has_more": total > limit,
        }


# ─────────────────────────────────────────────────────────────
# Helpers internes
# ─────────────────────────────────────────────────────────────


def _is_origin_sidecar(key: str, space_id: str) -> bool:
    """True si ``key`` est un sidecar de provenance ``live/_origin/...`` (P5-7) —
    PAS une note. ``list_and_get(.../live/)`` ramène tout le sous-arbre, sidecars
    inclus ; on les exclut avant le parsing de note.

    ATTENTION : ce skip n'est légitime QUE sur un space Hivemind confirmé. Sur un
    space NON-Hivemind, ``live/_origin/`` n'est pas un sidecar P5-7 mais un objet
    legacy ordinaire ; le sauter ferait perdre des notes (régression
    byte-for-byte). L'appelant DOIT donc gater cet appel derrière
    ``is_hivemind_space`` confirmé (cf. ``read_notes`` / ``search_notes``)."""
    return key.startswith(f"{space_id}/live/_origin/")


async def _is_confirmed_hive(storage, space_id: str) -> bool:
    """Gate Hivemind unique pour les chemins de lecture (P5-7). Réutilise le
    résolveur fail-closed du lifecycle : sur corruption critique, la
    ``CorruptedStateError`` PROPAGE (jamais lue comme « non-Hivemind »). C'est la
    MÊME autorité que ``_enrich_provenance`` consomme — on ne décide « est-ce un
    space Hivemind ? » qu'à un seul endroit logique.

    Le skip du sidecar ``live/_origin/`` (cf. ``_is_origin_sidecar``) est
    conditionné à ce verdict : un space NON-Hivemind voit TOUS ses objets
    ``live/*`` passer à ``_parse_note`` comme avant P5-7 (byte-for-byte)."""
    # Import paresseux : éviter un cycle live (core/) <-> hivemind (core/hivemind/).
    from .hivemind.lifecycle import is_hivemind_space

    return await is_hivemind_space(storage, space_id)


def _parse_note(key: str, raw_content: str) -> Optional[dict]:
    """
    Parse une note live depuis son contenu brut (front-matter YAML + body).

    Le front-matter est parsé sans librairie YAML externe (format simple).

    Args:
        key: Clé S3 complète (ex: "my-space/live/20260220T180000_cline_obs_a1b2.md")
        raw_content: Contenu brut du fichier (front-matter + body)

    Returns:
        Dict {filename, timestamp, agent, category, tags, content}
        ou None si le format est invalide
    """
    filename = key.split("/")[-1]

    # Séparer front-matter YAML et corps Markdown
    if raw_content.startswith("---"):
        parsed = split_live_note_front_matter(raw_content)
        if parsed is None:
            return None  # Front-matter mal formé
        front_matter_str, body = parsed
    else:
        # Pas de front-matter → corps brut
        body = raw_content.strip()
        front_matter_str = ""

    # Parser le front-matter (YAML simple, ligne par ligne)
    fm = {}
    for line in front_matter_str.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = decode_live_note_string(v)

    # Parser les tags (format JSON dans le YAML : tags: ["tag1", "tag2"])
    tags = []
    tags_raw = fm.get("tags", "")
    if tags_raw.startswith("["):
        try:
            tags = json.loads(tags_raw)
        except json.JSONDecodeError:
            tags = []

    return {
        "filename": filename,
        "timestamp": fm.get("timestamp", ""),
        "agent": fm.get("agent", ""),
        "category": fm.get("category", ""),
        "tags": tags,
        "content": body,
    }


# ─────────────────────────────────────────────────────────────
# Provenance Hivemind (P5-7) — enrichissement additif, byte-préservant
# ─────────────────────────────────────────────────────────────


async def _enrich_provenance(
    storage, space_id: str, notes: list[dict], *, is_hive: bool | None = None
) -> None:
    """
    Enrichit IN-PLACE chaque note avec ``note_id`` + ``provenance`` — UNIQUEMENT
    sur un space Hivemind (HIVEMIND.md §5.2). Additif et byte-préservant : ne
    touche JAMAIS le ``.md`` ni le front-matter (la provenance vient du sidecar
    ``live/_origin/{note_id}.json``).

    Gate fail-closed : sur un space NON-Hivemind, court-circuit total — aucun
    ``note_id``, aucune clé ``provenance`` -> sortie byte-identique au legacy. La
    corruption (``CorruptedStateError`` du contexte hive ou d'un sidecar) PROPAGE
    (jamais lue comme « non partagé »).

    ``is_hive`` : verdict Hivemind DÉJÀ résolu par l'appelant (read_notes /
    search_notes) via le MÊME résolveur fail-closed — passé pour ne décider du
    statut Hivemind qu'une seule fois par appel. ``None`` -> on (re)résout ici
    (compat appel direct), avec la même sémantique fail-closed.
    """
    # Imports paresseux : éviter un cycle live (core/) <-> hivemind (core/hivemind/).
    from .hivemind.lifecycle import is_hivemind_space, resolve_hive_context
    from .hivemind.note_replication import (
        NoteReplicationRuntime,
        note_id_from_filename,
        provenance_label,
    )
    from .hivemind.state import HivemindStateStore

    if is_hive is None:
        is_hive = await is_hivemind_space(storage, space_id)
    if not is_hive:
        return  # Non-Hivemind : sortie legacy byte-identique (aucune mutation).

    ctx = await resolve_hive_context(storage, space_id)
    local_node_id = ctx.node.node_id if ctx.node is not None else ""

    store = HivemindStateStore(storage=storage, space_id=space_id)
    runtime = NoteReplicationRuntime(store, storage, space_id)

    for note in notes:
        note_id = note_id_from_filename(note["filename"])
        note["note_id"] = note_id
        origin = await runtime.read_origin(note_id)  # CorruptedStateError propage
        if origin is not None:
            is_local = origin.origin_node_id == local_node_id
            note["provenance"] = {
                "origin_agent": origin.origin_agent,
                "origin_node_id": origin.origin_node_id,
                "is_local": is_local,
                "label": provenance_label(
                    origin_agent=origin.origin_agent,
                    is_local=is_local,
                    peer_alias=origin.origin_node_id,
                ),
            }
        else:
            # Pas de sidecar = note d'origine LOCALE (jamais répliquée).
            note["provenance"] = {
                "origin_agent": note["agent"],
                "origin_node_id": local_node_id,
                "is_local": True,
                "label": provenance_label(
                    origin_agent=note["agent"],
                    is_local=True,
                    peer_alias=local_node_id,
                ),
            }


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_live_service: LiveService | None = None


def get_live_service() -> LiveService:
    """Retourne le singleton LiveService."""
    global _live_service
    if _live_service is None:
        _live_service = LiveService()
    return _live_service
