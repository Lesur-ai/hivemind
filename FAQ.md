# ❓ FAQ — Hivemind

🇫🇷 [Version française](FAQ.fr.md)

---

> **Version notation:** Hivemind has its own public SemVer line (current value
> in [`VERSION`](VERSION)). Older version numbers mentioned below identify the
> inherited Live Memory release in which a behavior first appeared; they are
> provenance, not later Hivemind releases. Token-store schema versions such as
> `version: 2` are data-format versions, not product versions.

## General Concepts

### What are short, mid, and long memory tiers in Hivemind?

Hivemind exposes **one product** with three memory horizons:

|                  | **`short`**                          | **`mid`**                                  | **`long`**                                 |
| ---------------- | ------------------------------------ | ------------------------------------------ | ------------------------------------------ |
| **Role**         | Immediate live notes                 | Consolidated structured memory             | Derived ontology / knowledge graph         |
| **Data**         | Append-only observations / decisions | Markdown bank (LLM-consolidated)           | Typed entities + relations                 |
| **Storage**      | S3 (files)                           | S3 (files)                                 | Embedded Graph Memory runtime (Neo4j + Qdrant), shipped in the default compose stack (ADR-0019) |
| **Authority**    | Yes (commit path)                    | Yes (commit path)                          | **No** — derived projection only (ADR-0010) |
| **Analogy**      | Whiteboard                           | Project notebook                           | Library index                              |

The three tiers are complementary. Agents **write fast** (`short`),
**consolidate** into a durable bank (`mid`), and **capitalize** knowledge into
an ontology-backed graph (`long`).

> The historical tool names `live_*` / `bank_*` / `graph_*` remain callable as
> **compatibility aliases** that map one-to-one onto `short_*` / `mid_*` /
> `long_*`. Both sets stay registered indefinitely under the public
> [compatibility policy](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations)
> (ADR-0005); the canonical
> short/mid/long grammar is the recommended grammar going forward.

### What is a "space"?

An isolated memory space = a project. It contains:
- **Rules**: Markdown template defining the bank structure
- **Live notes**: observations, decisions, todos... from agents (append-only)
- **Bank**: Markdown files consolidated by the LLM according to rules

### What are "rules"?

Rules define the Memory Bank structure. They are written in Markdown when the
space is created and may later be replaced by a caller with `manage`
permission through `space_update_rules`. Replacing rules changes future
consolidation instructions; it does not silently rewrite existing mid files.
Review and version rule changes like other project policy. The LLM uses the
current rules to create and maintain bank files.

Example rules (standard Memory Bank):
```markdown
### projectbrief.md
Objectives, scope, success criteria.

### activeContext.md
Current focus, recent changes, next steps.

### progress.md
What works, what's left, known issues.
```

---

## Agents and Tokens

### What is the relationship between a token and an agent?

Since inherited Live Memory **v0.8.1**, each token **is** an agent. The token's `client_name` is automatically used as the agent identity — there is no `agent=` parameter in `short_note`.

|                        | **Token = Agent**                             |
| ---------------------- | --------------------------------------------- |
| **Role**               | Authentication **and** identity               |
| **Example**            | Token `cline-dev` → agent `cline-dev`         |
| **Shareable?**         | No — 1 token = 1 agent = 1 identity           |
| **Where provided?**    | `Authorization: Bearer` header (auto-detected) |

**Why this change?** The old model (Token ≠ Agent) allowed passing a free agent name, causing orphaned notes (agent not recognized during consolidation), identity spoofing, and fragmentation.

### Can an agent read another agent's notes?

Yes! `short_read(space_id="my-project")` returns notes from ALL agents. That's the collaboration principle: each agent sees the work of others. You can also filter by agent: `short_read(space_id="my-project", agent="claude-review")`.

---

## Permissions and Security

### What are the permission levels?

Since inherited Live Memory **v1.5.0**, there are 4 **hierarchical and cumulative** levels:

