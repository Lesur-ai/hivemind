# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Graph (7 outils).

Pont entre Live Memory et Graph Memory : connecter un space à une
instance de graphe de connaissances et y pousser la memory bank.

Permissions :
    - graph_connect     ✏️ (write) — Connecte un space à Graph Memory
    - graph_push        ✏️ (write) — Pousse la bank dans Graph Memory
    - graph_status      🔑 (read)  — Statut de la connexion + stats graphe
    - graph_disconnect  ✏️ (write) — Déconnecte le space de Graph Memory ; le
                                     mode use_embedded nécessite manage
    - long_query        🔑 (read)  — Interroge le graphe (P4-7, net-new long_*)
    - long_ingest       ✏️ (plan)  — Planifie l'ingestion canonique (P4-7,
                                     net-new long_*, source_path-keyed, PLAN-ONLY)
    - long_reindex      🛠️ (manage) — Reconstruit explicitement la projection
                                      d'embeddings du runtime embarqué

P4-7 : ``long_query`` / ``long_ingest`` sont DEUX outils long_* net-new,
enregistrés DIRECTEMENT par ``register()`` (PAS via ALIAS_MAP — ils n'ont aucun
jumeau ``graph_*``). ``long_ingest`` est l'ingestion canonique distincte du
mirror bank keyé par nom de fichier de ``graph_push`` : les documents sont keyés
par un ``source_path`` stable (PLAN-ONLY en v1, apply déféré ; ADR-0010).

Le push utilise une synchronisation intelligente :
    - Les fichiers existants sont supprimés puis ré-ingérés (recalcul du graphe)
    - Les fichiers disparus de la bank sont nettoyés dans le graphe
    - Les métriques de push sont tracées dans _meta.json

