# -*- coding: utf-8 -*-
"""
Rich display functions — shared between CLI Click and interactive Shell.

Each MCP tool has its show_xxx_result() function for colored rendering.
These functions are imported in both commands.py and shell.py (DRY).
"""

import json
from datetime import datetime, timezone
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


# =============================================================================
# Common utilities
# =============================================================================


def show_error(msg: str):
    """Displays an error message."""
    console.print(f"[red]❌ {escape_markup(str(msg))}[/red]")


def show_success(msg: str):
    """Displays a success message."""
    console.print(f"[green]✅ {msg}[/green]")


def show_warning(msg: str):
    """Displays a warning message."""
    console.print(f"[yellow]⚠️  {msg}[/yellow]")


def show_json(data: dict):
    """Displays a dict as raw JSON on stdout (machine-readable, pipeable).

    Uses print() instead of Rich to avoid ANSI pollution
    when output is redirected or piped to another process.
    """
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _format_local_timestamp(value, *, date_only: bool = False) -> str:
    """Format a server UTC timestamp in the CLI process's local timezone.

    Unknown values are returned verbatim. This is presentation-only: JSON
    output and identifiers keep their canonical server representation.
    """
    if value is None or value == "":
        return ""
    raw = str(value)
    if len(raw) == 10:
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            pass
        else:
            return raw
    parsed = None
    compact_raw = raw
    if (
        len(raw) == 52
        and raw[19] == "-"
        and all(character in "0123456789abcdef" for character in raw[20:])
    ):
        compact_raw = raw[:19]
    for compact_format in ("%Y-%m-%dT%H-%M-%S", "%Y%m%dT%H%M%S"):
        try:
            parsed = datetime.strptime(compact_raw, compact_format).replace(
                tzinfo=timezone.utc
            )
            break
        except ValueError:
            pass
    if parsed is None:
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return raw[:10] if date_only else raw
        if parsed.tzinfo is None:
            # Historical naïve server timestamps were emitted in UTC.
            parsed = parsed.replace(tzinfo=timezone.utc)

    local = parsed.astimezone()
    if date_only:
        return local.strftime("%Y-%m-%d")
    offset = local.utcoffset()
    assert offset is not None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{local:%Y-%m-%d %H:%M:%S} UTC{sign}{hours:02d}:{minutes:02d}"


# =============================================================================
# System
# =============================================================================


def show_health_result(result: dict):
    """Displays the health check result (HTTP /health or MCP system_health)."""
    status = result.get("status", "?")
    services = result.get("services", {})
    svc_name = result.get("service_name") or result.get("service", "?")
    version = result.get("version", "")

    if status == "healthy":
        icon = "✅"
    elif status == "degraded":
        icon = "⚠️"
    else:
        icon = "❌"

    title = f"{icon} Health — {svc_name}"
    if version:
        title += f" v{version}"

    table = Table(title=title, show_header=True)
    table.add_column("Service", style="cyan bold")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    for name, info in services.items():
        if isinstance(info, dict):
            s = info.get("status", "?")
            if s == "ok":
                s_icon = "✅"
            elif s == "warning":
                s_icon = "⚠️"
            else:
                s_icon = "❌"
            # Build details: model, latency, bucket, or error message
            details_parts = []
            if info.get("model"):
                details_parts.append(info["model"])
            if info.get("bucket"):
                details_parts.append(f"bucket={info['bucket']}")
            if info.get("latency_ms") is not None:
                details_parts.append(f"{info['latency_ms']}ms")
            if info.get("message"):
                details_parts.append(info["message"])
            table.add_row(name, f"{s_icon} {s}", "  ".join(details_parts))

    console.print(table)


def show_whoami_result(result: dict):
    """Displays the system_whoami result."""
    auth_type = result.get("auth_type", "?")
    type_icon = "🔑" if auth_type == "bootstrap" else "🏷️"
    perms = result.get("permissions", [])
    perm_str = ", ".join(perms) if perms else "none"
    # Permission icons
    perm_icons = []
    if "read" in perms:
        perm_icons.append("🔑 read")
    if "write" in perms:
        perm_icons.append("✏️ write")
    if "manage" in perms:
        perm_icons.append("🔧 manage")
    if "admin" in perms:
        perm_icons.append("👑 admin")
    perm_display = "  ".join(perm_icons) if perm_icons else perm_str

    spaces = result.get("allowed_spaces") or result.get("space_ids") or []
    is_admin = "admin" in (result.get("permissions") or [])
    spaces_str = (
        ", ".join(spaces)
        if spaces
        else (
            "[dim]all (admin)[/dim]"
            if is_admin
            else "[yellow]none — manager invitation required[/yellow]"
        )
    )

    lines = [
        f"[bold]Identity:[/bold] [cyan bold]{result.get('client_name', '?')}[/cyan bold]",
        f"[bold]Type     :[/bold] {type_icon} {auth_type}",
        f"[bold]Rights   :[/bold] {perm_display}",
        f"[bold]Spaces   :[/bold] {spaces_str}",
    ]

    # Additional metadata for S3 tokens
    if result.get("email"):
        lines.append(f"[bold]Email    :[/bold] {result['email']}")
    if result.get("token_hash"):
        lines.append(f"[bold]Hash     :[/bold] [dim]{result['token_hash']}[/dim]")
    if result.get("created_at"):
        lines.append(f"[bold]Created  :[/bold] {_format_local_timestamp(result['created_at'])}")
    expires = result.get("expires_at")
    if expires:
        lines.append(f"[bold]Expire   :[/bold] {_format_local_timestamp(expires)}")
    elif result.get("auth_type") == "token":
        lines.append("[bold]Expires  :[/bold] never")
    if result.get("note"):
        lines.append(f"\n[dim italic]{result['note']}[/dim italic]")

    console.print(
        Panel.fit(
            "\n".join(lines),
            title="👤 Who am I?",
            border_style="cyan",
        )
    )


def show_about_result(result: dict):
    """Displays the system_about result."""
    console.print(
        Panel.fit(
            f"[bold]Service :[/bold] [cyan]{result.get('name', '?')}[/cyan]\n"
            f"[bold]Version :[/bold] [green]{result.get('version', '?')}[/green]\n"
            f"[bold]Python  :[/bold] {result.get('python_version', '?')}\n"
            f"[bold]Tools   :[/bold] {result.get('tools_count', 0)}",
            title="ℹ️  About",
            border_style="blue",
        )
    )
    tools = result.get("tools", [])
    if tools:
        # Group by category (prefix before _)
        categories = {}
        for t in tools:
            name = t.get("name", "?")
            cat = name.split("_")[0].capitalize() if "_" in name else "Other"
            categories.setdefault(cat, []).append(t)

        table = Table(show_header=True, title="MCP Tools", title_style="bold")
        table.add_column("Cat.", style="bold", width=8)
        table.add_column("Tool", style="cyan bold", width=20)
        table.add_column("Description", style="dim", max_width=55)

        for cat, cat_tools in categories.items():
            for i, t in enumerate(cat_tools):
                # Extract the first non-empty line of the description
                desc = t.get("description", "")
                first_line = ""
                for line in desc.strip().split("\n"):
                    line = line.strip()
                    if (
                        line
                        and not line.startswith("Args:")
                        and not line.startswith("Returns:")
                    ):
                        first_line = line[:55]
                        break
                cat_label = f"[magenta]{cat}[/magenta]" if i == 0 else ""
                table.add_row(cat_label, t.get("name", "?"), first_line)

        console.print(table)


# =============================================================================
# Space
# =============================================================================


def show_space_created(result: dict):
    """Displays a space only after the server confirms ``created``."""
    console.print(
        Panel.fit(
            f"[bold]Space ID :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Description :[/bold] {result.get('description', '')}\n"
            f"[bold]Rules :[/bold] {result.get('rules_size', 0)} bytes\n"
            f"[bold]Created:[/bold] {_format_local_timestamp(result.get('created_at'))}",
            title="✅ Space Created",
            border_style="green",
        )
    )


