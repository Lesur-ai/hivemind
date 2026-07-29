"""P9 guards for the public MCP contract and Hivemind identity."""

import ast
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from mcp.server.fastmcp import FastMCP

from live_mem import server as live_mem_server
from live_mem.tools import graph as graph_tools
from live_mem.tools import system as system_tools


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cli import commands as cli_commands  # noqa: E402
from cli.commands import cli  # noqa: E402


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _system_handler(name: str):
    mcp = FastMCP(name="p9-system-contract")
    assert system_tools.register(mcp) == 3
    return mcp._tool_manager._tools[name].fn


@pytest.mark.asyncio
async def test_system_about_exposes_hivemind_public_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(mcp_server_name="Hivemind")
    monkeypatch.setattr("live_mem.config.get_settings", lambda: settings)

    result = await _system_handler("system_about")()

    assert result["name"] == "Hivemind"
    assert result["description"] == "Shared memory layer for collaborative AI agents"
    assert result["author"] == "Lesur AI"
    assert result["documentation"] == "https://github.com/Lesur-ai/hivemind"
    assert result["tools_count"] == len(result["tools"]) == 3


@pytest.mark.asyncio
async def test_system_health_uses_real_status_enum_and_hivemind_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Storage:
        async def test_connection(self) -> dict:
            return {"status": "ok", "bucket": "hivemind", "latency_ms": 1.0}

        async def list_prefixes(self, _prefix: str) -> list[str]:
            return ["alpha/", "_system/"]

    settings = SimpleNamespace(
        mcp_server_name="Hivemind",
        llmaas_api_url="",
        llmaas_api_key="",
        llmaas_model="unused",
    )
    monkeypatch.setattr("live_mem.config.get_settings", lambda: settings)
    monkeypatch.setattr("live_mem.core.storage.get_storage", lambda: Storage())

    result = await _system_handler("system_health")()

    assert result["status"] == "degraded"
    assert result["service_name"] == "Hivemind"
    assert result["services"]["s3"]["status"] == "ok"
    assert result["services"]["llmaas"]["status"] == "warning"
    assert result["spaces_count"] == 1


def test_public_docs_distinguish_authenticated_transport_from_baseline_tools() -> None:
    spec = _read("docs/MCP_TOOLS_SPEC.md")

    assert "There is **no anonymous MCP discovery or tool-call tier**" in spec
    assert "| 🔓     | MCP baseline" in spec
    assert '"status": "healthy"' in spec
    assert '"service_name": "Hivemind"' in spec
    assert '"author": "Lesur AI"' in spec
    assert '"documentation": "https://github.com/Lesur-ai/hivemind"' in spec
    assert '"service_name": "Live Memory"' not in spec
    assert "| 🔓     | Public     | No auth required" not in spec


def test_mcp_spec_matches_optional_default_space_rules_and_identifier_charset() -> None:
    spec = _read("docs/MCP_TOOLS_SPEC.md")
    assert 'rules: str = ""' in spec
    assert "empty loads DEFAULT_RULES_FILE" in spec
    assert "letters, numbers, underscore, hyphen" in spec


def test_long_query_discloses_its_embedding_provider_dependency() -> None:
    spec = _read("docs/MCP_TOOLS_SPEC.md")
    section = spec.split("### `long_query`", 1)[1].split("### `long_ingest`", 1)[0]
    assert "performs no\ngenerative/chat completion" in section
    assert "configured embedding endpoint" in section
    assert "(no-LLM)" not in section


def test_long_query_registered_schema_discloses_embedding_without_chat() -> None:
    mcp = FastMCP(name="p9-long-query-contract")
    assert graph_tools.register(mcp) == 6
    tool = mcp._tool_manager._tools["long_query"]
    schema_text = str(tool.description) + str(tool.parameters)

    assert "configured embedding endpoint" in schema_text
    assert "no generative chat completion" in schema_text
    assert "no language model is invoked" not in schema_text


