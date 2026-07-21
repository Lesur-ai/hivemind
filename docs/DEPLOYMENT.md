# Hivemind Deployment Guide

Version: see the repository [`VERSION`](../VERSION) file.

This guide is the canonical operator-facing document for deploying Hivemind.
It documents the unified Docker Compose stack, the hardened defaults enforced
by the WAF route table, and the secure operator steps every production install
should apply. The relevant decision boundaries are summarized in the
[public architecture contracts](ARCHITECTURE_CONTRACTS.md).

## Prerequisites

- Docker Engine 24+ with Docker Compose 2.17.0 or newer (`up --wait` is used)
- An S3-compatible object store (AWS S3, Dell ECS, MinIO, etc.) — Hivemind
  is the sole authoritative writer for live notes, banks, and Hivemind
  protocol state under the configured bucket
- TLS termination capability — either via Caddy's built-in Let's Encrypt
  or via an upstream reverse proxy
- No separate long backend to supply: the embedded Graph Memory runtime
  (plus Neo4j and Qdrant) ships in the default compose stack as a
  mandatory, derived-only, non-authoritative component (ADR-0019 / ADR-0010).

## Quickstart (Dev)

The dev profile brings up a self-contained storage stack with local MinIO and
the WAF in front of the Hivemind MCP service. Python 3.11+ is needed once to
generate local secrets; `uv` supplies the locked CLI environment.

```bash
python scripts/configure_dev_env.py
uv sync --locked --dev
# Before mid/long, set LLMAAS_API_URL, LLMAAS_API_KEY, LLMAAS_MODEL,
# LLMAAS_EMBEDDING_MODEL, and the exact LLMAAS_EMBEDDING_DIMENSIONS.

docker compose --profile dev up --build -d --wait
curl -fsS http://localhost:8080/health
```

