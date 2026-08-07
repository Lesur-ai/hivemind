# Hivemind public architecture contracts

This page makes the decision identifiers retained in source comments and
operator documentation understandable from the public repository alone. The
runtime code and tests remain authoritative for exact behavior; these summaries
state the user-visible invariants.

## Core contracts

| Identifier | Public contract |
| --- | --- |
| ADR-0001 | `docs/adr/README.md` is the canonical, append-only ADR registry. Numbers are never reused, and lifecycle changes use only the documented transitions. |
| ADR-0002 | `short`, `mid`, and `long` are the canonical memory tiers. Historical `live_*`, `bank_*`, and `graph_*` names remain compatibility aliases. |
| ADR-0003 | Hivemind OSS is mono-tenant. A token's `space_ids` is an access allowlist, not a tenant boundary. |
| ADR-0004 | One `space_id` owns short notes, the mid bank, the derived long projection, and Project Mesh coordination state. |
| ADR-0005 | Compatibility aliases remain callable until a future explicitly versioned removal. |
| ADR-0006 | Tools and the unified MCP facade depend on explicit short, mid, long, and hive engine ports. `mid` depends on the `hive`/`WriteSink` seam for shared writes, `long` consumes committed `mid` state, and `hive` depends on none of the tier engines; no engine bypasses these boundaries with ad-hoc storage access. |
| ADR-0007 | Non-Hivemind writes use a direct-local `WriteSink`; shared-space writes use the staged Hivemind sink and cannot become visible before the serialized `BANK_COMMIT`. |
| ADR-0008 | Missing, ambiguous, or corrupted critical Hivemind state fails closed; it is never silently treated as a local/non-Hivemind space. |
| ADR-0009 | Shared queue order is the deterministic tuple `(sequence, membership_epoch, requester_node_id, event_id)`. `sequence` stays caller-allocated and best-effort; there is no atomic sequence allocator. |
| ADR-0010 | Long memory is derived and non-authoritative. It never decides commit, rollback, membership, audit, tombstone, watermark, backup or recovery truth. |
| ADR-0011 | Shared-space commits pass through one authorization point after the required queue, lease, fencing, staging and acknowledgement checks. |
| ADR-0012 | Local credentials and long bindings are excluded from shared protocol state unless a field is explicitly allowlisted. |
| ADR-0013 | Under `PROTOCOL_VERSION = 1`, persisted `EventType` names and values are append-only: existing members cannot be removed, renamed, or repurposed. `origin_note_id` remains an alias of `note_id`, not a second durable key. |
| ADR-0014 | Restore over a shared/unsafe space is refused by default. Explicit `unsafe_recovery=True` uses forward-only recovery and finishes `RESYNC_REQUIRED`; corrupt state is always refused. |
| ADR-0015 | Membership changes are operator-driven and monotonic. Bootstrap imports only into a virgin target; eviction advances the membership epoch; rejoining requires explicit audited resync. No timeout silently removes a member. |
| ADR-0016 | The signed invitation, join claim and source approval declare enrollment intent; the applied `MembershipView` is runtime authority. Peer `read`/`propose`/`commit` scopes only narrow access, unknown peers default-deny, and private keys are never distributed. |
| ADR-0017 | Long freshness is a local derived watermark `(bank_version, commit_id, term, provenance)`. Missing or unreadable coordinates report long as unavailable and never block or authorize a commit. |
| ADR-0018 | Public artifacts use the Hivemind identity and SemVer. V1 means mono-tenant full-mesh all-ACK; migration notes are a prerequisite, and publication remains human-gated. |
| ADR-0019 | The long ontology/knowledge-graph engine is a mandatory embedded product component, not an optional bridge, operator-supplied image, or separately provisioned backend. It stays derived and non-authoritative under ADR-0010; external `graph_connect` is only an advanced diagnostic/migration override. |
| ADR-0020 | Independent review routing follows the provider and model that materially authored the change. The reviewer is cross-vendor, and unknown or inseparably mixed authorship fails closed. ADR-0031 changes round scope and bypass, not this route. |
| ADR-0021 | The independent adversarial gate starts on the assembled PR. Every new commit makes the current reviewed-head verdict stale. Local PLAN and working-diff self-reviews are quality controls. ADR-0030 reconciles routine trigger/merge intent; ADR-0031 bounds post-finding re-review. |
| ADR-0022 | `manage` is a transitive provisioning role; routine agents should use dedicated `read,write` tokens. |
| ADR-0023 | The private repository is canonical and the public repository is a fresh-history release artifact staged from an exact source SHA. Never make the working private repository public, substitute history filtering, use `push --mirror`, or add automatic continuous mirroring. Public tags, images and releases are bound to public commits; community intake preserves attribution. Rename, repository creation, first branch push, visibility, merge, tag, image, and GitHub Release each require a separate human GO. |
| ADR-0024 | Project Mesh pairing uses signed, one-time invitations and explicit source verification/approval. It supersedes only ADR-0016's declared enrollment-intent source; the Git manifest becomes an optional signed export/mirror, while the applied `MembershipView`, scoped rights, and fail-closed authority clauses remain in force. |
| ADR-0026 | The application defaults Mesh on and fails closed without a complete identity. The local development helper explicitly selects single-node mode. |
| ADR-0027 | Chat and embedding consumers use provider-neutral interfaces and separate operator profiles. Provider selection, egress, redaction, retries, health, and certification evidence remain explicit and fail closed. Its superseded vector lifecycle clauses are narrowed by ADR-0028 and its v1.4.0 provider-selection clauses by ADR-0029. |
| ADR-0028 | The canonical collection name is `memory_v1_<readable>_<digest>`: the first 32 ASCII `memory_id` characters plus the full 64-character lowercase SHA-256 digest, at most 107 characters. Its exact compact identity fields are `schema_version`, `embedding_contract_version`, `memory_namespace`, `provider_id`, `adapter_id`, `configured_model`, `resolved_model`, `model_evidence`, `dimensions`, `distance`, `endpoint_sha256`, and `profile_fingerprint`. `memory_namespace` is the exact ownership proof; `profile_fingerprint` covers exactly the ten compatibility fields and excludes `memory_namespace` and itself. The hidden `manage`-only `long_reindex` bounded maintenance operation accepts only an existing embedded `reindex_required` projection: one process-local per-memory maintenance gate rebuilds verified Neo4j/S3 sources into an attributable shadow, exhaustively validates it, and makes one final atomic stable-alias switch followed by a read. Once non-idempotent dispatch begins, an unverifiable outcome conservatively reports possible activation and is retry-unsafe. Old targets and shadows are retained; online, HA, resume, cleanup, and general long-data lifecycle remain a separate workstream outside this contract. |
| ADR-0029 | The planned v1.4.0 compatibility and certification matrix contains exactly Cloud Temple, OpenAI, Anthropic, and Gemini. Matrix membership defines required scope, not current evidence state: each profile remains experimental, compatible, certified, stale, blocked, or unsupported according to its dated exact-SHA evidence. Other named providers and generic endpoints are deferred and create no v1.4.0 release claim or certification dependency. |
| ADR-0030 | Clear intent authorizes only the bounded routine private Git transaction: PR publication, one non-force feature-branch push, or gated ordinary private PR merge. RC-to-`main` and hotfix-to-`main` integration are never ordinary. Merge intent automatically triggers the bounded review cycle and never implies review bypass. Direct `main`, force-push, destructive, release, public, deployment, operator-recovery, and live-system actions remain forbidden or separately exact-target gated. |
| ADR-0031 | The adversarial cycle is at most one global review, one initial-fix review, and one final fix-of-fix review. Later rounds stay traceable to the initial finding ledger; final NO-GO stops for a detailed human arbitration. The human may explicitly bypass review at any time, but that bypass never waives CI, tests, protected `main`, release, public, destructive, recovery, deployment, or live-system gates. |

## Project Mesh V1 boundary

Project Mesh V1 is full-mesh all-ACK. Every active member in the authoritative
`MembershipView` must acknowledge the serialized shared-space mutation before
it is committed. An unreachable active member blocks or fails the mutation
until explicit recovery or a membership change; it is never silently excluded
as a "reachable peers" quorum. There is no permanent central coordinator after
bootstrap.

<!-- non-claims -->
Project Mesh V1 does not claim quorum consensus, a hub topology, a permanent
master or leader runtime, offline-first CRDT merge, merging two populated
spaces, parallel collective consolidation, or multi-tenant isolation.
<!-- /non-claims -->

See [`PROJECT_MESH.md`](PROJECT_MESH.md) for pairing and operations,
[`MCP_TOOLS_SPEC.md`](MCP_TOOLS_SPEC.md) for tool contracts,
[`SECURITY.md`](SECURITY.md) for trust boundaries, and
[`MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
for upgrade and recovery procedures.