def test_public_onboarding_requires_complete_provider_model_configuration() -> None:
    required = (
        "LLMAAS_API_URL",
        "LLMAAS_API_KEY",
        "LLMAAS_MODEL",
        "LLMAAS_EMBEDDING_MODEL",
        "LLMAAS_EMBEDDING_DIMENSIONS",
        "/chat/completions",
        "/embeddings",
    )
    for relative in (".env.example", "README.md", "README.fr.md", "docs/DEPLOYMENT.md"):
        content = _read(relative)
        for item in required:
            assert item in content, f"{relative}: missing {item}"


def test_public_docs_lock_resolved_shared_restore_contract() -> None:
    positioning = _read("docs/POSITIONING.md")
    spec = _read("docs/MCP_TOOLS_SPEC.md")

    for text in (positioning, spec):
        assert "unsafe_recovery=True" in text
        assert "resync_required" in text
        assert "open gap" not in text.lower()

    assert "CommitRuntime.stage_commit()" in spec
    assert "live_bank_version + 1" in spec
    assert "unions live and backup tombstones" in spec
    assert "non-leader Mesh worker" in spec


def test_readme_tool_tables_keep_manage_and_confirmation_contracts() -> None:
    for relative in ("README.md", "README.fr.md"):
        readme = _read(relative)
        for tool in ("bank_compact", "bank_repair", "bank_write", "bank_delete"):
            row = next(
                line
                for line in readme.splitlines()
                if line.startswith("|") and f"`{tool}`" in line
            )
            assert "manage" in row
            assert "admin" not in row
        for tool in ("bank_delete", "backup_restore", "backup_delete"):
            row = next(
                line
                for line in readme.splitlines()
                if line.startswith("|") and f"`{tool}`" in line
            )
            assert "confirm" in row
        purge_row = next(
            line
            for line in readme.splitlines()
            if line.startswith("|") and "`admin_purge_tokens`" in line
        )
        assert "confirm" in purge_row
        graph_push_row = next(
            line
            for line in readme.splitlines()
            if line.startswith("|") and "`graph_push`" in line
        )
        graph_status_row = next(
            line
            for line in readme.splitlines()
            if line.startswith("|") and "`graph_status`" in line
        )
        assert "include_volatile" in graph_push_row
        assert "include_graph" in graph_status_row

    mapping = _read("docs/TOOL_MAPPING.md")
    assert "48 direct registry entries plus these\n   13 aliases" in mapping
    assert "43 historical" not in mapping
    assert "56 registered" not in mapping


def test_public_docker_quickstarts_pin_the_compose_feature_floor() -> None:
    for relative in (
        "README.md",
        "README.fr.md",
        "docs/DEPLOYMENT.md",
        "CLAUDE_CODE_INTEGRATION.md",
        "CLAUDE_CODE_INTEGRATION.fr.md",
    ):
        content = _read(relative)
        assert "Docker Compose" in content
        assert "2.17.0" in content


def test_deployment_bootstrap_curl_uses_the_exported_credential() -> None:
    deployment = _read("docs/DEPLOYMENT.md")
    assert 'Authorization: Bearer $MCP_TOKEN' in deployment
    assert 'Authorization: Bearer $ADMIN_BOOTSTRAP_KEY' not in deployment


@pytest.mark.parametrize("key", ["", "admin", "not-long-enough", "x" * 31])
def test_bootstrap_gate_rejects_every_key_shorter_than_32(key: str) -> None:
    with pytest.raises(RuntimeError, match="at least 32"):
        live_mem_server._reject_weak_bootstrap_key(key)


def test_bootstrap_gate_accepts_a_32_character_value() -> None:
    live_mem_server._reject_weak_bootstrap_key("x" * 32)


def test_main_applies_the_same_bootstrap_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_mem_server.settings, "admin_bootstrap_key", "short")
    with pytest.raises(SystemExit) as exc_info:
        live_mem_server.main()
    assert exc_info.value.code == 1