def show_space_create_recovery(result: dict):
    """Displays the typed, non-success recovery contract for ``space_create``.

    The server-provided action is operational guidance, so it is rendered
    verbatim (with Rich markup escaped) rather than paraphrased.  Booleans use
    their JSON spelling to preserve the exact ``recovery.retry_safe`` value.
    """
    recovery = result.get("recovery") or {}
    retry_safe = recovery.get("retry_safe")
    retry_safe_text = json.dumps(retry_safe, ensure_ascii=False)
    action = escape_markup(str(recovery.get("action", "<missing>")))
    message = escape_markup(str(result.get("message", "")))
    space_id = escape_markup(str(result.get("space_id", "?")))
    console.print(
        Panel.fit(
            f"[bold]status:[/bold] partial\n"
            f"[bold]space_id:[/bold] [cyan]{space_id}[/cyan]\n"
            f"[bold]recovery_required:[/bold] true\n"
            f"[bold]recovery.retry_safe:[/bold] {retry_safe_text}\n"
            f"[bold]recovery.action:[/bold] {action}\n\n"
            f"[yellow]{message}[/yellow]\n\n"
            "[bold]The space is not confirmed created. No automatic cleanup "
            "or rollback was performed.[/bold]",
            title="Space Creation — Recovery Required (not successful)",
            border_style="yellow" if retry_safe is True else "red",
        )
    )


def show_space_delete_recovery(result: dict):
    """Displays an incomplete ``space_delete`` without implying success."""
    recovery = result.get("recovery") or {}
    failed_keys = result.get("failed_keys")
    if not isinstance(failed_keys, list):
        failed_keys = []
    failed_lines = (
        "\n".join(f"  - {escape_markup(str(key))}" for key in failed_keys)
        if failed_keys
        else "  []"
    )
    access_pending_line = (
        "[bold]access_grants_pending:[/bold] "
        f"{json.dumps(result.get('access_grants_pending'), ensure_ascii=False)}\n"
        if "access_grants_pending" in result
        else ""
    )
    console.print(
        Panel.fit(
            "[bold]status:[/bold] partial\n"
            f"[bold]space_id:[/bold] [cyan]{escape_markup(str(result.get('space_id', '?')))}[/cyan]\n"
            f"[bold]files_total:[/bold] {json.dumps(result.get('files_total'), ensure_ascii=False)}\n"
            f"[bold]files_deleted:[/bold] {json.dumps(result.get('files_deleted'), ensure_ascii=False)}\n"
            f"[bold]marker_preserved:[/bold] {json.dumps(result.get('marker_preserved'), ensure_ascii=False)}\n"
            f"{access_pending_line}"
            f"[bold]recovery_required:[/bold] {json.dumps(result.get('recovery_required'), ensure_ascii=False)}\n"
            f"[bold]recovery.retry_safe:[/bold] {json.dumps(recovery.get('retry_safe'), ensure_ascii=False)}\n"
            f"[bold]failed_keys:[/bold]\n{failed_lines}\n"
            f"[bold]recovery.action:[/bold] {escape_markup(str(recovery.get('action', '<missing>')))}\n\n"
            f"[yellow]{escape_markup(str(result.get('message', '')))}[/yellow]\n\n"
            "[bold]The space deletion is incomplete. No automatic retry, "
            "cleanup, or navigation was performed.[/bold]",
            title="Space Deletion — Recovery Required (not successful)",
            border_style="yellow"
            if recovery.get("retry_safe") is True
            else "red",
        )
    )


def show_space_invite_result(result: dict):
    """Displays the idempotent result of adding a token to one space."""
    added = result.get("added") is True
    state = "Access granted" if added else "Already had access (no change)"
    console.print(
        Panel.fit(
            f"[bold]Space:[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Result:[/bold] {state}",
            title="Space Invitation",
            border_style="green" if added else "yellow",
        )
    )


def show_space_updated(result: dict):
    """Displays the result of a space update."""
    updated = result.get("updated_fields", [])
    panel_content = f"[bold]{result.get('space_id', '?')}[/bold]\n"
    if "description" in updated:
        panel_content += f"Description → {result.get('description', '')}\n"
    if "owner" in updated:
        panel_content += f"Owner → {result.get('owner', '')}\n"
    panel_content += f"Updated fields: {', '.join(updated)}"
    console.print(
        Panel(panel_content, title="✏️ Space Updated", border_style="green")
    )


def show_rules_updated(result: dict):
    """Displays the result of a rules update."""
    panel_content = (
        f"[bold]{result.get('space_id', '?')}[/bold]\n"
        f"Size: {result.get('rules_size', '?')} bytes"
    )
    console.print(
        Panel(panel_content, title="📜 Rules Updated", border_style="green")
    )


def show_space_list(result: dict):
    """Displays the list of spaces."""
    spaces = result.get("spaces", [])
    table = Table(title=f"📂 {result.get('total', 0)} spaces", show_header=True)
    table.add_column("Space ID", style="cyan bold")
    table.add_column("Description")
    table.add_column("Owner", style="dim")
    table.add_column("Notes", justify="right")
    table.add_column("Bank", justify="right")
    for s in spaces:
        table.add_row(
            s.get("space_id", "?"),
            s.get("description", ""),
            s.get("owner", ""),
            str(s.get("live_notes_count", 0)),
            str(s.get("bank_files_count", 0)),
        )
    console.print(table)


def show_space_info(result: dict):
    """Displays detailed space info."""
    live = result.get("live", {})
    bank = result.get("bank", {})
    console.print(
        Panel.fit(
            f"[bold]Space ID :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Description :[/bold] {result.get('description', '')}\n"
            f"[bold]Owner :[/bold] {result.get('owner', '') or '[dim]—[/dim]'}\n"
            f"[bold]Live notes :[/bold] {live.get('notes_count', 0)} ({live.get('total_size', 0)} bytes)\n"
            f"[bold]Bank files :[/bold] {bank.get('files_count', 0)} ({bank.get('total_size', 0)} bytes)\n"
            f"[bold]Consolidations :[/bold] {result.get('consolidation_count', 0)}\n"
            f"[bold]Last:[/bold] {_format_local_timestamp(result.get('last_consolidation')) or 'never'}",
            title="📋 Space",
            border_style="blue",
        )
    )


def show_rules(result: dict):
    """Displays the rules of a space."""
    rules = result.get("rules", "")
    console.print(
        Panel(Syntax(rules, "markdown"), title="📐 Rules", border_style="blue")
    )


def show_notes(result: dict):
    """Displays live notes."""
    notes = result.get("notes", [])
    # Colors by category
    colors = {
        "observation": "green",
        "decision": "yellow",
        "todo": "red",
        "insight": "magenta",
        "question": "cyan",
        "progress": "blue",
        "issue": "red",
    }
    table = Table(title=f"📝 {result.get('total', 0)} notes", show_header=True)
    table.add_column("Agent", style="cyan")
    table.add_column("Category")
    table.add_column("Content", max_width=60)
    table.add_column("Timestamp", style="dim")
    for n in notes:
        cat = n.get("category", "?")
        color = colors.get(cat, "white")
        table.add_row(
            n.get("agent", "?"),
            f"[{color}]{cat}[/{color}]",
            n.get("content", "")[:60],
            _format_local_timestamp(n.get("timestamp")),
        )
    console.print(table)


# =============================================================================
# Bank
# =============================================================================


def show_bank_list(result: dict):
    """Displays the list of bank files."""
    files = result.get("files", [])
    table = Table(
        title=f"📘 Bank — {result.get('file_count', 0)} files", show_header=True
    )
    table.add_column("File", style="cyan bold")
    table.add_column("Size", justify="right")
    for f in files:
        table.add_row(f.get("filename", "?"), f"{f.get('size', 0)} B")
    console.print(table)


def show_bank_content(result: dict):
    """Displays the content of a bank file."""
    console.print(
        Panel(
            Syntax(result.get("content", ""), "markdown"),
            title=f"📄 {result.get('filename', '?')}",
            border_style="blue",
        )
    )


def show_bank_write_result(result: dict):
    """Displays the bank_write result."""
    action = result.get("action", "?")
    icon = "✏️ Replaced" if action == "replaced" else "✨ Created"
    cleaned = result.get("unicode_duplicates_cleaned", 0)
    lines = [
        f"[bold]File    :[/bold] [cyan]{result.get('filename', '?')}[/cyan]",
        f"[bold]Action  :[/bold] {icon}",
        f"[bold]Size    :[/bold] {result.get('size', 0)} bytes",
    ]
    if cleaned:
        lines.append(
            f"[bold]Unicode duplicates cleaned:[/bold] [yellow]{cleaned}[/yellow]"
        )
    console.print(
        Panel.fit("\n".join(lines), title="📝 Bank Write", border_style="green")
    )


