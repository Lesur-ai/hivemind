# -*- coding: utf-8 -*-
"""
Modèles Pydantic — Structures de données de Live Memory.

Ces modèles définissent les objets échangés entre les services (storage,
space, consolidator, tokens, backup) et sérialisés en JSON/Markdown sur S3.

Voir S3_DATA_MODEL.md pour l'arborescence S3 complète.
"""

from typing import Optional
from pydantic import BaseModel, Field


# =============================================================================
# P7-3 — Constantes du binding long embarqué (ADR-0019)
# =============================================================================

#: Valeur écrite À LA PLACE du token embarqué réel dans ``_meta.json``
#: (bloc ``graph_memory.token``). Le token vivant n'est JAMAIS persisté :
#: il est résolu en mémoire (env/volume) au moment de l'usage. Ce sentinel
#: n'est PAS un bearer GM valide → une fuite échoue fermé. Rejeté comme
#: token opérateur explicite ET comme secret embarqué (fail-closed).
EMBEDDED_TOKEN_SENTINEL = "__embedded__"

#: Nom RÉSERVÉ du token interne long dans ``_system/tokens.json``. La rotation
#: (register_internal_long_token) ne révoque QUE les entrées portant ce nom —
#: jamais un token opérateur (never-orphan). Ne pas nommer un token opérateur
#: ainsi.
INTERNAL_LONG_TOKEN_NAME = "internal-long"


# =============================================================================
# Space — Métadonnées d'un espace mémoire
# =============================================================================


class GraphMemoryConfig(BaseModel):
    """
    Configuration de connexion vers une instance Graph Memory.

    Permet à un space de pousser ses fichiers bank dans un graphe
    de connaissances pour la mémoire long terme.

    Stocké dans _meta.json, champ "graph_memory".

    P4-3 / ADR-0012 — LOCALITÉ : le bloc ``graph_memory`` entier (endpoints,
    token, memory_id, métriques de push) est **local à l'instance** et exclu de
    la projection partagée (``SHARED_META_FIELDS`` ne le whitelise pas ;
    politique « default-exclude »). Le **watermark dérivé** de P4-5
    (``bank_version`` / ``commit_id`` / ``term`` / ``provenance``, ADR-0017) sera
    enregistré À L'INTÉRIEUR de ce bloc — il hérite donc automatiquement de la
    localité, sans toucher ``SHARED_META_FIELDS`` : jamais répliqué ni lu par le
    chemin de commit (downstream-derived only, ADR-0010).
    """

    url: str = ""  # URL de graph-memory (ex: "http://localhost:8080/mcp")
    token: str = ""  # Bearer token pour graph-memory
    memory_id: str = ""  # Memory cible dans graph-memory
    ontology: str = "general"  # Ontologie graph-memory à utiliser
    last_push: Optional[str] = None  # ISO 8601 du dernier push
    push_count: int = 0  # Nombre total de pushs effectués
    files_pushed: int = 0  # Nombre de fichiers poussés au dernier push


def mask_meta_secrets(meta: Optional[dict]) -> Optional[dict]:
    """
    LM2-03 fix : masque les secrets dans une copie d'un _meta.json.

    Doit être appliqué AVANT toute exposition vers l'extérieur :

    - réponses API REST (``/api/space/{id}`` — déjà fixé en VULN-12)
    - retour de outils MCP exposant le meta (``space_summary``, ``space_export``)
    - dump de backups (``backup_download``)

    Le token Graph Memory était auparavant masqué uniquement dans
    ``/api/space/{id}`` (VULN-12 partiel). Cette fonction généralise
    le masquage à tous les chemins, transformant le token en
    ``"<prefix>..."`` (8 premiers chars du token + ellipse).

    Args:
        meta: dict _meta.json brut (ou None si l'espace n'existe pas)

    Returns:
        Une COPIE du dict avec ``graph_memory.token`` masqué, ou ``None``
        si l'entrée était None. Ne modifie pas l'entrée.
    """
    if not meta:
        return meta

    gm = meta.get("graph_memory")
    if not gm:
        return meta

    token = gm.get("token") if isinstance(gm, dict) else None
    if not token:
        return meta

    # Copie défensive (jamais muter le dict en place — pourrait corrompre
    # un singleton de cache ou une réponse parallèle).
    masked_token = token[:8] + "..." if len(token) > 8 else "***"
    return {
        **meta,
        "graph_memory": {**gm, "token": masked_token},
    }


