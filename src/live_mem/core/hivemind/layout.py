# -*- coding: utf-8 -*-
"""
Layout S3 du protocole Hivemind.

Tout l'état durable du protocole pour un space vit sous le préfixe
``{space_id}/_hivemind/``. Ce module centralise la construction des clés S3
pour qu'aucun chemin ne soit codé en dur ailleurs dans le code.

Arborescence cible (cf. DESIGN/live-mem/HIVEMIND_STATE.md) ::

    {space_id}/_hivemind/
    ├── node.json                                # Identité locale (cet hôte)
    ├── node_status.json                         # Santé node-local (HiveNodeStatus)
    ├── members.json                             # Membership view (epoch + peers)
    ├── term.json                                # Term courant (monotone)
    ├── token.json                               # Détenteur courant du token + lease
    ├── bank_version.json                        # Pointeur vers le dernier commit
    ├── queue/
    │   └── {seq:020d}_{event_id}.json           # Demandes en attente (FIFO)
    ├── acks/
    │   └── {event_id}/{node_id}.json            # ACK individuel par peer
    ├── commits/
    │   └── {bank_version:020d}.json             # Journal des commits bank
    ├── staging/
    │   └── {commit_id}/                         # Résultats stagés AVANT publish
    │       ├── MANIFEST.json                    # BankCommit (écrit en dernier)
    │       └── bank/{rel_path}                  # fichier bank proposé (1:1 manifest)
    ├── tombstones/
    │   └── {note_id}.json                       # Tombstones de live-notes
    ├── watermarks/
    │   └── {node_id}.json                       # Watermark par peer
    └── events/
        └── {ts}_{event_id}.json                 # Journal append-only

Toutes les clés sont des chaînes ASCII safe pour S3 — pas d'espaces ni de
caractères réservés. Les ``event_id`` et ``node_id`` sont des UUIDs hex
(sans tirets) pour rester compatibles avec n'importe quel provider S3.
"""

from __future__ import annotations

# La version protocole est volontairement stockée ici plutôt que dans
# ``models`` pour casser les cycles d'import potentiels (layout est importé
# par tout le sous-package).
PROTOCOL_VERSION: int = 1

#: Largeur du zero-padding pour les compteurs monotones rangés dans une
#: clé S3. 20 chiffres décimaux suffisent largement (max int64 = 19 chars)
#: et garantissent l'ordre lexicographique == ordre numérique.
SEQ_PAD: int = 20


def _ensure_space_id(space_id: str) -> str:
    """Garde-fou minimal : un space_id vide casse les clés silencieusement."""
    if not space_id:
        raise ValueError("space_id ne peut pas être vide")
    if "/" in space_id:
        raise ValueError(f"space_id ne doit pas contenir '/': {space_id!r}")
    return space_id


def HIVEMIND_PREFIX(space_id: str) -> str:
    """Préfixe S3 racine du sous-arbre Hivemind d'un space."""
    return f"{_ensure_space_id(space_id)}/_hivemind/"


# ─────────────────────────────────────────────────────────────────────────
# Fichiers singletons (un seul par space)
# ─────────────────────────────────────────────────────────────────────────


def node_key(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}node.json"


def node_status_key(space_id: str) -> str:
    """
    Clé du fichier de santé **node-local** (HiveNodeStatus).

    Séparé de ``members.json`` (partagé) : il porte l'auto-évaluation de
    l'instance courante. Critique mais node-local — il n'est JAMAIS inclus
    dans un snapshot de bootstrap exporté vers un peer.
    """
    return f"{HIVEMIND_PREFIX(space_id)}node_status.json"


def members_key(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}members.json"


def term_key(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}term.json"


def token_key(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}token.json"


def bank_version_key(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}bank_version.json"


# ─────────────────────────────────────────────────────────────────────────
# Queue FIFO de demandes de token
# ─────────────────────────────────────────────────────────────────────────


def queue_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}queue/"


def queue_entry_key(space_id: str, sequence: int, event_id: str) -> str:
    """
    Clé d'une entrée de queue. Le ``sequence`` est zero-padded pour préserver
    l'ordre FIFO via le tri lexicographique LIST natif de S3.
    """
    if sequence < 0:
        raise ValueError(f"sequence doit être >= 0, reçu {sequence}")
    return f"{queue_prefix(space_id)}{sequence:0{SEQ_PAD}d}_{event_id}.json"


# ─────────────────────────────────────────────────────────────────────────
# ACKs (un fichier par {event_id, node_id})
# ─────────────────────────────────────────────────────────────────────────


def ack_prefix(space_id: str, event_id: str | None = None) -> str:
    base = f"{HIVEMIND_PREFIX(space_id)}acks/"
    if event_id:
        return f"{base}{event_id}/"
    return base


def ack_key(space_id: str, event_id: str, node_id: str) -> str:
    return f"{ack_prefix(space_id, event_id)}{node_id}.json"


# ─────────────────────────────────────────────────────────────────────────
# Bank commits (un fichier par bank_version)
# ─────────────────────────────────────────────────────────────────────────


def commit_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}commits/"


def commit_key(space_id: str, bank_version: int) -> str:
    if bank_version < 0:
        raise ValueError(f"bank_version doit être >= 0, reçu {bank_version}")
    return f"{commit_prefix(space_id)}{bank_version:0{SEQ_PAD}d}.json"


