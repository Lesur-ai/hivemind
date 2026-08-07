# -*- coding: utf-8 -*-
"""
CLI Click — Scriptable commands for Hivemind.

Each command calls an MCP tool via MCPClient and displays via display.py.

Usage :
    python scripts/mcp_cli.py health
    python scripts/mcp_cli.py space list
    python scripts/mcp_cli.py live note <space_id> <category> <content>
    python scripts/mcp_cli.py bank consolidate <space_id>
    python scripts/mcp_cli.py shell
"""

import asyncio
import re
import click
from . import BASE_URL, TOKEN
from .client import MCPClient
from .display import (
    console,
    show_error,
    show_success,
    show_warning,
    show_json,
    show_health_result,
    show_whoami_result,
    show_about_result,
    show_space_created,
    show_space_create_recovery,
    show_space_delete_recovery,
    show_space_invite_result,
    show_space_list,
    show_space_info,
    show_rules,
    show_notes,
    show_bank_list,
    show_bank_content,
    show_consolidation_result,
    show_graph_connected,
    show_graph_status,
    show_graph_push_result,
    show_graph_disconnected,
    show_graph_local,
    show_token_created,
    show_token_updated,
    show_token_list,
    show_backup_created,
    show_backup_list,
)


# ─────────────────────────────────────────────────────────────
# Helper to run async commands
# ─────────────────────────────────────────────────────────────


def _run_tool(
    ctx,
    tool_name,
    args,
    on_success,
    json_flag=False,
    on_recovery_required=None,
):
    """Common helper: calls an MCP tool and displays the result."""

    async def _run():
        try:
            client = MCPClient(ctx.obj["url"], ctx.obj["token"])
            result = await client.call_tool(tool_name, args)
            if json_flag:
                show_json(result)
            elif (
                result.get("status") == "partial"
                and result.get("recovery_required") is True
                and on_recovery_required is not None
            ):
                on_recovery_required(result)
            elif result.get("status") in (
                "ok",
                "healthy",
                "degraded",
                "created",
                "deleted",
                "grants_cleaned",
                "connected",
                "disconnected",
                "running",
                "queued",
            ):
                on_success(result)
            else:
                show_error(
                    result.get("message", f"Error: {result.get('status', '?')}")
                )
        except Exception as e:
            show_error(f"Connection failed: {e}")

    asyncio.run(_run())


def _run_token_create(
    ctx,
    *,
    name,
    permissions,
    space_ids,
    expires_in_days,
    email,
    json_flag=False,
):
    """Route token creation from the caller's live capability, not target shape.

    Bootstrap/admin keeps the historical ``admin_create_token`` CRUD path for
    every target profile (including initial ``space_ids`` for non-admin targets).
    Admin targets are global and v2 forbids a dormant allowlist, so the CLI
    rejects that meaningless combination. A persisted manager
    uses the bounded ``token_create`` path and can never request admin or an
    initial scope. This identity probe is read-only and prevents bootstrap from
    being accidentally routed to a tool that deliberately requires a stored
    actor hash.
    """

    async def _run():
        client = MCPClient(ctx.obj["url"], ctx.obj["token"])
        try:
            identity = await client.call_tool("system_whoami", {})
        except Exception as exc:
            show_error(f"Connection failed: {exc}")
            return

        if not isinstance(identity, dict) or identity.get("status") != "ok":
            show_error(
                (identity or {}).get("message", "Unable to determine caller identity.")
                if isinstance(identity, dict)
                else "Unable to determine caller identity."
            )
            return

        caller_permissions = identity.get("permissions", [])
        is_global_admin = (
            identity.get("auth_type") == "bootstrap"
            or "admin" in caller_permissions
        )
        is_manager = "manage" in caller_permissions
        admin_profile = "admin" in permissions.split(",")
        if not is_global_admin and not is_manager:
            raise click.UsageError(
                "Creating a token requires a manage or admin/bootstrap caller."
            )
        if not is_global_admin and admin_profile:
            raise click.UsageError(
                "Creating an admin token requires an admin/bootstrap caller."
            )
        if not is_global_admin and space_ids:
            raise click.UsageError(
                "--space-ids requires an admin/bootstrap caller. Manager-created "
                "tokens start without space access; grant one with "
                "`space invite <space_id> <full_hash>`."
            )
        if admin_profile and space_ids:
            raise click.UsageError(
                "--space-ids cannot be combined with an admin target. Admin "
                "access is global and token-store v2 persists space_ids=[]."
            )

        tool_name = "admin_create_token" if is_global_admin else "token_create"
        args = {
            "name": name,
            "permissions": permissions,
            "expires_in_days": expires_in_days,
            "email": email,
        }
        if is_global_admin:
            args["space_ids"] = space_ids
        try:
            result = await client.call_tool(tool_name, args)
        except Exception as exc:
            show_error(f"Connection failed: {exc}")
            return

        if json_flag:
            show_json(result)
        elif result.get("status") == "created" or (
            result.get("status") == "partial"
            and result.get("recovery_required") is True
            and result.get("token")
            and result.get("token_hash")
        ):
            show_token_created(result)
        else:
            show_error(result.get("message", f"Error: {result.get('status', '?')}"))

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────
# Root group
# ─────────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--url", "-u", envvar=["MCP_URL"], default=BASE_URL, help="MCP server URL"
)
@click.option(
    "--token",
    "-t",
    envvar=["MCP_TOKEN"],
    default=TOKEN,
    help="Authentication token",
)
@click.pass_context
def cli(ctx, url, token):
    """🧠 Hivemind — MCP server CLI."""
    ctx.ensure_object(dict)
    ctx.obj["url"] = url
    ctx.obj["token"] = token


