# MCP Tools Specification — Hivemind

> **Contract revision**: 2.6.0 (independent of the product release version in
> [`VERSION`](../VERSION)) | **Author**: Hivemind maintainers
>
> The live registered MCP tool surface is **61 tools = 48 direct registrations
> + 13 canonical tier aliases**, arranged in 8 categories. 13 canonical
> `short_*`/`mid_*`/`long_*` aliases are registered and callable as thin,
> behaviorally-identical re-registrations of their historical `live_*` /
> `bank_*` / `graph_*` implementations (see
> [Compatibility & deprecation expectations](#compatibility--deprecation-expectations)).
> `admin_audit_recent` is a deliberate cross-cutting, admin-gated registration;
> `token_create` and `space_invite_token` are direct registrations with no
> alias, and `space_create` requires `manage` permission. The two net-new
> `long_ingest` / `long_query` tools are also direct registrations with no
> `graph_*` twin.
>
> The permission model is the real **4-level hierarchy
> `admin ⊃ manage ⊃ write ⊃ read`** enforced in `auth/context.py`
> (`check_write_permission` / `check_manage_permission` /
> `check_admin_permission`). The permission matrix below covers all 43
> historical tools and flags every destructive / `confirm`-gated tool. See
> [Rules immutability](#rules-immutability-clarification) for the
> `space_update_rules` contract. Each tier-mapped tool is annotated with its
> canonical `short_*`/`mid_*`/`long_*` alias name; see
> [`TOOL_MAPPING.md`](TOOL_MAPPING.md) for the frozen tier-mapping table. ADR
> cross-references use the numbers indexed in `docs/adr/README.md` (ADR-0002
> tiers + grammar, ADR-0005 alias policy).

---

## Overview

Hivemind exposes **48 direct MCP tools** in 8 categories, plus **13 canonical
tier aliases**. Historical `live_*`, `bank_*`, and `graph_*` names remain
registered as compatibility surfaces; they are tool aliases, not the current
product identity.
The **live registered surface is 61 = 48 + 13**
(48 direct registrations + 13
`short_*`/`mid_*`/`long_*` aliases). The 2 net-new `long_ingest` /
`long_query` tools, `admin_audit_recent`, and
`token_create` / `space_invite_token` are all registered directly with no
alias. Tool
names are derived 1:1 from the decorated
Python function name; there are **zero `@mcp.tool(name=...)` overrides** in
`src/live_mem/tools/*.py`.

| Category        | Tools | Description                                        |
| --------------- | ----- | -------------------------------------------------- |
| **System** (3)  | 3     | Service health & identity                          |
| **Space** (10)  | 10    | Memory space CRUD and bounded invitation           |
| **Token** (1)   | 1     | Manager-safe non-admin token creation              |
| **Live** (3)    | 3     | Real-time notes (`short` tier)                     |
| **Bank** (11)   | 11    | LLM-consolidated Memory Bank (`mid` tier)          |
| **Graph** (6)   | 6     | Bridge to Graph Memory / ontology engine (`long` tier) |
| **Backup** (5)  | 5     | Backup & restore                                   |
| **Admin** (9)   | 9     | Token management, maintenance (GC), recent console/auth audit |

Per-category counts match the code: system 3, space 10, token 1, live 3, bank
11, graph 6, backup 5, admin 9 = **48 direct**. The 13 aliases bring the
registered total to **61**.

### Registration versus discovery

Registration is the complete compatibility contract; discovery is the compact
agent-facing projection. The server keeps all **61** names registered and
callable by exact name, while authenticated `tools/list` responses advertise
only canonical `agent_core` names for the effective request permission:

| Permission | Discovered names |
|---|---:|
| `read` | 17 |
| `write` | 20 |
| `manage` | 24 |
| `admin` | 24 |

Historical aliases, operator/admin-console tools, and Mesh HTTP operations are
not discovered. Hiding a name is not authorization: every exact-name call still
uses the handler's fresh permission and space-scope checks. Token permission or
scope changes take effect on the next call and successful effective rescope
responses include `mcp_reconnect_required=true` so clients can refresh cached
discovery. The total registry, exact profile lists, and console rendering hints
are generated in [`TOOL_EXPOSURE.md`](TOOL_EXPOSURE.md); the independent
complete registration baseline remains `tests/fixtures/tool_surface.json`.

### Tier grammar (ADR-0002)

The `live_*` / `bank_*` / `graph_*` tools map to the public `short` / `mid` /
`long` grammar. The 13
canonical `short_*`/`mid_*`/`long_*` names are registered as additive aliases bound to the
identical implementation function (ADR-0005 "thin re-registration, never a copy").
Both names are callable today. The historical name always stays callable. 13 tools
earn a tiered alias; the other 35 direct tools — including `space_*`, `token_*`,
`system_*`, `backup_*`, `admin_*`, bank ops/supervision, and the two net-new
long tools — keep their names only. The 2 net-new `long_ingest` / `long_query` tools are also direct
registrations with no `graph_*` twin. Each
affected section below is annotated **→ alias (live): `…`** or
**→ no tiered alias (keep historical)**.

---

## Conventions

### Standardized Return Format

Every tool returns a `dict` with a `status` field:

```python
{"status": "ok", "data": ...}           # Success
{"status": "error", "message": "..."}   # Error
{"status": "created", ...}              # Resource created
{"status": "deleted", ...}              # Resource deleted
{"status": "not_found", ...}            # Resource not found
{"status": "forbidden", ...}            # Access denied
{"status": "queued", ...}               # Accepted background consolidation job
{"status": "partial", ...}              # Durable progress exists; recovery required
```

### Permissions

The real permission model is a **4-level hierarchy** enforced in
`src/live_mem/auth/context.py`:

> **`admin ⊃ manage ⊃ write ⊃ read`** — a higher level satisfies any check for a
> lower one. `check_write_permission()` accepts `write`/`manage`/`admin`;
> `check_manage_permission()` accepts `manage`/`admin`; `check_admin_permission()`
> accepts `admin` only.

Every request to the Streamable HTTP MCP transport (`/mcp`) first requires a
valid authentication token; agent clients send it as an `Authorization: Bearer`
header. There is **no anonymous MCP discovery or tool-call tier**. The separate
HTTP `GET /health` probe is public and intentionally returns a smaller response.
In the table below, 🔓 means that the tool handler adds no permission or
space-scope check beyond the already-authenticated MCP transport; it does not
mean that `/mcp` can be called without authentication.

| Symbol | Permission | Description                                                        |
| ------ | ---------- | ----------------------------------------------------------------- |
| 🔓     | MCP baseline | Valid token required by `/mcp`; no additional handler permission |
| 🔑     | Read       | Token with `read` permission + space access                       |
| ✏️     | Write      | Token with `write` permission; mutations stay inside its persisted space allowlist |
| 🛠️     | Manage     | Token with `manage` permission + space access; bounded provisioning and operator escape hatches |
| 👑     | Admin      | Token with `admin` permission (global / token management)         |

> **Note on the matrix and earlier sections.** The historical spec collapsed
> `manage` into the `write`/`admin` columns. This reconciliation surfaces `manage`
> explicitly. The original spec recorded the *minimum* token scope per tool in a
> read/write/admin vocabulary; where the code actually calls
> `check_manage_permission()` it is noted **(manage)** in the tool section and in
> the matrix footnotes below. `manage` is a strict superset of `write` and a
> strict subset of `admin`.

---

## 1. System — Health & Identity

### `system_health` 🔓

Checks the service health status (S3, LLMaaS, space count).
The call uses the authenticated `/mcp` transport but performs no additional
handler-level permission check. For an anonymous liveness probe, use
`GET /health` instead.

```python
@mcp.tool()
async def system_health() -> dict:
```

**Response**:
```json
{
  "status": "healthy",
  "service_name": "Hivemind",
  "version": "<contents of VERSION>",
  "uptime_seconds": 3600,
  "services": {
    "s3": {"status": "ok", "bucket": "hivemind", "latency_ms": 45},
    "llmaas": {
      "status": "ok",
      "model": "configured-model",
      "model_available": true,
      "latency_ms": 120
    }
  },
  "spaces_count": 3
}
```

The top-level `status` is `healthy` only when every configured dependency probe
returns `ok`; otherwise it is `degraded`. `service_name` follows
`MCP_SERVER_NAME` (default `Hivemind`) and `version` is read from `VERSION`.

---

### `system_about` 🔓

Service information, version, and the permission-aware tool projection. Like
`system_health`, this handler adds no permission check, but the `/mcp` transport
still requires a valid authentication token.

```python
@mcp.tool()
async def system_about() -> dict:
```

**Response** (the `tools` array is shortened here; its real length equals
`tools_count`):

```json
{
  "status": "ok",
  "name": "Hivemind",
  "version": "<contents of VERSION>",
  "description": "Shared memory layer for collaborative AI agents",
  "author": "Lesur AI",
  "documentation": "https://github.com/Lesur-ai/hivemind",
  "tools_count": 17,
  "tools": [
    {"name": "system_health", "description": "..."}
  ]
}
```

`name` follows `MCP_SERVER_NAME`. `tools_count` and `tools` are computed from
the caller's current permission projection and therefore vary by token.

---

### `system_whoami` 🔑

Identity of the current token used to reach the server. Returns `client_name`
(= agent identity, Token=Agent), `auth_type` (`bootstrap` or `token`),
`permissions`, `allowed_spaces`, and — for S3-stored tokens — token metadata
(email, created/expires dates). Requires a valid authentication (read minimum).

```python
@mcp.tool()
async def system_whoami() -> dict:
```

> Implemented in `system.py:163`.
> → No tiered alias (cross-cutting `system_*`, keep historical name).

---

## 2. Space — Memory Space Management

> → All `space_*` tools are **cross-cutting** and keep historical names only (no
> `short_*`/`mid_*`/`long_*` alias).

### `space_create` 🛠️ (manage)

Creates a new memory space with its rules.

```python
@mcp.tool()
async def space_create(
    space_id: str,          # Identifier: letters, numbers, underscore, hyphen (max 64)
    description: str,       # Short description
    rules: str = "",        # Optional Markdown rules; empty loads DEFAULT_RULES_FILE
    owner: str = ""         # Owner (optional, informational)
) -> dict:
```

**Behavior**:
- Validates `space_id`: regex `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`
- If `rules` is empty, loads the configured `DEFAULT_RULES_FILE`; fails closed
  if no readable default is configured.
- Requires `manage` (or inherited `admin`). A `write` token cannot allocate a
  new space or expand its own allowlist.
- For a persisted non-admin manager, revalidates the caller under the token
  lock and automatically grants the new space to that caller.
- Creates `{space_id}/_rules.md` and the two `.keep` preparation objects first
  (immutable on the normal path — see
  [Rules immutability](#rules-immutability-clarification); the `manage`-only
  `space_update_rules` is the deliberate operator escape hatch)
- Writes `{space_id}/_meta.json` **last** as the committed-space marker.
- A matching retry resumes an incomplete preparation idempotently **only when**
  `recovery.retry_safe` is true. An incompatible preparation, stale historical
  grant, or failed token/final-marker write returns `status:"partial"` with an
  exact `recovery.action`; it is never reported as ordinary success and no
  destructive rollback is attempted. Because `space_delete` leaves token
  allowlists intact, reusing a deleted ID first requires an admin to remove all
  persisted scope references named by that action, including admin,
  active/revoked/expired entries.
- Persisted admins and the bootstrap identity have global access and skip the
  allowlist auto-grant. Bootstrap remains an explicit provisioning exception.
- Error if the space already exists (`status: "already_exists"`)

---

### `space_update` ✏️

Updates a space's **metadata** (`description`, `owner`). Only non-empty fields are
changed. **Rules stay immutable through this tool** — use `space_update_rules`
(manage) to change rules.

```python
@mcp.tool()
async def space_update(
    space_id: str,
    description: str = "",   # New description (empty = no change)
    owner: str = ""          # New owner (empty = no change)
) -> dict:
```

> Implemented in (`space.py:146`,
> `check_write_permission`). → No tiered alias (cross-cutting).

---

### `space_update_rules` 🛠️ (manage)

Updates a space's `_rules.md` **in place**, without delete/recreate. Reserved for
operators (`manage`+). This is the explicit, intentional exception to the
"rules immutable after creation" default — see
[Rules immutability](#rules-immutability-clarification).

```python
@mcp.tool()
async def space_update_rules(
    space_id: str,
    rules: str               # New Markdown rules content (full replacement)
) -> dict:
```

**Use cases**: correcting rules, migrating to a new template version, adding
consolidation rules.

> Implemented in (`space.py:202`,
> `check_manage_permission`). → No tiered alias (cross-cutting).

---

### `space_list` 🔑

Lists all spaces accessible by the current token.

```python
@mcp.tool()
async def space_list() -> dict:
```

---

### `space_info` 🔑

Detailed information about a space.

```python
@mcp.tool()
async def space_info(space_id: str) -> dict:
```

The successful response additionally includes an additive `hive_status_label`
field: the unified-space product label from `HIVE_STATUS_LABELS`
(`not_a_space` / `local_only` / `hivemind_healthy` / `hivemind_blocked` /
`unsafe` / `resync_required`). This 6-value space is **distinct** from the
4-value `hive_status` key of `hive_status()`; see ADR-0008. Fail-closed:
on corrupted coordination state the field is `"unsafe"` (never `local_only` /
`not_a_space`) and the call does not raise.

---

### `space_rules` 🔑

Reads the space rules. Rules are immutable on the normal path; an operator can
rewrite them via `space_update_rules` (manage) — see
[Rules immutability](#rules-immutability-clarification).

```python
@mcp.tool()
async def space_rules(space_id: str) -> dict:
```

---

### `space_summary` 🔑

Complete space synthesis (rules + bank + live stats). Useful for an agent to load all context at once on startup.

```python
@mcp.tool()
async def space_summary(space_id: str) -> dict:
```

The successful response additionally includes an additive `hive_status_label`
field: the unified-space product label from `HIVE_STATUS_LABELS`
(`not_a_space` / `local_only` / `hivemind_healthy` / `hivemind_blocked` /
`unsafe` / `resync_required`), a 6-value space **distinct** from the 4-value
`hive_status` key of `hive_status()`; see ADR-0008. Fail-closed: on
corrupted coordination state the field is `"unsafe"` and the call does not
raise. `graph_memory.token` is never emitted by this surface.

---

### `space_export` 🔑

Exports a complete space as a tar.gz archive (returns base64).

```python
@mcp.tool()
async def space_export(space_id: str) -> dict:
```

The successful response additionally includes an additive `hive_status_label`
field: the unified-space product label from `HIVE_STATUS_LABELS`
(`not_a_space` / `local_only` / `hivemind_healthy` / `hivemind_blocked` /
`unsafe` / `resync_required`), a 6-value space **distinct** from the 4-value
`hive_status` key of `hive_status()`; see ADR-0008. Fail-closed: on
corrupted coordination state the field is `"unsafe"` and the call does not
raise. The `_meta.json` inside the archive remains masked via
`mask_meta_secrets` (the raw `graph_memory.token` is never exposed).

---

### `space_delete` 🛠️ (manage) ⚠️ **destructive**

Deletes a space and ALL its data (irreversible). `destructiveHint=True`. Requires
`confirm=True`; gated by `check_manage_permission()`.

Deletion removes the space prefix but intentionally does **not** rewrite token
allowlists. Once a quiescent deletion has removed the commit marker, those
historical grants do not authorize the missing space; they instead block later
reuse of the identifier. An admin must explicitly remove every stale
persisted scope reference—including from admin/revoked/expired entries—before
`space_create` can reuse the ID. The same zero-reference rule applies to a
compatible incomplete preparation: the creating actor's own grant is not a
retry exception.

```python
@mcp.tool()
async def space_delete(
    space_id: str,
    confirm: bool = False,          # Must be True to confirm
    unsafe_recovery: bool = False  # Explicit shared/unsafe Hivemind recovery; never bypasses corrupt-state refusal
) -> dict:
```

`unsafe_recovery` is an advanced MCP-only recovery path. The normal console
does not send it. Without it, shared/unsafe Hivemind spaces are refused; even
with it, unclassifiable/corrupt coordination state remains fail-closed.

The service snapshots the prefix, deletes and reprobes each payload key,
re-lists, then deletes/reprobes `_meta.json` **last**. Success is count-honest:

```json
{
  "status": "deleted",
  "space_id": "project-alpha",
  "files_total": 12,
  "files_deleted": 12
}
```

Any unconfirmed payload/marker delete is **not** success. The typed recovery
shape is:

```json
{
  "status": "partial",
  "space_id": "project-alpha",
  "recovery_required": true,
  "message": "<server message>",
  "files_total": 12,
  "files_deleted": 10,
  "failed_keys": ["project-alpha/bank/example.md"],
  "marker_preserved": true,
  "recovery": {
    "retry_safe": true,
    "action": "<exact operator action>"
  }
}
```

`marker_preserved` can be `true`, `false`, or `null` when its state cannot be
read. A marker-absent residual prefix is returned as `partial` and is never
cleaned automatically. Clients must render the counts, failed keys, marker
state, and recovery action; they must not toast/navigate as success or blindly
retry when `recovery.retry_safe` is false.

**Operational precondition:** quiesce every same-space short, mid,
long/graph, backup/GC, and Hivemind mutation/background job before calling
`space_delete`, and keep it quiescent through recovery. The lifecycle lock
serializes create/delete/invite only. Without quiescence, a late writer can
leave orphan data or republish `_meta.json` after the final probe; marker-last
does not claim a universal or distributed deletion barrier.

> **Permission note:** the historical matrix listed this as 👑 admin; the code
> actually calls `check_manage_permission()` (`manage` or `admin`). Documented as
> `manage` here; The original spec recorded `admin` as its conservative minimum.
> → No tiered alias (cross-cutting).

---

### `space_invite_token` 🛠️ (manage)

Adds exactly one authorized space to one active non-admin token. This is an MCP
allowlist grant, **not** Project Mesh peer enrollment or membership.

```python
@mcp.tool()
async def space_invite_token(
    space_id: str,
    token_hash: str          # Exact `sha256:` + 64 lowercase hex characters
) -> dict:
```

The caller must have `manage` (or inherited `admin`) and access to `space_id`.
The persisted caller is revalidated while `LockManager.tokens` is held. The
target hash must be the exact canonical `sha256:` form; bare/uppercase digests
and prefixes are forbidden. The target must be active,
unexpired, non-admin, and not the reserved `internal-long` token. Malformed,
missing, revoked, expired, admin, and internal targets all return the same
generic error and disclose no target metadata. The operation is add-only and
idempotent:

```json
{"status": "ok", "space_id": "project-alpha", "added": true}
```

`added` is `false` when the target already had access. The response never
includes the target name, permissions, email, or hash. → No tiered alias
(cross-cutting).

If a registry PUT fails and its durable outcome cannot be re-read, the tool
returns `status:"partial"`, `recovery_required:true`, and an operator message.
That is not success and must not be auto-retried: an admin first verifies the
known exact target hash. A failed save that can be confirmed absent returns an
error; neither path invalidates caches or emits a success audit prematurely.

---

## 3. Token — Bounded Delegation

### `token_create` 🛠️ (manage)

Creates a new non-admin token with no initial space access.

```python
@mcp.tool()
async def token_create(
    name: str,
    permissions: str,        # read | read,write | read,write,manage
    expires_in_days: int = 0,
    email: str = ""
) -> dict:
```

The persisted caller is revalidated as active, unexpired `manage`+ while the
token-store lock is held. Bootstrap cannot call this tool; bootstrap creates
the first admin through `admin_create_token`. The tool rejects any admin
permission, the reserved name `internal-long`, and an `expires_in_days` that is
not a non-boolean integer `>= 0` or cannot be represented as a datetime, before
generating a secret. `0` alone means no expiration.
It has no `space_ids` or wildcard input: every token starts with
`space_ids: []` and must later be invited one space at a time.

Success returns the plaintext bearer token and full SHA-256 hash exactly once:

```json
{
  "status": "created",
  "name": "reviewer",
  "token": "lm_...",
  "token_hash": "sha256:...",
  "permissions": ["read", "write"],
  "space_ids": [],
  "expires_at": null,
  "warning": "Save this token now; it will not be shown again.",
  "warning_no_access": "This token has no space access until invited."
}
```

Manager delegation is intentionally transitive. A manager may create another
manager, which also receives the global ability to create new spaces. The
allowlist bounds invitation into existing spaces, not `space_create`: a manager
may create a new space, receive its auto-grant, and then invite others to it.
→ No tiered alias (cross-cutting).

If persistence becomes ambiguous after the secret is generated, the never-
orphan response is `status:"partial"`, `recovery_required:true`, plus that
plaintext `token`, its full `token_hash`, and a recovery `message`. Callers must
retain both values, must not assume the credential is active or absent, and
must have an admin inspect the exact hash before any retry. A confirmed absent
write returns an error and no credential is left active.

---

## 4. Live — Real-time Notes (`short` tier)

> → All three `live_*` tools have a `short_*` alias registered (live); the
> `short_note` category set and Token=Agent contract are preserved verbatim.

### `live_note` ✏️ — → alias (live): `short_note`

Writes a note to the space. This is the primary tool used by agents during their work.

```python
@mcp.tool()
async def live_note(
    space_id: str,
    category: str,          # observation | decision | todo | insight | question | progress | issue
    content: str,           # Note content (free text)
    tags: str = ""          # Comma-separated tags (optional)
) -> dict:
```

> **v0.8.1**: The `agent` parameter was removed. The agent identity is always
> the authentication token's `client_name` (Token = Agent).

**Behavior**:
- Generates a unique filename: `{timestamp}_{agent}_{category}_{uuid8}.md`
- Creates the file with YAML front-matter + content
- No conflict possible (append-only, unique name)
- No lock needed
- The agent is always the token's `client_name` (Token = Agent, v0.8.1)

**Standard categories**:

| Category      | Usage                            | Examples                                |
| ------------- | -------------------------------- | --------------------------------------- |
| `observation` | Factual finding                  | "The build passes", "API returns 200"   |
| `decision`    | Technical/organizational choice  | "Going with S3 instead of SQLite"       |
| `todo`        | Task to do                       | "Implement the backup module"           |
| `insight`     | Analysis, discovered pattern     | "Pattern X is relevant here"            |
| `question`    | Open question                    | "Should we support CSV format?"         |
| `progress`    | Advancement                      | "Auth module: 80% complete"             |
| `issue`       | Problem, bug                     | "LLM timeout exceeds 60s"              |

---

### `live_read` 🔑 — → alias (live): `short_read`

Reads recent live notes.

```python
@mcp.tool()
async def live_read(
    space_id: str,
    limit: int = 50,         # Max notes (default 50)
    category: str = "",      # Filter by category (optional)
    agent: str = "",         # Filter by agent (optional)
    since: str = ""          # ISO datetime: notes after this date (optional)
) -> dict:
```

---

### `live_search` 🔑 — → alias (live): `short_search`

Text search in live notes (case-insensitive).

```python
@mcp.tool()
async def live_search(
    space_id: str,
    query: str,              # Text to search for
    limit: int = 20
) -> dict:
```

---

## 5. Bank — Consolidated Memory Bank (`mid` tier)

> → `bank_*` splits into a **public CRUD/flow** surface with `mid_*` aliases now
> registered (live): `bank_read`→`mid_read`, `bank_read_all`→`mid_read_all`,
> `bank_list`→`mid_list`, `bank_write`→`mid_write`, `bank_consolidate`→
> `mid_consolidate`, `bank_delete`→`mid_delete`. The **internal supervision/ops**
> surface keeps historical names only: `bank_consolidation_status`,
> `bank_consolidation_queues`, `bank_stale_spaces`, `bank_repair`, `bank_compact`.
> 6 aliased, 5 historical-only. Aliases inherit permission + destructive gate verbatim.

### `bank_read` 🔑 — → alias (live): `mid_read`

Reads a specific bank file.

```python
@mcp.tool()
async def bank_read(
    space_id: str,
    filename: str            # Filename (e.g.: "activeContext.md")
) -> dict:
```

---

### `bank_read_all` 🔑 — → alias (live): `mid_read_all`

Reads the entire memory bank in a single request. This is the tool an agent calls at startup to load all its memory context.

```python
@mcp.tool()
async def bank_read_all(space_id: str) -> dict:
```

---

### `bank_list` 🔑 — → alias (live): `mid_list`

Lists bank files (without their content).

```python
@mcp.tool()
async def bank_list(space_id: str) -> dict:
```

---

### `bank_consolidate` ✏️/👑 — → alias (live): `mid_consolidate`

Enqueues LLM consolidation: returns immediately with a job acknowledgement. The background worker reads live notes, rules, and the current bank when the job actually runs, then uses the LLM to produce updated bank files.

Caller contract: call `bank_consolidate` once at session end, then return to the user. Do not wait for completion and do not watch/poll automatically unless the user explicitly asks for a status check.

```python
@mcp.tool()
async def bank_consolidate(
    space_id: str,
    agent: str | None = None  # Omitted/null = caller; explicit "" = all
) -> dict:
```

**`agent` parameter** (added in v0.2.0, corrected in v0.7.4 and 2026-07-18):
- parameter omitted or `agent=null`: auto-detects caller for **every role** → consolidates **own notes only**
- `agent=""` explicitly supplied: consolidates **ALL** notes → manage/admin required
- `agent="my-agent"` (= caller name): consolidates only this agent's notes → write permission sufficient
- `agent="other-agent"` (≠ caller): consolidates another agent's notes → manage permission required

**⚠️ Restrictions**:
- Only one consolidation mutates a space's bank at a time (global per-space lock)
- Same-space requests are serialized FIFO instead of rejected with `conflict`
- The per-space queue is in-memory only (`guarantee="in_memory_best_effort"`)
- The response explicitly sets `next_action="return_to_user_without_polling"`
- `polling.recommended=false`; `bank_consolidation_status` is manual-only for explicit status checks
- If no live notes exist, the background job result is `{"status": "ok", "notes_processed": 0, "message": "No new notes to consolidate"}`
- Configurable timeout (`CONSOLIDATION_TIMEOUT`, default 600s)

**Response**:

```json
{
  "status": "running",
  "job_id": "consol_...",
  "space_id": "my-project",
  "agent": "cline-dev",
  "requested_by": "cline-dev",
  "queue_position": 1,
  "guarantee": "in_memory_best_effort",
  "next_action": "return_to_user_without_polling",
  "polling": {
    "recommended": false,
    "mode": "manual_only",
    "status_tool": "bank_consolidation_status",
    "instruction": "Do not wait for completion or poll automatically. Store the job_id only if an explicit status check is needed."
  }
}
```

**Job result contract (P12-1 — honest structured outcomes).** When the job
finishes, its `result` carries a three-state status:

- `status="ok"` — every selected operation completed successfully;
- `status="error"` — a batch failed **before any durable mutation could have
  started** and zero batches were applied (no bank file, note, or metadata was
  modified);
- `status="partial"` — work was already applied, a durable write started or
  may have started, or durable state is ambiguous. Any failure raised from or
  after the bank-write step stays `partial`, **including on the first batch**.

Additional result fields:

- `failed_batch` (optional, one-based) — present only for an identifiable
  batch failure, including a batch whose bank integration was rejected or
  incomplete (`batch_write_failed`). Exact-selection truncation,
  metadata-only failure, and note-deletion failure stay `partial`
  **without** a fabricated `failed_batch`; `note_delete_failed` is reserved
  for a **completed** bank integration whose source-note deletion alone was
  incomplete (the retained notes stay eligible for a controlled retry).
- `failure_reason` — stable structured token: `batch_prompt_failed`,
  `batch_llm_failed`, `batch_refresh_failed`, `batch_write_failed`,
  `bank_compact_failed`, `note_delete_failed`, `exact_selection_truncated`,
  or `metadata_update_failed`. A consolidation that crashes before
  producing any result is reported by the queue as
  `failure_reason="consolidation_crashed"`.
- `message` — safe generic client text; raw provider/exception detail stays
  server-side, including on the queue crash path.

The queue marks every non-`ok` result as a `failed` job. The terminal
progress `phase` is `failed` for `error` and `partial`, and `done` only for
`ok`.

### `bank_consolidation_status` 🔑 — → no tiered alias (internal/ops)

Returns the in-memory status for a consolidation job.

```python
@mcp.tool()
async def bank_consolidation_status(job_id: str) -> dict:
```

Returns `queued`, `running`, `succeeded`, `failed`, or `not_found`. The caller must have read access to the job's `space_id`. This tool is for explicit manual status checks only; clients must not call it automatically after every `bank_consolidate`. The embedded `result` follows the job result contract above (`ok`/`error`/`partial`, optional `failed_batch`, stable `failure_reason`, terminal progress phase `done` only for `ok`).

---

### `bank_consolidation_queues` 🔑 — → no tiered alias (internal/ops)

Read-only summary of the consolidation lanes (one per space). Use it to drive a multi-space dashboard without N+1 calls.

```python
@mcp.tool()
async def bank_consolidation_queues(space_ids: str = "") -> dict:
```

**Behavior**:

- If `space_ids` is empty → enumerates all spaces accessible to the caller (or all spaces if admin).
- Returns one lane per space with: `lane_state` (idle/queued/running/failed), `running_job`, `queued_count`, `latest_jobs`, `parallelism_model`, `service_config.batch_size`.
- Adds aggregated counters: `total_spaces`, `active_spaces`, `running_spaces`, `queued_jobs`, `failed_recent`.
- Denied spaces are surfaced under `denied_spaces`.

---

### `bank_stale_spaces` 🔑 — → no tiered alias (internal/ops)

Read-only supervision tool that identifies memory banks whose consolidation has fallen behind. Useful to detect inactive agents that left notes queued or sessions that forgot to consolidate.

```python
@mcp.tool()
async def bank_stale_spaces(
    min_notes: int = 5,
    min_age_days: int = 5,
    space_ids: str = "",
) -> dict:
```

**Definition**: a space is `stale` iff `live_notes_count >= min_notes` **AND** `oldest_note_age_days >= min_age_days` (both inclusive).

**Behavior**:

- Lightweight S3 listing (`list_objects` on `{space}/live/`) — no content fetched.
- Oldest note age derived from the timestamp prefix of the filename (`YYYYMMDDTHHMMSS_…`), not from S3 `LastModified` (deterministic, clock-independent).
- Returns `spaces` (filtered + sorted by notes_count DESC, age DESC), `scanned` (every inspected space with its is_stale flag), and `denied_spaces`.
- Displayed `oldest_note_age_days` is truncated to 2 decimals (never rounded up) so the UI never shows an age exceeding the real value at the threshold boundary.

**Payload sketch**:

```json
{
    "status": "ok",
    "spaces": [
        {
            "space_id": "...",
            "live_notes_count": 12,
            "oldest_note_age_days": 8.5,
            "oldest_note_timestamp": "2026-05-13T18:00:00+00:00",
            "oldest_note_filename": "20260513T180000_agent_observation_<hash>.md",
            "is_stale": true
        }
    ],
    "scanned": [...],
    "total_spaces": 25,
    "total_stale": 3,
    "min_notes": 5,
    "min_age_days": 5,
    "denied_spaces": []
}
```

Clients can then iterate and call `bank_consolidate(space_id=…)` per stale space
for caller-only scope. A manage/admin UI offering per-row or bulk "Consolidate
all stale" must serialize that broader intent explicitly with `agent=""` after
confirmation.

---

### `bank_repair` 🛠️ (manage) — → no tiered alias (internal/ops)

Repairs bank files: strips invisible Unicode characters and parasitic prefixes
(`1.MEMORY_BANK/`, `MEMORY_BANK/`, `bank/`), and resolves multi-path duplicates
(same sanitized name at different S3 keys → keeps the most recent, deletes the
rest). Default `dry_run=True` scans and reports without modifying.

```python
@mcp.tool()
async def bank_repair(
    space_id: str,
    dry_run: bool = True     # True = scan/report only, False = apply fixes
) -> dict:
```

> Implemented in (`bank.py:744`,
> `check_manage_permission`). The original spec listed its minimum as `write`; the
> code requires `manage`. Internal/ops only — no `mid_*` alias (a `mid_repair` would
> imply routine CRUD it is not).

---

### `bank_write` 🛠️ (manage) — → alias (live): `mid_write`

Writes or replaces a single bank file directly, **bypassing LLM consolidation**.
For manual corrections when consolidation fails (duplicates, truncated content,
migration). Replaces an existing same-named file; auto-cleans Unicode duplicates.
Filenames are validated against dangerous characters (persistent-XSS guard)
in addition to Unicode sanitization.

```python
@mcp.tool()
async def bank_write(
    space_id: str,
    filename: str,           # e.g. "activeContext.md"
    content: str             # Full Markdown content
) -> dict:
```

> Implemented in (`bank.py:931`,
> `check_manage_permission`). The original spec listed its minimum as `write`; the
> code requires `manage`. Public CRUD write → earns `mid_write` alias (registered);
> the alias inherits the same `manage` gate (ADR-0002 / ADR-0005: no permission softening).

---

### `bank_delete` 🛠️ (manage) ⚠️ **destructive** — → alias (live): `mid_delete`

Deletes a bank file (and all its multi-path duplicates). `destructiveHint=True`.
Requires `confirm=True` (consistent with `space_delete`,
`backup_restore`, `backup_delete`). Irreversible — read the file first if needed.

```python
@mcp.tool()
async def bank_delete(
    space_id: str,
    filename: str,
    confirm: bool = False    # Must be True to confirm
) -> dict:
```

> Implemented in (`bank.py:1038`,
> `check_manage_permission`). The original spec recorded `admin` as its
> conservative minimum; the code requires `manage`. Destructive CRUD → earns
> `mid_delete` alias (registered); the alias keeps the destructive contract +
> `manage` gate verbatim; it is **not** exposed under any softer name.

---

### `bank_compact` 🛠️ (manage) — → no tiered alias (internal/ops)

Compacts oversized bank files via LLM. Files exceeding the universal size limit
(`BANK_FILE_MAX_SIZE`, default 15 KB) are summarized/cleaned using the space rules
to understand each file's role. Default `dry_run=True` scans and reports without
modifying. When `dry_run=False`, the operation is protected by the per-space
consolidation lock and returns `conflict` if a consolidation is in progress.

```python
@mcp.tool()
async def bank_compact(
    space_id: str,
    dry_run: bool = True     # True = scan/report only, False = compact via LLM
) -> dict:
```

> Implemented in (`bank.py:1142`,
> `check_manage_permission`). The original spec listed its minimum as `write`; the
> code requires `manage`. Internal/ops only — no `mid_*` alias.

---

## 6. Graph — Bridge to Graph Memory / Ontology Engine (`long` tier)

> → All four `graph_*` tools have `long_*` aliases registered (live):
> `graph_connect`→`long_connect`, `graph_push`→`long_push`,
> `graph_status`→`long_status`, `graph_disconnect`→`long_disconnect`.
> **Authority boundary (ADR-0002):** no `long_*` alias — including `long_push` —
> is a source of commit validity, rollback, audit, tombstones, watermarks, or
> recovery; `long_push` is a derived projection write into the ontology engine,
> never an authoritative memory commit.
>
> **New long-tier tools:** `long_ingest` (canonical document
> ingestion keyed by a stable `source_path`, dry-run / check-remote / apply
> planning — distinct from the filename-keyed `graph_push` bank mirror) and
> `long_query` (read-only graph/ontology query). These are
> net-new `long_*` tools (no `graph_*` twin), registered directly by
> `tools/graph.py::register` (NOT via ALIAS_MAP). Both stay strictly
> downstream / non-authoritative. See the documented subsections below.

### `graph_connect` ✏️ — → alias (live): `long_connect`

Advanced override that connects a Hivemind space to an external Graph Memory
instance or non-default ontology. The default embedded long runtime auto-binds
on the first `long_push`; routine setup does not call this tool.

```python
@mcp.tool()
async def graph_connect(
    space_id: str,
    url: str,                # Graph Memory URL (e.g.: "http://localhost:8080/mcp")
    token: str,              # Bearer token for Graph Memory
    memory_id: str,          # Target memory identifier
    ontology: str = "general"  # general | legal | cloud | managed-services | presales
) -> dict:
```

**Behavior**:
- Normalizes the URL (adds `/mcp` if missing)
- Tests the MCP Streamable HTTP connection
- Creates the memory in Graph Memory if it doesn't exist
- Saves config in `_meta.json` (`graph_memory` field)

---

### `graph_push` ✏️ — → alias (live): `long_push` (non-authoritative; never a commit path)

Synchronizes the bank into Graph Memory. Deletes old documents and re-ingests up-to-date bank files.

```python
@mcp.tool()
async def graph_push(space_id: str, include_volatile: bool = False) -> dict:
```

**Behavior**:
- Auto-binds an unbound space to the embedded long runtime on its first push;
  no `graph_connect` call is required for the default deployment
- Skips `activeContext.md` and `progress.md` by default; forcing them requires
  `include_volatile=True`, `manage`, and an audit event, and is discouraged
- Intelligent delete + re-ingestion (graph recalculation)
- Orphan cleanup (files removed from the bank)
- ~10-30s per file (LLM entity/relation extraction + embeddings)
- Updates metrics in `_meta.json`

---

### `graph_status` 🔑 — → alias (live): `long_status`

Checks the Graph Memory connection status and retrieves graph stats. The
optional `include_graph` flag adds a display-only graph preview for the admin
console. That preview is server-whitelisted, uses synthetic node identifiers,
omits document URIs/hashes/source paths, and is capped at 160 nodes and 320
edges; the default status call remains lightweight and unchanged.

```python
@mcp.tool()
async def graph_status(space_id: str, include_graph: bool = False) -> dict:
```

---

### `graph_disconnect` ✏️ — → alias (live): `long_disconnect`

Disconnects a space from Graph Memory. Data already pushed remains in the graph.
The optional maintenance mode `use_embedded=True` requires `manage`: it first
validates/provisions the embedded runtime and only then replaces the legacy
override in the local-only `graph_memory` block. Failure preserves the previous
binding. It neither deletes remote graph data nor ingests documents.

```python
@mcp.tool()
async def graph_disconnect(
    space_id: str,
    use_embedded: bool = False,
) -> dict:
```

---

### `long_query` 🔑 — net-new long-tier tool (no `graph_*` twin)

Read-only structured query over the long-tier knowledge graph. It performs no
generative/chat completion, but it does call the configured embedding endpoint
to vectorize the query, so provider availability, latency, and cost still
apply. A thin delegation to `LongEngine.query` → `memory_query`; returns the
graph/ontology results verbatim. `readOnlyHint=True`, `check_access` only — no
`manage`, no audit, no write. Strictly downstream / non-authoritative
(ADR-0010).

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def long_query(space_id: str, query: str, limit: int = 10) -> dict:
```

---

### `long_ingest` ✏️ — net-new long-tier tool (no `graph_*` twin)

Canonical document ingestion as a first-class long-tier capability, **distinct
from the filename-keyed `graph_push` bank mirror**: documents are keyed by a
stable `source_path` (NOT the mutable bank filename) and carry an optional
SHA-256. The **engine plans; the server is not a blind proxy**.
`readOnlyHint=False`, `idempotentHint=True`. Three modes:

- `dry-run` (default) — returns the planned `{source_path, sha256}` set with
  **zero transport** (no GM client built; sha256 computed server-side when
  omitted, echoed when supplied);
- `check-remote` — read-only `SKIP` / `UPDATE` / `INGEST` plan, comparing each
  doc's sha256 against the remote via a single `document_list` (keyed by
  `source_path`; absent → `INGEST`). **No write calls.**
- `apply` — **deferred in v1**: returns `status: ok`, `applied: false`, and a
  structured reason. No blind ingestion write from the tool.

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
async def long_ingest(
    space_id: str,
    documents: list[dict],   # each: {source_path, content|content_base64, sha256?}
    mode: str = "dry-run",   # dry-run | check-remote | apply
    include_volatile: bool = False,
) -> dict:
```

**Volatile guard (tool layer, ADR-0010):** a doc whose `source_path` basename is
a configured volatile file (`GRAPH_PUSH_VOLATILE_FILES`: `activeContext.md` /
`progress.md`) is **rejected by default on every mode** (including dry-run). The
`include_volatile=True` opt-in requires the `manage` permission and emits a
structured `long_ingest_volatile_optin` audit event **after** the gate (a refusal
never audits). The engine/bridge stay pure pass-through; the guard, the `manage`
gate, and the audit live only in the tool layer. Strictly downstream /
non-authoritative — never on the commit / rollback / audit / recovery path.

---

## 7. Backup — Backup & Restore

> → All `backup_*` tools are **cross-cutting** and keep historical names only (no
> tiered alias). Shared-space restore is governed by accepted ADR-0014: normal
> restore refuses Hivemind-marked targets, while an explicit unsafe-recovery
> mode performs the forward-forcing choreography described below.

### `backup_create` ✏️

Creates a complete snapshot of the space on S3.

```python
@mcp.tool()
async def backup_create(
    space_id: str,
    description: str = ""
) -> dict:
```

---

### `backup_list` 🔑

Lists available backups. If `space_id` is empty → lists all accessible backups.

```python
@mcp.tool()
async def backup_list(space_id: str = "") -> dict:
```

---

### `backup_restore` 🛠️ (manage) ⚠️ **destructive** — `confirm=True`

Restores a space from a backup. The caller must have access to the target space,
pass `check_manage_permission()`, and set `confirm=True`.

For a non-Hivemind/local target, the inherited path requires the space not to
exist (`_meta.json` absent). For a target classified `hivemind_healthy`,
`hivemind_blocked`, `unsafe`, or `resync_required`, restore is refused by
default. The only override is the advanced MCP-only disaster-recovery path
`unsafe_recovery=True`; the normal CLI and admin console deliberately do not
send this flag.

```python
@mcp.tool()
async def backup_restore(
    backup_id: str,          # Format: "space_id/timestamp"
    confirm: bool = False,   # Must be True to confirm
    unsafe_recovery: bool = False
) -> dict:
```

Before its first durable mutation, unsafe recovery refuses corrupt critical
live or backup state, a missing local node identity, a backup bank pointer ahead
of the live pointer, a non-leader Mesh worker, an active source pairing, or a
concurrent membership advance. Passing `unsafe_recovery=True` never bypasses
those fail-closed checks.

After preflight, the first durable state is a `RESYNC_REQUIRED` marker and
event. The choreography then:

1. replaces membership with the local node at
   `max(live_epoch, backup_epoch) + 1` so old peers must re-enrol;
2. advances the term to `max(live_term, backup_term) + 1` and fences the old
   token holder;
3. publishes the restored bank as `live_bank_version + 1` through
   `CommitRuntime.stage_commit()` and `apply_commit()`;
4. unions live and backup tombstones, drops the old queue, purges ACKs, and
   prunes watermarks to the replacement membership;
5. excludes the backup's historical `_hivemind/` state from the final copy and
   leaves the node `resync_required` for explicit operator recovery.

Success therefore means that the forward-forced snapshot was durably applied;
it does **not** mean that the shared space is healthy or converged.

> **Permission note:** historical matrix said 👑 admin; code calls
> `check_manage_permission()`. Documented as `manage`; the original spec recorded
> `admin` as its conservative minimum. `admin` still inherits `manage`; neither
> role bypasses the target-space allowlist, `confirm`, or recovery preflight.

---

### `backup_download` 🔑

Downloads a backup as a tar.gz archive (base64).

```python
@mcp.tool()
async def backup_download(backup_id: str) -> dict:
```

---

### `backup_delete` 🛠️ (manage) ⚠️ **destructive** — `confirm=True`

Deletes a backup. `destructiveHint=True`; requires `confirm=True`; gated by
`check_manage_permission()`.

```python
@mcp.tool()
async def backup_delete(
    backup_id: str,
    confirm: bool = False    # Must be True to confirm
) -> dict:
```

> **Permission note:** historical matrix said 👑 admin; code calls
> `check_manage_permission()`. Documented as `manage`; the original spec recorded
> `admin` as its conservative minimum.

---

## 8. Admin — Token Management & Maintenance

> → All `admin_*` tools are **cross-cutting** and keep historical names only (no
> tiered alias). All require `admin` scope (`check_admin_permission()`).

### `admin_audit_recent` 👑

Returns the newest console/auth audit entries from a bounded, per-instance
in-memory ring. It is a best-effort operator view since the last process
restart, **not** a persistent or complete audit trail. Only four event types are
captured: `admin_tool_call`, `login_success`, `login_failed`, and
`auth_rejected`. MCP `/mcp` tool calls are not captured.

```python
@mcp.tool()
async def admin_audit_recent(
    limit: int = 50,  # Clamped to 1..500; one call can return the full ring
) -> dict:
```

The response contains `status`, `entries`, `total`, `capacity`, and
`scope_note`, with entries newest first. Each entry has
exactly `ts`, `event`, `tool`, `arguments_keys`, `client`, and `auth_type`.
Argument **values and space IDs are never stored**. Keys are syntax-filtered,
redacted when unknown/secret-like, and bounded; unknown tools and control
characters are redacted. JSON-content budgets are 32 bytes per argument key,
64 for `tool`, 64 for `client`, and 24 for `auth_type`; at most 16 keys plus
an overflow marker are retained, subject to the final 900-byte serialized-entry
budget. The buffer capacity is configured by `ADMIN_AUDIT_RING_SIZE` (default 500,
validated `1..500` at startup), and the complete configured ring fits a single
response under the default 512 KB response limit.

> → No tiered alias (cross-cutting). `readOnlyHint=True`, but
> access still requires the `admin` permission.

---

### `admin_create_token` 👑

Creates a new authentication token through the global admin/bootstrap path.
Its authorization and input role remain compatible; the response
adds the full canonical `token_hash`. Unlike
`token_create`, it can create an admin token and can assign an initial
`space_ids` snapshot/list to a non-admin target. An admin target is global by
permission: its input scope is ignored and persisted as `[]` so a later
downgrade cannot activate a dormant allowlist. Managers must use
`token_create` instead.

```python
@mcp.tool()
async def admin_create_token(
    name: str,               # Descriptive name
    permissions: str,        # Explicit subset of read/write/manage/admin
    space_ids: str = "",     # non-admin: empty=none, */all=snapshot; admin: ignored/stored []
    expires_in_days: int = 0, # 0 = no expiration
    email: str = ""          # Optional owner metadata
) -> dict:
```

The token is hashed with SHA-256 before storage in `_system/tokens.json`.
Success returns the plaintext `token` and its canonical full `token_hash`
additively in the same one-time response. The plaintext is never stored;
clients should save both because `space_invite_token` requires the exact hash.
`expires_in_days` follows the same strict contract as `token_create`: a
non-boolean integer `>= 0`, representable as a datetime, with `0` alone meaning
no expiration. Invalid or overflowing values are rejected before S3 access,
lock acquisition, or secret generation.

If the registry PUT fails, the service re-reads the store before classifying
the outcome. A matching exact record is normal success. A conclusive absence is
`status:"error"` and returns no credential. An unreadable post-state or a
conflicting record is `status:"partial"`, `recovery_required:true`, and still
returns the one-time plaintext plus `token_hash` so the credential can never be
orphaned. Partial is not success and must not be retried until an admin inspects
that exact hash.

Token-store v2 enforces `space_ids=[]` for every admin entry. The one-shot v1
migration clears legacy admin scopes (including revoked entries). Single and
bulk promotion to admin clear scopes; downgrade from admin starts from `[]` and
applies only an explicit replacement/delta supplied in the same operation.
If admin creation receives a non-empty `space_ids`, success returns
`scope_normalized:true` plus `info` and still persists/returns `space_ids:[]`.

---

### `admin_list_tokens` 👑

Lists metadata of all tokens (never the token itself in clear text).

```python
@mcp.tool()
async def admin_list_tokens() -> dict:
```

---

### `admin_revoke_token` 👑 ⚠️ **destructive**

Revokes a token (permanently disables it). `destructiveHint=True`. Marks the token
inactive without removing it from `tokens.json`. Accepts the hash with or without a
`sha256:` prefix (min 16 hex chars). Gated by `admin` scope (no `confirm` param).

```python
@mcp.tool()
async def admin_revoke_token(token_hash: str) -> dict:
```

---

### `admin_delete_token` 👑 ⚠️ **destructive**

Physically removes a token from the registry (`tokens.json`) — unlike
`admin_revoke_token`, which only marks it inactive. Irreversible.
`destructiveHint=True`. The bootstrap key (env var) is never in `tokens.json` and
cannot be deleted. Gated by `admin` scope (no `confirm` param).

```python
@mcp.tool()
async def admin_delete_token(token_hash: str) -> dict:
```

> Implemented in `admin.py:223`. → No
> tiered alias (cross-cutting).

---

### `admin_purge_tokens` 👑 ⚠️ **destructive** — `confirm=True` for total purge

Bulk-purges tokens. By default (`revoked_only=True`) deletes only revoked tokens
(routine cleanup, allowed without `confirm`). With `revoked_only=False` it deletes
**ALL** tokens (full reset) and **requires `confirm=True`** (a guard against
leaving the server bootstrap-key-only). `destructiveHint=True`. The bootstrap key
is unaffected.

```python
@mcp.tool()
async def admin_purge_tokens(
    revoked_only: bool = True,   # True = revoked only, False = ALL tokens
    confirm: bool = False        # Required True when revoked_only=False
) -> dict:
```

> Implemented in `admin.py:265`. → No
> tiered alias (cross-cutting).

---

### `admin_update_token` 👑

Updates a single token's `permissions`, `email`, and/or authorized `space_ids`.
Three mutually-exclusive `space_ids` modes:
(1) no change; (2) full replacement via `space_ids` (CSV, or `*`/`all` snapshot —
risk of silent revocation); (3) idempotent delta via `space_ids_add` /
`space_ids_remove` (remove applied before add). Returns `warning_no_access` if the
update leaves the token with no readable space.

Under the v2 admin-empty-scope invariant, promotion to admin clears the
allowlist. Downgrade from admin starts from `[]`; absent an explicit scope
operation the resulting non-admin token is unscoped.

```python
@mcp.tool()
async def admin_update_token(
    token_hash: str,
    space_ids: str = "",         # REPLACE mode (CSV or "*"/"all"); empty = no change
    permissions: str = "",       # CSV subset; standard profiles through "read,write,manage,admin"; empty = no change
    email: str = "",             # New owner email; empty = no change
    space_ids_add: str = "",     # DELTA mode: spaces to add (CSV, idempotent)
    space_ids_remove: str = ""   # DELTA mode: spaces to remove (CSV, idempotent)
) -> dict:
```

> Implemented in `admin.py:332`. → No tiered alias (cross-cutting).

---

### `admin_bulk_update_tokens` 👑

Updates **many** tokens in one call. Selects tokens by an AND-combination of
filters (at least one required): `names` (exact CSV), `name_contains`
(case-insensitive substring), `has_space` (exact, case-sensitive `space_id`
membership). Applies `permissions`, `email`, and/or idempotent `space_ids_add` /
`space_ids_remove` (the `*`/`all` sugar is forbidden here). Revoked tokens are
excluded unless `include_revoked=True` (default `False`).

The same transition invariant applies in bulk: promotion clears scopes;
downgrade from admin applies any delta to `[]`, never to a dormant allowlist.

```python
@mcp.tool()
async def admin_bulk_update_tokens(
    names: str = "",             # Exact token names (CSV) — filter
    name_contains: str = "",     # Case-insensitive name substring — filter
    has_space: str = "",         # Tokens whose space_ids contain this space_id — filter
    permissions: str = "",       # New permissions to apply; empty = no change
    email: str = "",             # New email to apply; empty = no change
    space_ids_add: str = "",     # Spaces to add (CSV, idempotent)
    space_ids_remove: str = "",  # Spaces to remove (CSV, idempotent)
    include_revoked: bool = False # Include revoked tokens in the selection
) -> dict:
```

> Implemented in `admin.py:439`. Not
> destructive (mutation only; never deletes a token). → No tiered alias (cross-cutting).

---

### `admin_gc_notes` 👑 ⚠️ **destructive** — `confirm=True` (else dry-run)

Garbage Collector: identifies and processes orphaned notes (older than `max_age_days`).
`destructiveHint=True`. `confirm=False` is a dry-run; `confirm=True` executes.
`max_age_days` is an integer greater than or equal to zero.

```python
@mcp.tool()
async def admin_gc_notes(
    space_id: str = "",                    # Target space (empty = all spaces)
    max_age_days: int = 7,                 # Threshold in days
    confirm: bool = False,                 # False = dry-run, True = execute
    delete_only: bool = False,             # If True + confirm: destructive delete
    expected_eligible_set_token: str = ""  # Required for delete; from prior dry-run
) -> dict:
```

**3 modes**:
1. `confirm=False` (default): **DRY-RUN** — scans and reports orphaned notes,
   strips their raw keys, and returns an opaque `eligible_set_token` for the
   exact eligible-key set.
2. `confirm=True`: **CONSOLIDATES** only the selected orphaned-note keys via
   LLM (with a "⚠️ GC forced consolidation" notice); fresh notes from the same
   agent are not widened into the run.
3. `confirm=True, delete_only=True`: **DELETES** without consolidation (data
   loss) only when `expected_eligible_set_token` exactly matches a fresh,
   lock-protected scan. Any added, removed, or equal-count substituted key
   returns `status:"conflict", reason:"eligible_set_changed", deleted:0`.

Mutating modes preflight every candidate space through the Hivemind route seam
before the first durable write. While holding the relevant consolidation
lock(s), they revalidate before each GC notice, before invoking the consolidator
with its exact selection, and before each per-space delete batch. These are
route-first checks, not an intra-call compare-and-swap guarantee. Only
`DIRECT_LOCAL` proceeds. Healthy shared routes fail as staged-not-implemented;
unsafe, resync-required, or corrupt state retains its typed fail-closed refusal.
A global request preflights all candidates before mutating any one of them.

Completion is explicit and count-honest:

- consolidation returns `status:"ok"` or `status:"partial"` plus
  `consolidated`, `consolidation_requested`, `consolidation_failed`, and
  per-agent `consolidation_details`. Agent details always expose requested and
  processed counts when that agent was selected; after a notice attempt they
  may also expose `notice_written`, `notice_processed`, `notice_cleaned`, and
  `notice_cleanup_reason`, plus bank-file counts and a server message;
- delete returns `status:"deleted"` or `status:"partial"` plus
  `delete_requested`, `deleted`, `delete_failed`; partial delete uses
  `reason:"partial_delete"` and may add `failure_reason` when a later
  per-space route revalidation refuses the remaining work;
- stable public refusal reasons include `invalid_max_age_days`,
  `eligible_set_token_required`, `eligible_set_changed`,
  `consolidation_in_progress`, `route_staged_not_implemented`,
  `route_refused`, `state_corrupt`, `partial_consolidation`, and
  `partial_delete`. Per-agent consolidation details may additionally report
  `consolidation_busy`, `selected_note_set_changed`,
  `selected_note_recheck_failed`,
  `invalid_selected_note_key`, `gc_notice_failed`, or
  `consolidation_failed`; a post-start route read may report
  `route_recheck_failed`. Partial delete `failure_reason` can use a route code
  or `route_recheck_failed`; notice cleanup may report
  `gc_notice_cleanup_failed` through `notice_cleanup_reason`;
- `message` is server-authored and must be rendered verbatim. Clients must run
  and re-review a fresh dry run before a new delete attempt after conflict or
  partial completion. The admin console enforces that workflow by invalidating
  its cached proof; the server token itself is a deterministic exact-set proof,
  not a consumed nonce.

---

## Rules immutability clarification

The v2.4.0 spec described `_rules.md` as "immutable after creation" while also
shipping `space_update_rules`, which rewrites it. **Resolution against actual code:**

- **Normal path:** rules are effectively immutable. `space_create` (manage) writes
  `_rules.md` once; `space_update` (write) explicitly *cannot* touch rules
  (`space.py:168` — "Les rules restent immuables"); `space_rules` is read-only.
  Agents and write-scope clients never alter rules.
- **Operator escape hatch:** `space_update_rules` (`space.py:202`) performs a full
  in-place rewrite of `_rules.md` *without* delete/recreate. It is deliberately
  gated to **`manage`** (`check_manage_permission()` → `manage` or `admin`), one
  at the same minimum level as space creation and one level above ordinary
  write-in-scope mutations. Its docstring labels this
  the explicit exception ("⚠️ Les rules sont normalement immuables après création.
  Cet outil permet de les mettre à jour … Réservé aux opérateurs (manage+)").

So "immutable" is the **default contract for write-scope callers**, not an absolute
storage invariant: a `manage`/`admin` operator can intentionally migrate rules. The
two statements are not contradictory once the permission tier is named. There is **no
`confirm` gate** on `space_update_rules`; the `manage` scope is the only guard.

---

## Complete Matrix — Tools × Permissions (all 48 direct tools)

Permission is the **minimum** scope that satisfies the call; higher scopes inherit
it (`admin ⊃ manage ⊃ write ⊃ read`). "Dest." = `destructiveHint=True`. "Confirm" =
a runtime guard parameter (`confirm`/`dry_run`/`revoked_only`) that must be set to
actually mutate. The **canonical alias** column gives the `short_*`/`mid_*`/`long_*`
name registered (additive, live, never a rename of the historical name);
"—" = keeps historical name only.

| # | Tool | MCP baseline | Read | Write | Manage | Admin | Dest. | Confirm | Canonical alias (live) |
| - | ---- | :----------: | :--: | :---: | :----: | :---: | :---: | ------- | -------------------------- |
| 1 | `system_health` | ✅ | | | | | | — | — |
| 2 | `system_about` | ✅ | | | | | | — | — |
| 3 | `system_whoami` | | ✅ | | | | | — | — |
| 4 | `space_create` | | | | ✅ | (✅) | | — | — |
| 5 | `space_update` | | | ✅ | | | | — | — |
| 6 | `space_update_rules` | | | | ✅ | | | — | — |
| 7 | `space_list` | | ✅ | | | | | — | — |
| 8 | `space_info` | | ✅ | | | | | — | — |
| 9 | `space_rules` | | ✅ | | | | | — | — |
| 10 | `space_summary` | | ✅ | | | | | — | — |
| 11 | `space_export` | | ✅ | | | | | — | — |
| 12 | `space_delete` | | | | ✅ | (✅) | ⚠️ | `confirm=True` | — |
| 13 | `space_invite_token` | | | | ✅ | (✅) | | — | — |
| 14 | `token_create` | | | | ✅ | (✅) | | — | — |
| 15 | `live_note` | | | ✅ | | | | — | `short_note` |
| 16 | `live_read` | | ✅ | | | | | — | `short_read` |
| 17 | `live_search` | | ✅ | | | | | — | `short_search` |
| 18 | `bank_read` | | ✅ | | | | | — | `mid_read` |
| 19 | `bank_read_all` | | ✅ | | | | | — | `mid_read_all` |
| 20 | `bank_list` | | ✅ | | | | | — | `mid_list` |
| 21 | `bank_consolidate` | | | ✅* | (✅) | (✅) | | — | `mid_consolidate` |
| 22 | `bank_consolidation_status` | | ✅ | | | | | — | — |
| 23 | `bank_consolidation_queues` | | ✅ | | | | | — | — |
| 24 | `bank_stale_spaces` | | ✅ | | | | | — | — |
| 25 | `bank_repair` | | | | ✅ | | | `dry_run=False` to apply | — |
| 26 | `bank_write` | | | | ✅ | | | — | `mid_write` |
| 27 | `bank_delete` | | | | ✅ | (✅) | ⚠️ | `confirm=True` | `mid_delete` |
| 28 | `bank_compact` | | | | ✅ | | | `dry_run=False` to apply | — |
| 29 | `graph_connect` | | | ✅ | | | | — | `long_connect` |
| 30 | `graph_push` | | | ✅ | | | | — | `long_push` |
| 31 | `graph_status` | | ✅ | | | | | — | `long_status` |
| 32 | `graph_disconnect` | | | ✅ | | | | — | `long_disconnect` |
| 33 | `backup_create` | | | ✅ | | | | — | — |
| 34 | `backup_list` | | ✅ | | | | | — | — |
| 35 | `backup_restore` | | | | ✅ | (✅) | ⚠️ | `confirm=True`; shared recovery also needs `unsafe_recovery=True` | — |
| 36 | `backup_download` | | ✅ | | | | | — | — |
| 37 | `backup_delete` | | | | ✅ | (✅) | ⚠️ | `confirm=True` | — |
| 38 | `admin_create_token` | | | | | ✅ | | — | — |
| 39 | `admin_list_tokens` | | | | | ✅ | | — | — |
| 40 | `admin_revoke_token` | | | | | ✅ | ⚠️ | — | — |
| 41 | `admin_delete_token` | | | | | ✅ | ⚠️ | — | — |
| 42 | `admin_purge_tokens` | | | | | ✅ | ⚠️ | `confirm=True` (total purge) | — |
| 43 | `admin_update_token` | | | | | ✅ | | — | — |
| 44 | `admin_bulk_update_tokens` | | | | | ✅ | | — | — |
| 45 | `admin_gc_notes` | | | | | ✅ | ⚠️ | `confirm=True` (else dry-run); delete also requires prior-set token | — |
| 46 | `admin_audit_recent` | | | | | ✅ | | — | — (cross-cutting) |
| 47 | `long_query` | | ✅ | | | | | — | — (net-new) |
| 48 | `long_ingest` | | ✅ | | (✅) | | | `mode=apply` deferred; `include_volatile` needs `manage` | — (net-new) |

\* `bank_consolidate`: `write` is sufficient to consolidate your own notes
(`agent=caller`, omitted, or `null`). `manage`/`admin` is required to consolidate
ALL notes (`agent=""` explicitly supplied) or another agent's notes
(`agent=other`). Shown as `write`
minimum with `(✅)` manage/admin for the cross-agent path.

**Manage-vs-conservative-minimum footnote.** Rows 12, 27, 35, 37 show `manage` (real
`check_manage_permission()`) with `(✅)` admin because admin inherits manage.
The original spec recorded the conservative minimum for these as `admin`
(rows 12, 25) and the prose minimum for rows 23/24/26 as `write`; the code in fact
calls `check_manage_permission()` for all of `space_delete`, `bank_repair`,
`bank_write`, `bank_delete`, `bank_compact`, `space_update_rules`, `backup_restore`,
`backup_delete` (`auth/context.py:194-218`). Where the inventory and the code-level
detail differ, both are surfaced; neither softens a gate.

Rows 4, 13, and 14 are governed by ADR-0022. They use actor-aware persisted
caller revalidation in addition to the tool-level `manage` gate. The original
tool inventory predates those semantics and is not a current minimum-permission
authority for them.

**Destructive tools (8):** `space_delete`, `bank_delete`, `backup_restore`,
`backup_delete`, `admin_revoke_token`, `admin_delete_token`, `admin_purge_tokens`,
`admin_gc_notes` (all `destructiveHint=True`). Six enforce an explicit `confirm`
runtime gate (`space_delete`, `backup_restore`, `backup_delete`, `bank_delete`,
`admin_purge_tokens` total-purge, `admin_gc_notes` dry-run vs execute); GC delete
also requires the opaque exact-set token from a prior dry run. Token revoke/delete
rely on admin scope. `bank_repair` and
`bank_compact` are not destructive-flagged but mutate only when `dry_run=False`.

---

## Compatibility & deprecation expectations

This section governs the long-term relationship between the **historical tool names**
(`live_*`, `bank_*`, `graph_*`) and the **canonical tier grammar**
(`short_*`, `mid_*`, `long_*`).

### Stage A — current (indefinite)

- The 13 canonical aliases (`short_note`, `short_read`, `short_search`,
  `mid_read`, `mid_read_all`, `mid_list`, `mid_write`, `mid_consolidate`,
  `mid_delete`, `long_connect`, `long_push`, `long_status`, `long_disconnect`)
  are **registered and callable**. They are thin
  re-registrations of the **identical implementation function** — no divergent
  copy, no behavior difference.
- The historical `live_*` / `bank_*` / `graph_*` names are **compatibility
  aliases supported indefinitely**. There is no removal date and no timer.
  Callers using the historical names will continue to work without change.
- New integrations should prefer the canonical `short_*` / `mid_*` / `long_*`
  names — they are the recommended grammar going forward.
- The 35 direct no-alias tools (`space_*`, `token_*`, `system_*`, `backup_*`,
  `admin_*`, bank ops/supervision, plus direct-only long tools) have no tier
  alias now and keep their names as canonical.
- The 2 net-new `long_ingest` / `long_query` tools are direct long-tier
  registrations with no historical `graph_*` twin and no alias.

### Stage B — soft deprecation (future, ADR-gated)

Any future soft deprecation of a historical name requires an explicit
**Architecture Decision Record** (currently governed by **ADR-0005**,
`docs/adr/0005-compatibility-aliases.md`). A Stage B soft-deprecation must last
**at least one published release** before any removal consideration. Stage B may
add in-tool notices (e.g., response metadata, server banner) for the affected
names, but must not break callers.

Per-tool "Legacy" or "deprecated" markings in individual tool descriptions or
metadata are a Stage-B concern and are **out of scope for Stage A**. No such
markings appear in this document today.

### Stage C — removal (future, ADR-gated)

Removal of a historical name, if ever needed, is **never date-triggered or
release-number-triggered automatically**. It requires a separate ADR decision
after Stage B has run for at least one published release. Any Stage C removal
will be communicated through the standard ADR process and changelog.

> **Summary for integrators:** historical `live_*` / `bank_*` / `graph_*` calls
> are safe indefinitely. The canonical `short_*` / `mid_*` / `long_*` names are
> preferred for new work. No removal is planned, contemplated, or will happen
> without an explicit ADR and a Stage B soft-deprecation period.

---

*See the contract-revision banner at the top of this file.*
