# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Admin (9 outils).

Gestion des tokens d'authentification et maintenance.

Permissions :
    - admin_audit_recent       👑 (admin) — Lit l'audit local récent
    - admin_create_token       👑 (admin) — Crée un token
    - admin_list_tokens        👑 (admin) — Liste les tokens (avec filtres)
    - admin_revoke_token       👑 (admin) — Révoque un token
    - admin_delete_token       👑 (admin) — Supprime physiquement un token
    - admin_purge_tokens       👑 (admin) — Purge en masse les tokens
    - admin_update_token       👑 (admin) — Modifie un token (remplacement ou delta)
    - admin_bulk_update_tokens 👑 (admin) — Bulk update : delta sur N tokens
    - admin_gc_notes           👑 (admin) — GC des notes orphelines

Tous les outils admin requièrent la permission "admin".
Voir AUTH_AND_COLLABORATION.md pour le modèle de tokens.
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 9 outils admin sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (9)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def admin_create_token(
        name: Annotated[
            str,
            Field(
                description="Descriptive token name, for example 'agent-cline' or 'ci-pipeline'"
            ),
        ],
        permissions: Annotated[
            str,
            Field(
                description=(
                    "Comma-separated subset of read,write,manage,admin. Common "
                    "profiles are 'read', 'read,write', 'read,write,manage', or "
                    "'read,write,manage,admin'"
                )
            ),
        ],
        space_ids: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Comma-separated accessible spaces. Empty grants no access "
                    "to existing spaces until a manager or administrator invites "
                    "the token. Use '*' or 'all' for a snapshot of current spaces; "
                    "future spaces are not added automatically. This value is "
                    "ignored for admin tokens, whose scope is global."
                ),
            ),
        ] = "",
        expires_in_days: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Validity period in days; 0 means no expiration",
            ),
        ] = 0,
        email: Annotated[
            str,
            Field(
                default="", description="Optional owner email for traceability"
            ),
        ] = "",
    ) -> dict:
        """
        Create an authentication token.

        The plaintext token is returned only once. Only its SHA-256 hash is
        stored.

        Args:
            name: Descriptive token name.
            permissions: Comma-separated permission subset.
            space_ids: Comma-separated spaces. Empty grants no space access;
                ``*`` or ``all`` captures current spaces. Admin scope is global.
            expires_in_days: Validity period in days; 0 means no expiration.
            email: Optional owner email.

        Returns:
            Plaintext token, permissions, and expiration. A
            ``warning_no_access`` field is included when a non-admin token has
            no access to an existing space.
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().create_token(
                name=name,
                permissions=permissions,
                space_ids=space_ids,
                expires_in_days=expires_in_days,
                email=email,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def admin_list_tokens(
        name_contains: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Filter by a case-insensitive substring of the token name. "
                    "Empty disables this filter."
                ),
            ),
        ] = "",
        has_space: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Filter tokens whose space_ids contain this exact, "
                    "case-sensitive space identifier. Empty disables this filter."
                ),
            ),
        ] = "",
        include_revoked: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Whether to include revoked tokens (default: true)"
                ),
            ),
        ] = True,
    ) -> dict:
        """
        List token metadata; plaintext credentials are never returned.

        Optional filters are applied server-side and combine with AND semantics.

        Args:
            name_contains: Case-insensitive token-name substring.
            has_space: Exact space identifier required in the token scope.
            include_revoked: Whether to include revoked tokens.

        Returns:
            Token metadata and, when active, the applied filters.
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().list_tokens(
                name_contains=name_contains,
                has_space=has_space,
                include_revoked=include_revoked,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def admin_audit_recent(
        limit: Annotated[
            int,
            Field(
                default=50,
                description=(
                    "Number of recent events (1..500, default 50). "
                    "The value is bounded server-side."
                ),
            ),
        ] = 50,
    ) -> dict:
        """Retourne l'audit local récent de cette instance, plus récent d'abord.

        Cette vue est volatile et best-effort : elle couvre seulement les
        événements console/auth depuis le dernier redémarrage du processus.
        Elle n'est ni persistante, ni une piste d'audit complète.
        """
        from ..auth.context import check_admin_permission
        from ..core.audit_ring import AUDIT_SCOPE_NOTE, capacity, snapshot

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            effective_limit = min(max(limit, 1), 500)
            entries = list(reversed(snapshot()))[:effective_limit]
            return {
                "status": "ok",
                "entries": entries,
                "total": len(entries),
                "capacity": capacity(),
                "scope_note": AUDIT_SCOPE_NOTE,
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def admin_revoke_token(
        token_hash: Annotated[
            str,
            Field(
                description=(
                    "Hash of the token to revoke (from admin_list_tokens). "
                    "The 'sha256:' prefix is optional; accepts either "
                    "'sha256:abc...' or 'abc...'. At least 16 hex characters."
                )
            ),
        ],
    ) -> dict:
        """
        Révoque un token (le rend définitivement inutilisable).

        Args:
            token_hash: Hash tronqué du token (depuis admin_list_tokens)

        Returns:
            Confirmation de révocation
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().revoke_token(token_hash)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
    async def admin_delete_token(
        token_hash: Annotated[
            str,
            Field(
                description=(
                    "Hash of the token to delete (from admin_list_tokens). "
                    "The 'sha256:' prefix is optional. At least 16 hex characters."
                )
            ),
        ],
    ) -> dict:
        """
        Supprime physiquement un token du registre.

        Contrairement à revoke_token qui marque le token comme inactif,
        cette opération le retire complètement de tokens.json.
        ⚠️ Opération irréversible.

        Note: Le bootstrap key (variable d'environnement) n'est jamais
        dans tokens.json et ne peut donc pas être supprimé.

        Args:
            token_hash: Hash tronqué du token (depuis admin_list_tokens)

        Returns:
            Confirmation de suppression avec nombre de tokens restants
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().delete_token(token_hash)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False))
    async def admin_purge_tokens(
        revoked_only: Annotated[
            bool,
            Field(
                default=True,
                description="True deletes only revoked tokens; false deletes every stored token",
            ),
        ] = True,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Required when revoked_only is false. Prevents accidental "
                    "deletion of every token, which leaves bootstrap-only access."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Permanently purge tokens from the registry.

        By default, only revoked tokens are deleted. Setting
        ``revoked_only=False`` deletes every stored token and requires an
        explicit ``confirm=True`` acknowledgement.

        This operation is irreversible. The configured bootstrap key is not
        affected.

        Args:
            revoked_only: Delete only revoked tokens when true.
            confirm: Must be true when deleting every token.

        Returns:
            Counts of deleted and remaining tokens.
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            # LM2-31 fix : exiger confirm=True pour la purge totale.
            # La purge des seuls révoqués reste possible sans confirm (nettoyage
            # courant, non-destructeur pour les agents actifs).
            if not revoked_only and not confirm:
                return {
                    "status": "error",
                    "message": (
                        "Full purge refused: confirm=True is required when "
                        "revoked_only=False. This operation deletes ALL tokens "
                        "and leaves the server accessible only through the "
                        "bootstrap_key. If this is intended, call the tool "
                        "again with confirm=True."
                    ),
                }

            return await get_token_service().purge_tokens(revoked_only=revoked_only)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def admin_update_token(
        token_hash: Annotated[
            str,
            Field(
                description=(
                    "Hash of the token to update, as returned by "
                    "admin_list_tokens. The 'sha256:' prefix is optional; at "
                    "least 16 hexadecimal characters are required."
                )
            ),
        ],
        space_ids: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Replacement mode: complete comma-separated space scope. "
                    "Empty leaves the scope unchanged. Use '*' or 'all' for a "
                    "snapshot of current spaces. Prefer space_ids_add for a "
                    "safe additive update."
                ),
            ),
        ] = "",
        permissions: Annotated[
            str,
            Field(
                default="",
                description=(
                    "New comma-separated subset of read,write,manage,admin. "
                    "Common profiles include read, read,write, "
                    "read,write,manage, and read,write,manage,admin. "
                    "Empty leaves permissions unchanged. Promotion to admin "
                    "clears the space scope; downgrade starts with an empty scope."
                ),
            ),
        ] = "",
        email: Annotated[
            str,
            Field(
                default="",
                description="New owner email; empty leaves it unchanged",
            ),
        ] = "",
        space_ids_add: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Delta mode: comma-separated spaces to add. Idempotent and "
                    "incompatible with replacement mode. '*' and 'all' are not "
                    "accepted here."
                ),
            ),
        ] = "",
        space_ids_remove: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Delta mode: comma-separated spaces to remove. Idempotent; "
                    "removals are applied before additions when both are supplied."
                ),
            ),
        ] = "",
    ) -> dict:
        """
        Update a token's permissions, email, or space scope.

        Space scope has three modes:

        1. No change when every ``space_ids*`` parameter is empty.
        2. Complete replacement when ``space_ids`` is non-empty.
        3. Idempotent delta through ``space_ids_add`` and
           ``space_ids_remove``; removals are applied first.

        Replacement and delta modes are mutually exclusive.

        Args:
            token_hash: Token hash returned by ``admin_list_tokens``.
            space_ids: Complete replacement scope.
            permissions: New permissions; empty leaves them unchanged.
            email: New email; empty leaves it unchanged.
            space_ids_add: Comma-separated spaces to add.
            space_ids_remove: Comma-separated spaces to remove.

        Returns:
            Updated scope details and a warning when the token has no access.
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().update_token(
                token_hash=token_hash,
                space_ids=space_ids,
                permissions=permissions,
                email=email,
                space_ids_add=space_ids_add,
                space_ids_remove=space_ids_remove,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def admin_bulk_update_tokens(
        names: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Comma-separated exact token names, for example "
                    "'agent-laptop,agent-desktop,agent-ci'. Combines with "
                    "name_contains and has_space using AND semantics. At least "
                    "one selection filter is required."
                ),
            ),
        ] = "",
        name_contains: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Case-insensitive token-name substring. Combines with names "
                    "and has_space using AND semantics. At least one selection "
                    "filter is required."
                ),
            ),
        ] = "",
        has_space: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Select tokens whose space_ids contain this exact, "
                    "case-sensitive space identifier. Combines with other "
                    "selection filters using AND semantics."
                ),
            ),
        ] = "",
        permissions: Annotated[
            str,
            Field(
                default="",
                description=(
                    "New comma-separated permission subset for selected tokens. "
                    "Common profiles include read, read,write, "
                    "read,write,manage, and read,write,manage,admin. "
                    "Empty leaves permissions unchanged. Promotion to admin "
                    "clears space scopes; downgrade applies deltas from an empty scope."
                ),
            ),
        ] = "",
        email: Annotated[
            str,
            Field(
                default="",
                description="New email for selected tokens; empty leaves it unchanged",
            ),
        ] = "",
        space_ids_add: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Comma-separated spaces to add. Idempotent; '*' and 'all' are not accepted."
                ),
            ),
        ] = "",
        space_ids_remove: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Comma-separated spaces to remove. Idempotent; '*' and 'all' are not accepted."
                ),
            ),
        ] = "",
        include_revoked: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include revoked tokens in updates (default: false). "
                    "Revoked matches that are not updated are returned in "
                    "skipped_revoked."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Update multiple tokens in one operation.

        Use additive scope deltas to grant or remove space access across a
        selected set without rebuilding each token's complete scope. Complete
        scope replacement is intentionally unavailable in bulk.

        Validation and updates are applied in memory, then persisted in one
        write under a process-local lock. An error before that write leaves the
        registry unchanged. This atomicity applies within one server instance.

        At least one of ``names``, ``name_contains``, or ``has_space`` is
        required, and all supplied filters combine with AND semantics. Revoked
        tokens are skipped by default; set ``include_revoked=True`` to update
        them explicitly.

        At least one update operation is required. Permission and email changes
        apply to every selected token; space additions and removals are
        idempotent. A structured audit event is recorded after persistence.

        Returns:
            ``{"status": "ok", "updated": N, "tokens": [{name, hash,
            before: {...}, after: {...}, space_ids_added: [...], ...}],
            "skipped_revoked": [...], "filters": {...}, "operations": {...}}``.
            If no active token matches, ``updated`` is zero and status is ok.
        """
        from ..auth.context import check_admin_permission
        from ..core.tokens import get_token_service

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            return await get_token_service().bulk_update_tokens(
                names=names,
                name_contains=name_contains,
                has_space=has_space,
                permissions=permissions,
                email=email,
                space_ids_add=space_ids_add,
                space_ids_remove=space_ids_remove,
                include_revoked=include_revoked,
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def admin_gc_notes(
        space_id: Annotated[
            str,
            Field(
                default="", description="Target space (empty = scan ALL spaces)"
            ),
        ] = "",
        max_age_days: Annotated[
            int,
            Field(
                default=7,
                ge=0,
                description="Age threshold in days for considering a note orphaned (default 7)",
            ),
        ] = 7,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description="False = dry run (scan only); True = execute",
            ),
        ] = False,
        delete_only: Annotated[
            bool,
            Field(
                default=False,
                description="With confirm=True, delete WITHOUT consolidating (data loss)",
            ),
        ] = False,
        expected_eligible_set_token: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Opaque precondition returned by the previous dry run; "
                    "required with confirm=True + delete_only=True"
                ),
            ),
        ] = "",
    ) -> dict:
        """
        Garbage Collector : consolide ou supprime les notes orphelines.

        Les notes live non consolidées par un agent disparu s'accumulent.
        Cet outil les identifie (plus vieilles que max_age_days).

        3 modes :
        - confirm=False (défaut) : DRY-RUN — scanne et rapporte
        - confirm=True : CONSOLIDE les notes dans la bank via LLM
          (ajoute une notice "⚠️ GC consolidation forcée" dans chaque bank)
        - confirm=True, delete_only=True : SUPPRIME sans consolider

        Args:
            space_id: Espace cible (vide = scanner TOUS les espaces)
            max_age_days: Seuil en jours (défaut 7)
            confirm: False = dry-run, True = exécution
            delete_only: Si True + confirm, supprime SANS consolider
            expected_eligible_set_token: Token exact de l'ensemble éligible
                renvoyé par un dry-run préalable (suppression uniquement)

        Returns:
            Rapport : nombre de notes, taille, répartition par agent
        """
        from ..auth.context import check_admin_permission
        from ..core.engines import RegistryRefused
        from ..core.gc import get_gc_service
        from ..core.hivemind import CorruptedStateError
        from ..core.write_sink import StagedWriteNotImplemented

        try:
            admin_err = check_admin_permission()
            if admin_err:
                return admin_err

            # Défense en profondeur pour les appels directs à la fonction
            # enregistrée (les clients MCP sont déjà bornés par ``Field(ge=0)``).
            # Un seuil négatif déplacerait le cutoff dans le futur et pourrait
            # rendre éligibles des notes qui ne sont pas anciennes.
            if max_age_days < 0:
                return {
                    "status": "error",
                    "reason": "invalid_max_age_days",
                    "message": "max_age_days must be greater than or equal to 0.",
                }

            gc = get_gc_service()

            if confirm and delete_only:
                # Mode suppression sans consolidation (perte de données)
                return await gc.delete_old_notes(
                    space_id=space_id,
                    max_age_days=max_age_days,
                    expected_eligible_set_token=expected_eligible_set_token,
                )
            elif confirm:
                # Mode consolidation (défaut avec confirm)
                return await gc.consolidate_old_notes(
                    space_id=space_id,
                    max_age_days=max_age_days,
                )
            else:
                # Mode dry-run : scanner seulement
                result = await gc.scan_old_notes(
                    space_id=space_id,
                    max_age_days=max_age_days,
                )
                for sid in result.get("spaces", {}):
                    if "keys" in result["spaces"][sid]:
                        count = len(result["spaces"][sid]["keys"])
                        del result["spaces"][sid]["keys"]
                        result["spaces"][sid]["keys_count"] = count
                result["mode"] = "dry-run"
                result["message"] = (
                    f"Dry run: {result['total_old_notes']} orphaned notes "
                    f"found. Use confirm=True to consolidate, or "
                    f"confirm=True+delete_only=True with "
                    f"expected_eligible_set_token to delete."
                )
                return result

        except StagedWriteNotImplemented:
            return {
                "status": "error",
                "reason": "route_staged_not_implemented",
                "message": (
                    "GC refused: staged Hivemind writes are not supported "
                    "for this operation."
                ),
            }
        except RegistryRefused:
            return {
                "status": "error",
                "reason": "route_refused",
                "message": (
                    "GC refused: the space cannot be modified in its current "
                    "Hivemind state."
                ),
            }
        except CorruptedStateError:
            return {
                "status": "error",
                "reason": "state_corrupt",
                "message": (
                    "GC refused: the space's critical Hivemind state is "
                    "corrupted."
                ),
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "admin")

    return 9  # Nombre d'outils enregistrés