def show_bank_delete_result(result: dict):
    """Displays the bank_delete result."""
    deleted = result.get("files_deleted", 0)
    keys = result.get("keys_deleted", [])
    lines = [
        f"[bold]File    :[/bold] [cyan]{result.get('filename', '?')}[/cyan]",
        f"[bold]Deleted   :[/bold] {deleted} file(s)",
    ]
    if len(keys) > 1:
        lines.append(f"[bold]Variants  :[/bold] {', '.join(keys)}")
    console.print(
        Panel.fit("\n".join(lines), title="🗑️ Bank Delete", border_style="red")
    )


def show_bank_repair_result(result: dict):
    """Displays the bank_repair result."""
    mode = result.get("mode", "?")
    mode_label = (
        "[yellow]DRY-RUN (no modifications)[/yellow]"
        if mode == "dry-run"
        else "[green]APPLIED[/green]"
    )

    console.print(
        Panel.fit(
            f"[bold]Space   :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Mode    :[/bold] {mode_label}\n"
            f"[bold]Scanned :[/bold] {result.get('files_scanned', 0)} unique files\n"
            f"[bold]OK      :[/bold] {result.get('files_ok', 0)}\n"
            f"[bold]To repair :[/bold] {result.get('files_to_repair', 0)}\n"
            f"[bold]Duplicates:[/bold] {result.get('duplicates_found', 0)}",
            title="🔧 Bank Repair",
            border_style="yellow" if mode == "dry-run" else "green",
        )
    )

    repairs = result.get("repairs", [])
    if repairs:
        table = Table(title="Files to move", show_header=True)
        table.add_column("Original", style="red")
        table.add_column("→", justify="center", width=2)
        table.add_column("Corrected", style="green")
        table.add_column("Status")
        for r in repairs:
            status_icon = "✅" if r.get("status") == "repaired" else "🔍"
            table.add_row(
                r.get("original_relpath", "?"),
                "→",
                r.get("sanitized", "?"),
                status_icon,
            )
        console.print(table)

    duplicates = result.get("duplicates", [])
    if duplicates:
        table = Table(title="Duplicates to delete", show_header=True)
        table.add_column("File", style="red")
        table.add_column("Canonical", style="dim")
        table.add_column("Status")
        for d in duplicates:
            status_icon = "🗑️" if d.get("status") == "deleted" else "🔍"
            table.add_row(d.get("relpath", "?"), d.get("canonical", "?"), status_icon)
        console.print(table)

    if not repairs and not duplicates:
        show_success("All bank files are OK!")


def show_consolidation_result(result: dict):
    """Displays the consolidation result."""
    console.print(
        Panel.fit(
            f"[bold]Notes total    :[/bold] {result.get('notes_total', '?')}\n"
            f"[bold]Notes processed:[/bold] {result.get('notes_processed', 0)}\n"
            f"[bold]Declared useless:[/bold] {result.get('notes_discarded_count', 0)}\n"
            f"[bold]Notes deleted  :[/bold] {result.get('notes_deleted', 0)}\n"
            f"[bold]Notes remaining:[/bold] {result.get('notes_remaining', 0)}\n"
            f"[bold]Files created  :[/bold] {result.get('bank_files_created', 0)}\n"
            f"[bold]Files updated  :[/bold] {result.get('bank_files_updated', 0)}\n"
            f"[bold]Synthesis  :[/bold] {result.get('synthesis_size', 0)} chars\n"
            f"[bold]LLM tokens :[/bold] {result.get('llm_tokens_used', 0)}\n"
            f"[bold]Duration   :[/bold] {result.get('duration_seconds', 0)}s",
            title="🧠 Consolidation complete",
            border_style="green",
        )
    )


def show_consolidation_response(result: dict):
    """Show an async acknowledgement as a job, never as completed counters."""
    if result.get("status") in ("queued", "running"):
        show_consolidation_job(result)
    else:
        show_consolidation_result(result)


def _utf8_bytes_or_unasserted(value: object) -> str:
    """Render a byte metric without inventing a value for a null diagnostic."""

    if type(value) is int and value >= 0:
        return f"{value} UTF-8 bytes"
    return "unknown / not asserted"


def _safe_compaction_target_resolution_text(failure: object) -> str | None:
    """Render only the closed, content-free target-resolution tuple."""

    if not isinstance(failure, dict):
        return None
    operation_index = failure.get("operation_index")
    target_resolution = failure.get("target_resolution")
    target_match_count = failure.get("target_match_count")
    target_heading_sha256 = failure.get("target_heading_sha256")
    if (
        failure.get("error") != "ambiguous_or_missing_compaction_target"
        or type(operation_index) is not int
        or operation_index < 0
        or type(target_resolution) is not str
        or target_resolution not in {"missing", "ambiguous"}
        or type(target_match_count) is not int
        or target_match_count < 0
        or type(target_heading_sha256) is not str
        or len(target_heading_sha256) != 64
        or any(character not in "0123456789abcdef" for character in target_heading_sha256)
        or (target_resolution == "missing" and target_match_count != 0)
        or (target_resolution == "ambiguous" and target_match_count < 2)
    ):
        return None
    return (
        f"operation_index={operation_index}; target_resolution={target_resolution}; "
        f"target_match_count={target_match_count}; "
        f"target_heading_sha256={target_heading_sha256}"
    )


def _safe_compaction_failure_lines(failures: object) -> list[str]:
    """Format server-projected compaction failures without inspecting extras."""

    if not isinstance(failures, list):
        return []
    lines: list[str] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        filename = escape_markup(str(failure.get("filename", "")))
        error = escape_markup(str(failure.get("error", "unknown")))
        target_resolution = _safe_compaction_target_resolution_text(failure)
        suffix = (
            "; " + escape_markup(target_resolution)
            if target_resolution is not None
            else ""
        )
        lines.append(f"  - {filename or '<space>'}: {error}{suffix}")
    return lines


def _bank_size_advisory_lines(items: object) -> list[str]:
    """Format the server-projected bank size advisory without inspecting extras."""

    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        utf8_bytes = item.get("utf8_bytes")
        max_size = item.get("max_size")
        if (
            not isinstance(filename, str)
            or type(utf8_bytes) is not int
            or type(max_size) is not int
        ):
            continue
        lines.append(
            f"  - {escape_markup(filename)}: {utf8_bytes} UTF-8 bytes "
            f"(advisory threshold {max_size})"
        )
    return lines


def show_bank_compact_failure(result: dict):
    """Display a typed compact refusal or unresolved recovery as non-success."""

    status = escape_markup(str(result.get("status", "error")))
    recovery_required = result.get("recovery_required") is True
    failure_reason = escape_markup(str(result.get("failure_reason", "unknown")))
    message = escape_markup(str(result.get("message", "")))
    remediation = escape_markup(str(result.get("remediation", "<missing>")))
    preimage_id = result.get("preimage_id")
    failures = result.get("failures")
    if not isinstance(failures, list):
        failures = []

    failure_lines = _safe_compaction_failure_lines(failures)
    failure_text = "\n".join(failure_lines) if failure_lines else "  []"

    lines = [
        f"[bold]status:[/bold] {status}",
        f"[bold]failure_reason:[/bold] {failure_reason}",
        "[bold]total_size_after:[/bold] "
        f"{_utf8_bytes_or_unasserted(result.get('total_size_after'))}",
    ]
    if "failed_phase" in result:
        lines.append(
            "[bold]failed_phase:[/bold] "
            f"{escape_markup(str(result.get('failed_phase')))}"
        )
    if "rollback_outcome" in result:
        lines.append(
            "[bold]rollback_outcome:[/bold] "
            f"{escape_markup(str(result.get('rollback_outcome')))}"
        )
    if "files_applied_before_failure" in result:
        lines.append(
            "[bold]files_applied_before_failure:[/bold] "
            f"{json.dumps(result.get('files_applied_before_failure'), ensure_ascii=False)}"
        )
    if "apply_may_have_mutated" in result:
        lines.append(
            "[bold]apply_may_have_mutated:[/bold] "
            f"{json.dumps(result.get('apply_may_have_mutated'), ensure_ascii=False)}"
        )
    if recovery_required:
        lines.append("[bold]recovery_required:[/bold] true")
    if preimage_id is not None:
        lines.append(
            f"[bold]preimage_id:[/bold] {escape_markup(str(preimage_id))}"
        )
    file_reports = result.get("files")
    hash_lines = []
    if isinstance(file_reports, list):
        for file_report in file_reports:
            if not isinstance(file_report, dict):
                continue
            source_sha256 = file_report.get("source_sha256")
            result_sha256 = file_report.get("result_sha256")
            if source_sha256 is None and result_sha256 is None:
                continue
            filename = escape_markup(str(file_report.get("filename", "<space>")))
            source_text = (
                escape_markup(str(source_sha256))
                if source_sha256 is not None
                else "—"
            )
            result_text = (
                escape_markup(str(result_sha256))
                if result_sha256 is not None
                else "—"
            )
            hash_lines.append(
                f"  - {filename}: source_sha256={source_text}; result_sha256={result_text}"
            )
    if hash_lines:
        lines.extend(["[bold]file hashes:[/bold]", *hash_lines])
    lines.extend(
        [
            "[bold]failures:[/bold]",
            failure_text,
            f"[bold]remediation:[/bold] {remediation}",
        ]
    )
    if message:
        lines.append(f"[yellow]{message}[/yellow]")
    lines.append("[bold]No automatic retry or restore was performed.[/bold]")

    console.print(
        Panel.fit(
            "\n".join(lines),
            title=(
                "Compaction — Recovery Required (not successful)"
                if recovery_required
                else "Compaction — Failed"
            ),
            border_style="yellow" if recovery_required else "red",
        )
    )


