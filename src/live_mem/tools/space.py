# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Space (9 outils).

Gestion des espaces mémoire : créer, lister, inspecter, exporter, supprimer.

Permissions :
    - space_create        🔧 (manage)  — Crée un nouvel espace
    - space_update        ✏️ (write)   — Met à jour description/owner
    - space_update_rules  🔧 (manage)  — Met à jour les rules d'un espace
    - space_list          🔑 (read)    — Liste les espaces accessibles
    - space_info          🔑 (read)    — Infos détaillées d'un espace
    - space_rules         🔑 (read)    — Lit les rules
    - space_summary       🔑 (read)    — Synthèse complète (rules + bank)
    - space_export        🔑 (read)    — Export tar.gz en base64
    - space_delete        🔧 (manage)  — Supprime un espace (irréversible)

Chaque outil délègue au SpaceService (core/space.py) après vérification
des permissions via les helpers auth/context.py.
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 9 outils space sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (9)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def space_create(
        space_id: Annotated[
            str,
            Field(
                description=(
                    "Unique space identifier: letters, numbers, hyphens, and "
                    "underscores; maximum 64 characters"
                )
            ),
        ],
        description: Annotated[
            str, Field(description="Short description of the space")
        ],
        rules: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Markdown consolidation rules that define the memory bank "
                    "structure; empty uses DEFAULT_RULES_FILE"
                ),
            ),
        ] = "",
        owner: Annotated[
            str,
            Field(
                default="",
                description="Optional informational owner of the space",
            ),
        ] = "",
    ) -> dict:
        """
        Create a memory space with its consolidation rules.

        Rules define the structure and expected content of the memory bank.
        A manage-level operator can update them later with
        ``space_update_rules``.

        If ``rules`` is empty, the server loads the configured default rules.

        Args:
            space_id: Unique identifier using letters, numbers, hyphens, and
                underscores; maximum 64 characters.
            description: Short space description.
            rules: Markdown rules; empty selects the configured defaults.
            owner: Optional informational owner.

        Returns:
            Details of the created space.
        """
        from pathlib import Path
        from ..auth.context import (
            check_manage_permission,
            get_effective_token_info,
        )
        from ..config import get_settings
        from ..core.space import get_space_service

        try:
            # LM2-11 : provisionner un espace est une opération manage, pas une
            # écriture ordinaire. SpaceService revalide ensuite tout token S3
            # exact sous le verrou tokens ; ce check est l'early deny MCP.
            manage_err = check_manage_permission()
            if manage_err:
                return manage_err
            actor = get_effective_token_info()
            if actor is None:
                return {"status": "error", "message": "Authentification requise"}

            # Si rules vide, charger les rules par défaut
            effective_rules = rules
            if not effective_rules.strip():
                settings = get_settings()
                if settings.default_rules_file:
                    rules_path = Path(settings.default_rules_file)
                    if rules_path.is_file():
                        effective_rules = rules_path.read_text(encoding="utf-8")
                    else:
                        return {
                            "status": "error",
                            "message": f"Fichier de rules par défaut introuvable : {settings.default_rules_file}",
                        }
                else:
                    return {
                        "status": "error",
                        "message": (
                            "Paramètre 'rules' requis. "
                            "Aucun fichier de rules par défaut configuré (DEFAULT_RULES_FILE)."
                        ),
                    }

            return await get_space_service().create(
                space_id=space_id,
                description=description,
                rules=effective_rules,
                owner=owner,
                actor_token_hash=actor.get("token_hash", ""),
                bootstrap_admin=(
                    actor.get("type") == "bootstrap"
                    and "admin" in actor.get("permissions", [])
                ),
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def space_update(
        space_id: Annotated[
            str, Field(description="Identifiant de l'espace à modifier")
        ],
        description: Annotated[
            str,
            Field(
                default="",
                description="Nouvelle description (vide = pas de changement)",
            ),
        ] = "",
        owner: Annotated[
            str,
            Field(
                default="",
                description="Nouveau propriétaire (vide = pas de changement)",
            ),
        ] = "",
    ) -> dict:
        """
        Update a space's description or owner metadata.

        Rules are managed separately through ``space_update_rules``. Only
        non-empty metadata fields are changed.

        Args:
            space_id: Space to update.
            description: New description; empty leaves it unchanged.
            owner: New owner; empty leaves it unchanged.

        Returns:
            Changed fields and their new values.
        """
        from ..auth.context import check_access, check_write_permission
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            write_err = check_write_permission()
            if write_err:
                return write_err

            return await get_space_service().update(
                space_id=space_id,
                description=description if description else None,
                owner=owner if owner else None,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def space_update_rules(
        space_id: Annotated[str, Field(description="Space identifier")],
        rules: Annotated[str, Field(description="Complete replacement rules in Markdown")],
    ) -> dict:
        """
        Replace a space's consolidation rules.

        This manage-level operation replaces the complete Markdown rules
        document without deleting or recreating the space.

        Use it for rule corrections, template migrations, or consolidation
        policy changes.

        Args:
            space_id: Space to update.
            rules: Complete replacement rules in Markdown.

        Returns:
            Size of the stored rules document.
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            return await get_space_service().update_rules(
                space_id=space_id,
                rules=rules,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def space_list() -> dict:
        """
        List memory spaces accessible to the current credential.

        Returns metadata plus short-note and memory-bank file counts for each
        visible space.

        Returns:
            Accessible spaces and summary statistics.
        """
        from ..auth.context import _get_effective_token_info
        from ..core.space import get_space_service

        try:
            # Récupérer les space_ids autorisés depuis le token (données fraîches)
            token_info = _get_effective_token_info()
            if token_info is None:
                return {"status": "error", "message": "Authentification requise"}

            permissions = token_info.get("permissions", [])
            allowed = token_info.get("allowed_resources", [])
            # Admin → accès à tous les espaces
            # Non-admin + allowed vide → aucun espace (v1.5.0)
            if "admin" in permissions:
                allowed_ids = None  # Pas de filtre
            elif not allowed:
                allowed_ids = []  # Aucun espace
            else:
                allowed_ids = allowed

            return await get_space_service().list_spaces(allowed_space_ids=allowed_ids)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def space_info(
        space_id: Annotated[str, Field(description="Space identifier")],
    ) -> dict:
        """
        Return detailed metadata and statistics for a space.

        Includes short-note counts and sizes, memory-bank file statistics, and
        consolidation status.

        Args:
            space_id: Space to inspect.

        Returns:
            Space metadata and statistics.
        """
        from ..auth.context import check_access
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_space_service().get_info(space_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def space_rules(
        space_id: Annotated[str, Field(description="Space identifier")],
    ) -> dict:
        """
        Read the current consolidation rules for a space.

        Rules define the intended memory-bank structure and guide the
        consolidator when it creates or updates bank files. A manage-level
        operator can replace them with ``space_update_rules``.

        Args:
            space_id: Space whose rules should be returned.

        Returns:
            Current rules as Markdown.
        """
        from ..auth.context import check_access
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_space_service().get_rules(space_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def space_summary(
        space_id: Annotated[str, Field(description="Space identifier")],
    ) -> dict:
        """
        Load a complete space bootstrap: rules, memory bank, and statistics.

        Use this at session startup when an agent needs the whole compact
        project context in one request.

        Args:
            space_id: Space to summarize.

        Returns:
            Rules, complete bank files, residual summary, and statistics.
        """
        from ..auth.context import check_access
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_space_service().get_summary(space_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def space_export(
        space_id: Annotated[
            str, Field(description="Identifiant de l'espace à exporter")
        ],
    ) -> dict:
        """
        Exporte un espace complet en archive tar.gz (base64).

        L'archive contient tous les fichiers de l'espace : _meta.json,
        _rules.md, notes live, fichiers bank, synthèse.

        Args:
            space_id: Identifiant de l'espace

        Returns:
            Archive base64, taille et nombre de fichiers
        """
        from ..auth.context import check_access
        from ..core.space import get_space_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_space_service().export_space(space_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def space_delete(
        space_id: Annotated[
            str, Field(description="Identifiant de l'espace à supprimer")
        ],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description="Doit être True pour confirmer la suppression (sécurité)",
            ),
        ] = False,
        unsafe_recovery: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Explicitly allow deletion of a shared space that is in an "
                    "unsafe recovery state. This never bypasses corruption "
                    "checks; corrupted state still fails closed."
                ),
            ),
        ] = False,
        recover_access_grants: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Explicitly remove surviving token scopes only when the "
                    "space prefix is already confirmed empty after a known "
                    "older or interrupted deletion. Never set this merely "
                    "because an ID is absent: it can destroy intentional "
                    "future-space pre-grants."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Supprime un espace et TOUTES ses données (irréversible).

        ⚠️ ATTENTION : cette opération est destructive et ne peut pas être annulée.
        Le paramètre confirm doit être True pour confirmer la suppression.
        Nécessite la permission manage ou admin.

        Deleting a shared Hivemind space also removes its coordination state and
        forces peers to resynchronize. The operation therefore refuses shared
        unsafe state by default. Set ``unsafe_recovery=True`` to acknowledge
        and authorize that recovery action explicitly. Corrupted state still
        fails closed.

        A successful deletion also removes this ``space_id`` from every
        persisted token allowlist. Any registry rewrite is reported as deleted
        only after a fresh token-store read confirms zero remaining references.
        If the prefix is already absent, grants are preserved by default because
        they may be intentional future-space pre-grants. Set
        ``recover_access_grants=True`` only to resume a known older or
        interrupted deletion; grants-only success is ``grants_cleaned``.

        Args:
            space_id: Identifiant de l'espace à supprimer
            confirm: Doit être True pour confirmer (sécurité)
            unsafe_recovery: Explicitly authorize deletion of a shared space in
                an unsafe recovery state.
            recover_access_grants: Explicitly clean surviving scopes for a
                known deletion whose prefix is already empty.

        Returns:
            Confirmation de suppression avec nombres de fichiers supprimés et
            de grants révoqués, ou recovery typée si une étape est ambiguë.
        """
        from ..auth.context import (
            check_access,
            check_manage_permission,
            get_effective_token_info,
        )
        from ..core.space import get_space_service

        try:
            # Double vérification : accès + manage
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err
            actor = get_effective_token_info()
            if actor is None:
                return {"status": "error", "message": "Authentification requise"}

            # Sécurité : confirm obligatoire
            if not confirm:
                return {
                    "status": "error",
                    "message": (
                        "Suppression refusée : confirm=True requis. "
                        "⚠️ Cette opération est irréversible !"
                    ),
                }

            return await get_space_service().delete(
                space_id,
                unsafe_recovery=unsafe_recovery,
                recover_access_grants=recover_access_grants,
                actor_token_hash=actor.get("token_hash", ""),
                bootstrap_admin=(
                    actor.get("type") == "bootstrap"
                    and "admin" in actor.get("permissions", [])
                ),
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "space")

    return 9  # Nombre d'outils enregistrés