| Level      | Includes              | Access                                                                                                                                             |
| ---------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **read**   | —                     | Read: `bank_read`, `short_read`, `space_info`, `backup_list`, etc.                                                                                  |
| **write**  | read                  | Mutate only authorized spaces: `short_note`, own `mid_consolidate`, `graph_push`, etc.                                                             |
| **manage** | write + read          | Provisioning + maintenance: `space_create`, `token_create`, `space_invite_token`, `bank_write`, `space_delete`, backup restore/delete              |
| **admin**  | manage + write + read | Administration: `admin_create_token`, `admin_gc_notes`, etc.                                                                                       |

A `write` token cannot create a space/token, widen access, directly modify bank
files, or delete spaces. It only mutates spaces already in its allowlist.
`manage` is a high-trust, transitive provisioning role: every manager can create
new spaces globally and can create further non-admin managers.

### Why are permissions cumulative?

Each level **automatically includes** all lower levels. You don't need to specify `read,write` if you grant `manage` — `manage` already includes `write` and `read`.

```
read < write < manage < admin
```

In practice, when creating or updating a token, always specify the **full list** of permissions (e.g.: `"read,write,manage"`), because the `permissions` field is an **explicit list** stored on S3, not a single level. The server checks for the presence of the required level in this list.

### What type of token should I create for my use case?

| Use case | Recommended permissions | `space_ids` |
| --- | --- | --- |
| AI agent in work mode (Cline, Claude) | `read,write` | Project spaces |
| Provisioner / AI agent + maintenance | `read,write,manage` | Relevant existing spaces; can create new spaces globally |
| Human operator (multi-project maintenance) | `read,write,manage` | Relevant existing spaces; can create/delegate managers |
| Administrator | `read,write,manage,admin` | Empty (admin sees everything) |
| Reader / monitoring dashboard | `read` | Spaces to monitor |

### How to restrict a token to specific spaces?

Each token has a `space_ids` field listing authorized spaces:

```bash
# Restrict KSE to 3 spaces
uv run python scripts/mcp_cli.py token update sha256:363... -p "read,write" -s "live-mem,graph-mem,mcp-office"
```

**`space_ids` semantics (inherited Live Memory v1.5.0+)**:
- `space_ids = ["a", "b"]` → access only to these spaces
- `space_ids = []` for a **non-admin** → **no access** (changed in inherited Live Memory v1.5.0, was "all" before)
- `space_ids = []` for an **admin** → access to **everything**; v2 requires this
  empty stored form so a later downgrade cannot activate a dormant allowlist

When **creating a non-admin token** via `admin_create_token`, you can use:
- `space_ids=""` (default) → "mute" token (no access to existing spaces). The response contains a `warning_no_access` field to explicitly signal this.
- `space_ids="a,b,c"` → explicit list.
- `space_ids="*"` or `space_ids="all"` → **snapshot** of all existing spaces at creation time (not future spaces — intentional to stay aligned with the strict inherited v1.5.0 semantics).

For an admin target, those inputs are ignored and `space_ids: []` is stored.
Promotion to admin clears scopes; downgrade starts empty unless that same
update explicitly assigns a new non-admin scope. Bulk updates use the same rule.

Admin compatibility tools accept canonical explicit IDs on non-admin targets even when a space does
not yet exist. Do not use that as a reservation: any such non-admin pre-grant
blocks later `space_create` for the same ID (including a matching partial
preparation) until an admin removes the grant. Prefer create first, assign
second.

A manager uses the narrower flow instead:

```text
token_create(name="agent", permissions="read,write")
space_invite_token(space_id="project-a", token_hash="sha256:<64 lowercase hex>")
```

`token_create` has no `space_ids` parameter and always starts empty. Each
`space_invite_token` call adds one existing space the manager can access.

### The hash returned by `admin_list_tokens` contains `sha256:` — should I pass it as-is?

**Both forms are accepted** by `admin_revoke_token`, `admin_delete_token`, and `admin_update_token`:
```bash
admin_update_token(token_hash="sha256:f172084ef03...", space_ids="x")  # OK
admin_update_token(token_hash="f172084ef03...", space_ids="x")          # OK too
```

The minimum is still 16 hex characters (8 hash bytes) to avoid accidental collisions.

That compatibility rule applies only to admin lifecycle tools.
`space_invite_token` is deliberately stricter: it requires the exact canonical
hash (`sha256:` + all 64 lowercase hex characters). Bare hashes, uppercase, and
prefixes are rejected so manager onboarding cannot become a registry oracle.