`configure_dev_env.py` creates `.env` with mode `0600`, random bootstrap,
MinIO and Neo4j credentials, `S3_SIGNATURE_MODE=sigv4`, and
`HIVEMIND_MESH_ENABLED=false` for this deliberate single-node evaluation. It
refuses to overwrite an existing file and never prints the secrets. The
application’s default-on Mesh behavior is unchanged; enabling Mesh requires the
complete identity described in [Project Mesh deployment](#project-mesh-deployment).

The configured provider must expose OpenAI-compatible `/chat/completions` and
`/embeddings` endpoints. Model ids are provider-specific; the values in
`.env.example` are illustrative and must be replaced when the provider does
not publish those exact names. The embedding dimension must equal the vector
length returned by that model. A mismatch breaks long writes/search; changing
it after ingestion requires a reviewed Qdrant collection rebuild and
re-ingestion.

After start-up (the WAF at `http://localhost:8080` is the only exposed
entrypoint to Hivemind — the Hivemind container publishes no host port; the
dev profile additionally publishes the MinIO web console on `:9001`):

1. Mint the first admin token. `ADMIN_BOOTSTRAP_KEY` is accepted as a
   Bearer credential with full admin rights; use it once to call
   `admin_create_token`, then switch to the minted token:

   ```
   # Via the bundled CLI (Streamable HTTP through the WAF /mcp route):
   export MCP_URL=http://localhost:8080
   export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"
   uv run python scripts/mcp_cli.py whoami --json
   uv run python scripts/mcp_cli.py token create ops-admin \
     -p read,write,manage,admin --json

   # Or via the admin-console REST proxy (POST /api/tool):
   curl -sS http://localhost:8080/api/tool \
     -H "Authorization: Bearer $MCP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"tool": "admin_create_token", "arguments": {"name": "ops-admin", "permissions": "read,write,manage,admin"}}'
   ```

   The response contains the clear-text token (`lm_...`) and its full canonical
   `token_hash` exactly once — only the SHA-256 hash is stored. Save both. Use
   the minted token as `MCP_TOKEN`
   for all subsequent work; the bootstrap key is a break-glass
   credential, not a day-to-day token.
2. Optional: open the redesigned admin console at
   `http://localhost:8080/admin` (log in with the minted token). It lands on
   the **Dashboard**, with **Spaces**, **Space Detail**, **Consolidation**,
   **Audit**, **Access**, and **Operator tools** (Backups, Maintenance) in the
   sidebar. The read-only viewer stays at `http://localhost:8080/live`.
3. Create your first space with `space_create` using the admin token (or a
   dedicated `read,write,manage` token). `write` alone is deliberately denied.
4. For routine agent work, create an unscoped non-admin token, save its one-time
   secret and exact returned hash, then invite it to the space:

   ```
   uv run python scripts/mcp_cli.py token create agent-worker -p read,write --json
   uv run python scripts/mcp_cli.py space invite my-space \
     sha256:<64-lowercase-hex-from-token-create>
   ```

   `token_create` accepts only `read`, `read,write`, or
   `read,write,manage`; it never creates admin and always starts with
   `space_ids: []`. `space_invite_token` is add-only and requires the caller to
   manage/access the target space. Existing global `admin_*` lifecycle remains
   admin-only.
5. Switch to the invited writer and exercise canonical short/mid tools
   (`short_note`, `short_read`, own `mid_consolidate`, `mid_read`) to confirm
   round-trip persistence to the configured S3 bucket. Create a different
   token for every agent identity; never share one agent token across clients.

For MCP client configuration and reusable project instructions, follow
[Configure agents for unified Hivemind memory](AGENT_MEMORY_SETUP.md).

> `manage` is a transitive, global space-provisioning role: a delegated manager
> can create arbitrary new spaces and further managers. Its allowlist bounds
> existing-space access and invitations, not `space_create`. Grant it only to
> trusted provisioners.

## Production

A production deployment uses the default profile — WAF, Hivemind, and the
embedded long runtime (Graph Memory + Neo4j + Qdrant, internal Docker
network only, no host ports) — backed by a remote S3-compatible object
store. The WAF stays the only exposed entrypoint.

```
docker compose up --build -d --wait
```

Production checklist:

- TLS terminates AT the WAF. Either set `SITE_ADDRESS=my-service.example.com`
  to let Caddy obtain Let's Encrypt certificates automatically (uncomment
  the `80:80` and `443:443` port bindings in `docker-compose.yml`), or
  front the WAF with an upstream reverse proxy that handles TLS.
- Configure S3 endpoint, credentials, and bucket name via `.env`.
- Generate independent random values of at least 32 characters for
  `ADMIN_BOOTSTRAP_KEY` and `NEO4J_PASSWORD`; the blank template values are
  intentionally refused or unusable.
- The shipped `.env.example` selects single-node mode explicitly. For Project
  Mesh, set `HIVEMIND_MESH_ENABLED=true` only together with the public URL,
  private Ed25519 key and display name documented below.
- Run the Hivemind container as the non-root user the image ships with
  (UID 10001); do not override the `USER` directive.
- Keep the shipped `hivemind-secrets-init` dependency enabled. It is a
  networkless, one-shot ownership initializer with only `CAP_CHOWN`; the main
  Hivemind service remains non-root with every capability dropped. Hivemind
  independently validates and registers the durable embedded credential during
  ASGI startup and refuses readiness on any unsafe or non-durable state.
- Apply per-container outbound allowlists. Hivemind needs the configured S3
  and LLMaaS endpoints plus `graph-memory` on the internal network. The
  embedded Graph Memory container independently needs that S3 endpoint (source
  documents and shared token authority), that LLMaaS endpoint (extraction and
  embeddings), and its internal Neo4j/Qdrant services. An additional external
  graph destination exists only under the advanced `graph_connect` override.
  Deny every other destination, including cloud metadata endpoints.
- Separate credentials by role: `read,write` for routine agents,
  `read,write,manage` only for trusted transitive provisioners, and admin for
  global registry/recovery operations.
