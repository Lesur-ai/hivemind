# Hivemind — Public Security & Threat-Model Contract

This document is the **public threat-model contract** shipped with Hivemind.
It makes accepted risks, trust assumptions, and deployer responsibilities
visible to anyone running the service in production. A documented acceptance
is a boundary to evaluate, not a claim that the underlying risk disappeared.

For coordinated disclosure of new vulnerabilities, see the repo-root
[`SECURITY.md`](../SECURITY.md).

---

## 1. Scope & non-claims

Hivemind is an **OSS, mono-tenant, single-mesh** MCP service (ADR-0003).

* **No quorum.** Project Mesh V1 / Mesh Sync V1 is full-mesh all-ACK. There
  is no Raft, no Paxos, no leader election, no majority-write semantics.
* **No hub.** No central authority sequences events.
* **No CRDT, no offline-first.** Conflicting offline writes are not
  reconciled at the protocol level; an offline peer either rejoins under the
  current shared state or is evicted and re-bootstrapped.
* **No permanent master.** Critical Hivemind state is protocol-derived;
  `long` / graph memory is a **derived view**, never authoritative
  (ADR-0010, ADR-0017).
* **No multi-tenant claim.** A single deployment serves a single
  organizational mesh; tenancy-shaped metadata is local-only and not
  replicated (ADR-0003, ADR-0012).
* **Fail-closed on corruption.** Corrupted critical state must surface as
  `RESYNC_REQUIRED` / `CorruptedStateError`; the node is treated as unsafe
  until a clean resync completes. Hivemind never silently downgrades.

If a feature you depend on is not listed in the published MCP tool surface or
the [public architecture contracts](ARCHITECTURE_CONTRACTS.md), it is **not**
part of the supported security contract.

---

## 2. Trust boundaries (route table)

The deployment pipes traffic through Caddy + Coraza WAF in front of the
Python MCP service. Two boundaries matter:

| Edge route   | WAF inspects body? | Carries           | Owner of validation        |
|--------------|--------------------|-------------------|----------------------------|
| `/mcp*`      | **No**             | Streamable HTTP / JSON-RPC MCP | **Application layer** (Hivemind code) |
| `/mesh/v1*` (enabled only) | No body (URI/headers only, after an exact 256 KiB raw cap) | Signed peer HTTP | Edge cap + WAF metadata + Mesh signature/membership policy |
| `/api/tool`  | No (body off)      | Admin console tool calls       | Application layer + cookie auth |
| `/api/*`     | Yes                | Admin REST + UI                | WAF + application layer   |
| `/`, `/static/*` | Yes            | Admin UI, static assets        | WAF + application layer   |

The WAF protects Mesh (when enabled), `/api/*`, and the fallback. It **does not
protect `/mcp`**. See §3.1.

Inside the Docker network, the WAF reaches the MCP service over **plain
HTTP** (§3.2).

### 2.1 Embedded internal services (ADR-0019) — not publicly routed

The default compose topology embeds the long runtime **inside the internal
Docker network**. None of these services appear in the WAF route table and
none publish host ports:

| Internal service | Internal endpoint      | Role                              |
|------------------|------------------------|-----------------------------------|
| `graph-memory`   | `graph-memory:8002`    | Embedded long engine (Graph Memory, vendored) |
| `neo4j`          | `neo4j:7687`           | Graph datastore for `graph-memory` |
| `qdrant`         | `qdrant:6333`          | Vector datastore for `graph-memory` |

The **only public entrypoint is the WAF** (`:8080` by default). The only
intended caller of `graph-memory` is Hivemind's long bridge, authenticating
with the scoped internal token (§4.5). Graph Memory's own public/admin
endpoints are **not** exposed through the WAF route table. Publishing host
ports for any of these services voids this boundary (§5 checklist).

---

## 3. Accepted risks → deployer responsibilities

Each item below is a documented **accepted risk**, restated as a deployer
contract: the changed trust assumption and the operator action that restores
defense-in-depth in a public deployment.

### 3.1 WAF `/mcp` bypass — all MCP validation is application-layer

* **Boundary**: The OWASP CRS in Coraza
  cannot inspect Streamable HTTP MCP traffic (response streaming +
  base64-heavy JSON bodies produce false positives and break the protocol).
  `/mcp*` is therefore reverse-proxied **without** WAF body inspection. See
  `waf/Caddyfile` route 1.
* **Trust assumption changed**: A public deployer cannot assume "the WAF
  protects all routes." For `/mcp`, the **only** layer between the public
  internet and the MCP tool surface is the Hivemind Python application.
