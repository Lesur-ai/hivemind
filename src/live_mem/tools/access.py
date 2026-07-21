# -*- coding: utf-8 -*-
"""Outils MCP — provisionnement délégué manage (LM2-11).

Ces outils sont volontairement séparés des opérations ``admin_*`` : un token
``manage`` peut créer un credential non-admin sans scope initial puis l'inviter
dans un espace qu'il gère. Il ne peut ni lister, révoquer, rescope négativement
ou promouvoir un token admin.
"""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


def register(mcp: FastMCP) -> int:
    """Enregistre les deux outils cross-cutting LM2-11 (aucun alias tier)."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def token_create(
        name: Annotated[
            str,
            Field(description="Descriptive name for the token to create"),
        ],
        permissions: Annotated[
            Literal["read", "read,write", "read,write,manage"],
            Field(
                description=(
                    "Canonical permission profile: 'read', 'read,write', or "
                    "'read,write,manage'"
                )
            ),
        ],
        expires_in_days: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Lifetime in days; 0 means no expiration",
            ),
        ] = 0,
        email: Annotated[
            str,
            Field(default="", description="Optional owner email address"),
        ] = "",
    ) -> dict:
        """Create a non-admin token without initial space access.

        The secret and its complete hash are returned only once, after fresh
        authorization of the calling manager or administrator.
        """
        from ..auth.context import (
            check_manage_permission,
            get_effective_token_info,
            safe_error,
        )
        from ..core.tokens import get_token_service

        try:
            manage_err = check_manage_permission()
            if manage_err:
                return manage_err
            actor = get_effective_token_info()
            actor_hash = actor.get("token_hash", "") if actor else ""
            return await get_token_service().create_delegated_token(
                actor_token_hash=actor_hash,
                name=name,
                permissions=permissions,
                expires_in_days=expires_in_days,
                email=email,
            )
        except Exception as exc:
            return safe_error(exc, "token_create")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def space_invite_token(
        space_id: Annotated[
            str,
            Field(description="Space to grant the token access to"),
        ],
        token_hash: Annotated[
            str,
            Field(
                description=(
                    "Complete canonical target hash: sha256: followed by 64 "
                    "lowercase hexadecimal characters"
                )
            ),
        ],
    ) -> dict:
        """Idempotently grant an active token access to a managed space."""
        from ..auth.context import (
            check_access,
            check_manage_permission,
            get_effective_token_info,
            safe_error,
        )
        from ..core.tokens import get_token_service

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err
            manage_err = check_manage_permission()
            if manage_err:
                return manage_err
            actor = get_effective_token_info()
            actor_hash = actor.get("token_hash", "") if actor else ""
            return await get_token_service().invite_token_to_space(
                actor_token_hash=actor_hash,
                space_id=space_id,
                target_token_hash=token_hash,
            )
        except Exception as exc:
            return safe_error(exc, "space_invite_token")

    return 2