### What happens when a token creates a new space?

A `write` token is denied. A persisted `manage` token can create a new space
regardless of its current allowlist; the committed space is automatically added
to that manager's `space_ids`. Admin/bootstrap already have global access and
need no grant. `_meta.json` is written last; a partial/recovery-required result
is never treated as success or automatically rolled back. Retry the exact same
inputs only when `recovery.retry_safe` is true. Otherwise follow
`recovery.action`: in particular, deleting a space leaves historical token
allowlists intact, so reuse of that ID is blocked until an admin explicitly
removes every stale scope reference (including admin/revoked/expired tokens).

### What must I do before deleting or reusing a space ID?

Quiesce every same-space writer and background job first: notes,
consolidation, graph operations, restore/GC, and Hivemind activity. Deletion
reprobes each payload object and removes `_meta.json` last, but its lifecycle
lock does not fence every writer. A `partial` result is not success: follow its
counts, failed keys, marker state, and `recovery.action` without automatic
retry. Even after a clean deletion, `space_create` refuses reuse until an admin
removes that ID from every token carrying it, including admin/revoked/expired
entries. Admin entries normally carry `[]` under v2; counting them remains a
defense-in-depth barrier for legacy or pre-migration objects.

### How to add the `manage` permission to a token?

```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -p "read,write,manage"
```

⚠️ Permission updates **replace** the full list — always include `read,write` in addition to `manage`.

This is an explicit trust upgrade. There is **no automatic promotion**
of existing writers: upgrade only tokens that should allocate arbitrary new
spaces and delegate further managers. Otherwise keep the writer and provision
through a separate manager.

### What happened during the inherited Live Memory v1.5.0 migration?

Before inherited Live Memory v1.5.0, `space_ids=[]` meant "access to everything". Since that release, it means "no access" (for non-admin tokens).

**One-shot schema migration**: before the server accepts requests, its ASGI
lifespan upgrades a legacy version-1 token store. Non-admin tokens with
`space_ids=[]` receive a point-in-time snapshot of all existing spaces, then the
store is durably written as version 2; failure aborts startup. A version-2 empty
allowlist is never widened on later restarts. New `token_create` tokens
therefore remain unscoped until invited. This migration never promotes a writer
to manager.

The embedded Graph Memory validator does not perform this migration. It accepts
only an integer version `2` registry and denies missing, legacy, future, or
malformed versions. It validates the whole token array before matching a
bearer, so corruption in any other entry also denies authentication, matching
Hivemind's fail-closed authority. The long engine cannot bypass the startup or
structural-validation gate.

### Can I give admin rights to a token?

Yes, but with caution:
```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -p "read,write,manage,admin"
```

An admin token can manage other tokens, consolidate all agents' notes, and run the GC. It sees all spaces through the `admin` permission; v2 stores its `space_ids` as `[]`.

A manager cannot create or promote an admin through `token_create`; the global
admin/bootstrap lifecycle must use `admin_create_token` or `admin_update_token`.

---

## Consolidation

### How does consolidation work?

1. The LLM reads the **rules**, the **current bank**, the **previous synthesis**, and the **live notes**
2. It produces updated bank files (pure Markdown)
3. Consolidated notes are **deleted** from `live/`
4. A residual synthesis is saved

### What happens if 2 agents consolidate at the same time?

An `asyncio.Lock` per space prevents simultaneous consolidations:
- The first request is accepted as an async job with `{"status": "running"}` and a `job_id`
- The second receives `{"status": "queued"}` with a `job_id` and queue position
- Call `mid_consolidate` once at session end and return to the user; do not watch/poll unless an explicit status check is requested

This is intentional: both agents write to the same bank files. Sequential consolidation lets each agent see the previous one's work.

### Can I consolidate ALL agents' notes at once?

Yes. A manage/admin caller must request that scope explicitly with
`mid_consolidate(space_id="my-project", agent="")`. Omitting `agent` (or
passing `null`) always consolidates only the caller's own notes, regardless of
permission level.

⚠️ **Permissions**: consolidating another agent's notes or all agents' notes requires a **manage** (or admin) token. A write token can only consolidate its own notes (omit `agent`, pass `null`, or use `agent="my-name"`).

