# 🖥️ Hivemind CLI, Shell & Tests

> Scriptable CLI, interactive shell, and operational test scripts for Hivemind
> `1.3.0`.

🇫🇷 [Version française](README.fr.md)

---

## Prerequisites

```bash
uv sync --dev
```

Environment variables:

```bash
export MCP_URL=http://localhost:8080    # Server URL (via WAF)
export MCP_TOKEN=your_secret_token      # Authentication token
```

---

## Relationship with `/admin` and `/live`

| Surface          | Exposes                                         | Notes                                                                                       |
| ---------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `mcp_cli.py`     | **45 direct MCP operations**                    | Click commands + interactive shell. This README is the reference list.                       |
| Web `/admin`     | Curated workflows via `POST /api/tool` proxy   | Authenticated web console (HttpOnly cookie). Sections: Dashboard, Spaces, Space Detail, Consolidation, Audit, Access, and Operator tools (Backups, Maintenance). |
| Web `/live`      | Read-only viewer of spaces / live notes / bank  | Uses dedicated REST endpoints (`/api/spaces`, `/api/live/<id>`, `/api/bank/<id>`), NOT MCP. |

The MCP server's frozen fixture is the canonical tool surface. The CLI exposes
45 direct operations, including space invitation and read-only long query, but does not mirror
every additive MCP tool or tier alias. **Parity is directional, not
bijective**: the `/admin` console curates a subset of the surface for operator
workflows (it does not expose `backup_download`, `space_export`, or
`admin_bulk_update_tokens`, which remain CLI/MCP-only), and the CLI in turn
does not mirror every additive MCP tool. `/live` is a read-only convenience UI
whose capabilities are a subset of `live read`, `bank read`, `bank list`, and
`space info`.

---

## Scriptable CLI (Click)

Every operation listed below maps to a Click command. Full help:
`uv run python scripts/mcp_cli.py --help` or `... <group> --help`.

### System (3 tools)

```bash
uv run python scripts/mcp_cli.py health                              # Service health (S3 + LLM probes)
uv run python scripts/mcp_cli.py whoami                              # Current token identity
uv run python scripts/mcp_cli.py about                               # Service version, capabilities
```

### Space (10 MCP tools)

```bash
uv run python scripts/mcp_cli.py space list                          # List accessible spaces
uv run python scripts/mcp_cli.py space create my-proj -d "Desc" --rules-file RULES/live-mem.standard.memory.bank.md  # manage
uv run python scripts/mcp_cli.py space invite my-proj sha256:<64-lowercase-hex>  # manage + access; add-only
uv run python scripts/mcp_cli.py space info my-proj                  # Details (counts, owner, dates, queue summary)
uv run python scripts/mcp_cli.py space rules my-proj                 # Memory Bank rules of this space
uv run python scripts/mcp_cli.py space summary my-proj               # Full synthesis (rules + bank + notes counts)
uv run python scripts/mcp_cli.py space update my-proj -d "New desc"  # Update description / owner
uv run python scripts/mcp_cli.py space update-rules my-proj -f rules.md  # Replace rules (manage)
uv run python scripts/mcp_cli.py space export my-proj                # Export as tar.gz
uv run python scripts/mcp_cli.py space delete my-proj --confirm      # Irreversible (manage)
```

Before `space delete`, quiesce every writer and background job for that space.
The CLI renders `status: partial` with exact deletion counts, failed keys,
marker state, and recovery action; it never labels or retries that result as
success. Advanced Hivemind `unsafe_recovery` remains an MCP-client procedure,
not a CLI flag.

### Live notes (3 tools)

```bash
uv run python scripts/mcp_cli.py live note my-proj observation "Found X"   # Append a note (agent = token)
uv run python scripts/mcp_cli.py live read my-proj                          # List recent unconsolidated notes
uv run python scripts/mcp_cli.py live search my-proj "keyword"              # Full-text search in notes
```

### Bank (11 tools)

```bash
uv run python scripts/mcp_cli.py bank list my-proj                          # List bank files
uv run python scripts/mcp_cli.py bank read my-proj activeContext.md         # Read one bank file
uv run python scripts/mcp_cli.py bank read-all my-proj                      # Read entire bank (agent startup)
uv run python scripts/mcp_cli.py bank consolidate my-proj                   # 🧠 Enqueue own-note LLM consolidation (fire-and-forget)
uv run python scripts/mcp_cli.py bank consolidate my-proj --all-agents      # Explicit global scope (manage/admin)
uv run python scripts/mcp_cli.py bank consolidation-status <job_id>         # Manual status check (do NOT poll automatically)
uv run python scripts/mcp_cli.py bank consolidation-queues                  # Lane summary across all accessible spaces
uv run python scripts/mcp_cli.py bank stale-spaces                          # 🚨 Spaces ≥5 notes / oldest ≥5 days
uv run python scripts/mcp_cli.py bank stale-spaces --min-notes 10 --min-age-days 7 --consolidate  # Trigger caller-scoped bulk consolidation
uv run python scripts/mcp_cli.py bank stale-spaces --consolidate --all-agents  # Explicit global bulk scope (manage/admin)
uv run python scripts/mcp_cli.py bank compact my-proj                       # Dry-run scan of oversized files
uv run python scripts/mcp_cli.py bank compact my-proj --apply               # Apply LLM compaction (manage)
uv run python scripts/mcp_cli.py bank repair my-proj                        # Dry-run scan (Unicode / parasitic prefixes)
uv run python scripts/mcp_cli.py bank repair my-proj --apply                # Apply fixes (manage)
uv run python scripts/mcp_cli.py bank write my-proj activeContext.md -f ./ctx.md   # Bypass LLM (manage)
uv run python scripts/mcp_cli.py bank delete my-proj progress.md --confirm  # Delete file + Unicode duplicates (manage)
```

