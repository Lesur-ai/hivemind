<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/hivemind-mark-dark.svg">
  <img alt="Hivemind" src="assets/brand/hivemind-mark.svg" width="92" height="92">
</picture>

# hivemind

***The open memory layer for collective agent awareness.***

Vendor-neutral, open-source MCP service for three-tier agent memory:
`short` · `mid` · `long`.

Agents notice what others are doing, inherit what others have learned, and
understand complex projects together.

[![protocol](https://img.shields.io/badge/protocol-MCP-00A7C7?style=flat-square)](#how-memory-works)
[![version](https://img.shields.io/badge/version-1.6.1-9CA3AF?style=flat-square)](#license)
[![CI](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml/badge.svg)](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-111827?style=flat-square)](#license)
[![python](https://img.shields.io/badge/python-3.11+-F59E0B?style=flat-square)](#requirements)

Français · [README.fr.md](README.fr.md)

English is the canonical contract. The French README preserves the same
critical behavior but may trail this page's editorial structure.

</div>

---

**MCP transport (1.6.1).** Core and embedded LONG use the same pinned MCP Python SDK 2.3.0 (`MCPServer`). Existing supported MCP clients retain their tool names and aliases; the modern protocol is also served. The Python maintenance CLI and LONG bridge use the SDK 2 Streamable HTTP transport. Inference configuration, stored banks and graph data are unchanged; rebuild both images together. MCP requests are bounded to 75 MiB on the wire, with a 50 MiB decoded-document limit.

> **Security fixes in 1.5.3.** Hivemind 1.5.3 addresses all seven application
> findings from the 16 September 2026 source security review, including Graph
> storage boundaries, request and archive limits, and admin Markdown commands.
> It also updates DOMPurify to 3.4.15. See the
> [security report](docs/SECURITY_AUDIT_1.5.3.md) for verification and remaining
> work, and the [release notes](CHANGELOG.md#153--2026-09-17).

## Why Hivemind?

Agent memory usually lives inside one chat, one IDE, or one vendor. That works
until another agent joins the project or the team changes tools. Then context is
copied by hand, decisions are rediscovered, and useful knowledge fragments.

Hivemind gives the project its own memory. Any MCP-capable agent can connect
with a scoped token, work in the same `space_id`, and leave useful context for
the next agent without moving project knowledge into a vendor-specific history.

## How memory works

Hivemind uses three simple memory horizons:

| Memory | Think of it as | What it does |
| --- | --- | --- |
| **`short`** | A shared scratchpad | Captures what agents are doing now: observations, decisions, questions, and todos. |
| **`mid`** | The project handbook | Turns those notes into organized Markdown that another agent can pick up later. |
| **`long`** | Connected knowledge | Links people, decisions, systems, and ideas so agents can find relevant context even when they ask in different words. |

The normal flow is:

```text
short notes  →  mid synthesis  →  long connected knowledge
   notice           inherit              understand
```

`long` is included in the standard Hivemind stack. You do not install or bind a
separate graph service: the first `long_push` prepares the space automatically.
It derives connected knowledge from source documents and retained MID captures.
Put simply: `long` helps agents discover context; it does not decide what the
project has committed or overwrite the project memory.

The older `live_*`, `bank_*`, and `graph_*` names remain callable as compatibility
aliases for `short_*`, `mid_*`, and `long_*`. New integrations should use the
new names. See [the tool mapping](docs/TOOL_MAPPING.md).

## Project Mesh

Project Mesh lets Hivemind instances share one logical project memory:

- **Same deployment:** agents and teams connect to the same `space_id`.
- **Two deployments:** an administrator pairs one source with one blank target
  using a signed, one-time invitation valid for **3,600 seconds**.

Mesh is enabled by default in production and requires a complete Ed25519
identity, a public HTTPS URL, and a display name. Pairing is an explicit admin
action in `/admin/#/mesh`. The current release creates a **two-node mesh**; a
**third node** is unsupported, and there are no MCP `mesh_*` tools.

For a deliberate single-node setup, set `HIVEMIND_MESH_ENABLED=false`. See the
[Project Mesh guide](docs/PROJECT_MESH.md) for setup and protocol details.

## Quickstart

### Requirements

- Docker 24+
- Docker Compose 2.17.0+
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) for the CLI and tests
- A compatible S3 service
- Chat and embedding provider credentials for `mid` and `long`

The development profile includes MinIO. The default stack includes Hivemind,
the WAF, Graph Memory, Neo4j, and Qdrant.

### 1. Start the stack

```bash
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind

# Creates .env with random local credentials and single-node Mesh mode.
python scripts/configure_dev_env.py
uv sync --locked --dev

docker compose --profile dev up --build -d --wait
docker compose ps
```

Before using `mid` or `long`, add complete chat and embedding profiles to
`.env`. The [provider profile guide](docs/INFERENCE_PROVIDER_PROFILES.md)
contains the supported combinations and migration rules. The v1.4 Cloud Temple
reference chat model is `Qwen/Qwen3.6-27B-FP8`.

On the legacy unified `LLMAAS_*` path, one provider must expose both
`/chat/completions` and `/embeddings`. Split `INFERENCE_*` profiles configure
each role separately; native Anthropic chat uses its own Messages API.

### 2. Create a space and write a note

```bash
export MCP_URL=http://localhost:8080
export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"

uv run python scripts/mcp_cli.py health --json
uv run python scripts/mcp_cli.py space create hivemind-demo \
  --description "Quickstart demo space" \
  --rules-file RULES/live-mem.standard.memory.bank.md

uv run python scripts/mcp_cli.py live note \
  hivemind-demo observation "hello short"
uv run python scripts/mcp_cli.py live read hivemind-demo

uv run python scripts/mcp_cli.py bank consolidate hivemind-demo --json
```

Consolidation runs asynchronously. Save the returned `job_id`, then make one
deliberate status check:

```bash
JOB_ID="paste-returned-job-id"
uv run python scripts/mcp_cli.py bank consolidation-status "$JOB_ID" --json
```

Continue only when the top-level response is `"status": "succeeded"`.
`running` or `queued` means wait and check later; `failed` or `not_found` means
stop and diagnose the job. Do not build an automatic polling loop.

### 3. Build and query connected knowledge

```bash
uv run python scripts/mcp_cli.py bank read-all hivemind-demo --json
uv run python scripts/mcp_cli.py graph push hivemind-demo --json
uv run python scripts/mcp_cli.py graph status hivemind-demo --json
# Replace ing_EXAMPLE with a job ID returned by long_ingest_list(archive=True).
uv run python scripts/mcp_cli.py graph job hivemind-demo ing_EXAMPLE --archive --json
uv run python scripts/mcp_cli.py graph query hivemind-demo "hello" --json

unset MCP_TOKEN
```

That final query searches by meaning, not only by exact wording. For persistent
deployments, replace the bootstrap credential with one manager token and one
dedicated `read,write` token per agent. Follow the
[deployment guide](docs/DEPLOYMENT.md#quickstart-dev).

## Connect an MCP client

For document ingestion, the supported 1.6.0 path uses a chosen ontology name or
YAML with `long_ingest_async`. The `hivemind-ingest` CLI provides batching,
progress tracking and resubmission of unchanged sources; see its
[usage guide](tools/hivemind-ingest/README.md).

Automatic ontology for document ingestion is planned for **1.7.0**. Its
implementation is already present for experimental qualification, but is not
part of supported 1.6.0 document ingestion. This delivery schedule is separate
from historical MID captures, which use an automatic ontology in 1.6.0.
See the [ingestion contract](docs/MCP_TOOLS_SPEC.md) for the experimental
`options={"ontology": "auto"}` path, payloads, restart limits and diagnostics.

Hivemind serves Streamable HTTP at `/mcp` through the WAF on port `8080`.

```python
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx2


async def use_hivemind():
    headers = {"Authorization": f"Bearer {os.environ['HIVEMIND_TOKEN']}"}

    async with httpx2.AsyncClient(headers=headers) as http_client, streamable_http_client(
        "http://localhost:8080/mcp", http_client=http_client,
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            await session.call_tool("short_note", {
                "space_id": "my-project",
                "category": "observation",
                "content": "The release build passed",
            })

            result = await session.call_tool("long_query", {
                "space_id": "my-project",
                "query": "What do we know about the release?",
            })
```

Start with [the agent memory setup guide](docs/AGENT_MEMORY_SETUP.md), then use
the [Codex](CODEX_INTEGRATION.md) or
[Claude Code](CLAUDE_CODE_INTEGRATION.md) integration guide.

## Web interface

### Hivemind Portal (`/admin`)

Open `http://localhost:8080/admin` with an operator token (`write` or higher).
Read-only tokens use the linked `/live` viewer. The Portal covers the
shipped operator workflow in seven areas: **Dashboard**, **Spaces**, **Space
Detail**, **Consolidation**, **Audit**, **Access**, and **Operator tools**.

Use it to inspect memory, manage space access, follow consolidation jobs, work
with backups, and run maintenance actions. The older `/live` page remains a
lightweight real-time viewer for notes and bank files.

Manual compaction is visible in **Consolidation → In progress** and
**Space → Active work**, and on the Dashboard for its displayed spaces. It
shows the start time, an indeterminate running state and the latest final
result, including recovery warnings. The original `bank_compact` request still
waits for completion; dry runs and refused calls do not create jobs. Tracking
uses bounded server memory and is cleared by a restart. The latest manual job
per space is separate from the consolidation queue and its automatic compaction.

The Dashboard follows consolidation activity for up to 20 spaces per page,
with recent results and their details. Optionally show the latest 20 notes from
up to three selected spaces. Auto-refresh continues checking that page while
idle; new spaces appear after a manual refresh. Note selection stays in this
session only. Reading 20 notes still scans that space's full SHORT prefix, so
notes are fetched only for the spaces you select.

In **Space → Memory**, SHORT shows pending notes grouped by your browser's
local day. Apply agent, category or **Since…** filters explicitly; auto-refresh
keeps the applied filters while you edit their inputs. MID lists every bank
file as a tab and updates the selected file without resetting your reading
position. File modification, last consolidation and last refresh have separate
timestamps. The existing `/live` viewer and its read-only access remain available.

**Space → Long memory** has five separate panels: **Overview**, **Ontology**,
**Documents**, **Ingestion jobs** and **Graph**. Browse available ontology YAML
and validate it without changing the configured ontology. Filter documents and
load their content explicitly. Follow ingestion jobs with optional Auto-refresh
and request cancellation after confirmation. Graph shows a bounded snapshot
(up to 160 nodes and 320 edges), with local search and selection. Each panel
loads only its own data through the existing APIs; LONG remains derived memory.

Inside a space, use **Memory** (Short / Mid), **Consolidation**, **Long memory**,
**Rules**, **Access**, **Backups** and **Maintenance**. Permission-dependent
tabs remain hidden when unavailable; **Global audit** opens the instance audit.
Older Space links continue to work. The top bar shows the instance and an
explicit **Check services** action; refreshing a page never probes model health.
The shared **Auto-refresh** control is off by default and offers 15/30/60 seconds
on views integrated with it. Existing views keep their current refresh behaviour
until their Portal integration. Only this preference is stored in the browser;
signing out disables it.

In **Access**, create a token, save its one-time secret, grant access to the
selected spaces, then copy the Codex or Claude Code configuration. Enter the
MCP URL reachable from the client's machine; the configuration references
`HIVEMIND_TOKEN` and never includes the secret. A failed grant can be retried
without repeating successful grants or creating another token. **Done** and
**Finish later** close the instructions; test the connection from your client.
Creating a space opens this same token form with that space selected.

## Configuration

All options are documented in [`.env.example`](.env.example). The main groups
are:

| Area | Variables |
| --- | --- |
| S3 | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, `S3_REGION_NAME` |
| Chat | Complete `INFERENCE_CHAT_*` profile |
| Embeddings | Complete `INFERENCE_EMBEDDING_*` profile |
| First admin | `ADMIN_BOOTSTRAP_KEY` (at least 32 random characters) |
| Mesh | `HIVEMIND_MESH_ENABLED` and, when enabled, the complete identity fields |

Existing 1.x deployments can keep the unified `LLMAAS_*` profile. New
deployments should use the split `INFERENCE_*` profiles. Never mix the two
families: Hivemind refuses ambiguous configuration at startup.

For production sizing, TLS, secrets, proxying, provider profiles, backups, and
recovery, use [the deployment guide](docs/DEPLOYMENT.md).

## Tools and operator safety

Normal agents usually need only:

- `short_note`, `short_read`, and `short_search`
- `mid_read_all` and `mid_consolidate`
- `long_push`, `long_status`, and `long_query`

The full permission-aware surface is documented in
[MCP Tools Specification](docs/MCP_TOOLS_SPEC.md) and
[MCP Exposure](docs/TOOL_EXPOSURE.md). The following advanced operations keep
explicit authorization or confirmation gates:

| Tool | Guard |
| --- | --- |
| `bank_compact` | `manage`; dry-run by default; apply is DirectLocal-only |
| `bank_repair` | `manage`; dry-run by default |
| `mid_write` (`bank_write`) | `manage`; writes a bank file directly |
| `mid_delete` (`bank_delete`) | `manage`; requires `confirm=True` |
| `backup_restore` | `manage`; requires `confirm=True`; shared/unsafe recovery also requires `unsafe_recovery=True` |
| `backup_delete` | `manage`; requires `confirm=True`; compaction preimages are retained |
| `admin_purge_tokens` | `admin`; destructive modes require confirmation |
| `long_push` (`graph_push`) | `include_volatile=True` is opt-in and requires `manage` |
| `long_status` (`graph_status`) | `include_graph` is opt-in |

Compaction preimages use the existing `timestamp-<operation_id>` backup identity.
They remain available through backup listing, download, and restore, including
preimages created by 1.5.x. `backup_delete` refuses their deletion even when a
compaction was interrupted or its backup metadata is incomplete. This retention
protects the source for MID-to-LONG projection; it does not itself index it in LONG.
Every attempt retains a full-space snapshot, without automatic purge or deletion
through the backup tool. Stored volume and backup-listing work grow with the
number of captures; direct storage maintenance remains a separate operator action.

After a DirectLocal compaction captures verified preimages, a durable
pending record lets the background worker project those historical MID sources
into LONG. New captures use an automatically constructed ontology: the complete
first capture supplies the initial catalogue, then later captures reuse it
without recalculation. Documents keep their own ontology choice; both remain
searchable within the same space. No extra service or configuration is needed.
Each destination's durable local archive binding is separate from the connection
configuration. Reconnecting to the same parent keeps the initial capture and
catalogue, with archives readable immediately without waiting for the worker.
Earlier pending records retain their original destination; this does not migrate
or reclassify existing graphs. The worker resumes after restarts, checks document
identity before retrying,
and keeps the raw source even after successful indexing. A failed compaction
attempt can also leave a historical capture; its provenance does not claim that
MID was changed. `long_status.mid_archive_projection` reports pending captures,
the age of the oldest one, and a safe failure code. If the first catalogue is
blocked, later captures also wait with their raw sources retained: inspect that
status and correct repairable source or route faults before retrying. If local
state was lost or externally corrupted, an orphaned, unfrozen archive requires
restoring its original binding/checkpoint from a verified backup or operator
investigation; retries alone cannot repair it. The worker never skips an invalid
capture to bootstrap from another one. If archives are
unavailable, documentary query/search/list results remain available with
`partial: true` and warnings scoped to `mid_archive`; they are not a complete
archive result. LONG availability never decides MID apply or recovery.
Continuous ontology evolution and shared-space compaction remain separate work;
this wiring does not qualify large-volume ingestion.

**Recovering a paused MID archive (1.6.1).** Admission or a running job is not
successful indexing: failure accounting is reset only after stored-document
SHA verification or guarded operator resumption. Three `invalid_output` cycles
pause the capture. A latest `inference_timeout` also pauses it after at least
three unresolved failed capture cycles; those cycles need not all be timeouts.
Rate-limit and availability failures keep their capped backoff. A paused capture
retains its sources and admitted construction calls, including across restarts.

Inspect `long_status.mid_archive_projection`: `blocked` counts paused captures;
`error`, `failures`, `next_attempt_at` and `rejection_reason` explain pending
work. The CLI displays `PAUSED`; the Portal currently shows pending/error
information without an explicit pause/resume control. Aggregate status does not
identify the exact paused capture. Use the space's backup list to locate its
full preimage ID; that list includes ordinary backups too. A non-capture ID is
refused, and retrying a valid unpaused capture changes nothing.

```bash
uv run python scripts/mcp_cli.py graph status my-proj
uv run python scripts/mcp_cli.py backup list --space-id my-proj
uv run python scripts/mcp_cli.py bank archive-retry my-proj PREIMAGE_ID
```

Replace `PREIMAGE_ID` with the complete ID, including its space prefix. Resume
requires `manage` permission and rechecks the space, destination, DirectLocal
route and every retained source hash. It schedules the existing worker; it
neither compacts again nor immediately ingests, and `MID_AUTO_ARCHIVE=false`
still prevents execution. Correct the cause before resuming. Changes to an
unfinished construction's profile, input allowance or incompatible checkpoint
bindings remain refused; this command does not bypass them. Preserve the bank,
preimage, pending record and checkpoint before any separately authorized recovery.
Older binaries can refuse new error categories and do not preserve the new
timeout pause policy; rollback is not an automatic recovery strategy.

LONG archives are historical evidence. Results carry `preimage_id`, `bank_path`,
`captured_at`, source SHA and `ingested_at`. Capture/indexing dates do not prove
when a fact became valid or whether it is still current. Consumers answering
questions about the current state should read the current MID file and recent
SHORT notes alongside dated historical sources; LONG does not resolve temporal
conflicts automatically. See the [archive tool contract](docs/MCP_TOOLS_SPEC.md).

In v1.5.0, `CONSOLIDATION_TRANSIENT_RETRIES=0..3` (default `3`) controls
normal consolidation retries for chat timeout, rate-limit and temporary
unavailability failures before batch writes, after 60, 120 and 300 seconds.
Logs show cause, wait and resumption; the job stays `running` in
`batch_retry_wait`. The shared budget permits four main-generation requests,
or five including the single model correction. This reduces manual restarts,
but each batch can incur extra paid calls and up to **2 h 38 min** of main-call
and retry-wait time at the default 1800-second timeout, before auxiliary work.
The same-space lock remains held; other jobs for that space wait, and manual
compaction or GC may refuse while it is busy. Other spaces have separate lanes.
See the [MCP tool reference](docs/MCP_TOOLS_SPEC.md) for the complete budget.

Normal SHORT→MID generation on an `openai-compatible` chat profile uses a JSON
Schema for both the initial plan and its single correction. The endpoint must
support schema-constrained output; a refusal never triggers a silent downgrade.
The failed batch's notes remain available. Schema conformance describes the
output's structure, not the accuracy of its contents: the result still passes
all strict parsing and pre-write checks. Other profile identities, text merging
and compaction retain their existing generation mode.

Each MID generation also carries a fresh opaque request identifier, so a retry,
correction or restarted job sends a different request body to a response cache.
This applies to consolidation, compaction and MID maintenance. A reasoning-only
response remains an error; cache diagnostics do not turn it into usable output.
See [inference profiles](docs/INFERENCE_PROVIDER_PROFILES.md#mid-generation-freshness-and-response-cache-diagnostics)
for the profile boundary, diagnostics and provider-specific limits.

A successful job accounts for every processed note as integrated or explicitly
discarded with a reason; unprocessed notes remain available. This verifies
disposition and persisted bytes, not the completeness or accuracy of the
model's summary. Review important bank changes and keep authoritative source
records separately. A storage failure can leave earlier writes in the same
batch applied: normal consolidation has no batch-wide rollback, and retains
the failed batch's source notes for diagnosis.

The [`.env.example`](.env.example) selects the recipe profile
`Qwen/Qwen3.8-27B-FP8`, `LLMAAS_EFFORT=low`, context `500000`, output `200000`,
temperature `0.6` and batches of `2`. These explicit values differ from some
internal defaults (including batch size `3`); upgrading does not overwrite an
existing `.env`. Keep one complete inference configuration family. Split
profiles use `INFERENCE_CHAT_EFFORT` with `openai` or `openai-compatible`.
`COMPACT_THRESHOLD` and `CONSOLIDATION_LEGACY_FRENCH_PROMPTS` are removed; stale
values are ignored.

Context and output allowances must match the actual provider/model. Recipe
values are not universal model capabilities. Leave enough generation headroom:
splitting an input cannot make a growing catalogue fit an undersized output
allowance. Qualify a representative copy before changing an existing profile.

**Language change for existing spaces:** newly generated bank prose and the
residual synthesis are now requested in English, even when your notes, rules
or existing bank are French. Required headings, exact terms, identifiers,
URLs and quotations are preserved; untouched content is not translated just
to change its language. An existing French bank can therefore become bilingual
as it is updated. The removed French-prompt setting cannot restore French
generation. Back up and review a representative copy before upgrading a
language-sensitive workflow.

For 1.6.0, automatic compaction and MID archive transfer are enabled by default.
After a queued consolidation successfully processes notes, the server calls the
existing compactor once, under the same space lock, for files exceeding
`BANK_FILE_MAX_SIZE`. `MID_AUTO_COMPACT=false` disables this follow-up;
`MID_AUTO_ARCHIVE=false` pauses LONG transfer while retaining raw captures and
pending work for resumption. These are independent server settings loaded at
startup. Manual `bank_compact` remains available with `manage` permission.
Shared/unsafe routes remain ineligible. Direct MID edits and zero-note jobs do
not trigger compaction. The consolidation itself still requires a caller to
queue `mid_consolidate`; pending notes do not automatically start it.
`CONSOLIDATION_MAX_NOTES` bounds notes selected by one job (default `200`),
while `CONSOLIDATION_BATCH_SIZE` bounds each LLM request. Remaining notes stay
in SHORT for a later job; automatic bounded-cycle orchestration is not delivered.

`long_status.mid_automation` exposes both settings; `/admin` and
`scripts/mcp_cli.py` show them alongside pending captures and indexing errors.
Consolidation jobs carry a separate timestamped `auto_compaction` outcome:
completed consolidation is not undone by failed maintenance. This job history
is process-local. Zero pending captures does not prove that every current MID
file is indexed. The displayed documentary graph is separate from MID archives.
A compacted file may remain above the threshold and be compacted again after a
later consolidation. Each attempt can retain a full-space backup and a distinct
LONG capture; this version adds neither cross-capture deduplication nor retention.

Compaction refines medium-term memory so a new chat can understand the situation and resume
useful work. It deliberately summarizes secondary detail. The program separates
recent and undated passages from older dated history; the model extracts useful
historical lessons, then writes a concise Markdown handoff. Dates guide reading
priority and do not prove which statement is correct. The recent-input boundary
keeps passages of the same date together, even above the advisory byte marker.

`BANK_FILE_MAX_SIZE` remains a positive per-file threshold in persisted UTF-8
bytes. It also guides the recent-input and output reservations; it imposes no
summary length or retention ratio. A result must be non-empty and strictly
smaller, but may remain above the threshold. Original H1 headings and file paths
are owned by the code. One corrective generation per file is allowed for unusable
model responses, shared across stages; timeouts and other provider failures
remain terminal. Generated summary bodies are checked against the normal
editor's Markdown grammar to support subsequent consolidation edits.

Upgrading from 1.5.0 requires no new environment variable or storage migration.
Compaction can now produce a summary smaller than 5% of its source: it keeps
useful working context rather than a minimum quantity of text. Review a
representative copy before applying it to an important bank.

Apply is DirectLocal-only: all candidates are prepared before the existing
verified backup, writes, readback and bounded rollback. Shared Project Mesh
routes refuse before inference or mutation. Context-incompatible requests fail
before that request is sent. Compaction has no multipart persistence or
crash-durable resume. In 1.6.0, verified pre-compaction captures are retained
and projected asynchronously into LONG using an automatic ontology. See the
[MCP tool specification](docs/MCP_TOOLS_SPEC.md) for the complete contract.

## Security and boundaries

The [1.5.3 security report](docs/SECURITY_AUDIT_1.5.3.md) documents the seven
application corrections inherited by 1.6.0, dependency maintenance, and the
audit's scope and limitations. Broader audit follow-up remains open; this is not
a security certification.

- Every MCP request requires a bearer token.
- Tokens are stored as SHA-256 hashes; plaintext is shown only at creation.
- Permissions are `read`, `write`, `manage`, and `admin`.
- Agent identity comes from its token; use one token per agent.
- The WAF provides TLS termination, OWASP CRS filtering, and rate limiting.
- Hivemind OSS is strictly mono-tenant. A token's `space_ids` list is an access
  allowlist, not a tenant boundary. See [extension points](docs/EXTENSION_POINTS.md).

Read [the security model](docs/SECURITY.md) before exposing Hivemind outside a
trusted network.

## What Hivemind does not claim

<!-- non-claims -->
This release does not claim:

- quorum consensus; Project Mesh V1 uses full-mesh all-ACK;
- a hub topology, permanent master, or leader runtime;
- offline-first CRDT behavior;
- multi-space merge or merging two already-populated spaces;
- parallel consolidation for one shared space;
- multi-tenant isolation in the OSS edition.

`long` memory is connected knowledge for discovery. It is not the authority for
commits, audit, rollback, membership, backups, or recovery. Shared-space restore
is an explicit operator recovery flow, not an everyday sync mechanism.
<!-- /non-claims -->

These limits keep the public contract precise. The canonical detail lives in
[Positioning](docs/POSITIONING.md) and
[Architecture Contracts](docs/ARCHITECTURE_CONTRACTS.md).

## Upgrade from Live Memory or Graph Memory

Historical MCP names remain callable, but separate services move into one
Hivemind deployment and one `space_id` model. Follow the
[migration guide](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md) space by space.

## Develop and contribute

```bash
uv sync --locked --dev
uv run pytest tests/test_hivemind_state.py tests/test_hivemind_peer.py
uv run pytest tests
python scripts/check_doc_links.py
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request.
The public architecture summary is in
[Architecture Contracts](docs/ARCHITECTURE_CONTRACTS.md).

## Documentation

| Need | Start here |
| --- | --- |
| Install and operate | [Deployment](docs/DEPLOYMENT.md) |
| Configure inference | [Provider profiles](docs/INFERENCE_PROVIDER_PROFILES.md) |
| Connect an agent | [Agent memory setup](docs/AGENT_MEMORY_SETUP.md) |
| Browse tools and permissions | [MCP tool spec](docs/MCP_TOOLS_SPEC.md) · [exposure inventory](docs/TOOL_EXPOSURE.md) |
| Understand Project Mesh | [Project Mesh](docs/PROJECT_MESH.md) |
| Understand architecture | [Architecture contracts](docs/ARCHITECTURE_CONTRACTS.md) |
| Secure or recover a deployment | [Security](docs/SECURITY.md) · [migration and recovery](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md) |
| Troubleshoot | [FAQ](FAQ.md) · [Support](SUPPORT.md) |

## License

Apache License 2.0. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Hivemind and its original memory engines were created by **Christophe Lesur**.
Public releases must retain that code authorship; third-party components
retain their own attribution and licenses in the notices above.

---

*The open memory layer for collective agent awareness.* `short · mid · long`.
