# -*- coding: utf-8 -*-
"""Fail-closed MCP exposure registry and request-scoped discovery projection.

The complete compatibility surface remains registered and callable.  This
module only controls which canonical agent-core names are advertised by
``tools/list``; call-time authorization remains in the existing tool handlers.

P10-1 contract: ``DESIGN/hivemind/P10_MCP_DISCOVERY_CONTRACT.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool


class ToolAudience(StrEnum):
    """Who should see a tool in regular MCP discovery."""

    AGENT_CORE = "agent_core"
    OPERATOR = "operator"


class ToolPermission(StrEnum):
    """Lowest permission relevant to the registry entry."""

    PUBLIC = "public"
    READ = "read"
    WRITE = "write"
    MANAGE = "manage"
    ADMIN = "admin"


class ToolOperation(StrEnum):
    """Whether the handler is observational or may mutate state."""

    READ = "read"
    MUTATION = "mutation"


@dataclass(frozen=True, slots=True)
class ToolExposure:
    """One canonical tool plus every compatibility name for its handler."""

    canonical_name: str
    aliases: tuple[str, ...]
    audience: ToolAudience
    minimum_permission: ToolPermission
    operation: ToolOperation
    space_scope_argument: str | None = None


def _entry(
    canonical_name: str,
    *,
    aliases: tuple[str, ...] = (),
    audience: ToolAudience,
    permission: ToolPermission,
    operation: ToolOperation,
    space_scope_argument: str | None = None,
) -> ToolExposure:
    return ToolExposure(
        canonical_name=canonical_name,
        aliases=aliases,
        audience=audience,
        minimum_permission=permission,
        operation=operation,
        space_scope_argument=space_scope_argument,
    )


_CORE = ToolAudience.AGENT_CORE
_OPERATOR = ToolAudience.OPERATOR
_PUBLIC = ToolPermission.PUBLIC
_READ = ToolPermission.READ
_WRITE = ToolPermission.WRITE
_MANAGE = ToolPermission.MANAGE
_ADMIN = ToolPermission.ADMIN
_R = ToolOperation.READ
_M = ToolOperation.MUTATION


# Order is wire-significant for deterministic tools/list responses. Agent-core
# entries precede the hidden operator/compatibility entries, which stay
# registered and callable by exact name.
TOOL_EXPOSURES: tuple[ToolExposure, ...] = (
    # read discovery
    _entry("system_about", audience=_CORE, permission=_PUBLIC, operation=_R),
    _entry("system_health", audience=_CORE, permission=_PUBLIC, operation=_R),
    _entry("system_whoami", audience=_CORE, permission=_READ, operation=_R),
    _entry("space_list", audience=_CORE, permission=_READ, operation=_R),
    _entry(
        "space_info",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_rules",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_summary",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "short_read",
        aliases=("live_read",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "short_search",
        aliases=("live_search",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "mid_list",
        aliases=("bank_list",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "mid_read",
        aliases=("bank_read",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "mid_read_all",
        aliases=("bank_read_all",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "bank_consolidation_queues",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_ids",
    ),
    _entry(
        "bank_consolidation_status",
        audience=_CORE,
        permission=_READ,
        operation=_R,
    ),
    _entry(
        "bank_stale_spaces",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_ids",
    ),
    _entry(
        "long_query",
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "long_status",
        aliases=("graph_status",),
        audience=_CORE,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    # write discovery additions
    _entry(
        "short_note",
        aliases=("live_note",),
        audience=_CORE,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "long_push",
        aliases=("graph_push",),
        audience=_CORE,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    # write discovery addition restored by the P10 regression correction:
    # own-note consolidation has always had a write runtime floor.
    _entry(
        "mid_consolidate",
        aliases=("bank_consolidate",),
        audience=_CORE,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    # manage discovery additions; admin deliberately adds none
    _entry(
        "long_ingest",
        audience=_CORE,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_create",
        audience=_CORE,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry("token_create", audience=_CORE, permission=_MANAGE, operation=_M),
    _entry(
        "space_invite_token",
        audience=_CORE,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    # Hidden operator / compatibility surface
    _entry(
        "space_update",
        audience=_OPERATOR,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_update_rules",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_export",
        audience=_OPERATOR,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry(
        "space_delete",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "mid_write",
        aliases=("bank_write",),
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "mid_delete",
        aliases=("bank_delete",),
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "bank_repair",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "bank_compact",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "long_connect",
        aliases=("graph_connect",),
        audience=_OPERATOR,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "long_disconnect",
        aliases=("graph_disconnect",),
        audience=_OPERATOR,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "long_reindex",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "inference_self_test",
        audience=_OPERATOR,
        permission=_MANAGE,
        operation=_M,
    ),
    _entry(
        "backup_create",
        audience=_OPERATOR,
        permission=_WRITE,
        operation=_M,
        space_scope_argument="space_id",
    ),
    _entry(
        "backup_list",
        audience=_OPERATOR,
        permission=_READ,
        operation=_R,
        space_scope_argument="space_id",
    ),
    _entry("backup_restore", audience=_OPERATOR, permission=_MANAGE, operation=_M),
    _entry("backup_download", audience=_OPERATOR, permission=_READ, operation=_R),
    _entry("backup_delete", audience=_OPERATOR, permission=_MANAGE, operation=_M),
    _entry("admin_audit_recent", audience=_OPERATOR, permission=_ADMIN, operation=_R),
    _entry("admin_create_token", audience=_OPERATOR, permission=_ADMIN, operation=_M),
    _entry("admin_list_tokens", audience=_OPERATOR, permission=_ADMIN, operation=_R),
    _entry("admin_revoke_token", audience=_OPERATOR, permission=_ADMIN, operation=_M),
    _entry("admin_delete_token", audience=_OPERATOR, permission=_ADMIN, operation=_M),
    _entry("admin_purge_tokens", audience=_OPERATOR, permission=_ADMIN, operation=_M),
    _entry("admin_update_token", audience=_OPERATOR, permission=_ADMIN, operation=_M),
    _entry(
        "admin_bulk_update_tokens",
        audience=_OPERATOR,
        permission=_ADMIN,
        operation=_M,
    ),
    _entry(
        "admin_gc_notes",
        audience=_OPERATOR,
        permission=_ADMIN,
        operation=_M,
        space_scope_argument="space_id",
    ),
)


DISCOVERY_SCHEMA_BUDGET_BYTES = 64 * 1024

_PERMISSION_RANK: Mapping[ToolPermission, int] = MappingProxyType(
    {
        ToolPermission.PUBLIC: 0,
        ToolPermission.READ: 1,
        ToolPermission.WRITE: 2,
        ToolPermission.MANAGE: 3,
        ToolPermission.ADMIN: 4,
    }
)

# Independent frozen projection: validation compares the mutable registry seam
# against these P10-0 literals so an audience/floor/order edit fails startup.
_FROZEN_READ_NAMES = (
    "system_about",
    "system_health",
    "system_whoami",
    "space_list",
    "space_info",
    "space_rules",
    "space_summary",
    "short_read",
    "short_search",
    "mid_list",
    "mid_read",
    "mid_read_all",
    "bank_consolidation_queues",
    "bank_consolidation_status",
    "bank_stale_spaces",
    "long_query",
    "long_status",
)
_FROZEN_WRITE_NAMES = _FROZEN_READ_NAMES + (
    "short_note",
    "long_push",
    "mid_consolidate",
)
_FROZEN_MANAGE_NAMES = _FROZEN_WRITE_NAMES + (
    "long_ingest",
    "space_create",
    "token_create",
    "space_invite_token",
)
DISCOVERY_NAMES_BY_PERMISSION: Mapping[ToolPermission, tuple[str, ...]] = (
    MappingProxyType(
        {
            ToolPermission.READ: _FROZEN_READ_NAMES,
            ToolPermission.WRITE: _FROZEN_WRITE_NAMES,
            ToolPermission.MANAGE: _FROZEN_MANAGE_NAMES,
            ToolPermission.ADMIN: _FROZEN_MANAGE_NAMES,
        }
    )
)

_FROZEN_CORE_MINIMUMS: Mapping[str, ToolPermission] = MappingProxyType(
    {
        "system_about": _PUBLIC,
        "system_health": _PUBLIC,
        "system_whoami": _READ,
        "space_list": _READ,
        "space_info": _READ,
        "space_rules": _READ,
        "space_summary": _READ,
        "short_read": _READ,
        "short_search": _READ,
        "mid_list": _READ,
        "mid_read": _READ,
        "mid_read_all": _READ,
        "bank_consolidation_queues": _READ,
        "bank_consolidation_status": _READ,
        "bank_stale_spaces": _READ,
        "long_query": _READ,
        "long_status": _READ,
        "short_note": _WRITE,
        "long_push": _WRITE,
        "mid_consolidate": _WRITE,
        "long_ingest": _MANAGE,
        "space_create": _MANAGE,
        "token_create": _MANAGE,
        "space_invite_token": _MANAGE,
    }
)

_FROZEN_OPERATOR_MINIMUMS: Mapping[str, ToolPermission] = MappingProxyType(
    {
        "space_update": _WRITE,
        "space_update_rules": _MANAGE,
        "space_export": _READ,
        "space_delete": _MANAGE,
        "mid_write": _MANAGE,
        "mid_delete": _MANAGE,
        "bank_repair": _MANAGE,
        "bank_compact": _MANAGE,
        "long_connect": _WRITE,
        "long_disconnect": _WRITE,
        "long_reindex": _MANAGE,
        "inference_self_test": _MANAGE,
        "backup_create": _WRITE,
        "backup_list": _READ,
        "backup_restore": _MANAGE,
        "backup_download": _READ,
        "backup_delete": _MANAGE,
        "admin_audit_recent": _ADMIN,
        "admin_create_token": _ADMIN,
        "admin_list_tokens": _ADMIN,
        "admin_revoke_token": _ADMIN,
        "admin_delete_token": _ADMIN,
        "admin_purge_tokens": _ADMIN,
        "admin_update_token": _ADMIN,
        "admin_bulk_update_tokens": _ADMIN,
        "admin_gc_notes": _ADMIN,
    }
)

_FROZEN_READ_OPERATION_NAMES = frozenset(
    {
        *_FROZEN_READ_NAMES,
        "space_export",
        "backup_list",
        "backup_download",
        "admin_audit_recent",
        "admin_list_tokens",
    }
)

_FROZEN_SPACE_SCOPE_ARGUMENTS: Mapping[str, str] = MappingProxyType(
    {
        "space_info": "space_id",
        "space_rules": "space_id",
        "space_summary": "space_id",
        "short_read": "space_id",
        "short_search": "space_id",
        "mid_list": "space_id",
        "mid_read": "space_id",
        "mid_read_all": "space_id",
        "bank_consolidation_queues": "space_ids",
        "bank_stale_spaces": "space_ids",
        "long_query": "space_id",
        "long_status": "space_id",
        "short_note": "space_id",
        "long_push": "space_id",
        "mid_consolidate": "space_id",
        "long_ingest": "space_id",
        "space_create": "space_id",
        "space_update": "space_id",
        "space_update_rules": "space_id",
        "space_export": "space_id",
        "space_delete": "space_id",
        "mid_write": "space_id",
        "mid_delete": "space_id",
        "bank_repair": "space_id",
        "bank_compact": "space_id",
        "long_connect": "space_id",
        "long_disconnect": "space_id",
        "long_reindex": "space_id",
        "backup_create": "space_id",
        "backup_list": "space_id",
        "admin_gc_notes": "space_id",
        "space_invite_token": "space_id",
    }
)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def registered_exposure_names(
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> tuple[str, ...]:
    """Return every canonical and compatibility name exactly once in registry order."""

    return tuple(
        name
        for entry in registry
        for name in (entry.canonical_name, *entry.aliases)
    )


def exposure_by_registered_name(
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> Mapping[str, ToolExposure]:
    """Build a read-only lookup for canonical names and aliases."""

    return MappingProxyType(
        {
            name: entry
            for entry in registry
            for name in (entry.canonical_name, *entry.aliases)
        }
    )


def effective_permission(
    token_info: Mapping[str, Any] | None,
) -> ToolPermission | None:
    """Collapse a token's permission set to the hierarchical effective level.

    Malformed, duplicate, empty, or unknown permissions fail closed.  A normal
    request has already passed token-store validation; the defensive checks here
    protect discovery from synthetic/corrupt context state.
    """

    if not isinstance(token_info, Mapping):
        return None
    raw = token_info.get("permissions")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    if any(not isinstance(item, str) for item in raw) or len(set(raw)) != len(raw):
        return None
    try:
        permissions = [ToolPermission(item) for item in raw]
    except (TypeError, ValueError):
        return None
    return max(permissions, key=_PERMISSION_RANK.__getitem__)


def discovery_names_for_permission(
    permission: ToolPermission | str,
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> tuple[str, ...]:
    """Project canonical agent-core names for one hierarchical permission."""

    try:
        effective = ToolPermission(permission)
    except (TypeError, ValueError):
        return ()
    if effective is ToolPermission.PUBLIC:
        # MCP authentication has no public discovery tier.  The two public-call
        # handlers enter discovery at the lowest connectable tier, ``read``.
        return ()
    rank = _PERMISSION_RANK[effective]
    return tuple(
        entry.canonical_name
        for entry in registry
        if entry.audience is ToolAudience.AGENT_CORE
        and rank
        >= max(_PERMISSION_RANK[entry.minimum_permission], _PERMISSION_RANK[_READ])
    )


def discovery_names_for_token(
    token_info: Mapping[str, Any] | None,
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> tuple[str, ...]:
    permission = effective_permission(token_info)
    if permission is None:
        return ()
    return discovery_names_for_permission(permission, registry)


def console_capability_manifest(
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> dict[str, dict[str, str | None]]:
    """Return flattened metadata for all console-callable registered names.

    This is a rendering/coverage aid, not authorization. Space allowlists,
    bootstrap restrictions, conditional modes, and every handler guard remain
    authoritative at call time.
    """

    return {
        name: {
            "canonical": entry.canonical_name,
            "audience": entry.audience.value,
            "minimum_permission": entry.minimum_permission.value,
            "operation": entry.operation.value,
            "space_scope_argument": entry.space_scope_argument,
        }
        for entry in registry
        for name in (entry.canonical_name, *entry.aliases)
    }


def exposure_manifest(
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
) -> dict[str, Any]:
    """Machine-readable source for checked-in docs and discovery fixtures."""

    return {
        "version": 1,
        "registered_total": len(registered_exposure_names(registry)),
        "registry_entries": len(registry),
        "serialized_schema_budget_bytes": DISCOVERY_SCHEMA_BUDGET_BYTES,
        "discovery": {
            permission.value: list(discovery_names_for_permission(permission, registry))
            for permission in (
                ToolPermission.READ,
                ToolPermission.WRITE,
                ToolPermission.MANAGE,
                ToolPermission.ADMIN,
            )
        },
        "entries": [
            {
                "canonical_name": entry.canonical_name,
                "aliases": list(entry.aliases),
                "audience": entry.audience.value,
                "minimum_permission": entry.minimum_permission.value,
                "operation": entry.operation.value,
                "space_scope_argument": entry.space_scope_argument,
            }
            for entry in registry
        ],
    }


def validate_tool_exposure_registry(
    mcp: FastMCP,
    *,
    registry: Sequence[ToolExposure] = TOOL_EXPOSURES,
    declared_registration_count: int | None = None,
) -> None:
    """Validate totality, uniqueness, classification, and alias identity.

    Called after all direct and compatibility registrations.  Any ambiguity is
    a startup error: discovery must never silently infer a partial surface.
    """

    # Do not restate global registration counts here. The checks below preserve
    # relative consistency across frozen classification, alias mapping, unique
    # ownership, and live FastMCP names. The canonical test fixture owns the
    # deliberately test-tier-only absolute cardinality pin.
    entries = tuple(registry)
    owners: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, ToolExposure):
            raise RuntimeError("ToolExposure registry contains a non-ToolExposure entry")
        if not isinstance(entry.audience, ToolAudience):
            raise RuntimeError(f"{entry.canonical_name}: invalid ToolExposure audience")
        if not isinstance(entry.minimum_permission, ToolPermission):
            raise RuntimeError(
                f"{entry.canonical_name}: invalid ToolExposure minimum permission"
            )
        if not isinstance(entry.operation, ToolOperation):
            raise RuntimeError(f"{entry.canonical_name}: invalid ToolExposure operation")
        names = (entry.canonical_name, *entry.aliases)
        if any(
            not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name)
            for name in names
        ):
            raise RuntimeError(f"{entry.canonical_name!r}: invalid ToolExposure name")
        if entry.canonical_name in entry.aliases:
            raise RuntimeError(
                f"{entry.canonical_name}: canonical name also appears as its alias"
            )
        if len(set(entry.aliases)) != len(entry.aliases):
            raise RuntimeError(f"{entry.canonical_name}: duplicate aliases")
        if entry.space_scope_argument is not None and (
            not isinstance(entry.space_scope_argument, str)
            or not _TOOL_NAME_RE.fullmatch(entry.space_scope_argument)
        ):
            raise RuntimeError(
                f"{entry.canonical_name}: invalid space_scope_argument"
            )
        for name in names:
            previous = owners.get(name)
            if previous is not None:
                raise RuntimeError(
                    f"ToolExposure name collision {name!r}: owned by "
                    f"{previous!r} and {entry.canonical_name!r}"
                )
            owners[name] = entry.canonical_name

    core_entries = [e for e in entries if e.audience is ToolAudience.AGENT_CORE]
    core_names = tuple(entry.canonical_name for entry in core_entries)
    frozen_core_names = DISCOVERY_NAMES_BY_PERMISSION[ToolPermission.ADMIN]
    if core_names != frozen_core_names:
        raise RuntimeError(
            "ToolExposure agent_core classification/order contradicts the "
            "frozen P10 discovery contract"
        )
    frozen_names = set(_FROZEN_CORE_MINIMUMS) | set(_FROZEN_OPERATOR_MINIMUMS)
    if frozen_names != {entry.canonical_name for entry in entries}:
        raise RuntimeError("frozen ToolExposure classification is not total")
    for entry in entries:
        expected_audience = (
            ToolAudience.AGENT_CORE
            if entry.canonical_name in _FROZEN_CORE_MINIMUMS
            else ToolAudience.OPERATOR
        )
        expected_permission = (
            _FROZEN_CORE_MINIMUMS.get(entry.canonical_name)
            or _FROZEN_OPERATOR_MINIMUMS[entry.canonical_name]
        )
        expected_operation = (
            ToolOperation.READ
            if entry.canonical_name in _FROZEN_READ_OPERATION_NAMES
            else ToolOperation.MUTATION
        )
        expected_scope = _FROZEN_SPACE_SCOPE_ARGUMENTS.get(entry.canonical_name)
        if entry.audience is not expected_audience:
            raise RuntimeError(
                f"{entry.canonical_name}: exposure audience misclassified; "
                f"expected {expected_audience}, got {entry.audience}"
            )
        if entry.minimum_permission is not expected_permission:
            raise RuntimeError(
                f"{entry.canonical_name}: exposure permission misclassified; "
                f"expected {expected_permission}, got {entry.minimum_permission}"
            )
        if entry.operation is not expected_operation:
            raise RuntimeError(
                f"{entry.canonical_name}: operation misclassified; "
                f"expected {expected_operation}, got {entry.operation}"
            )
        if entry.space_scope_argument != expected_scope:
            raise RuntimeError(
                f"{entry.canonical_name}: scope argument misclassified; "
                f"expected {expected_scope!r}, got {entry.space_scope_argument!r}"
            )
    for permission, expected in DISCOVERY_NAMES_BY_PERMISSION.items():
        actual = discovery_names_for_permission(permission, entries)
        if actual != expected:
            raise RuntimeError(
                f"ToolExposure {permission.value} projection contradicts frozen "
                f"discovery contract: {actual!r} != {expected!r}"
            )

    from .aliases import ALIAS_MAP

    registry_alias_map = {
        alias: entry.canonical_name for entry in entries for alias in entry.aliases
    }
    if registry_alias_map != ALIAS_MAP:
        raise RuntimeError(
            "ToolExposure canonical/alias mapping contradicts ALIAS_MAP"
        )

    tools = getattr(mcp._tool_manager, "_tools", {})
    registered_names = set(tools)
    exposure_names = set(owners)
    if registered_names != exposure_names:
        missing = sorted(registered_names - exposure_names)
        unregistered = sorted(exposure_names - registered_names)
        raise RuntimeError(
            "ToolExposure registry is not total for the registered surface; "
            f"missing_entries={missing}, registry_only={unregistered}"
        )
    if declared_registration_count is not None and (
        declared_registration_count != len(registered_names)
    ):
        raise RuntimeError(
            "Tool registration count contradicts the tool manager; possible "
            f"duplicate overwrite ({declared_registration_count} declared, "
            f"{len(registered_names)} present)"
        )

    metadata_fields = (
        "description",
        "parameters",
        "output_schema",
        "annotations",
        "title",
        "icons",
        "meta",
    )
    for entry in entries:
        canonical = tools[entry.canonical_name]
        readonly = getattr(canonical.annotations, "readOnlyHint", None)
        if entry.operation is ToolOperation.READ and readonly is not True:
            raise RuntimeError(
                f"{entry.canonical_name}: registry says read but handler is not readOnly"
            )
        if entry.operation is ToolOperation.MUTATION and readonly is True:
            raise RuntimeError(
                f"{entry.canonical_name}: registry says mutation but handler is readOnly"
            )
        if entry.space_scope_argument is not None:
            properties = canonical.parameters.get("properties", {})
            if entry.space_scope_argument not in properties:
                raise RuntimeError(
                    f"{entry.canonical_name}: space scope argument "
                    f"{entry.space_scope_argument!r} missing from input schema"
                )
        for alias_name in entry.aliases:
            alias = tools[alias_name]
            if alias.fn is not canonical.fn:
                raise RuntimeError(
                    f"{alias_name}: alias handler differs from {entry.canonical_name}"
                )
            for field in metadata_fields:
                if getattr(alias, field) != getattr(canonical, field):
                    raise RuntimeError(
                        f"{alias_name}: alias {field} differs from "
                        f"{entry.canonical_name}"
                    )


class HivemindFastMCP(FastMCP):
    """FastMCP whose low-level list handler is permission-aware by construction.

    ``FastMCP.__init__`` binds ``self.list_tools`` while setting up protocol
    handlers.  Overriding the method on the class (instead of monkey-patching an
    instance later) guarantees the low-level ``tools/list`` handler receives the
    Hivemind projection.
    """

    async def list_tools(self) -> list[MCPTool]:
        from ..auth.context import get_mcp_request_token_info

        complete = await super().list_tools()
        by_name = {tool.name: tool for tool in complete}
        if len(by_name) != len(complete):
            raise RuntimeError("duplicate tool names in FastMCP list_tools result")
        has_mcp_context, token_info = get_mcp_request_token_info()
        if not has_mcp_context or token_info is None:
            return []
        names = discovery_names_for_token(token_info)
        missing = [name for name in names if name not in by_name]
        if missing:
            raise RuntimeError(
                f"ToolExposure discovery references unregistered tools: {missing}"
            )
        return [by_name[name] for name in names]