def show_bank_compact_result(result: dict):
    """Displays a successful bank_compact result with persisted UTF-8 bytes."""
    dry_run = result.get("dry_run", True)
    mode_label = (
        "[yellow]DRY-RUN (no modifications)[/yellow]"
        if dry_run
        else "[green]APPLIED[/green]"
    )
    files_over = result.get("files_over_limit", 0)
    border = "yellow" if dry_run else ("green" if files_over == 0 else "cyan")

    size_before = result.get("total_size_before", 0)
    size_after = result.get("total_size_after", 0)
    reduction = ""
    if (
        not dry_run
        and type(size_before) is int
        and type(size_after) is int
        and size_before > 0
        and size_after < size_before
    ):
        pct = round((1 - size_after / size_before) * 100)
        reduction = (
            "\n[bold]Reduction  :[/bold] "
            f"[green]-{pct}%[/green] ({size_before} → {size_after} UTF-8 bytes)"
        )

    console.print(
        Panel.fit(
            f"[bold]Space      :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Mode       :[/bold] {mode_label}\n"
            f"[bold]Files      :[/bold] {result.get('files_total', 0)} total\n"
            f"[bold]Oversized  :[/bold] {files_over}\n"
            "[bold]Bank size  :[/bold] "
            f"{_utf8_bytes_or_unasserted(size_before)}\n"
            "[bold]After      :[/bold] "
            f"{_utf8_bytes_or_unasserted(size_after)}" + reduction,
            title="📦 Bank Compact",
            border_style=border,
        )
    )

    # File details table
    files = result.get("files", [])
    if files:
        table = Table(title="Details per file", show_header=True)
        table.add_column("File", style="cyan bold")
        table.add_column("Size (UTF-8 bytes)", justify="right")
        table.add_column("Limit (UTF-8 bytes)", justify="right")
        table.add_column("Source SHA-256", style="dim")
        table.add_column("Result SHA-256", style="dim")
        table.add_column("Ratio", justify="right")
        table.add_column("Status")

        for f in files:
            size = f.get("size", 0)
            max_size = f.get("max_size", 0)
            ratio = f.get("ratio", 0)
            over = f.get("over_limit", False)

            # Colored ratio indicator
            if ratio > 1.5:
                ratio_str = f"[red bold]{ratio}x[/red bold]"
            elif ratio > 1.0:
                ratio_str = f"[yellow]{ratio}x[/yellow]"
            else:
                ratio_str = f"[green]{ratio}x[/green]"

            # Statut
            if not over:
                status = "✅ OK"
            elif type(f.get("compacted_size")) is int:
                pct = f.get("reduction_pct", 0)
                status = f"📦 -{pct}% ({f['compacted_size']} UTF-8 bytes)"
            elif f.get("error"):
                status = f"[red]❌ {escape_markup(str(f['error']))}[/red]"
            else:
                status = "⚠️ needs compaction" if dry_run else "⚠️ oversized"

            table.add_row(
                escape_markup(str(f.get("filename", "?"))),
                f"{size}",
                f"{max_size}",
                (
                    escape_markup(str(f["source_sha256"]))
                    if f.get("source_sha256") is not None
                    else "—"
                ),
                (
                    escape_markup(str(f["result_sha256"]))
                    if f.get("result_sha256") is not None
                    else "—"
                ),
                ratio_str,
                status,
            )
        console.print(table)

    if files_over == 0:
        show_success("All bank files are within their size limit!")
    elif dry_run and files_over > 0:
        show_warning(
            f"{files_over} oversized file(s). Run with --apply to compact."
        )


def show_consolidation_job(result: dict):
    """Displays a consolidation job status."""
    status = result.get("status", "?")
    color = {
        "running": "cyan",
        "queued": "yellow",
        "succeeded": "green",
        "failed": "red",
        "cancelled": "yellow",
    }.get(status, "white")
    progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}

    lines = [
        f"[bold]Job ID     :[/bold] [cyan]{escape_markup(str(result.get('job_id', '?')))}[/cyan]",
        f"[bold]Space      :[/bold] {escape_markup(str(result.get('space_id', '?')))}",
        f"[bold]Status     :[/bold] [{color}]{escape_markup(str(status))}[/{color}]",
        f"[bold]Agent      :[/bold] {escape_markup(str(result.get('agent', '*') or 'all agents'))}",
        f"[bold]Requested  :[/bold] {escape_markup(str(result.get('requested_by', '?')))}",
        f"[bold]Position   :[/bold] {escape_markup(str(result.get('queue_position', '?')))}",
    ]
    if progress:
        phase = progress.get("phase", "?")
        lines.append(f"[bold]Phase      :[/bold] {escape_markup(str(phase))}")
        if phase == "compacting":
            lines.append("[dim]MID compaction in progress; no per-file percentage available.[/dim]")
        if progress.get("notes_total") is not None:
            lines.append(
                f"[bold]Notes      :[/bold] {progress.get('notes_done', 0)}/{progress.get('notes_total', '?')}"
            )
        if progress.get("batches_total") is not None:
            lines.append(
                f"[bold]Batches    :[/bold] {progress.get('batches_done', 0)}/{progress.get('batches_total', '?')}"
            )
    if result.get("error"):
        lines.append(f"[bold]Error      :[/bold] [red]{escape_markup(str(result['error']))}[/red]")
    job_result = result.get("result")
    # The advisory is measured before consolidation; maintenance has its own outcome.
    advisory_lines = _bank_size_advisory_lines(
        job_result.get("bank_size_advisory") if isinstance(job_result, dict) else None
    )
    if advisory_lines:
        lines.extend(
            [
                "[bold yellow]Bank size advisory[/bold yellow] "
                "(measured before consolidation; automatic maintenance is reported separately):",
                *advisory_lines,
            ]
        )
    # Les compteurs finaux sont affiches aussi pour un job echoue :
    # un arret au lot K laisse un acquis (lots completes, notes supprimees) et
    # une file en attente que l'operateur doit voir.
    if isinstance(job_result, dict) and job_result.get("notes_total") is not None:
        lines.append(
            "[bold]Counters   :[/bold] "
            f"total={job_result.get('notes_total', '?')} "
            f"processed={job_result.get('notes_processed', 0)} "
            f"useless={job_result.get('notes_discarded_count', 0)} "
            f"deleted={job_result.get('notes_deleted', 0)} "
            f"remaining={job_result.get('notes_remaining', 0)}"
        )
        for label, key in (("Failed batch", "failed_batch"), ("Failure    ", "failure_reason")):
            value = job_result.get(key)
            if isinstance(value, (int, str)) and value not in ("", None):
                lines.append(f"[bold]{label}:[/bold] {escape_markup(str(value))}")
    console.print(
        Panel.fit(
            "\n".join(lines),
            title=f"🔄 Consolidation Job — {status}",
            border_style=color,
        )
    )
    if status in ("queued", "running") and result.get("job_id"):
        console.print(
            "[dim]Accepted, not completed. Check later: "
            f"bank consolidation-status {escape_markup(str(result['job_id']))}[/dim]"
        )

    if isinstance(job_result, dict):
        maintenance = job_result.get("auto_compaction")
        if isinstance(maintenance, dict):
            lines = [f"Outcome: {escape_markup(str(maintenance.get('status', 'unknown')))}"]
            reason = maintenance.get("reason") or maintenance.get("failure_reason")
            if reason:
                lines.append(f"Reason: {escape_markup(str(reason))}")
            if maintenance.get("failed_phase"):
                lines.append(f"Phase: {escape_markup(str(maintenance['failed_phase']))}")
            if maintenance.get("recovery_required") is True:
                lines.append("[bold red]RECOVERY REQUIRED[/bold red]")
            for label, key in (("Started", "started_at"), ("Finished", "finished_at")):
                if maintenance.get(key):
                    lines.append(f"{label}: {escape_markup(_format_local_timestamp(maintenance[key]))}")
            if isinstance(maintenance.get("preimage_id"), str) and maintenance["preimage_id"]:
                lines.append("Originals retained; LONG indexing has its own status.")
            if maintenance.get("status") == "not_needed" and maintenance.get("files_over_limit") == 0:
                lines.append("No MID file exceeded the threshold; no new LONG capture from this job.")
            elif maintenance.get("status") == "disabled":
                lines.append("Automatic MID compaction is disabled by configuration.")
            elif maintenance.get("status") == "not_applicable":
                lines.append("This space is not eligible for automatic MID compaction.")
            console.print(Panel.fit("\n".join(lines), title="Automatic MID compaction"))
            if maintenance.get("status") == "ok":
                show_bank_compact_result(maintenance)
            elif maintenance.get("status") in ("error", "partial", "cancelled"):
                show_bank_compact_failure(maintenance)