Voir core/graph_bridge.py pour la logique métier et le client MCP Streamable HTTP.
"""

import os
import json
import logging
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

# P4-8 : audit structuré du forçage volatil (include_volatile=True), émis sur le
# MÊME logger que l'AuditMiddleware / tokens._emit_bulk_update_audit, au tool
# layer (ADR-0010 : engine/bridge restent pass-through, sans auth ni logging).
audit_logger = logging.getLogger("live_mem.audit")

# LM2-02 fix : validation anti-SSRF du paramètre `url` de graph_connect.
# P4-4 : le validateur a été relocalisé dans core/url_guard.py (source unique)
# pour que la couche adaptateur (core/graph_bridge.py) puisse l'appliquer avant
# de construire un client, SANS qu'un module core/ importe tools/ (back-edge
# interdit, ADR-0006/0010). On le ré-exporte ici sous son nom historique
# `_validate_gm_url` : le comportement et le call-site de graph_connect sont
# inchangés (byte-for-byte), les tests SSRF existants restent verts.
from ..core.url_guard import validate_gm_url as _validate_gm_url


def _emit_volatile_optin_audit(space_id: str, volatile_files: list[str]) -> None:
    """Émet l'événement d'audit structuré ``graph_push_volatile_optin``.

    Appelé UNIQUEMENT après que la garde 'manage' a accepté un push avec
    ``include_volatile=True`` (gouvernance : tracer qui force-pousse des fichiers
    volatils dans Graph Memory, et lesquels). Mirroir exact de
    ``core/tokens.py::_emit_bulk_update_audit`` : identité du caller + request_id
    récupérés best-effort depuis les ContextVar du middleware (fallback
    ``"system"`` / ``"-"`` hors-requête HTTP), ``json.dumps(..., ensure_ascii=
    False)``, et tout échec de logging est avalé (ne casse jamais l'opération).
    """
    try:
        from ..auth.context import current_token_info
        from ..middleware import current_request_id

        tinfo = current_token_info.get()
        caller = tinfo.get("client_name", "unknown") if tinfo else "system"
        req_id = current_request_id.get()
    except Exception:
        caller = "system"
        req_id = "-"

    entry = {
        "event": "graph_push_volatile_optin",
        "request_id": req_id,
        "caller": caller,
        "space_id": space_id,
        "volatile_files": sorted(volatile_files),
    }
    try:
        audit_logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass


def _emit_long_ingest_volatile_optin_audit(
    space_id: str, volatile_source_paths: list[str]
) -> None:
    """Émet l'événement d'audit structuré ``long_ingest_volatile_optin`` (P4-7).

    Miroir exact de :func:`_emit_volatile_optin_audit` pour le chemin
    ``long_ingest`` : appelé UNIQUEMENT après que la garde 'manage' a accepté un
    opt-in ``include_volatile=True`` (gouvernance : tracer qui force-admet des
    fichiers volatils dans l'ingestion canonique long-tier, et lesquels). NB :
    événement et champ DISTINCTS de ``graph_push_volatile_optin`` /
    ``volatile_files`` (le chemin graph_push) — ici l'événement est
    ``long_ingest_volatile_optin`` et le champ est ``volatile_source_paths``
    (on trace des ``source_path`` canoniques, pas des noms de fichiers bank).
    Tout échec de logging est avalé (ne casse jamais l'opération).
    """
    try:
        from ..auth.context import current_token_info
        from ..middleware import current_request_id

        tinfo = current_token_info.get()
        caller = tinfo.get("client_name", "unknown") if tinfo else "system"
        req_id = current_request_id.get()
    except Exception:
        caller = "system"
        req_id = "-"

    entry = {
        "event": "long_ingest_volatile_optin",
        "request_id": req_id,
        "caller": caller,
        "space_id": space_id,
        "volatile_source_paths": sorted(volatile_source_paths),
    }
    try:
        audit_logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 7 outils graph sur l'instance MCP.

    Les 4 historiques (graph_connect / graph_push / graph_status /
    graph_disconnect) + les 3 outils long_* directs (long_query / long_ingest /
    long_reindex), enregistrés DIRECTEMENT ici (PAS via ALIAS_MAP — aucun
    jumeau graph_*).

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (7)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def graph_connect(
        space_id: Annotated[
            str, Field(description="Space identifier")
        ],
        url: Annotated[
            str,
            Field(
                description="Graph Memory URL, for example 'https://graph.example.com/mcp'"
            ),
        ],
        token: Annotated[
            str, Field(description="Bearer token used to authenticate to Graph Memory")
        ],
        memory_id: Annotated[
            str, Field(description="Target memory identifier in Graph Memory")
        ],
        ontology: Annotated[
            str,
            Field(
                default="general",
                description="Extraction ontology: general|legal|cloud|managed-services|presales",
            ),
        ] = "general",
    ) -> dict:
        """
        Bind a Hivemind space to an explicit Graph Memory instance.

        Validates the connection, creates the target memory when needed, and
        stores the binding in the space.

        Manual binding is only needed for a custom remote instance. Without an
        explicit binding, the first non-empty ``long_push`` automatically binds
        the configured embedded long-memory runtime.

        Args:
            space_id: Space to bind.
            url: Graph Memory MCP endpoint.
            token: Bearer token for the remote service.
            memory_id: Target memory identifier.
            ontology: Extraction ontology.

        Returns:
            Connection status and target-memory details.
        """
        from ..auth.context import check_access, check_write_permission
        from ..core.engines import get_engine_registry

        try:
            # Vérifier accès au space + permission write
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            # LM2-02 fix : valider l'URL pour bloquer le SSRF (IP privées,
            # metadata cloud, schemes non HTTP). Doit être fait AVANT toute
            # tentative de connexion réseau ET avant la persistance S3.
            # P3-7: la validation SSRF reste DANS le tool layer (ADR-0010 :
            # LongEngine ne la réplique pas).
            url_err = _validate_gm_url(url)
            if url_err:
                return {"status": "error", "message": url_err}

            # P3-7: graph_* est downstream-derived (ADR-0010) — JAMAIS un write
            # WriteSink, donc PAS de resolve_sink gate. LongEngine ne prend pas
            # de WriteSink ; connect écrit seulement le bloc local-only
            # graph_memory de _meta.json via le bridge.
            return await get_engine_registry().long_engine().connect(
                space_id=space_id,
                url=url,
                token=token,
                memory_id=memory_id,
                ontology=ontology,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def graph_push(
        space_id: Annotated[
            str, Field(description="Space whose committed memory bank should be projected")
        ],
        include_volatile: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Also project volatile files such as activeContext.md and "
                    "progress.md. They are skipped by default. Enabling this "
                    "option requires manage permission and records an audit event."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Project the committed memory bank into the long-term knowledge graph.

        Existing mirrored files are replaced with their current content, new
        files are ingested, and stale bank-mirror entries are removed. Canonical
        documents identified by a stable ``source_path`` are never removed by
        this bank projection.

        For a non-empty bank, the tool uses an explicit Graph Memory binding
        when one exists. Otherwise it automatically provisions and stores a
        binding to the configured embedded long-memory runtime. An empty bank
        returns without provisioning a binding.

        ``activeContext.md``, ``progress.md``, and other configured volatile
        basenames are skipped by default and reported in ``skipped_volatile``.
        Set ``include_volatile=True`` only when those transient snapshots are
        intentionally required; doing so requires manage permission and records
        an audit event.

        This operation is idempotent and re-runnable. It projects already
        committed bank state and never authorizes, invalidates, or replaces a
        Hivemind commit.

        Args:
            space_id: Space whose committed bank should be projected.
            include_volatile: Whether to include configured volatile files.

        Returns:
            Projection counts, skipped volatile files, errors, and duration.
        """
        from ..auth.context import (
            check_access,
            check_manage_permission,
            check_write_permission,
        )
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            # P4-8 — Garde-fou volatil. Le forçage (include_volatile=True)
            # requiert 'manage' ET émet un audit structuré APRÈS la garde (un
            # refus n'audite jamais). Le tool layer porte la permission + l'audit
            # (ADR-0010 : engine/bridge restent pass-through).
            if include_volatile:
                manage_err = check_manage_permission()
                if manage_err:
                    return manage_err

                from ..config import get_settings

                _emit_volatile_optin_audit(
                    space_id, list(get_settings().graph_push_volatile_files)
                )

            # P3-7: downstream-derived (ADR-0010) — no resolve_sink gate.
            return await get_engine_registry().long_engine().push(
                space_id, include_volatile=include_volatile
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def graph_status(
        space_id: Annotated[str, Field(description="Space identifier")],
        include_graph: Annotated[
            bool,
            Field(
                description="Include a bounded, sanitized graph view for the admin console"
            ),
        ] = False,
    ) -> dict:
        """
        Read a space's long-memory connection and projection status.

        Tests Graph Memory connectivity and returns target-memory statistics,
        including document, entity, and relation counts, plus the latest bank
        projection metadata.

        Args:
            space_id: Space to inspect.
            include_graph: Include a bounded sanitized graph view.

        Returns:
            Binding, connectivity, graph statistics, and projection status.
        """
        from ..auth.context import check_access
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            # P3-7: read-only delegation to LongEngine.status (bridge read);
            # not a WriteSink path.
            return await get_engine_registry().long_engine().status(
                space_id, include_graph=include_graph
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def graph_disconnect(
        space_id: Annotated[
            str, Field(description="Space whose explicit Graph Memory binding should be removed")
        ],
        use_embedded: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Replace the explicit Graph Memory binding with the "
                    "validated embedded long-memory runtime. Requires manage "
                    "permission. No remote data is deleted or ingested."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Remove a space's explicit Graph Memory binding.

        With ``use_embedded=True``, the tool provisions and validates the
        embedded long-memory runtime before replacing the previous binding.
        Existing remote graph data is never deleted by this operation.

        Delete remote data separately through Graph Memory when required.

        Args:
            space_id: Space whose binding should be removed.
            use_embedded: Replace the binding with the embedded runtime.

        Returns:
            Confirmation and the previous binding details.
        """
        from ..auth.context import (
            check_access,
            check_manage_permission,
            check_write_permission,
        )
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            if use_embedded:
                manage_err = check_manage_permission()
                if manage_err:
                    return manage_err

            # P3-7: downstream-derived (ADR-0010) — no resolve_sink gate.
            # disconnect clears only the local-only graph_memory block in
            # _meta.json via the bridge.
            if use_embedded:
                return await get_engine_registry().long_engine().disconnect(
                    space_id, use_embedded=True
                )
            # Preserve the legacy default call shape for injected engines.
            return await get_engine_registry().long_engine().disconnect(space_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

    # ─────────────────────────────────────────────────────────
    # P4-7 — long_query : outil READ-ONLY thin sur LongEngine.query
    # ─────────────────────────────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def long_query(
        space_id: Annotated[
            str, Field(description="Space identifier")
        ],
        query: Annotated[
            str,
            Field(
                description=(
                    "Semantic graph query using the configured embedding endpoint; "
                    "no generative chat completion is performed"
                )
            ),
        ],
        limit: Annotated[
            int,
            Field(default=10, description="Maximum number of results to return"),
        ] = 10,
    ) -> dict:
        """
        Query a space's long-term knowledge graph (read-only).

        Embeds the query through the configured embedding endpoint, then
        searches the graph and vector projection. It performs no generative
        chat completion. Long-term memory is a derived index and is never an
        authority for Hivemind commit state.

        Args:
            space_id: Space to query.
            query: Semantic search query.
            limit: Maximum number of results.

        Returns:
            Graph and ontology query results.
        """
        from ..auth.context import check_access
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            # READ-ONLY : delegation à LongEngine.query (bridge read) ; jamais un
            # WriteSink, jamais une source de commit (ADR-0010).
            return await get_engine_registry().long_engine().query(
                space_id, query, limit=limit
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

    # ─────────────────────────────────────────────────────────
    # P4-7 — long_ingest : ingestion canonique PLAN-ONLY (source_path-keyed)
    # ─────────────────────────────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def long_ingest(
        space_id: Annotated[
            str, Field(description="Target space identifier")
        ],
        documents: Annotated[
            list[dict],
            Field(
                description=(
                    "Canonical documents keyed by a stable source_path rather "
                    "than a mutable bank filename. Each document uses "
                    "{source_path, content|content_base64, sha256?}."
                )
            ),
        ],
        mode: Annotated[
            str,
            Field(
                default="dry-run",
                description=(
                    "Planning mode: 'dry-run' computes source_path and sha256 "
                    "locally with no network access; 'check-remote' compares "
                    "against the remote index without writing; 'apply' is not "
                    "supported and returns applied=false with a reason."
                ),
            ),
        ] = "dry-run",
        include_volatile: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Allow volatile documents such as activeContext.md and "
                    "progress.md. They are rejected by default. Enabling this "
                    "option requires manage permission and records an audit event."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Plan canonical-document ingestion into long-term memory without writing.

        Canonical documents use a stable ``source_path`` and optional SHA-256,
        independently of the filename-keyed bank projection performed by
        ``long_push``.

        - ``dry-run`` computes ``{source_path, sha256}`` locally with no network
          access.
        - ``check-remote`` compares hashes through a read-only remote listing
          and plans SKIP, UPDATE, or INGEST actions.
        - ``apply`` is currently unsupported and returns ``applied: false``
          without performing a write.

        Configured volatile basenames are rejected in every mode by default.
        ``include_volatile=True`` requires manage permission and records an
        audit event only after authorization succeeds.

        Args:
            space_id: Target space.
            documents: Canonical documents keyed by ``source_path``.
            mode: ``dry-run`` (default), ``check-remote``, or ``apply``.
            include_volatile: Allow configured volatile basenames.

        Returns:
            An ingestion plan, or an explicit unsupported result for ``apply``.
        """
        from ..auth.context import (
            check_access,
            check_manage_permission,
            safe_error,
        )
        from ..config import get_settings
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            # Garde volatile (tool layer, ADR-0010) : match sur le BASENAME du
            # source_path. Rejetée par défaut sur TOUS les modes (y compris
            # dry-run) — la garde précède toute planification / tout transport.
            volatile = set(get_settings().graph_push_volatile_files)
            offending = sorted(
                {
                    d.get("source_path", "")
                    for d in documents
                    if os.path.basename(d.get("source_path", "")) in volatile
                }
            )
            if offending:
                if not include_volatile:
                    return {
                        "status": "error",
                        "message": (
                            "Volatile files (activeContext.md / progress.md) are "
                            "REJECTED from canonical long-tier ingestion by "
                            "default. These files are continuously rewritten; "
                            "use include_volatile=True (the 'manage' permission "
                            "is required) to force their admission."
                        ),
                        "rejected_volatile": offending,
                    }
                # Opt-in : la permission 'manage' est requise. La garde précède
                # l'audit (un refus n'audite JAMAIS).
                manage_err = check_manage_permission()
                if manage_err:
                    return manage_err

                _emit_long_ingest_volatile_optin_audit(space_id, offending)

            # Délégation PLAN-ONLY : l'engine/bridge porte le dispatch de mode et
            # la planification source_path-keyed (downstream-only, ADR-0010).
            return await get_engine_registry().long_engine().plan_ingest(
                space_id, documents, mode=mode, include_volatile=include_volatile
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def long_reindex(
        space_id: Annotated[
            str,
            Field(
                description=(
                    "Space whose embedded long-memory embedding projection "
                    "should be rebuilt in explicit maintenance mode"
                )
            ),
        ],
    ) -> dict:
        """Rebuild a space's embedded long-memory embedding projection.

        This hidden operator operation is synchronous, non-idempotent, and
        supported only for an already-persisted embedded runtime binding. It
        never provisions a binding and never operates on a custom remote
        binding.

        Args:
            space_id: Space whose derived embedding projection should be rebuilt.

        Returns:
            The bounded maintenance result returned by the embedded runtime.
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.engines import get_engine_registry

        # Authorization is deliberately complete before engine lookup. The
        # bridge lookup resolves persisted binding state and may construct a
        # network client, so neither may happen for an unauthorized caller.
        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "graph")

        try:
            return await get_engine_registry().long_engine().reindex(space_id)
        except Exception:
            from ..core.graph_bridge import _reindex_error

            return _reindex_error("reindex_failed")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def ontology_list(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
    ) -> dict:
        """List ontologies available in Graph Memory for a space.

        Args:
            space_id: Target space identifier.

        Returns:
            Dictionary containing the list of available ontologies.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err
            return await get_engine_registry().long_engine().list_ontologies(space_id)
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def ontology_get(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        name: Annotated[
            str,
            Field(description="Ontology name"),
        ],
    ) -> dict:
        """Get an ontology definition by name.

        Args:
            space_id: Target space identifier.
            name: Name of the ontology to retrieve.

        Returns:
            Dictionary containing the ontology definition and YAML content.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err
            return await get_engine_registry().long_engine().get_ontology(space_id, name)
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def ontology_validate(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        content_yaml: Annotated[
            str,
            Field(description="Ontology YAML content to validate"),
        ],
    ) -> dict:
        """Validate an ontology YAML definition without mutating state.

        Args:
            space_id: Target space identifier.
            content_yaml: Raw YAML string of the ontology definition.

        Returns:
            Validation result with status, validity flag and errors if any.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            MAX_ONTOLOGY_BYTES = 512 * 1024
            if not isinstance(content_yaml, str):
                return {
                    "status": "ok",
                    "valid": False,
                    "name": None,
                    "version": None,
                    "sha256": None,
                    "entity_types_count": 0,
                    "relation_types_count": 0,
                    "yaml_content": None,
                    "errors": ["content_yaml must be a string"],
                    "warnings": [],
                }

            try:
                encoded = content_yaml.encode("utf-8")
            except (UnicodeError, UnicodeEncodeError) as e:
                return {
                    "status": "ok",
                    "valid": False,
                    "name": None,
                    "version": None,
                    "sha256": None,
                    "entity_types_count": 0,
                    "relation_types_count": 0,
                    "yaml_content": None,
                    "errors": [f"Invalid Unicode encoding: {e}"],
                    "warnings": [],
                }

            if len(encoded) > MAX_ONTOLOGY_BYTES:
                return {
                    "status": "ok",
                    "valid": False,
                    "name": None,
                    "version": None,
                    "sha256": None,
                    "entity_types_count": 0,
                    "relation_types_count": 0,
                    "yaml_content": None,
                    "errors": [f"Ontology content exceeds maximum allowed size ({MAX_ONTOLOGY_BYTES} bytes)"],
                    "warnings": [],
                }

            return await get_engine_registry().long_engine().validate_ontology(space_id, content_yaml)
        except Exception as e:
            return safe_error(e, "graph")


    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def long_ingest_async(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        documents: Annotated[
            list[dict],
            Field(description="Batch of documents to ingest asynchronously"),
        ],
        options: Annotated[
            Optional[dict],
            Field(description="Optional ingestion options (e.g. replace_existing)"),
        ] = None,
        include_volatile: Annotated[
            bool,
            Field(
                description=(
                    "Allow volatile documents such as activeContext.md and "
                    "progress.md. They are rejected by default. Enabling this "
                    "option requires manage permission and records an audit event."
                ),
            ),
        ] = False,
    ) -> dict:
        """Queue a batch of documents for asynchronous ingestion into long-term memory.

        Args:
            space_id: Target space identifier.
            documents: Each dict requires filename, a stable source_path,
                content_base64, and a hexadecimal sha256 of the decoded bytes.
                Optional fields are metadata and source_modified_at (ISO 8601).
                This async path does not convert plain content and
                does not compute a missing checksum for the caller.
            options: Ingestion options (e.g. replace_existing: bool).
            include_volatile: Allow configured volatile basenames.

        Returns:
            Batch submission result with batch_id and list of queued job details.
        """
        from ..auth.context import (
            check_access,
            check_manage_permission,
            check_write_permission,
            safe_error,
        )
        from ..config import get_settings
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            if not isinstance(documents, list) or not documents:
                return {
                    "status": "error",
                    "message": "documents must be a non-empty list",
                }

            volatile = set(get_settings().graph_push_volatile_files)
            offending = sorted(
                {
                    d.get("source_path", "")
                    for d in documents
                    if isinstance(d, dict) and os.path.basename(d.get("source_path", "")) in volatile
                }
            )
            if offending:
                if not include_volatile:
                    return {
                        "status": "error",
                        "message": (
                            "Volatile files (activeContext.md / progress.md) are "
                            "REJECTED from canonical long-tier ingestion by "
                            "default. Use include_volatile=True (the 'manage' permission "
                            "is required) to force their admission."
                        ),
                        "rejected_volatile": offending,
                    }
                manage_err = check_manage_permission()
                if manage_err:
                    return manage_err

                _emit_long_ingest_volatile_optin_audit(space_id, offending)

            return await get_engine_registry().long_engine().ingest_async(
                space_id=space_id,
                documents=documents,
                options=options,
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def long_ingest_status(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        job_id: Annotated[
            str,
            Field(description="Ingestion job identifier"),
        ],
    ) -> dict:
        """Return the status and progress of an asynchronous ingestion job.

        Args:
            space_id: Target space identifier.
            job_id: Job identifier returned by long_ingest_async.

        Returns:
            Job status dictionary with progress, step, entity/relation counts and error if any.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            if not isinstance(job_id, str) or not job_id.strip():
                return {
                    "status": "error",
                    "message": "job_id must be a non-empty string",
                }

            return await get_engine_registry().long_engine().ingest_status(
                space_id=space_id,
                job_id=job_id.strip(),
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def long_ingest_list(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        batch_id: Annotated[
            Optional[str],
            Field(description="Optional batch ID filter"),
        ] = None,
        status: Annotated[
            Optional[str],
            Field(description="Optional job status filter (e.g. queued, running, succeeded, failed)"),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Maximum number of jobs to return (1-100)"),
        ] = 50,
        offset: Annotated[
            int,
            Field(description="Number of jobs to skip"),
        ] = 0,
    ) -> dict:
        """List asynchronous ingestion jobs for a space.

        Args:
            space_id: Target space identifier.
            batch_id: Optional filter by batch ID.
            status: Optional filter by status.
            limit: Maximum items to return.
            offset: Items offset.

        Returns:
            List of job status dictionaries and pagination metadata.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            if type(limit) is not int or limit < 1 or limit > 100:
                return {
                    "status": "error",
                    "message": "limit must be an integer between 1 and 100",
                }
            if type(offset) is not int or offset < 0:
                return {
                    "status": "error",
                    "message": "offset must be a non-negative integer",
                }

            return await get_engine_registry().long_engine().ingest_list(
                space_id=space_id,
                batch_id=batch_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def long_ingest_cancel(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        job_id: Annotated[
            str,
            Field(description="Ingestion job identifier to cancel"),
        ],
    ) -> dict:
        """Request cooperative cancellation of an asynchronous ingestion job.

        Args:
            space_id: Target space identifier.
            job_id: Job identifier to cancel.

        Returns:
            Cancellation acknowledgment result.
        """
        from ..auth.context import check_access, check_write_permission, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            if not isinstance(job_id, str) or not job_id.strip():
                return {
                    "status": "error",
                    "message": "job_id must be a non-empty string",
                }

            return await get_engine_registry().long_engine().ingest_cancel(
                space_id=space_id,
                job_id=job_id.strip(),
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def long_document_list(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        limit: Annotated[
            int,
            Field(description="Maximum number of documents to return (1-100)"),
        ] = 50,
        offset: Annotated[
            int,
            Field(description="Number of documents to skip"),
        ] = 0,
        status: Annotated[
            Optional[str],
            Field(description="Optional filter by ingestion status (e.g. succeeded, failed)"),
        ] = None,
        query: Annotated[
            Optional[str],
            Field(description="Optional search filter on filename or source_path"),
        ] = None,
    ) -> dict:
        """List indexed documents in long memory for a space with pagination and filters.

        Args:
            space_id: Target space identifier.
            limit: Maximum items to return (1-100).
            offset: Items offset.
            status: Optional filter by status.
            query: Optional search filter.

        Returns:
            Dictionary with total_count, pagination, and documents list.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            if type(limit) is not int or limit < 1 or limit > 100:
                return {
                    "status": "error",
                    "message": "limit must be an integer between 1 and 100",
                }
            if type(offset) is not int or offset < 0:
                return {
                    "status": "error",
                    "message": "offset must be a non-negative integer",
                }

            return await get_engine_registry().long_engine().list_documents(
                space_id=space_id,
                limit=limit,
                offset=offset,
                status=status,
                query=query,
            )
        except Exception as e:
            return safe_error(e, "graph")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def long_document_get(
        space_id: Annotated[
            str,
            Field(description="Space identifier"),
        ],
        document_id: Annotated[
            Optional[str],
            Field(description="Internal document identifier (UUID)"),
        ] = None,
        source_path: Annotated[
            Optional[str],
            Field(description="Stable canonical source path of the document"),
        ] = None,
        include_content: Annotated[
            bool,
            Field(description="Download and include S3 content; default False"),
        ] = False,
    ) -> dict:
        """Get indexed document metadata and optional content by document_id or source_path.

        Args:
            space_id: Target space identifier.
            document_id: Optional internal document UUID.
            source_path: Optional canonical source path.
            include_content: Download content if True.

        Returns:
            Document metadata dictionary and content if requested.
        """
        from ..auth.context import check_access, safe_error
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            if type(include_content) is not bool:
                return {
                    "status": "error",
                    "message": f"'include_content' must be a boolean (True/False), got {type(include_content).__name__}: {include_content!r}",
                }

            if not document_id and not source_path:
                return {
                    "status": "error",
                    "message": "At least one of 'document_id' or 'source_path' must be provided",
                }

            if document_id is not None and (not isinstance(document_id, str) or not document_id.strip()):
                return {
                    "status": "error",
                    "message": "'document_id' must be a non-empty string when provided",
                }
            if source_path is not None and (not isinstance(source_path, str) or not source_path.strip()):
                return {
                    "status": "error",
                    "message": "'source_path' must be a non-empty string when provided",
                }

            return await get_engine_registry().long_engine().get_document(
                space_id=space_id,
                document_id=document_id.strip() if isinstance(document_id, str) else None,
                source_path=source_path.strip() if isinstance(source_path, str) else None,
                include_content=include_content,
            )
        except Exception as e:
            return safe_error(e, "graph")

    return 16  # 4 historiques + long_query / long_ingest / long_reindex + 3 ontology_* + 4 long_ingest_* + 2 long_document_*