### Long tier (5 tools + local-binding maintenance)

Routine flow against the default stack: the first `graph push` auto-binds the
space to the embedded long runtime — no `graph connect` step.

```bash
uv run python scripts/mcp_cli.py graph push my-proj                         # Push bank → graph (first push auto-binds; delete + re-ingest)
uv run python scripts/mcp_cli.py graph status my-proj                       # Connection + graph stats
uv run python scripts/mcp_cli.py graph query my-proj "deployment" --limit 10  # Read-only semantic query
# Advanced override / diagnostic only (external engine, non-default ontology):
uv run python scripts/mcp_cli.py graph connect my-proj <url> <token> <memory_id> [-o ontology]
uv run python scripts/mcp_cli.py graph disconnect my-proj
# Validate/provision embedded Graph Memory, then replace the legacy override.
# Remote graph data remains untouched; no document is ingested.
uv run python scripts/mcp_cli.py graph use-local my-proj
```

### Backup (5 tools)

```bash
uv run python scripts/mcp_cli.py backup create my-proj -d "before migration"
uv run python scripts/mcp_cli.py backup create --all                        # Backup ALL accessible spaces (admin)
uv run python scripts/mcp_cli.py backup list --space-id my-proj             # List backups filtered by space
uv run python scripts/mcp_cli.py backup download <backup_id>                # Download archive
uv run python scripts/mcp_cli.py backup restore <backup_id> --confirm       # Restore (space must not exist)
uv run python scripts/mcp_cli.py backup delete <backup_id> --confirm        # Permanent
```

### Token delegation and admin lifecycle

```bash
uv run python scripts/mcp_cli.py token create agent-cline -p read,write --email cline@team.io  # manage; starts with no spaces
uv run python scripts/mcp_cli.py token list                                 # List tokens (filterable)
uv run python scripts/mcp_cli.py token update <hash> --add-spaces my-proj   # Delta update (add/remove spaces, perms, email)
uv run python scripts/mcp_cli.py token bulk-update --name-contains agent --add-spaces my-proj --confirm   # Mass update
uv run python scripts/mcp_cli.py token revoke <hash>                        # Soft-revoke (keeps audit trail)
uv run python scripts/mcp_cli.py token delete <hash>                        # Hard-delete (admin)
uv run python scripts/mcp_cli.py token purge [--all] --confirm              # Purge revoked tokens (or --all)
uv run python scripts/mcp_cli.py gc --space-id my-proj --confirm            # Consolidate the currently eligible orphan notes
uv run python scripts/mcp_cli.py gc --space-id my-proj                      # Delete step 1: dry-run; copy eligible_set_token
uv run python scripts/mcp_cli.py gc --space-id my-proj --confirm --delete-only --expected-eligible-set-token '<token>'  # Delete step 2: destructive delete
```

`token create` routes from the authenticated caller, not the requested target
profile. A persisted non-admin manager uses manager-safe `token_create`, may
create only `read`, `read,write`, or `read,write,manage`, starts the child with
no spaces, and invites the exact full hash separately. An admin/bootstrap
caller uses `admin_create_token` for every target profile and may initially
scope non-admin targets. `--space-ids` is rejected for an admin target: v2
stores its scope as `[]`; promotion clears scopes and downgrade starts empty
unless explicitly re-scoped. List/update/revoke/delete/purge/bulk remain
admin-only.

`manage` is transitive: any manager can create arbitrary new spaces and further
managers. Its allowlist bounds existing-space invitations/mutations, not
`space create`. Routine agent tokens should stay `read,write`.

GC writes proceed only when every candidate space resolves to `DIRECT_LOCAL`;
shared, unsafe, resync-required, or corrupt state fails closed. Delete mode is
never a one-shot shortcut: the CLI requires the opaque token returned by the
prior dry run, and the server rejects any changed eligible-key set. Inspect the
JSON result for `status: "partial"` and the actual processed/deleted/failed
counts before retrying with a new dry run.

`admin_audit_recent` deliberately has **no CLI command**. It is an
admin-gated MCP tool used by the `/admin` Audit view; call it through an MCP
client when raw access is needed. The Admin CLI therefore remains the 8
commands above.

