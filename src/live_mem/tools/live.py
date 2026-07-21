# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Live (3 outils).

Notes en temps réel : écrire, lire, rechercher.

Permissions :
    - live_note   ✏️ (write) — Écrit une note (append-only, zéro conflit)
    - live_read   🔑 (read)  — Lit les notes récentes avec filtres
    - live_search 🔑 (read)  — Recherche texte dans les notes

Les notes live sont l'outil principal utilisé par les agents pendant
leur travail. Chaque note = 1 fichier S3 unique → aucun conflit
entre agents écrivant simultanément.

Catégories standard :
    observation — Constat factuel ("Le build passe")
    decision    — Choix technique ("On part sur S3")
    todo        — Tâche à faire ("Implémenter le backup")
    insight     — Pattern découvert ("Le pattern X marche")
    question    — Question ouverte ("Supporter le CSV ?")
    progress    — Avancement ("Module auth : 80%")
    issue       — Problème, bug ("Timeout LLM > 60s")
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 3 outils live sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (3)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def live_note(
        space_id: Annotated[str, Field(description="Target space identifier")],
        category: Annotated[
            str,
            Field(
                description="Note category: observation|decision|todo|insight|question|progress|issue"
            ),
        ],
        content: Annotated[str, Field(description="Free-form note content")],
        tags: Annotated[
            str,
            Field(
                default="",
                description="Comma-separated tags, for example 'auth,security,urgent'",
            ),
        ] = "",
    ) -> dict:
        """
        Append a note to a space's short-term memory.

        This is the primary write tool for agents during active work. Each note
        is stored as a unique append-only object so concurrent writers do not
        overwrite one another.

        The agent identity always comes from the authenticated credential's
        ``client_name``. Callers cannot supply a different agent identity.

        Args:
            space_id: Target space.
            category: observation|decision|todo|insight|question|progress|issue
            content: Free-form note content.
            tags: Optional comma-separated tags.

        Returns:
            Created object name, size, and timestamp.
        """
        from ..auth.context import check_access, check_write_permission
        from ..core.engines import get_engine_registry
        from ..core.write_sink import (
            DirectLocalWriteSink,
            StagedWriteNotImplemented,
        )

        try:
            # Vérifier accès à l'espace + permission write
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            # P3-7 ROUTE-FIRST-THEN-DELEGATE: resolve the per-space WriteSink
            # BEFORE any durable write. The single durable mutation is the live
            # note PUT, which lives INSIDE LiveService.write_note (it builds the
            # filename and calls get_storage() directly — the held sink is inert,
            # and live.py is NOT edited here per wrap-don't-rewrite). So:
            #   - DIRECT_LOCAL (non-Hivemind) -> delegate to the short engine's
            #     write_note: byte-for-byte identical legacy PUT via get_storage.
            #   - STAGED (Hivemind-healthy) -> raise StagedWriteNotImplemented
            #     BEFORE LiveService runs, so NO PUT ever happens (the staged
            #     sink would refuse, but the key is built inside LiveService, so
            #     we surface the typed refusal directly).
            #   - REFUSE (unsafe/resync) / corrupt -> resolve_sink raises
            #     (RegistryRefused / CorruptedStateError) before any write,
            #     and the except below renders it via safe_error.
            # SINGLE resolution: build the engine (which resolves the route
            # once) and gate on the ENGINE's own resolved sink — never a second,
            # independent resolve_sink whose verdict could differ from the one
            # the engine carries (that gap let an observed STAGED still fall
            # through to the inert legacy write). REFUSE/corrupt raise inside
            # short_engine() before it returns.
            registry = get_engine_registry()
            engine = await registry.short_engine(space_id)
            if not isinstance(engine.write_sink, DirectLocalWriteSink):
                raise StagedWriteNotImplemented(
                    op="put", key=f"{space_id}/live/"
                )
            return await engine.write_note(
                space_id=space_id,
                category=category,
                content=content,
                tags=tags,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "live")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def live_read(
        space_id: Annotated[str, Field(description="Target space identifier")],
        limit: Annotated[
            int,
            Field(
                default=50, description="Maximum number of notes to return (default: 50)"
            ),
        ] = 50,
        category: Annotated[
            str,
            Field(
                default="",
                description="Filter by category: observation|decision|todo|insight|question|progress|issue",
            ),
        ] = "",
        agent: Annotated[
            str, Field(default="", description="Filter by agent identifier")
        ] = "",
        since: Annotated[
            str,
            Field(
                default="",
                description="Return notes after this ISO 8601 timestamp, for example '2026-03-08T10:00:00'",
            ),
        ] = "",
    ) -> dict:
        """
        Read recent short-term notes from a space.

        Supports optional category, agent, and timestamp filters. Notes are
        returned from newest to oldest.

        Args:
            space_id: Target space.
            limit: Maximum number of notes.
            category: Optional category filter.
            agent: Optional agent filter.
            since: Optional ISO 8601 lower bound.

        Returns:
            Matching notes with metadata and content.
        """
        from ..auth.context import check_access
        from ..core.live import get_live_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_live_service().read_notes(
                space_id=space_id,
                limit=limit,
                category=category,
                agent=agent,
                since=since,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "live")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def live_search(
        space_id: Annotated[str, Field(description="Target space identifier")],
        query: Annotated[
            str, Field(description="Case-insensitive text to find in note content")
        ],
        limit: Annotated[
            int, Field(default=20, description="Maximum number of results (default: 20)")
        ] = 20,
    ) -> dict:
        """
        Search a space's short-term notes by text.

        Searches note content case-insensitively and returns matches from newest
        to oldest.

        Args:
            space_id: Target space.
            query: Text to search for.
            limit: Maximum number of results.

        Returns:
            Notes whose content matches the query.
        """
        from ..auth.context import check_access
        from ..core.live import get_live_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_live_service().search_notes(
                space_id=space_id,
                query=query,
                limit=limit,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "live")

    return 3  # Nombre d'outils enregistrés