# ─────────────────────────────────────────────────────────────────────────
# Staging d'un commit (P5-6 / ADR-0007 — le chemin staged du WriteSink split)
#
# Le holder STAGE ses résultats sous ``staging/{commit_id}/`` AVANT de publier
# le ``BankCommit``. Clé sur ``commit_id`` (PAS ``bank_version``) : un retry
# fencé visant la MÊME version cible ne doit pas écraser les octets stagés d'un
# autre holder ; l'arbre du perdant est orphelin et balayé (best-effort). Le
# ``MANIFEST.json`` est écrit EN DERNIER = marqueur de publication (un crash en
# milieu de stage laisse un arbre sans manifest, jamais appliqué).
#
#   {space}/_hivemind/staging/{commit_id}/
#       ├── MANIFEST.json            # le BankCommit (model_dump), écrit EN DERNIER
#       └── bank/{rel_path}          # texte de fichier bank proposé, 1:1 manifest
# ─────────────────────────────────────────────────────────────────────────


def staging_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}staging/"


def staging_commit_prefix(space_id: str, commit_id: str) -> str:
    if not commit_id or "/" in commit_id:
        raise ValueError(f"commit_id invalide: {commit_id!r}")
    return f"{staging_prefix(space_id)}{commit_id}/"


def staging_manifest_key(space_id: str, commit_id: str) -> str:
    return f"{staging_commit_prefix(space_id, commit_id)}MANIFEST.json"


def staging_bank_prefix(space_id: str, commit_id: str) -> str:
    """Préfixe S3 de l'arbre bank STAGÉ d'un commit (sous lequel chaque
    ``rel_path`` est rangé par ``staging_bank_key``). Sert à LISTER les objets
    réellement stagés pour détecter un fichier hors manifest (``PARTIAL_STAGE``)."""
    return f"{staging_commit_prefix(space_id, commit_id)}bank/"


def staging_bank_key(space_id: str, commit_id: str, rel_path: str) -> str:
    """
    Clé S3 d'un fichier bank stagé. ``rel_path`` est le ``path`` d'une
    ``BankCommitManifestEntry`` (relatif à ``bank/``, peut contenir ``/``).

    Garde-fou aligné sur ``BootstrapManifestEntry._validate_path`` (models.py) :
    un path vide, absolu, ou contenant un segment ``..`` est rejeté pour éviter
    toute évasion hors du sous-arbre de staging.
    """
    if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
        raise ValueError(f"staging rel_path invalide: {rel_path!r}")
    return f"{staging_commit_prefix(space_id, commit_id)}bank/{rel_path}"


# ─────────────────────────────────────────────────────────────────────────
# Tombstones de live-notes
# ─────────────────────────────────────────────────────────────────────────


def tombstone_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}tombstones/"


def tombstone_key(space_id: str, note_id: str) -> str:
    if not note_id or "/" in note_id:
        raise ValueError(f"note_id invalide: {note_id!r}")
    return f"{tombstone_prefix(space_id)}{note_id}.json"


# ─────────────────────────────────────────────────────────────────────────
# Provenance des notes répliquées (sidecar live/_origin/{note_id}.json — P5-7)
#
# La note ``.md`` est écrite VERBATIM sous ``{space}/live/{filename}`` (octets
# byte-identiques à l'origine -> préserve le checksum du snapshot bootstrap).
# La provenance (origin_node_id/agent/timestamp) vit À CÔTÉ, dans un sidecar
# JSON sous ``live/_origin/`` — JAMAIS dans le front-matter (qui muterait les
# octets du ``.md``). Le ``note_id`` est le stem du filename (identité unique,
# ADR-0013 ; ``origin_note_id`` n'en est que l'alias documenté, pas une 2e clé).
# ─────────────────────────────────────────────────────────────────────────


def origin_prefix(space_id: str) -> str:
    return f"{_ensure_space_id(space_id)}/live/_origin/"


def origin_key(space_id: str, note_id: str) -> str:
    """Clé du sidecar de provenance d'une note. Garde-fou aligné sur
    ``tombstone_key`` : un ``note_id`` vide ou contenant ``/`` est rejeté (le
    ``/`` évaderait le préfixe ``_origin/`` et casserait l'identité)."""
    if not note_id or "/" in note_id:
        raise ValueError(f"note_id invalide: {note_id!r}")
    return f"{origin_prefix(space_id)}{note_id}.json"


# ─────────────────────────────────────────────────────────────────────────
# Watermarks (un par peer)
# ─────────────────────────────────────────────────────────────────────────


def watermark_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}watermarks/"


def watermark_key(space_id: str, node_id: str) -> str:
    if not node_id or "/" in node_id:
        raise ValueError(f"node_id invalide: {node_id!r}")
    return f"{watermark_prefix(space_id)}{node_id}.json"


# ─────────────────────────────────────────────────────────────────────────
# Event journal (append-only, source de vérité pour la déduplication)
# ─────────────────────────────────────────────────────────────────────────


def event_prefix(space_id: str) -> str:
    return f"{HIVEMIND_PREFIX(space_id)}events/"


def event_key(space_id: str, created_at: str, event_id: str) -> str:
    """
    Le timestamp ISO est en préfixe pour ordonner les events ; l'``event_id``
    est inclus pour rester unique en cas de collision sur la milliseconde
    (clock skew côté provider, par exemple).

    Les ``:`` ISO sont remplacés par ``-`` pour rester compatibles S3 sur
    les SDK clients qui font de l'URL-encoding agressif (Dell ECS notamment).
    """
    safe_ts = created_at.replace(":", "-")
    return f"{event_prefix(space_id)}{safe_ts}_{event_id}.json"