def test_user_facing_server_and_cli_identity_is_hivemind() -> None:
    server = _read("src/live_mem/server.py")
    commands = _read("scripts/cli/commands.py")
    system = _read("src/live_mem/tools/system.py")

    assert "Hivemind MCP Server" in server
    assert "🧠 Hivemind — MCP server CLI." in commands
    assert "https://github.com/Lesur-ai/hivemind" in system
    assert "Live Memory MCP Server" not in server
    assert "🧠 Live Memory — MCP server CLI." not in commands
    assert "Cloud Temple" not in system
    assert "https://github.com/Cloud-Temple/live-memory" not in system


def test_embedded_long_about_is_hivemind_derived_and_auth_gated() -> None:
    server = _read("services/graph-memory/src/mcp_memory/server.py")

    assert "is_authenticated = auth is not None" in server
    assert "is_authenticated = auth is not None or True" not in server
    assert '"provider": "Lesur AI"' in server
    assert '"repo": "https://github.com/Lesur-ai/hivemind"' in server
    assert '"upstream": "https://github.com/cloud-temple/graph-memory"' in server
    assert "derived projection only" in server
    assert "isolation multi-tenant par namespace" not in server
    assert "Knowledge Graph est la source principale" not in server

    notices = _read("THIRD_PARTY_NOTICES.md")
    for changed_path in (
        "src/mcp_memory/core/__init__.py",
        "src/mcp_memory/core/storage.py",
        "src/mcp_memory/server.py",
    ):
        assert changed_path in notices


@pytest.mark.asyncio
async def test_embedded_long_about_hides_configuration_until_authenticated() -> None:
    """Execute the shipped handler in isolation without optional graph clients."""

    server_path = ROOT / "services/graph-memory/src/mcp_memory/server.py"
    module = ast.parse(server_path.read_text(encoding="utf-8"))
    handler = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "system_about"
    )
    handler.decorator_list = []
    isolated = ast.Module(body=[handler], type_ignores=[])
    ast.fix_missing_locations(isolated)

    auth_context: ContextVar[dict | None] = ContextVar(
        "embedded_about_test_auth", default=None
    )

    class Service:
        async def list_memories(self):
            return []

        async def test_connection(self):
            return {"status": "ok"}

    service = Service()
    namespace = {
        "__file__": os.fspath(server_path),
        "__name__": "embedded_about_contract_test",
        "__package__": "mcp_memory",
        "os": os,
        "current_auth": auth_context,
        "settings": SimpleNamespace(
            mcp_server_name="hivemind-graph-memory",
            llmaas_model="chat-model",
            llmaas_embedding_model="embedding-model",
            llmaas_embedding_dimensions=1024,
            rag_score_threshold=0.5,
            chunk_size=800,
            backup_retention_count=10,
        ),
        "get_graph": lambda: service,
        "get_storage": lambda: service,
        "get_vector_store": lambda: service,
        "get_extractor": lambda: service,
        "get_embedder": lambda: service,
    }
    exec(compile(isolated, os.fspath(server_path), "exec"), namespace)
    about = namespace["system_about"]

    anonymous = await about()
    assert anonymous["status"] == "ok"
    assert anonymous["memories"] == []
    assert anonymous["services"] == {}
    assert anonymous["configuration"] == {}

    token = auth_context.set({"type": "token", "permissions": ["read"]})
    try:
        authenticated = await about()
    finally:
        auth_context.reset(token)
    assert authenticated["status"] == "ok"
    assert authenticated["services"] == {
        "neo4j": "ok",
        "s3": "ok",
        "qdrant": "ok",
        "llmaas": "ok",
        "embedding": "ok",
    }
    assert authenticated["configuration"] == {
        "llm_model": "chat-model",
        "embedding_model": "embedding-model",
        "embedding_dimensions": 1024,
        "rag_score_threshold": 0.5,
        "chunk_size": 800,
        "backup_retention": 10,
    }