---

## Interactive Shell

```bash
uv run python scripts/mcp_cli.py shell
```

Features:

- **Tab completion** on all commands and subcommands
- **Persistent history** (`~/.hivemind_shell_history`)
- **Contextual help**: `help`, `help <verb>` (e.g. `help bank`)
- **Rich display** with colors (tables, panels, Markdown)
- **`--json` flag** on any command for raw JSON output

---

## 🧪 Test Scripts

### Embedded credential Docker proof — `verify_embedded_secret_docker.sh`

Linux/Docker proof for the embedded-credential lifecycle. It builds an isolated image and Compose
project, proves root-owned volume repair with the shipped capability profiles,
recreates the Hivemind container and compares only SHA-256 fingerprints, then
checks fail-closed inputs. It refuses to overwrite an existing `.env` and
removes only its invocation-scoped containers, volumes, and image.

```bash
bash scripts/verify_embedded_secret_docker.sh
```

### Consolidation claim-validation regression

The LLM can still produce unsupported content. The focused suite proves the
defensive prompt and optional unattributed-claim heuristic without claiming
that either mechanism guarantees correctness:

```bash
uv run pytest tests/test_issue17_validation.py
```

---

### Global Test Suite — `test_recette.py`

Unified script with **4 selectable suites**:

```bash
uv run python scripts/test_recette.py --list                       # List available suites
uv run python scripts/test_recette.py --url http://localhost:8080  # ALL suites
uv run python scripts/test_recette.py --suite recette              # Pipeline agent (7 tests)
uv run python scripts/test_recette.py --suite isolation            # Cross-space allowlist (18 tests)
uv run python scripts/test_recette.py --suite qualite              # MCP tools (19 tests)
uv run python scripts/test_recette.py --suite recette,isolation    # Multiple suites
uv run python scripts/test_recette.py --suite isolation -v --step  # Step-by-step
uv run python scripts/test_recette.py --no-cleanup                 # Keep test data
```

#### Available Suites

| Suite       | Tests | Description                                                                                                |
| ----------- | ----- | ---------------------------------------------------------------------------------------------------------- |
| `recette`   | 7     | Full pipeline: token → space → notes → LLM consolidation → bank → cleanup                                  |
| `isolation` | 18    | Mono-tenant space allowlist: cross-space denial, backup filtering, read-only enforcement, token grants |
| `qualite`   | 19    | MCP tools: system, admin, space, live, bank, backup, GC                                                    |
| `graph`     | ~8    | Explicit `graph_connect` override path: connect, push, status, disconnect (skipped without `--graph-url`/`--graph-token`) |

```bash
# Graph suite — exercises the explicit graph_connect override path against an
# operator-supplied long engine; skipped without --graph-url / --graph-token
# (the nominal embedded auto-bind path needs no flags: graph push binds by itself)
uv run python scripts/test_recette.py --suite graph \
  --graph-url http://host.docker.internal:8080 \
  --graph-token TOKEN
```

> ⚠️ When Hivemind runs in Docker, use `host.docker.internal` instead of `localhost` in `--graph-url` for an engine running on the host.

### Bank Compaction Unit Test — `test_bank_compact.py`

Direct unit test for the compaction engine. Run via `uv run python scripts/test_bank_compact.py`.

---

## Common Options

| Option          | Description                                                                |
| --------------- | -------------------------------------------------------------------------- |
| `--url`         | Hivemind server URL (default: `$MCP_URL` or `http://localhost:8080`)       |
| `--token`       | Admin bootstrap key (default: `$ADMIN_BOOTSTRAP_KEY` or `.env`)            |
| `--json` / `-j` | Raw JSON output on any command (bypasses Rich formatting)                  |
| `--suite`       | Suites to run, comma-separated (default: all)                              |
| `--graph-url`   | Graph Memory URL (for `--suite graph`)                                     |
| `--graph-token` | Graph Memory token (for `--suite graph`)                                   |
| `--step`        | Step-by-step mode (pause between steps)                                    |
| `--no-cleanup`  | Keep test data after completion                                            |
| `-v`            | Verbose output                                                             |
| `--list`        | List available suites and exit                                             |

---

## Architecture

```
scripts/
├── mcp_cli.py                # CLI entry point (Click) + Interactive shell
├── test_recette.py           # 🧪 Global test suite (4 suites, ~44 tests)
├── test_bank_compact.py      # 🧪 Bank compaction unit tests
├── configure_dev_env.py      # Secure local .env generator (refuses overwrite)
├── README.md                 # Documentation (English) ← You are here
├── README.fr.md              # Documentation (French)
└── cli/
    ├── __init__.py           # Config (BASE_URL, TOKEN)
    ├── client.py             # MCPClient Streamable HTTP (MCP SDK)
    ├── commands.py           # Click commands (1 per MCP tool)
    ├── display.py            # Rich display (tables, panels)
    └── shell.py              # Interactive shell (prompt_toolkit)
```

---

*Hivemind CLI — 1.3.0*
