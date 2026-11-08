# -*- coding: utf-8 -*-
"""Selective admission gate around every Graph tool handler (#1420).

One wrapper per handler covers MCP dispatch and the ``/api/tool`` console
proxy, which resolves the very same handler objects. Graph has no exposure
registry, so each tool is classified here. Fail closed: a tool missing from
both sets is a MUTATION, and tests pin that every registered tool is listed.

Ordering contract for the consumer (not enforced here, Core and Graph are
separate processes): pause and drain Core first, then Graph. Every Core call
into Graph is made under a Core ticket, so a drained Core cannot have a nested
call left to refuse; resume in the reverse order.
"""

from __future__ import annotations

import functools
import inspect

from .selective_maintenance import MaintenanceRefused, get_controller, validate_scope

READ_TOOLS = frozenset({
    "memory_list", "memory_stats", "memory_search", "question_answer",
    "memory_query", "memory_get_context", "memory_graph",
    "ingest_job_status", "ingest_job_list",
    "document_list", "document_get",
    "ontology_list", "ontology_validate", "ontology_get", "ontology_export",
    "system_about", "system_health", "system_whoami",
    "backup_list", "backup_download", "admin_list_tokens",
    # Diagnostic only: its S3 probe key is exempt in the physical gate.
    "storage_check",
})
MUTATION_TOOLS = frozenset({
    "memory_create", "memory_update", "memory_delete", "memory_reindex",
    "memory_ingest", "memory_ingest_async", "memory_ingest_batch_async",
    "ingest_job_cancel",
    "admin_create_token", "admin_revoke_token", "admin_update_token",
    "document_delete",
    "ontology_import", "ontology_update", "ontology_delete",
    "storage_cleanup",
    "backup_create", "backup_restore", "backup_delete", "backup_restore_archive",
})
REFUSAL = {"status": "error", "error": "maintenance_admission_closed",
           "message": "Maintenance in progress; retry later"}


def kind_of(name: str) -> str:
    return "read" if name in READ_TOOLS else "mutation"


def _scope(arguments: dict) -> str:
    try:
        return validate_scope(arguments.get("memory_id"))
    except MaintenanceRefused:
        # Missing or malformed: whole deployment, and the handler keeps its
        # own, unchanged validation error.
        return "*"


def _wrap(fn, kind: str):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def gated(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs).arguments
        try:
            with get_controller().operation(kind, _scope(bound)):
                return await fn(*args, **kwargs)
        except MaintenanceRefused as refusal:
            if refusal.code != "maintenance_admission_closed":
                raise
            return dict(REFUSAL)

    return gated


def install(mcp) -> int:
    """Wrap every registered handler once. Returns the number wrapped."""
    count = 0
    for name, tool in mcp._tool_manager._tools.items():
        if getattr(tool.fn, "_selective_gate", False):
            continue
        gated = _wrap(tool.fn, kind_of(name))
        gated._selective_gate = True
        tool.fn = gated
        count += 1
    return count
