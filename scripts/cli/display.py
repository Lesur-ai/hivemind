# -*- coding: utf-8 -*-
"""
Rich display functions — shared between CLI Click and interactive Shell.

Each MCP tool has its show_xxx_result() function for colored rendering.
These functions are imported in both commands.py and shell.py (DRY).
"""

import json
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
    console.print(f"[red]❌ {msg}[/red]")


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
        lines.append(f"[bold]Created  :[/bold] {result['created_at'][:19]}")
    expires = result.get("expires_at")
    if expires:
        lines.append(f"[bold]Expire   :[/bold] {expires[:19]}")
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
            f"[bold]Created:[/bold] {result.get('created_at', '')}",
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
            f"[bold]Last:[/bold] {result.get('last_consolidation', 'never')}",
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
            n.get("timestamp", "")[:19],
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
            f"[bold]Notes processed:[/bold] {result.get('notes_processed', 0)}\n"
            f"[bold]Files created  :[/bold] {result.get('bank_files_created', 0)}\n"
            f"[bold]Files updated  :[/bold] {result.get('bank_files_updated', 0)}\n"
            f"[bold]Synthesis  :[/bold] {result.get('synthesis_size', 0)} chars\n"
            f"[bold]LLM tokens :[/bold] {result.get('llm_tokens_used', 0)}\n"
            f"[bold]Duration   :[/bold] {result.get('duration_seconds', 0)}s",
            title="🧠 Consolidation complete",
            border_style="green",
        )
    )


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
    }.get(status, "white")
    progress = result.get("progress", {})

    lines = [
        f"[bold]Job ID     :[/bold] [cyan]{result.get('job_id', '?')}[/cyan]",
        f"[bold]Space      :[/bold] {result.get('space_id', '?')}",
        f"[bold]Status     :[/bold] [{color}]{status}[/{color}]",
        f"[bold]Agent      :[/bold] {result.get('agent', '*')}",
        f"[bold]Requested  :[/bold] {result.get('requested_by', '?')}",
        f"[bold]Position   :[/bold] {result.get('queue_position', '?')}",
    ]
    if progress:
        phase = progress.get("phase", "?")
        lines.append(f"[bold]Phase      :[/bold] {phase}")
        if progress.get("notes_total") is not None:
            lines.append(
                f"[bold]Notes      :[/bold] {progress.get('notes_done', 0)}/{progress.get('notes_total', '?')}"
            )
        if progress.get("batches_total") is not None:
            lines.append(
                f"[bold]Batches    :[/bold] {progress.get('batches_done', 0)}/{progress.get('batches_total', '?')}"
            )
    if result.get("error"):
        lines.append(f"[bold]Error      :[/bold] [red]{result['error']}[/red]")
    job_result = result.get("result")
    compaction_failure_lines = _safe_compaction_failure_lines(
        job_result.get("compaction_failures")
        if isinstance(job_result, dict)
        else None
    )
    if compaction_failure_lines:
        lines.extend(["[bold]Compaction failures:[/bold]", *compaction_failure_lines])

    console.print(
        Panel.fit(
            "\n".join(lines),
            title=f"🔄 Consolidation Job — {status}",
            border_style=color,
        )
    )


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
            (s.get("oldest_note_timestamp", "") or "")[:19].replace("T", " "),
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
            f"[bold]Expires:[/bold] {result.get('expires_at', 'never')}\n\n"
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
    table.add_column("Created")
    table.add_column("Expires")
    for t in tokens:
        created = t.get("created_at", "?")[:10] if t.get("created_at") else "?"
        expires = t.get("expires_at") or None
        expires = expires[:10] if expires else "never"
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
        lines.append(f"[bold]Last push   :[/bold] {result['last_push'][:19]}")
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
    table.add_column("Timestamp")
    for b in backups:
        table.add_row(
            b.get("backup_id", "?"), b.get("space_id", "?"), b.get("timestamp", "?")
        )
    console.print(table)