def _space_status_jobs(info: dict):
    queue = info.get("consolidation_queue")
    queue = queue if isinstance(queue, dict) else {}
    jobs = queue.get("latest_jobs")
    jobs = jobs if isinstance(jobs, list) else []
    waiting = queue.get("queued_jobs")
    waiting = waiting if isinstance(waiting, list) else []
    active = queue.get("running_job")
    if not isinstance(active, dict):
        active = waiting[0] if waiting and isinstance(waiting[0], dict) else None
    if active is None and jobs and isinstance(jobs[0], dict):
        if jobs[0].get("status") in ("queued", "running"):
            active = jobs[0]
    terminal = next(
        (job for job in jobs if isinstance(job, dict)
         and job.get("status") in ("succeeded", "failed", "cancelled")),
        None,
    )
    return queue, active, terminal


def space_status_needs_recovery(info: dict) -> bool:
    """Whether the latest terminal compaction reports required recovery."""
    _, _, terminal = _space_status_jobs(info)
    return consolidation_job_needs_recovery(terminal)


def consolidation_job_needs_recovery(job: dict | None) -> bool:
    """Recovery signal from a consolidation job's separate compaction result."""
    result = job.get("result") if isinstance(job, dict) else None
    maintenance = result.get("auto_compaction") if isinstance(result, dict) else None
    return isinstance(maintenance, dict) and maintenance.get("recovery_required") is True


def _append_embedding_status(lines: list[str], label: str, status: dict):
    identity = status.get("embedding_identity")
    collection = status.get("embedding_collection")
    if not isinstance(identity, dict) or not isinstance(collection, dict):
        return
    persisted = identity.get("persisted")
    configured = identity.get("configured") or {}
    state = escape_markup(str(collection.get("state", "unknown")))
    if isinstance(persisted, dict):
        lines.append(f"{label} index: {escape_markup(str(persisted.get('model', '?')))} · {persisted.get('dimensions', '?')} dimensions · {state}")
    else:
        lines.append(f"{label} index: no verified stored model · {state}")
    if not persisted or any(persisted.get(key) != configured.get(key) for key in ("model", "provider", "dimensions")):
        lines.append(f"Configured: {escape_markup(str(configured.get('model', '?')))} · {configured.get('dimensions', '?')} dimensions")
    if collection.get("state") == "reindex_required":
        lines.append("Explicit reindex required before search; no model fallback")


def show_ingest_job(result: dict):
    """Display actual job fields; unknown progress remains unknown."""
    job = result.get("job") or result
    if not isinstance(job, dict) or not job.get("job_id"):
        show_error(result.get("message", "Job unavailable"))
        return
    lines = [f"Job: {escape_markup(str(job.get('job_id', '?')))}",
             f"Status: {escape_markup(str(job.get('status', 'unknown')))}"]
    if job.get("current_step"):
        lines.append(f"Step: {escape_markup(str(job['current_step']))}")
    progress = job.get("progress_percent")
    if type(progress) in (int, float) and 0 <= progress <= 100:
        lines.append(f"Progress: {progress:g}%")
    for key, label in (("started_at", "Started"), ("finished_at", "Finished")):
        if job.get(key):
            lines.append(f"{label}: {escape_markup(_format_local_timestamp(job[key]))}")
    if job.get("error"):
        lines.append(f"Error: {escape_markup(str(job['error']))}")
    console.print(Panel("\n".join(lines), title="LONG ingestion job", border_style="cyan"))


def _append_ingest_jobs(lines: list[str], label: str, running: dict, queued: dict):
    active_count = running.get("total")
    queued_count = queued.get("total")
    active_count = active_count if type(active_count) is int and active_count >= 0 else "unknown"
    queued_count = queued_count if type(queued_count) is int and queued_count >= 0 else "unknown"
    lines.append(f"         {label}: {active_count} running · {queued_count} queued")
    for job in (running.get("jobs") or [])[:3]:
        if isinstance(job, dict):
            step = escape_markup(str(job.get("current_step") or "step unknown"))
            pct = job.get("progress_percent")
            progress_text = f" · {pct}%" if type(pct) is int and 0 <= pct <= 100 else ""
            lines.append(f"         {escape_markup(str(job.get('job_id', '?')))} · {step}{progress_text}")
    for job in (queued.get("jobs") or [])[:2]:
        if isinstance(job, dict):
            lines.append(f"         Queued: {escape_markup(str(job.get('job_id', '?')))}")