* **Deployer responsibility**:
  1. Treat `/mcp` as the actual attack surface. Anything that would be
     CRS-blocked on `/api/*` must be blocked or harmless when it reaches
     application code on `/mcp`.
  2. Keep MCP authentication on. `/mcp` is bearer-token-authenticated at
     the application layer; **do not** disable that auth.
  3. Keep rate limiting on `/mcp`. The Caddy `rate_limit` zone on `/mcp*`
     is the only abuse limiter on this route — calibrate, do not remove.
  4. If you front Hivemind with your own ingress, replicate the same
     posture: stream-friendly proxying, bearer-token enforcement,
     per-IP rate limiting.

### 3.2 WAF → service plain-HTTP hop

* **Boundary**: Inside the Docker network, Caddy reaches
  `hivemind:8002` over plain HTTP. This assumes the network is
  considered internal.
* **Trust assumption changed**: Anyone with packet-capture inside the
  container network sees MCP bearer tokens and request bodies in clear.
* **Deployer responsibility**:
  * Treat the Docker network as a **trusted segment**; do not co-locate
    untrusted workloads on the same Docker network or shared host.
  * For higher-assurance deployments, terminate TLS at the WAF **and**
    enable in-cluster TLS (e.g. mTLS sidecar, service-mesh) between WAF
    and MCP service.
  * Never expose port 8002 directly to the public internet — public
    traffic must enter via the WAF, never bypass it.

### 3.3 No Docker egress filter

* **Boundary**: `docker-compose.yml` does not restrict outbound traffic from
  `hivemind` or `graph-memory`. Combined with any SSRF vector, outbound
  requests could go anywhere.
* **Trust assumption changed**: The service is **not** sandboxed at the
  network layer. Any tool that issues an HTTP call (`graph_connect`,
  LLMaaS calls, S3) can reach arbitrary IPs unless the host network
  policy prevents it.
* **Deployer responsibility**:
  * Apply **per-container egress allowlists** at the orchestration layer.
    Hivemind needs the configured S3 and LLMaaS endpoints plus the internal
    `graph-memory` service. Graph Memory independently needs that S3 endpoint
    for source documents and token validation, that LLMaaS endpoint for
    extraction/embeddings, and internal Neo4j/Qdrant. An additional external
    graph destination exists only under the advanced `graph_connect` override.
  * Setting `PROXY_URL` routes the application-level Internet egress of both
    services through one outbound HTTP proxy — the Hivemind core's S3 and
    LLM traffic (consolidation and health probes) and the embedded Graph
    Memory's extraction/embedding calls, provider-health probes,
    document-storage S3, and shared token-store S3 reads — and fails closed
    on proxy failure (no direct fallback). Internal traffic (the
    Hivemind→graph-memory bridge, Neo4j, Qdrant, local health checks) always
    stays direct. This concentrates outbound traffic on one enforcement
    point but is **not** a sandbox: keep the network-layer controls below as
    defense in depth.
  * On Compose: use a host firewall (iptables, nftables) or a sidecar
    egress proxy.
  * On Kubernetes: define a `NetworkPolicy` with explicit egress rules.
  * Block link-local / RFC1918 / metadata IPs (`169.254.169.254`) at the
    egress layer if the host could otherwise reach them.

### 3.4 Unsalted SHA-256 token hashing

* **Boundary**: Stored token hashes use
  unsalted SHA-256. Accepted because tokens are 32 bytes of cryptographic
  randomness (no rainbow-table risk on the input space).
* **Trust assumption changed**: The hash is **not** a credential KDF.
  It is fine for the 32-byte-random tokens Hivemind issues, but it is
  **not safe** to reuse this hashing path for low-entropy user-supplied
  passwords.
* **Deployer responsibility**:
  * Do not issue tokens out-of-band that have less than 256 bits of
    entropy.
  * Do not extend the auth code to accept human-chosen passwords without
    swapping the hash function for a real KDF (argon2id / scrypt) first.

### 3.5 Raw bearer token in admin cookie; no CSRF token

* **Boundary**: the admin console accepts the
  MCP bearer token; once moved to a cookie, the cookie carries the raw
  token. There is no separate CSRF token on `/api/tool`; the route is
  shielded by application-layer auth and by the same-origin model the
  WAF enforces.
* **Trust assumption changed**: Anyone able to mint a cross-site request
  from the admin's browser that the browser will attach the cookie to
  could trigger an admin action. The defenses are: `HttpOnly` + `Secure`
  + `SameSite=Strict` on the cookie, and CSP without `script-src
  'unsafe-inline'`.