# ─────────────────────────────────────────────────────────────
# System
# ─────────────────────────────────────────────────────────────


@cli.command("health")
@click.option("--json", "-j", "jflag", is_flag=True, help="Raw JSON")
@click.pass_context
def health_cmd(ctx, jflag):
    """❤️  Service health status."""
    import httpx

    try:
        url = ctx.obj["url"].rstrip("/") + "/health"
        resp = httpx.get(url, timeout=10)
        result = resp.json()
        if jflag:
            show_json(result)
        else:
            show_health_result(result)
    except Exception as e:
        show_error(f"Connection failed: {e}")


@cli.command("whoami")
@click.option("--json", "-j", "jflag", is_flag=True, help="Raw JSON")
@click.pass_context
def whoami_cmd(ctx, jflag):
    """👤 Current token identity."""
    _run_tool(ctx, "system_whoami", {}, show_whoami_result, jflag)


@cli.command("about")
@click.option("--json", "-j", "jflag", is_flag=True, help="Raw JSON")
@click.pass_context
def about_cmd(ctx, jflag):
    """ℹ️  Service information."""
    _run_tool(ctx, "system_about", {}, show_about_result, jflag)


# ─────────────────────────────────────────────────────────────
# Space (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("space")
def space_grp():
    """📂 Memory space management."""
    pass


@space_grp.command("create")
@click.argument("space_id")
@click.option("--description", "-d", default="", help="Space description")
@click.option(
    "--rules-file", "-r", type=click.Path(exists=True), help="Rules file (.md)"
)
@click.option("--rules", default="", help="Inline rules content")
@click.option("--owner", "-o", default="", help="Owner")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_create_cmd(ctx, space_id, description, rules_file, rules, owner, jflag):
    """Create a memory space (manage permission required)."""
    if rules_file:
        rules = open(rules_file).read()
    if not rules:
        show_error("Rules required (--rules-file or --rules)")
        return
    _run_tool(
        ctx,
        "space_create",
        {
            "space_id": space_id,
            "description": description,
            "rules": rules,
            "owner": owner,
        },
        show_space_created,
        jflag,
        on_recovery_required=show_space_create_recovery,
    )


