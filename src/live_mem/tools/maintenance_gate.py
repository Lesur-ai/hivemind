# -*- coding: utf-8 -*-
"""Selective admission gate around every registered Core tool (#1420).

One wrapper over each tool's handler covers both the MCP dispatch and the
``/api/tool`` console proxy, which calls the same handler directly. Kinds come
from the existing exposure registry (``ToolOperation``), never from HTTP
methods: the MCP endpoint mixes reads and writes.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from ..core.selective_maintenance import (
    MaintenanceRefused,
    get_controller,
    validate_scope,
)
from .exposure import ToolOperation, exposure_by_registered_name

# SHORT note writers stay admitted until the explicit ``all`` boundary; every
# other mutation (consolidation, compaction, structural tools) closes earlier.
SHORT_WRITE_TOOLS = frozenset({"short_note"})
REFUSAL = {"status": "error", "error": "maintenance_admission_closed",
           "message": "Maintenance in progress; retry later"}


def _kind(canonical: str, operation: ToolOperation) -> str:
    if operation is ToolOperation.READ:
        return "read"
    return "short" if canonical in SHORT_WRITE_TOOLS else "mutation"


def _scope(space_argument: str | None, arguments: dict[str, Any]) -> str:
    value = arguments.get(space_argument) if space_argument else None
    try:
        return validate_scope(value)
    except MaintenanceRefused:
        # Missing or malformed id: assume the whole deployment (the conservative
        # overlap) and let the handler produce its own, unchanged validation error.
        return "*"


def _wrap(fn, kind: str, space_argument: str | None):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def gated(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs).arguments
        try:
            scope = _scope(space_argument, bound)
            with get_controller().operation(kind, scope):
                return await fn(*args, **kwargs)
        except MaintenanceRefused as refusal:
            if refusal.code != "maintenance_admission_closed":
                raise
            return dict(REFUSAL)

    return gated


def install(mcp) -> int:
    """Wrap every registered handler once. Returns the number wrapped."""
    exposures = exposure_by_registered_name()
    count = 0
    # An alias must keep sharing its canonical handler object (the registry
    # pins that identity), so one wrapper is built per original function.
    wrapped: dict[int, Any] = {}
    for name, tool in mcp._tool_manager._tools.items():
        if getattr(tool.fn, "_selective_gate", False):
            continue
        original = tool.fn
        if id(original) not in wrapped:
            entry = exposures[name]
            gated = _wrap(
                original,
                _kind(entry.canonical_name, entry.operation),
                entry.space_scope_argument,
            )
            gated._selective_gate = True
            wrapped[id(original)] = gated
        tool.fn = wrapped[id(original)]
        count += 1
    return count