def show_space_memory_status(
    info: dict, long_status: dict, running_jobs: dict | None = None, queued_jobs: dict | None = None,
    archive_running: dict | None = None, archive_queued: dict | None = None,
):
    """One compact operator reading of existing SHORT, MID and LONG status."""
    space_id = escape_markup(str(info.get("space_id", "?")))
    live = info.get("live") if isinstance(info.get("live"), dict) else {}
    bank = info.get("bank") if isinstance(info.get("bank"), dict) else {}
    queue, active, terminal = _space_status_jobs(info)
    notes = live.get("notes_count", "unknown")
    files = bank.get("files_count", "unknown")
    size = bank.get("total_size", "unknown")
    lines = [
        f"[bold cyan]SHORT[/bold cyan]  {escape_markup(str(notes))} notes awaiting consolidation",
        f"[bold cyan]MID[/bold cyan]    {escape_markup(str(files))} files · {escape_markup(str(size))} bytes",
    ]
    queued_count = queue.get("queued_count")
    if type(queued_count) is int and queued_count > 0:
        noun = "job" if queued_count == 1 else "jobs"
        lines.append(f"         {queued_count} consolidation {noun} queued")

    if active:
        job_status = escape_markup(str(active.get("status", "unknown")))
        started = active.get("started_at") or active.get("requested_at")
        when = _format_local_timestamp(started) if started else "time unavailable"
        agent = active.get("agent")
        scope = "all agents" if agent == "" else str(agent or "scope unknown")
        lines.append(f"         Current job: {job_status} · {escape_markup(scope)}")
        lines.append(f"         At: {escape_markup(when)}")
        progress = active.get("progress")
        if isinstance(progress, dict):
            phase = progress.get("phase")
            if phase:
                lines.append(f"         Phase: {escape_markup(str(phase))}")
            for label, done_key, total_key in (
                ("notes", "notes_done", "notes_total"),
                ("batches", "batches_done", "batches_total"),
            ):
                done, total = progress.get(done_key), progress.get(total_key)
                if type(done) is int and type(total) is int:
                    lines.append(f"         {done}/{total} {label}")
            if phase == "compacting":
                lines.append("         MID compaction in progress; no per-file percentage")
    if terminal:
        status = escape_markup(str(terminal.get("status", "unknown")))
        finished = terminal.get("finished_at") or terminal.get("started_at")
        when = _format_local_timestamp(finished) if finished else "time unavailable"
        lines.append(f"         Last result: {status} · {escape_markup(when)}")
        if terminal.get("error"):
            lines.append(f"         Error: {escape_markup(str(terminal['error']))}")
        result = terminal.get("result") if isinstance(terminal.get("result"), dict) else {}
        if type(result.get("notes_processed")) is int:
            lines.append(f"         {result['notes_processed']} notes processed")
        maintenance = result.get("auto_compaction")
        if isinstance(maintenance, dict):
            outcome = maintenance.get("status")
            if outcome == "not_needed" and maintenance.get("files_over_limit") == 0:
                lines.append("         No MID file exceeded threshold; no LONG capture")
            elif outcome == "ok":
                lines.append("         MID compaction completed")
                if maintenance.get("preimage_id"):
                    lines.append("         Original capture retained")
            elif outcome == "disabled":
                lines.append("         MID compaction disabled by configuration")
            elif outcome == "not_applicable":
                lines.append("         MID compaction not applicable to this space")
            elif outcome in ("error", "partial", "cancelled"):
                lines.append(f"         MID compaction {escape_markup(str(outcome))}; consolidation is separate")
            else:
                lines.append(f"         MID compaction: {escape_markup(str(outcome or 'unknown'))}")
            reason = maintenance.get("reason") or maintenance.get("failure_reason")
            if reason:
                lines.append(f"         Reason: {escape_markup(str(reason))}")
            if maintenance.get("failed_phase"):
                lines.append(f"         Phase: {escape_markup(str(maintenance['failed_phase']))}")
            if maintenance.get("rollback_outcome"):
                lines.append(f"         Rollback: {escape_markup(str(maintenance['rollback_outcome']))}")
            if maintenance.get("recovery_required") is True:
                lines.append("         [bold red]RECOVERY REQUIRED[/bold red] — inspect the job receipt")
            if (maintenance.get("recovery_required") is True or outcome in ("error", "partial", "cancelled")) and terminal.get("job_id"):
                lines.append(f"         bank consolidation-status {escape_markup(str(terminal['job_id']))}")
        elif terminal.get("status") in ("succeeded", "failed", "cancelled"):
            lines.append("         No automatic compaction outcome recorded for this job")
    if not active and not terminal:
        lines.append("         No recent job in this server process")
    elif active and not terminal and len(queue.get("latest_jobs") or []) >= 10:
        lines.append("         Last result: not among the 10 most recent jobs")

    policy = long_status.get("mid_automation")
    policy = policy if isinstance(policy, dict) else {}
    projection = long_status.get("mid_archive_projection")
    projection = projection if isinstance(projection, dict) else {}
    if long_status.get("status") != "ok":
        lines.append("[bold cyan]LONG[/bold cyan]   Status unavailable; check graph status for details")
    else:
        archive = policy.get("archive_enabled")
        transfer = "enabled" if archive is True else "disabled" if archive is False else "unknown"
        pending = projection.get("pending")
        if projection.get("error") == "projection_unavailable":
            pending_text = "capture backlog unknown"
        elif type(pending) is int and pending >= 0:
            noun = "capture" if pending == 1 else "captures"
            pending_text = f"{pending} {noun} pending indexing"
        else:
            pending_text = "capture backlog unknown"
        lines.append(f"[bold cyan]LONG[/bold cyan]   Transfer {transfer} · {pending_text}")
        if type(pending) is int and pending > 0 and projection.get("oldest_at"):
            lines.append(
                f"         Oldest capture: {escape_markup(_format_local_timestamp(projection['oldest_at']))}"
            )
            age = projection.get("oldest_age_seconds")
            if type(age) in (int, float) and age >= 0:
                hours, remainder = divmod(int(age), 3600)
                minutes = remainder // 60
                lines.append(f"         Age: {hours}h {minutes}m")
        if projection.get("error"):
            lines.append(f"         Indexing problem: {escape_markup(str(projection['error']))}")
        if long_status.get("connected") is False:
            if long_status.get("embedded") is True or long_status.get("bound") is False:
                lines.append("         Waiting for first ingestion; LONG binds automatically")
            else:
                lines.append("         Graph is not connected")
        elif long_status.get("reachable") is False:
            lines.append("         Graph service unreachable")
        lines.append("         Backlog count is not proof every MID file is indexed")
        if running_jobs is not None or queued_jobs is not None:
            if any(response is not None and response.get("status") != "ok"
                   for response in (running_jobs, queued_jobs)):
                failure = next(
                    response for response in (running_jobs, queued_jobs)
                    if response is not None and response.get("status") != "ok"
                )
                reason = failure.get("message") or failure.get("status") or "unknown"
                lines.append(f"         Ingestion jobs unavailable: {escape_markup(str(reason))}")
            else:
                _append_ingest_jobs(lines, "Document ingestion", running_jobs or {}, queued_jobs or {})
        if (type(pending) is int and pending > 0) or archive_running is not None:
            if archive_running and archive_running.get("reason") == "archive_not_configured":
                lines.append("         Capture indexing: archive not yet created")
            elif archive_running is None:
                lines.append("         Capture-indexing jobs unavailable")
            elif archive_running.get("status") != "ok" or (archive_queued is not None and archive_queued.get("status") != "ok"):
                failure = archive_running if archive_running.get("status") != "ok" else archive_queued
                lines.append(f"         Capture indexing unavailable: {escape_markup(str(failure.get('message') or failure.get('reason') or 'unknown'))}")
            elif archive_queued is not None:
                _append_ingest_jobs(lines, "Capture indexing", archive_running, archive_queued)
    _append_embedding_status(lines, "LONG", long_status)
    archive_index = long_status.get("mid_archive_index")
    if isinstance(archive_index, dict) and archive_index.get("status") == "ok":
        _append_embedding_status(lines, "MID archive", archive_index)
    if active and active.get("job_id"):
        lines.append(f"[dim]Next: bank consolidation-status {escape_markup(str(active['job_id']))}[/dim]")
    elif type(notes) is int and notes > 0:
        lines.append(f"[dim]Own notes: bank consolidate {space_id} (write token)[/dim]")
        lines.append(f"[dim]All agents: bank consolidate {space_id} --all-agents (manage)[/dim]")
    console.print(Panel("\n".join(lines), title=f"Memory · {space_id}", border_style="cyan"))


def show_stale_spaces(result: dict):
    """Displays spaces flagged as stale (too many unconsolidated notes)."""
    spaces = result.get("spaces", [])
    summary = (
        f"[bold]Stale spaces  :[/bold] {result.get('total_stale', len(spaces))}\n"
        f"[bold]Scanned       :[/bold] {result.get('total_spaces', '?')}\n"
        f"[bold]Min notes     :[/bold] {result.get('min_notes', '?')}\n"
        f"[bold]Min age (days):[/bold] {result.get('min_age_days', '?')}"
    )
    color = "red" if spaces else "green"
    title_icon = "🚨" if spaces else "✅"
    console.print(
        Panel.fit(
            summary,
            title=f"{title_icon} Stale Memory Banks",
            border_style=color,
        )
    )

    if not spaces:
        console.print("[dim]No space matches the staleness thresholds.[/dim]")
        return

    table = Table(show_header=True)
    table.add_column("Space", style="cyan bold")
    table.add_column("Notes", justify="right")
    table.add_column("Oldest (days)", justify="right")
    table.add_column("Oldest timestamp", style="dim")
    for s in spaces:
        age = s.get("oldest_note_age_days", 0)
        age_str = f"{age:.1f}"
        age_style = "red" if age >= 14 else "yellow" if age >= 7 else "white"
        table.add_row(
            s.get("space_id", "?"),
            str(s.get("live_notes_count", 0)),
            f"[{age_style}]{age_str}[/{age_style}]",
            _format_local_timestamp(s.get("oldest_note_timestamp")),
        )
    console.print(table)

    denied = result.get("denied_spaces", [])
    if denied:
        console.print(
            f"[dim]({len(denied)} space(s) denied — insufficient permissions)[/dim]"
        )


