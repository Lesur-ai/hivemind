# 🔌 Hivemind Integration Guide for Claude Code

> **Documentation revision**: 2026-07-19

This guide connects **Claude Code** to Hivemind's unified short, mid, and long
memory through one MCP endpoint. The reusable cross-client instruction contract
is in [Configure agents for unified Hivemind memory](docs/AGENT_MEMORY_SETUP.md).

---

## 📋 Table of Contents

- [Prerequisites](#-prerequisites)
- [Step 1 — Start Hivemind](#-step-1--start-hivemind)
- [Step 2 — Create a token for Claude Code](#-step-2--create-a-token-for-claude-code)
- [Step 3 — Connect Claude Code to Hivemind](#-step-3--connect-claude-code-to-hivemind)
- [Step 4 — Create a memory space](#-step-4--create-a-memory-space)
- [Step 5 — Give Claude Code its instructions](#-step-5--give-claude-code-its-instructions)
- [Recommended Workflow](#-recommended-workflow)
- [Multi-agent: Claude Code + Cline + other supported clients](#-multi-agent-claude-code--cline--other-supported-clients)
- [Troubleshooting](#-troubleshooting)
- [With Claude Desktop](#-with-claude-desktop)
- [Summary](#-summary)

---

## 📦 Prerequisites

| Component            | Version            | Check                               |
| -------------------- | ------------------ | ----------------------------------- |
| **Docker**           | ≥ 24.0             | `docker --version`                  |
| **Docker Compose**   | ≥ 2.17.0           | `docker compose version`            |
| **Claude Code**      | ≥ 2.1              | `claude --version`                  |
| **Hivemind**      | Deployed & running | `curl http://localhost:8080/health` |

> 💡 If Claude Code is not installed: `npm install -g @anthropic-ai/claude-code` (macOS/Linux/Windows) or use the dedicated installer — see Anthropic's official documentation. Claude Code provides the `claude` command in the terminal and ships IDE extensions (VS Code, JetBrains) that share the same configuration.

---

## 🚀 Step 1 — Start Hivemind

If Hivemind is not yet running:

```bash
cd /path/to/hivemind
python scripts/configure_dev_env.py
uv sync --locked --dev
# Before mid/long, configure the provider URL/key, chat model, embedding model,
# and exact embedding dimensions described in docs/DEPLOYMENT.md.
docker compose --profile dev up --build -d --wait
```

The helper creates a mode-`0600` local file with random credentials, local
MinIO, and Mesh disabled. It refuses to overwrite an existing `.env`. For a
networked or production deployment, configure the production template and
follow [the deployment guide](docs/DEPLOYMENT.md) instead.

**Check**:

```bash
# With local S3 and no LLM: HTTP 200 and "degraded". With the LLM: "healthy".
curl -fsS http://localhost:8080/health \
  | jq -e '.status == "healthy" or .status == "degraded"'
```

---

## 🔑 Step 2 — Create a token for Claude Code

Claude Code needs a **new Bearer Token dedicated to this agent identity**, with
`read,write` permissions. Never reuse a legacy Live Memory or Graph Memory
token, an administrator token, or one token shared by several agents.

### First clean install — bootstrap the operator token

On the stack created in Step 1, no stored token exists yet. Use the generated
bootstrap credential once to mint the first administrator token, then replace
it in the shell before creating the Claude Code token:

```bash
cd /path/to/hivemind
export MCP_URL=http://localhost:8080
export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"
uv run python scripts/mcp_cli.py token create local-ops-admin \
  -p read,write,manage,admin --json

# Copy the one-time lm_... token from the JSON response, then replace this value.
export MCP_TOKEN='lm_REPLACE_WITH_RETURNED_ADMIN_TOKEN'
uv run python scripts/mcp_cli.py whoami --json
```

The first command routes to `admin_create_token` because the caller is the
bootstrap identity. Save the returned plaintext and full canonical hash; the
plaintext is never shown again. Production operators must retrieve their
bootstrap secret from their secret manager rather than parsing a local file.

### Create the dedicated agent token via the CLI

```bash
cd /path/to/hivemind
export MCP_TOKEN=<trusted_manage_or_admin_token>

# Create a "read,write" token for Claude Code
uv run python scripts/mcp_cli.py token create claude-code-agent -p read,write
```

The CLI will print something like:

```
Token created successfully!
  Name   : claude-code-agent
  Token  : lm_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2
  Perms  : read, write

⚠️  This token will NEVER be displayed again. Copy it now!
```

> **⚠️ IMPORTANT**: Copy this token immediately! It will never be shown again (only the SHA-256 hash is stored).
> Keep the exact returned `token_hash` too. The new token has no space access
> until a manager invites that full canonical hash in Step 4.

### Why the bootstrap credential is separate

The bootstrap key can create the first admin through `admin_create_token`; it
cannot call the manager-safe `token_create`. Do not configure Claude Code with
the bootstrap credential for routine work. The clean-install sequence above
mints an admin first; the following CLI call then uses that admin to create the
dedicated `read,write` token.

---

## ⚙️ Step 3 — Connect Claude Code to Hivemind

Configure one Hivemind MCP entry only. The same endpoint provides short, mid,
and long memory; do not add a separate Graph Memory MCP server. The scopes,
HTTP type, environment expansion, and timeout units below follow the current
[official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

Claude Code stores its MCP configuration in a JSON file. Three scopes are available:

| Scope     | Location                                       | Reach                              |
| --------- | ---------------------------------------------- | ---------------------------------- |
| `local`   | `~/.claude.json` (key `projects.<cwd>`)        | Current directory only             |
| `user`    | `~/.claude.json` (top-level `mcpServers` key)  | All projects of the current user   |
| `project` | `.mcp.json` at the project root                | Committed to the repo (teams)      |

The recommended setup is a project-scoped `.mcp.json` that contains only an
environment-variable reference. Claude Code asks for approval before loading a
project-scoped server. Review the file before approving it. Use `local` or
`user` scope instead when the endpoint itself must remain private.

### 3.1 — Environment-backed project configuration (recommended)

Create `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "hivemind": {
      "type": "http",
      "url": "https://your-server/mcp",
      "headers": {
        "Authorization": "Bearer ${HIVEMIND_TOKEN}"
      }
    }
  }
}
```

Claude Code expands `${HIVEMIND_TOKEN}` in HTTP headers. Set it in the
environment that launches Claude Code without recording it in shell history:

```bash
printf 'Hivemind token: '
IFS= read -r -s HIVEMIND_TOKEN
printf '\n'
export HIVEMIND_TOKEN
claude
```

Use `http://localhost:8080/mcp` as the URL for a local instance. Never add a
default value for `HIVEMIND_TOKEN`: a missing secret must fail visibly.

### 3.2 — Private local or user scope

`local` and `user` entries live in `~/.claude.json`. The `claude mcp add`
command accepts `--header`, but persists that header in this private file. Use
this only if your secret-storage policy permits a clear-text credential in a
user-only file; the project method above keeps the credential itself out of
configuration and source control.

### 3.3 — Verify the connection

After configuration:

```bash
claude mcp list
```

You should see `hivemind` with a connected status. Then launch Claude Code in
the project and ask:

> *"Call `system_whoami` on hivemind and show me the agent name, permissions,
> and assigned spaces."*

Verify the dedicated agent name, `read,write` permissions, and expected space.
Then call `space_rules`, `mid_read_all`, and `short_read` for that space. The
public `/health` endpoint checks reachability only and does not validate the
token. An invalid token must leave the server failed in `/mcp` and return HTTP
401 on `/mcp`.

### 3.4 — Whitelist the tools (avoid permission prompts)

Claude Code asks for confirmation on every unauthorized MCP tool call. To avoid these interruptions, add the Hivemind tools to the project (or user) allow-list.

Create or edit `.claude/settings.local.json` at the project root:

```json
{
  "permissions": {
    "allow": [
      "mcp__hivemind__space_list",
      "mcp__hivemind__space_info",
      "mcp__hivemind__space_rules",
      "mcp__hivemind__mid_read_all",
      "mcp__hivemind__mid_read",
      "mcp__hivemind__short_read",
      "mcp__hivemind__short_note",
      "mcp__hivemind__short_search",
      "mcp__hivemind__mid_consolidate",
      "mcp__hivemind__long_query",
      "mcp__hivemind__long_status",
      "mcp__hivemind__bank_consolidation_status",
      "mcp__hivemind__system_health",
      "mcp__hivemind__system_whoami"
    ]
  }
}
```

> 💡 **Naming convention**: Claude Code exposes each MCP tool as `mcp__<server-name>__<tool-name>`. If you named your server `hivemind-prod` in Step 3.1, adjust the prefix accordingly.

Interactive alternative: type `/permissions` in a Claude Code session to open the permissions editor.

For a global configuration (all projects), use `~/.claude/settings.json` instead.

### 3.5 — Remote HTTPS server

For a production deployment, the URL and JSON block are identical — only the scheme changes (`https://` instead of `http://`). No additional option is required on the Claude Code side.

---

## 📁 Step 4 — Create a memory space

Before Claude Code can write notes, a trusted provisioner must create a
**memory space** with **rules** and invite Claude Code's writer token. Routine
`read,write` sessions do not discover `space_create`; a separate `manage` or
`admin` provisioning session discovers and may invoke the complete
`space_create` → `token_create` → `space_invite_token` flow. You can also use
the CLI or authenticated Admin Console.

### Via the CLI

```bash
uv run python scripts/mcp_cli.py space create my-project \
  --rules-file ./RULES/live-mem.standard.memory.bank.md \
  -d "My development project"

uv run python scripts/mcp_cli.py space invite my-project \
  sha256:<exact-64-lowercase-hex-from-step-2>
```

Several rule templates are provided in the `RULES/` directory of the repo:

| Template                                  | Use case                                              |
| ----------------------------------------- | ----------------------------------------------------- |
| `RULES/live-mem.standard.memory.bank.md`  | Standard six-file project Memory Bank                 |
| `RULES/product.management.memory.bank.md` | Product team (vision, portfolio, personas, features)  |
| `RULES/medical.memory.bank.md`            | Non-clinical health-note organization; human verification required |
| `RULES/presales.memory.bank.md`           | Pre-sales, prospect qualification, RFP                |
| `RULES/book.memory.bank.md`               | Book writing / editorial project                      |

Alternatively, use the authenticated Admin Console's space workflow, then
invite Claude Code's exact token hash. Keep routine Claude Code sessions on the
invited `read,write` credential.

### Example of standard rules

```markdown
# Memory Bank Rules

## Files to maintain

### projectbrief.md
Vision, goals, project scope.

### activeContext.md
Current focus, ongoing work, recent decisions, next steps.

### progress.md
What works, what's left to do, known issues.

### techContext.md
Technologies used, configuration, technical constraints.

### systemPatterns.md
Architecture, patterns, technical decisions, components.

### productContext.md
Why this project exists, problems solved, user experience.
```

---

## 📝 Step 5 — Give Claude Code its instructions

Hivemind already includes the `long` ontology/knowledge-graph tier behind the
same endpoint and `space_id`. For the complete source hierarchy, fail-closed
startup gate, long lookup policy, and workflow-rewrite checklist, use the
canonical [agent memory setup](docs/AGENT_MEMORY_SETUP.md).

Claude Code automatically reads `CLAUDE.md` files on startup. Two possible locations:

| Location                  | Reach                                              | Recommended for                       |
| ------------------------- | -------------------------------------------------- | ------------------------------------- |
| `<project-root>/CLAUDE.md` | The current project (committed with the repo)     | Project-specific workflow             |
| `~/.claude/CLAUDE.md`     | All projects of the current user (private)         | Global preferences, identity, style   |

For Hivemind, the project-level `CLAUDE.md` is the ideal spot because `{SPACE}` is project-specific.

### Recommended template (paste into `CLAUDE.md`)

This template uses the `{SPACE}` placeholder — you only need to configure **one value**:

```markdown
# Memory Bank — Hivemind MCP

Hivemind is my canonical shared memory across agents and sessions. Claude Code
also has local `CLAUDE.md` and auto-memory context; I treat those as local,
non-authoritative context and never use them to bypass the Hivemind startup
gate. Repository files remain the final authority. See Claude Code's
[official memory documentation](https://code.claude.com/docs/en/memory).

## 🔌 Configuration (to customize per project)

My persistent memory is managed by the **Hivemind** MCP server (`hivemind`).

> **⚙️ The only value to customize:**
>
> - **SPACE** = `my-project`       ← Replace with your space_id
>
> All instructions below use `{SPACE}` — I substitute it automatically with the value above.
> The agent name is **auto-detected** from the authentication token (no need to configure it).

## 📖 At the start of EVERY task (MANDATORY)

1. Call `space_rules("{SPACE}")` to read the rules (bank structure)
2. Call `mid_read_all("{SPACE}")` to load ALL consolidated context
3. Call `short_read(space_id="{SPACE}")` to read **unconsolidated notes**
4. Read the content carefully before starting
5. Identify the current focus in `activeContext.md`

> ⚠️ NEVER start working without having read the bank.
> If any startup call fails, times out, returns non-OK, or is unavailable, stop
> before mutation. Do not substitute local memory or a legacy memory endpoint.
>
> 💡 **Why read live notes?** Between sessions, notes may have been written (by me or other agents) without being consolidated yet. These notes contain recent context not yet reflected in the bank files. Ignoring them = risking redoing work already done or missing recent decisions.

## 📝 During work

Write frequent, atomic notes via `short_note`:

    short_note(space_id="{SPACE}", category="<category>", content="...")

The `agent` parameter is **auto-detected** from the token — no need to pass it.

**Categories**:
- `observation` — Factual findings, command results
- `decision` — Technical choices and their rationale
- `progress` — Advancement, completed work
- `issue` — Problems encountered, bugs
- `todo` — Identified tasks to do
- `insight` — Learnings, discovered patterns
- `question` — Points to clarify, pending decisions

Use `long_query` for historical or cross-document context, then re-read the
referenced repository file before acting. Long memory is derived and
non-authoritative. Do not run `long_push`, alter long bindings, or ingest
documents as routine session-end work. Never ingest `activeContext.md`,
`progress.md`, or raw mid-memory summaries into long memory.

## 🧠 At session end (or after a significant work block)

Only if meaningful new notes exist, confirm the work summary with the user
unless the project's active instructions explicitly require immediate
consolidation, then call:

    mid_consolidate(space_id="{SPACE}")

The LLM will consolidate **my own notes** (agent auto-detected from token) by updating the bank files according to the space rules.

> ℹ️ Omitted/null `agent` always means your own notes. Only a manage/admin
> caller can explicitly consolidate all agents' notes with `agent=""`.
>
> 🔕 `mid_consolidate` is **fire-and-forget**: it returns an async job ack (`running` / `queued`) with `next_action="return_to_user_without_polling"`. **Call it once and return to the user.** Do not watch or poll. `bank_consolidation_status(job_id)` exists for **explicit manual checks only**.

## ⚠️ Strict rules

1. **NEVER write directly into the bank** — only the LLM consolidation does that
2. **Always pass `space_id="{SPACE}"`** in every call
3. **Write atomic notes after each significant step** — 1 note = 1 fact, 1 decision, or 1 task
4. **Consolidate only meaningful work** — after user validation unless active instructions explicitly require it, call `mid_consolidate` at most once and return without polling or re-reading the bank
5. **Read the bank at startup** — never work without context
6. **Use one Hivemind endpoint** — never substitute legacy memory services
7. **Keep secrets out of instructions** — tokens and URLs belong in MCP client configuration

## 🔄 When to request an update

If the user says **"update memory bank"**:
1. Write `short_note` notes summarizing the current state of work
2. Call `mid_consolidate(space_id="{SPACE}")`
3. After explicit completion confirmation, optionally verify with `mid_read_all("{SPACE}")`

## 📊 Useful commands

| Action                          | Command                                                                   |
| ------------------------------- | ------------------------------------------------------------------------- |
| Read full context               | `mid_read_all("{SPACE}")`                                                |
| Read rules                      | `space_rules("{SPACE}")`                                                  |
| Write a note                    | `short_note(space_id="{SPACE}", category="...", content="...")`            |
| Consolidate                     | `mid_consolidate(space_id="{SPACE}")`                                    |
| See recent notes                | `short_read(space_id="{SPACE}")`                                           |
| See another agent's notes       | `short_read(space_id="{SPACE}", agent="other-agent")`                      |
| Space info                      | `space_info("{SPACE}")`                                                   |
```

> 💡 **For a new project**: copy this file into `<project-root>/CLAUDE.md`, change the `SPACE` line, that's it!

### Minimalist version (`~/.claude/CLAUDE.md` global)

If you'd rather not commit Hivemind instructions in every project, add this short block to `~/.claude/CLAUDE.md`:

```
You have access to Hivemind (MCP server "hivemind").
- At startup: space_rules("{SPACE}"), mid_read_all("{SPACE}"), short_read(space_id="{SPACE}")
- During work: short_note(space_id="{SPACE}", category="...", content="...")
- After meaningful notes and user validation (unless active instructions require immediate consolidation): call mid_consolidate(space_id="{SPACE}") at most once, then return without polling or re-reading
`{SPACE}` is defined in the current project's CLAUDE.md. The agent is auto-detected from the token.
```

Each project then declares only its `{SPACE}` value in its own `CLAUDE.md`.

---

## 🔄 Recommended Workflow

### Typical development session workflow

```
┌────────────────────────────────────────────────┐
│  1. STARTUP                                    │
│     space_rules("my-project")                  │
│     mid_read_all("my-project")                │
│     short_read(space_id="my-project")           │
│     → Claude reads rules + bank + live notes   │
├────────────────────────────────────────────────┤
│  2. WORK (loop)                                │
│     • Claude codes, analyzes, replies          │
│     • short_note(space_id="my-project", …)       │
├────────────────────────────────────────────────┤
│  3. AFTER MEANINGFUL, USER-VALIDATED WORK      │
│     mid_consolidate(space_id="my-project")    │
│     → LLM synthesizes notes into the bank      │
│     → Live notes deleted after success         │
└────────────────────────────────────────────────┘
```

### Consolidation decision

| Situation | Recommendation |
| --- | --- |
| No meaningful new notes | Do not consolidate |
| Meaningful notes, no explicit immediate instruction | Confirm the summary with the user, then enqueue at most once |
| Active instruction explicitly requires immediate consolidation | Enqueue at most once, then return without polling or re-reading |
| User explicitly asks for the job status | Make one status check with `bank_consolidation_status(job_id)` |

### Real-time visualization

While Claude Code works, open the web UI to watch live:

```
http://localhost:8080/live
```

Notes will appear in real time in the **Live Timeline**, and the **Bank** updates after each consolidation.

---

## 👥 Multi-agent: Claude Code + Cline + other supported clients

Hivemind lets **multiple agents** collaborate on the same memory space.

### Scenario: Claude Code (development) + Cline (review)

For several agents to collaborate, create **one token per identity**:

1. With a manager, call `token_create` for `claude-code-dev` and
   `cline-review`.
2. Save each plaintext + exact full hash from its one-time response.
3. Invite each hash to the shared space with `space_invite_token`.
4. Configure each agent with its own `read,write` token.

The agent's identity is **automatically derived from its token** every time it calls `short_note` or `mid_consolidate`. No `agent` parameter to pass.

### Agent-to-agent communication

Agents don't talk to each other directly. They communicate **through the shared space**:

```
Claude Code   → short_note(space_id="my-project", category="question", content="Should we support CSV?")
Cline         → short_read(space_id="my-project", category="question")   ← sees the question
Cline         → short_note(space_id="my-project", category="decision", content="No, JSON only")
Claude Code   → short_read(space_id="my-project", category="decision")   ← sees the answer
```

### Per-agent consolidation

Each agent consolidates **its own notes** without interfering with others'. A
manage/admin agent can explicitly consolidate all notes with
`mid_consolidate(space_id="my-project", agent="")`; the default call remains
caller-scoped.

---

## 🔍 Troubleshooting

### `claude mcp list` doesn't show hivemind

1. Check the server is running: `curl http://localhost:8080/health`
2. Check the project `.mcp.json`, or `~/.claude.json` for local/user scope
   (no trailing comma, braces closed)
3. Fully quit Claude Code and relaunch — the file is read only at startup
4. Inspect the logs: `claude --debug` then run a short session

### "401 Unauthorized" error

- Token is wrong, expired, or revoked
- Make sure `HIVEMIND_TOKEN` is set in the environment that launched Claude
  Code and starts with `lm_`
- Run `claude mcp list`, inspect `/mcp`, and call `system_whoami` after fixing it
- Never use the bootstrap credential for routine agent access

### "Access denied to space" error

The token is restricted to certain spaces (`space_ids`). Either:
- Ask a manager with access to call
  `space_invite_token(space_id="my-project", token_hash="sha256:<64 lowercase hex>")`
  using the exact canonical full hash.
- Or ask an admin to update the token globally with `admin_update_token`.

### Claude Code prompts for permission on every call

Whitelist the tools via `.claude/settings.local.json` (see Step 3.4), or type `/permissions` in the session to add them interactively.

### Claude Code doesn't use Hivemind on its own

Without an explicit `CLAUDE.md`, Claude Code doesn't know it should call these tools at the start of a session. Add the Step 5 template to `<project-root>/CLAUDE.md` or `~/.claude/CLAUDE.md`.

### MCP won't connect behind a VPN or proxy

If Hivemind is on a remote server, check that:
- Port 443 (HTTPS) or 8080 (HTTP) is reachable
- The URL in the Claude Code config is correct (with `/mcp` at the end)
- Confirm the server in `/mcp`, then call `system_whoami`; `/health` alone does
  not test authentication

### Following a consolidation in progress

Do not follow it automatically. `mid_consolidate` returns an asynchronous
acknowledgement with `job_id` and
`next_action="return_to_user_without_polling"`; call it once and return. Use
`bank_consolidation_status(job_id)` only for an explicit user-requested status
check. If the acknowledgement itself times out, diagnose the connection or
server and do not blindly submit a duplicate job.

---

## 🖥️ With Claude Desktop

Claude Desktop is a different surface from Claude Code:

- **Remote connector UI:** **Settings → Connectors → Add custom connector** is
  brokered through Anthropic's cloud and expects a publicly reachable MCP
  server with supported OAuth. Hivemind's current static bearer-token mode
  cannot be entered as a custom Authorization header in that UI.
- **Local Desktop configuration:** `claude_desktop_config.json` is a separate
  local MCP mechanism. Anthropic does not document environment-variable
  expansion for bearer headers in that file. Do not copy a Hivemind token into
  a repository or publish a token-bearing example.

Until Hivemind supports the connector's OAuth flow or Anthropic documents a
safe secret provider for Desktop's local HTTP configuration, use Claude Code
for bearer-authenticated Hivemind access. Do not reuse the Claude Code JSON in
Desktop: a URL without an explicit transport type is invalid, and timeout
fields in Claude Code `.mcp.json` use milliseconds (`600000` means ten
minutes), not seconds. Do not assume that an undocumented Desktop field has the
same contract.

---

## 📊 Summary

| Step      | Action                                                  | Time       |
| --------- | ------------------------------------------------------- | ---------- |
| 1         | Start Hivemind (`docker compose up -d`)              | 1 min      |
| 2         | Create a token (`mcp_cli.py token create`)              | 30 sec     |
| 3         | Configure Claude Code (`claude mcp add`)                | 1 min      |
| 3.4       | Whitelist the tools (`.claude/settings.local.json`)     | 1 min      |
| 4         | Manager creates the space and invites the exact token hash | 30 sec  |
| 5         | Add the project's `CLAUDE.md`                           | 2 min      |
| **Total** | **Ready to use**                                        | **~6 min** |

---

*Hivemind ↔ Claude Code integration guide — [Full documentation](README.md)*
