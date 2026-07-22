<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/hivemind-mark-dark.svg">
  <img alt="Hivemind" src="assets/brand/hivemind-mark.svg" width="92" height="92">
</picture>

# hivemind

***The open memory layer for collective agent awareness.***

Vendor-neutral, open-source MCP service for three-tier agent memory:
`short` · `mid` · `long`.

Agents from any MCP-capable runtime notice what others are doing, inherit what
others have learned, and understand complex projects together.

[![protocol](https://img.shields.io/badge/protocol-MCP-00A7C7?style=flat-square)](#-concept)
[![version](https://img.shields.io/badge/version-1.3.0-9CA3AF?style=flat-square)](#-license)
[![CI](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml/badge.svg)](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-111827?style=flat-square)](#-license)
[![python](https://img.shields.io/badge/python-3.11+-F59E0B?style=flat-square)](#-prerequisites)

Français · [README.fr.md](README.fr.md)

</div>

---

## 📋 Table of Contents

- [Concept](#-concept)
- [Project Mesh](#project-mesh)
- [What Hivemind does NOT claim (V1)](#-what-hivemind-does-not-claim-v1)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Getting Started](#-getting-started)
- [MCP Tools](#-mcp-tools)
- [Long Tier — Ontology / Knowledge Graph](#-long-tier--ontology--knowledge-graph)
- [Web Interface](#-web-interface)
- [MCP Integration](#-mcp-integration)
- [CLI and Shell](#-cli-and-shell)
- [Tests](#-tests)
- [Security](#-security)
- [Project Structure](#-project-structure)
- [Troubleshooting](#-troubleshooting)
- [Contributing](#-contributing)
- [Documentation map](#-documentation-map)

---

## 🎯 Concept

**Hivemind** is the open-source MCP (Model Context Protocol) service that gives
AI agents a **vendor-neutral shared memory across three horizons** — `short`,
`mid`, and `long` — plus **Project Mesh**, its project-level synchronization
feature: several agents and teams can share one logical project memory space,
and administrators can opt into the implemented Mesh Sync V1 federation flow
through the capability-gated admin console — see [Project Mesh](#project-mesh)
for its exact full-mesh all-ACK boundary.

Hivemind turns isolated agent context into a shared space where agents
**notice** what others are doing, **inherit** what others have learned, and
**understand** complex projects together.

Markdown memory files are a good primitive, but by themselves they become
islands: one agent writes a `.md` bank, another agent or vendor starts from a
different context, and the project owner becomes the integration layer.
Hivemind makes the **space** the owner of memory instead of any one assistant,
IDE, model provider, or prompt history. If you move from one MCP-capable agent
to another, the accumulated project context remains in your Hivemind storage
and governance boundary.

### Three memory horizons

| Horizon | Was | What it holds | Aha moment |
| --- | --- | --- | --- |
| **`short`** | `live_*` | Append-only live notes: observations, decisions, todos — immediate working context, visible across the space. | **Notice** — an agent changes course because it sees another agent's current work. |
| **`mid`** | `bank_*` | The consolidated Markdown memory bank: rules, synthesis, project context. The structured working memory other agents inherit. | **Inherit** — an agent recovers a rule or method left by another agent, no manual prompt. |
| **`long`** | `graph_*` | The ontology / knowledge-graph tier: derived associative recall, conceptual links built by the collective process. | **Understand** — an agent retrieves logical links across the collective knowledge. |

The historical `live_*` / `bank_*` / `graph_*` tool names map one-to-one onto
`short_*` / `mid_*` / `long_*` and **remain callable** as compatibility aliases.
See [Compatibility aliases](#compatibility-aliases).

<a id="project-mesh"></a>

### Project Mesh

Beyond a single server, **Project Mesh** is Hivemind's project-level
synchronization feature. In V1 it ships in two clearly separated stages:

- **Available today — agent-level sharing.** Multiple teams, open-source
  contributors, and agent fleets connect their own MCP-capable runtimes to one
  unified `space_id` on a Hivemind deployment — one space owns its `short`
  notes, `mid` bank, `long` projection, and Project Mesh coordination state.
- **Available as opt-in instance federation.** Two administrators pair a blank
  target in three actions: create one opaque, one-time invitation (valid for
  **3,600 seconds**), paste and accept it on the target, then verify and
  approve it at the source. The signed peer exchange performs the pending
  membership transition, bounded bootstrap import, final ACK, and activation;
  post-mutation faults remain in explicit recovery rather than rolling back.
  This V1 pairing flow provisions exactly a **two-node mesh** from a source
  whose space has one active member. It refuses a source that already has more
  than one active member; adding a third node through this workflow is not
  supported in V1. After bootstrap, the two peers operate symmetrically.
  Operators use the capability-gated `/admin` `#/mesh` routes. Mesh remains an
  admin/peer HTTP surface: the 24-tool maximum agent discovery and the complete
  registered MCP surface expose **no `mesh_*` MCP tool**.

The use case is software-development acceleration: several contributors can
work in parallel with their own agents while shared memory, provenance, and
mutation ordering stay inside the project boundary. See
[`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md) for the
canonical vocabulary.

> Project Mesh's **Mesh Sync V1** protocol is conservative by design:
> **full-mesh all-ACK, not quorum.** See [What Hivemind does NOT claim (V1)](#-what-hivemind-does-not-claim-v1)
> for the exact boundary between what runs today and what is later work.

### Vendor-neutral ownership

Hivemind's multi-agent promise is also a multi-vendor promise:

| Without Hivemind | With Hivemind |
| --- | --- |
| Markdown memories live as isolated files beside each agent or tool. | `short`, `mid`, and `long` memory live in one governed Hivemind space. |
| Switching agents often means re-prompting, copying context, or trusting vendor history. | Any MCP-capable agent can read and write through scoped tokens. |
| Project knowledge drifts into vendor-specific sessions. | Memory persists in operator-controlled storage, protecting continuity, ownership, and project IP. |

### Why three horizons?

One level is never enough:

- `short` alone is **ephemeral** — it scrolls away as the project moves.
- `long` alone is **too heavy** for quick daily notes.
- `mid` is the structured bridge: agents **write fast** (`short`),
  **consolidate** into a durable bank (`mid`), and **capitalize** knowledge into
  an ontology-backed graph (`long`).

This shared-memory architecture follows the multi-agent framework in
[Tran et al., 2025 — *Multi-Agent Collaboration Mechanisms: A Survey of LLMs*](https://arxiv.org/abs/2501.06322),
which identifies a **shared environment** and **shared memory** as fundamental
components for LLM agents to coordinate, rather than operate as isolated
algorithms.

---

## 🚫 What Hivemind does NOT claim (V1)

Hivemind is positioned honestly. The following are **not** current behavior. A
later phase may revisit each, but until then they are not implemented and must
not be assumed. This section is the public mirror of
[`docs/POSITIONING.md`](docs/POSITIONING.md), the canonical
non-claims guardrail, and is fenced with HTML-comment sentinels so an
automated release-doc lint can detect it deterministically.

<!-- non-claims -->
Hivemind V1 does NOT claim:

- **quorum consensus** — Project Mesh V1 / Mesh Sync V1 is full-mesh all-ACK,
  not a quorum runtime.
- **hub topology** — there is no central hub; all peers are equivalent under
  Mesh Sync V1.
- **permanent master / leader runtime** — no node holds permanent leadership,
  and there is no leader-election path.
- **offline-first CRDT merge** — Hivemind V1 is not a CRDT system and does not
  attempt offline-first conflict-free merge.
- **merging two already-populated spaces** — V1 does not merge two spaces that
  each already carry state; there is no two-populated-space reconciliation
  path.
- **parallel collective consolidation** — `mid`-tier consolidation is
  serialized per space; there is no parallel collective consolidation across
  agents.
- **multi-tenant behavior** — the per-token `space_ids` allowlist is the
  **only** isolation primitive. There is no tenant object, no row-level
  security, and no per-tenant bucket isolation in the open-source edition.
  `space_ids` is an allowlist, **not** tenancy; for tenancy, see the
  [downstream extension seams](docs/EXTENSION_POINTS.md) (ADR-0003).

Additionally:

- **`long` memory is never authoritative.** The `long` ontology /
  knowledge-graph tier is a **derived projection only**. It is never the source
  of commit validity, rollback, audit, tombstones, watermarks, or recovery, and
  no `long` state sits in the commit path (ADR-0010).
- **`backup_restore` over a shared Project Mesh space is refused by default,
  forward-forcing only with explicit operator confirmation.** Restoring **over**
  a shared / unsafe / corrupted Hivemind space (read-only detection via
  `hive_status_label`, ADR-0008) is **refused by default**; corrupted critical
  state, a missing local `NodeIdentity` (orphan node), or a backup whose
  `bank_version` is strictly greater than the live pointer are all refused
  **fail-closed with zero mutation**. With operator-confirmed
  `unsafe_recovery=True`, the restore runs the field-by-field forward-forcing
  choreography of **ADR-0014 (Accepted)**: it stages the
  backup bank via `CommitRuntime`, forces `membership_epoch` and `term`
  strictly forward to `max(live, backup)+1`, unions live and backup
  tombstones, drops the pending queue, purges `acks/`, prunes `watermarks/`
  to the post-bump `MembershipView`, publishes a forward `BankCommit` through
  `assert_commit_allowed()` (single authorisation point, ADR-0011) at
  `pointer+1`, emits `UNSAFE_RECOVERY_RESTORED` + `RESYNC_REQUIRED` audit
  events under `{space}/_hivemind/events/`, and marks the node
  `HiveNodeStatus.RESYNC_REQUIRED` until re-bootstrap. See the public
  [migration and recovery guide](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md#6-shared-space-restore-caveat)
  (ADR-0014). For the
  single-instance, non-shared (`local_only` / `not_a_space`) case
  `backup_restore` is unchanged — byte-for-byte passthrough.
<!-- /non-claims -->

"Collective awareness" and "collective consciousness" are positioning language,
not literal claims — Hivemind makes no claim of AGI, sentience, or consciousness.

---

## 🏗️ Architecture

```
     Agent Cline        Agent Claude        Agent X
          │                   │                │
          └────────┬──────────┘                │
                   │                           │
                   ▼  MCP Protocol (Streamable HTTP)  ▼
          ┌────────────────────────────────────────┐
          │   WAF (Caddy + Coraza CRS)             │
          │   Rate Limiting • TLS • OWASP CRS      │
          └────────────┬───────────────────────────┘
                       │
          ┌────────────┴───────────────────┐
          │   Hivemind MCP service         │
          │   short · mid · long           │
          │   Project Mesh sync state      │
          │   Auth Bearer • consolidation  │
          └──────┬──────────┬──────┬───────┘
                 │          │      │
          ┌──────┴──┐  ┌────┴───┐  │
          │   S3    │  │  LLM   │  │  MCP Streamable HTTP
          │ durable │  │ (mid   │  │  (internal long-engine binding)
          │  store  │  │ consol)│  │
          └─────────┘  └────────┘  │
                       ┌───────────┴────────────┐
                       │  long-tier engine      │
                       │  ontology / knowledge  │
                       │  graph (derived only)  │
                       └────────────────────────┘
```

**Protocol stack**: S3 + LLM for authoritative short/mid and Project Mesh state.
**Complete Hivemind product**: includes the mandatory `long` ontology /
knowledge-graph engine bound internally to the space. It is a **derived
projection**, outside the commit path — see
[Long Tier](#-long-tier--ontology--knowledge-graph).

> The WAF profile and the embedded `long` runtime ship in the default compose
> stack ([docker-compose.yml](docker-compose.yml)); the S3 backend and the LLM
> provider are operator-supplied via `.env`. Concrete endpoints shown in
> examples are examples, not defaults.

---

## 📦 Prerequisites

- **Docker** >= 24.0 + **Docker Compose** >= 2.17.0 (`up --wait` is used)
- **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/) (for local CLI/tests)
- A compatible **S3 storage** (Dell ECS, AWS, MinIO)
- An OpenAI-API-compatible **LLM** (for `mid` consolidation and `long`
  extraction, embeddings, and semantic queries). The provider must expose
  **both** `/chat/completions` **and** `/embeddings` on the **same**
  `LLMAAS_API_URL` + `LLMAAS_API_KEY` — Hivemind uses one endpoint and key for
  both. Chat-only gateways are therefore **not** sufficient on their own:
  notably **Anthropic** and **OpenRouter** provide chat completions but no
  `/embeddings` endpoint, so `long` (and any semantic query) fails. Use a
  provider that serves both (e.g. OpenAI, a self-hosted vLLM/Ollama), or put a
  proxy (e.g. LiteLLM) in front that routes chat and embeddings to different
  backends behind a single endpoint. See
  [Local evaluation with Ollama](#-local-evaluation-with-ollama-no-api-key) for
  a zero-cost, no-key path.
- No separate graph backend or graph token: Graph Memory + Neo4j + Qdrant are
  **embedded in the default compose stack** (ADR-0019). The embedded runtime
  still uses the configured LLM API for long ingestion and queries.

---

## 🚀 Installation

> The default `docker compose up -d` brings up the **complete** Hivemind
> product: WAF, the Hivemind MCP service, and the embedded `long` runtime
> (Graph Memory + Neo4j + Qdrant) on the internal Docker network (ADR-0019).
> A networkless one-shot initializer prepares the local secret volume, then
> Hivemind durably creates/registers its scoped internal credential before
> readiness; persistence or revocation failures stop startup.
> Each product space binds to the embedded `long` engine automatically on its
> first long write (`long_push`) — there is no separate backend to provision
> and no manual bind step.

> Migrating from separate Live Memory + Graph Memory services to a single
> Hivemind deployment? Follow the English
> [space-by-space migration playbook](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
> (FR: [migration guide](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md)). For
> new or migrated agents, use the vendor-neutral
> [unified agent memory setup](docs/AGENT_MEMORY_SETUP.md), including the
> requirement for one new Hivemind token per agent identity.

### 1. Clone the repository

```bash
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind
```

### 2. Create a local development environment

```bash
python scripts/configure_dev_env.py
```

The helper creates `.env` with mode `0600`, random bootstrap/MinIO/Neo4j
credentials, `sigv4`, and Mesh disabled for a deliberate single-node local
evaluation. It refuses to overwrite an existing file and never prints the
generated secrets. Before testing `mid` or `long`, configure
`LLMAAS_API_URL`, `LLMAAS_API_KEY`, `LLMAAS_MODEL`,
`LLMAAS_EMBEDDING_MODEL`, and the embedding model's exact
`LLMAAS_EMBEDDING_DIMENSIONS`. The provider must expose compatible
`/chat/completions` and `/embeddings` endpoints; the model names shipped in the
template are examples, not portable defaults. Production operators must instead copy
`.env.example`, provide their own S3 and secrets, and configure a complete Mesh
identity when enabling Mesh.

### 3a. Docker Start (recommended)

```bash
# Build images, including local MinIO from the dev profile
docker compose --profile dev build

# Start the full default stack
# (WAF + secret initializer + Hivemind + embedded Graph Memory + Neo4j + Qdrant)
docker compose --profile dev up -d --wait

# Check status
docker compose ps

# Health check
curl -s http://localhost:8080/health
```

### 3b. Local Start (development)

Direct host starts do not run the Compose volume initializer. Set a stable,
non-empty `LONG_EMBEDDED_TOKEN` in `.env`, especially on macOS, or configure a
Linux-local secret path that satisfies the `0700`/`0600` contract documented in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#embedded-credential-lifecycle-and-repair).

```bash
# Create the project environment and install locked development dependencies
uv sync --locked --dev

# Run server
uv run python -m live_mem
```

> **Note:** the source package is still `live_mem` (the imported engine). The
> public `short`/`mid`/`long` MCP grammar is an additive naming layer over the
> same code; renaming the Python package is later work, not a behavior
> change.

### 4. Use the bundled CLI

```bash
uv run python scripts/mcp_cli.py --help
```

### 5. `long` tier — embedded and auto-bound

The mandatory `long` ontology / knowledge-graph engine is already running:
the default compose stack (step 3a) starts it alongside its Neo4j and Qdrant
datastores, internal-network only (ADR-0019). Each product space binds to it
automatically on its first long write (`long_push` / `graph_push`), deriving
a deterministic `memory_id` from the `space_id` — **no manual bind step**.

`graph_connect` / `long_connect` remains callable as an **advanced override /
diagnostic only** (e.g. temporarily pointing a space at a legacy external
Graph Memory during a migration, or selecting a non-default ontology). It is
never a required installation step.

### 6. Verify Installation

```bash
# Health check via CLI
uv run python scripts/mcp_cli.py health

# Or full E2E test (creates space, writes notes, consolidates)
uv run python scripts/test_recette.py

# Long-tier readiness: after a space's first long_push, graph_status /
# long_status reports the embedded runtime as connected (auto-bind, no
# manual step).
```

### Exposed Ports

| Service    | Port   | Description                                   |
| ---------- | ------ | --------------------------------------------- |
| **WAF**    | `8080` | Only exposed port — Caddy WAF → Hivemind MCP  |
| MCP Server | `8002` | Internal Docker network only                  |

---

## ⚙️ Configuration

Edit `.env`. All variables are documented in `.env.example`.

### Mandatory Variables

| Variable               | Description              | Example                                      |
| ---------------------- | ------------------------ | -------------------------------------------- |
| `S3_ENDPOINT_URL`      | S3 endpoint URL          | `https://s3.example.com`                     |
| `S3_ACCESS_KEY_ID`     | S3 access key            | `AKIA...`                                    |
| `S3_SECRET_ACCESS_KEY` | S3 secret key            | `wJal...`                                    |
| `S3_BUCKET_NAME`       | Bucket name              | `hivemind`                                   |
| `S3_REGION_NAME`       | S3 region                | `eu-west-1`                                  |
| `LLMAAS_API_URL`       | LLM API URL (must include `/v1`)  | `https://api.example.com/v1`         |
| `LLMAAS_API_KEY`       | LLM API key                       | `sk-...`                             |
| `LLMAAS_MODEL`         | Exact chat model id accepted by `/chat/completions` | `provider-chat-model` |
| `LLMAAS_EMBEDDING_MODEL` | Exact embedding model id accepted by `/embeddings` | `provider-embedding-model` |
| `LLMAAS_EMBEDDING_DIMENSIONS` | Exact vector length returned by the embedding model | `1024` |
| `ADMIN_BOOTSTRAP_KEY`  | Admin bootstrap key (≥ 32 random chars) | generated by `configure_dev_env.py` |

> The `LLMAAS_*` environment-variable prefix is inherited from the upstream
> LLM-as-a-Service integration and is kept as-is in the public release — the
> names in these tables are the ones the service actually reads.
> The template's `qwen3.5:27b`, `bge-m3:567m`, and `1024` values describe one
> provider profile only. Replace the model ids and dimension together. A wrong
> dimension breaks long writes/search; changing it after ingestion requires a
> reviewed Qdrant collection rebuild and re-ingestion.

### 🦙 Local evaluation with Ollama (no API key)

For a fully local evaluation with no external provider, API key, or billing,
point Hivemind at [Ollama](https://ollama.com/). Ollama serves an
OpenAI-compatible API with **both** chat and embedding models, which satisfies
the single-endpoint requirement above.

```bash
# 1. Install and start Ollama, then pull a chat model and an embedding model
ollama pull llama3.2:3b
ollama pull nomic-embed-text

# 2. Point the LLMAAS_* variables in .env at Ollama.
#    From inside the compose containers, reach the host via host.docker.internal.
#    The API key is required but ignored by Ollama — any non-empty string works.
LLMAAS_API_URL=http://host.docker.internal:11434/v1
LLMAAS_API_KEY=ollama
LLMAAS_MODEL=llama3.2:3b
LLMAAS_EMBEDDING_MODEL=nomic-embed-text
LLMAAS_EMBEDDING_DIMENSIONS=768   # nomic-embed-text returns 768-dim vectors
```

Then `docker compose --profile dev up -d` to apply. `LLMAAS_EMBEDDING_DIMENSIONS`
must match the embedding model exactly (768 for `nomic-embed-text`); a mismatch
breaks `long` writes and search. Small local models are lower quality than a
hosted provider for the structured extraction/consolidation `mid` and `long`
perform — fine for verifying the plumbing, but expect weaker results than
OpenAI-grade models.

### Embedded Long Runtime (mandatory, ADR-0019)

The `long` engine ships in the default compose stack and auto-binds per
space on the first long write — there are no operator-provided `url` /
`token` / `memory_id` values to configure. Its `.env` knobs are:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `NEO4J_PASSWORD` | _(required)_ | Password for the embedded Neo4j graph store — the stack refuses to start without it |
| `LONG_EMBEDDED_URL` | `http://graph-memory:8002` | Internal-network URL of the embedded long runtime |
| `LONG_EMBEDDED_TOKEN` | _(empty = startup-created)_ | Local-only scoped `read,write` token; when empty, Hivemind atomically persists and registers one before readiness, and fails closed instead of using volatile or revoked state |

The per-space `graph_connect` / `long_connect` override (url, token,
memory_id, ontology) is an advanced / diagnostic escape hatch only — e.g. a
legacy external Graph Memory during a migration — never a setup requirement.

### Optional Variables — LLM (`mid`-tier consolidation)

The `mid`-tier consolidator uses an LLM (OpenAI-compatible API) to transform
`short` live notes into the structured `mid` bank.

| Variable                  | Default           | Description                     |
| ------------------------- | ----------------- | ------------------------------- |
| `LLMAAS_MODEL`            | `qwen3.5:27b` | LLM model name as exposed by the provider |
| `LLMAAS_CONTEXT_WINDOW`   | `131072`          | TOTAL context window of the model (input + output combined, in tokens) |
| `LLMAAS_MAX_TOKENS`       | `16384`           | Max OUTPUT tokens per request. Must stay **strictly below** `LLMAAS_CONTEXT_WINDOW`, otherwise startup fails with a configuration error naming both values. The consolidator adjusts dynamically: `output = min(MAX_TOKENS, CONTEXT_WINDOW - input)` |
| `LLMAAS_TEMPERATURE`      | `0.3`             | LLM creativity (0.0 = deterministic, 1.0 = very creative) |
| `PROXY_URL`               | _(none)_          | Outbound HTTP proxy (e.g. `http://10.0.0.1:3128`). **Custom variable** (not `HTTP_PROXY`) — injected manually into boto3 (S3) and httpx (LLM consolidation calls **and** the lightweight LLM health probes of `/health` and `system_health`). Not supported for `long`-tier connections. |

### Optional Variables — Consolidation and Compaction

| Variable                  | Default           | Description                     |
| ------------------------- | ----------------- | ------------------------------- |
| `MCP_SERVER_PORT`         | `8002`            | MCP server listening port       |
| `MCP_SERVER_DEBUG`        | `false`           | Detailed logs (full error messages) |
| `CONSOLIDATION_TIMEOUT`   | `600`             | Timeout per LLM call (seconds)  |
| `CONSOLIDATION_MAX_NOTES` | `200`             | Max notes per consolidation     |
| `CONSOLIDATION_BATCH_SIZE`| `5`               | Notes per LLM batch (small = precise, large = faster) |
| `CONSOLIDATION_COOLDOWN_SECONDS` | `60`      | Per-space anti-spam cooldown for `bank_consolidate` (`0` disables) |
| `CONSOLIDATION_VALIDATION_ENABLED` | `false` | Optional post-consolidation check for unattributed claims |
| `CONSOLIDATION_VALIDATION_MAX_EXAMPLES` | `20` | Max examples returned by the validation pass |
| `COMPACT_THRESHOLD`       | `0.6`             | Auto-compaction trigger (0.6 = compact if bank > 60% of budget) |
| `BANK_FILE_MAX_SIZE`      | `15360`           | Max size per bank file (bytes, 15 KB). Above = compaction candidate |
| `RESPONSE_MAX_BYTES`      | `524288`          | Max non-MCP response body size before truncation |
| `API_TOOL_MAX_BODY_BYTES` | `1048576`         | Max request body accepted by `/api/tool` |
| `ADMIN_AUDIT_RING_SIZE`   | `500`             | Per-instance in-memory console/auth audit capacity; validated `1..500` at startup |

---

## ▶️ Getting Started

This is the copy-paste local path. It uses generated local secrets, MinIO, and
the bootstrap credential only for the initial evaluation. Before `mid`,
configure the provider base URL, key, exact chat model id, exact embedding
model id, and returned vector dimension in `.env`; it must expose compatible
`/chat/completions` and `/embeddings` endpoints. `short` does not need an LLM.
The embedded `long` runtime starts with the stack and auto-binds on its first
write, but long ingestion and semantic query use that provider too.

```bash
# 1. Clone the repository
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind

# 2. Create random local credentials and install the locked CLI environment
python scripts/configure_dev_env.py
uv sync --locked --dev

# 3. Bring up WAF + Hivemind + MinIO + embedded long runtime
docker compose --profile dev up --build -d --wait
docker compose ps
docker compose logs hivemind --tail 50

# 4. Point the CLI at the WAF and verify the generated bootstrap credential
export MCP_URL=http://localhost:8080
export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"
uv run python scripts/mcp_cli.py health --json
uv run python scripts/mcp_cli.py whoami --json

# 5. Create a demo space from the shipped standard rules
uv run python scripts/mcp_cli.py space create hivemind-demo \
  --description "Quickstart demo space" \
  --rules-file RULES/live-mem.standard.memory.bank.md

# 6. short — write and read a real note
uv run python scripts/mcp_cli.py live note hivemind-demo observation "hello short"
uv run python scripts/mcp_cli.py live read hivemind-demo

# 7. mid — requires the complete LLMAAS provider/model configuration in .env
# This returns immediately with status running|queued and a job_id.
uv run python scripts/mcp_cli.py bank consolidate hivemind-demo --json
```

Stop here after the acknowledgement. This operator quickstart explicitly asks
for deliberate, operator-timed status checks; routine agents must return
without polling. Paste the returned `job_id` only when you intentionally check:

```bash
JOB_ID="paste-returned-job-id"
uv run python scripts/mcp_cli.py bank consolidation-status "$JOB_ID" --json
```

Continue only when that response reports the terminal top-level state
`"status": "succeeded"`. If it still reports `running` or `queued`, stop and
check again only at a deliberately chosen later time—do not automate a loop. A
`failed`, `not_found`, or error response is a failed quickstart: diagnose it
before reading the bank or pushing long memory.

```bash
# 8. Verify the completed mid result
uv run python scripts/mcp_cli.py bank read-all hivemind-demo --json

# 9. long — also requires the configured embedding model/dimension
uv run python scripts/mcp_cli.py graph push hivemind-demo --json
uv run python scripts/mcp_cli.py graph status hivemind-demo --json
uv run python scripts/mcp_cli.py graph query hivemind-demo "hello" --json

# Stop exposing the bootstrap key to child processes after the evaluation.
unset MCP_TOKEN
```

For any persistent deployment, mint a dedicated manager and one dedicated
`read,write` token per agent, invite each agent token to the space, and stop
using the bootstrap credential. The exact procedure is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#quickstart-dev).

Historical `live_*` / `bank_*` / `graph_*` tool names remain callable as
compatibility aliases — see [Compatibility aliases](#compatibility-aliases).

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) and
[`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
for full operator details.

---

## 🔧 MCP Tools

Hivemind exposes the canonical set documented in
[tests/fixtures/tool_surface.json](tests/fixtures/tool_surface.json)
(**61 registered names = 48 direct registrations + 13 tier aliases**, tracked
by the tool-surface fixture) via the MCP protocol
(Streamable HTTP): historical tools + `short_*`/`mid_*`/`long_*` tier
aliases (both sets callable).
The `short`/`mid`/`long` tiers carry the public grammar; `space_*`, `token_*`,
`system_*`, `backup_*`, and `admin_*` are **cross-cutting** and keep their names
(no tier home). There are 35 direct no-alias tools; the 13 tier aliases are
unchanged. See the frozen mapping in
[`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)
for the canonical per-tool mapping and
[`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md)
for the full specification including deprecation expectations.

### System

| Tool            | Parameters | Description                                            |
| --------------- | ---------- | ------------------------------------------------------ |
| `system_health` | —          | Health status (S3, LLM, number of spaces)              |
| `system_whoami` | —          | Current token identity (name, permissions, spaces)     |
| `system_about`  | —          | Service identity (version, tools, capabilities)        |

### Space

| Tool                 | Parameters                                   | Description                                               |
| -------------------- | -------------------------------------------- | --------------------------------------------------------- |
| `space_create`       | `space_id`, `description`, `rules?`, `owner?` | **manage**: creates a space; `_meta.json` commits last and persisted manager is auto-granted |
| `space_update`       | `space_id`, `description?`, `owner?`         | Updates description and/or owner                          |
| `space_update_rules` | `space_id`, `rules`                          | Updates space rules (manage)                              |
| `space_list`         | —                                            | Lists spaces accessible by current token                  |
| `space_info`    | `space_id`                                   | Detailed info (notes, bank, consolidation)                |
| `space_rules`   | `space_id`                                   | Reads space rules                                         |
| `space_summary` | `space_id`                                   | Complete summary: rules + bank + stats (agent startup)    |
| `space_export`  | `space_id`                                   | tar.gz export in base64                                   |
| `space_delete`  | `space_id`, `confirm`, `unsafe_recovery?`    | Deletes the space (⚠️ irreversible, manage; advanced flag explicitly permits classifiable shared/unsafe Hivemind recovery, never corrupt state) |
| `space_invite_token` | `space_id`, `token_hash`                  | **manage + space access**: exact canonical hash, add-only/idempotent; not Project Mesh enrollment |

### Token

| Tool | Parameters | Description |
| --- | --- | --- |
| `token_create` | `name`, `permissions`, `expires_in_days?`, `email?` | **manage**: creates `read`, `read,write`, or `read,write,manage` token with `space_ids: []`; secret + full hash shown once |

`manage` is a transitive, high-trust provisioning role. Any manager can create
new spaces globally (even with an empty allowlist) and can create further
managers. The allowlist bounds access/invitations for existing spaces, not
`space_create`. Global token list/update/revoke/delete/purge remains admin-only.

Space identity reuse is fail-closed: any persisted scope reference for an
absent or partially prepared ID—including admin, the creating manager, revoked,
or expired tokens—blocks `space_create` until explicit admin cleanup. `space_delete`
removes/reprobes payload first and `_meta.json` last, but operators must quiesce
all same-space mutations and background jobs; its lifecycle lock is not a
universal writer barrier. A `partial` deletion is recovery-required, never
success.

### `short` — live notes (historical `live_*`)

| Tool          | Parameters                                  | Description                                                                                                                 |
| ------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `live_note`   | `space_id`, `category`, `content`, `tags?`  | Writes a timestamped note (agent = token name). Categories: `observation`, `decision`, `todo`, `insight`, `question`, `progress`, `issue` |
| `live_read`   | `space_id`, `limit?`, `category?`, `agent?` | Reads live notes (optional filters)                                                                                         |
| `live_search` | `space_id`, `query`, `limit?`               | Full-text search in notes                                                                                                   |

> **Token = Agent identity.** The agent name is derived from the auth token; the
> `short` note-category set is fixed. Both are preserved verbatim into the
> `short_*` grammar.

### `mid` — memory bank (historical `bank_*`)

| Tool               | Parameters                        | Description                                                                                             |
| ------------------ | --------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `bank_read`        | `space_id`, `filename`            | Reads a bank file (supports subfolders: `personaProfiles/buyer.md`)                                     |
| `bank_read_all`    | `space_id`                        | Reads entire bank in one request (agent startup)                                                        |
| `bank_list`        | `space_id`                        | Lists bank files with relative paths (without content)                                                  |
| `bank_consolidate` | `space_id`, `agent?`              | Enqueues async LLM consolidation. Omitted/null `agent` = caller; explicit `agent=""` = all agents (manage/admin). Call once; do not watch/poll unless explicitly requested |
| `bank_consolidation_status` | `job_id`              | Manual-only status check for a job returned by `bank_consolidate` |
| `bank_consolidation_queues` | `space_ids?`          | Read-only summary of consolidation lanes by space |
| `bank_stale_spaces` | `min_notes?=5`, `min_age_days?=5`, `space_ids?` | Lists spaces with ≥N unconsolidated notes whose oldest is ≥D days old (supervision) |
| `bank_compact`     | `space_id`, `dry_run?`            | Compacts oversized bank files via LLM. `dry_run=True` by default (**manage**)                           |
| `bank_repair`      | `space_id`, `dry_run?`            | Repairs corrupted filenames (Unicode, parasitic prefixes). `dry_run=True` by default (**manage**)       |
| `bank_write`       | `space_id`, `filename`, `content` | Writes/replaces a bank file directly — bypasses LLM consolidation (**manage**)                          |
| `bank_delete`      | `space_id`, `filename`, `confirm?=False` | Deletes a bank file and Unicode duplicates (**manage**, irreversible); `confirm=True` is required |

> The five ops/supervision tools (`bank_consolidation_status`,
> `bank_consolidation_queues`, `bank_stale_spaces`, `bank_repair`,
> `bank_compact`) **keep their historical names** — they are internal/ops, not
> public `mid_*` CRUD verbs. See `TOOL_MAPPING.md`.

### `long` — ontology / knowledge graph (historical `graph_*`)

| Tool               | Parameters                                           | Description                                                                                               |
| ------------------ | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `graph_connect`    | `space_id`, `url`, `token`, `memory_id`, `ontology?` | Attaches the `long`-tier engine to a space. Tests connection, creates the knowledge memory if needed. Target alias `long_connect`. |
| `graph_push`       | `space_id`, `include_volatile?`                      | Ingests canonical `mid` content into the `long` knowledge graph. Delete + re-ingest, orphan cleanup. **Not a routine bidirectional channel** — see below. |
| `graph_status`     | `space_id`, `include_graph?`                         | Connection status + graph stats (documents, entities, relations, top entities); optionally includes graph detail |
| `graph_disconnect` | `space_id`, `use_embedded?`                          | Detaches the `long`-tier engine, or replaces a legacy override with the embedded/local runtime when `use_embedded=true` (`manage`). Graph data remains untouched. Target alias `long_disconnect`. |
| `long_ingest`      | `space_id`, `documents`, `mode?`, `include_volatile?` | Plans canonical-document ingestion; `apply` remains deferred in V1. Direct tool, no `graph_*` twin.       |
| `long_query`       | `space_id`, `query`, `limit?`                        | Read-only semantic query over the derived long engine. Direct tool, no `graph_*` twin.                    |

### Backup

| Tool              | Parameters                 | Description                              |
| ----------------- | -------------------------- | ---------------------------------------- |
| `backup_create`   | `space_id`, `description?` | Creates a full snapshot on S3            |
| `backup_list`     | `space_id?`                | Lists available backups                  |
| `backup_restore`  | `backup_id`, `confirm?=False`, `unsafe_recovery?=False` | **manage**; `confirm=True` is always required. Normally the space must not exist. Over a shared/unsafe Hivemind space, additionally pass `unsafe_recovery=True` for explicit forward-only recovery; corruption is still refused fail-closed. |
| `backup_download` | `backup_id`                | Download as tar.gz base64                |
| `backup_delete`   | `backup_id`, `confirm?=False` | **manage**; irreversibly deletes a backup only with `confirm=True` |

### Admin

| Tool                 | Parameters                                                        | Description                                                                                                  |
| -------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `admin_audit_recent` | `limit?=50`                                                       | Admin-only, newest-first console/auth events from the per-instance in-memory ring (`1..500`); metadata and argument keys only, never values or space IDs |
| `admin_create_token` | `name`, `permissions`, `space_ids?`, `expires_in_days?`, `email?` | Admin/bootstrap global creation; initial scopes apply only to non-admin targets, admin targets persist `[]`; plaintext + full hash shown together once |
| `admin_list_tokens`  | —                                                                 | Lists active tokens                                                                                          |
| `admin_revoke_token` | `token_hash`                                                      | Revokes a token (makes it unusable)                                                                          |
| `admin_delete_token` | `token_hash`                                                      | Physically deletes a token from the registry (⚠️ irreversible)                                             |
| `admin_purge_tokens` | `revoked_only?`, `confirm?=False`                                 | Bulk purge: revoked only by default; purging all tokens requires explicit confirmation                       |
| `admin_update_token` | `token_hash`, `permissions?`, `email?`, `space_ids?` or `space_ids_add?` / `space_ids_remove?` | Single-token update; replacement and delta scope modes are exclusive; promotion clears scopes and downgrade starts empty |
| `admin_bulk_update_tokens` | `names?`, `name_contains?`, `has_space?`, `permissions?`, `email?`, `space_ids_add?`, `space_ids_remove?`, `include_revoked?` | Filtered bulk update; scope changes are add/remove deltas only                                                 |
| `admin_gc_notes`     | `space_id?`, `max_age_days?`, `confirm?`, `delete_only?`, `expected_eligible_set_token?` | Garbage Collector: dry-run, consolidate, or conditionally delete orphaned notes                              |

GC writes are routed fail-closed: every candidate space must resolve to
`DIRECT_LOCAL` before any notice, consolidation, or deletion. Deletion is an
explicit two-step operation: first run a dry run and retain its opaque
`eligible_set_token`, then send that value as `expected_eligible_set_token`
with `confirm=true, delete_only=true`. A changed exact key set is refused with
zero deletion; partial operations return honest processed/deleted/failed
counts instead of reporting complete success.

### Compatibility aliases

The canonical `short_*` / `mid_*` / `long_*` tier aliases (exact set tracked
by [`tests/fixtures/tool_surface.json`](tests/fixtures/tool_surface.json) and
documented per-tool in
[`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)) are
**registered and callable** — a thin re-registration
of the *identical* function, never a divergent copy. The historical
`live_*` / `bank_*` / `graph_*`
names are **compatibility aliases supported indefinitely**: no removal date, no
timer. Callers using the historical names continue to work without change.

New integrations should prefer `short_*` / `mid_*` / `long_*` — they are the
recommended grammar going forward. Any future removal is **ADR-gated** (ADR-0005):
a Stage B soft-deprecation lasting at least one published release must precede any
Stage C removal, and no removal is ever date- or release-number-triggered
automatically. For the full policy see
[`docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations`](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations).

- Policy (ADR-0005): [`docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations`](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations)
- Per-tool mapping: [`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)
- Grammar & tier semantics (ADR-0002): [`docs/TOOL_MAPPING.md#invariants`](docs/TOOL_MAPPING.md#invariants)

---

## 🌐 Long Tier — Ontology / Knowledge Graph

The `long` tier is an **ontology / knowledge-graph engine** bound internally to
a space. It extracts typed entities and relations from canonical content and
stores them as a navigable knowledge graph for **associative recall** — the
"understand" moment.

> ### Authority boundary (non-negotiable)
>
> The `long` tier is a **derived semantic projection only**. It is **never** the
> source of commit validity, rollback, audit, tombstones, watermarks, or
> recovery, and it sits **outside the commit path**. `mid` (the consolidated
> bank) and the repository's canonical files remain the authority; the `long`
> graph localizes and associates, it does not confirm.
>
> This boundary is protocol-derived. See the
> [public tier and authority mapping](docs/TOOL_MAPPING.md#invariants)
> (ADR-0002 / ADR-0004).

### `graph_push` is ingestion, not a routine channel

`graph_push` is a **one-directional ingestion** of canonical `mid` content into
the `long` graph — not a routine bidirectional sync. Pushing the entire bank on
every cycle teaches the graph transient content that a later compaction strands
as stale. Routine flows should ingest **stable, canonical documents** with
stable `source_path` keys; volatile focus files (e.g. `activeContext.md`,
`progress.md`) must **never** end up in the `long` graph. `graph_push` remains
available for one-off bootstrap and explicit debug / migration.

### Workflow

```
1. bank_consolidate(space_id)
   └─ Builds the canonical mid bank (call once; do not poll unless asked)

2. graph_push(space_id)
   ├─ First push auto-binds the space to the embedded long runtime
   │  (derives memory_id from space_id, ontology "general" — no graph_connect)
   ├─ For each modified canonical file: delete + re-ingest (graph recalculation)
   ├─ Cleans deleted documents (orphan entities removed)
   └─ Updates ingestion metrics (last_push, push_count)

3. graph_status(space_id)
   └─ Stats: entities, relations, top entities, documents...
```

Advanced override / diagnostic only: `graph_connect(space_id, url, token,
memory_id, ontology?)` (canonical: `long_connect`) re-points a space at an
external engine or selects a non-default ontology — never a required step of
the routine workflow.

Each push is a **complete refresh** of the graph for that file: existing files
are deleted then re-ingested so the engine recalculates ontology-guided entities
and typed relations with up-to-date content.

### Available Ontologies

| Ontology            | Usage                                      |
| ------------------- | ------------------------------------------ |
| `general` (default) | Versatile: FAQ, specs, certifications      |
| `legal`             | Legal documents, contracts                 |
| `cloud`             | Cloud infrastructure, product sheets       |
| `managed-services`  | Managed services, outsourcing              |
| `presales`          | Pre-sales, RFP/RFI, proposals              |

---

## 🖥️ Web Interface

> **Note:** the `/live` real-time viewer described first is the **inherited**
> visualization surface bundled with the imported engine — it is documented
> here because it ships with the engine, and its future is a separate
> observability decision. The `/admin` operator console below was rebuilt
> to the Hivemind design language and **is** the target product surface.

Hivemind exposes a web interface on `/live` to visualize memory spaces in
real-time.

### Access

```
http://localhost:8080/live
```

### Features

| Zone                               | Content                                                                                                                       |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard** (left)               | Space info, consolidation (date + counters), short/mid stats, colored agents, categories with %, Markdown rules, long-tier status |
| **Live Timeline** (top-right)      | `short` notes grouped by date (Today/Yesterday/date), cards with agent + category + Markdown                                  |
| **Bank Viewer** (bottom-right)     | Consolidated `mid` file tabs, Markdown rendering                                                                              |

### Smart Auto-refresh

- Configurable: 3s / 5s / 10s / 30s / manual
- **Anti-flicker**: only re-renders DOM if data has changed
- Status dot with last refresh timestamp
- Space selection → immediate loading

### REST API (5 endpoints)

| Endpoint                        | Description                                              |
| ------------------------------- | -------------------------------------------------------- |
| `GET /api/spaces`               | List of spaces                                           |
| `GET /api/space/{id}`           | Complete info (meta + rules + stats + long-tier)         |
| `GET /api/live/{id}`            | `short` notes (filters: `?agent=`, `?category=`, `?limit=`) |
| `GET /api/bank/{id}`            | `mid` bank file list                                     |
| `GET /api/bank/{id}/{filename}` | `mid` bank file content                                  |

`/api/*` endpoints require a Bearer Token. The `/live` page and `/static/*`
files are public.

### Admin Console (`/admin`)

The hash-routed administration console is available at `/admin` and uses the
authenticated `/api/tool` proxy for its operator workflows:

```
http://localhost:8080/admin
```

| Section | Features |
| --- | --- |
| **Dashboard** | Health status (S3 / LLM / version / uptime), identity bar, spaces and token counts, consolidation queue/lane signals |
| **Spaces** | Spaces index with short/mid/long counts and state labels; the entry point into Space Detail (create shown only to manage/admin) |
| **Space Detail** | Unified per-space view across memory-tier selectors: `short` notes, `mid` bank files, `long` derived knowledge, rules, access summary, and per-space safe actions (create/delete backups, delete space) |
| **Consolidation** | Consolidation lanes/jobs (queued / running / succeeded / failed) and the stale-banks planning filter |
| **Audit** | Recent console/auth events from `admin_audit_recent` (admin only, this instance, in-memory since restart) |
| **Access** | Token and space-access management: create (manager-safe vs admin), invite by exact hash, update / revoke / delete / purge with typed confirmations |
| **Operator tools** | Backups (create / restore / delete) and Maintenance (compact, repair, GC, purge) behind explicit confirmations |

- **Auth**: requires a valid token (same as `/live`), session via HttpOnly cookie
- **CSP-safe**: zero inline handlers, all via `data-action` + event delegation
- **Long tier is derived, never authoritative**: never a commit, rollback,
  audit, membership, or recovery source — the Space Detail long panel renders
  its real state (or an honest failure), never a neutral "disabled".
- **Mono-tenant**: a token's `space_id` list is an allowlist, not a tenant
  boundary (Hivemind OSS is mono-tenant).
- **Honest audit scope**: the Audit view is best-effort, not persistent or
  complete; it records argument keys only (never values or space IDs), and MCP
  `/mcp` tool calls are not listed

---

## 🔌 MCP Integration

> 📖 **Full guides**: see the per-client integration guides for step-by-step
> configuration (server config, custom instructions, workflow, multi-agents,
> troubleshooting):
> [`CLAUDE_CODE_INTEGRATION.md`](CLAUDE_CODE_INTEGRATION.md),
> [`CODEX_INTEGRATION.md`](CODEX_INTEGRATION.md).

The compatibility surface contains **61 registered and directly callable
names** (48 direct registrations plus 13 historical/canonical aliases), but
regular `tools/list` discovery is intentionally compact and permission-aware:
**17 / 20 / 24 / 24** canonical agent tools for read / write / manage / admin.
Operator tools, historical aliases, and future Mesh HTTP operations are not
advertised there; exact-name calls still reach the same handler and its fresh
call-time authorization guard. Permission or space-scope changes return
`mcp_reconnect_required=true` when a client may need to refresh cached
discovery. See the generated [MCP exposure inventory](docs/TOOL_EXPOSURE.md).

### With Claude Desktop

Claude Desktop's remote connector UI currently expects supported OAuth and
does not accept Hivemind's static bearer header. Anthropic also does not
document safe environment-variable expansion for bearer headers in
`claude_desktop_config.json`. Until one of those contracts changes, use Claude
Code for bearer-authenticated access; do not copy a token into a repository.
See [the precise Desktop boundary](CLAUDE_CODE_INTEGRATION.md#-with-claude-desktop).

### Via Python (MCP client)

```python
import os

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def example():
    headers = {"Authorization": f"Bearer {os.environ['HIVEMIND_TOKEN']}"}
    async with streamablehttp_client("http://localhost:8080/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # Load all mid-tier context
            result = await session.call_tool("mid_read_all", {
                "space_id": "my-project"
            })

            # Write a short note
            await session.call_tool("short_note", {
                "space_id": "my-project",
                "category": "observation",
                "content": "Build passing in CI"
            })
```

---

## 💻 CLI and Shell

### CLI Installation

```bash
uv sync --locked --dev
export MCP_URL=http://localhost:8080
export MCP_TOKEN=your_token
```

### CLI Commands (Click)

```bash
uv run python scripts/mcp_cli.py health
uv run python scripts/mcp_cli.py whoami                       # Current token identity
uv run python scripts/mcp_cli.py about
uv run python scripts/mcp_cli.py space list
uv run python scripts/mcp_cli.py space create my-project \
  --description "My project" \
  --rules-file RULES/live-mem.standard.memory.bank.md
uv run python scripts/mcp_cli.py token create agent-cline -p read,write   # manage/admin caller; starts unscoped
uv run python scripts/mcp_cli.py space invite my-project sha256:<64-lowercase-hex>
uv run python scripts/mcp_cli.py live note my-project observation "Build OK"   # short
uv run python scripts/mcp_cli.py bank consolidate my-project --json            # async mid ACK; stop here
```

For this explicit operator walkthrough, copy the returned `job_id` and perform a
deliberate later check (never an automatic polling loop):

```bash
uv run python scripts/mcp_cli.py bank consolidation-status <job_id> --json
```

Run the remaining commands only after that check reports terminal
`"status": "succeeded"`; `running`, `queued`, `failed`, `not_found`, or an
error is a hard stop before bank reads or long pushes.

```bash
uv run python scripts/mcp_cli.py bank read-all my-project                      # mid
uv run python scripts/mcp_cli.py graph push my-project    # long — first push auto-binds to the embedded runtime
uv run python scripts/mcp_cli.py graph status my-project  # long — connection + graph stats
uv run python scripts/mcp_cli.py graph query my-project "Build" --json
# Advanced override / diagnostic only (external engine, non-default ontology):
uv run python scripts/mcp_cli.py graph connect my-project URL TOKEN MEM-ID -o general
uv run python scripts/mcp_cli.py graph disconnect my-project
# Maintenance: validate/provision the embedded runtime, then replace the legacy
# override without deleting remote graph data or ingesting documents.
uv run python scripts/mcp_cli.py graph use-local my-project
```

### Interactive Shell

```bash
uv run python scripts/mcp_cli.py shell
```

Autocomplete, history, Rich display. See [scripts/README.md](scripts/README.md) for full reference.

---

## 🧪 Tests

Unified test script with selectable suites via `--suite`:

```bash
docker compose --profile dev up -d --wait   # Prerequisite

# All suites
uv run python scripts/test_recette.py --url http://localhost:8080

# Single suite
uv run python scripts/test_recette.py --suite recette     # Agent pipeline
uv run python scripts/test_recette.py --suite isolation    # Space-scope isolation
uv run python scripts/test_recette.py --suite qualite      # MCP tools regression

# Long-tier suite — exercises the explicit graph_connect override path
# against an operator-supplied long engine; skipped without
# --graph-url / --graph-token. The nominal embedded auto-bind path needs
# no flags (see the quickstart: long_push binds by itself).
uv run python scripts/test_recette.py --suite graph \
  --graph-url http://host.docker.internal:8080 \
  --graph-token your_token

# List available suites
uv run python scripts/test_recette.py --list
```

| Suite       | Description                                                                          |
| ----------- | ------------------------------------------------------------------------------------ |
| `recette`   | Full pipeline: token → `short` notes → LLM consolidation → `mid` bank                |
| `isolation` | Writer provisioning denial, manager create/auto-grant/invite, cross-space isolation, backup filtering |
| `qualite`   | MCP tools regression testing: system, admin, space, short, mid, backup, GC           |
| `graph`     | `long`-tier explicit `graph_connect` override path: connect, push, status, disconnect (skipped without `--graph-url`/`--graph-token`) |

The imported Hivemind protocol suite also runs under `pytest`:

```bash
uv run pytest tests/test_hivemind_state.py tests/test_hivemind_peer.py
uv run pytest tests
```

---

## 🔒 Security

### Authentication

- **Bearer Token** mandatory on all MCP requests
- **Bootstrap key** to create the first admin token
- **SHA-256 Tokens** stored on S3 (never in clear text)
- **4-level permission hierarchy**: admin ⊃ manage ⊃ write ⊃ read
- **Space scope**: the per-token `space_ids` allowlist is the **only** isolation
  primitive (mono-tenant — see [What Hivemind does NOT claim (V1)](#-what-hivemind-does-not-claim-v1))
- **Writer boundary**: `write` can mutate only allowlisted spaces; it cannot
  create spaces/tokens or widen access
- **Manager boundary**: `manage` can create arbitrary new spaces and delegate
  non-admin managers transitively; existing-space invitations remain bounded by
  the caller's allowlist (ADR-0022)

### WAF (Caddy + Coraza)

- **OWASP CRS**: SQL/XSS injection, path traversal, SSRF
- **Rate Limiting**: 600 MCP HTTP events/min/IP (Streamable HTTP)
- **Mesh edge**: enabled by default, with 120 peer requests/min and a raw 256 KiB body cap
  before Coraza; Ed25519 authentication remains application-layer
- **Automatic TLS**: Let's Encrypt in production (`SITE_ADDRESS=domain.com`)
- **Non-root container**: `mcp` user

> Several inherited security acceptances assumed a trusted single operator.
> They are re-stated as explicit deployer responsibilities in the public
> threat-model contract, [`docs/SECURITY.md`](docs/SECURITY.md), under
> the [OSS mono-tenant scope](docs/EXTENSION_POINTS.md) (ADR-0003).

---

## 📂 Project Structure

```
hivemind/
├── src/live_mem/              # Source code (MCP tools + web interface)
│   ├── server.py              # FastMCP server + middlewares
│   ├── config.py              # pydantic-settings configuration
│   ├── auth/                  # Authentication (check_access = allowlist isolation)
│   ├── static/                # /live (inherited viewer) + /admin (operator console)
│   ├── core/                  # Business services
│   │   ├── storage.py         #   S3 durable store
│   │   ├── space.py           #   Memory spaces CRUD
│   │   ├── live.py            #   short notes (append-only)
│   │   ├── consolidator.py    #   mid LLM consolidation pipeline
│   │   ├── graph_bridge.py    #   long-tier ingestion (derived projection)
│   │   ├── tokens.py          #   SHA-256 token management
│   │   ├── backup.py          #   S3 snapshots
│   │   └── ...
│   └── tools/                 # MCP tools (8 modules)
│       ├── system.py          #   system_* (cross-cutting)
│       ├── space.py           #   space_*  (cross-cutting)
│       ├── access.py          #   token_create + space_invite_token (manage)
│       ├── live.py            #   live_*  → short_*
│       ├── bank.py            #   bank_*  → mid_*
│       ├── graph.py           #   graph_* → long_*
│       ├── backup.py          #   backup_* (cross-cutting)
│       └── admin.py           #   admin_*  (cross-cutting)
├── scripts/                   # CLI + Shell + Tests
├── waf/                       # Caddy + Coraza WAF
├── docs/                      # Deployment, security, migration, protocol/tool docs
├── assets/brand/              # Logo assets (license + hash provenance)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── uv.lock
```

> **Note:** the source package and several module names still carry the imported
> `live_mem` / `graph_bridge` identity. The public grammar is an additive layer;
> source renames are later work, not behavior changes.

---

## 🔍 Troubleshooting

### Service does not start

```bash
docker compose logs hivemind --tail 50
docker compose logs waf --tail 20
```

### 401 Unauthorized

- Check your token: `Authorization: Bearer YOUR_TOKEN`
- Bootstrap key is not a token — create a token first via `admin_create_token`

### Consolidation fails

- Check LLM credentials in `.env`
- Default timeout is 600s — increase `CONSOLIDATION_TIMEOUT` if needed
- `bank_consolidate` returns an async job acknowledgement (`running` or `queued`)
  with `next_action="return_to_user_without_polling"`; call it once and do not
  watch/poll unless explicitly requested
- `bank_consolidation_status(job_id)` remains available for manual status checks only

---

## 🤝 Contributing

Development happens **through GitHub** — issues, pull requests, and code
review. Public architecture contracts live in
[`docs/ARCHITECTURE_CONTRACTS.md`](docs/ARCHITECTURE_CONTRACTS.md),
[`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md), and
[`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md). Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) before opening an issue or pull request.

---

## 🗺️ Documentation map

English is the canonical contract; French guides preserve the same critical
behavior without requiring line-by-line parity.

| Need | Start here |
| --- | --- |
| Evaluate or install Hivemind | This README, then [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Connect an agent | [`docs/AGENT_MEMORY_SETUP.md`](docs/AGENT_MEMORY_SETUP.md), then the [Codex](CODEX_INTEGRATION.md) or [Claude Code](CLAUDE_CODE_INTEGRATION.md) guide |
| Understand tools and permissions | [`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md), [`docs/TOOL_EXPOSURE.md`](docs/TOOL_EXPOSURE.md), and [`scripts/README.md`](scripts/README.md) |
| Understand architecture and boundaries | [`docs/ARCHITECTURE_CONTRACTS.md`](docs/ARCHITECTURE_CONTRACTS.md), [`docs/POSITIONING.md`](docs/POSITIONING.md), and [`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md) |
| Secure, back up, restore, or migrate | [`docs/SECURITY.md`](docs/SECURITY.md), [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), and [`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md) |
| Troubleshoot or ask for help | [`FAQ.md`](FAQ.md), [`SUPPORT.md`](SUPPORT.md), and [`SECURITY.md`](SECURITY.md) for confidential vulnerability reports |
| Contribute | [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), and [`CHANGELOG.md`](CHANGELOG.md) |

---

## 📄 License

Apache License 2.0

---

## 👤 Origin

Hivemind builds on engines originally developed by **Christophe Lesur**. The
project is released under Apache-2.0.

> The public release identity is recorded in [`VERSION`](VERSION) and the
> public [`CHANGELOG.md`](CHANGELOG.md): the project ships as `hivemind` at
> [github.com/Lesur-ai/hivemind](https://github.com/Lesur-ai/hivemind),
> versioned by the repository `VERSION` file (SemVer).

---

*Hivemind — the open memory layer for collective agent awareness. `short · mid · long`.*