def show_consolidation_queues(result: dict):
    """Displays consolidation queue lanes per space."""
    spaces = result.get("spaces", [])
    totals = result.get("totals", {})

    summary = (
        f"[bold]Spaces     :[/bold] {totals.get('spaces_total', len(spaces))}\n"
        f"[bold]Running    :[/bold] {totals.get('running', 0)}\n"
        f"[bold]Queued     :[/bold] {totals.get('queued', 0)}\n"
        f"[bold]Guarantee  :[/bold] {result.get('guarantee', '?')}"
    )
    console.print(
        Panel.fit(summary, title="🔄 Consolidation Lanes", border_style="cyan")
    )

    if spaces:
        table = Table(show_header=True)
        table.add_column("Space", style="cyan bold")
        table.add_column("Lane", justify="center")
        table.add_column("Running", justify="center")
        table.add_column("Queued", justify="right")

        for s in spaces:
            lane = s.get("lane_state", "idle")
            lane_color = {"running": "cyan", "queued": "yellow", "idle": "dim", "failed": "red"}.get(lane, "white")
            running = s.get("running_job")
            running_str = running.get("job_id", "—")[:16] if running else "—"
            table.add_row(
                s.get("space_id", "?"),
                f"[{lane_color}]{lane}[/{lane_color}]",
                running_str,
                str(s.get("queued_count", 0)),
            )
        console.print(table)


# =============================================================================
# Admin tokens
# =============================================================================


def show_token_created(result: dict):
    """Displays a created token (with warning)."""
    uncertain = (
        result.get("status") == "partial"
        and result.get("recovery_required") is True
    )
    permissions = result.get("permissions", [])
    spaces = result.get("space_ids", [])
    if "admin" in permissions:
        spaces_label = "all (admin)"
    elif spaces:
        spaces_label = ", ".join(spaces)
    else:
        spaces_label = "none — grant with `space invite <space_id> <full_hash>`"
    hash_line = (
        f"[bold]Full hash:[/bold] [cyan]{result['token_hash']}[/cyan]\n"
        if result.get("token_hash")
        else ""
    )
    recovery_warning = (
        "\n[bold yellow]CREATION STATE UNCERTAIN — do not discard either value. "
        "Do not assume the token is active or absent. Ask an admin to inspect "
        "and validate the registry before retrying, revoking, or granting access.[/bold yellow]\n"
        if uncertain
        else ""
    )
    server_notes = "\n".join(
        str(value)
        for value in (
            result.get("message"),
            result.get("info"),
            result.get("warning_no_access"),
        )
        if value
    )
    console.print(
        Panel.fit(
            f"[bold]Name:[/bold] {result.get('name', '?')}\n"
            f"[bold red]Plaintext token:[/bold red] [red]{result.get('token', '?')}[/red]\n"
            f"{hash_line}"
            f"[bold]Perms:[/bold] {', '.join(permissions)}\n"
            f"[bold]Spaces:[/bold] {spaces_label}\n"
            f"[bold]Expires:[/bold] {_format_local_timestamp(result.get('expires_at')) or 'never'}\n\n"
            f"{recovery_warning}"
            f"{server_notes}\n"
            f"[bold yellow]{result.get('warning', '')}[/bold yellow]",
            title="Token Creation State Uncertain" if uncertain else "🔑 Token Created",
            border_style="yellow" if uncertain else "red",
        )
    )


def show_token_updated(result: dict):
    """Display a token update without hiding server transition guidance."""
    show_success(result.get("message", "Token updated"))
    if result.get("info"):
        console.print(str(result["info"]))
    if result.get("warning_no_access"):
        show_warning(str(result["warning_no_access"]))


def show_token_list(result: dict):
    """Displays the list of tokens."""
    tokens = result.get("tokens", [])
    table = Table(title=f"🔑 {result.get('total', 0)} tokens", show_header=True)
    table.add_column("Name", style="cyan bold")
    table.add_column("Email")
    table.add_column("Hash (ID)", style="dim")
    table.add_column("Permissions")
    table.add_column("Spaces")
    table.add_column("Created (local)")
    table.add_column("Expires (local)")
    for t in tokens:
        created = _format_local_timestamp(t.get("created_at"), date_only=True) or "?"
        expires = t.get("expires_at") or None
        expires = _format_local_timestamp(expires, date_only=True) if expires else "never"
        is_admin_token = "admin" in t.get("permissions", [])
        spaces = ", ".join(t.get("space_ids", [])) or ("all" if is_admin_token else "none")
        name = t.get("name", "?")
        if t.get("revoked"):
            name = f"[dim strikethrough]{name}[/dim strikethrough]"
        # Hash truncated to 24 chars min (sufficient for token update/revoke
        # which require 16 chars min). Full hash available via --json.
        raw_hash = t.get("hash", "?")
        token_hash = raw_hash[:24] + "…" if len(raw_hash) > 24 else raw_hash
        table.add_row(
            name,
            t.get("email", "") or "",
            token_hash,
            ", ".join(t.get("permissions", [])),
            spaces,
            created,
            expires,
        )
    console.print(table)
    # Filtres actifs (issue #13)
    filters = result.get("filters") or {}
    if filters:
        parts = []
        if filters.get("name_contains"):
            parts.append(f"name~={filters['name_contains']}")
        if filters.get("has_space"):
            parts.append(f"has_space={filters['has_space']}")
        if filters.get("include_revoked") is False:
            parts.append("no-revoked")
        if parts:
            console.print(f"[dim]🔎 Active filters: {', '.join(parts)}[/dim]")
    # Aide contextuelle
    console.print(
        "[dim]💡 Copy the Hash for: token revoke <hash> · token update <hash> --email user@example.com · token delete <hash>[/dim]"
    )


def show_bulk_update_result(result: dict):
    """Displays the bulk update report (issue #13)."""
    updated = result.get("updated", 0)
    tokens = result.get("tokens", [])
    filters = result.get("filters", {})
    operations = result.get("operations", {})

    if updated == 0:
        show_warning(result.get("message", "No tokens modified."))
        if filters:
            console.print(f"[dim]Filters: {filters}[/dim]")
        return

    show_success(f"{updated} token(s) updated")

    # Summary of requested operations
    op_parts = []
    if operations.get("space_ids_add"):
        op_parts.append(f"+spaces={operations['space_ids_add']}")
    if operations.get("space_ids_remove"):
        op_parts.append(f"-spaces={operations['space_ids_remove']}")
    if operations.get("permissions"):
        op_parts.append(f"perms={operations['permissions']}")
    if operations.get("email"):
        op_parts.append(f"email={operations['email']}")
    if op_parts:
        console.print(f"[dim]Operations: {', '.join(op_parts)}[/dim]")

    # Table before/after par token
    table = Table(title="📋 Modification details", show_header=True)
    table.add_column("Token", style="cyan bold")
    table.add_column("Added", style="green")
    table.add_column("Removed", style="red")
    table.add_column("Spaces before", style="dim")
    table.add_column("Spaces after")
    table.add_column("No-op", style="yellow")
    for t in tokens:
        before = t.get("before", {})
        after = t.get("after", {})
        added = ", ".join(t.get("space_ids_added", [])) or "—"
        removed = ", ".join(t.get("space_ids_removed", [])) or "—"
        noop = ", ".join(t.get("space_ids_noop", [])) or "—"
        before_spaces = ", ".join(before.get("space_ids", [])) or "(none)"
        after_spaces = ", ".join(after.get("space_ids", [])) or "(none)"
        table.add_row(
            t.get("name", "?"),
            added,
            removed,
            before_spaces,
            after_spaces,
            noop,
        )
    console.print(table)


# =============================================================================
# Backup
# =============================================================================


def show_backup_created(result: dict):
    """Displays a created backup."""
    show_success(
        f"Backup '{result.get('backup_id', '?')}' — "
        f"{result.get('files_backed_up', 0)} files, "
        f"{result.get('total_size', 0)} bytes"
    )


def show_backup_all_result(result: dict):
    """Displays the result of an all-spaces backup."""
    ok = result.get("spaces_backed_up", 0)
    failed = result.get("spaces_failed", 0)
    total = result.get("spaces_total", 0)
    border = "green" if failed == 0 else "yellow"

    console.print(
        Panel.fit(
            f"[bold]Total spaces   :[/bold] {total}\n"
            f"[bold]Backed up      :[/bold] [green]{ok}[/green]\n"
            f"[bold]Failed         :[/bold] {'[red]' + str(failed) + '[/red]' if failed else '0'}",
            title="💾 Backup ALL",
            border_style=border,
        )
    )

    details = result.get("details", [])
    if details:
        table = Table(title="Details per space", show_header=True)
        table.add_column("Space", style="cyan bold")
        table.add_column("Backup ID", style="dim")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Status")

        for d in details:
            if d.get("status") == "created":
                table.add_row(
                    d.get("space_id", "?"),
                    d.get("backup_id", ""),
                    str(d.get("files", 0)),
                    f"{d.get('size', 0)} B",
                    "✅",
                )
            else:
                table.add_row(
                    d.get("space_id", "?"),
                    "",
                    "",
                    "",
                    f"[red]❌ {d.get('message', '?')}[/red]",
                )
        console.print(table)