* **Deployer responsibility**:
  * Serve the admin UI over HTTPS (TLS at the WAF, see §3.7).
  * Do **not** weaken the cookie attributes set by the application
    (`HttpOnly`, `Secure`, `SameSite=Strict`).
  * Do **not** weaken the WAF Content-Security-Policy in
    `waf/Caddyfile`. In particular, do not reintroduce `script-src
    'unsafe-inline'` or untrusted CDNs.
  * Restrict the admin console to an operator network (VPN, allow-list)
    if your threat model includes targeted CSRF from compromised admin
    browsers.

### 3.6 Opt-in S3 server-side encryption + bucket versioning

* **Boundary**: bucket versioning and server-side encryption are not enabled
  by the application itself.
* **Trust assumption changed**: Hivemind **does not** force
  server-side encryption on the S3 bucket and **does not** force bucket
  versioning. These are bucket-policy / operator concerns.
* **Deployer responsibility (mandatory for any public deployment)**:
  * Enable **S3 server-side encryption** (SSE-S3 at minimum; SSE-KMS if
    you want per-key audit) on the bucket Hivemind writes to.
  * Enable **bucket versioning** on the same bucket. Without it,
    `space_delete(confirm=True)` and any backup-restore mistake become
    unrecoverable.
  * Enable an **object-lock or lifecycle** policy that matches your
    recovery objectives. Hivemind exposes `backup_create` /
    `backup_restore` (ADR-0014) but does not replace a bucket-level
    recovery story.
  * Restrict the IAM principal Hivemind uses to the bucket and prefixes
    it actually needs. No wildcard `s3:*` on the whole account.

### 3.7 TLS at the WAF

* **Trust assumption changed**: Out of the box, `SITE_ADDRESS=:8080`
  serves HTTP. That is intended for local development.
* **Deployer responsibility** for any non-loopback exposure:
  * Set `SITE_ADDRESS=mon-service.exemple.fr` in `.env` and let Caddy
    obtain a Let's Encrypt certificate, **or** front Hivemind with an
    upstream reverse proxy that terminates TLS.
  * Do not expose plaintext HTTP to the public internet. Bearer tokens
    travel on every `/mcp` and `/api/*` request.

### 3.8 Auth coupled to S3 availability

* **Inherited posture**: Authentication tokens, allowed resources, and
  scoped peer rights are resolved against the same shared storage that
  holds the protocol log. If S3 is unreachable, **auth requests fail
  closed**.
* **Trust assumption changed**: An S3 outage is not just a data outage
  — it is an **availability** outage of the MCP service itself,
  including for tokens that were valid one second earlier.
* **Deployer responsibility**:
  * Choose an S3-compatible backend with an availability target that
    matches your MCP SLO.
  * Monitor S3 reachability from the MCP host; alert on
     `/health` failures.
  * Hivemind fails closed by design (ADR-0008). Do **not** patch this
    behaviour to "fail open on S3 errors" — that would silently let
    revoked tokens back in once cache state diverges.

### 3.9 `manage` is transitive provisioning authority (ADR-0022)

* **Permission boundary**: `write` can mutate only spaces in its persisted
  `space_ids` allowlist. It cannot create a space, create a token, or widen an
  allowlist. `manage` can call `space_create`, create canonical non-admin tokens
  through `token_create`, and add a token to an accessible existing space
  through `space_invite_token`.
* **Trust assumption changed**: `manage` is not merely a maintenance bit. It is
  a transitive delegation role and a global space-allocation role. A manager
  created by another manager can create further managers and arbitrary new
  spaces, even when its allowlist is empty. The allowlist constrains
  mutations/invitations for existing spaces; it does not constrain
  `space_create`.
* **Controls**:
  * `token_create` cannot create `admin`, cannot use the `internal-long` name,
    and always creates `space_ids: []`;
  * `space_invite_token` requires the exact canonical full hash (`sha256:` + 64
    lowercase hex), is add-only/idempotent, returns no target metadata, and uses
    one opaque failure for invalid/protected targets;
  * both operations revalidate the persisted actor while the token-store lock
    is held. Revoked, expired, downgraded, or re-scoped callers fail closed;
  * existing global list/update/revoke/delete/purge/bulk tools remain admin-only.
* **Deployer responsibility**:
  * grant `manage` only to principals trusted to allocate storage prefixes and
    recursively delegate that capability;
  * use short expirations and audit the creation/invitation path;
  * use a separate `write` token for routine agent work when provisioning is not
    required;
  * treat `space_invite_token` as MCP allowlist management, never as Project
    Mesh peer enrollment.

