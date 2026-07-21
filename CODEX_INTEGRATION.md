# 🔌 Hivemind Integration Guide for OpenAI Codex

> **Documentation revision**: 2026-07-19

This guide connects **OpenAI Codex** to Hivemind's unified short, mid, and long
memory through one MCP endpoint. The reusable cross-client instruction contract
is in [Configure agents for unified Hivemind memory](docs/AGENT_MEMORY_SETUP.md).

---

## 📋 Table of Contents

- [Prerequisites](#-prerequisites)
- [Step 1 — Obtain a Hivemind Token](#-step-1--obtain-a-hivemind-token)
- [Step 2 — Configure Codex via `.codex/config.toml`](#-step-2--configure-codex-via-codexconfigtoml)
- [Step 3 — Create a Memory Space](#-step-3--create-a-memory-space)
- [Step 4 — Give Codex Instructions](#-step-4--give-codex-instructions)
- [Recommended Workflow](#-recommended-workflow)
- [Troubleshooting](#-troubleshooting)

---

## 📦 Prerequisites

| Component          | Detail                                                              |
| ------------------ | ------------------------------------------------------------------- |
| **OpenAI Codex**   | CLI or environment with MCP server support                          |
| **Hivemind**       | Running Hivemind instance (self-hosted or hosted)                   |
| **Bearer Token**   | `read,write` token created on your Hivemind instance            |

---

## 🔑 Step 1 — Obtain a Hivemind Token

Codex needs a **new Bearer Token dedicated to this agent identity**, with at
minimum `read,write` permissions. Never reuse a legacy Live Memory or Graph
Memory token, an administrator token, or one token shared by several agents.

### Option A — Via the CLI

```bash
cd /path/to/hivemind
export MCP_TOKEN=<trusted_manage_or_admin_token>

# Create a "write" token for Codex
uv run python scripts/mcp_cli.py token create codex-agent -p read,write
```

The CLI will display something like:

```
Token created successfully!
  Name   : codex-agent
  Token  : lm_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2
  Perms  : read, write

⚠️  This token will NEVER be displayed again. Copy it now!
```

> **⚠️ IMPORTANT**: Copy this token immediately! It will never be shown again (only the SHA-256 hash is stored).
> Also retain the exact `token_hash` printed by the command. The token starts
> with no space access; a manager must invite that full hash in Step 3.

### Option B — Via the Admin Console

1. Open `https://<your-hivemind-instance>/admin` in your browser
2. Log in with a manage or admin credential
3. Navigate to **Access**
4. Click **Create Token**, fill in the name (`codex-agent`), set permissions to `read,write`
5. Copy the displayed token

### Option C — Hosted Hivemind Instance

If you are using a hosted Hivemind instance, your token has already been provisioned by the operator. Use it directly — it looks like:

```
lm_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ Your token is confidential. Never include it in documentation or commit it to a repository.

---

## ⚙️ Step 2 — Configure Codex via `.codex/config.toml`

Codex reads MCP configuration from `~/.codex/config.toml` or from
`.codex/config.toml` at the project root. Project configuration is loaded only
after you trust the project, so review a repository before approving it. See
the current [official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

### 2.1 Create or Edit the Configuration File

```bash
mkdir -p ~/.codex
# or at project level:
mkdir -p .codex
```

### 2.2 Add the Hivemind Server

Open `.codex/config.toml` and add the following section:

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.example.com/mcp"
```

Export the dedicated token in the environment that starts Codex without
recording it in shell history:

```bash
printf 'Hivemind token: '
IFS= read -r -s HIVEMIND_TOKEN
printf '\n'
export HIVEMIND_TOKEN
codex
```

Do not put the token itself in `config.toml`, shell history, or the repository.
`bearer_token_env_var` makes Codex read the value at runtime and send it as an
`Authorization: Bearer ...` header.

> Keep only this Hivemind memory entry for the project. The `long` tier is
> reached through Hivemind; do not add a separate Graph Memory MCP entry.

### 2.3 Hosted Instance Example

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.example.com/mcp"
```

### 2.4 Self-Hosted Instance Example

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.your-domain.com/mcp"
```

For a local development instance:

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "http://localhost:8080/mcp"
```

### 2.5 Where to Place `config.toml`

| Scope           | Location                           | When to Use                            |
| --------------- | ---------------------------------- | -------------------------------------- |
| **Global**      | `~/.codex/config.toml`             | All projects share the same server     |
| **Per-project** | `<project-root>/.codex/config.toml`| Per-project MCP configuration          |

> **Precedence**: project-level config overrides global config if both exist.
> A project-level file is considered only for a trusted project. The example is
> safe to commit because it contains only an environment-variable name, but the
> environment value remains a secret and must never be committed.

### 2.6 Verify the Connection

After saving `config.toml`, restart Codex from a shell where `HIVEMIND_TOKEN` is
set, then verify all three layers:

```bash
codex mcp list
```

1. In Codex, use `/mcp` and confirm that `my-hivemind` is connected.
2. Ask Codex to call `system_whoami` on `my-hivemind`. Check the expected agent
   name, `read,write` permissions, and assigned space.
3. Ask it to call `space_rules`, `mid_read_all`, and `short_read` for that
   space. A green `/health` response alone proves only server reachability; it
   does **not** validate the token.

For a negative authentication check, an invalid token must be rejected on the
MCP endpoint:

```bash
curl -i -H 'Authorization: Bearer invalid' \
  https://hivemind.example.com/mcp
# Expected: HTTP 401
```

---

## 📁 Step 3 — Create a Memory Space

Before Codex can write notes, a trusted provisioner needs a **memory space**
with **rules** and must invite the Codex token. Routine `read,write` sessions do
not discover `space_create`; a separate `manage` or `admin` provisioning
session discovers and may invoke the complete `space_create` → `token_create`
→ `space_invite_token` flow. You can also provision through the CLI or
authenticated Admin Console.

### Via the Hivemind CLI

```bash
uv run python scripts/mcp_cli.py space create my-project \
  --rules-file ./RULES/live-mem.standard.memory.bank.md \
  -d "My Codex project"

uv run python scripts/mcp_cli.py space invite my-project \
  sha256:<exact-64-lowercase-hex-from-step-1>
```

Alternatively, use the authenticated Admin Console's space workflow, then
invite Codex's exact token hash. Keep routine Codex sessions on the invited
`read,write` credential.

### Standard Rules Template

```markdown
# Memory Bank Rules

## Files to Maintain

### projectbrief.md
Vision, objectives, project scope.

### activeContext.md
Current focus, work in progress, recent decisions, next steps.

### progress.md
What works, what remains to build, known issues.

### techContext.md
Technologies used, configuration, technical constraints.

### systemPatterns.md
Architecture, patterns, technical decisions, components.

### productContext.md
Why this project exists, problems solved, user experience.
```

---

## 📝 Step 4 — Give Codex Instructions

Hivemind already includes the `long` ontology/knowledge-graph tier behind the
same endpoint and `space_id`. Do not configure a second Graph Memory server.
For the full source hierarchy, fail-closed startup gate, long lookup policy,
and workflow-rewrite checklist, use the canonical
[agent memory setup](docs/AGENT_MEMORY_SETUP.md).

For Codex to automatically use Hivemind, add instructions in a `AGENTS.md` file at the root of your project (Codex automatically loads it as agent-level instructions).

### 4.1 Recommended `AGENTS.md` Template

````markdown
# Codex Agent Instructions — Hivemind MCP

Hivemind is my canonical shared memory across agents and sessions. Codex may
also maintain local product memory; I treat it as local, non-authoritative
context and never use it to bypass the Hivemind startup gate. Repository files
remain the final authority.

## MCP Server Configuration

My persistent memory is managed by the **Hivemind** MCP server (`my-hivemind`).

> **The only value to customize:**
> - **SPACE** = `my-project`  ← Replace with your space_id
>
> All instructions below use `{SPACE}`. Agent name is auto-detected from the token.

## At the Start of EVERY Task (MANDATORY)

1. Call `space_rules("{SPACE}")` to read the rules (bank structure)
2. Call `mid_read_all("{SPACE}")` to load ALL consolidated context
3. Call `short_read(space_id="{SPACE}")` to read **unconsolidated notes**
4. Read the content carefully before starting
5. Identify the current focus in `activeContext.md`

> ⚠️ NEVER start working without reading the bank first.
> If any startup call fails, times out, returns non-OK, or is unavailable, stop
> before mutation. Do not substitute local memory or a legacy memory endpoint.

## During Work

Write frequent, atomic notes with `short_note`:

```
short_note(space_id="{SPACE}", category="<category>", content="...")
```

**Categories**: `observation`, `decision`, `progress`, `issue`, `todo`, `insight`, `question`

Use `long_query` for historical or cross-document context, then re-read the
referenced repository file before acting. Long memory is derived and
non-authoritative. Do not run `long_push`, alter long bindings, or ingest
documents as routine session-end work. Never ingest `activeContext.md`,
`progress.md`, or raw mid-memory summaries into long memory.

## After meaningful work

Only if meaningful new notes exist, confirm the work summary with the user
unless the project's active instructions explicitly require immediate
consolidation, then call:

```
mid_consolidate(space_id="{SPACE}")
```

This default consolidates only the current token's notes. A manage/admin caller
must pass `agent=""` explicitly to consolidate all agents' notes.

> 🔕 `mid_consolidate` is **fire-and-forget**: it returns an async job ack (`running` / `queued`) with `next_action="return_to_user_without_polling"`. **Call it once and return to the user.** Do not watch or poll. `bank_consolidation_status(job_id)` exists for **explicit manual checks only**.

## Mandatory Rules

1. **NEVER write directly to the bank** — only the LLM consolidation does that
2. **Always pass `space_id="{SPACE}"`** in every call
3. **Write atomic notes after each significant step** — 1 note = 1 fact, 1 decision, or 1 task
4. **Consolidate only meaningful work** — after user validation unless active instructions explicitly require it, call `mid_consolidate` at most once and return without polling or re-reading the bank
5. **Read the bank at startup** — never work without context
6. **Use one Hivemind endpoint** — never substitute legacy memory services
7. **Keep secrets out of instructions** — tokens and URLs belong in MCP client configuration
````

### 4.2 Minimalist Version (inline prompt)

```
You have access to Hivemind (MCP server: my-hivemind).
- At startup: space_rules("my-project"), mid_read_all("my-project"), short_read(space_id="my-project")
- During work: short_note(space_id="my-project", category="...", content="...")
- After meaningful notes and user validation (unless active instructions require immediate consolidation): call mid_consolidate(space_id="my-project") at most once, then return without polling or re-reading
The agent name is auto-detected from the authentication token.
```

---

## 🔄 Recommended Workflow

```
┌────────────────────────────────────────────────┐
│  1. STARTUP                                    │
│     space_rules("my-project")                  │
│     mid_read_all("my-project")                │
│     short_read(space_id="my-project")           │
│     → Codex reads rules + bank + live notes    │
├────────────────────────────────────────────────┤
│  2. WORK (loop)                                │
│     • Codex codes, analyzes, responds          │
│     • short_note(space_id="my-project", …)       │
├────────────────────────────────────────────────┤
│  3. AFTER MEANINGFUL, USER-VALIDATED WORK      │
│     mid_consolidate(space_id="my-project")    │
│     → LLM synthesizes notes into bank          │
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

---

## 👥 Multi-agent: Codex + Cline + Others

Hivemind enables **multiple agents** to collaborate on the same memory space:

1. Create one token per agent (`codex-agent`, `cline-agent`, `claude-agent`, etc.)
2. Configure each agent with its own token
3. All agents share the same `space_id`

Agent identity is **automatically inferred from the token** — no manual specification needed.

Inter-agent communication happens **through the shared space**:

```
Codex  → short_note(space_id="my-project", category="todo", content="Add pagination to /users endpoint")
Cline  → short_read(space_id="my-project", category="todo")  ← sees Codex's task
Cline  → short_note(space_id="my-project", category="progress", content="Pagination implemented")
Codex  → short_read(space_id="my-project", category="progress")  ← picks up where Cline left off
```

---

## 🔍 Troubleshooting

### Codex Doesn't See Hivemind Tools

1. Run `codex mcp list`, then inspect `/mcp` in Codex.
2. Verify `config.toml` is in the correct location and the TOML syntax is valid.
3. Ensure the project is trusted when using `.codex/config.toml`.
4. Ensure `HIVEMIND_TOKEN` is present in the environment that launched Codex.
5. Confirm the URL ends with `/mcp`, then call `system_whoami` to validate auth.

### "401 Unauthorized" Error

- The token is incorrect, expired, or revoked
- Verify `HIVEMIND_TOKEN` exists in the environment that launched Codex and
  starts with `lm_`
- Check if the token has been revoked via the admin console

### "Access Denied to Space" Error

The token is restricted to certain spaces (`space_ids`). Either:
- Ask a manager with access to invite the exact canonical full hash:
  ```
  space_invite_token(space_id="my-project", token_hash="sha256:<64 lowercase hex>")
  ```
- Or ask an admin to add the space globally:
  ```
  admin_update_token(token_hash, space_ids_add="my-project")
  ```

### Consolidation Is Slow or Times Out

`mid_consolidate` returns a quick asynchronous acknowledgement containing a
`job_id` and `next_action="return_to_user_without_polling"`. Call it once and
return; do not wait for the LLM work on the same MCP request and do not start an
automatic status loop. Use `bank_consolidation_status(job_id)` only when the
user explicitly asks for that job's status. A timeout before the acknowledgement
indicates a connection/server problem, not a need to repeat the consolidation.

### TOML Syntax Errors

Common mistakes in `config.toml`:

```toml
# ✅ CORRECT
bearer_token_env_var = "HIVEMIND_TOKEN"

# ❌ WRONG (stores the secret in clear text)
http_headers = { "Authorization" = "Bearer lm_abc123" }

# ❌ WRONG (Codex would use this literal name as the environment variable)
bearer_token_env_var = "lm_abc123"
```

---

## 📊 Summary

| Step      | Action                                                    | Time       |
| --------- | --------------------------------------------------------- | ---------- |
| 1         | Obtain a token (`token create codex-agent`)               | 1 min      |
| 2         | Export the token and configure the MCP URL                 | 2 min      |
| 3         | Manager creates the space and invites the exact token hash | 30 sec     |
| 4         | Add `AGENTS.md` with Memory Bank instructions             | 2 min      |
| **Total** | **Ready to use**                                          | **~6 min** |

---

*Hivemind Integration Guide for OpenAI Codex — [Full Documentation](README.md)*