#: Allowlist des champs ``_meta.json`` **partageables** dans un cluster
#: Hivemind. C'est la frontière locale/partagé de HIVEMIND.md §3.4 : seuls
#: ces champs voyagent dans un snapshot de bootstrap. Tout champ absent de
#: cette liste (notamment le bloc ``graph_memory`` entier — endpoints, token,
#: memory_id, métriques de push) est local à l'instance et EXCLU, pas masqué.
#:
#: Politique « default-exclude » : un futur champ ajouté à ``SpaceMeta`` reste
#: hors du snapshot tant qu'il n'est pas explicitement ajouté ici. C'est
#: volontaire — on ne réplique jamais aveuglément un champ inconnu.
SHARED_META_FIELDS: tuple[str, ...] = (
    "space_id",
    "description",
    "owner",
    "created_at",
    "last_consolidation",
    "consolidation_count",
    "total_notes_processed",
    "version",
)


def meta_shared_projection(meta: Optional[dict]) -> Optional[dict]:
    """
    Projette un ``_meta.json`` brut sur les seuls champs partageables.

    Contrairement à ``mask_meta_secrets`` (qui masque le token Graph Memory
    pour les chemins de LECTURE), cette projection EXCLUT entièrement tout
    champ non whitelisté — en particulier le bloc ``graph_memory`` complet —
    car la réplication vers une instance souveraine ne doit hériter ni des
    endpoints, ni du memory_id, ni des métriques locales.

    Args:
        meta: dict ``_meta.json`` brut (ou None si l'espace n'existe pas).

    Returns:
        Une COPIE ne contenant que les clés de ``SHARED_META_FIELDS``
        présentes dans l'entrée, ou ``None`` si l'entrée était None. Ne mute
        jamais l'entrée.
    """
    if meta is None:
        return None
    return {k: meta[k] for k in SHARED_META_FIELDS if k in meta}


def meta_local_complement(meta: Optional[dict]) -> Optional[dict]:
    """
    Complément **local** de :func:`meta_shared_projection`.

    Renvoie une COPIE ne contenant QUE les champs **non** partageables — tout
    ce qui n'est pas dans ``SHARED_META_FIELDS`` : le bloc ``graph_memory``
    complet (endpoints, token, memory_id, métriques) ET tout champ futur non
    classé. C'est la moitié « locale » de la frontière de réplication, en
    miroir exact de la projection partagée.

    Politique « deny-by-default » : un champ inconnu retombe ICI (local), donc
    n'est jamais répliqué par accident. Avec ``meta_shared_projection`` elles
    partitionnent le document sans perte ::

        {**meta_local_complement(m), **meta_shared_projection(m)} == m

    Args:
        meta: dict ``_meta.json`` brut (ou None si l'espace n'existe pas).

    Returns:
        Une COPIE des seuls champs locaux, ou ``None`` si l'entrée était None.
        Ne mute jamais l'entrée.
    """
    if meta is None:
        return None
    return {k: v for k, v in meta.items() if k not in SHARED_META_FIELDS}


class SpaceMeta(BaseModel):
    """
    Métadonnées d'un espace (_meta.json sur S3).

    Créé par space_create, mis à jour par bank_consolidate.
    """

    space_id: str
    description: str = ""
    owner: str = ""
    created_at: str = ""  # ISO 8601
    last_consolidation: Optional[str] = None  # ISO 8601 ou None
    consolidation_count: int = 0
    total_notes_processed: int = 0
    graph_memory: Optional[GraphMemoryConfig] = None  # Connexion graph-memory
    version: int = 1


# =============================================================================
# Live — Notes en temps réel
# =============================================================================