Space creation is a single-process prepare/grant/commit-marker protocol, not a
distributed transaction. `_meta.json` is written last. A partial result requires
an identical retry only when `recovery.retry_safe` is true. Once any scope
reference is durable, including the creating manager's own grant, an admin must
inspect the marker/prefix and remove every persisted reference before the identical
retry. Incompatible/corrupt prefixes require explicit admin object inspection
and possibly manual removal. Never automate deletion or grant removal as
rollback.

`space_delete` takes lifecycle→token locks and, for stored-token calls,
revalidates the persisted caller; bootstrap administration has no persisted
caller to revalidate. It deletes/reprobes payload first and `_meta.json` last,
then removes the deleted ID from every token allowlist. When scopes change, it
returns `deleted` only after a fresh validated token-store read proves zero
references and emits one best-effort aggregate audit event with the stored
actor's canonical full hash when one exists. That event also records the
sorted canonical full hashes of every affected token in
`target_token_hashes`, so an operator can identify whose access was removed
without exposing plaintext credentials. Payload/marker failure leaves token
scopes unchanged;
ambiguous grant cleanup is typed `partial` and surviving references keep
`space_create` blocked. An empty prefix plus scopes is non-destructive
`not_found` by default because it may be an intentional future pre-grant.
Only a caller explicitly resuming a known deletion sets
`recover_access_grants=True`; grants-only success is `grants_cleaned`. The
cleanup converges to zero references, but is not caller-idempotent: after its
own scope is removed, a manager retry is denied; a global stored admin or
bootstrap identity can inspect the terminal `not_found` state. A successful
ordinary deletion has the same caller-authority caveat. The lifecycle lock
still does not cover every short/mid/long, restore/GC, or Hivemind writer.
Quiesce all same-space mutations and background jobs before deletion. Outside
that operational precondition, a late writer may orphan data or republish the
marker after a nominal success.

If token cleanup may have persisted but its confirming read is unavailable, the
operation returns `partial` and emits a best-effort
`space_delete_grants_unconfirmed` audit record containing the sorted canonical
hashes whose scopes were submitted for removal. It never emits the successful
`space_delete_grants` event on that path.

Backup restoration is intentionally data-only: it copies space objects and
never restores token allowlists. After a successful deletion, no non-admin
token retains the target scope, so a global/bootstrap administrator must
perform the restore. A persisted global admin can then re-grant each intended
token with `space_invite_token`; a bootstrap identity has no persisted actor
hash and must instead add the scope with `admin_update_token` or
`admin_bulk_update_tokens` (`space_ids_add`). Never delete and recreate a
restored space to repair access; that destroys the data that was just restored.

Deletion loads and validates `_system/tokens.json` before the first prefix
mutation. If that registry is corrupt, version-incompatible, or unreadable, the
operation fails closed as a masked error rather than entering the
post-mutation `partial/recovery_required` contract. No space object is deleted;
an administrator must repair or restore the token registry before retrying.

Admin lifecycle tools retain their compatibility behavior and can store a
canonical `space_id` that does not yet exist on a non-admin target. Such a
future pre-grant intentionally blocks `space_create` under the ABA rule until
a global admin or an active manager already scoped to that ID deliberately
removes it. A normal `space_delete` on the absent ID also preserves it;
`recover_access_grants=True` is an explicit destructive decision that removes
peer pre-grants too. Prefer creating the space first, then assigning it; do not
use `space_ids` as a future-space reservation mechanism.

Token-store v2 forbids dormant admin allowlists: every admin entry persists
`space_ids: []`, because global access comes from the permission. Migration
clears legacy admin scopes, promotion clears scopes, and downgrade starts empty
unless that same update explicitly assigns a new non-admin scope. Hivemind and
the Graph Memory consumer fail closed on a v2 admin entry with non-empty scopes.

### 3.10 Admin console Audit view is a diagnostic buffer, not an audit log of record

* **Implemented surface**: the `/admin` console includes an **Audit**
  view backed by `admin_audit_recent`, an **in-memory ring buffer** scoped to
  the current process. It records recent console/auth events — tool name,
  argument *keys* only (never values or space IDs), and the calling client.
* **Trust assumption**: this buffer is **best-effort and non-authoritative**.
  It is per-instance, does not survive a restart, has a bounded capacity
  (oldest entries evict), and does **not** cover MCP `/mcp` tool calls. It is a
  live operator diagnostic, never the durable audit trail of record.
* **Deployer responsibility**: for a durable audit trail, enable S3 access
  logging / object-level auditing on the bucket (§3.6) and ship the service
  `audit_logger` output (one JSON line per admin tool call, argument keys only)
  to your log pipeline. Do not treat the console Audit view as complete or
  persistent. The WAF Content-Security-Policy (`waf/Caddyfile`) already scopes
  `font-src 'self'`; do not widen it to fetch fonts off-origin.