### What happens to notes after consolidation?

They are **deleted** from `live/`. Their content is integrated into bank files. This is irreversible (hence the value of backups).

### Can the consolidator invent content (hallucinate)?

Yes. Consolidation is LLM-assisted and can still omit, distort, or invent
content. Hivemind supplies note metadata and a defensive prompt that asks the
model to preserve domain terms, source numbers, avoid invented structures, and
keep agents/tasks distinct. An optional heuristic validation pass can report
apparently unattributed claims with `CONSOLIDATION_VALIDATION_ENABLED=true`.
It is disabled by default, is not a proof of correctness, and does not replace
human review of important bank changes. Keep source records outside Hivemind
when the domain requires an authoritative record.

Run the shipped regression tests with:

```bash
uv run pytest tests/test_issue17_validation.py
```

**If you see unsupported content**, report it on the
[Hivemind issue tracker](https://github.com/Lesur-ai/hivemind/issues) with the
notes and bank output.

### How do I find which banks need consolidation across many spaces?

Use **`bank_stale_spaces`** (introduced in inherited Live Memory v2.4.0) — a read-only supervision tool that scans the
S3 listing of every accessible space and flags those whose live notes have
accumulated:

```bash
# Default thresholds: ≥5 unconsolidated notes AND oldest ≥5 days old
uv run python scripts/mcp_cli.py bank stale-spaces

# Custom thresholds + trigger consolidation on each stale space
uv run python scripts/mcp_cli.py bank stale-spaces --min-notes 10 --min-age-days 7 --consolidate
```

The same view is available in the web admin console under
**Consolidation → stale filter** (`/admin#/consolidation`), with live filter
inputs and per-row / bulk `Consolidate` buttons.

A space is reported as `stale` iff `short_notes_count >= min_notes` **AND**
`oldest_note_age_days >= min_age_days` (both inclusive). Listing is lightweight
(S3 keys only, no content fetched). Oldest age is derived from the timestamp
prefix of the filename (`YYYYMMDDTHHMMSS_…`), not S3 `LastModified` — so the
result is deterministic and independent of clock drift between agents.

### What is bank compaction (`bank_compact`)?

When bank files grow too large (> `BANK_FILE_MAX_SIZE`, default 15 KB), they may cause consolidation failures (LLM context window overflow) or slow performance.

`bank_compact` summarizes oversized files via a dedicated LLM call, preserving key decisions and milestones while removing obsolete details.

```bash
# Scan only (dry-run, default)
uv run python scripts/mcp_cli.py bank compact my-space

# Apply compaction
uv run python scripts/mcp_cli.py bank compact my-space --apply
```

**Auto-compaction** is also triggered automatically before consolidation if the bank exceeds `COMPACT_THRESHOLD` (default 60%) of the LLM's output budget.

### Can I use an HTTP proxy for outbound connections?

Yes. Supported since inherited Live Memory **v1.8.1**, set `PROXY_URL` in `.env`:

```env
PROXY_URL=http://10.0.0.1:3128
```

This routes every Internet-bound request through the proxy: S3 (boto3) and LLM
(httpx) traffic of the core — consolidation calls and the `/health` /
`system_health` probes — plus the embedded Graph Memory egress: extraction and
embedding LLM calls (including their provider-health probes), document-storage
S3, and the shared token-store S3 reads. It's a **custom variable** (not
`HTTP_PROXY`) to avoid affecting other Python libraries: the internal
Hivemind→graph-memory MCP bridge, Neo4j, Qdrant, and container-local health
checks always stay direct, and the dev-profile MinIO stack, which does not set
`PROXY_URL`, stays direct too. A proxy failure fails closed — requests are
never silently retried over a direct connection.

---

## Garbage Collector

### Why a Garbage Collector?

If an agent writes notes but never consolidates (crash, deletion, oversight), notes accumulate endlessly in `live/`. The GC identifies and handles these orphaned notes.

### How does the GC work?

3 modes via `admin_gc_notes`:

| Mode              | Parameters                       | Action                                                                 |
| ----------------- | -------------------------------- | ---------------------------------------------------------------------- |
| **Dry-run**       | `confirm=False` (default)        | Scans and reports                                                      |
| **Consolidation** | `confirm=True`                   | Consolidates notes into bank via LLM + adds a "⚠️ GC" notice         |
| **Deletion**      | `confirm=True, delete_only=True, expected_eligible_set_token=<dry-run token>` | Deletes the reviewed exact set without consolidating (data loss) |

The default is a read-only dry run. Its opaque `eligible_set_token` identifies
the exact eligible key set without exposing the keys. Destructive deletion is
an explicit second request that sends the token as
`expected_eligible_set_token`; any addition, removal, or equal-count key
substitution returns `status: "conflict"` and deletes nothing. In the admin
console, the operator must also type the exact `delete <N> notes` challenge.

Before any GC mutation, the service proves all candidate spaces are
`DIRECT_LOCAL`. Under the consolidation locks it revalidates before each GC
notice, before handing the exact selection to the consolidator, and before each
per-space delete batch. These route-first checks do not provide an intra-call
compare-and-swap while LLM or storage work is running. Healthy shared spaces are
staged-not-implemented; unsafe, resync-required, and corrupt spaces fail closed.
Consolidation consumes only the exact selected old-note keys, never fresh notes
from the same agent. A partially completed consolidation or deletion returns
`status: "partial"` with honest requested/processed/deleted/failed counts and
is never retried automatically. Clients should run and re-review a fresh dry
run before a new destructive delete attempt; the admin console enforces this by
invalidating its cached proof. The server token is a deterministic exact-set
proof, not a one-use nonce.

### Does the GC leave a trace in the bank?

On a successful consolidation, yes: the GC writes a special note before the
consolidator runs:
```
⚠️ GARBAGE COLLECTOR — Forced consolidation
The GC detected X orphaned notes from agent 'agent-name' (> 7 days).
These notes were never consolidated by the agent.
```

The LLM sees that note as the first selected input and integrates it into the
bank. A partial or failed run may instead leave the notice unprocessed and clean
it up; inspect the per-agent `notice_processed` and `notice_cleaned` fields.

---

## Docker and Deployment

### How to test locally?

```bash
# 1. Configure environment
python scripts/configure_dev_env.py
uv sync --dev
# For mid/long, set provider URL/key, chat model, embedding model, and its exact
# dimensions as documented in .env.example.

# 2. Start the full default stack
#    (WAF + Hivemind + embedded long runtime + Neo4j + Qdrant + dev MinIO)
docker compose --profile dev up --build -d --wait

# 3. Test
uv run python scripts/test_recette.py
uv run pytest tests/test_issue17_validation.py
```

### How does the WAF work?

Caddy + Coraza (OWASP CRS) protects against injections, XSS, etc. MCP routes (Streamable HTTP) are authenticated by token on the server side. Other routes pass through the WAF.

### How to deploy to production?

1. Set `SITE_ADDRESS=my-domain.com` in `.env`
2. Expose ports 80+443 in docker-compose.yml
3. Caddy automatically obtains a Let's Encrypt certificate
4. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full operator runbook

---

## S3 and Storage

### Why S3 and not a database?

- Simplicity: no schema, no migrations, no DB server
- Portability: everything is Markdown/JSON files
- Scalability: S3 handles billions of objects
- Cost: S3 storage is very affordable

### Why two S3 clients (SigV2 + SigV4)?

Constraint of some S3-compatible storage (notably Dell ECS):
- SigV2 for data operations (PUT, GET, DELETE)
- SigV4 for metadata operations (HEAD, LIST)

This dual wiring is the `S3_SIGNATURE_MODE=dual` default. If you use AWS S3
or MinIO, set `S3_SIGNATURE_MODE=sigv4` in `.env`: a single SigV4 client then
serves every operation — both Hivemind and the embedded Graph Memory runtime
mirror the same setting.

### Can I use AWS S3 or MinIO?

Yes! Configure `S3_ENDPOINT_URL` and credentials, and set
`S3_SIGNATURE_MODE=sigv4` (see [.env.example](.env.example)). The dual
SigV2/V4 mode (`dual`, the default) is only needed for Dell ECS. No code
modification is required.