- Treat a token-store migration startup failure as auth-critical. Hivemind's
  ASGI lifespan must durably upgrade legacy v1 to v2 before serving; the
  embedded Graph Memory validator accepts only exact integer v2 and never
  performs an independent migration. Do not bypass or manually coerce a
  malformed/future registry. V2 also requires every admin entry to carry
  `space_ids: []`; the migration clears legacy admin scopes, and a violation
  fails closed in both Hivemind and Graph Memory.
- Alert on `space_create` partial/recovery-required results. Retry with exactly
  matching inputs only when `recovery.retry_safe` is true. A persisted manager
  grant first requires explicit admin cleanup of every persisted scope
  reference (including admin/revoked/expired entries); incompatible/corrupt
  prefixes require admin S3 inspection and possibly manual removal. Never
  automate rollback deletion or grant removal.
- Before `space_delete`, quiesce every same-space note, consolidation, graph,
  restore/GC, and Hivemind mutation/job until the result is fully handled.
  Deletion reprobes payload and removes `_meta.json` last, but its lifecycle
  lock does not fence every writer. Treat `status:"partial"` as recovery,
  render its exact counts/failed keys/action, and never auto-retry blindly.

## WAF route table

The route table below is enforced verbatim by `waf/Caddyfile`. Three legacy
handles cover every request, with a fourth default-on handle installed unless
`HIVEMIND_MESH_ENABLED=false`:

1. **`/mcp*` — Streamable HTTP bypass.** Routed straight through to
   `hivemind:8002` with `flush_interval -1` and 1800-second read / write
   / response-header timeouts. The Coraza WAF is intentionally bypassed
   on this prefix because (a) Coraza buffers responses, which is
   incompatible with SSE streaming, and (b) MCP request bodies legitimately
   carry base64 payloads that trip the OWASP CRS. MCP authentication is
   enforced by the server itself via bearer token.

2. **`/mesh/v1*` — Signed Project Mesh peer transport.** When Mesh is enabled
   (the default), Caddy applies a separate 120 requests/minute per-IP zone and rejects
   a raw request body above exactly 256 KiB **before** Coraza can buffer or
   inspect it. Coraza retains URI/header inspection but body inspection is off
   for this signed canonical payload; the application applies its own stricter
   canonical/header/body bounds and Ed25519 peer authentication. When disabled,
   the matcher is inactive and the historical fallback handles the path.

3. **`/api/*` — Authenticated admin console.** Routed through the Coraza
   WAF with the OWASP CRS active, with a generous 300-second timeout for
   long-running admin tools. The CRS body-inspection step is disabled
   specifically for `/api/tool` (rule id 900500) because legitimate
   markdown bodies trip XSS / SQLi heuristics; URI inspection, header
   inspection, rate limiting, and size limits remain active.

4. **Fallback `/` — Everything else.** Routed through the full Coraza
   WAF + OWASP CRS pipeline with the default 30-second proxy timeouts.

Rate-limit zones (`mesh` 120/min when enabled, `mcp` 600/min, `api` 120/min,
`global` 1500/min) apply ahead of their routes. Security headers (CSP without
`'unsafe-inline'` on script-src, HSTS-ready, X-Frame-Options DENY, etc.) apply
to every response.

## Long runtime (embedded, mandatory)

The long ontology/knowledge-graph runtime (ADR-0019) is a
**mandatory, repository-shipped, embedded product component** — not an
optional add-on, an operator-supplied image, or a separately provisioned
backend. The default `docker compose up --build -d --wait` brings up the **complete**
stack: WAF, Hivemind, the embedded Graph Memory runtime (built from
`./services/graph-memory`), and its datastores Neo4j and Qdrant. There is
no opt-in `long` profile and no disabled-state release path.

- **Internal-network only.** Graph Memory, Neo4j, and Qdrant expose no
  host ports and are not routed through the WAF. Hivemind reaches Graph
  Memory at `http://graph-memory:8002` on the internal Docker network;
  Hivemind remains the only public MCP entrypoint.