### 3.11 Project Mesh signed transport and pairing are default-on

* **Transport boundary**: when and only when
  `HIVEMIND_MESH_ENABLED=true`, `/mesh/v1*` bypasses MCP bearer auth and enters
  a separate Ed25519 proof-of-possession boundary. The broad namespace also
  captures malformed variants so they cannot fall through to MCP/admin code.
  Caddy applies a separate per-IP rate zone and the exact 256 KiB raw-body cap
  before Coraza; the application independently bounds headers, canonical JSON,
  control responses, and raw bootstrap streams.
* **No authority expansion**: a signature alone never grants membership or
  scope. Event delivery also requires the exact healthy local
  `MembershipView`, a unique active local configured key, a unique active
  source key/fingerprint matching `origin_node_id`, required event scope, and
  exact epoch before durable replay admission. Pairing remains bound to
  those checks plus the current membership authority, exact epoch fencing, the
  target reservation, full-mesh ACKs, and a bounded signed bootstrap. The
  normal three-action console flow never exposes a private key, endpoint,
  snapshot, manifest, or individual ACK input. Invitation secrets are shown
  once, persisted only as a domain-separated hash, and cleared from the console
  on every terminal or dismissal path; a post-mutation fault is explicit
  `blocked_recovery`, never a silent rollback.
* **Outbound SSRF posture**: base URLs are strict HTTPS origins outside an
  injected loopback test capability. Every DNS answer is validated against a
  checked-in IANA special-purpose registry snapshot and `is_global`; one
  deterministic validated numeric address is pinned while the original host is
  retained for Host and TLS certificate/SNI verification. Redirects, proxies,
  retries, response decompression, and environment trust are disabled.
* **Secret and replay posture**: the configured private key is an
  instance-local deployment secret and has no storage/serialization API. Replay
  records contain public identifiers/digests only and use fail-closed,
  read-after-write verified local operational storage plus a process-lifetime OS
  lock. The lock uses the exact `mesh-process-locks` sibling of the configured
  embedded-token file, with a runtime-owned `0700` local directory and a
  regular, single-link, same-device `0600` file per identity. Compose repairs
  only validated entries in that exact directory; network/distributed storage
  fails closed. The supported deployment is one process and one Mesh identity;
  multiple workers sharing an identity are outside this contract. The bundled
  Compose file masks `HIVEMIND_MESH_PRIVATE_KEY` to an empty value in the
  derived `graph-memory` service. Hivemind and Graph Memory share the
  repository-root build context so both import the same lifecycle guard. The
  root `.dockerignore` carries explicit root and recursive rules for `.env`,
  `.env.*`, `*.key`, and `*.pem`, and also excludes local worktrees,
  virtualenvs, bytecode, and tool caches before the context reaches the Docker
  daemon or BuildKit cache. The Graph Dockerfile then copies only its explicit
  vendored-runtime inputs plus `src/hivemind_inference` into the image. Retain
  both boundaries when adding services or build inputs.
* **Deployer responsibility**: configure the default-on Mesh identity before
  startup, or explicitly set `HIVEMIND_MESH_ENABLED=false` for a deliberately
  non-Mesh deployment; terminate public traffic with TLS; protect the key as a
  `0600` deployment secret; do not expose application port 8002 directly; and
  retain an egress firewall even though application SSRF checks are mandatory
  defense in depth. Exact domains and bounds are in
  [`PROJECT_MESH.md`](PROJECT_MESH.md) and
  [`ARCHITECTURE_CONTRACTS.md`](ARCHITECTURE_CONTRACTS.md).

### 3.12 Process lifecycle failures are fail-closed, not self-healing

Both shipped services own process shutdown hooks, so disabling the ASGI
lifespan protocol is unsupported. With `--lifespan off`, every request,
including health and metrics, is refused before application handling because
no shutdown can release the resources.

If the inner lifespan application dies after startup, the shared guard closes
owned resources and refuses later requests. Uvicorn can nevertheless remain
listening and returning failures: a process that has not exited does not
trigger Compose's `restart: unless-stopped` policy merely because its health
check is failing. Operators must restart/recreate that container manually or
use a health-aware supervisor that recycles persistently unhealthy processes.
This is a known, documented availability residual that requires explicit
deployment acceptance; it is not represented as automatic process recovery.

A lifespan task that deliberately suppresses repeated Python cancellation is
treated the same way: it is retained in an explicit process-terminal
quarantine, cleanup runs once, and the request gate remains permanently failed.
The guard does not claim to forcibly kill Python code; process recycle is the
only recovery boundary.