class LiveNote(BaseModel):
    """
    Une note live (front-matter YAML + contenu Markdown).

    Chaque note = 1 fichier S3 unique dans {space_id}/live/.
    Naming convention : {YYYYMMDD}T{HHMMSS}_{agent}_{category}_{uuid8}.md
    """

    filename: str = ""  # Nom du fichier S3 (sans le préfixe)
    timestamp: str = ""  # ISO 8601
    agent: str = ""  # Identifiant de l'agent auteur
    category: str = (
        ""  # observation, decision, todo, insight, question, progress, issue
    )
    tags: list[str] = Field(default_factory=list)
    space_id: str = ""
    content: str = ""  # Corps de la note (sans le front-matter)
    size: int = 0  # Taille en octets du fichier complet

    # Catégories autorisées
    VALID_CATEGORIES: list[str] = [
        "observation",
        "decision",
        "todo",
        "insight",
        "question",
        "progress",
        "issue",
    ]


# =============================================================================
# Bank — Fichiers consolidés
# =============================================================================


class BankFile(BaseModel):
    """
    Un fichier de la memory bank (Markdown pur, sans front-matter).

    Les fichiers bank sont créés et maintenus exclusivement par le LLM
    lors de la consolidation. Les noms sont décidés par le LLM selon les rules.
    """

    filename: str = ""  # Ex: "activeContext.md"
    content: str = ""  # Contenu Markdown complet
    size: int = 0  # Taille en octets
    last_modified: Optional[str] = None  # ISO 8601
    action: str = ""  # "created", "updated", "unchanged" (post-consolidation)


# =============================================================================
# Tokens — Authentification
# =============================================================================


class TokenInfo(BaseModel):
    """
    Infos d'un token d'authentification (stocké dans _system/tokens.json).

    Le token en clair n'est JAMAIS stocké — seul le hash SHA-256 est conservé.
    """

    hash: str = ""  # "sha256:{hex}" — identifiant unique
    name: str = ""  # Nom descriptif (ex: "agent-cline")
    email: str = ""  # Email du propriétaire (optionnel, traçabilité)
    permissions: list[str] = Field(
        default_factory=list
    )  # ["read"], ["read", "write"], etc.
    space_ids: list[str] = Field(
        default_factory=list
    )  # non-admin : [] = aucun accès ; admin v2 : [] obligatoire (aucun scope dormant)
    created_at: str = ""  # ISO 8601
    expires_at: Optional[str] = None  # ISO 8601 ou None (jamais)
    last_used_at: Optional[str] = None
    revoked: bool = False


class TokensStore(BaseModel):
    """
    Registre complet des tokens (_system/tokens.json).

    Protégé par un asyncio.Lock pour éviter les conflits
    lors de modifications concurrentes par plusieurs admins.
    """

    # v2 marque durablement l'application de la migration historique
    # ``space_ids=[]`` (anciennement wildcard) vers la sémantique stricte
    # ``[] = aucun accès``. Toute nouvelle écriture utilise v2.
    version: int = 2
    tokens: list[TokenInfo] = Field(default_factory=list)


# =============================================================================
# Backup — Sauvegarde & restauration
# =============================================================================


class BackupMeta(BaseModel):
    """
    Métadonnées d'un backup (snapshot d'un espace).

    Stocké dans _backups/{space_id}/{timestamp}/.
    """

    backup_id: str = ""  # "space_id/timestamp"
    space_id: str = ""
    timestamp: str = ""  # ISO 8601
    description: str = ""
    files_count: int = 0
    total_size: int = 0
    created_at: str = ""  # ISO 8601


# =============================================================================
# Consolidation — Résultat du pipeline LLM
# =============================================================================


class ConsolidationResult(BaseModel):
    """
    Résultat d'une consolidation LLM.

    Retourné par bank_consolidate avec les métriques d'exécution.
    """

    notes_processed: int = 0
    notes_remaining: int = 0  # Si > consolidation_max_notes
    bank_files_updated: int = 0
    bank_files_created: int = 0
    bank_files_unchanged: int = 0
    synthesis_size: int = 0
    llm_tokens_used: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    duration_seconds: float = 0.0
