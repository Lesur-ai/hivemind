# -*- coding: utf-8 -*-
"""
Service Graph Bridge — Pont entre Live Memory et Graph Memory.

Ce service permet à un space de pousser ses fichiers bank consolidés
dans une instance Graph Memory (graphe de connaissances) pour la
mémoire long terme.

Flux de push :
    1. Connexion MCP Streamable HTTP à graph-memory
    2. Vérification/création de la mémoire cible
    3. Synchronisation : delete + re-ingest pour chaque fichier bank
    4. Nettoyage des fichiers obsolètes dans graph-memory
    5. Mise à jour des métadonnées du space

Communication : protocole MCP via Streamable HTTP (SDK officiel mcp>=1.8.0).
Graph Memory est un service externe, on utilise son API MCP telle quelle.

Migration SSE → Streamable HTTP (issue #1) :
    - Remplace httpx + httpx-sse par mcp.client.streamable_http
    - Endpoint : /sse → /mcp
    - Plus de handshake manuel (le SDK gère initialize automatiquement)
    - Chaque call_tool crée sa propre session (évite les conflits
      de cancel scope quand appelé depuis le serveur MCP)

Voir le README de graph-memory pour les outils disponibles :
    - memory_create, memory_list, memory_stats
    - memory_ingest, document_list, document_delete
"""

import os
import json
import math
import time
import base64
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import get_settings
from .storage import get_storage, bank_relpath
from .models import GraphMemoryConfig, EMBEDDED_TOKEN_SENTINEL
from .memory_id import derive_memory_id
from .embedded_secret import resolve_embedded_token
from .url_guard import validate_gm_url
from .reservation_guard import assert_space_not_reserved

logger = logging.getLogger("live_mem.graph_bridge")

# P7-3 — classification EXPLICITE du binding persisté dans le bloc
# ``graph_memory`` (jamais inférée depuis url/token — cf. Codex round-2 R3).
_BINDING_EMBEDDED = "embedded"
_BINDING_EXPLICIT = "explicit"
_GRAPH_VIEW_MAX_NODES = 160
_GRAPH_VIEW_MAX_EDGES = 320


# P4-6: every long/graph status response carries this explicit marker so callers
# can never mistake the long graph for an authority. It is protocol-derived only
# and never on the commit / rollback / audit / recovery path (ADR-0010).
_LONG_AUTHORITY_MARKER: dict = {
    "derived": True,
    "authoritative": False,
    "authority_note": (
        "long graph is protocol-derived, not authoritative — never on the "
        "commit / rollback / audit / recovery path (ADR-0010)"
    ),
}

_EMBEDDING_COLLECTION_ERROR_REASONS: dict[str, frozenset[str]] = {
    # Exact reason/state pairs reachable from
    # VectorStoreService.get_collection_info(). Mutation, restore, export, and
    # per-call dynamic-evidence failures are deliberately not status values.
    "reindex_required": frozenset(
        {
            "active_alias_invalid",
            "fingerprint_mismatch",
            "invalid_metadata",
            "legacy_nonempty",
            "legacy_unreadable",
            "memory_namespace_mismatch",
            "payload_ownership_mismatch",
            "shadow_invalid",
            "static_profile_mismatch",
            "vector_config_mismatch",
        }
    ),
    "unavailable": frozenset(
        {
            "active_alias_unreadable",
            "canonical_unreadable",
            "embedding_profile_unavailable",
            "qdrant_unreadable",
            "shadow_validation_failed",
        }
    ),
}
_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")
_REINDEX_PHASES = frozenset(
    {
        "admission",
        "snapshot",
        "rebuild",
        "validate",
        "pre_switch",
        "activated",
        "verified",
    }
)
_REINDEX_ACTIVE_STATES = frozenset(
    {"missing", "ready", "reindex_required", "unavailable"}
)
_REINDEX_MAX_SOURCE_DOCUMENTS = 10_000
_REINDEX_MAX_SOURCE_CHUNKS = 250_000
_REINDEX_ERROR_REASONS = frozenset(
    {
        "active_target_changed",
        "activation_unverified",
        "backend_unavailable",
        "chunking_config_changed",
        "chunking_config_unavailable",
        "embedding_failed",
        "embedding_identity_changed",
        "embedding_invalid",
        "embedding_profile_changed",
        "initial_state_invalid",
        "maintenance_unavailable",
        "namespace_busy",
        "post_switch_unverified",
        "shadow_collision",
        "shadow_creation_failed",
        "shadow_invalid",
        "shadow_write_failed",
        "source_changed",
        "source_chunk_accounting_mismatch",
        "source_chunking_failed",
        "source_chunks_empty",
        "source_document_duplicate",
        "source_extraction_failed",
        "source_hash_mismatch",
        "source_inventory_empty",
        "source_inventory_invalid",
        "source_inventory_unavailable",
        "source_metadata_mismatch",
        "source_object_duplicate",
        "source_object_mismatch",
        "source_object_unavailable",
        "source_ownership_invalid",
        "source_size_limit_exceeded",
        "source_size_mismatch",
        "source_status_invalid",
    }
)
_REINDEX_POST_SWITCH_ERROR_REASONS = frozenset(
    {"activation_unverified", "post_switch_unverified"}
)
_REINDEX_BOUNDARY_ERROR_REASONS = frozenset(
    {
        "binding_unavailable",
        "invalid_result",
        "reindex_failed",
        "runtime_unavailable",
        "space_not_found",
        "unsupported_runtime",
    }
)
_REINDEX_RESULT_KEYS = frozenset(
    {
        "status",
        "phase",
        "reason",
        "operation_id",
        "source_documents",
        "source_chunks",
        "vectors_written",
        "activated",
        "active_state",
    }
)
# A bounded source can still require tens of thousands of sequential provider
# batches. The ordinary 120-second Graph Memory client deadline would make the
# public maintenance path unusable and manufacture an ambiguous activation.
# Keep a finite ceiling, but make it maintenance-sized; operator-facing MCP
# clients and proxies must keep their own request open for the same call.
_REINDEX_CLIENT_TIMEOUT_SECONDS = 7 * 24 * 60 * 60


def _reindex_error(reason: str) -> dict:
    if reason not in _REINDEX_ERROR_REASONS | _REINDEX_BOUNDARY_ERROR_REASONS:
        reason = "reindex_failed"
    return {
        "status": "error",
        "phase": "admission",
        "reason": reason,
        "operation_id": None,
        "source_documents": 0,
        "source_chunks": 0,
        "vectors_written": 0,
        "activated": False,
        "active_state": "unavailable",
    }


def _reindex_uncertain(reason: str) -> dict:
    """Return a retry-unsafe envelope once non-idempotent dispatch may have run."""
    result = _reindex_error(reason)
    result.update({"phase": "activated", "activated": True})
    return result


def _invalid_reindex_result() -> dict:
    return _reindex_uncertain("invalid_result")


def _reindex_result_view(raw: object) -> dict:
    """Project the embedded maintenance response onto one exact safe schema."""
    if type(raw) is not dict or set(raw) != _REINDEX_RESULT_KEYS:
        return _invalid_reindex_result()
    status = raw.get("status")
    phase = raw.get("phase")
    reason = raw.get("reason")
    operation_id = raw.get("operation_id")
    active_state = raw.get("active_state")
    activated = raw.get("activated")
    counts = (
        raw.get("source_documents"),
        raw.get("source_chunks"),
        raw.get("vectors_written"),
    )
    if (
        status not in {"ok", "error"}
        or phase not in _REINDEX_PHASES
        or active_state not in _REINDEX_ACTIVE_STATES
        or type(activated) is not bool
        or any(type(value) is not int or value < 0 for value in counts)
        or counts[0] > _REINDEX_MAX_SOURCE_DOCUMENTS
        or counts[1] > _REINDEX_MAX_SOURCE_CHUNKS
        or counts[2] > counts[1]
        or (
            operation_id is not None
            and (
                type(operation_id) is not str
                or len(operation_id) != 32
                or any(character not in _LOWER_HEX_DIGITS for character in operation_id)
            )
        )
    ):
        return _invalid_reindex_result()
    if status == "ok":
        if (
            phase != "verified"
            or reason is not None
            or operation_id is None
            or activated is not True
            or active_state != "ready"
            or counts[0] < 1
            or counts[1] < 1
            or counts[1] < counts[0]
            or counts[1] != counts[2]
        ):
            return _invalid_reindex_result()
    elif (
        type(reason) is not str
        or reason not in _REINDEX_ERROR_REASONS
        or phase == "verified"
        or (phase == "activated") != activated
        or (reason in _REINDEX_POST_SWITCH_ERROR_REASONS) != activated
        or (activated and active_state != "unavailable")
    ):
        return _invalid_reindex_result()
    return {
        "status": status,
        "phase": phase,
        "reason": reason,
        "operation_id": operation_id,
        "source_documents": counts[0],
        "source_chunks": counts[1],
        "vectors_written": counts[2],
        "activated": activated,
        "active_state": active_state,
    }


def _invalid_embedding_collection_view() -> dict:
    """Return a fresh fixed failure; never reflect malformed backend values."""
    return {"state": "unavailable", "reason": "invalid_status"}


def _embedding_collection_view(raw: object) -> dict:
    """Project one strict, value-free ``memory_stats`` collection status."""
    if type(raw) is not dict:
        return _invalid_embedding_collection_view()

    keys = tuple(dict.keys(raw))
    if any(type(key) is not str for key in keys):
        return _invalid_embedding_collection_view()
    state = dict.get(raw, "state")
    if type(state) is not str:
        return _invalid_embedding_collection_view()

    if state == "missing":
        if len(keys) == 1 and "state" in raw:
            return {"state": "missing"}
        return _invalid_embedding_collection_view()

    if state == "ready":
        if (
            len(keys) != 3
            or "state" not in raw
            or "profile_fingerprint" not in raw
            or "points_count" not in raw
        ):
            return _invalid_embedding_collection_view()
        fingerprint = dict.get(raw, "profile_fingerprint")
        points_count = dict.get(raw, "points_count")
        if (
            type(fingerprint) is not str
            or len(fingerprint) != 64
            or any(character not in _LOWER_HEX_DIGITS for character in fingerprint)
            or type(points_count) is not int
            or points_count < 0
        ):
            return _invalid_embedding_collection_view()
        return {
            "state": "ready",
            "profile_fingerprint": fingerprint,
            "points_count": points_count,
        }

    if state in _EMBEDDING_COLLECTION_ERROR_REASONS:
        if (
            len(keys) != 2
            or "state" not in raw
            or "reason" not in raw
        ):
            return _invalid_embedding_collection_view()
        reason = dict.get(raw, "reason")
        if (
            type(reason) is not str
            or reason not in _EMBEDDING_COLLECTION_ERROR_REASONS[state]
        ):
            return _invalid_embedding_collection_view()
        return {"state": state, "reason": reason}

    return _invalid_embedding_collection_view()


