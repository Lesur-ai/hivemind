# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Backup (5 outils).

Sauvegarde et restauration d'espaces mémoire.

Permissions :
    - backup_create   ✏️ (write)   — Crée un snapshot d'espace
    - backup_list     🔑 (read)    — Liste les backups disponibles
    - backup_restore  🔧 (manage)  — Restaure un espace depuis un backup
    - backup_download 🔑 (read)    — Télécharge un backup (tar.gz base64)
    - backup_delete   🔧 (manage)  — Supprime un backup

Les backups sont des snapshots complets stockés dans _backups/ sur S3.
Voir S3_DATA_MODEL.md pour l'arborescence.
"""

import re
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


# LM2-09 fix : validation stricte du format backup_id avant tout accès S3.
# Le space_id doit matcher SPACE_ID_REGEX (déjà validé par check_access),
# et le timestamp doit matcher le format ISO produit par BackupService.create.
_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:-[0-9a-f]{32})?$"
)


def _parse_backup_id(backup_id: str) -> tuple[str | None, str | None, dict | None]:
    """
    LM2-09 fix : parse et valide un backup_id au format ``"space_id/timestamp"``.

    Returns:
        Tuple ``(space_id, timestamp, error)`` :

        - Si OK : ``(space_id, timestamp, None)``
        - Si invalide : ``(None, None, {"status": "error", ...})``

    Sécurité : la validation regex est appliquée AVANT tout accès S3
    pour empêcher path traversal (ex: ``"../_system/foo"``) ou injection
    de préfixes système.
    """
    if not backup_id or not isinstance(backup_id, str):
        return (
            None,
            None,
            {"status": "error", "message": "backup_id is required"},
        )

    parts = backup_id.split("/", 1)
    if len(parts) != 2:
        return (
            None,
            None,
            {
                "status": "error",
                "message": "Invalid backup_id (expected format: space_id/timestamp)",
            },
        )

    sid, ts = parts
    if not _SPACE_ID_RE.match(sid):
        return (
            None,
            None,
            {
                "status": "error",
                "message": f"Invalid space_id in backup_id: '{sid[:64]}'",
            },
        )
    if not _TIMESTAMP_RE.match(ts):
        return (
            None,
            None,
            {
                "status": "error",
                "message": (
                    f"Invalid timestamp in backup_id: '{ts[:32]}' "
                    "(expected: YYYY-MM-DDTHH-MM-SS or a suffixed operation id)"
                ),
            },
        )

    return sid, ts, None


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 5 outils backup sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (5)
    """

    @mcp.tool(
        description="Create an S3 backup of one space or all spaces.",
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False),
    )
    async def backup_create(
        space_id: Annotated[
            str,
            Field(
                description="Space to back up (empty means all spaces and requires admin)"
            ),
        ],
        description: Annotated[
            str,
            Field(
                default="",
                description="Optional backup description (for example, 'before migration')",
            ),
        ] = "",
    ) -> dict:
        """
        Crée un snapshot complet d'un espace sur S3.

        Copie tous les fichiers (meta, rules, notes, bank, synthesis)
        dans _backups/{space_id}/{timestamp}/.

        Si space_id est vide, crée un backup de TOUS les espaces
        (permission admin requise). Les erreurs sur un espace
        n'empêchent pas le backup des suivants.

        Args:
            space_id: Espace à sauvegarder (vide = tous, admin requis)
            description: Description du backup (optionnel)

        Returns:
            backup_id, nombre de fichiers, taille totale
        """
        from ..auth.context import (
            check_access,
            check_write_permission,
            check_admin_permission,
        )
        from ..core.backup import get_backup_service

        try:
            if not space_id:
                # Backup ALL spaces — admin only
                admin_err = check_admin_permission()
                if admin_err:
                    return admin_err
                return await get_backup_service().create_all(description)
            else:
                # Backup single space — write permission
                access_err = check_access(space_id)
                if access_err:
                    return access_err

                write_err = check_write_permission()
                if write_err:
                    return write_err

                return await get_backup_service().create(space_id, description)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "backup")

    @mcp.tool(
        description="List backups visible to the caller.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def backup_list(
        space_id: Annotated[
            str,
            Field(
                default="",
                description="Filter by space (empty = all accessible spaces)",
            ),
        ] = "",
    ) -> dict:
        """
        Liste les backups disponibles.

        Si space_id est fourni, ne liste que les backups de cet espace.
        Sinon, liste tous les backups de tous les espaces accessibles.

        Args:
            space_id: Filtrer par espace (vide = tous)

        Returns:
            Liste des backups avec backup_id et timestamp
        """
        # HM-04 fix : lire le store FRAIS (_get_effective_token_info) et non le
        # contextvar figé. Dans une session MCP Streamable-HTTP, current_token_info
        # est la copie gelée à l'init de session — un token rétrogradé/rescopé
        # en cours de session continuait à filtrer sur ses anciennes permissions
        # (persistance de privilège). Aligne backup_list sur ses sœurs
        # (space_list, bank_stale_spaces, bank_consolidation_queues).
        from ..auth.context import _get_effective_token_info, check_access
        from ..core.backup import get_backup_service

        try:
            token_info = _get_effective_token_info()
            if token_info is None:
                return {"status": "error", "message": "Authentication required"}

            # Si un espace est spécifié, vérifier l'accès
            if space_id:
                access_err = check_access(space_id)
                if access_err:
                    return access_err

            result = await get_backup_service().list_backups(space_id)

            # Filtrage par space_ids du token (alignement Graph Memory v0.7.0)
            # Un client ne doit voir que les backups des spaces autorisés.
            # Admin bypass. Non-admin + allowed=[] → aucun backup (v1.5.0).
            permissions = token_info.get("permissions", [])
            allowed = token_info.get("allowed_resources", [])
            if "admin" not in permissions and result.get("status") == "ok" and not space_id:
                filtered = [
                    b
                    for b in result.get("backups", [])
                    if b.get("space_id", b.get("backup_id", "").split("/")[0])
                    in allowed
                ]
                result["backups"] = filtered
                result["total"] = len(filtered)
                result["filtered_by_token"] = True

            return result
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "backup")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False
        )
    )
    async def backup_restore(
        backup_id: Annotated[
            str,
            Field(description="Backup identifier in 'space_id/timestamp' format"),
        ],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description="Must be true to confirm the restore operation",
            ),
        ] = False,
        unsafe_recovery: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Explicitly allow a forward-only restore over an existing "
                    "shared space in an unsafe recovery state. The operation "
                    "records an audit event, requires peer resynchronization, "
                    "and never bypasses corruption checks."
                ),
            ),
        ] = False,
    ) -> dict:
        """
        Restore a space from a backup.

        By default, the target space must not already exist. An explicit
        ``confirm=True`` acknowledgement is always required.

        Restoring over an existing shared or unsafe Hivemind space is refused
        unless ``unsafe_recovery=True`` is supplied explicitly. Corrupted state
        always fails closed, regardless of that flag.

        Unsafe recovery advances coordination state rather than rolling it
        backward, preserves deletion knowledge, clears incompatible queued
        work, records the recovery, and places the node in a state that requires
        peer resynchronization. If the safety preconditions cannot be proven,
        the restore is refused without mutation.

        Args:
            backup_id: Identifier in ``space_id/timestamp`` format.
            confirm: Must be true to acknowledge the destructive restore.
            unsafe_recovery: Explicitly authorize forward-only recovery over an
                existing shared space in an unsafe state.

        Returns:
            Restored file count and recovery status.
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.backup import get_backup_service

        try:
            # LM2-09 fix : valider le format backup_id AVANT tout accès S3
            space_id, _ts, parse_err = _parse_backup_id(backup_id)
            if parse_err:
                return parse_err

            # LM2-29 fix : vérifier l'accès à l'espace en plus de la permission
            # manage. Sans cela, un opérateur manage restreint à `["project-a"]`
            # pouvait restaurer un backup de `project-b` (cross-tenant leak).
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            if not confirm:
                return {
                    "status": "error",
                    "message": "Restore refused: confirm=True is required.",
                }

            return await get_backup_service().restore(
                backup_id, unsafe_recovery=unsafe_recovery
            )
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "backup")

    @mcp.tool(
        description="Download a backup as a base64-encoded tar archive.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def backup_download(
        backup_id: Annotated[
            str,
            Field(description="Backup identifier in 'space_id/timestamp' format"),
        ],
    ) -> dict:
        """
        Télécharge un backup en archive tar.gz (base64).

        Args:
            backup_id: Format "space_id/timestamp"

        Returns:
            Archive base64, taille, nombre de fichiers
        """
        # HM-04 fix : cohérence — store frais plutôt que contextvar figé.
        from ..auth.context import _get_effective_token_info, check_access
        from ..core.backup import get_backup_service

        try:
            token_info = _get_effective_token_info()
            if token_info is None:
                return {"status": "error", "message": "Authentication required"}

            # LM2-09 fix : valider strictement le backup_id avant accès S3
            space_id, _ts, parse_err = _parse_backup_id(backup_id)
            if parse_err:
                return parse_err

            access_err = check_access(space_id)
            if access_err:
                return access_err

            return await get_backup_service().download(backup_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "backup")

    @mcp.tool(
        description="Permanently delete a backup.",
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True),
    )
    async def backup_delete(
        backup_id: Annotated[
            str,
            Field(description="Backup identifier in 'space_id/timestamp' format"),
        ],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description="Must be true to confirm permanent deletion",
            ),
        ] = False,
    ) -> dict:
        """
        Supprime un backup (irréversible).

        Args:
            backup_id: Format "space_id/timestamp"
            confirm: Doit être True pour confirmer (sécurité)

        Returns:
            Nombre de fichiers supprimés
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.backup import get_backup_service

        try:
            # LM2-09 fix : valider le format backup_id AVANT tout accès S3
            space_id, _ts, parse_err = _parse_backup_id(backup_id)
            if parse_err:
                return parse_err

            # LM2-29 fix : check_access en plus de check_manage_permission
            # (cf. backup_restore — même rationale anti cross-tenant).
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            if not confirm:
                return {
                    "status": "error",
                    "message": "Deletion refused: confirm=True is required.",
                }

            return await get_backup_service().delete(backup_id)
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "backup")

    return 5  # Nombre d'outils enregistrés