def test_public_contract_summary_covers_all_operator_cited_decisions() -> None:
    contracts = _read("docs/ARCHITECTURE_CONTRACTS.md")
    for identifier in ("ADR-0015", "ADR-0016", "ADR-0017"):
        assert f"| {identifier} |" in contracts


def test_current_changelog_has_no_superseded_release_state() -> None:
    current = _read("CHANGELOG.md").split(
        "## Inherited Live Memory history", 1
    )[0]

    assert "Opt-in Project Mesh" not in current
    assert "tracked as a known follow-up" not in current
    assert "ship as honest \"not available in this build\" placeholders" not in current
    assert "starting Hivemind version is decided at tag time" not in current


def test_faq_qualifies_inherited_product_versions() -> None:
    for relative in ("FAQ.md", "FAQ.fr.md"):
        faq = _read(relative)
        assert "VERSION" in faq
        assert "Live Memory" in faq
        assert "version: 2" in faq


def test_faq_avoids_unbounded_capacity_and_unsourced_latency_claims() -> None:
    for relative in ("FAQ.md", "FAQ.fr.md"):
        faq = _read(relative)
        assert "No theoretical limit" not in faq
        assert "Pas de limite théorique" not in faq
        assert "append-only, zero conflicts" not in faq
        assert "append-only, zéro conflit" not in faq
        assert "100,000 characters" in faq or "100 000 caractères" in faq
        assert "~50ms" not in faq
        assert "~15-30s" not in faq


def test_public_mesh_docs_expose_the_two_node_pairing_limit() -> None:
    for relative in (
        "README.md",
        "README.fr.md",
        "docs/PROJECT_MESH.md",
        "docs/DEPLOYMENT.md",
    ):
        content = _read(relative).lower()
        assert "two-node mesh" in content or "mesh à deux nœuds" in content
        assert "third node" in content or "troisième nœud" in content

    project_mesh = _read("docs/PROJECT_MESH.md")
    assert "synchronizing multiple sovereign instances" not in project_mesh

    for relative in ("README.md", "README.fr.md"):
        first_stage = _read(relative).split("- **", 2)[1]
        assert "permanent" not in first_stage.lower()


def test_graph_query_cli_forwards_read_only_long_query(monkeypatch) -> None:
    calls = []

    def fake_run_tool(ctx, tool_name, args, renderer, json_flag=False):
        calls.append((tool_name, args, renderer, json_flag))

    monkeypatch.setattr(cli_commands, "_run_tool", fake_run_tool)
    runner = CliRunner()

    default = runner.invoke(cli, ["graph", "query", "alpha", "what changed?"])
    limited = runner.invoke(
        cli,
        ["graph", "query", "alpha", "routing", "--limit", "25", "--json"],
    )
    invalid = runner.invoke(
        cli, ["graph", "query", "alpha", "routing", "--limit", "0"]
    )

    assert default.exit_code == 0, default.output
    assert limited.exit_code == 0, limited.output
    assert invalid.exit_code == 2
    assert "0 is not in the range 1<=x<=500" in invalid.output
    assert calls == [
        (
            "long_query",
            {"space_id": "alpha", "query": "what changed?", "limit": 10},
            cli_commands.show_json,
            False,
        ),
        (
            "long_query",
            {"space_id": "alpha", "query": "routing", "limit": 25},
            cli_commands.show_json,
            True,
        ),
    ]


def test_graph_query_cli_help_exposes_bounded_limit_and_json() -> None:
    help_result = CliRunner().invoke(cli, ["graph", "query", "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "SPACE_ID QUERY" in help_result.output
    assert "-n, --limit INTEGER RANGE" in help_result.output
    assert "1<=x<=500" in help_result.output
    assert "-j, --json" in help_result.output
    assert "read-only" in help_result.output