def _watermark_view(gm_config: dict) -> dict:
    """Read-only view of the P4-5 derived watermark recorded in the local
    ``graph_memory`` block. Coords are ``null`` when not yet recorded / "not
    available" — never fabricated."""
    return {
        "bank_version": gm_config.get("bank_version"),
        "commit_id": gm_config.get("commit_id"),
        "term": gm_config.get("term"),
        "provenance": gm_config.get("provenance"),
        "recorded_at": gm_config.get("recorded_at"),
        "flagged": bool(gm_config.get("flagged", False)),
    }


def _binding_view(gm_config: dict) -> str:
    """Return the effective persisted binding classification for read views.

    P8-3/G4 exposes this already-authoritative classification without deriving
    it from the URL.  Pre-P7-3 embedded bindings did not persist ``binding``;
    their token sentinel remains the only supported legacy fallback, matching
    ``_resolve_or_embedded`` exactly.  Every other bound record is explicit.
    """
    binding = gm_config.get("binding")
    if binding == _BINDING_EMBEDDED or (
        binding is None and gm_config.get("token") == EMBEDDED_TOKEN_SENTINEL
    ):
        return _BINDING_EMBEDDED
    return _BINDING_EXPLICIT


def _graph_view_text(value: object, limit: int = 280) -> str:
    """Normalize one display-only graph value without exposing raw objects."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value).replace("\x00", "")[:limit]


def _graph_view_mentions(value: object) -> int:
    try:
        return max(0, min(int(value or 0), 1_000_000_000))
    except (TypeError, ValueError):
        return 0


def _graph_view_payload(raw: dict) -> dict:
    """Build the bounded, browser-safe subset of ``memory_graph``.

    Graph Memory's full response intentionally contains document URIs, hashes,
    repository paths, source document lists, and backend identifiers. The admin
    console needs none of them. This boundary uses synthetic node IDs and an
    explicit display-field allowlist so those values cannot reach the browser.
    """
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return {"status": "unavailable", "message": "Graph data is unavailable."}

    raw_nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    raw_edges = raw.get("edges") if isinstance(raw.get("edges"), list) else []
    entities = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and node.get("node_type") != "document"
    ]
    documents = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and node.get("node_type") == "document"
    ]
    entities.sort(key=lambda node: _graph_view_mentions(node.get("mentions")), reverse=True)

    document_budget = min(24, _GRAPH_VIEW_MAX_NODES // 4)
    selected = entities[: _GRAPH_VIEW_MAX_NODES - document_budget]
    selected.extend(documents[:document_budget])
    if len(selected) < _GRAPH_VIEW_MAX_NODES:
        entity_ids = {id(node) for node in selected}
        remainder = [node for node in entities if id(node) not in entity_ids]
        remainder.extend(documents[document_budget:])
        selected.extend(remainder[: _GRAPH_VIEW_MAX_NODES - len(selected)])

    public_nodes: list[dict] = []
    public_id_by_raw: dict[str, str] = {}
    for index, node in enumerate(selected):
        raw_id = str(node.get("id") or "")
        if not raw_id or raw_id in public_id_by_raw:
            continue
        public_id = f"n{len(public_nodes) + 1}"
        public_id_by_raw[raw_id] = public_id
        node_type = "document" if node.get("node_type") == "document" else "entity"
        display_node = {
            "id": public_id,
            "label": _graph_view_text(node.get("label") or node.get("name") or "Untitled", 120),
            "type": _graph_view_text(node.get("type") or ("Document" if node_type == "document" else "Unknown"), 80),
            "description": "" if node_type == "document" else _graph_view_text(node.get("description"), 500),
            "mentions": _graph_view_mentions(node.get("mentions")),
            "node_type": node_type,
        }
        if node_type == "document":
            display_node["filename"] = _graph_view_text(node.get("filename") or node.get("label"), 160)
        public_nodes.append(display_node)

    public_edges: list[dict] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = public_id_by_raw.get(str(edge.get("from") or ""))
        target = public_id_by_raw.get(str(edge.get("to") or ""))
        if not source or not target:
            continue
        try:
            weight = float(edge.get("weight") or 1)
        except (TypeError, ValueError):
            weight = 1.0
        if not math.isfinite(weight):
            weight = 1.0
        public_edges.append(
            {
                "id": f"e{len(public_edges) + 1}",
                "from": source,
                "to": target,
                "type": _graph_view_text(
                    edge.get("type") or edge.get("label") or "RELATED_TO", 80
                ),
                "weight": max(0.1, min(weight, 1000.0)),
            }
        )
        if len(public_edges) >= _GRAPH_VIEW_MAX_EDGES:
            break

    return {
        "status": "ok",
        "nodes": public_nodes,
        "edges": public_edges,
        "node_count": len(public_nodes),
        "edge_count": len(public_edges),
        "total_node_count": len(raw_nodes),
        "total_edge_count": len(raw_edges),
        "truncated": len(public_nodes) < len(raw_nodes) or len(public_edges) < len(raw_edges),
    }


# ─────────────────────────────────────────────────────────────
# Client MCP léger pour communiquer avec Graph Memory
# ─────────────────────────────────────────────────────────────


class GraphMemoryClient:
    """
    Client MCP Streamable HTTP pour appeler les outils de Graph Memory.

    Chaque appel call_tool() crée sa propre connexion MCP complète
    (transport + session + initialize + appel + fermeture).

    C'est volontaire : le SDK MCP utilise des anyio TaskGroups qui ne
    supportent pas d'être ouvertes dans une task et fermées dans une
    autre (erreur "cancel scope in different task"). Comme le serveur
    MCP exécute les outils dans ses propres tasks, un context manager
    persistant casse. Chaque appel auto-contenu résout le problème.

    Pour les opérations multi-appels (push), on utilise call_tools_batch()
    qui exécute tout dans un seul scope asyncio.

    Usage :
        gm = GraphMemoryClient("http://localhost:8080", "token")
        result = await gm.call_tool("memory_list", {})
    """

    def __init__(self, base_url: str, token: str, timeout: float = 120.0):
        """
        Args:
            base_url: URL de base de graph-memory (ex: "http://localhost:8080")
            token: Bearer token pour l'authentification
            timeout: Timeout par appel d'outil en secondes
        """
        # NOTE: ce pont Hivemind→graph-memory est du trafic INTERNE (réseau
        # Compose) et reste TOUJOURS direct, même si PROXY_URL est défini —
        # c'est la classification P12-3 (#268), et streamablehttp_client
        # (SDK MCP officiel) n'expose de toute façon ni proxy ni http_client.
        # L'egress Internet du service graph-memory lui-même (LLM, S3) honore
        # PROXY_URL depuis P12-3 côté service embarqué.

        # Normaliser l'URL : retirer /sse ou /mcp si présent en fin
        self._base_url = base_url.rstrip("/")
        for suffix in ("/sse", "/mcp"):
            if self._base_url.endswith(suffix):
                self._base_url = self._base_url[: -len(suffix)]
        self._token = token
        self._timeout = timeout

    @property
    def _headers(self) -> dict:
        """Headers HTTP avec authentification Bearer."""
        h = {}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @property
    def _mcp_url(self) -> str:
        return f"{self._base_url}/mcp"

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """
        Appelle un outil MCP sur Graph Memory (session auto-contenue).

        Crée une connexion complète pour chaque appel :
        transport → session → initialize → call_tool → fermeture.

        Args:
            tool_name: Nom de l'outil (ex: "memory_create")
            arguments: Paramètres de l'outil

        Returns:
            Résultat de l'outil (dict)
        """
        try:
            async with streamablehttp_client(
                self._mcp_url,
                headers=self._headers,
                timeout=self._timeout,
                sse_read_timeout=self._timeout,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments),
                        timeout=self._timeout,
                    )

                    # Extraire le résultat (SDK MCP encapsule dans content[0].text)
                    if result.content and len(result.content) > 0:
                        text = result.content[0].text
                        try:
                            return json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            return {"status": "ok", "raw": text}

                    return {"status": "ok", "raw": ""}

        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Timeout après {self._timeout}s pour '{tool_name}' sur Graph Memory"
            )
        except Exception as e:
            raise ConnectionError(f"Erreur MCP '{tool_name}' sur Graph Memory : {e}")

    async def call_tools_batch(self, calls: list[tuple[str, dict]]) -> list[dict]:
        """
        Exécute plusieurs appels d'outils dans une seule session MCP.

        Utile pour les opérations multi-appels (push) : une seule
        connexion pour N appels, tout dans le même scope asyncio.

        Args:
            calls: Liste de (tool_name, arguments) tuples

        Returns:
            Liste de résultats (même ordre que les appels)
        """
        results = []
        try:
            async with streamablehttp_client(
                self._mcp_url,
                headers=self._headers,
                timeout=self._timeout,
                sse_read_timeout=self._timeout,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    for tool_name, arguments in calls:
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(tool_name, arguments),
                                timeout=self._timeout,
                            )
                            if result.content and len(result.content) > 0:
                                text = result.content[0].text
                                try:
                                    results.append(json.loads(text))
                                except (json.JSONDecodeError, TypeError):
                                    results.append({"status": "ok", "raw": text})
                            else:
                                results.append({"status": "ok", "raw": ""})
                        except asyncio.TimeoutError:
                            results.append(
                                {
                                    "status": "error",
                                    "message": f"Timeout {self._timeout}s pour '{tool_name}'",
                                }
                            )
                        except Exception as e:
                            results.append(
                                {
                                    "status": "error",
                                    "message": f"Erreur '{tool_name}': {e}",
                                }
                            )

        except Exception as e:
            # Si la connexion elle-même échoue, remplir tous les résultats manquants
            while len(results) < len(calls):
                results.append(
                    {
                        "status": "error",
                        "message": f"Connexion Graph Memory échouée : {e}",
                    }
                )

        return results

    # Context manager pour compatibilité (délègue à call_tool par appel)
    async def __aenter__(self):
        logger.info(
            "GraphMemoryClient connecté (mode appels auto-contenus) : %s",
            self._base_url,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass  # Rien à fermer — chaque call_tool gère sa propre session


# ─────────────────────────────────────────────────────────────
# Service Graph Bridge
# ─────────────────────────────────────────────────────────────


class GraphBridgeService:
    """
    Service orchestrateur pour le pont live-memory → graph-memory.

    Gère la configuration de connexion, la synchronisation des fichiers
    bank, et les métriques de push.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[..., "GraphMemoryClient"]] = None,
        url_validator: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        # Seam d'injection (P4-4). Les défauts reproduisent EXACTEMENT la
        # production : la vraie classe GraphMemoryClient et le vrai validateur
        # SSRF. AUCUN client n'est construit ici (lazy) — la factory n'est
        # invoquée qu'aux sites d'usage, donc `GraphBridgeService()` (singleton
        # + EngineRegistry) ne crée aucun client MCP/réseau à la construction.
        #
        # `client_factory` par défaut est l'OBJET classe GraphMemoryClient (pas
        # un lambda) : zéro frame intermédiaire, la preuve byte-for-byte tient.
        self._client_factory = client_factory or GraphMemoryClient
        self._url_validator = url_validator or validate_gm_url

    def _make_client(self, url: str, token: str, **kwargs) -> "GraphMemoryClient":
        # Site unique de construction. La factory par défaut EST la classe
        # GraphMemoryClient, donc cet appel est byte-for-byte identique à la
        # construction inline historique, y compris le kwarg timeout=180.0 du
        # chemin push/ingest (forwardé via **kwargs).
        return self._client_factory(url, token, **kwargs)

    async def _load_gm_config(
        self, space_id: str
    ) -> tuple[Optional[GraphMemoryConfig], Optional[dict]]:
        """Charge la config Graph Memory locale d'un space (READ, jamais bind).

        Thin wrapper sur le seam unique ``_resolve_or_embedded`` (P7-3) :
        conserve la signature ``(config, err)`` pour les 5 méthodes typées de
        projection. ``provision=False`` → aucune écriture ; un space non lié
        renvoie l'erreur historique not_found / non-connecté (byte-for-byte).
        """
        config, _block, err = await self._resolve_or_embedded(
            space_id, provision=False
        )
        return config, err

    def _guard_url(self, url: str) -> Optional[dict]:
        """Applique la garde SSRF AVANT toute connexion (chemin adaptateur).

        Retourne ``None`` si l'URL est sûre, sinon un dict d'erreur prêt à
        renvoyer. Doit être appelé AVANT ``_make_client`` dans toute nouvelle
        méthode, en miroir de la validation que fait le tool layer pour connect.

        Finding 1 (revue Codex PR #150) : l'URL du runtime « long » embarqué
        (config OPÉRATEUR, défaut ``http://graph-memory:8002``) pointe
        LÉGITIMEMENT vers une IP privée du réseau Docker. Le garde SSRF renforcé
        (HM-11, qui résout le DNS et bloque les IP privées) la rejetait, cassant
        l'auto-bind embedded P7 (ADR-0019). On reconnaît cette URL de CONFIANCE
        par forme canonique et on la valide en mode ``allow_private_hosts``.
        Toute AUTRE url (``graph_connect`` explicite, non fiable) reste pleinement
        gardée — même niveau de confiance que S3 / LLMaaS (config opérateur).
        """
        from ..config import get_settings

        embedded = get_settings().long_embedded_url
        trusted = bool(embedded) and self._canonical_url(url) == self._canonical_url(
            embedded
        )
        err = self._url_validator(url, allow_private_hosts=trusted)
        if err:
            return {"status": "error", "message": err}
        return None

    # ─────────────────────────────────────────────────────────
    # P7-3 — Seam de résolution embedded/explicit (source UNIQUE)
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _canonical_url(url: str) -> str:
        """Forme canonique pour comparer embedded vs explicit.

        Strip trailing ``/`` et un suffixe ``/mcp`` ou ``/sse`` (le client les
        retire aussi), casse-insensible. Évite qu'un override explicite sur la
        MÊME URL (à un slash/suffixe près) soit mésclassifié comme embedded.
        """
        u = (url or "").strip().rstrip("/")
        for suffix in ("/mcp", "/sse"):
            if u.endswith(suffix):
                u = u[: -len(suffix)]
        return u.rstrip("/").lower()

    @staticmethod
    def _health_ok(health: object) -> bool:
        """Fail-closed : santé GM valide UNIQUEMENT si ``status ∈ {ok, healthy}``.

        Une réponse malformée (non-dict, ou sans ``status``) → ``False`` : jamais
        de fail-open sur « pas explicitement error » (Codex round-1 attack Q1)."""
        return isinstance(health, dict) and health.get("status") in ("ok", "healthy")

    def _sentinelize_for_persist(self, block: dict) -> dict:
        """Retourne une COPIE du bloc ``graph_memory`` SÛRE à persister.

        Pour un binding embedded (``binding == _BINDING_EMBEDDED``), remplace le
        token par le sentinel : le token embarqué VIVANT n'est JAMAIS écrit dans
        ``_meta.json`` (ni donc dans les backups qui copient ``_meta.json`` brut,
        backup.py). No-op pour un binding explicite (token opérateur conservé,
        masqué seulement à l'egress)."""
        if not isinstance(block, dict):
            return block
        if block.get("binding") == _BINDING_EMBEDDED:
            return {**block, "token": EMBEDDED_TOKEN_SENTINEL}
        return dict(block)

    async def _resolve_or_embedded(
        self, space_id: str, *, provision: bool = False
    ) -> tuple[Optional[GraphMemoryConfig], Optional[dict], Optional[dict]]:
        """SOURCE UNIQUE de résolution de la config ``graph_memory`` d'un space.

        Retourne ``(config, block, None)`` — ``config`` porte le token VIVANT
        (dé-sentinelisé, pour construire le client) ; ``block`` est le dict brut
        (token = sentinel pour un embedded) que l'appelant ré-persiste — ou
        ``(None, None, error_dict)``.

        Précédence : (1) override explicite persisté → tel quel ; (2) binding
        embedded persisté → injecte le token vivant (URL doit matcher l'URL
        embarquée, sinon fail-closed) ; (3) absent → ``provision=False`` : erreur
        historique ; ``provision=True`` (chemin d'écriture ``push`` UNIQUEMENT) :
        auto-bind embedded.

        Toute URL résolue passe la garde SSRF AVANT construction du client —
        embedded ET explicite (corrige le trou pré-P7-3 où push/status
        construisaient un client sans ``_guard_url``, graph_bridge.py:638/:907).
        """
        storage = get_storage()
        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return None, None, {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        block = meta_data.get("graph_memory")
        settings = get_settings()
        embedded_url = settings.long_embedded_url

        if block:
            binding = block.get("binding")
            token = block.get("token")
            # Classification EXPLICITE : embedded ssi binding=embedded (ou legacy
            # token==sentinel). Jamais inférée depuis l'URL seule.
            is_embedded = binding == _BINDING_EMBEDDED or (
                binding is None and token == EMBEDDED_TOKEN_SENTINEL
            )

            if not is_embedded:
                # (1) Override explicite → tel quel, garde SSRF sur l'URL persistée.
                guard = self._guard_url(block.get("url", ""))
                if guard is not None:
                    return None, None, guard
                return GraphMemoryConfig(**block), block, None

            # (2) Binding embedded : l'URL persistée DOIT correspondre à l'URL
            # embarquée courante, sinon fail-closed (ne JAMAIS envoyer le token
            # embarqué à une URL non-embarquée — anti-leak).
            if self._canonical_url(block.get("url", "")) != self._canonical_url(
                embedded_url
            ):
                return None, None, {
                    "status": "error",
                    "connected": False,
                    "message": (
                        "Binding embedded incohérent : l'URL persistée ne "
                        "correspond pas au runtime long embarqué — refus "
                        "fail-closed."
                    ),
                    "long_authority": _LONG_AUTHORITY_MARKER,
                }
            # Chemin lecture (provision=False) : NE GÉNÈRE PAS de secret (status
            # reste read-only). Chemin write (provision=True) : peut générer.
            live_token = resolve_embedded_token(settings, generate=provision)
            if not live_token:
                return None, None, {
                    "status": "error",
                    "connected": False,
                    "reachable": False,
                    "message": "Secret du runtime long embarqué indisponible.",
                    "long_authority": _LONG_AUTHORITY_MARKER,
                }
            guard = self._guard_url(block.get("url", ""))
            if guard is not None:
                return None, None, guard
            cfg = GraphMemoryConfig(**{**block, "token": live_token})
            return cfg, block, None

        # (3) Aucun bloc persisté.
        if not provision:
            return None, None, {
                "status": "error",
                "message": (
                    f"Espace '{space_id}' non connecté à Graph Memory. "
                    f"Utilisez graph_connect d'abord."
                ),
            }

        # (3b) provision=True — chemin d'écriture (graph_push) UNIQUEMENT.
        return await self._provision_embedded(space_id, meta_data, settings)

    async def _provision_embedded(
        self, space_id: str, meta_data: dict, settings
    ) -> tuple[Optional[GraphMemoryConfig], Optional[dict], Optional[dict]]:
        """Auto-bind embedded (P7-3). Appelé UNIQUEMENT depuis le chemin write.

        Ordre (Codex round-2 R1) : résoudre le token (rejet sentinel) → garde
        SSRF → ENREGISTRER le hash du token AVANT tout appel GM authentifié
        (Model B : le GM valide via le store S3) → health strict → memory_list →
        memory_create (exists=succès via re-check) → retourne le bloc sentinelisé
        que l'appelant persiste EN DERNIER. Toute erreur → aucun bloc « bound »
        persisté (l'appelant n'atteint pas la persistance) ; le token enregistré
        est un état infra idempotent bénin, réutilisé au bind suivant.
        """
        url = settings.long_embedded_url
        token = resolve_embedded_token(settings)
        if not url or not token or token == EMBEDDED_TOKEN_SENTINEL:
            return None, None, {
                "status": "error",
                "connected": False,
                "message": "Runtime long embarqué non configuré (URL/token).",
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        guard = self._guard_url(url)
        if guard is not None:
            return None, None, guard

        # Enregistrer le hash AVANT tout appel GM authentifié (le token ne peut
        # appeler system_health/memory_list/memory_create qu'une fois enregistré
        # dans _system/tokens.json). Idempotent + rotation (P7-3 R1/R5).
        from .tokens import get_token_service

        reg = await get_token_service().register_internal_long_token(token)
        if reg.get("status") != "ok" or reg.get("current_active") is not True:
            return None, None, {
                "status": "error",
                "connected": False,
                "message": (
                    "Token interne long indisponible ou inactif : "
                    f"{reg.get('message', '')}"
                ),
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        memory_id = derive_memory_id(space_id)
        try:
            # Protected certification runs two sequential provider-discovery
            # probes inside Graph Memory's ``system_health``.  A complete,
            # validated strict-certification environment grants that one
            # auto-bind call its reviewed larger bound.  Ordinary runtimes
            # retain the historical constructor byte-for-byte (no timeout
            # kwarg, therefore GraphMemoryClient's 120-second default), while
            # a partial strict environment raises here before any health call.
            # Route through the existing core inference seam.  This keeps the
            # long bridge structurally independent of the commit-state
            # ``live_mem.core.hivemind`` package without hiding a dynamic
            # import from the negative-import guards.
            from .inference_runtime import (
                protected_certification_graph_health_timeout_seconds,
            )

            graph_health_timeout = (
                protected_certification_graph_health_timeout_seconds()
            )
            if graph_health_timeout is None:
                gm = self._make_client(url, token)
            else:
                gm = self._make_client(
                    url,
                    token,
                    timeout=graph_health_timeout,
                )

            health = await gm.call_tool("system_health", {})
            if not self._health_ok(health):
                return None, None, {
                    "status": "error",
                    "connected": False,
                    "reachable": False,
                    "message": "Runtime long embarqué indisponible (health).",
                    "long_authority": _LONG_AUTHORITY_MARKER,
                }

            memories = await gm.call_tool("memory_list", {})
            existing_ids = []
            if isinstance(memories, dict) and memories.get("status") == "ok":
                existing_ids = [
                    m.get("memory_id", m.get("id", ""))
                    for m in memories.get("memories", [])
                ]

            if memory_id not in existing_ids:
                create = await gm.call_tool(
                    "memory_create",
                    {
                        "memory_id": memory_id,
                        "name": f"Hivemind long — {space_id}",
                        "description": (
                            f"Embedded long memory for Hivemind space '{space_id}'"
                        ),
                        "ontology": "general",
                    },
                )
                # Race (P7-3 R4) : GM renvoie « existe déjà » en DICT d'erreur
                # (server.py:247, pas une exception). Re-check via memory_list →
                # présence = succès (robuste, sans matcher le message localisé).
                if isinstance(create, dict) and create.get("status") == "error":
                    recheck = await gm.call_tool("memory_list", {})
                    recheck_ids = []
                    if isinstance(recheck, dict) and recheck.get("status") == "ok":
                        recheck_ids = [
                            m.get("memory_id", m.get("id", ""))
                            for m in recheck.get("memories", [])
                        ]
                    if memory_id not in recheck_ids:
                        return None, None, {
                            "status": "error",
                            "connected": False,
                            "message": (
                                "Création de la mémoire embarquée échouée : "
                                f"{create.get('message', '')}"
                            ),
                            "long_authority": _LONG_AUTHORITY_MARKER,
                        }
        except ConnectionError as e:
            return None, None, {
                "status": "error",
                "connected": False,
                "reachable": False,
                "message": f"Connexion impossible au runtime long embarqué : {e}",
                "long_authority": _LONG_AUTHORITY_MARKER,
            }
        except Exception as e:
            return None, None, {
                "status": "error",
                "connected": False,
                "message": f"Erreur d'auto-bind embarqué : {e}",
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        # Bloc sentinelisé retourné à l'appelant (push) qui le persiste EN
        # DERNIER, avec ses métriques. Le token vivant n'est JAMAIS dans le bloc.
        block = {
            "binding": _BINDING_EMBEDDED,
            "url": url,
            "token": EMBEDDED_TOKEN_SENTINEL,
            "memory_id": memory_id,
            "ontology": "general",
        }
        cfg = GraphMemoryConfig(**{**block, "token": token})
        return cfg, block, None

    # ─────────────────────────────────────────────────────────
    # CONNECT — Configurer la connexion graph-memory
    # ─────────────────────────────────────────────────────────

    async def connect(
        self,
        space_id: str,
        url: str,
        token: str,
        memory_id: str,
        ontology: str = "general",
    ) -> dict:
        """
        Connecte un space à une instance Graph Memory.

        Opérations :
        1. Vérifie que le space existe
        2. Teste la connexion à graph-memory (health check)
        3. Vérifie/crée la mémoire cible dans graph-memory
        4. Sauvegarde la config dans _meta.json

        Args:
            space_id: Identifiant du space live-memory
            url: URL de graph-memory (ex: "http://localhost:8080" ou "/mcp")
            token: Bearer token pour graph-memory
            memory_id: Memory cible dans graph-memory
            ontology: Ontologie à utiliser (défaut: "general")

        Returns:
            {"status": "connected", ...} ou erreur
        """
        await assert_space_not_reserved(space_id)
        # P7-3 : un token opérateur ne peut JAMAIS valoir le sentinel embarqué
        # (sinon un override explicite deviendrait indistinguable d'un embedded
        # et le sentinel serait persisté comme bearer). Fail-closed.
        if token == EMBEDDED_TOKEN_SENTINEL:
            return {
                "status": "error",
                "message": (
                    f"Token réservé interdit ('{EMBEDDED_TOKEN_SENTINEL}') — "
                    "valeur sentinelle du runtime long embarqué."
                ),
            }

        storage = get_storage()

        # Vérifier que le space existe
        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Garde SSRF sur l'URL fournie AVANT toute construction de client.
        guard = self._guard_url(url)
        if guard is not None:
            return guard

        # Tester la connexion à graph-memory
        try:
            gm = self._make_client(url, token)

            # Vérifier la santé — fail-closed : healthy UNIQUEMENT si status ∈
            # {ok, healthy} (une réponse malformée n'est PAS un succès, MINOR-1).
            health = await gm.call_tool("system_health", {})
            if not self._health_ok(health):
                return {
                    "status": "error",
                    "message": (
                        f"Graph Memory non disponible : "
                        f"{health.get('message', 'erreur inconnue') if isinstance(health, dict) else 'réponse invalide'}"
                    ),
                }

            # Vérifier si la mémoire existe déjà
            memories = await gm.call_tool("memory_list", {})
            existing_ids = []
            if memories.get("status") == "ok":
                existing_ids = [
                    m.get("memory_id", m.get("id", ""))
                    for m in memories.get("memories", [])
                ]

            memory_created = False
            if memory_id not in existing_ids:
                # Créer la mémoire dans graph-memory
                create_result = await gm.call_tool(
                    "memory_create",
                    {
                        "memory_id": memory_id,
                        "name": f"Live Memory — {space_id}",
                        "description": (
                            f"Memory Bank synchronisée depuis live-memory "
                            f"space '{space_id}'"
                        ),
                        "ontology": ontology,
                    },
                )

                if create_result.get("status") == "error":
                    return {
                        "status": "error",
                        "message": (
                            f"Impossible de créer la mémoire '{memory_id}' "
                            f"dans Graph Memory : "
                            f"{create_result.get('message', '')}"
                        ),
                    }
                memory_created = True
                logger.info(
                    "Mémoire '%s' créée dans Graph Memory (ontologie: %s)",
                    memory_id,
                    ontology,
                )

        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur lors du test de connexion : {e}",
            }

        # Sauvegarder la config dans _meta.json — binding EXPLICITE (P7-3) :
        # un override opérateur est classé "explicit", jamais confondu avec un
        # embedded. _sentinelize_for_persist est un no-op ici (token conservé).
        graph_config = GraphMemoryConfig(
            url=url,
            token=token,
            memory_id=memory_id,
            ontology=ontology,
        )
        meta_data["graph_memory"] = self._sentinelize_for_persist(
            {**graph_config.model_dump(), "binding": _BINDING_EXPLICIT}
        )
        await storage.put_json(f"{space_id}/_meta.json", meta_data)

        logger.info(
            "Space '%s' connecté à Graph Memory '%s' (%s)",
            space_id,
            memory_id,
            url,
        )

        return {
            "status": "connected",
            "space_id": space_id,
            "graph_memory": {
                "url": url,
                "memory_id": memory_id,
                "ontology": ontology,
                "memory_created": memory_created,
            },
        }

    # ─────────────────────────────────────────────────────────
    # WATERMARK — Lecture READ-ONLY des coords committées (ADR-0017)
    # ─────────────────────────────────────────────────────────

    async def _read_committed_coords(
        self, space_id: str
    ) -> tuple[Optional[int], Optional[str], Optional[int]]:
        """Latest COMMITTED (bank_version, commit_id, term) or (None,None,None).

        Read-only downstream consumption (ADR-0017). Best-effort: any
        absence/malformation/exception degrades to all-None, never raises,
        never fabricates, never feeds commit validity.

        Source : le pointeur courant ``bank_version.json`` + l'entrée du journal
        des commits ``commits/{bank_version:020d}.json`` sous
        ``{space_id}/_hivemind/`` — lues via ``storage.get_json``.

        Les chemins sont construits EN DUR ici (et NON via ``hivemind.layout``)
        pour que le tier ``long`` n'importe RIEN du sous-paquet ``hivemind`` :
        ``hivemind/__init__`` exécute ``from .state import …`` au chargement, donc
        importer ``layout`` tirerait transitivement le module d'ÉTAT de commit
        dans le graphe d'import du bridge downstream. La concordance avec
        ``layout.bank_version_key`` / ``layout.commit_key`` (autorité canonique)
        est verrouillée par un test anti-drift (test_long_watermark). All-or-
        nothing : si term ou commit_id manque, on dégrade les trois.
        """
        try:
            storage = get_storage()

            pointer = await storage.get_json(f"{space_id}/_hivemind/bank_version.json")
            if not pointer:
                return None, None, None

            bank_version = pointer.get("bank_version")
            if not isinstance(bank_version, int) or bank_version < 0:
                return None, None, None

            commit = await storage.get_json(
                f"{space_id}/_hivemind/commits/{bank_version:020d}.json"
            )
            if not commit:
                return None, None, None

            term = commit.get("term")
            if not isinstance(term, int) or term < 0:
                return None, None, None

            # commit_id autoritatif du journal, repli sur le pointeur.
            commit_id = commit.get("commit_id") or pointer.get("commit_id")
            if not commit_id:
                return None, None, None

            return bank_version, commit_id, term
        except Exception:
            # Best-effort : aucune erreur de lecture ne doit faire échouer un
            # push GM réussi — on dégrade silencieusement à "non disponible".
            return None, None, None

    # ─────────────────────────────────────────────────────────
    # PUSH — Pousser la bank dans graph-memory
    # ─────────────────────────────────────────────────────────

    async def push(self, space_id: str, *, include_volatile: bool = False) -> dict:
        """
        Pousse les fichiers bank du space dans graph-memory.

        Synchronisation intelligente (delete + re-ingest) :
        1. Liste les documents existants dans graph-memory
        2. Pour chaque fichier bank :
           - Si existe dans graph-memory → delete puis re-ingest
           - Sinon → ingest
        3. Supprime les documents orphelins du namespace bank-mirror
           (nettoyage SCOPÉ : seuls les docs précédemment mirrorés ET sans
           ``source_path`` peuvent être supprimés — un doc canonique P4-7 ou
           archive n'est JAMAIS supprimé même s'il n'a plus de fichier bank
           correspondant). Data-loss critical (P4-8).
        4. Met à jour _meta.json avec les métriques de push + le ledger
           ``bank_mirror`` (set des relpaths réellement mirrorés cette fois).

        Garde-fou volatil (P4-8) : par défaut, les fichiers volatils configurés
        (``GRAPH_PUSH_VOLATILE_FILES``, ex. activeContext.md / progress.md) sont
        SAUTÉS — jamais ingérés, jamais supprimés — et reportés dans
        ``skipped_volatile``. Le filtre s'applique sur le BASENAME du relpath
        normalisé (après bank_relpath). ``include_volatile=True`` force le push
        de tous les fichiers ; la garde de permission 'manage' + l'audit
        structuré vivent au tool layer (ADR-0010 : le bridge reste pass-through).

        Utilise call_tools_batch() pour exécuter tous les appels
        dans une seule session MCP (performance).

        Args:
            space_id: Identifiant du space live-memory
            include_volatile: si True, n'applique PAS le filtre volatil (force le
                push de tous les fichiers bank). Le gating 'manage' + l'audit
                sont assurés en amont par le tool layer.

        Returns:
            {"status": "ok", "pushed": N, "pushed_files": [...],
             "skipped_volatile": [...], "cleaned_orphans": N, "errors": N, ...}
        """
        await assert_space_not_reserved(space_id)
        storage = get_storage()
        t0 = time.monotonic()

        # Lire la config
        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # P7-3 : la résolution de la config (embedded/explicit + auto-bind) est
        # DÉFÉRÉE après le court-circuit "bank vide" ci-dessous, pour préserver
        # l'invariant "bank ORIGINALE vide → aucun contact GM, aucune écriture"
        # (test_empty_bank_skips_push_and_records_no_watermark). Un push dont la
        # bank est non-vide provisionne le binding embedded au besoin.

        # Lire tous les fichiers bank depuis S3
        bank_data = await storage.list_and_get(f"{space_id}/bank/")
        bank_files = {
            bank_relpath(item["key"], space_id): item["content"] for item in bank_data
        }

        # Le test d'early-return s'appuie sur l'emptiness ORIGINALE de la bank
        # (avant filtrage volatil), de sorte qu'une bank vide → no-write path
        # (pas de contact GM, pas d'orphelins, pas de watermark), invariant pinné
        # par test_empty_bank_skips_push_and_records_no_watermark. Une bank
        # NON-vide mais 100% volatile, elle, PROCÈDE jusqu'à GM pour que les
        # orphelins bank-mirror enregistrés soient quand même réconciliés.
        original_empty = not bank_files

        # Garde-fou volatil (P4-8) : partition APRÈS bank_relpath, match sur le
        # BASENAME du relpath normalisé (un "1.MEMORY_BANK/activeContext.md" est
        # donc filtré). Le gating 'manage' + l'audit vivent au tool layer.
        volatile_basenames = set(get_settings().graph_push_volatile_files)
        skipped_volatile: list[str] = []
        if not include_volatile:
            kept: dict[str, str] = {}
            for relpath, content in bank_files.items():
                if os.path.basename(relpath) in volatile_basenames:
                    skipped_volatile.append(relpath)
                else:
                    kept[relpath] = content
            bank_files = kept
        skipped_volatile.sort()

        if original_empty:
            return {
                "status": "ok",
                "space_id": space_id,
                "message": "Aucun fichier bank à pousser",
                "pushed": 0,
                "pushed_files": [],
                "skipped_volatile": skipped_volatile,
                "deleted": 0,
                "errors": 0,
            }

        # P7-3 : chemin d'écriture → résout (et auto-bind embedded si non lié).
        # ``gm_config`` est le bloc brut (token sentinel pour un embedded) que le
        # tail ré-persiste ; ``config`` porte le token VIVANT pour le client.
        config, gm_config, err = await self._resolve_or_embedded(
            space_id, provision=True
        )
        if err is not None:
            return err
        memory_id = config.memory_id

        # Connexion à graph-memory
        gm = self._make_client(config.url, config.token, timeout=180.0)

        try:
            # 1. Lister les documents existants dans graph-memory
            doc_list = await gm.call_tool(
                "document_list",
                {
                    "memory_id": memory_id,
                },
            )
            # P7-8 : le VRAI ``document_delete`` de Graph Memory est keyé par
            # ``document_id`` (UUID), PAS par ``filename`` — un delete par
            # filename est rejeté par le serveur. On résout donc ici la map
            # filename → [document_id, ...] depuis le ``document_list``, qui
            # expose ``id``, ``filename`` ET ``source_path`` (clé TOUJOURS
            # présente, ``None`` si absente — contrat homogène GM,
            # core/graph.py). SEULS les docs SANS ``source_path`` sont des
            # copies bank-mirror candidates à suppression : un doc canonique
            # P4-7 porte un ``source_path`` et n'est JAMAIS supprimé, même
            # s'il partage le filename d'un fichier bank. Une entrée mirror
            # sans ``id`` résolvable reste visible dans ``existing_docs``
            # mais n'est JAMAIS candidate (fail-closed : aucune destruction
            # sans preuve positive d'identité).
            existing_docs = set()
            mirror_ids_by_filename: dict[str, list[str]] = {}
            if doc_list.get("status") == "ok":
                for doc in doc_list.get("documents", []):
                    fname = doc.get("filename", "")
                    existing_docs.add(fname)
                    if doc.get("source_path") is not None:
                        continue  # canonique/archive : jamais candidat
                    doc_id = doc.get("id")
                    if doc_id:
                        mirror_ids_by_filename.setdefault(fname, []).append(doc_id)

            logger.info(
                "Push '%s' → '%s' : %d fichiers bank, %d docs existants",
                space_id,
                memory_id,
                len(bank_files),
                len(existing_docs),
            )

            # 2. Construire le batch d'appels (delete + ingest pour chaque fichier)
            calls = []
            call_metadata = []  # Pour tracker quel appel fait quoi

            for filename, content in bank_files.items():
                # Si une copie MIRROR existe → supprimer d'abord. Le delete est
                # keyé par ``document_id`` résolu depuis ``document_list``
                # (P7-8), et restreint aux docs sans ``source_path`` : un doc
                # canonique qui partage le filename n'est jamais touché.
                # Plusieurs ids mirror pour un même filename = copies
                # dupliquées (héritées du bug delete-par-filename) : toutes
                # remplacées. Un doc présent (filename connu) mais sans id
                # mirror résolvable → PAS de delete (fail-closed), l'ingest
                # procède quand même (dupliquer est visible et réparable ;
                # supprimer sans preuve d'identité ne l'est pas).
                if filename in existing_docs:
                    resolved_ids = mirror_ids_by_filename.get(filename, [])
                    if not resolved_ids:
                        logger.warning(
                            "Document '%s' présent dans GM sans copie mirror "
                            "identifiable (id + source_path nul) — delete-"
                            "avant-réingestion SAUTÉ (fail-closed)",
                            filename,
                        )
                    for doc_id in resolved_ids:
                        calls.append(
                            (
                                "document_delete",
                                {
                                    "memory_id": memory_id,
                                    "document_id": doc_id,
                                },
                            )
                        )
                        call_metadata.append(("delete", filename))

                # Encoder en base64 et ingérer
                content_bytes = content.encode("utf-8")
                content_b64 = base64.b64encode(content_bytes).decode("ascii")
                calls.append(
                    (
                        "memory_ingest",
                        {
                            "memory_id": memory_id,
                            "content_base64": content_b64,
                            "filename": filename,
                        },
                    )
                )
                call_metadata.append(("ingest", filename))

            # 3. Nettoyage des orphelins — SCOPÉ au namespace bank-mirror
            #    (data-loss critical, P4-8).
            #
            # DOUBLE PROTECTION : un doc n'est éligible au nettoyage QUE s'il
            # (a) était dans le set bank-mirror PRÉCÉDEMMENT enregistré
            # (ledger ``bank_mirror`` du bloc local graph_memory) ET est
            # absent de la bank courante, ET (b) est identifié côté GM comme
            # copie mirror (``source_path`` nul + ``id`` résolu, cf. la map
            # ci-dessus — P7-8). Les docs canoniques (P4-7, ``source_path``
            # non nul) ne sont ni dans le ledger ni dans la map : jamais
            # candidats. Le complément naïf ``existing_docs -
            # bank_files.keys()`` est INTERDIT : il supprimerait des docs
            # canoniques/archive.
            prior_mirror = set(gm_config.get("bank_mirror") or [])
            orphan_candidates = (prior_mirror & existing_docs) - set(bank_files.keys())
            # Garde-fou volatil : ne jamais évincer un volatil déjà présent dans
            # GM — comparaison par basename pour couvrir les noms préfixés.
            orphan_docs = {
                d
                for d in orphan_candidates
                if os.path.basename(d) not in volatile_basenames
            }
            # P7-8 : le nettoyage aussi est keyé par ``document_id``. Un orphelin
            # du ledger sans id mirror résolvable n'est PAS supprimé
            # (fail-closed) ; il est CONSERVÉ dans le ledger réécrit (voir le
            # tail) pour rester candidat au prochain push — jamais de delete
            # sans preuve positive d'identité, jamais d'oubli silencieux.
            unresolved_orphans: set[str] = set()
            for orphan in sorted(orphan_docs):
                resolved_ids = mirror_ids_by_filename.get(orphan, [])
                if not resolved_ids:
                    logger.warning(
                        "Orphelin bank-mirror '%s' sans copie mirror "
                        "identifiable (id + source_path nul) — nettoyage "
                        "SAUTÉ (fail-closed), conservé au ledger",
                        orphan,
                    )
                    unresolved_orphans.add(orphan)
                    continue
                for doc_id in resolved_ids:
                    calls.append(
                        (
                            "document_delete",
                            {
                                "memory_id": memory_id,
                                "document_id": doc_id,
                            },
                        )
                    )
                    call_metadata.append(("clean", orphan))

            # 4. Exécuter tout le batch dans une seule session
            results = await gm.call_tools_batch(calls)

            # 5. Analyser les résultats
            pushed = 0
            pushed_files: list[str] = []
            deleted_before_reingest = 0
            cleaned = 0
            errors = 0
            error_details = []

            for (action, filename), result in zip(call_metadata, results):
                if result.get("status") == "error":
                    if action == "ingest":
                        errors += 1
                        error_details.append(
                            {
                                "filename": filename,
                                "error": result.get("message", ""),
                            }
                        )
                        logger.error(
                            "Échec %s '%s' : %s",
                            action,
                            filename,
                            result.get("message", ""),
                        )
                    else:
                        logger.warning(
                            "Échec %s '%s' : %s",
                            action,
                            filename,
                            result.get("message", ""),
                        )
                else:
                    if action == "ingest":
                        pushed += 1
                        pushed_files.append(filename)
                        logger.info("Ingéré '%s'", filename)
                    elif action == "delete":
                        deleted_before_reingest += 1
                    elif action == "clean":
                        cleaned += 1
                        logger.info("Nettoyé orphelin '%s'", filename)

        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur lors du push : {e}",
            }

        duration = round(time.monotonic() - t0, 1)

        # 6. Mettre à jour _meta.json avec les métriques
        now = datetime.now(timezone.utc).isoformat()
        gm_config["last_push"] = now
        gm_config["push_count"] = gm_config.get("push_count", 0) + 1
        gm_config["files_pushed"] = pushed

        # P4-5 (ADR-0017, refines ADR-0010) — watermark dérivé READ-ONLY :
        # enregistre les coords du commit mid consommé, à PLAT dans le bloc
        # local-only graph_memory (hérite de la localité via SHARED_META_FIELDS,
        # cf. test_meta_shared_local.py). Strictement downstream : jamais lu par
        # le chemin de commit, jamais fabriqué, monotone-ou-absent.
        bv, cid, term = await self._read_committed_coords(space_id)
        prev_bv = gm_config.get("bank_version")
        if bv is None:
            # Coords absentes (cas courant : #8 production de commits non landé).
            if isinstance(prev_bv, int):
                # Préserve les coords réelles antérieures ; ne régresse pas.
                gm_config["provenance"] = "not available"
            else:
                gm_config["bank_version"] = None
                gm_config["commit_id"] = None
                gm_config["term"] = None
                gm_config["provenance"] = "not available"
            gm_config["flagged"] = False
        elif isinstance(prev_bv, int) and bv < prev_bv:
            # Régression stricte (rollback / split-brain) : FLAG, ne clobber pas
            # le high-water mark déjà enregistré.
            gm_config["flagged"] = True
            gm_config["provenance"] = "mid-consolidation"
        else:
            # Avance ou égalité : enregistre les coords consommées, efface le flag.
            gm_config["bank_version"] = bv
            gm_config["commit_id"] = cid
            gm_config["term"] = term
            gm_config["provenance"] = "mid-consolidation"
            gm_config["flagged"] = False

        # Horodatage de la dernière projection (local, downstream-only).
        gm_config["recorded_at"] = now

        # Ledger bank-mirror (P4-8) : set des relpaths réellement mirrorés cette
        # fois (post-filtre volatil). Local-only dans le bloc graph_memory
        # (hérite de la localité via SHARED_META_FIELDS — jamais committé).
        # Sert de gate primaire au nettoyage d'orphelins du push SUIVANT.
        # P7-8 : les orphelins dont le nettoyage a été SAUTÉ faute d'id mirror
        # résolvable RESTENT au ledger — ils redeviennent candidats au prochain
        # push au lieu de sortir silencieusement du périmètre de nettoyage.
        gm_config["bank_mirror"] = sorted(set(bank_files.keys()) | unresolved_orphans)

        # P7-3 : re-persistance SENTINELISÉE — pour un binding embedded, le token
        # vivant (dé-sentinelisé dans ``config``) n'est JAMAIS réécrit sur disque
        # (donc jamais copié tel quel dans un backup brut de _meta.json). No-op
        # pour un binding explicite (token opérateur conservé).
        meta_data["graph_memory"] = self._sentinelize_for_persist(gm_config)
        await storage.put_json(f"{space_id}/_meta.json", meta_data)

        result = {
            "status": "ok",
            "space_id": space_id,
            "memory_id": memory_id,
            "pushed": pushed,
            "pushed_files": pushed_files,
            "skipped_volatile": skipped_volatile,
            "deleted_before_reingest": deleted_before_reingest,
            "cleaned_orphans": cleaned,
            "errors": errors,
            "duration_seconds": duration,
        }

        if error_details:
            result["error_details"] = error_details

        logger.info(
            "Push terminé '%s' → '%s' : %d poussés, %d nettoyés, %d erreurs (%.1fs)",
            space_id,
            memory_id,
            pushed,
            cleaned,
            errors,
            duration,
        )

        return result

    # ─────────────────────────────────────────────────────────
    # STATUS — Vérifier la connexion et les stats
    # ─────────────────────────────────────────────────────────

    async def status(self, space_id: str, *, include_graph: bool = False) -> dict:
        """
        Vérifie le statut de la connexion graph-memory d'un space.

        Récupère :
        - Statistiques de la mémoire (documents, entités, relations)
        - État sûr de la collection d'embeddings
        - Liste des documents ingérés avec leurs métadonnées
        - Top entités du graphe de connaissances
        - Vue graphe plafonnée et assainie quand ``include_graph`` est vrai
        - Historique des pushs

        Args:
            space_id: Identifiant du space live-memory

        Returns:
            {"status": "ok", "connected": bool, "graph_stats": {...},
             "graph_documents": [...], "top_entities": [...], ...}
        """
        storage = get_storage()

        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        gm_config = meta_data.get("graph_memory")
        if not gm_config:
            # P7-3 : non lié → rapport READ-ONLY (aucun réseau, aucune écriture,
            # aucune génération de secret). Si le runtime embarqué est configuré,
            # on signale la liaison automatique au premier long_push.
            if get_settings().long_embedded_url:
                return {
                    "status": "ok",
                    "space_id": space_id,
                    "connected": False,
                    "bound": False,
                    "embedded": True,
                    "message": (
                        "Runtime long embarqué configuré ; liaison automatique "
                        "au premier long_push (aucun graph_connect requis)."
                    ),
                    "long_authority": _LONG_AUTHORITY_MARKER,
                }
            return {
                "status": "ok",
                "space_id": space_id,
                "connected": False,
                "message": "Aucune connexion Graph Memory configurée",
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        # P7-3 : lié → résout via le seam unique (dé-sentinelise un token embedded
        # + garde SSRF). provision=False → JAMAIS d'écriture ni de génération.
        config, gm_config, err = await self._resolve_or_embedded(
            space_id, provision=False
        )
        if err is not None:
            return err

        # G4 (P8-3): additive read-only visibility for bound spaces.  This is
        # the persisted classification (plus the legacy sentinel fallback),
        # never an inference from the URL and never a new source of truth.
        binding = _binding_view(gm_config)

        # Tester la connexion et récupérer les stats + documents
        try:
            gm = self._make_client(config.url, config.token)

            calls = [
                ("memory_stats", {"memory_id": config.memory_id}),
                ("document_list", {"memory_id": config.memory_id}),
            ]
            if include_graph:
                calls.append(
                    (
                        "memory_graph",
                        {"memory_id": config.memory_id, "format": "full"},
                    )
                )
            results = await gm.call_tools_batch(calls)

            stats = results[0]
            doc_list = results[1]

            graph_stats = None
            top_entities = []
            embedding_collection = _invalid_embedding_collection_view()
            if stats.get("status") == "ok":
                graph_stats = {
                    "document_count": stats.get("document_count", 0),
                    "entity_count": stats.get("entity_count", 0),
                    "relation_count": stats.get("relation_count", 0),
                }
                top_entities = stats.get("top_entities", [])
                embedding_collection = _embedding_collection_view(
                    stats.get("embedding_collection")
                )

            graph_documents = []
            if doc_list.get("status") == "ok":
                for doc in doc_list.get("documents", []):
                    graph_documents.append(
                        {
                            "filename": doc.get("filename", "?"),
                            "entity_count": doc.get("entity_count", 0),
                            "ingested_at": doc.get("ingested_at", ""),
                            "size": doc.get("size_bytes", doc.get("size", 0)),
                        }
                    )

            graph_view = _graph_view_payload(results[2]) if include_graph else None

        except ConnectionError as e:
            return {
                "status": "ok",
                "space_id": space_id,
                "connected": True,
                "reachable": False,
                "binding": binding,
                "config": {
                    "url": config.url,
                    "memory_id": config.memory_id,
                    "ontology": config.ontology,
                },
                "last_push": config.last_push,
                "push_count": config.push_count,
                "files_pushed": config.files_pushed,
                "long_authority": _LONG_AUTHORITY_MARKER,
                "watermark": _watermark_view(gm_config),
                "error": str(e),
            }
        except Exception as e:
            return {
                "status": "ok",
                "space_id": space_id,
                "connected": True,
                "reachable": False,
                "binding": binding,
                "config": {
                    "url": config.url,
                    "memory_id": config.memory_id,
                    "ontology": config.ontology,
                },
                "long_authority": _LONG_AUTHORITY_MARKER,
                "watermark": _watermark_view(gm_config),
                "error": str(e),
            }

        return {
            "status": "ok",
            "space_id": space_id,
            "connected": True,
            "reachable": True,
            "binding": binding,
            "config": {
                "url": config.url,
                "memory_id": config.memory_id,
                "ontology": config.ontology,
            },
            "last_push": config.last_push,
            "push_count": config.push_count,
            "files_pushed": config.files_pushed,
            "graph_stats": graph_stats,
            "embedding_collection": embedding_collection,
            "graph_documents": graph_documents,
            "top_entities": top_entities,
            **({"graph_view": graph_view} if include_graph else {}),
            "long_authority": _LONG_AUTHORITY_MARKER,
            "watermark": _watermark_view(gm_config),
        }

    # ─────────────────────────────────────────────────────────
    # DISCONNECT — Retirer la connexion graph-memory
    # ─────────────────────────────────────────────────────────

    async def disconnect(
        self, space_id: str, *, use_embedded: bool = False
    ) -> dict:
        """
        Déconnecte un space de Graph Memory.

        Par défaut, retire la configuration graph_memory de _meta.json.
        Avec ``use_embedded=True``, remplace un override explicite par le
        runtime long embarqué APRÈS avoir prouvé que celui-ci est disponible.
        Dans les deux cas, ne supprime PAS les données dans graph-memory.

        Args:
            space_id: Identifiant du space live-memory

        Returns:
            {"status": "disconnected", ...} ou {"status": "connected", ...}
        """
        await assert_space_not_reserved(space_id)
        storage = get_storage()

        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        if use_embedded:
            return await self._replace_with_embedded(
                space_id=space_id,
                meta_data=meta_data,
                previous_config=meta_data.get("graph_memory"),
            )

        if "graph_memory" not in meta_data or meta_data["graph_memory"] is None:
            return {
                "status": "ok",
                "message": (f"Espace '{space_id}' n'est pas connecté à Graph Memory"),
            }

        old_config = meta_data["graph_memory"]
        meta_data["graph_memory"] = None
        await storage.put_json(f"{space_id}/_meta.json", meta_data)

        logger.info(
            "Space '%s' déconnecté de Graph Memory '%s'",
            space_id,
            old_config.get("memory_id", ""),
        )

        return {
            "status": "disconnected",
            "space_id": space_id,
            "was_connected_to": {
                "url": old_config.get("url", ""),
                "memory_id": old_config.get("memory_id", ""),
                "push_count": old_config.get("push_count", 0),
            },
        }

    async def _replace_with_embedded(
        self,
        *,
        space_id: str,
        meta_data: dict,
        previous_config: object,
    ) -> dict:
        """Replace one space-local long override with the embedded binding.

        The previous block is retained until the embedded runtime has passed
        token registration, SSRF validation, health, and memory provisioning.
        No graph-side data is deleted or ingested.  The final write stores only
        the embedded token sentinel and preserves unrelated ``_meta.json``
        fields from the freshest read.
        """
        settings = get_settings()
        config, embedded_block, err = await self._provision_embedded(
            space_id, meta_data, settings
        )
        if err is not None:
            return {
                **err,
                "space_id": space_id,
                "previous_binding_preserved": previous_config is not None,
            }

        if config is None or embedded_block is None:  # defensive fail-closed
            return {
                "status": "error",
                "space_id": space_id,
                "message": "Provisioning du runtime long embarqué incomplet.",
                "previous_binding_preserved": previous_config is not None,
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        storage = get_storage()
        latest_meta = await storage.get_json(f"{space_id}/_meta.json")
        if latest_meta is None:
            return {
                "status": "not_found",
                "space_id": space_id,
                "message": f"Espace '{space_id}' introuvable après provisioning",
            }

        # The remote checks above can be slow.  Refuse to overwrite a binding
        # changed by another operator while they were running.  This is a
        # best-effort drift guard over the existing S3 metadata contract; the
        # storage layer has no compare-and-swap primitive.
        if latest_meta.get("graph_memory") != previous_config:
            return {
                "status": "error",
                "space_id": space_id,
                "message": (
                    "La configuration long a changé pendant le provisioning ; "
                    "bascule locale refusée, relancez après vérification."
                ),
                "long_authority": _LONG_AUTHORITY_MARKER,
            }

        persisted_block = self._sentinelize_for_persist(embedded_block)
        latest_meta["graph_memory"] = persisted_block
        await storage.put_json(f"{space_id}/_meta.json", latest_meta)

        previous_view: dict | None = None
        if isinstance(previous_config, dict):
            previous_view = {
                "binding": _binding_view(previous_config),
                "url": previous_config.get("url", ""),
                "memory_id": previous_config.get("memory_id", ""),
                "push_count": previous_config.get("push_count", 0),
            }
        elif previous_config is not None:
            previous_view = {"binding": "invalid"}

        logger.info(
            "Space '%s' basculé vers le runtime long embarqué '%s'",
            space_id,
            config.memory_id,
        )

        return {
            "status": "connected",
            "space_id": space_id,
            "binding": _BINDING_EMBEDDED,
            "changed": True,
            "graph_memory": {
                "url": config.url,
                "memory_id": config.memory_id,
                "ontology": config.ontology,
            },
            "previous_graph_memory": previous_view,
            "note": (
                "L'ancienne configuration locale a été remplacée ; aucune "
                "donnée Graph Memory distante n'a été supprimée et aucun "
                "document n'a été ingéré."
            ),
            "long_authority": _LONG_AUTHORITY_MARKER,
        }

    # ─────────────────────────────────────────────────────────
    # P4-4 — Méthodes typées de projection Graph Memory
    # ─────────────────────────────────────────────────────────
    #
    # Chaque méthode : charge la config locale → applique la garde SSRF AVANT
    # toute construction de client (_guard_url before _make_client) → appelle
    # l'outil Graph Memory → retourne le dict GM TEL QUEL (aucun reshape).
    #
    # Ce sont des projections pures côté GM (downstream-only, ADR-0010) : elles
    # ne lisent/écrivent jamais commit_id/bank_version/term, n'appellent jamais
    # assert_commit_allowed, ne touchent jamais _hivemind/.

    async def list_ontologies(self, space_id: str) -> dict:
        """Liste les ontologies disponibles dans Graph Memory.

        Outil GM : ``ontology_list`` — AUCUN argument (le schéma ne prend pas
        de ``memory_id`` ; en passer un ferait échouer l'appel côté GM).
        """
        config, err = await self._load_gm_config(space_id)
        if err is not None:
            return err

        guard = self._guard_url(config.url)
        if guard is not None:
            return guard

        try:
            gm = self._make_client(config.url, config.token)
            return await gm.call_tool("ontology_list", {})
        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur ontology_list : {e}",
            }

    async def query(self, space_id: str, query: str, limit: int = 10) -> dict:
        """Interroge le graphe (recherche structurée, SANS LLM).

        Outil GM : ``memory_query`` — args ``{memory_id, query, limit}``.
        Volontairement sans LLM (chemin déterministe, agent-friendly).
        """
        config, err = await self._load_gm_config(space_id)
        if err is not None:
            return err

        guard = self._guard_url(config.url)
        if guard is not None:
            return guard

        try:
            gm = self._make_client(config.url, config.token)
            return await gm.call_tool(
                "memory_query",
                {
                    "memory_id": config.memory_id,
                    "query": query,
                    "limit": limit,
                },
            )
        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur memory_query : {e}",
            }

    async def search(self, space_id: str, query: str, limit: int = 10) -> dict:
        """Recherche graph-first dans le graphe de connaissances.

        Outil GM : ``memory_search`` — args ``{memory_id, query, limit}``.
        """
        config, err = await self._load_gm_config(space_id)
        if err is not None:
            return err

        guard = self._guard_url(config.url)
        if guard is not None:
            return guard

        try:
            gm = self._make_client(config.url, config.token)
            return await gm.call_tool(
                "memory_search",
                {
                    "memory_id": config.memory_id,
                    "query": query,
                    "limit": limit,
                },
            )
        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur memory_search : {e}",
            }

    async def ingest(
        self,
        space_id: str,
        *,
        filename: str,
        content: Optional[str] = None,
        content_base64: Optional[str] = None,
        source_path: Optional[str] = None,
        source_modified_at: Optional[str] = None,
        metadata: Optional[dict] = None,
        force: bool = False,
    ) -> dict:
        """Ingère UN document dans Graph Memory (projection GM-side).

        XOR strict : fournir exactement un de ``content`` ou ``content_base64``.
        ``content`` est encodé en base64 comme dans push (extraction LLM côté
        GM → ``timeout=180.0``). Les args optionnels (``source_path`` /
        ``source_modified_at`` / ``metadata``) ne sont ajoutés que s'ils sont
        non-``None``.

        Outil GM : ``memory_ingest``. Projection pure : ne lit jamais
        ``source_path`` depuis le disque, n'implémente pas de dry-run (ces
        responsabilités sont au tool layer P4-7).
        """
        if (content is None) == (content_base64 is None):
            return {
                "status": "error",
                "message": "Fournir exactement un de content ou content_base64",
            }

        config, err = await self._load_gm_config(space_id)
        if err is not None:
            return err

        guard = self._guard_url(config.url)
        if guard is not None:
            return guard

        if content_base64 is None:
            content_bytes = content.encode("utf-8")
            content_base64 = base64.b64encode(content_bytes).decode("ascii")

        arguments: dict = {
            "memory_id": config.memory_id,
            "content_base64": content_base64,
            "filename": filename,
            "force": force,
        }
        if source_path is not None:
            arguments["source_path"] = source_path
        if source_modified_at is not None:
            arguments["source_modified_at"] = source_modified_at
        if metadata is not None:
            arguments["metadata"] = metadata

        try:
            gm = self._make_client(config.url, config.token, timeout=180.0)
            return await gm.call_tool("memory_ingest", arguments)
        except ConnectionError as e:
            return {
                "status": "error",
                "message": f"Connexion impossible à Graph Memory : {e}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur memory_ingest : {e}",
            }

    async def reindex(self, space_id: str) -> dict:
        """Run bounded embedding maintenance on a persisted embedded binding.

        This method never provisions a binding. Custom ``graph_connect``
        endpoints are unsupported because Hivemind cannot prove their
        single-runtime maintenance boundary. For a valid embedded binding it
        constructs one client and issues exactly one non-idempotent
        ``memory_reindex`` call with the persisted target memory identifier.
        """
        try:
            return await self._reindex_embedded(space_id)
        except (ConnectionError, TimeoutError):
            return _reindex_error("runtime_unavailable")
        except Exception:
            # This wrapper covers storage, binding resolution, client creation,
            # and dispatch. Never reflect or log an exception's raw text.
            logger.error("Embedded long-memory reindex failed")
            return _reindex_error("reindex_failed")

    async def _reindex_embedded(self, space_id: str) -> dict:
        """Execute the post-auth embedded-only path under fixed envelopes."""
        storage = get_storage()
        meta_data = await storage.get_json(f"{space_id}/_meta.json")
        if meta_data is None:
            return _reindex_error("space_not_found")

        persisted = meta_data.get("graph_memory")
        if (
            not isinstance(persisted, dict)
            or persisted.get("binding") != _BINDING_EMBEDDED
            or persisted.get("token") != EMBEDDED_TOKEN_SENTINEL
        ):
            return _reindex_error("unsupported_runtime")

        # Resolve the live embedded token only after the persisted classification
        # has passed. ``provision=False`` forbids auto-binding or secret creation.
        config, resolved, err = await self._resolve_or_embedded(
            space_id, provision=False
        )
        if err is not None:
            return _reindex_error("binding_unavailable")
        if (
            config is None
            or not isinstance(resolved, dict)
            or resolved.get("binding") != _BINDING_EMBEDDED
            or resolved.get("token") != EMBEDDED_TOKEN_SENTINEL
            or config.memory_id != derive_memory_id(space_id)
        ):
            # A concurrent binding replacement between the two reads cannot
            # redirect this maintenance operation to an explicit endpoint.
            return _reindex_error("unsupported_runtime")

        gm = self._make_client(
            config.url,
            config.token,
            timeout=_REINDEX_CLIENT_TIMEOUT_SECONDS,
        )
        try:
            raw_result = await gm.call_tool(
                "memory_reindex", {"memory_id": config.memory_id}
            )
        except (ConnectionError, TimeoutError):
            return _reindex_uncertain("runtime_unavailable")
        except Exception:
            logger.error("Embedded long-memory reindex dispatch failed")
            return _reindex_uncertain("reindex_failed")
        return _reindex_result_view(raw_result)

    # ─────────────────────────────────────────────────────────
    # P4-7 — Planification d'ingestion canonique (PLAN-ONLY, downstream-only)
    # ─────────────────────────────────────────────────────────
    #
    # plan_ingest PLANIFIE l'ingestion canonique d'un SET de documents, keyés
    # par un ``source_path`` stable (PAS le nom de fichier bank mutable). Le
    # serveur n'est PAS un proxy aveugle (EVOLUTION C-Q2.a) : l'ENGINE planifie.
    # Trois modes, tous downstream-only (ADR-0010) — JAMAIS sur le chemin
    # commit/rollback/audit/recovery, AUCUN import du sous-paquet hivemind :
    #
    # - ``dry-run``     : renvoie le plan ``{source_path, sha256}`` SANS aucun
    #                     contact GM (pas de _load_gm_config / _guard_url /
    #                     _make_client — ZÉRO transport).
    # - ``check-remote``: plan SKIP/UPDATE/INGEST par comparaison du sha256 de
    #                     chaque doc au remote (UN seul ``document_list``
    #                     read-only, AUCUNE écriture).
    # - ``apply``       : DÉFÉRÉ en v1 (D13 / EVOLUTION Vague C : apply est
    #                     codex-gated, v2.7.0+) — ``applied: false`` + raison
    #                     explicite, AUCUNE écriture aveugle.
    #
    # La garde volatile, la permission 'manage' et l'audit vivent UNIQUEMENT au
    # tool layer (ADR-0010) ; ``include_volatile`` est accepté ici puis ignoré
    # pour garder l'engine en pass-through pur.

    async def plan_ingest(
        self,
        space_id: str,
        documents: list[dict],
        *,
        mode: str = "dry-run",
        include_volatile: bool = False,
    ) -> dict:
        """Planifie l'ingestion canonique d'un set de documents (PLAN-ONLY).

        Voir le bloc P4-7 ci-dessus pour le contrat des trois modes. Le sha256
        est calculé INLINE (``hashlib.sha256(content.encode("utf-8")).hexdigest()``,
        hex nu, sans préfixe) quand ``content`` est fourni, sur les octets décodés
        quand ``content_base64`` est fourni, ou échoé verbatim quand le caller
        fournit déjà ``sha256``. Le ``source_path`` est la clé canonique stable.
        """

        def _plan_pair(doc: dict) -> dict:
            source_path = doc.get("source_path")
            sha256 = doc.get("sha256")
            if sha256 is None:
                content = doc.get("content")
                if content is not None:
                    raw = content.encode("utf-8")
                else:
                    raw = base64.b64decode(doc.get("content_base64") or "")
                sha256 = hashlib.sha256(raw).hexdigest()
            return {"source_path": source_path, "sha256": sha256}

        if mode == "dry-run":
            # ZÉRO transport : on ne charge AUCUNE config, on ne construit AUCUN
            # client. Le plan est purement local (source_path + sha256).
            return {
                "status": "ok",
                "space_id": space_id,
                "mode": "dry-run",
                "planned": [_plan_pair(d) for d in documents],
                "applied": False,
            }

        if mode == "apply":
            # DÉFÉRÉ en v1 : pas d'écriture aveugle, AUCUN transport construit.
            return {
                "status": "ok",
                "space_id": space_id,
                "mode": "apply",
                "applied": False,
                "reason": (
                    "apply deferred — plan-only in this release (D13 / EVOLUTION "
                    "Vague C; codex-gated, v2.7.0+). Use mode='dry-run' or "
                    "mode='check-remote' to plan; apply lands in v2.7.0+."
                ),
            }

        if mode == "check-remote":
            config, err = await self._load_gm_config(space_id)
            if err is not None:
                return err

            guard = self._guard_url(config.url)
            if guard is not None:
                return guard

            try:
                gm = self._make_client(config.url, config.token)
                # UN seul read read-only : document_list.
                doc_list = await gm.call_tool(
                    "document_list", {"memory_id": config.memory_id}
                )
            except ConnectionError as e:
                return {
                    "status": "error",
                    "message": f"Connexion impossible à Graph Memory : {e}",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Erreur document_list : {e}",
                }

            # Map remote keyée par source_path (jamais par le nom de fichier
            # bank). Seules les entrées qui portent un source_path comptent ;
            # absent → INGEST (fail-open-to-INGEST, jamais une écriture).
            # NOTE (P7-8) : le ``document_list`` réel de Graph Memory EXPOSE
            # ``source_path`` (clé toujours présente, ``None`` si absente —
            # cf. core/graph.py, contrat homogène avec document_get). Un doc
            # mirror (source_path nul) reste donc hors de ``remote`` → classé
            # INGEST (conservateur, ZÉRO écriture) ; les docs canoniques sont
            # comparés par sha256 pour SKIP/UPDATE.
            remote: dict = {}
            if isinstance(doc_list, dict) and doc_list.get("status") == "ok":
                for rdoc in doc_list.get("documents", []):
                    sp = rdoc.get("source_path")
                    if sp is not None:
                        remote[sp] = rdoc.get("sha256")

            plan: list[dict] = []
            for doc in documents:
                pair = _plan_pair(doc)
                sp = pair["source_path"]
                if sp not in remote:
                    action = "INGEST"
                elif remote[sp] == pair["sha256"]:
                    action = "SKIP"
                else:
                    action = "UPDATE"
                plan.append(
                    {"source_path": sp, "sha256": pair["sha256"], "action": action}
                )

            return {
                "status": "ok",
                "space_id": space_id,
                "mode": "check-remote",
                "plan": plan,
            }

        return {
            "status": "error",
            "message": "mode must be one of dry-run|check-remote|apply",
        }


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_graph_bridge: GraphBridgeService | None = None


def get_graph_bridge() -> GraphBridgeService:
    """Retourne le singleton GraphBridgeService."""
    global _graph_bridge
    if _graph_bridge is None:
        _graph_bridge = GraphBridgeService()
    return _graph_bridge