- **Auto-bind, no manual step.** A Hivemind space binds to the embedded
  long runtime automatically on its first long write (`long_push`): it
  derives a deterministic `memory_id` from the `space_id` and provisions
  the embedded memory. No manual bind is required for the default install;
  `long_status` then reports the embedded runtime and `long_query` /
  `graph_status` work. Explicit `graph_connect` remains supported as an
  advanced override / diagnostic only.
- **Local-only secret, ready before traffic.** Set `LONG_EMBEDDED_TOKEN`, or
  leave it empty to let Hivemind create one scoped `read,write` (never admin)
  internal token. Before readiness, startup atomically persists the plaintext
  `0600` on the local `hivemind_secrets` volume, registers its hash, and verifies
  that the exact current credential is active. It never falls back to a
  process-local credential. The embedded runtime validates the same token store
  (one token system end-to-end), while plaintext is **never** written into
  shared metadata, commits, shared-state backups, or audit payloads. See the
  [security threat model](SECURITY.md).
- **Derived, never authoritative.** Long stays a downstream, derived
  projection: the commit / rollback / audit / recovery path has no call
  edge into it, and no long state is ever a source of truth (ADR-0010).
  A slow, stale, or restarting embedded runtime never affects bank
  correctness.

### Embedded credential lifecycle and repair

The default Compose path is deliberately fixed at
`/data/secrets/long_embedded_token`. Changing
`LONG_EMBEDDED_TOKEN_FILE` in `.env` alone does not move it. The one-shot
`hivemind-secrets-init` service receives no environment or network access and
can inspect only the `hivemind_secrets` volume. It accepts the token file, its
lock, and exact crash-orphan temporary names; an unknown entry, symlink,
directory, cross-device inode, or unsupported metadata aborts initialization
without replacing the credential or deleting the rejected entry.

File-backed secret resolution is Linux-local only. The parent must be owned by
the runtime UID with mode `0700`, and the credential/lock files must be regular,
single-link files with mode `0600`. Supported backing filesystems are ext2,
ext3, ext4, XFS, Btrfs, tmpfs, and overlayfs. NFS, CIFS/SMB, FUSE, and unknown
filesystem types are rejected because their locking and atomic-publication
semantics are
outside this contract. A non-Compose deployment may use another path only when
it preserves the same constraints; an explicit non-empty
`LONG_EMBEDDED_TOKEN` remains the fileless alternative.

The initializer runs when its container is created or recreated, not on every
plain restart. Compose may reuse a previously successful one-shot container.
This is safe for an already-valid volume because every Hivemind process start
performs its own read-only metadata check plus durable credential registration
before readiness. `docker compose restart hivemind` therefore does not rerun
the initializer; `docker compose up --no-deps hivemind` must not be used for a
first install or repair and still cannot make an invalid secret pass startup.

For the **first upgrade from a deployment that used the former process-local
embedded credential**, stop the old WAF and Hivemind containers before applying
the current Compose topology. This prevents the old implementation from
accessing the volume while the new initializer repairs it. The named volume is
deliberately retained:

```
docker compose stop waf hivemind
docker compose up --build -d --wait
```

Do not use a no-downtime `up`, `restart`, or `--no-deps` shortcut for this one
upgrade. Confirm that `hivemind-secrets-init` exited successfully and that the
new Hivemind container is healthy before restoring traffic.

To force ownership/mode repair without deleting the named volume, stop the
public and application services, remove only their containers plus the exited
initializer, and recreate them:

```
docker compose stop waf hivemind
docker compose rm -f hivemind hivemind-secrets-init
docker compose up --build -d --wait
```

The initializer repairs recognized regular entries and removes only exact
crash-orphan temporary files. It fails closed on an unknown entry without
deleting it or replacing a credential. Inspect and correct such state offline
before retrying; never run volume maintenance while Hivemind is live.

