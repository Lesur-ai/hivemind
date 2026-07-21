# Configure agents for unified Hivemind memory

This guide defines the public, vendor-neutral setup for agents that use
Hivemind as their shared memory source. It applies to Codex (`AGENTS.md`),
Claude Code (`CLAUDE.md`), and other MCP clients.

For a migration from separate Live Memory and Graph Memory services, complete
the [space-by-space migration playbook](MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
first. Client-specific connection examples remain available in the
[Codex](../CODEX_INTEGRATION.md) and
[Claude Code](../CLAUDE_CODE_INTEGRATION.md) guides.

## Unified memory contract

Each agent connects to one Hivemind MCP endpoint and uses one `space_id` for
all three memory tiers:

| Tier | Purpose | Canonical tools |
| --- | --- | --- |
| `short` | Recent append-only notes and inter-agent handoff | `short_note`, `short_read`, `short_search` |
| `mid` | Consolidated Markdown project memory and session bootstrap | `mid_read`, `mid_read_all`, `mid_list`, `mid_consolidate` |
| `long` | Ontology and knowledge-graph lookup | `long_query`, `long_status`; ingestion is explicit and non-routine |

Repository documents remain the final authority for detailed product,
technical, operational, and protocol facts. The `mid` tier is the compact
session bootstrap; `short` contains facts not yet consolidated; `long` locates
related canonical documents and relationships but never supplies commit,
rollback, audit, membership, tombstone, watermark, or recovery truth.

Hivemind is the canonical **shared** memory contract. A client may also keep
local conversation history, instruction files, or product-specific memory
(for example Claude Code auto memory). Treat that local context as
non-authoritative and never use it to bypass the Hivemind startup gate. You do
not need to claim that all client memory resets between sessions.

The historical `live_*`, `bank_*`, and `graph_*` names remain callable as
compatibility aliases. New instructions should use the canonical
`short_*`/`mid_*`/`long_*` names so tool discovery and documentation agree.

## Create a new token for every agent

Create a **new Hivemind token for each agent identity**. Do not reuse a legacy
Live Memory token, a legacy Graph Memory token, an administrator token, or one
shared token for several agents. Hivemind derives the author of a short note
from the token, so token sharing destroys provenance and makes revocation and
least-privilege access unreliable.

A trusted manager or administrator creates the routine token and grants it to
each required space:

```bash
uv run python scripts/mcp_cli.py token create codex-project-a -p read,write
uv run python scripts/mcp_cli.py space invite project-a \
  sha256:<exact-64-lowercase-hex-returned-by-token-create>
```

Repeat `space invite` for every space the same agent may access. Repeat `token
create` for every distinct agent identity. A routine agent normally needs only
`read,write`; reserve `read,write,manage` for a trusted provisioner because
`manage` can create spaces and delegate further managers. Never put the
clear-text token in `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, source control, or
logs. Store it only in the MCP client configuration, a secret manager, or a
protected environment variable.

## Configure one MCP endpoint

Remove the separate Live Memory and Graph Memory MCP entries from the agent's
active configuration after that agent's space passes migration validation.
Configure one Hivemind entry instead. Transport names and secret interpolation
are client-specific; do not copy one client's JSON into another:

| Client | Streamable HTTP configuration | Environment-backed bearer |
| --- | --- | --- |
| Codex | `[mcp_servers.hivemind]` with `url = "…/mcp"` | `bearer_token_env_var = "HIVEMIND_TOKEN"` |
| Claude Code | `.mcp.json` entry with `"type": "http"` | `"Authorization": "Bearer ${HIVEMIND_TOKEN}"` |

Set `HIVEMIND_TOKEN` in the environment that launches the client, preferably
through an OS keychain, secret manager, or non-recorded prompt. Missing secret
expansion must fail visibly. Never replace an environment reference with a
clear-text credential in a committed file. See the client guides for current
file locations, UI paths, and exact examples.

The endpoint name is client-local. Use one stable name such as `hivemind` and
refer to it consistently in the project's agent instructions. The URL and
environment-variable reference belong in client configuration; the token value
belongs in the protected environment or secret store. None belongs in project
instruction files.

## Recommended project instruction block

Put the following contract in the instruction file loaded by the agent. Replace
`{HIVEMIND_MCP_SERVER}` and `{SPACE}` with non-secret identifiers. If several
agent-specific files exist, keep this block identical or have the secondary
file explicitly defer to the primary one.

```markdown
# Project memory — Hivemind MCP

Hivemind is the canonical shared memory across agents and sessions. The primary
shared source is the `{HIVEMIND_MCP_SERVER}` Hivemind MCP server, space
`{SPACE}`. One space owns short, mid, and long memory. Client-local history or
product memory is non-authoritative and never bypasses this startup gate.
Legacy Live Memory or Graph Memory endpoints do not substitute for Hivemind.

Repository files are the final authority. Mid memory is the compact session
bootstrap, short notes are recent unconsolidated facts, and long memory is a
derived ontology/knowledge-graph locator. If long results conflict with a
repository file, trust the repository.

## Start of every task

Before changing files, tests, external state, project direction, or review
output, call all three on `{HIVEMIND_MCP_SERVER}`:

1. `space_rules(space_id="{SPACE}")`
2. `mid_read_all(space_id="{SPACE}")`
3. `short_read(space_id="{SPACE}")`

Read the returned content and identify the current focus. If any startup call
fails, times out, returns non-OK, or is unavailable, stop before mutation. Do
not substitute local memory or a legacy memory endpoint.

## During work

Write concise atomic notes only for durable facts:

`short_note(space_id="{SPACE}", category="<category>", content="...")`

Allowed categories are `observation`, `decision`, `progress`, `issue`, `todo`,
`insight`, and `question`. Agent identity is derived from the dedicated token;
never pass or invent an agent identity.

Use `long_query` for historical context, prior incidents, runbooks, decisions,
and cross-document relationships, then re-read the referenced canonical file
before acting. Long memory is non-authoritative. Do not run `long_push`, alter
long bindings, or ingest documents as routine session-end work. Never ingest
`activeContext.md`, `progress.md`, or raw mid-memory summaries into long
memory.

## End of a meaningful work block

1. Write one short summary note.
2. Ask for or confirm user validation before consolidation unless current
   project instructions explicitly require immediate consolidation.
3. Call `mid_consolidate(space_id="{SPACE}")` at most once.
4. Return without polling. `bank_consolidation_status` is only for an explicit
   manual status check.

`mid_consolidate` returns an asynchronous acknowledgement with
`next_action="return_to_user_without_polling"`. Do not immediately read mid
memory expecting the job's result, and do not submit a second job because the
bank has not changed yet. Verify later only after explicit job completion
confirmation.

Never edit mid-memory files directly during normal agent operation. Never put
tokens, endpoints, or secrets in project instructions, notes, long memory,
commits, or logs.
```

### File-specific placement

- **Codex:** put the block in the repository's `AGENTS.md`.
- **Claude Code:** put it in `CLAUDE.md`, or make `CLAUDE.md` say that
  `AGENTS.md` is the canonical contract and then add only Claude-specific
  instructions.
- **Other agents:** use the instruction mechanism that is loaded before tool
  calls, and keep the same startup/failure contract.

## Rewrite existing agent workflows

Review every instruction source that an agent may load, including nested
`AGENTS.md` or `CLAUDE.md` files, `.clinerules/`, global user instructions,
review prompts, CI-agent prompts, and organization-provided workflow snippets.
For each file:

1. Replace separate `LIVE_MCP_SERVER` and `GRAPH_MCP_SERVER` variables with one
   `HIVEMIND_MCP_SERVER`.
2. Replace separate Live `space_id` and Graph `memory_id` values with the one
   migrated Hivemind `space_id`.
3. Replace startup calls `bank_read_all` and `live_read` with `mid_read_all`
   and `short_read`; keep `space_rules`.
4. Replace routine writes `live_note` and `bank_consolidate` with `short_note`
   and `mid_consolidate`.
5. Route long lookup through the same Hivemind endpoint with `long_query` and
   keep the repository-confirmation rule.
6. Remove instructions that call `graph_connect`, synchronize Graph Memory at
   every session end, push the whole bank, or treat graph output as recovery
   truth.
7. Remove embedded tokens, URLs, legacy Graph credentials, and legacy memory
   IDs. Put secrets in the MCP client or secret store.
8. Preserve project-specific engineering, testing, Git, safety, and review
   rules that are unrelated to memory. The migration changes their memory
   dependency, not their purpose.

Do not confuse agent instruction files with a space's consolidation rules.
`space_rules` defines how Hivemind consolidates short notes into mid-memory
files; `AGENTS.md`, `CLAUDE.md`, and similar files define how an agent works.
Migrate and verify both, but do not overwrite customized consolidation rules
with the generic agent block.

## Verify each agent

Using the agent's new token and only the Hivemind endpoint:

1. `system_whoami()` reports the expected unique agent name and permissions.
2. `space_rules`, `mid_read_all`, and `short_read` succeed for every assigned
   space and fail for a space outside the token allowlist.
3. A test `short_note` is attributed to the expected agent identity.
4. `long_status` addresses the same `space_id`; after long migration,
   `long_query` returns expected source references.
5. The agent's instruction loader actually reads the rewritten file.
6. No active client configuration or instruction file still depends on the
   separate Live Memory or Graph Memory endpoint.
7. A deliberately invalid bearer is rejected on `/mcp` with HTTP 401. Do not
   use the public `/health` endpoint as an authentication test.

Only after these checks pass should the operator revoke that agent's legacy
tokens. Keep legacy services read-only until every mapped space and every agent
has passed its own validation.