@space_grp.command("invite")
@click.argument("space_id")
@click.argument("token_hash")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_invite_cmd(ctx, space_id, token_hash, jflag):
    """Invite a token to SPACE_ID using its complete SHA-256 hash.

    \b
    The target is additive and idempotent. Prefix shortcuts are rejected.
    Example:
      space invite project-a sha256:0123456789abcdef...<64 hex chars>
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", token_hash):
        raise click.UsageError(
            "TOKEN_HASH must be canonical: 'sha256:' followed by exactly "
            "64 lowercase hexadecimal characters."
        )
    _run_tool(
        ctx,
        "space_invite_token",
        {"space_id": space_id, "token_hash": token_hash},
        show_space_invite_result,
        jflag,
    )


@space_grp.command("update")
@click.argument("space_id")
@click.option(
    "--description",
    "-d",
    default="",
    help="New description (empty = no change)",
)
@click.option(
    "--owner", "-o", default="", help="New owner (empty = no change)"
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_update_cmd(ctx, space_id, description, owner, jflag):
    """Updates the description and/or owner of a space.

    Use ``space update-rules`` to change rules separately.

    Examples:
      space update mon-projet -d "Nouvelle description"
      space update mon-projet -o "Nouveau Owner"
      space update mon-projet -d "Desc" -o "Owner"
    """
    args = {"space_id": space_id}
    if description:
        args["description"] = description
    if owner:
        args["owner"] = owner
    if not description and not owner:
        show_error("Nothing to update. Use --description/-d and/or --owner/-o.")
        return
    from .display import show_space_updated

    _run_tool(ctx, "space_update", args, show_space_updated, jflag)


@space_grp.command("update-rules")
@click.argument("space_id")
@click.option(
    "--rules-file",
    "-f",
    required=True,
    type=click.Path(exists=True),
    help="Markdown rules file",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_update_rules_cmd(ctx, space_id, rules_file, jflag):
    """📜 Updates a space's rules (manage permission required).

    \b
    Examples:
      space update-rules mon-projet -f RULES/live-mem.standard.memory.bank.md
    """
    content = open(rules_file, "r", encoding="utf-8").read()
    if not content.strip():
        show_error("The rules file is empty.")
        return
    from .display import show_rules_updated

    _run_tool(
        ctx,
        "space_update_rules",
        {"space_id": space_id, "rules": content},
        show_rules_updated,
        jflag,
    )


@space_grp.command("list")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_list_cmd(ctx, jflag):
    """List spaces."""
    _run_tool(ctx, "space_list", {}, show_space_list, jflag)


@space_grp.command("info")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_info_cmd(ctx, space_id, jflag):
    """Detailed space info."""
    _run_tool(ctx, "space_info", {"space_id": space_id}, show_space_info, jflag)


@space_grp.command("rules")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_rules_cmd(ctx, space_id, jflag):
    """Read the rules of a space."""
    _run_tool(ctx, "space_rules", {"space_id": space_id}, show_rules, jflag)


@space_grp.command("summary")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def space_summary_cmd(ctx, space_id, jflag):
    """📋 Full synthesis (rules + bank + stats)."""
    _run_tool(ctx, "space_summary", {"space_id": space_id}, show_json, jflag)


@space_grp.command("export")
@click.argument("space_id")
@click.pass_context
def space_export_cmd(ctx, space_id):
    """📦 Export a space as tar.gz (base64)."""
    _run_tool(ctx, "space_export", {"space_id": space_id}, show_json, True)


@space_grp.command("delete")
@click.argument("space_id")
@click.option("--confirm", is_flag=True, help="Confirm deletion")
@click.option(
    "--recover-access-grants",
    is_flag=True,
    help=(
        "Clean grants for a known older/interrupted deletion whose prefix is "
        "already empty"
    ),
)
@click.pass_context
def space_delete_cmd(ctx, space_id, confirm, recover_access_grants):
    """⚠️ Delete a space and remove its token grants (irreversible)."""
    args = {
        "space_id": space_id,
        "confirm": confirm,
    }
    if recover_access_grants:
        args["recover_access_grants"] = True
    _run_tool(
        ctx,
        "space_delete",
        args,
        lambda r: show_success(
            (
                f"Access grants for '{space_id}' cleaned "
                f"({r.get('access_grants_removed', 0)} grants)"
                if r.get("status") == "grants_cleaned"
                else (
                    f"Space '{space_id}' deleted "
                    f"({r.get('files_deleted', 0)} files, "
                    f"{r.get('access_grants_removed', 0)} grants)"
                )
            )
        ),
        on_recovery_required=show_space_delete_recovery,
    )


# ─────────────────────────────────────────────────────────────
# Live (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("live")
def live_grp():
    """📝 Real-time notes."""
    pass


@live_grp.command("note")
@click.argument("space_id")
@click.argument("category")
@click.argument("content")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def live_note_cmd(ctx, space_id, category, content, tags, jflag):
    """Write a note (agent = token name, always)."""
    _run_tool(
        ctx,
        "short_note",
        {
            "space_id": space_id,
            "category": category,
            "content": content,
            "tags": tags,
        },
        lambda r: show_success(f"Note created: {r.get('filename', '?')}"),
        jflag,
    )


@live_grp.command("read")
@click.argument("space_id")
@click.option("--limit", "-l", default=50, help="Max count")
@click.option("--category", "-c", default="", help="Filter by category")
@click.option("--agent", "-a", default="", help="Filter by agent")
@click.option("--since", default="", help="Notes after this ISO date")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def live_read_cmd(ctx, space_id, limit, category, agent, since, jflag):
    """Read live notes."""
    _run_tool(
        ctx,
        "short_read",
        {
            "space_id": space_id,
            "limit": limit,
            "category": category,
            "agent": agent,
            "since": since,
        },
        show_notes,
        jflag,
    )


@live_grp.command("search")
@click.argument("space_id")
@click.argument("query")
@click.option("--limit", "-l", default=20)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def live_search_cmd(ctx, space_id, query, limit, jflag):
    """Search in notes."""
    _run_tool(
        ctx,
        "short_search",
        {
            "space_id": space_id,
            "query": query,
            "limit": limit,
        },
        show_notes,
        jflag,
    )


# ─────────────────────────────────────────────────────────────
# Bank (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("bank")
def bank_grp():
    """📘 Consolidated Memory Bank."""
    pass


@bank_grp.command("read")
@click.argument("space_id")
@click.argument("filename")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_read_cmd(ctx, space_id, filename, jflag):
    """Read a bank file."""
    _run_tool(
        ctx,
        "bank_read",
        {"space_id": space_id, "filename": filename},
        show_bank_content,
        jflag,
    )


@bank_grp.command("read-all")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_read_all_cmd(ctx, space_id, jflag):
    """Read the entire bank."""

    def _show(r):
        for f in r.get("files", []):
            show_bank_content(f)

    _run_tool(ctx, "bank_read_all", {"space_id": space_id}, _show, jflag)


@bank_grp.command("list")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_list_cmd(ctx, space_id, jflag):
    """List bank files."""
    _run_tool(ctx, "bank_list", {"space_id": space_id}, show_bank_list, jflag)


@bank_grp.command("consolidate")
@click.argument("space_id")
@click.option(
    "--all-agents",
    is_flag=True,
    help="Explicitly consolidate all agents' notes (manage/admin required).",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_consolidate_cmd(ctx, space_id, all_agents, jflag):
    """🧠 Enqueue LLM consolidation (async, returns job_id)."""
    args = {"space_id": space_id}
    if all_agents:
        args["agent"] = ""
    _run_tool(
        ctx,
        "bank_consolidate",
        args,
        show_consolidation_result,
        jflag,
    )


@bank_grp.command("consolidation-status")
@click.argument("job_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_consolidation_status_cmd(ctx, job_id, jflag):
    """🔄 Track an in-memory consolidation job."""
    from .display import show_consolidation_job

    _run_tool(
        ctx,
        "bank_consolidation_status",
        {"job_id": job_id},
        show_consolidation_job,
        jflag,
    )


@bank_grp.command("consolidation-queues")
@click.argument("space_ids", default="")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_consolidation_queues_cmd(ctx, space_ids, jflag):
    """🔄 Show consolidation lanes per space."""
    from .display import show_consolidation_queues

    _run_tool(
        ctx,
        "bank_consolidation_queues",
        {"space_ids": space_ids},
        show_consolidation_queues,
        jflag,
    )


@bank_grp.command("stale-spaces")
@click.option(
    "--min-notes",
    "min_notes",
    type=int,
    default=5,
    show_default=True,
    help="Minimum number of unconsolidated live notes to flag a space.",
)
@click.option(
    "--min-age-days",
    "min_age_days",
    type=int,
    default=5,
    show_default=True,
    help="Minimum age (days) of the oldest note to flag a space.",
)
@click.option(
    "--space-ids",
    "space_ids",
    default="",
    help="CSV of spaces to inspect (default: all accessible).",
)
@click.option(
    "--consolidate",
    is_flag=True,
    help="After listing, enqueue bank_consolidate on each stale space.",
)
@click.option(
    "--all-agents",
    is_flag=True,
    help=(
        "With --consolidate, explicitly consolidate all agents' notes "
        "(manage/admin required)."
    ),
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_stale_spaces_cmd(
    ctx, min_notes, min_age_days, space_ids, consolidate, all_agents, jflag
):
    """🚨 List spaces with too many unconsolidated notes (optionally trigger)."""
    from .display import show_stale_spaces

    if all_agents and not consolidate:
        raise click.UsageError("--all-agents requires --consolidate")

    async def _run():
        try:
            client = MCPClient(ctx.obj["url"], ctx.obj["token"])
            result = await client.call_tool(
                "bank_stale_spaces",
                {
                    "min_notes": min_notes,
                    "min_age_days": min_age_days,
                    "space_ids": space_ids,
                },
            )
            if jflag:
                show_json(result)
            elif result.get("status") == "ok":
                show_stale_spaces(result)
            else:
                show_error(result.get("message", "?"))
                return

            if not consolidate:
                return
            stale = result.get("spaces", [])
            if not stale:
                return
            for entry in stale:
                sid = entry.get("space_id")
                if not sid:
                    continue
                consolidate_args = {"space_id": sid}
                if all_agents:
                    consolidate_args["agent"] = ""
                job = await client.call_tool("bank_consolidate", consolidate_args)
                if jflag:
                    show_json(job)
                elif job.get("status") in ("running", "queued"):
                    show_consolidation_result(job)
                else:
                    show_error(
                        f"{sid}: {job.get('message', job.get('status', '?'))}"
                    )
        except Exception as e:
            show_error(f"Connection failed: {e}")

    asyncio.run(_run())


@bank_grp.command("write")
@click.argument("space_id")
@click.argument("filename")
@click.option(
    "--content-file", "-f", type=click.Path(exists=True), help="Source file (.md)"
)
@click.option("--content", "-c", default="", help="Inline content")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_write_cmd(ctx, space_id, filename, content_file, content, jflag):
    """✏️ Write/replace a bank file (manage, bypasses the LLM).

    \b
    Examples:
      bank write mon-projet activeContext.md -f ./context.md
      bank write mon-projet progress.md -c "# Progress\\n- v1 OK"
    """
    if content_file:
        content = open(content_file, encoding="utf-8").read()
    if not content:
        show_error("Content required: --content-file/-f or --content/-c")
        return
    from .display import show_bank_write_result

    _run_tool(
        ctx,
        "bank_write",
        {
            "space_id": space_id,
            "filename": filename,
            "content": content,
        },
        show_bank_write_result,
        jflag,
    )


@bank_grp.command("delete")
@click.argument("space_id")
@click.argument("filename")
@click.option("--confirm", is_flag=True, help="Confirm deletion (required)")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_delete_cmd(ctx, space_id, filename, confirm, jflag):
    """🗑️ Delete a bank file + duplicates (manage, irreversible)."""
    from .display import show_bank_delete_result

    if not confirm:
        show_warning("⚠️  Bank deletion requires explicit --confirm:")
        show_warning(f"   bank delete {space_id} {filename} --confirm")
        return
    _run_tool(
        ctx,
        "bank_delete",
        {
            "space_id": space_id,
            "filename": filename,
            "confirm": True,
        },
        show_bank_delete_result,
        jflag,
    )


@bank_grp.command("repair")
@click.argument("space_id")
@click.option("--apply", is_flag=True, help="Apply corrections (otherwise dry-run)")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_repair_cmd(ctx, space_id, apply, jflag):
    """🔧 Repair corrupted names (manage). Dry-run by default.

    \b
    Examples:
      bank repair mon-projet              # Scan only (dry-run)
      bank repair mon-projet --apply      # Apply corrections
    """
    from .display import show_bank_repair_result

    _run_tool(
        ctx,
        "bank_repair",
        {
            "space_id": space_id,
            "dry_run": not apply,
        },
        show_bank_repair_result,
        jflag,
    )


@bank_grp.command("compact")
@click.argument("space_id")
@click.option("--apply", is_flag=True, help="Actually compact (otherwise dry-run)")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def bank_compact_cmd(ctx, space_id, apply, jflag):
    """📦 Compact oversized bank files via LLM (manage).

    \b
    Analyzes each file and compares its size to the configured limit
    (activeContext.md: 8KB, progress.md: 20KB, others: 15KB).
    Oversized files are summarized/cleaned by the LLM.

    \b
    Examples:
      bank compact mon-projet              # Scan only (dry-run)
      bank compact mon-projet --apply      # Compaction effective
    """
    if not apply:
        console.print("[dim]Dry-run mode — analysis without modifications.[/dim]")
    else:
        console.print(
            "[dim]Compaction in progress... (may take several seconds per file)[/dim]"
        )
    from .display import show_bank_compact_result

    _run_tool(
        ctx,
        "bank_compact",
        {
            "space_id": space_id,
            "dry_run": not apply,
        },
        show_bank_compact_result,
        jflag,
    )


# ─────────────────────────────────────────────────────────────
# Token (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("token")
def token_grp():
    """🔑 Token management."""
    pass


# Valid permission levels (from least to most permissive)
VALID_PERMISSIONS = click.Choice(
    ["read", "read,write", "read,write,manage", "read,write,manage,admin"],
    case_sensitive=False,
)


@token_grp.command("create")
@click.argument("name")
@click.option(
    "--permissions",
    "-p",
    type=VALID_PERMISSIONS,
    required=True,
    help="Permissions: read | read,write | read,write,manage | read,write,manage,admin",
)
@click.option(
    "--space-ids",
    default="",
    help=(
        "Initial spaces for a non-admin target when the caller is "
        "admin/bootstrap; forbidden with an admin target"
    ),
)
@click.option(
    "--expires-in-days",
    default=0,
    type=click.IntRange(min=0),
    help="Expiration (0=never)",
)
@click.option("--email", "-e", default="", help="Owner email")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def token_create_cmd(ctx, name, permissions, space_ids, expires_in_days, email, jflag):
    """Create a token (manage; admin profile requires admin permission).

    \b
    Examples:
      token create KSE -p read,write --email kevin@example.com
      token create bot-ci --permissions read
      token create ops-maint -p read,write,manage
      token create admin-ops -p read,write,manage,admin

    \b
    Available permissions:
      read                    — Read only
      read,write              — Read + write in explicitly allowed spaces
      read,write,manage       — + create spaces/tokens and invite tokens
      read,write,manage,admin — Full access (tokens, GC, no space restriction)
    """
    _run_token_create(
        ctx,
        name=name,
        permissions=permissions,
        space_ids=space_ids,
        expires_in_days=expires_in_days,
        email=email,
        json_flag=jflag,
    )


@token_grp.command("update")
@click.argument("token_hash")
@click.option(
    "--permissions",
    "-p",
    type=VALID_PERMISSIONS,
    default=None,
    help="New permissions (read | read,write | read,write,manage | read,write,manage,admin)",
)
@click.option(
    "--space-ids",
    "-s",
    default="",
    help=(
        "REPLACEMENT MODE — new authorized spaces (CSV, or '*'/'all'). "
        "⚠️ Replaces the full list: risk of silent revocation. "
        "Prefer --add-spaces / --remove-spaces for a safe delta."
    ),
)
@click.option(
    "--add-spaces",
    "-a",
    default="",
    help=(
        "DELTA MODE — spaces to add (CSV). Idempotent. "
        "Incompatible with --space-ids. (issue #13)"
    ),
)
@click.option(
    "--remove-spaces",
    "-r",
    default="",
    help=(
        "DELTA MODE — spaces to remove (CSV). Idempotent. "
        "Incompatible with --space-ids. (issue #13)"
    ),
)
@click.option("--email", "-e", default="", help="Owner email")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def token_update_cmd(
    ctx, token_hash, permissions, space_ids, add_spaces, remove_spaces, email, jflag
):
    """✏️  Update a token (permissions, spaces, email).

    \b
    Examples:
      token update sha256:a8c5 --email user@example.com
      token update sha256:a8c5 -p read,write
      token update sha256:a8c5 -s "mon-projet"                 # remplace
      token update sha256:a8c5 -a "new-space"                  # ajoute (delta)
      token update sha256:a8c5 -a "new-a,new-b" -r "old"       # mix delta
    """
    if (
        not permissions
        and not space_ids
        and not add_spaces
        and not remove_spaces
        and not email
    ):
        show_error(
            "Nothing to update. Use --permissions, --space-ids, "
            "--add-spaces, --remove-spaces and/or --email."
        )
        return

    # Client-side guard (server also validates, but we avoid the round-trip)
    if space_ids and (add_spaces or remove_spaces):
        show_error(
            "--space-ids (replacement) is incompatible with "
            "--add-spaces / --remove-spaces (delta). Choose one or the other."
        )
        return

    args = {"token_hash": token_hash}
    if permissions:
        args["permissions"] = permissions
    if space_ids:
        args["space_ids"] = space_ids
    if add_spaces:
        args["space_ids_add"] = add_spaces
    if remove_spaces:
        args["space_ids_remove"] = remove_spaces
    if email:
        args["email"] = email
    _run_tool(
        ctx,
        "admin_update_token",
        args,
        show_token_updated,
        jflag,
    )


@token_grp.command("list")
@click.option(
    "--name-contains",
    "-n",
    default="",
    help="Filter by name substring (case-insensitive). (issue #13)",
)
@click.option(
    "--has-space",
    "-s",
    default="",
    help="Filter tokens allowing this space_id (exact match). (issue #13)",
)
@click.option(
    "--no-revoked",
    is_flag=True,
    default=False,
    help="Exclude revoked tokens from results. (issue #13)",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def token_list_cmd(ctx, name_contains, has_space, no_revoked, jflag):
    """List tokens (with optional filters).

    \b
    Examples:
      token list
      token list --name-contains agent
      token list --has-space mon-projet
      token list --has-space mon-projet --no-revoked
    """
    args = {
        "name_contains": name_contains,
        "has_space": has_space,
        "include_revoked": not no_revoked,
    }
    _run_tool(ctx, "admin_list_tokens", args, show_token_list, jflag)


@token_grp.command("bulk-update")
@click.option(
    "--names",
    default="",
    help="CSV list of exact names (ex: 'agent-laptop,agent-desktop').",
)
@click.option(
    "--name-contains",
    "-n",
    default="",
    help="Name substring (case-insensitive).",
)
@click.option(
    "--has-space",
    "-s",
    default="",
    help=(
        "Filters tokens whose space_ids contains this space_id "
        "(exact match, case-sensitive). Ideal for 'remove old-project "
        "from all tokens that have it'. (review PR #14)"
    ),
)
@click.option(
    "--add-spaces",
    "-a",
    default="",
    help="Spaces to add (CSV). Idempotent.",
)
@click.option(
    "--remove-spaces",
    "-r",
    default="",
    help="Spaces to remove (CSV). Idempotent.",
)
@click.option(
    "--permissions",
    "-p",
    type=VALID_PERMISSIONS,
    default=None,
    help="New permissions to apply to all selected tokens.",
)
@click.option(
    "--email",
    "-e",
    default="",
    help="New email to apply to all selected tokens.",
)
@click.option(
    "--include-revoked",
    is_flag=True,
    default=False,
    help=(
        "Include revoked tokens (default: skipped). Intentional asymmetry "
        "with 'token list' (which includes them by default) — observing vs "
        "mute. (review PR #14)"
    ),
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Confirm application (otherwise, client dry-run).",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def token_bulk_update_cmd(
    ctx,
    names,
    name_contains,
    has_space,
    add_spaces,
    remove_spaces,
    permissions,
    email,
    include_revoked,
    confirm,
    jflag,
):
    """🔁 Update multiple tokens in a single operation (issue #13).

    \b
    ⚠️ FILTERS COMBINED WITH AND: a token must satisfy EACH of the
    provided filters (not just one). For OR logic, make
    multiple calls.
    \b
    Filters (at least one required) :
      --names, --name-contains, --has-space
    Operations (at least one required):
      --add-spaces, --remove-spaces, --permissions, --email

    \b
    Examples:
      # Add "new-project" to all agents
      token bulk-update --name-contains agent --add-spaces new-project --confirm

      # Remove "old-project" from ALL tokens that have it
      token bulk-update --has-space old-project --remove-spaces old-project --confirm

      # Migrate 3 explicit tokens
      token bulk-update --names "a,b,c" -a new-space -r old-space --confirm

      # Also modify revoked tokens (opt-in)
      token bulk-update --name-contains old-agent --remove-spaces dead --include-revoked --confirm

      # Dry-run (default without --confirm): shows what would be done
      token bulk-update --name-contains agent --add-spaces new-project
    """
    if not names and not name_contains and not has_space:
        show_error(
            "At least one filter required: --names, --name-contains or --has-space. "
            "See 'token bulk-update --help' for examples."
        )
        return
    if not (add_spaces or remove_spaces or permissions or email):
        show_error(
            "At least one operation required: --add-spaces, --remove-spaces, "
            "--permissions or --email."
        )
        return

    if not confirm:
        show_warning(
            "⚠️  Dry-run: no modifications will be applied. "
            "Add --confirm to execute."
        )
        # In dry-run, we simulate with a filtered list to show targets.
        # We reproduce server semantics (AND-combination + include_revoked
        # respected) — list_tokens does not filter by 'names', we replay it here.
        list_args = {
            "name_contains": name_contains,
            "has_space": has_space,
            "include_revoked": include_revoked,
        }
        names_set = {n.strip() for n in names.split(",") if n.strip()}

        async def _dry_run():
            from .client import MCPClient
            client = MCPClient(ctx.obj["url"], ctx.obj["token"])
            try:
                res = await client.call_tool("admin_list_tokens", list_args)
                if res.get("status") != "ok":
                    show_error(res.get("message", "?"))
                    return
                tokens = res.get("tokens", [])
                if names_set:
                    tokens = [t for t in tokens if t["name"] in names_set]
                if not tokens:
                    console.print("[yellow]No tokens match the filter.[/yellow]")
                    return
                console.print(
                    f"[bold]Potential targets ({len(tokens)} token(s)):[/bold]"
                )
                for t in tokens:
                    revoked_tag = (
                        " [red](revoked)[/red]" if t.get("revoked") else ""
                    )
                    console.print(
                        f"  • [cyan]{t['name']}[/cyan]{revoked_tag}  "
                        f"spaces={t.get('space_ids', [])}  "
                        f"perms={t.get('permissions', [])}"
                    )
                console.print(
                    "\n[dim]Re-run with --confirm to apply the modifications.[/dim]"
                )
            except Exception as e:
                show_error(f"Connection failed: {e}")

        asyncio.run(_dry_run())
        return

    args = {
        "names": names,
        "name_contains": name_contains,
        "has_space": has_space,
        "space_ids_add": add_spaces,
        "space_ids_remove": remove_spaces,
        "include_revoked": include_revoked,
    }
    if permissions:
        args["permissions"] = permissions
    if email:
        args["email"] = email

    def _on_success(result):
        from .display import show_bulk_update_result
        show_bulk_update_result(result)

    _run_tool(
        ctx,
        "admin_bulk_update_tokens",
        args,
        _on_success,
        jflag,
    )


@token_grp.command("revoke")
@click.argument("token_hash")
@click.pass_context
def token_revoke_cmd(ctx, token_hash):
    """Revoke a token."""
    _run_tool(
        ctx,
        "admin_revoke_token",
        {"token_hash": token_hash},
        lambda r: show_success(r.get("message", "Token revoked")),
    )


@token_grp.command("delete")
@click.argument("token_hash")
@click.pass_context
def token_delete_cmd(ctx, token_hash):
    """🗑️ Physically delete a token (irreversible)."""
    _run_tool(
        ctx,
        "admin_delete_token",
        {"token_hash": token_hash},
        lambda r: show_success(r.get("message", "Token deleted")),
    )


@token_grp.command("purge")
@click.option(
    "--all",
    "purge_all",
    is_flag=True,
    help="Delete ALL tokens (not just revoked ones)",
)
@click.option("--confirm", is_flag=True, help="Confirm purge (required)")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def token_purge_cmd(ctx, purge_all, confirm, jflag):
    """🧹 Purge revoked tokens (or all with --all). Requires --confirm."""
    if not confirm:
        mode = "ALL tokens" if purge_all else "revoked tokens"
        show_warning(f"⚠️  Purge {mode} — add --confirm to confirm:")
        show_warning(f"   token purge {'--all ' if purge_all else ''}--confirm")
        return
    revoked_only = not purge_all
    _run_tool(
        ctx,
        "admin_purge_tokens",
        {"revoked_only": revoked_only, "confirm": True},
        lambda r: show_success(
            f"{r.get('deleted', 0)} token(s) deleted, {r.get('remaining', 0)} remaining"
        ),
        jflag,
    )


# ─────────────────────────────────────────────────────────────
# Backup (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("backup")
def backup_grp():
    """💾 Backup & restore."""
    pass


@backup_grp.command("create")
@click.argument("space_id", default="")
@click.option(
    "--all", "backup_all", is_flag=True, help="Backup ALL spaces (admin required)"
)
@click.option("--description", "-d", default="")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def backup_create_cmd(ctx, space_id, backup_all, description, jflag):
    """Create a backup (--all for all spaces, admin required).

    \b
    Examples:
      backup create mon-projet                # Single space
      backup create --all                     # ALL spaces (admin)
      backup create --all -d "avant migration"
    """
    if backup_all:
        space_id = ""
        console.print("[dim]Backing up all spaces...[/dim]")
    elif not space_id:
        show_error("Space ID required, or use --all for all spaces.")
        return
    from .display import show_backup_all_result

    on_success = show_backup_all_result if not space_id else show_backup_created
    _run_tool(
        ctx,
        "backup_create",
        {
            "space_id": space_id,
            "description": description,
        },
        on_success,
        jflag,
    )


@backup_grp.command("list")
@click.option("--space-id", default="", help="Filter by space")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def backup_list_cmd(ctx, space_id, jflag):
    """List backups."""
    _run_tool(ctx, "backup_list", {"space_id": space_id}, show_backup_list, jflag)


@backup_grp.command("restore")
@click.argument("backup_id")
@click.option("--confirm", is_flag=True)
@click.pass_context
def backup_restore_cmd(ctx, backup_id, confirm):
    """Restore from a backup."""
    _run_tool(
        ctx,
        "backup_restore",
        {"backup_id": backup_id, "confirm": confirm},
        lambda r: show_success(f"Restored: {r.get('files_restored', 0)} files"),
    )


@backup_grp.command("download")
@click.argument("backup_id")
@click.pass_context
def backup_download_cmd(ctx, backup_id):
    """📥 Download a backup (tar.gz base64)."""
    _run_tool(ctx, "backup_download", {"backup_id": backup_id}, show_json, True)


@backup_grp.command("delete")
@click.argument("backup_id")
@click.option("--confirm", is_flag=True)
@click.pass_context
def backup_delete_cmd(ctx, backup_id, confirm):
    """Delete a backup."""
    _run_tool(
        ctx,
        "backup_delete",
        {"backup_id": backup_id, "confirm": confirm},
        lambda r: show_success(f"Deleted: {r.get('files_deleted', 0)} files"),
    )


# ─────────────────────────────────────────────────────────────
# Graph Bridge (sous-groupe)
# ─────────────────────────────────────────────────────────────


@cli.group("graph")
def graph_grp():
    """🌉 Bridge to Graph Memory (long-term memory)."""
    pass


@graph_grp.command("connect")
@click.argument("space_id")
@click.argument("url")
@click.argument("graph_token")
@click.argument("memory_id")
@click.option(
    "--ontology",
    "-o",
    default="general",
    help="Graph Memory ontology (general, legal, cloud, managed-services, presales)",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_connect_cmd(ctx, space_id, url, graph_token, memory_id, ontology, jflag):
    """Connect a space to Graph Memory."""
    _run_tool(
        ctx,
        "graph_connect",
        {
            "space_id": space_id,
            "url": url,
            "token": graph_token,
            "memory_id": memory_id,
            "ontology": ontology,
        },
        show_graph_connected,
        jflag,
    )


@graph_grp.command("push")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_push_cmd(ctx, space_id, jflag):
    """📤 Push bank to Graph Memory (delete + re-ingest)."""
    console.print("[dim]Push in progress... (may take several minutes)[/dim]")
    _run_tool(ctx, "graph_push", {"space_id": space_id}, show_graph_push_result, jflag)


@graph_grp.command("status")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_status_cmd(ctx, space_id, jflag):
    """📊 Graph Memory connection status (stats, documents, entities)."""
    _run_tool(ctx, "graph_status", {"space_id": space_id}, show_graph_status, jflag)


@graph_grp.command("query")
@click.argument("space_id")
@click.argument("query")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=1, max=500),
    default=10,
    show_default=True,
    help="Maximum number of graph results (1-500).",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_query_cmd(ctx, space_id, query, limit, jflag):
    """🔎 Query the derived long-tier graph (read-only)."""
    _run_tool(
        ctx,
        "long_query",
        {"space_id": space_id, "query": query, "limit": limit},
        show_json,
        jflag,
    )


@graph_grp.command("disconnect")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_disconnect_cmd(ctx, space_id, jflag):
    """🔌 Disconnect a space from Graph Memory."""
    _run_tool(
        ctx, "graph_disconnect", {"space_id": space_id}, show_graph_disconnected, jflag
    )


@graph_grp.command("use-local")
@click.argument("space_id")
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def graph_use_local_cmd(ctx, space_id, jflag):
    """🔁 Replace a legacy Graph override with the embedded runtime (manage)."""
    _run_tool(
        ctx,
        "long_disconnect",
        {"space_id": space_id, "use_embedded": True},
        show_graph_local,
        jflag,
    )


# ─────────────────────────────────────────────────────────────
# GC (Garbage Collector)
# ─────────────────────────────────────────────────────────────


@cli.command("gc")
@click.option("--space-id", default="", help="Target space (empty = all)")
@click.option("--max-age-days", default=7, help="Age threshold in days (default 7)")
@click.option("--confirm", is_flag=True, help="Actually execute (otherwise dry-run)")
@click.option("--delete-only", is_flag=True, help="Delete without consolidating")
@click.option(
    "--expected-eligible-set-token",
    default="",
    help="Opaque eligible-set token from a prior dry-run (required for delete)",
)
@click.option("--json", "-j", "jflag", is_flag=True)
@click.pass_context
def gc_cmd(
    ctx,
    space_id,
    max_age_days,
    confirm,
    delete_only,
    expected_eligible_set_token,
    jflag,
):
    """🧹 Garbage Collector: clean up orphan notes."""
    if max_age_days < 0:
        raise click.UsageError("--max-age-days must be >= 0")
    if delete_only and not confirm:
        raise click.UsageError("--delete-only requires --confirm")
    if confirm and delete_only and not expected_eligible_set_token:
        raise click.UsageError(
            "delete mode requires --expected-eligible-set-token from a prior dry-run"
        )
    _run_tool(
        ctx,
        "admin_gc_notes",
        {
            "space_id": space_id,
            "max_age_days": max_age_days,
            "confirm": confirm,
            "delete_only": delete_only,
            "expected_eligible_set_token": expected_eligible_set_token,
        },
        show_json,
        jflag,
    )


# ─────────────────────────────────────────────────────────────
# Shell
# ─────────────────────────────────────────────────────────────


@cli.command("shell")
@click.pass_context
def shell_cmd(ctx):
    """🐚 Start the interactive shell."""
    from .shell import run_shell

    asyncio.run(run_shell(ctx.obj["url"], ctx.obj["token"]))