---

## CLI and Shell

### How to configure the CLI?

3 ways to pass the URL and token:

```bash
# 1. Environment variables
export MCP_URL=http://localhost:8080
export MCP_TOKEN=lm_xxx
uv run python scripts/mcp_cli.py health

# 2. CLI parameters
uv run python scripts/mcp_cli.py --url http://my-server:8080 --token lm_xxx health

# 3. Automatic (reads .env)
uv run python scripts/mcp_cli.py health   # Default URL 8080, token from .env
```

### How to get help on a command?

```bash
# CLI Click (native --help)
uv run python scripts/mcp_cli.py space --help
uv run python scripts/mcp_cli.py bank consolidate --help

# Interactive shell
hivemind> help           # global help
hivemind> help space     # space subcommands
hivemind> space          # same
hivemind> help bank      # bank subcommands
```

### Can I use the CLI in JSON mode for scripting?

Yes! Add `--json` to any command:

```bash
uv run python scripts/mcp_cli.py space list --json | jq '.spaces[].space_id'
```

---

## Troubleshooting — Common Issues

### I get a 403 on all spaces

**Most common cause**: your token has `space_ids=[]` (no access). Under the current semantics inherited from Live Memory v1.5.0, a non-admin token without `space_ids` cannot access anything.

**Diagnosis**:
```bash
uv run python scripts/mcp_cli.py token list --json | jq '.tokens[] | select(.name=="my-token") | .space_ids'
```

**Solution**: ask a manager with access to invite the exact full token hash, or
ask an admin to update the token:
```bash
uv run python scripts/mcp_cli.py space invite space-a sha256:<64-lowercase-hex>
uv run python scripts/mcp_cli.py token update sha256:xxx -s "space-a,space-b"
```

### My `manage` token can't do anything

A `manage` token without `space_ids` cannot access or invite into existing
spaces. It still has global authority to create new spaces and further managers;
a newly created space is auto-added to its allowlist.

**Solution**: have an authorized manager invite it with the exact hash, or have
an admin update it:
```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -s "space-a,space-b"
```

### Consolidation fails with "LLM returned invalid JSON"

Probable cause: the bank is too large. The LLM has a limited context window and may fail on long JSON responses.

**Solutions**:
1. Compact the bank: `bank_compact my-space --apply`
2. Check sizes: `bank_list my-space` — if a file exceeds 15 KB, it's a compaction candidate
3. Retry consolidation after compaction

### `mid_consolidate` returns "queued"

Another agent (or yourself in another terminal) is consolidating the same space. Your request was accepted and will run after earlier same-space jobs.

**Solution**: return to the user without polling. Keep the returned `job_id` only if an explicit status check is needed later. `bank_consolidation_status(job_id)` is manual-only; do not watch/poll automatically.

### I can't find my notes after consolidation

That's normal! Notes are **deleted** from `live/` after consolidation. Their content is integrated into bank files. Use `mid_read_all` to find the consolidated content.

If you think notes were lost, check the residual synthesis: `space_summary my-space`.

---

## Limits and Performance

### How many notes can be written?

Hivemind does not publish an unlimited-capacity claim. One note is capped at
100,000 characters, `short_read` returns at most 500 notes per call, and
consolidation processes up to 200 notes per job by default
(`CONSOLIDATION_MAX_NOTES`). Total retained volume depends on the configured
S3 backend, object lifecycle, request limits, and operator capacity planning.

### What is the latency?

There is no published latency SLA or portable benchmark in this release.
Results depend on S3 distance and load, network paths, selected chat and
embedding models, provider queueing, bank size, and host resources. Benchmark
the exact deployment with representative data and record environment,
operation count, warm-up, and percentile before setting an operational target.

### How many simultaneous agents?

Hivemind does not publish an unlimited-agent or zero-conflict guarantee.
Append-only note writes use unique object keys, while practical concurrency is
bounded by the WAF, Hivemind workers, S3, network, and provider capacity.
Consolidation is queued FIFO per space (one job mutates a space's bank at a
time), and Project Mesh writes require every active member ACK. Load-test the
intended topology. `mid_consolidate` is a call-once async handoff; do not
watch/poll unless explicitly requested.