---

## 4. `long` / graph memory — local-only credentials & config

`long` / graph memory is a **derived** layer (ADR-0010, ADR-0017). Its
credentials and connection config are governed by **ADR-0012 (Shared vs
local metadata allowlist)**. The actual contract is precise — and it is
**not** "credentials never touch S3":

### 4.1 Where `graph_memory` is excluded — Hivemind shared protocol

* The `graph_memory` block in `_meta.json` is **local-only** in the sense
  of ADR-0012's shared-vs-local allowlist (`SHARED_META_FIELDS` in
  `core/models.py`).
* It is **excluded from Hivemind shared protocol replication** —
  commit/audit projection, bootstrap snapshots, and peer events. The
  staged Hivemind commit only carries fields explicitly whitelisted in
  `SHARED_META_FIELDS`; `graph_memory` is not whitelisted.
* Fail-closed: if a future field is added to `_meta.json` without
  classification, the default is **local** (excluded from shared
  replication). Adding a field to the shared allowlist is an explicit,
  reviewable change.

### 4.2 Where `graph_memory` IS present — S3 `_backups/` objects

* `BackupService.create()` (ADR-0014) takes a **byte-for-byte snapshot**
  of the space prefix. It copies every object under `{space_id}/` —
  including `_meta.json` with its `graph_memory` block (endpoint, token
  field, config) — into `_backups/{space_id}/{timestamp}/` via S3
  `copy_object`.
* **Default embedded binding: the live internal token is never at
  rest in SHARED state** (`_meta.json`, and therefore S3 `_backups/`
  copies of it). The persisted `graph_memory` block of an embedded
  binding stores the sentinel `__embedded__` in its `token` field, never
  the live secret — so a raw `_backups/` copy of `_meta.json` cannot
  leak the embedded credential (locked by test). The plaintext's ONLY
  intended store is the local `0600` secret file in the
  `hivemind_secrets` volume (§4.5) — protect that volume accordingly.
* **Explicit `graph_connect` overrides remain credential-bearing.** An
  operator-supplied token passed to `graph_connect` (advanced/diagnostic
  path) is persisted verbatim in `_meta.json` and therefore lands in raw
  `_backups/` objects. No secret stripping happens on the S3 side for
  that path.
* Operators using explicit overrides **MUST protect access to the
  `_backups/` prefix** (IAM scoping, bucket policies, audit log
  retention) **as if it contained secrets, because it does**.

### 4.3 Where masking DOES happen — outbound archive responses

* `backup_download()` applies `mask_meta_secrets()` to `_meta.json`
  before returning the tar.gz archive. If `_meta.json` cannot be parsed
  as JSON, `backup_download()` REPLACES the file content with `{}`
  (fail-closed). `space_export()` applies `mask_meta_secrets()` when
  `_meta.json` is valid JSON, but on parse failure it falls back to
  leaving the raw content in the exported archive (BEST-EFFORT masking,
  NOT fail-closed). This asymmetry is a known limitation; operators
  relying on `space_export` for redaction MUST verify the `_meta.json`
  in the resulting archive. The `graph_memory.token` field, when
  masked, is replaced with a prefix-truncated form.
* Masking applies to **outbound archive responses to MCP/HTTP callers
  only** — not to S3-side `_backups/` objects, and not to the
  application's local read path.
* Space metadata returned by `/api/space/{id}` and the equivalent MCP
  tools is similarly masked before serialization.

### 4.4 Deployer responsibility

* The default embedded binding needs **no operator-provisioned
  credential** (§4.5). If you use an explicit `graph_connect` override,
  provision that credential per-node, out-of-band, and treat each node's
  local `_meta.json` (or equivalent local store) as credential-bearing —
  same protection as a `.env` file.
* **Restrict S3 read access to the `_backups/` prefix to operator-only
  IAM. Do not assume credentials are scrubbed there — they are not.**
* Do **not** commit local-only metadata to your operations repo.
* If you build tooling that prints space metadata or unpacks S3-side
  backups, mirror the upstream masking behaviour (`"***"` for
  credentials). Do not log raw tokens. Pulling a `_backups/` object
  directly (bypassing `backup_download`) returns the raw credentials.

### 4.5 Embedded long runtime (ADR-0019) — internal token handling

The default install embeds Graph Memory plus its datastores (§2.1) and
authenticates Hivemind→Graph Memory with a dedicated **internal token**:

* **Resolution and readiness.** The plaintext is resolved during ASGI startup
  from `LONG_EMBEDDED_TOKEN` (env), then `LONG_EMBEDDED_TOKEN_FILE`, and is
  otherwise atomically created in the `hivemind_secrets` volume. The file must
  be durably published with mode `0600` before its hash is registered; no
  process-local fallback exists. Startup refuses readiness when persistence,
  validation, or registration is unsafe. No operator step is required for a
  healthy default install.
* **Local-filesystem boundary.** File-backed resolution is supported only on
  Linux-local ext2, ext3, ext4, XFS, Btrfs, tmpfs, or overlayfs. The directory
  is `0700`;
  credential and lock entries are regular, single-link, same-device `0600`
  files. Descriptor-relative `O_NOFOLLOW` access, advisory locking,
  no-replace atomic publication, file/directory `fsync`, and exact
  crash-orphan cleanup protect concurrent starts and process death. NFS,
  CIFS/SMB, FUSE, unknown filesystems, links, or unexpected entries fail
  closed. Explicit `LONG_EMBEDDED_TOKEN` is the fileless alternative.
* **Least-privilege volume initialization.** The default Compose initializer is
  a one-shot service with no network, environment, ports, or unrelated mounts
  and only `CAP_CHOWN`. It repairs recognized volume ownership/modes before the
  main service starts. Hivemind itself stays UID 10001 with all capabilities
  dropped and repeats the safety checks before readiness. The initializer is
  not guaranteed to rerun on a plain container restart.
* **Sentinel at rest.** Persisted state (`_meta.json`) stores the
  sentinel `__embedded__`, never the live plaintext (§4.2). A secret
  source that *contains* the sentinel value is treated as tampering and
  fails closed (no regeneration, no client).
* **Least privilege, enforced.** The token is registered by hash in the
  shared store `_system/tokens.json` under the reserved name
  `internal-long` with **exactly `read` + `write`** — never `manage`,
  never `admin`. The registration seam rejects any other permission set
  fail-closed (locked by test). Graph Memory validates the SAME store
  (one shared token authority): revocation is immediate and there is no separate
  credential authority.
* **Rotation & revocation.** Registration guarantees exactly ONE active token
  under the reserved name (stale same-name entries are revoked; operator tokens
  are never touched). An exact current credential that an operator revoked or
  allowed to expire causes a zero-mutation startup failure: it is neither
  reactivated nor silently replaced. Rotate only while Hivemind is stopped by
  pinning a new explicit environment secret or atomically replacing the local
  file through audited offline volume maintenance. The startup preflight is a
  readiness gate, not continuous health monitoring.

### 4.6 Graph Memory-native backups — long-runtime-only, never recovery truth

The embedded Graph Memory service ships its own backup tooling
(`backup_create`, `backup_list`, `backup_restore`, `backup_download`,
`backup_delete`, `backup_restore_archive`). Its contract in Hivemind:

* It is **long-runtime backup only** — operational tooling for the
  DERIVED graph projection (ADR-0010). It is **never Hivemind protocol
  recovery truth**: restoring Hivemind protocol state never reads,
  restores, or trusts long/graph state as commit/rollback/audit/
  membership/recovery input. Structurally, Hivemind's
  `BackupService` has no Graph Memory client edge (locked by test).
* A Graph Memory-native restore rewrites only the derived graph; the
  authoritative sources (bank files, canonical repository documents)
  are unaffected, and the graph can always be rebuilt from them.
* The GM-native backup/restore surface is reachable by **write-capable**
  Graph Memory tokens (access + write, not admin) — including the
  internal token (§4.5). This is bounded by ADR-0010: the blast radius
  of a graph restore is the derived projection, never Hivemind protocol
  state. Operators who want stricter control must protect the internal
  network boundary (§2.1) and audit the shared token store.

---

## 5. Secure-by-default operator checklist

Operator-side hardening that Hivemind expects but cannot enforce from
inside the container:

1. **TLS at the WAF** (`SITE_ADDRESS=<your-domain>`), public traffic
   over HTTPS only.
2. **S3 SSE enabled** on the bucket (SSE-S3 or SSE-KMS).
3. **S3 bucket versioning enabled** on the bucket.
4. **S3 IAM principal scoped** to the bucket/prefixes Hivemind uses.
5. **Per-container egress allowlists** at the host or orchestrator layer:
   Hivemind → S3, LLMaaS and internal Graph Memory; Graph Memory → S3,
   LLMaaS and internal Neo4j/Qdrant. Add an external graph destination only
   when intentionally using `graph_connect`; deny everything else.
6. **Admin console exposed only to an operator network** if your threat
   model includes targeted CSRF or credential phishing.