Revoking or expiring the exact current `internal-long` hash is an explicit
operator stop: the next startup performs no registry write for that credential
and refuses readiness. Do not expect a restart to reactivate or silently replace
it. For an intentional rotation, stop WAF and Hivemind first, then either keep a
new non-empty `LONG_EMBEDDED_TOKEN` pinned in `.env`, or atomically replace the
local credential through audited offline volume maintenance before restarting.
Do not revoke first unless the replacement source and rollback procedure are
ready. The initializer normalizes safe metadata; it does not choose a new secret.

This startup gate is not continuous health monitoring. After readiness, normal
token validation still enforces revocation on requests, but an operator must
restart Hivemind to re-run the durable startup preflight itself.

Existing external Graph Memory deployments are legacy references: their
content is re-ingested / rebound into the embedded runtime, never imported
as commit or recovery truth — see
`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`.

## Backup / restore

Hivemind backups follow ADR-0014:

- `backup_create` snapshots the authoritative S3 state under a versioned
  key and emits an audit entry.
- Every `backup_restore` call requires `confirm=True`; a normal restore
  needs nothing more. It targets a fresh space: the tool refuses if the
  target space already exists (delete it first), then copies the backup
  back. No `unsafe_recovery` flag is involved on this path.
- Restoring OVER a space that carries shared Hivemind protocol state is
  REFUSE-BY-DEFAULT: the call fails with a blocking error unless the
  operator additionally passes `unsafe_recovery=True`. That explicit
  unsafe recovery triggers the ADR-0014 forward-forcing choreography
  (membership epoch, term, token, and bank version all advance strictly
  — the restored state is published as new history, never a silent
  rewrite of peer-visible state), emits `UNSAFE_RECOVERY_RESTORED` and
  `RESYNC_REQUIRED` audit events, and leaves the node in
  `RESYNC_REQUIRED` until the operator resyncs the peers. Reserve the
  flag for that recovery scenario — never pass it routinely.