# =============================================================================
# Graph Bridge
# =============================================================================


def show_graph_connected(result: dict):
    """Displays the graph_connect result."""
    gm = result.get("graph_memory", {})
    created = "✨ created" if gm.get("memory_created") else "already existed"
    console.print(
        Panel.fit(
            f"[bold]Space :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]URL :[/bold] {gm.get('url', '?')}\n"
            f"[bold]Memory ID :[/bold] [green]{gm.get('memory_id', '?')}[/green]\n"
            f"[bold]Ontology  :[/bold] {gm.get('ontology', '?')}\n"
            f"[bold]Memory    :[/bold] {created}",
            title="🌉 Connected to Graph Memory",
            border_style="green",
        )
    )


def show_graph_status(result: dict):
    """Displays the graph_status result."""
    embedding_lines = []
    _append_embedding_status(embedding_lines, "LONG", result)
    archive_index = result.get("mid_archive_index")
    if isinstance(archive_index, dict) and archive_index.get("status") == "ok":
        _append_embedding_status(embedding_lines, "MID archive", archive_index)
    if embedding_lines:
        console.print(Panel("\n".join(embedding_lines), title="Embedding models", border_style="cyan"))
    policy = result.get("mid_automation")
    if isinstance(policy, dict):
        def mode(key):
            value = policy.get(key)
            return "Enabled" if value is True else "Disabled by configuration" if value is False else "Unknown"
        lines = [f"Automatic compaction: {mode('compaction_enabled')}",
                 f"Automatic transfer to LONG: {mode('archive_enabled')}",
                 "Compaction: after successful consolidation, oversized files only (DirectLocal)."]
        backlog = result.get("mid_archive_projection")
        if isinstance(backlog, dict):
            pending = backlog.get("pending")
            if type(pending) is int and pending >= 0:
                lines.append(f"Captures pending indexing: {pending}")
            if backlog.get("oldest_at"):
                lines.append(f"Oldest capture: {escape_markup(_format_local_timestamp(backlog['oldest_at']))}")
            if backlog.get("error"):
                lines.append(f"Indexing problem: {escape_markup(str(backlog['error']))}")
            lines.append("A zero backlog does not mean every current MID file is indexed.")
        console.print(Panel.fit("\n".join(lines), title="MID → LONG automation", border_style="blue"))
    connected = result.get("connected", False)
    if not connected:
        console.print(
            Panel.fit(
                f"[bold]Space :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
                f"[dim]{result.get('message', 'Not connected')}[/dim]",
                title="🌉 Graph Memory — Not connected",
                border_style="dim",
            )
        )
        return

    config = result.get("config", {})
    reachable = result.get("reachable", False)
    stats = result.get("graph_stats")
    docs = result.get("graph_documents", [])
    top = result.get("top_entities", [])

    # Section config
    lines = [
        f"[bold]URL :[/bold] {config.get('url', '?')}",
        f"[bold]Memory ID :[/bold] [green]{config.get('memory_id', '?')}[/green]",
        f"[bold]Ontology  :[/bold] {config.get('ontology', '?')}",
        f"[bold]Reachable  :[/bold] {'✅ yes' if reachable else '❌ no'}",
    ]

    # Section pushs
    if result.get("last_push"):
        lines.append(f"[bold]Last push   :[/bold] {_format_local_timestamp(result['last_push'])}")
        lines.append(f"[bold]Total pushes:[/bold] {result.get('push_count', 0)}")
        lines.append(f"[bold]Files      :[/bold] {result.get('files_pushed', 0)}")

    console.print(
        Panel.fit(
            "\n".join(lines), title="🌉 Graph Memory — Config", border_style="blue"
        )
    )

    # Section stats graphe
    if stats:
        table = Table(title="📊 Graph statistics", show_header=True)
        table.add_column("Metric", style="cyan bold")
        table.add_column("Value", justify="right")
        table.add_row("Documents", str(stats.get("document_count", 0)))
        table.add_row("Entities", str(stats.get("entity_count", 0)))
        table.add_row("Relations", str(stats.get("relation_count", 0)))
        console.print(table)

    # Section documents
    if docs:
        table = Table(title="📄 Ingested documents", show_header=True)
        table.add_column("File", style="cyan bold")
        table.add_column("Entities", justify="right")
        table.add_column("Size", justify="right")
        for d in docs:
            table.add_row(
                d.get("filename", "?"),
                str(d.get("entity_count", 0)),
                f"{d.get('size', 0)} B",
            )
        console.print(table)

    # Top entities section
    if top:
        table = Table(title="🏷️  Top entities", show_header=True)
        table.add_column("Type", style="magenta")
        table.add_column("Name", style="cyan bold")
        for e in top[:10]:
            if isinstance(e, dict):
                table.add_row(
                    e.get("type", "?"),
                    e.get("name", "?"),
                )
            else:
                table.add_row("", str(e))
        console.print(table)


def show_graph_push_result(result: dict):
    """Displays the graph_push result."""
    errs = result.get("errors", 0)
    border = "green" if errs == 0 else "yellow"
    lines = [
        f"[bold]Files pushed    :[/bold] {result.get('pushed', 0)}",
        f"[bold]Deleted (re-ingest):[/bold] {result.get('deleted_before_reingest', 0)}",
        f"[bold]Orphans cleaned :[/bold] {result.get('cleaned_orphans', 0)}",
        f"[bold]Errors         :[/bold] {'[red]' + str(errs) + '[/red]' if errs else '0'}",
        f"[bold]Duration   :[/bold] {result.get('duration_seconds', 0)}s",
    ]
    error_details = result.get("error_details", [])
    if error_details:
        lines.append("")
        for ed in error_details:
            lines.append(
                f"  [red]✗ {ed.get('filename', '?')} : {ed.get('error', '?')}[/red]"
            )
    console.print(
        Panel.fit("\n".join(lines), title="📤 Push Graph Memory", border_style=border)
    )


def show_graph_disconnected(result: dict):
    """Displays the graph_disconnect result."""
    was = result.get("was_connected_to", {})
    console.print(
        Panel.fit(
            f"[bold]Space :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Was connected to:[/bold] {was.get('memory_id', '?')}\n"
            f"[bold]URL :[/bold] {was.get('url', '?')}\n"
            f"[bold]Pushes done    :[/bold] {was.get('push_count', 0)}",
            title="🔌 Disconnected from Graph Memory",
            border_style="yellow",
        )
    )


def show_graph_local(result: dict):
    """Displays the graph use-local maintenance result."""
    gm = result.get("graph_memory", {})
    previous = result.get("previous_graph_memory") or {}
    previous_label = previous.get("memory_id") or "unbound"
    console.print(
        Panel.fit(
            f"[bold]Space :[/bold] [cyan]{result.get('space_id', '?')}[/cyan]\n"
            f"[bold]Previous memory:[/bold] {previous_label}\n"
            f"[bold]Local URL :[/bold] {gm.get('url', '?')}\n"
            f"[bold]Local memory ID:[/bold] [green]{gm.get('memory_id', '?')}[/green]\n"
            f"[bold]Ontology :[/bold] {gm.get('ontology', '?')}\n"
            "[dim]Remote Graph data was not deleted; no document was ingested.[/dim]",
            title="🔁 Using embedded/local Graph Memory",
            border_style="green",
        )
    )


# =============================================================================
# Backup
# =============================================================================


def show_backup_list(result: dict):
    """Displays the list of backups."""
    backups = result.get("backups", [])
    table = Table(title=f"💾 {result.get('total', 0)} backups", show_header=True)
    table.add_column("Backup ID", style="cyan bold")
    table.add_column("Space", style="dim")
    table.add_column("Timestamp (local)")
    for b in backups:
        table.add_row(
            b.get("backup_id", "?"),
            b.get("space_id", "?"),
            _format_local_timestamp(b.get("timestamp")) or "?",
        )
    console.print(table)