7. **Explicit `graph_connect` override credentials** kept local; never
   committed and never replicated through the Hivemind shared protocol.
   Note that S3-side `_backups/` objects **do** contain raw credentials
   for that explicit path (see §4.2) — the default embedded binding
   stores only the `__embedded__` sentinel at rest; masking applies to
   outbound archive responses.
8. **Restrict S3 read access to the `_backups/` prefix to operator-only
   IAM**; do not assume credentials are scrubbed there.
9. **`HttpOnly` / `Secure` / `SameSite=Strict`** cookie attributes left
   intact; CSP unchanged from `waf/Caddyfile` defaults.
10. **Rate limits on `/mcp`** kept enabled (Caddy `rate_limit` zone).
11. **Monitoring** of S3 reachability and of `RESYNC_REQUIRED` /
    `CorruptedStateError` events — both are availability-shaped signals
    that must page an operator.
12. **Set a strong `NEO4J_PASSWORD`** beginning with an alphanumeric
    character (required by the compose file — it gates the embedded graph
    datastore, and the Neo4j bootstrap CLI treats a leading `-` as an option).
13. **Never publish host ports for `graph-memory`, `neo4j` or `qdrant`**
    (§2.1) — the WAF is the only public entrypoint; the embedded
    services trust the internal network.
14. **Protect the `hivemind_secrets` volume** (startup-created internal token,
    mode `0600`) with the same care as a `.env` file. Stop WAF/Hivemind before
    offline maintenance. Prepare and persist the replacement source before
    revoking the old hash; restart never reactivates or implicitly regenerates
    an explicitly revoked current credential.
15. **Keep `LOCALHOST_AUTH_BYPASS=false`** (the shipped default) on the
    embedded Graph Memory so it fails closed on a missing or rejected
    credential.
16. **Set a strong `ADMIN_BOOTSTRAP_KEY` (≥32 random characters)** in
    `.env` — it is mandatory: Hivemind refuses to start on an empty,
    default, known-weak, or shorter-than-32-character value at both the ASGI
    factory and CLI entrypoint. Any bearer presenting this key gets full
    admin, and the compose file passes the same key to the embedded
    Graph Memory as the single shared admin credential — treat it as
    break-glass only and mint scoped tokens via `admin_create_token`
    for day-to-day use. To withdraw the bootstrap credential from the
    running embedded Graph Memory, blank `ADMIN_BOOTSTRAP_KEY` in a
    compose override of the `graph-memory` service environment (the
    service then disables its bootstrap auth path); do **not** blank
    it in `.env`, which stops Hivemind from starting.
17. **Use `read,write` for routine agents**. Grant `manage` only to identities
    trusted for arbitrary space creation and transitive manager delegation.
18. **Onboard through bounded tools**: a manager calls `token_create`, stores
    the one-time secret, then calls `space_invite_token` with the exact canonical
    hash. Reserve `admin_*` for global lifecycle work.
19. **Alert on `space_create` partial/recovery-required**. Retry identical
    inputs only when `recovery.retry_safe` is true. A durable manager grant
    requires admin inspection and explicit removal of every persisted reference
    before retry; never auto-delete preparation state or roll grants back.
20. **Quiesce a space before `space_delete`**. Stop notes, consolidation,
    graph, restore/GC, and Hivemind jobs until the result is handled. The
    payload-first, `_meta.json`-last protocol then removes all token grants and
    confirms zero references. Any unconfirmed object or token cleanup is typed
    `partial`; retry only when `recovery.retry_safe` is exactly true. The
    lifecycle lock is not a universal writer barrier.

---

## 6. Reporting vulnerabilities

Use the coordinated-disclosure process described in the repo-root
[`SECURITY.md`](../SECURITY.md). Do **not** file public GitHub issues
for suspected vulnerabilities.

---

## 7. References

The ADR identifiers below are stable decision labels. Their public meaning and
boundaries are summarized in
[`ARCHITECTURE_CONTRACTS.md`](ARCHITECTURE_CONTRACTS.md).

* ADR-0003 — OSS mono-tenant scope.
* ADR-0008 — Fail-closed Hive context resolution.
* ADR-0010 — `long` memory is derived only.
* ADR-0012 — Shared vs local metadata allowlist.
* ADR-0014 — Backup / restore shared-space semantics.
* ADR-0015 — Membership lifecycle (bootstrap, eviction, resync).
* ADR-0016 — Repo-driven enrollment & scoped peer rights.
* ADR-0017 — Derived long-memory watermark.
* ADR-0019 — Long runtime is a mandatory embedded product component.
* ADR-0022 — Manage delegation and space provisioning.
* `waf/Caddyfile` — public route table and WAF posture.
* `docker-compose.yml` — container topology.
