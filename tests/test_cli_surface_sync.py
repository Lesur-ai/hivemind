# -*- coding: utf-8 -*-
"""
Regression tests for CLI/admin surface parity.

The web admin console calls MCP tools directly through /api/tool. The script
entry point delegates to Click, while the interactive shell has its own
dispatcher. These tests pin the critical bank supervision commands so future
tool additions do not silently land in one surface only.
"""

import asyncio
import inspect
import io
import sys
from pathlib import Path

from click.testing import CliRunner


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cli.commands import cli  # noqa: E402
from cli import commands, display, shell  # noqa: E402
import test_recette as recipe_runner  # noqa: E402


def test_mcp_cli_entrypoint_delegates_to_click_cli():
    source = (ROOT / "scripts" / "mcp_cli.py").read_text(encoding="utf-8")

    assert "from cli.commands import cli" in source
    assert "cli()" in source


def test_click_exposes_stale_spaces_with_admin_console_contract():
    result = CliRunner().invoke(cli, ["bank", "stale-spaces", "--help"])

    assert result.exit_code == 0
    assert "--min-notes" in result.output
    assert "--min-age-days" in result.output
    assert "--space-ids" in result.output
    assert "--consolidate" in result.output
    assert "--all-agents" in result.output
    assert "--json" in result.output
    assert "bank_consolidate" in result.output


def test_click_consolidate_defaults_to_own_and_serializes_explicit_global(
    monkeypatch,
):
    captured = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        captured.append((tool_name, args, json_flag))

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    runner = CliRunner()

    own = runner.invoke(cli, ["bank", "consolidate", "proj"])
    all_agents = runner.invoke(
        cli, ["bank", "consolidate", "proj", "--all-agents", "--json"]
    )

    assert own.exit_code == 0, own.output
    assert all_agents.exit_code == 0, all_agents.output
    assert captured == [
        ("bank_consolidate", {"space_id": "proj"}, False),
        ("bank_consolidate", {"space_id": "proj", "agent": ""}, True),
    ]


def test_click_stale_all_agents_requires_consolidate():
    result = CliRunner().invoke(
        cli, ["bank", "stale-spaces", "--all-agents"]
    )

    assert result.exit_code == 2
    assert "--all-agents requires --consolidate" in result.output


class _RecipeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class _RecipeClient:
    def __init__(self, acknowledgement, statuses):
        self.acknowledgement = acknowledgement
        self.statuses = list(statuses)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "bank_consolidate":
            return self.acknowledgement
        assert name == "bank_consolidation_status"
        if self.statuses:
            return self.statuses.pop(0)
        return {"status": "running", "job_id": args["job_id"]}


