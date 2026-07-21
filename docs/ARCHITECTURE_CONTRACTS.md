# Hivemind public architecture contracts

This page makes the decision identifiers retained in source comments and
operator documentation understandable from the public repository alone. The
runtime code and tests remain authoritative for exact behavior; these summaries
state the user-visible invariants.

## Core contracts

| Identifier | Public contract |
| --- | --- |
| ADR-0002 | `short`, `mid`, and `long` are the canonical memory tiers. Historical `live_*`, `bank_*`, and `graph_*` names remain compatibility aliases. |
| ADR-0003 | Hivemind OSS is mono-tenant. A token's `space_ids` is an access allowlist, not a tenant boundary. |
| ADR-0004 | One `space_id` owns short notes, the mid bank, the derived long projection, and Project Mesh coordination state. |
| ADR-0005 | Compatibility aliases remain callable until a future explicitly versioned removal. |
| ADR-0008 | Missing, ambiguous, or corrupted critical Hivemind state fails closed; it is never silently treated as a local/non-Hivemind space. |
| ADR-0010 | Long memory is derived and non-authoritative. It never decides commit, rollback, membership, audit, tombstone, watermark, backup or recovery truth. |
| ADR-0011 | Shared-space commits pass through one authorization point after the required queue, lease, fencing, staging and acknowledgement checks. |
| ADR-0012 | Local credentials and long bindings are excluded from shared protocol state unless a field is explicitly allowlisted. |
| ADR-0014 | Restore over a shared/unsafe space is refused by default. Explicit `unsafe_recovery=True` uses forward-only recovery and finishes `RESYNC_REQUIRED`; corrupt state is always refused. |
| ADR-0015 | Membership changes are operator-driven and monotonic. Bootstrap imports only into a virgin target; eviction advances the membership epoch; rejoining requires explicit audited resync. No timeout silently removes a member. |
| ADR-0016 | The signed invitation, join claim and source approval declare enrollment intent; the applied `MembershipView` is runtime authority. Peer `read`/`propose`/`commit` scopes only narrow access, unknown peers default-deny, and private keys are never distributed. |
| ADR-0017 | Long freshness is a local derived watermark `(bank_version, commit_id, term, provenance)`. Missing or unreadable coordinates report long as unavailable and never block or authorize a commit. |
| ADR-0018 | Public artifacts use the Hivemind identity and SemVer. Release claims, migration notes and publication remain human-gated. |
| ADR-0019 | The default product includes the embedded long runtime; external `graph_connect` is an advanced diagnostic/migration override. |
| ADR-0022 | `manage` is a transitive provisioning role; routine agents should use dedicated `read,write` tokens. |
| ADR-0024 | Project Mesh pairing uses signed, one-time invitations and explicit source verification/approval. |
| ADR-0026 | The application defaults Mesh on and fails closed without a complete identity. The local development helper explicitly selects single-node mode. |

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