- `unsafe_recovery=True` does NOT bypass corruption: if the target's
  coordination state (or the backup's) is corrupted or unclassifiable,
  the restore fails closed regardless of the flag.

S3 bucket versioning (operator-enabled — see `.env.example`) is the
recommended safety net for accidental deletes that pre-date a backup.

## Secure-by-default operator steps

Every production install SHOULD apply the following hardening steps.
Each step is independently valuable and none of them require a code
change.

1. **TLS at the WAF.** Either Let's Encrypt via Caddy or an upstream
   reverse proxy. No production deployment should expose port 8080 in
   plain HTTP.
2. **S3 Server-Side Encryption (recommended).** Uncomment `S3_SSE=AES256`
   in `.env` on AWS S3 and any provider that supports the standard
   header. Dell ECS Cloud Temple users keep the line commented — Hivemind
   detects unsupported SSE headers and refuses PUTs otherwise.
3. **S3 bucket versioning (recommended).** Apply at provisioning time,
   directly on the bucket — not via `.env`:
   ```
   aws s3api put-bucket-versioning \
     --bucket "$S3_BUCKET_NAME" \
     --versioning-configuration Status=Enabled
   ```
4. **Restrict egress per container.** Allow Hivemind to reach S3, LLMaaS and
   the internal `graph-memory` service. Allow Graph Memory to reach the same S3
   and LLMaaS endpoints plus internal Neo4j/Qdrant. An additional external long
   destination exists only under the advanced `graph_connect` override. Block
   everything else at the host or network firewall.
5. **Rotate `ADMIN_BOOTSTRAP_KEY`** before the first production start,
   then rotate again after the first admin token is issued.
6. **Audit retention.** Persist Hivemind audit logs off-host. The
   [security threat model](SECURITY.md) calls for tamper-evident retention of
   protocol and authorization events. Credential lifecycle events and rights
   changes must be attributable, but plaintext long credentials never enter
   the protocol commit log, audit payloads, or the in-memory admin audit ring.

## Upgrading from live-memory

The Hivemind release rebrands the public artifact from "Live Memory" to
"Hivemind". Internal state, on-disk layouts, and the internal Python
module path (`live_mem`) are intentionally UNCHANGED — only the
operator-visible surface moves. Operators upgrading an existing
deployment should apply these one-time edits to their `.env` and host
configuration:

1. **Keep your existing bucket name.** The `.env.example` default flips
   from `live-mem` to `hivemind` for fresh installs. Existing operators
   MUST set `S3_BUCKET_NAME=live-mem` (or whatever bucket they were
   using before) explicitly in their `.env` to preserve all stored
   state. Hivemind has no migration path that renames live S3 objects.
2. **Service rename: `live-mem-service` -> `hivemind`.** Any operator
   scripts, host firewall rules, or external monitoring that referenced
   the old container name or compose service must be updated. The WAF
   upstream now resolves to `hivemind:8002` inside the compose network.
3. **Network rename: `live-mem-network` -> `hivemind-network`.** Custom
   sidecars that joined the previous bridge network must be updated.
4. **MinIO data volume.** The Docker Compose project name prefixes the
   `minio_data` named volume. If the operator wants to preserve the dev
   MinIO data across the rename, set
   `COMPOSE_PROJECT_NAME=live-memory` in the environment (or in
   `.env`); otherwise Compose will create a fresh, empty `minio_data`
   under the new project name.
5. **MCP server identity.** The new default is
   `MCP_SERVER_NAME=Hivemind`. Clients that pin on the legacy
   `Live Memory` string will need to be updated.

Internal module path stays `live_mem` (ADR-0018). The Dockerfile `CMD`
intentionally continues to invoke `python -m live_mem` inside the image.

## Project Mesh deployment

When Mesh is enabled, the authenticated peer transport performs durable
pairing. Two administrators use `/admin#/mesh` for three
actions: create one opaque invitation, accept it on a blank target, then verify
and approve it. An invitation is one-time and expires after exactly **3,600
seconds**. The service, not the operator, completes full-mesh membership ACKs,
bounded signed bootstrap transfer, final ACK, and activation. A post-mutation
failure remains `blocked_recovery` and must follow its explicit recovery action;
it never rolls membership back silently. Mesh stays on the admin/peer HTTP
surface: regular MCP discovery exposes no `mesh_*` tool.

The V1 pairing workflow accepts only a source space with exactly one active
member and provisions exactly a **two-node mesh**. It fails closed if the
source already has more than one active member. Adding a third node, or using
this workflow to enroll into an existing two-node mesh, is not supported in V1.

Generate one instance-wide Ed25519 identity on the deployment host:

```bash
uv run python -m live_mem mesh-keygen --output /secure/local/mesh-identity.key
```

The command refuses to replace a path, creates a regular single-link file mode
`0600`, fsyncs it, and prints only the public key, fingerprint, and output path.
The file contains one private-key value. Load that value through your deployment
secret facility (or a protected, uncommitted `.env`); never paste it into a
space, S3, logs, an issue, the console, or long memory. The bundled Compose
configuration explicitly replaces `HIVEMIND_MESH_PRIVATE_KEY` with an empty
value in the `graph-memory` service even though that service consumes the shared
environment file; preserve this isolation for every service other than
`hivemind`. The root `.dockerignore` also excludes `.env*`, `*.key`, and `*.pem`
from the Hivemind build context; the separate Graph Memory context is scoped to
`services/graph-memory` and cannot read root deployment files.

Configure the eight variables documented in `.env.example`. In particular:

- `HIVEMIND_MESH_PUBLIC_URL` is this instance's externally reachable HTTPS
  origin, with no credentials, path, query, or fragment;
- `HIVEMIND_MESH_DISPLAY_NAME` is required and bounded;
- `HIVEMIND_MESH_PRIVATE_KEY` is the exact `ed25519-private:v1:...` secret;
- Mesh is enabled by default. Set `HIVEMIND_MESH_ENABLED=false` only for a
  deliberately non-Mesh deployment; otherwise supply the three identity values
  before startup.

Startup fails closed on any missing/non-canonical value. The current replay
ledger intentionally supports one Hivemind process and one configured Mesh
identity per deployment; do not run multiple application workers behind the
same identity. Its retained OS lock lives in the exact sibling directory
`mesh-process-locks` under the parent of `LONG_EMBEDDED_TOKEN_FILE` (the
default is `/data/secrets/mesh-process-locks`). That parent and the lock
directory must be on the same supported Linux-local filesystem, owned by the
runtime UID, and mode `0700`; each identity lock is a regular, single-link,
same-device file owned by that UID with mode `0600`. Network/distributed
filesystems and shared multi-host volumes are unsupported and fail closed. The
Compose initializer recognizes only this exact directory and repairs its
validated entries on container recreation so a retained lock cannot make a
normal upgrade unsafe.

Peer HTTP never reuses MCP/admin bearer tokens. Production
destinations are HTTPS-only, redirects and environment proxies are disabled,
and every DNS answer must pass the checked-in special-purpose-address policy.
See [`PROJECT_MESH.md`](PROJECT_MESH.md) for the public wire, resource and
failure-order contract, and [`ARCHITECTURE_CONTRACTS.md`](ARCHITECTURE_CONTRACTS.md)
for the fail-closed invariants referenced by identifier in code comments.

## What Hivemind does NOT claim

<!-- non-claims -->
Hivemind ships with **Project Mesh V1 / Mesh Sync V1**: full-mesh
all-ACK coordination (every active member in the authoritative membership view
acknowledges every mutation). Per ADR-0018, the product/service name "Hivemind" is kept
distinct from the protocol label; the bounds below attribute to the
V1 protocol, not to the product as a whole.

The V1 protocol does NOT claim:

- **No quorum.** Project Mesh V1 / Mesh Sync V1 is not a quorum system.
  Every acknowledged write is acknowledged by every active member. An
  unreachable member blocks or fails the write until recovery or an explicit
  membership change; there is no majority/minority or "reachable peers" logic.
- **No hub topology.** The protocol is full-mesh; there is no central
  relay or star-shaped fan-out.
- **No permanent master.** No peer holds a durable primary role.
- **No leader runtime.** There is no live leader-election loop; the
  protocol does not depend on a runtime-elected coordinator.
- **No CRDT reconciliation.** Project Mesh V1 / Mesh Sync V1 does not
  ship CRDT merge semantics or offline-first divergent-merge.
- **No multi-space merge.** Cross-space merge semantics are out of
  scope for V1.
- **No parallel consolidation.** V1 consolidates serially per space;
  parallel consolidation is not in the V1 contract.
- **No multi-tenant scope (ADR-0003).** A single Hivemind deployment
  serves a single logical operator; the V1 protocol does not provide
  multi-tenant isolation.

Adjacent bounds that frame the V1 surface:

- **Long backend is derived-only (ADR-0010).** Long state is a
  non-authoritative view derived from authoritative live + bank state.
  Hivemind never treats long-backend output as a write path.
- **Bound by the public threat model.** Hivemind defends against the
  documented threat surface in [`docs/SECURITY.md`](SECURITY.md); it is not a
  substitute for network-level or host-level hardening (operator
  egress, S3 IAM, host audit) which the operator owns.

See the [public architecture contracts](ARCHITECTURE_CONTRACTS.md) for the
canonical product, feature, protocol, and authority boundaries.
<!-- /non-claims -->

## References

- [`ARCHITECTURE_CONTRACTS.md`](ARCHITECTURE_CONTRACTS.md) — stable decision
  labels and public architecture boundaries
- [`SECURITY.md`](SECURITY.md) — public security and threat-model contract
- [`MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
  — separate-services migration guide