async def test_recipe_waits_from_async_ack_to_terminal_success():
    clock = _RecipeClock()
    client = _RecipeClient(
        {"status": "queued", "job_id": "consol_success"},
        [
            {"status": "running", "job_id": "consol_success"},
            {
                "status": "succeeded",
                "job_id": "consol_success",
                "result": {"status": "ok", "notes_processed": 3},
            },
        ],
    )

    outcome = await recipe_runner._consolidate_and_wait(
        client,
        "project-a",
        timeout_seconds=5,
        poll_interval_seconds=1,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["result"]["notes_processed"] == 3
    assert client.calls == [
        ("bank_consolidate", {"space_id": "project-a"}),
        ("bank_consolidation_status", {"job_id": "consol_success"}),
        ("bank_consolidation_status", {"job_id": "consol_success"}),
    ]


async def test_recipe_returns_terminal_failure_without_reenqueueing():
    clock = _RecipeClock()
    client = _RecipeClient(
        {"status": "running", "job_id": "consol_failed"},
        [
            {
                "status": "failed",
                "job_id": "consol_failed",
                "error": "provider rejected request",
                "result": {"status": "error", "message": "provider rejected"},
            }
        ],
    )

    outcome = await recipe_runner._consolidate_and_wait(
        client,
        "project-a",
        timeout_seconds=5,
        poll_interval_seconds=1,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert outcome["status"] == "failed"
    assert recipe_runner._consolidation_failure_detail(outcome) == (
        "provider rejected request"
    )
    assert [name for name, _ in client.calls] == [
        "bank_consolidate",
        "bank_consolidation_status",
    ]


async def test_recipe_consolidation_wait_is_bounded_and_reports_last_status():
    clock = _RecipeClock()
    client = _RecipeClient(
        {"status": "queued", "job_id": "consol_timeout"},
        [
            {"status": "running", "job_id": "consol_timeout"},
            {"status": "running", "job_id": "consol_timeout"},
        ],
    )

    outcome = await recipe_runner._consolidate_and_wait(
        client,
        "project-a",
        timeout_seconds=3,
        poll_interval_seconds=1,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert outcome == {
        "status": "timeout",
        "job_id": "consol_timeout",
        "last_status": "running",
        "message": (
            "consolidation job consol_timeout did not reach a terminal state "
            "within 3s (last status: running)"
        ),
    }
    assert [name for name, _ in client.calls].count(
        "bank_consolidation_status"
    ) == 2


async def test_recipe_rejects_ack_without_job_id_before_status_read():
    clock = _RecipeClock()
    client = _RecipeClient({"status": "running"}, [])

    outcome = await recipe_runner._consolidate_and_wait(
        client,
        "project-a",
        timeout_seconds=3,
        poll_interval_seconds=1,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert outcome["status"] == "invalid_ack"
    assert client.calls == [("bank_consolidate", {"space_id": "project-a"})]


async def test_recipe_hard_deadline_also_bounds_a_stuck_status_call():
    class StuckStatusClient:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            if name == "bank_consolidate":
                return {"status": "running", "job_id": "consol_stuck"}
            await asyncio.Event().wait()

    client = StuckStatusClient()
    outcome = await recipe_runner._consolidate_and_wait(
        client,
        "project-a",
        timeout_seconds=0.05,
        poll_interval_seconds=0.001,
    )

    assert outcome["status"] == "timeout"
    assert outcome["job_id"] == "consol_stuck"
    assert [name for name, _ in client.calls] == [
        "bank_consolidate",
        "bank_consolidation_status",
    ]


def test_recipe_three_e2e_paths_use_terminal_wait_helper():
    source = (ROOT / "scripts" / "test_recette.py").read_text(encoding="utf-8")

    for space_constant in ("RECETTE_SPACE", "QUALITE_SPACE", "GRAPH_SPACE"):
        assert (
            f"outcome = await _consolidate_and_wait(agent, {space_constant})"
            in source
        )
    assert source.count("outcome = await _consolidate_and_wait(agent,") == 3
    assert "aucun push long exécuté" in source


def test_click_exposes_consolidation_queues():
    result = CliRunner().invoke(cli, ["bank", "consolidation-queues", "--help"])

    assert result.exit_code == 0
    assert "Show consolidation lanes per space" in result.output
    assert "SPACE_IDS" in result.output
    assert "--json" in result.output


def test_click_bank_delete_requires_confirm_and_forwards_true(monkeypatch):
    captured = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        captured.append((tool_name, args, renderer, json_flag))

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    runner = CliRunner()

    help_result = runner.invoke(cli, ["bank", "delete", "--help"])
    unconfirmed = runner.invoke(cli, ["bank", "delete", "proj", "progress.md"])
    confirmed = runner.invoke(
        cli, ["bank", "delete", "proj", "progress.md", "--confirm", "--json"]
    )

    assert help_result.exit_code == 0, help_result.output
    assert "--confirm" in help_result.output
    assert "manage" in help_result.output
    assert unconfirmed.exit_code == 0, unconfirmed.output
    assert confirmed.exit_code == 0, confirmed.output
    assert captured == [
        (
            "bank_delete",
            {"space_id": "proj", "filename": "progress.md", "confirm": True},
            display.show_bank_delete_result,
            True,
        )
    ]


def test_click_token_purge_guard_forwards_confirm_to_total_purge(monkeypatch):
    captured = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        captured.append((tool_name, args, json_flag))

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    runner = CliRunner()

    unconfirmed = runner.invoke(cli, ["token", "purge", "--all"])
    confirmed = runner.invoke(
        cli, ["token", "purge", "--all", "--confirm", "--json"]
    )

    assert unconfirmed.exit_code == 0, unconfirmed.output
    assert confirmed.exit_code == 0, confirmed.output
    assert captured == [
        ("admin_purge_tokens", {"revoked_only": False, "confirm": True}, True)
    ]


async def test_shell_destructive_bank_and_token_routes_require_and_forward_confirm():
    client = _ShellClient()

    await shell.dispatch(client, "bank delete proj progress.md", True)
    await shell.dispatch(client, "token purge --all", True)
    await shell.dispatch(client, "bank delete proj progress.md --confirm", True)
    await shell.dispatch(client, "token purge --all --confirm", True)

    assert client.calls == [
        (
            "bank_delete",
            {"space_id": "proj", "filename": "progress.md", "confirm": True},
        ),
        ("admin_purge_tokens", {"revoked_only": False, "confirm": True}),
    ]


def test_cli_help_uses_manage_and_routes_rules_updates_separately():
    runner = CliRunner()
    update_help = runner.invoke(cli, ["space", "update", "--help"])
    update_rules_help = runner.invoke(cli, ["space", "update-rules", "--help"])

    assert update_help.exit_code == 0, update_help.output
    assert "Rules remain immutable" not in update_help.output
    assert "space update-rules" in update_help.output
    assert update_rules_help.exit_code == 0, update_rules_help.output
    assert "manage permission required" in update_rules_help.output
    assert "admin only" not in update_rules_help.output

    for argv in (
        ["bank", "write", "--help"],
        ["bank", "repair", "--help"],
        ["bank", "compact", "--help"],
        ["graph", "use-local", "--help"],
    ):
        help_result = runner.invoke(cli, argv)
        assert help_result.exit_code == 0, help_result.output
        assert "manage" in help_result.output
        assert "admin" not in help_result.output

    for command in ("bank write", "bank delete", "bank repair", "bank compact"):
        assert "manage" in shell.SHELL_COMMANDS[command]
        assert "admin" not in shell.SHELL_COMMANDS[command]
    assert "manage" in shell.SHELL_COMMANDS["space update-rules"]
    assert "manage" in shell.SHELL_COMMANDS["graph use-local"]
    assert "--confirm" in shell.SHELL_COMMANDS["bank delete"]
    assert "--confirm" in shell.SHELL_COMMANDS["token purge"]
    assert "--recover-access-grants" in shell.SHELL_COMMANDS["space delete"]
    assert "token grants" in shell.SHELL_COMMANDS["space delete"]

    delete_help = runner.invoke(cli, ["space", "delete", "--help"])
    assert delete_help.exit_code == 0, delete_help.output
    assert "--recover-access-grants" in delete_help.output
    assert "remove its token grants" in delete_help.output


def test_public_cli_snippets_use_executable_current_syntax():
    readmes = [
        (ROOT / "scripts" / "README.md").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "README.fr.md").read_text(encoding="utf-8"),
    ]
    for content in readmes:
        assert (
            'space create my-proj -d "Desc" --rules-file '
            "RULES/live-mem.standard.memory.bank.md"
        ) in content
        assert "backup list --space-id my-proj" in content
        assert "token purge [--all] --confirm" in content
        assert "http://localhost:8080" in content
        assert "http://localhost:8085" not in content

    for faq_name in ("FAQ.md", "FAQ.fr.md"):
        content = (ROOT / faq_name).read_text(encoding="utf-8")
        assert "hivemind> help" in content
        assert "live-mem>" not in content


def test_public_faq_pins_safe_space_delete_and_reuse_contract():
    english = (ROOT / "FAQ.md").read_text(encoding="utf-8")
    french = (ROOT / "FAQ.fr.md").read_text(encoding="utf-8")
    english_normalized = " ".join(english.split())
    french_normalized = " ".join(french.split())

    assert (
        "successful deletion removes the ID from every token allowlist"
        in english
    )
    assert "recover_access_grants=True" in english
    assert "intentional future pre-grant" in english
    assert (
        "deleting a space leaves historical token allowlists intact"
        not in english
    )
    assert "Even after a clean deletion" not in english
    assert (
        "Restoring a backup copies space objects only; it never restores "
        "token allowlists."
        in english_normalized
    )
    assert (
        "Never delete and recreate the restored space to repair access"
        in english_normalized
    )
    assert "active manager already scoped to that ID" in english_normalized
    assert "not caller-idempotent" in english_normalized
    assert "bootstrap has no persisted actor hash" in english_normalized
    assert "admin_update_token" in english_normalized

    assert "suppression réussie retire l'ID de toutes les allowlists" in french
    assert "recover_access_grants=True" in french
    assert "pré-grant futur intentionnel" in french
    assert (
        "supprimer un space conserve les allowlists historiques"
        not in french
    )
    assert "Même après une suppression propre" not in french
    assert (
        "Restaurer un backup recopie uniquement les objets du space"
        in french_normalized
    )
    assert (
        "Ne jamais supprimer puis recréer le space restauré pour réparer "
        "les accès"
        in french_normalized
    )
    assert "manager actif déjà scopé sur cet ID" in french_normalized
    assert "n'est pas idempotent pour le caller" in french_normalized
    assert "bootstrap n'a pas de hash d'acteur persisté" in french_normalized
    assert "admin_update_token" in french_normalized

    private_overlay_root = ROOT / "release" / "public-overlay"
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    mcp_spec = (ROOT / "docs" / "MCP_TOOLS_SPEC.md").read_text(
        encoding="utf-8"
    )
    security_normalized = " ".join(security.split())
    deployment_normalized = " ".join(deployment.split())
    mcp_spec_normalized = " ".join(mcp_spec.split())
    assert "Backup restoration is intentionally data-only" in security_normalized
    assert "target_token_hashes" in security
    assert "space_delete_grants_unconfirmed" in security
    assert "not caller-idempotent" in security_normalized
    assert "data-only recovery" in deployment_normalized
    assert (
        "Never delete/recreate the restored space" in deployment_normalized
    )
    assert (
        "Backup restoration copies space objects only" in mcp_spec_normalized
    )
    assert "Bootstrap has no persisted actor hash" in mcp_spec_normalized
    assert "admin_bulk_update_tokens" in mcp_spec_normalized
    assert "not caller-idempotent" in mcp_spec_normalized
    assert (
        "Do not delete and recreate a restored space" in mcp_spec_normalized
    )

    if private_overlay_root.exists():
        private_mcp_spec = (
            ROOT / "DESIGN" / "live-mem" / "MCP_TOOLS_SPEC.md"
        ).read_text(encoding="utf-8")
        private_mcp_spec_normalized = " ".join(private_mcp_spec.split())
        assert (
            "Backup restoration copies space objects only"
            in private_mcp_spec_normalized
        )
        assert (
            "Do not delete and recreate a restored space"
            in private_mcp_spec_normalized
        )
        assert "delete it first" not in private_mcp_spec
        assert "Bootstrap has no persisted actor hash" in private_mcp_spec_normalized
        assert "unsafe_recovery=True" in private_mcp_spec
        plan = (
            ROOT / "DESIGN" / "hivemind" / "PLAN-space-delete-grant-revocation.md"
        ).read_text(encoding="utf-8")
        assert "complete cleanup idempotently" not in plan
        assert "not caller-idempotent" in plan
        caller_idempotence_contracts = [
            ROOT / "DESIGN" / "live-mem" / "CONCURRENCY.md",
            ROOT / "DESIGN" / "live-mem" / "ARCHITECTURE.md",
            ROOT / "DESIGN" / "live-mem" / "S3_DATA_MODEL.md",
            ROOT
            / "docs"
            / "adr"
            / "0022-manage-delegation-and-space-provisioning.md",
        ]
        for contract in caller_idempotence_contracts:
            content = " ".join(
                contract.read_text(encoding="utf-8").split()
            )
            assert "not caller-idempotent" in content
            assert "admin/bootstrap repeat returns `not_found`" in content

    public_overlay = private_overlay_root / "CHANGELOG.md"
    if private_overlay_root.exists():
        assert public_overlay.exists()
        public_changelog_path = public_overlay
    else:
        public_changelog_path = ROOT / "CHANGELOG.md"
    public_changelog = public_changelog_path.read_text(encoding="utf-8")
    assert (
        "Deleting a space now revokes its token access grants."
        in public_changelog
    )
    assert "Backup restoration" in public_changelog
    assert "canonical hashes of affected tokens" in public_changelog


def test_shell_dispatcher_exposes_admin_console_bank_supervision_tools():
    assert "bank stale-spaces" in shell.SHELL_COMMANDS
    assert "bank consolidation-queues" in shell.SHELL_COMMANDS

    source = inspect.getsource(shell._handle_bank)
    assert 'sub == "stale-spaces"' in source
    assert 'sub == "consolidation-queues"' in source
    assert '"bank_stale_spaces"' in source
    assert '"bank_consolidation_queues"' in source
    assert '"bank_consolidate"' in source
    assert 'in ("ok", "running", "queued")' in source

    helper_source = inspect.getsource(commands._run_tool)
    assert '"running"' in helper_source
    assert '"queued"' in helper_source


def test_click_exposes_graph_use_local_and_forwards_canonical_tool(monkeypatch):
    captured = {}

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        captured.update(
            tool_name=tool_name,
            args=args,
            renderer=renderer,
            json_flag=json_flag,
        )

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    result = CliRunner().invoke(
        cli, ["graph", "use-local", "chat-engine", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "tool_name": "long_disconnect",
        "args": {"space_id": "chat-engine", "use_embedded": True},
        "renderer": commands.show_graph_local,
        "json_flag": True,
    }


async def test_shell_graph_use_local_forwards_canonical_tool():
    client = _ShellClient()

    await shell.dispatch(client, "graph use-local chat-engine", True)

    assert client.calls == [
        (
            "long_disconnect",
            {"space_id": "chat-engine", "use_embedded": True},
        )
    ]


async def test_shell_graph_query_forwards_bounded_long_query():
    client = _ShellClient()

    await shell.dispatch(client, 'graph query alpha "what changed?"', True)
    await shell.dispatch(client, "graph query alpha routing --limit 25", True)
    await shell.dispatch(client, "graph query alpha routing --limit 0", True)
    await shell.dispatch(client, "graph query alpha routing --limit nope", True)

    assert "graph query" in shell.SHELL_COMMANDS
    assert client.calls == [
        (
            "long_query",
            {"space_id": "alpha", "query": "what changed?", "limit": 10},
        ),
        (
            "long_query",
            {"space_id": "alpha", "query": "routing", "limit": 25},
        ),
    ]


async def test_shell_consolidate_defaults_to_own_and_serializes_explicit_global():
    client = _ShellClient()

    await shell.dispatch(client, "bank consolidate proj", True)
    await shell.dispatch(client, "bank consolidate proj --all-agents", True)

    assert client.calls == [
        ("bank_consolidate", {"space_id": "proj"}),
        ("bank_consolidate", {"space_id": "proj", "agent": ""}),
    ]


def test_gc_click_help_exposes_exact_set_precondition():
    result = CliRunner().invoke(cli, ["gc", "--help"])

    assert result.exit_code == 0
    assert "--expected-eligible-set-token" in result.output
    assert "required for delete" in result.output


def test_gc_click_forwards_the_prior_dry_run_token(monkeypatch):
    captured = {}

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        captured.update(tool_name=tool_name, args=args, json_flag=json_flag)

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    token = "gc-set-v1:" + "a" * 64
    result = CliRunner().invoke(
        cli,
        [
            "gc",
            "--space-id",
            "proj",
            "--max-age-days",
            "9",
            "--confirm",
            "--delete-only",
            "--expected-eligible-set-token",
            token,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "tool_name": "admin_gc_notes",
        "args": {
            "space_id": "proj",
            "max_age_days": 9,
            "confirm": True,
            "delete_only": True,
            "expected_eligible_set_token": token,
        },
        "json_flag": True,
    }


def test_gc_click_refuses_unguarded_delete_modes_before_network():
    runner = CliRunner()

    no_confirm = runner.invoke(cli, ["gc", "--delete-only"])
    no_token = runner.invoke(cli, ["gc", "--confirm", "--delete-only"])
    negative_age = runner.invoke(cli, ["gc", "--max-age-days", "-1"])

    assert no_confirm.exit_code == 2
    assert "--delete-only requires --confirm" in no_confirm.output
    assert no_token.exit_code == 2
    assert "--expected-eligible-set-token" in no_token.output
    assert negative_age.exit_code == 2
    assert "--max-age-days must be >= 0" in negative_age.output


class _ShellClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"status": "ok"}


def test_click_live_group_routes_through_discoverable_short_tools(monkeypatch):
    calls = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        calls.append((tool_name, args, json_flag))

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    runner = CliRunner()

    note = runner.invoke(cli, ["live", "note", "proj", "progress", "done"])
    read = runner.invoke(cli, ["live", "read", "proj", "--limit", "12"])
    search = runner.invoke(cli, ["live", "search", "proj", "needle", "--limit", "7"])

    assert note.exit_code == read.exit_code == search.exit_code == 0
    assert calls == [
        (
            "short_note",
            {
                "space_id": "proj",
                "category": "progress",
                "content": "done",
                "tags": "",
            },
            False,
        ),
        (
            "short_read",
            {
                "space_id": "proj",
                "limit": 12,
                "category": "",
                "agent": "",
                "since": "",
            },
            False,
        ),
        (
            "short_search",
            {"space_id": "proj", "query": "needle", "limit": 7},
            False,
        ),
    ]


async def test_shell_live_group_routes_through_discoverable_short_tools():
    client = _ShellClient()

    await shell.dispatch(client, "live note proj progress done", True)
    await shell.dispatch(client, "live read proj", True)
    await shell.dispatch(client, "live search proj needle", True)

    assert client.calls == [
        (
            "short_note",
            {"space_id": "proj", "category": "progress", "content": "done"},
        ),
        ("short_read", {"space_id": "proj", "limit": 20}),
        ("short_search", {"space_id": "proj", "query": "needle"}),
    ]


async def test_gc_shell_forwards_token_and_rejects_missing_preconditions():
    token = "gc-set-v1:" + "b" * 64
    client = _ShellClient()

    await shell.dispatch(
        client,
        "gc --space-id proj --max-age-days 3 --confirm --delete-only "
        f"--expected-eligible-set-token {token}",
        True,
    )
    await shell.dispatch(client, "gc --delete-only", True)
    await shell.dispatch(client, "gc --confirm --delete-only", True)
    await shell.dispatch(client, "gc --confirm --space-id", True)
    await shell.dispatch(client, "gc --confirm --space-id --delete-only", True)
    await shell.dispatch(client, "gc --max-age-days not-an-int", True)

    assert client.calls == [
        (
            "admin_gc_notes",
            {
                "space_id": "proj",
                "max_age_days": 3,
                "confirm": True,
                "delete_only": True,
                "expected_eligible_set_token": token,
            },
        )
    ]


def _invoke_click_token_create(monkeypatch, identity, argv, create_result=None):
    calls = []

    class FakeClient:
        def __init__(self, url, token):
            assert url
            assert token is not None

        async def call_tool(self, tool_name, args):
            calls.append((tool_name, args))
            if tool_name == "system_whoami":
                return identity
            assert tool_name in {"token_create", "admin_create_token"}
            return create_result or {
                "status": "created",
                "name": args["name"],
                "permissions": args["permissions"].split(","),
            }

    monkeypatch.setattr(commands, "MCPClient", FakeClient)
    result = CliRunner().invoke(cli, ["token", "create", *argv])
    return result, calls


def test_token_create_click_routes_from_live_identity_for_every_target_profile(
    monkeypatch,
):
    cases = [
        (
            {
                "status": "ok",
                "auth_type": "token",
                "permissions": ["read", "write", "manage", "admin"],
            },
            [
                "writer",
                "-p",
                "read,write",
                "--space-ids",
                "project-a",
                "--expires-in-days",
                "7",
                "--json",
            ],
            "admin_create_token",
            {
                "name": "writer",
                "permissions": "read,write",
                "expires_in_days": 7,
                "email": "",
                "space_ids": "project-a",
            },
        ),
        (
            {"status": "ok", "auth_type": "bootstrap", "permissions": []},
            ["root", "-p", "read,write,manage,admin", "--json"],
            "admin_create_token",
            {
                "name": "root",
                "permissions": "read,write,manage,admin",
                "expires_in_days": 0,
                "email": "",
                "space_ids": "",
            },
        ),
        (
            {
                "status": "ok",
                "auth_type": "token",
                "permissions": ["read", "write", "manage"],
            },
            ["writer", "-p", "read,write", "--email", "w@example.test", "--json"],
            "token_create",
            {
                "name": "writer",
                "permissions": "read,write",
                "expires_in_days": 0,
                "email": "w@example.test",
            },
        ),
    ]

    for identity, argv, expected_tool, expected_args in cases:
        result, calls = _invoke_click_token_create(monkeypatch, identity, argv)
        assert result.exit_code == 0, result.output
        assert calls == [
            ("system_whoami", {}),
            (expected_tool, expected_args),
        ]


def test_token_create_click_manager_never_reaches_admin_or_initial_scope(
    monkeypatch,
):
    identity = {
        "status": "ok",
        "auth_type": "token",
        "permissions": ["read", "write", "manage"],
    }
    admin_result, admin_calls = _invoke_click_token_create(
        monkeypatch,
        identity,
        ["root", "-p", "read,write,manage,admin"],
    )
    scoped_result, scoped_calls = _invoke_click_token_create(
        monkeypatch,
        identity,
        ["writer", "-p", "read,write", "--space-ids", "project-a"],
    )

    assert admin_result.exit_code == 2
    assert "admin token requires an admin/bootstrap caller" in admin_result.output
    assert scoped_result.exit_code == 2
    assert "space invite <space_id> <full_hash>" in scoped_result.output
    assert admin_calls == [("system_whoami", {})]
    assert scoped_calls == [("system_whoami", {})]
    assert not any(name.startswith("admin_") for name, _ in admin_calls + scoped_calls)


def test_token_create_click_rejects_scope_on_admin_target(monkeypatch):
    result, calls = _invoke_click_token_create(
        monkeypatch,
        {
            "status": "ok",
            "auth_type": "token",
            "permissions": ["read", "write", "manage", "admin"],
        },
        [
            "root",
            "-p",
            "read,write,manage,admin",
            "--space-ids",
            "project-a",
        ],
    )
    assert result.exit_code == 2
    assert "token-store v2 persists space_ids=[]" in result.output
    assert calls == [("system_whoami", {})]


def test_token_created_renderer_surfaces_scope_normalization_and_warning():
    payload = {
        "status": "created",
        "name": "root",
        "token": "lm_secret",
        "token_hash": "sha256:" + "a" * 64,
        "permissions": ["read", "write", "manage", "admin"],
        "space_ids": [],
        "info": "admin scope normalized to empty",
        "warning_no_access": "synthetic warning",
    }
    with display.console.capture() as capture:
        display.show_token_created(payload)
    rendered = capture.get()
    assert "admin scope normalized to empty" in rendered
    assert "synthetic warning" in rendered


def test_token_update_renderer_and_both_cli_paths_surface_server_guidance():
    payload = {
        "status": "ok",
        "message": "Token updated",
        "info": "admin scope normalized to empty",
        "warning_no_access": "token has no readable space",
    }
    with display.console.capture() as capture:
        display.show_token_updated(payload)
    rendered = capture.get()
    assert "Token updated" in rendered
    assert "admin scope normalized to empty" in rendered
    assert "token has no readable space" in rendered

    assert "show_token_updated" in inspect.getsource(
        commands.token_update_cmd.callback
    )
    assert "show_token_updated(result)" in inspect.getsource(shell._handle_token)


def test_token_create_click_non_manager_stops_after_identity_probe(monkeypatch):
    result, calls = _invoke_click_token_create(
        monkeypatch,
        {"status": "ok", "auth_type": "token", "permissions": ["read", "write"]},
        ["writer", "-p", "read,write"],
    )

    assert result.exit_code == 2
    assert "requires a manage or admin/bootstrap caller" in result.output
    assert calls == [("system_whoami", {})]


def test_space_invite_click_requires_and_forwards_full_hash(monkeypatch):
    calls = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        calls.append((tool_name, args, json_flag))

    monkeypatch.setattr(commands, "_run_tool", fake_run_tool)
    full_hash = "sha256:" + "a" * 64
    runner = CliRunner()
    ok = runner.invoke(cli, ["space", "invite", "project-a", full_hash, "--json"])
    bad = runner.invoke(cli, ["space", "invite", "project-a", "sha256:abcd"])
    bare = runner.invoke(cli, ["space", "invite", "project-a", "a" * 64])
    uppercase = runner.invoke(cli, ["space", "invite", "project-a", "sha256:" + "A" * 64])

    assert ok.exit_code == 0, ok.output
    assert calls == [
        (
            "space_invite_token",
            {"space_id": "project-a", "token_hash": full_hash},
            True,
        )
    ]
    assert bad.exit_code == 2
    assert "must be canonical" in bad.output
    assert bare.exit_code == 2
    assert uppercase.exit_code == 2
    assert len(calls) == 1


class _RoleRoutingShellClient:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "system_whoami":
            return self.identity
        if name in {"token_create", "admin_create_token"}:
            return {"status": "created", "permissions": args["permissions"].split(",")}
        return {"status": "ok", "space_id": args.get("space_id"), "added": True}


async def test_shell_matches_click_role_routing_and_invite_grammar():
    manager = _RoleRoutingShellClient(
        {
            "status": "ok",
            "auth_type": "token",
            "permissions": ["read", "write", "manage"],
        }
    )
    admin = _RoleRoutingShellClient(
        {
            "status": "ok",
            "auth_type": "token",
            "permissions": ["read", "write", "manage", "admin"],
        }
    )
    bootstrap = _RoleRoutingShellClient(
        {"status": "ok", "auth_type": "bootstrap", "permissions": []}
    )
    writer = _RoleRoutingShellClient(
        {"status": "ok", "auth_type": "token", "permissions": ["read", "write"]}
    )
    full_hash = "sha256:" + "b" * 64

    await shell.dispatch(manager, "token create writer -p read,write", True)
    await shell.dispatch(
        manager,
        "token create root -p read,write,manage,admin --space-ids project-a",
        True,
    )
    await shell.dispatch(
        manager,
        "token create scoped -p read,write --space-ids project-a",
        True,
    )
    await shell.dispatch(manager, f"space invite project-a {full_hash}", True)
    await shell.dispatch(manager, "space invite project-a sha256:short", True)
    await shell.dispatch(manager, f"space invite project-a {'b' * 64}", True)
    await shell.dispatch(manager, f"space invite project-a sha256:{'B' * 64}", True)
    await shell.dispatch(
        admin,
        "token create writer -p read,write --space-ids project-a",
        True,
    )
    await shell.dispatch(
        admin,
        "token create root -p read,write,manage,admin --space-ids project-a",
        True,
    )
    await shell.dispatch(
        bootstrap,
        "token create root -p read,write,manage,admin",
        True,
    )
    await shell.dispatch(writer, "token create child -p read", True)

    assert manager.calls == [
        ("system_whoami", {}),
        ("token_create", {"name": "writer", "permissions": "read,write"}),
        ("system_whoami", {}),
        ("system_whoami", {}),
        (
            "space_invite_token",
            {"space_id": "project-a", "token_hash": full_hash},
        ),
    ]
    assert not any(name.startswith("admin_") for name, _ in manager.calls)
    assert admin.calls == [
        ("system_whoami", {}),
        (
            "admin_create_token",
            {
                "name": "writer",
                "permissions": "read,write",
                "space_ids": "project-a",
            },
        ),
        ("system_whoami", {}),
    ]
    assert bootstrap.calls == [
        ("system_whoami", {}),
        (
            "admin_create_token",
            {
                "name": "root",
                "permissions": "read,write,manage,admin",
                "space_ids": "",
            },
        ),
    ]
    assert writer.calls == [("system_whoami", {})]


def test_click_manager_surfaces_partial_recovery_credential_without_admin_call(
    monkeypatch,
):
    payload = {
        "status": "partial",
        "recovery_required": True,
        "token": "lm_uncertain",
        "token_hash": "sha256:" + "c" * 64,
    }
    rendered = []
    monkeypatch.setattr(commands, "show_token_created", rendered.append)
    result, calls = _invoke_click_token_create(
        monkeypatch,
        {
            "status": "ok",
            "auth_type": "token",
            "permissions": ["read", "write", "manage"],
        },
        ["uncertain", "-p", "read,write"],
        create_result=payload,
    )

    assert result.exit_code == 0, result.output
    assert rendered == [payload]
    assert [name for name, _ in calls] == ["system_whoami", "token_create"]


def test_click_space_create_surfaces_actionable_partial_recovery(monkeypatch):
    payload = {
        "status": "partial",
        "space_id": "project-a",
        "recovery_required": True,
        "message": "commit marker not confirmed",
        "recovery": {"retry_safe": True, "action": "retry identical request"},
    }
    recovery_rendered = []
    success_rendered = []

    class FakeClient:
        def __init__(self, url, token):
            pass

        async def call_tool(self, tool_name, args):
            assert tool_name == "space_create"
            return payload

    monkeypatch.setattr(commands, "MCPClient", FakeClient)
    monkeypatch.setattr(commands, "show_space_created", success_rendered.append)
    monkeypatch.setattr(
        commands, "show_space_create_recovery", recovery_rendered.append
    )
    result = CliRunner().invoke(
        cli,
        ["space", "create", "project-a", "--rules", "# Rules"],
    )

    assert result.exit_code == 0, result.output
    assert recovery_rendered == [payload]
    assert success_rendered == []


def test_space_recovery_renderer_preserves_typed_values_and_is_not_success(
    monkeypatch,
):
    stream = io.StringIO()
    monkeypatch.setattr(
        display,
        "console",
        display.Console(file=stream, color_system=None, width=180),
    )
    action = "Inspect [exact] prefix; do not retry & do not delete."

    display.show_space_create_recovery(
        {
            "status": "partial",
            "space_id": "project-a",
            "recovery_required": True,
            "message": "commit marker not confirmed",
            "recovery": {"retry_safe": False, "action": action},
        }
    )

    output = stream.getvalue()
    assert "Recovery Required (not successful)" in output
    assert "recovery.retry_safe: false" in output
    assert f"recovery.action: {action}" in output
    assert "commit marker not confirmed" in output
    assert "Space Created" not in output


async def test_shell_surfaces_partial_recovery_credential(monkeypatch):
    payload = {
        "status": "partial",
        "recovery_required": True,
        "token": "lm_uncertain",
        "token_hash": "sha256:" + "d" * 64,
        "permissions": ["read", "write"],
    }
    rendered = []

    class PartialClient:
        async def call_tool(self, name, args):
            if name == "system_whoami":
                return {
                    "status": "ok",
                    "auth_type": "token",
                    "permissions": ["read", "write", "manage"],
                }
            assert name == "token_create"
            return payload

    monkeypatch.setattr(shell, "show_token_created", rendered.append)
    await shell.dispatch(PartialClient(), "token create uncertain -p read,write", False)

    assert rendered == [payload]


async def test_shell_space_partial_uses_only_recovery_renderer(monkeypatch):
    payload = {
        "status": "partial",
        "space_id": "project-a",
        "recovery_required": True,
        "message": "commit marker not confirmed",
        "recovery": {"retry_safe": True, "action": "retry identical request"},
    }
    recovery_rendered = []
    success_rendered = []

    class PartialClient:
        async def call_tool(self, name, args):
            assert name == "space_create"
            return payload

    monkeypatch.setattr(shell, "show_space_create_recovery", recovery_rendered.append)
    monkeypatch.setattr(shell, "show_space_created", success_rendered.append)
    await shell.dispatch(
        PartialClient(), "space create project-a description rules", False
    )

    assert recovery_rendered == [payload]
    assert success_rendered == []


def test_click_space_delete_partial_uses_only_recovery_renderer(monkeypatch):
    payload = {
        "status": "partial",
        "space_id": "project-a",
        "recovery_required": True,
        "message": "payload deletion unconfirmed",
        "files_total": 9,
        "files_deleted": 4,
        "failed_keys": ["project-a/live/a.md"],
        "marker_preserved": True,
        "recovery": {"retry_safe": True, "action": "retry identical request"},
    }
    calls = []
    recovery_rendered = []
    success_rendered = []

    class PartialClient:
        def __init__(self, url, token):
            pass

        async def call_tool(self, name, args):
            calls.append((name, args))
            return payload

    monkeypatch.setattr(commands, "MCPClient", PartialClient)
    monkeypatch.setattr(
        commands, "show_space_delete_recovery", recovery_rendered.append
    )
    monkeypatch.setattr(commands, "show_success", success_rendered.append)
    result = CliRunner().invoke(
        cli,
        [
            "space",
            "delete",
            "project-a",
            "--confirm",
            "--recover-access-grants",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "space_delete",
            {
                "space_id": "project-a",
                "confirm": True,
                "recover_access_grants": True,
            },
        )
    ]
    assert recovery_rendered == [payload]
    assert success_rendered == []


def test_click_space_delete_grants_only_recovery_is_not_reported_as_deletion(
    monkeypatch,
):
    payload = {
        "status": "grants_cleaned",
        "space_id": "project-a",
        "files_total": 0,
        "files_deleted": 0,
        "access_grants_removed": 3,
        "recovered": True,
    }
    calls = []
    successes = []

    class RecoveryClient:
        def __init__(self, url, token):
            pass

        async def call_tool(self, name, args):
            calls.append((name, args))
            return payload

    monkeypatch.setattr(commands, "MCPClient", RecoveryClient)
    monkeypatch.setattr(commands, "show_success", successes.append)
    result = CliRunner().invoke(
        cli,
        [
            "space",
            "delete",
            "project-a",
            "--confirm",
            "--recover-access-grants",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "space_delete",
            {
                "space_id": "project-a",
                "confirm": True,
                "recover_access_grants": True,
            },
        )
    ]
    assert successes == ["Access grants for 'project-a' cleaned (3 grants)"]
    assert all("deleted" not in message.lower() for message in successes)


def test_click_space_delete_reports_deleted_files_and_grants(monkeypatch):
    payload = {
        "status": "deleted",
        "space_id": "project-a",
        "files_total": 4,
        "files_deleted": 4,
        "access_grants_removed": 3,
    }
    successes = []

    class DeletedClient:
        def __init__(self, url, token):
            pass

        async def call_tool(self, name, args):
            assert name == "space_delete"
            assert args == {"space_id": "project-a", "confirm": True}
            return payload

    monkeypatch.setattr(commands, "MCPClient", DeletedClient)
    monkeypatch.setattr(commands, "show_success", successes.append)
    result = CliRunner().invoke(
        cli,
        ["space", "delete", "project-a", "--confirm"],
    )

    assert result.exit_code == 0, result.output
    assert successes == ["Space 'project-a' deleted (4 files, 3 grants)"]


async def test_shell_space_delete_partial_never_retries_or_reports_success(
    monkeypatch,
):
    payload = {
        "status": "partial",
        "space_id": "project-a",
        "recovery_required": True,
        "message": "marker deletion unconfirmed",
        "files_total": 5,
        "files_deleted": 4,
        "failed_keys": ["project-a/_meta.json"],
        "marker_preserved": None,
        "recovery": {"retry_safe": True, "action": "retry the same space_id"},
    }
    calls = []
    recovery_rendered = []
    success_rendered = []

    class PartialClient:
        async def call_tool(self, name, args):
            calls.append((name, args))
            return payload

    monkeypatch.setattr(shell, "show_space_delete_recovery", recovery_rendered.append)
    monkeypatch.setattr(shell, "show_success", success_rendered.append)
    await shell.dispatch(PartialClient(), "space delete project-a --confirm", False)

    assert calls == [
        ("space_delete", {"space_id": "project-a", "confirm": True})
    ]
    assert recovery_rendered == [payload]
    assert success_rendered == []


async def test_shell_space_delete_confirmation_preserves_recovery_flag_and_warns(
    monkeypatch,
):
    warnings = []

    class NeverCalledClient:
        async def call_tool(self, name, args):
            raise AssertionError((name, args))

    monkeypatch.setattr(shell, "show_warning", warnings.append)
    await shell.dispatch(
        NeverCalledClient(),
        "space delete project-a --recover-access-grants",
        False,
    )

    assert any(
        "space delete project-a --confirm --recover-access-grants" in message
        for message in warnings
    )
    assert any("_system/tokens.json" in message for message in warnings)


async def test_shell_space_delete_renders_grants_only_and_deleted_successes(
    monkeypatch,
):
    responses = [
        {
            "status": "grants_cleaned",
            "space_id": "project-a",
            "files_total": 0,
            "files_deleted": 0,
            "access_grants_removed": 3,
            "recovered": True,
        },
        {
            "status": "deleted",
            "space_id": "project-b",
            "files_total": 4,
            "files_deleted": 4,
            "access_grants_removed": 2,
        },
    ]
    calls = []
    successes = []

    class SuccessClient:
        async def call_tool(self, name, args):
            calls.append((name, args))
            return responses.pop(0)

    monkeypatch.setattr(shell, "show_success", successes.append)
    client = SuccessClient()
    await shell.dispatch(
        client,
        "space delete project-a --confirm --recover-access-grants",
        False,
    )
    await shell.dispatch(client, "space delete project-b --confirm", False)

    assert calls == [
        (
            "space_delete",
            {
                "space_id": "project-a",
                "confirm": True,
                "recover_access_grants": True,
            },
        ),
        (
            "space_delete",
            {"space_id": "project-b", "confirm": True},
        ),
    ]
    assert successes == [
        "Access grants cleaned (3 grants)",
        "Deleted (4 files, 2 grants)",
    ]


def test_space_delete_recovery_renderer_preserves_counts_keys_and_action(
    monkeypatch,
):
    stream = io.StringIO()
    monkeypatch.setattr(
        display,
        "console",
        display.Console(file=stream, color_system=None, width=240),
    )
    action = "Inspect [exact] prefix; do not retry & do not delete."
    failed_keys = ["project-a/live/a.md", "project-a/<unsafe>&.md"]

    display.show_space_delete_recovery(
        {
            "status": "partial",
            "space_id": "project-a",
            "recovery_required": True,
            "message": "payload deletion unconfirmed",
            "files_total": 7,
            "files_deleted": 3,
            "failed_keys": failed_keys,
            "marker_preserved": False,
            "access_grants_pending": 2,
            "recovery": {"retry_safe": False, "action": action},
        }
    )

    output = stream.getvalue()
    assert "Recovery Required (not successful)" in output
    assert "files_total: 7" in output
    assert "files_deleted: 3" in output
    assert "marker_preserved: false" in output
    assert "access_grants_pending: 2" in output
    assert "recovery.retry_safe: false" in output
    assert f"recovery.action: {action}" in output
    assert all(key in output for key in failed_keys)
    assert "Space deleted" not in output
