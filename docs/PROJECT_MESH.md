# Project Mesh

> **Scope:** public positioning, protocol docs, release docs, landing-page copy.
> **Related:** [`POSITIONING.md`](POSITIONING.md).

## Definition

**Project Mesh** is Hivemind's project-level synchronization feature.

It lets several teams, contributors, and agent fleets connect around one
logical project memory space. The feature exists so software teams can
accelerate development cycles: multiple contributors can work in parallel with
their own agents without losing context, overwriting each other's memory, or
turning the project owner into the manual handoff layer. The current V1
operator pairing flow federates exactly two sovereign Hivemind instances; it
does not provision a third node.

## Operator quickstart

Project Mesh is enabled by default. Configure each instance with its own
Ed25519 identity, public HTTPS URL, and display name; the exact deployment
variables and key handling are in
[`DEPLOYMENT.md`](DEPLOYMENT.md#project-mesh-deployment).
An intentionally non-Mesh deployment must set `HIVEMIND_MESH_ENABLED=false`.

The normal operator journey deliberately has three actions and no protocol
wizard:

1. An administrator on the initialized source creates one invitation. It is
   opaque, one-time, and expires after exactly **3,600 seconds**.
2. An administrator on a verifiably blank target pastes and accepts that code.
   The target validates the signed source and reserves the space before its
   claim; it does not ask for a key, endpoint, epoch, manifest, snapshot, or
   per-peer ACK.
3. The source administrator verifies the derived target identity and approves.
   The services complete the full-mesh membership ACK, signed bounded bootstrap,
   final ACK, and activation in the background.

This V1 workflow requires the source space to have exactly one active member
and provisions a two-node mesh. It fails closed when the source already has
more than one active member. Adding a third node, or otherwise enrolling into
an existing two-node mesh, is not supported by this workflow in V1.

Use `/admin#/mesh` and `/admin#/mesh/<space-id>` for this workflow. Project
Mesh is not an MCP tool family: regular agent discovery remains capped at 24
canonical tools and exposes no `mesh_*` name. If a post-mutation step fails,
the session reports `blocked_recovery`; use its explicit resume, resync, or
eviction guidance rather than attempting a rollback.

## Product Vocabulary

Use this hierarchy consistently:

| Name | Meaning |
| --- | --- |
| **Hivemind** | The open memory layer for collective agent awareness: one MCP service, `short` / `mid` / `long` memory, workspace-owned context. |
| **Project Mesh** | The Hivemind project-level synchronization feature. Its V1 pairing workflow provisions exactly two sovereign instances around one project memory space. |
| **Mesh Sync** | The technical synchronization protocol/action behind Project Mesh. Mesh Sync V1 is full-mesh all-ACK. |

Do not use "Hivemind" alone as a public synonym for the full-mesh protocol.
Hivemind is the product. Project Mesh is the feature.

## Core Use Cases

### Multi-team software development

Several teams can work on the same project with their own agent fleets. They
can share one Hivemind deployment, or use the V1 two-instance pairing workflow.
Each deployment keeps local operational autonomy while shared project memory
is synchronized through Project Mesh.

### Open-source agent contribution

External contributors can bring their own agents and tooling into a project via
scoped credentials. Project Mesh provides shared memory, provenance, membership,
and synchronization boundaries without requiring every contributor to use the
same model vendor or agent runtime.

### Faster development cycles

Agent fleets can split work across issues, reviews, docs, tests, and recovery
without stepping on one another. Shared mutations are serialized through the
implemented Project Mesh path: durable queue, token lease, term, fencing,
staging, manifest, `BANK_COMMIT`, and ACKs.

## V1 Claims Contract

Project Mesh V1 is conservative by design:

- **Mesh Sync V1 is full-mesh all-ACK, not quorum.**
- No hub topology.
- No permanent master after bootstrap.
- No offline-first CRDT behavior.
- No merge of two already-populated spaces.
- The V1 pairing workflow provisions a two-node mesh from a single-active-member
  source; it does not enroll a third node into an existing mesh.
- No parallel collective consolidation.
- No public multi-tenant behavior in the open-source edition.
- `long` memory is never the source of commit validity, rollback, audit,
  tombstones, watermarks, or recovery.

## Copy Rules

Prefer:

- "Project Mesh synchronizes multiple teams and agent fleets around one project
  memory space."
- "Mesh Sync V1 is full-mesh all-ACK."
- "Two sovereign instances, shared project memory in the V1 pairing flow."
- "Open-source contributors can bring their own agents without giving up
  provenance or ownership."
- "Accelerate software development cycles without context collisions."

Avoid:

- "Hivemind is quorum."
- "Project Mesh is multi-tenant."
- "Offline merge" / "CRDT" / "hub" / "leader" / "permanent master".
- "Unlimited parallel agents."
- "Long memory decides commit validity."
