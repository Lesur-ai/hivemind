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
[![version](https://img.shields.io/badge/version-1.4.0-9CA3AF?style=flat-square)](#license)
[![CI](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml/badge.svg)](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-111827?style=flat-square)](#license)
[![python](https://img.shields.io/badge/python-3.11+-F59E0B?style=flat-square)](#requirements)

Français · [README.fr.md](README.fr.md)

English is the canonical contract. The French README preserves the same
critical behavior but may trail this page's editorial structure.

</div>

---

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
It is built from the organized `mid` memory and can always be rebuilt from it.
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
uv run python scripts/mcp_cli.py graph query hivemind-demo "hello" --json

unset MCP_TOKEN
```

That final query searches by meaning, not only by exact wording. For persistent
deployments, replace the bootstrap credential with one manager token and one
dedicated `read,write` token per agent. Follow the
[deployment guide](docs/DEPLOYMENT.md#quickstart-dev).

## Connect an MCP client

Hivemind serves Streamable HTTP at `/mcp` through the WAF on port `8080`.

```python
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def use_hivemind():
    headers = {"Authorization": f"Bearer {os.environ['HIVEMIND_TOKEN']}"}

    async with streamablehttp_client(
        "http://localhost:8080/mcp", headers=headers
    ) as (read, write, _):
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

### Admin Console

Open `http://localhost:8080/admin` with a valid token. The console covers the
shipped operator workflow in seven areas: **Dashboard**, **Spaces**, **Space
Detail**, **Consolidation**, **Audit**, **Access**, and **Operator tools**.

Use it to inspect memory, manage space access, follow consolidation jobs, work
with backups, and run maintenance actions. The older `/live` page remains a
lightweight real-time viewer for notes and bank files.

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
| `bank_compact` | `manage`; dry-run by default |
| `bank_repair` | `manage`; dry-run by default |
| `mid_write` (`bank_write`) | `manage`; writes a bank file directly |
| `mid_delete` (`bank_delete`) | `manage`; requires `confirm=True` |
| `backup_restore` | `manage`; requires `confirm=True`; shared/unsafe recovery also requires `unsafe_recovery=True` |
| `backup_delete` | `manage`; requires `confirm=True` |
| `admin_purge_tokens` | `admin`; destructive modes require confirmation |
| `long_push` (`graph_push`) | `include_volatile=True` is opt-in and requires `manage` |
| `long_status` (`graph_status`) | `include_graph` is opt-in |

## Security and boundaries

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
Hivemind 1.4 does not claim:

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

Hivemind builds on engines originally developed by **Christophe Lesur**.

---

*The open memory layer for collective agent awareness.* `short · mid · long`.
