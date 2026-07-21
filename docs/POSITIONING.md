# Hivemind Positioning and Non-Claims Guardrail

> **Governed by:** ADR-0002 (short/mid/long tiers + MCP grammar), ADR-0003 (OSS
> mono-tenant scope + downstream extension seams), ADR-0004 (unified
> single-`space_id` model),
> ADR-0005 (compatibility alias policy).
> Project-level sync vocabulary: [`PROJECT_MESH.md`](PROJECT_MESH.md).
> **Audience:** every later doc, README, marketing surface, and integration
> guide. This file is the single non-claims contract. If any public copy
> contradicts it, the copy is wrong.

Hivemind is **the open memory layer for collective agent awareness** — a
vendor-neutral, open-source MCP service that gives AI agents a shared memory
across three horizons (`short` · `mid` · `long`) plus **Project Mesh**, the
project-level synchronization feature for multi-instance collaboration on one
logical space.

The strategic thesis is deliberately multi-agent in the **multi-vendor** sense:
Markdown memory files kept by one tool are useful but become isolated islands
when every agent, IDE, or model vendor owns its own context. Hivemind makes the
memory space the durable owner instead. Any MCP-capable agent can join through a
token, the project memory persists when the operator switches vendors, and the
knowledge/IP stays in the storage and governance boundary controlled by the
workspace.

This document fixes what Hivemind **is**, and — more importantly for honest
positioning — what it **does not claim yet**. Every claim below is enforceable:
it maps to a ratified ADR or a documented invariant.

---

## 1. What Hivemind is

- **Three memory horizons, one MCP service** (ADR-0002):
  - `short` — append-only live notes; immediate working context. Historical
    `live_*` tools.
  - `mid` — the consolidated Markdown memory bank: rules, synthesis,
    consolidation state. The structured memory other agents inherit. Historical
    `bank_*` tools.
  - `long` — the ontology / knowledge-graph tier: derived associative recall.
    Historical `graph_*` tools.
- **Project Mesh.** The named feature for synchronizing multiple teams,
  open-source contributors, agent fleets, and sovereign Hivemind instances
  around one project memory space. The technical protocol/action name is
  **Mesh Sync**; Mesh Sync V1 is full-mesh all-ACK.
- **A project synchronization layer.** Several sovereign MCP instances can
  share one logical unified space (`space_id`) without a permanent central
  master.
- **Vendor-neutral agent memory.** Hivemind is not tied to one assistant,
  model provider, IDE, or coding-agent runtime. The integration contract is MCP
  plus scoped tokens, so compatible agents can share continuity without moving
  project memory into a vendor-specific prompt history.
- **Workspace-owned memory.** The durable memory belongs to the Hivemind space
  and its operator-controlled storage/policies. This is the sovereignty and IP
  protection promise: agents may change, but the accumulated project context
  remains portable and governed by the workspace.
- **Mono-tenant, allowlist-isolated** (ADR-0003): the per-token `space_ids`
  allowlist is the only isolation primitive.
- **Fail-closed on corruption.** Corrupted critical Hivemind state is treated as
  unsafe / resync-required, never as "not a Hivemind space" (ADR-0004).

The three product moments are the verbal frame: agents **notice** current work
(`short`), **inherit** prior learning (`mid`), and **understand** complex
projects through the collective knowledge graph (`long`).

---

## 2. What Hivemind does NOT claim (yet)

These are hard capability boundaries. Do not state, imply, or design copy that
asserts the excluded behavior as current Hivemind behavior. A future phase may
revisit each boundary; until then, describe only the bounded behavior below.

1. **Not quorum.** Project Mesh V1 / Mesh Sync V1 is **full-mesh all-ACK**,
   not a quorum runtime. Quorum, hub topology, and offline-first CRDT behavior
   are later evolutions, not V1.
2. **No permanent master.** There is no central permanent master role after
   bootstrap.
3. **Mono-tenant only.** No tenant object, no row-level security, no per-tenant
   bucket/key isolation in the open-source edition. `space_ids` is an allowlist,
   **not** tenancy; treating it as tenancy is a violation. Multi-tenant behavior
   belongs in an independently operated downstream extension and is never a
   public-repository dependency.
   (ADR-0003.)
   Vendor-neutral memory ownership does **not** imply OSS multi-tenancy.
4. **No merge of two populated spaces.** V1 does not merge two already-populated
   spaces, and there is no parallel collective consolidation.
5. **Long memory is never authoritative.** The `long` ontology / knowledge-graph
   tier is a **derived semantic projection only**. It is never the source of
   commit validity, rollback, audit, tombstones, watermarks, or recovery, and no
   long-memory graph state sits in the commit validity path. (ADR-0002, ADR-0004.)
6. **No ordinary in-place restore of a shared Project Mesh space.** The normal
   `backup_restore` path refuses every shared, blocked, unsafe, or
   `resync_required` Hivemind space. Disaster recovery is a distinct,
   operator-confirmed path: `confirm=True` and `unsafe_recovery=True` are both
   required, the caller needs `manage`, and corrupt or unclassifiable critical
   state is still refused fail-closed. The recovery choreography forces the
   membership epoch, term, token lease, and bank version forward; unions
   tombstones; clears obsolete queue/ACK state; prunes watermarks to the new
   membership; publishes through the staged commit runtime; and leaves the
   local node explicitly `resync_required`. It does **not** return a healthy,
   converged cluster: peers must resync or re-enrol before normal shared writes
   resume. Do not describe this escape hatch as a transparent or risk-free
   restore. (ADR-0014.)
7. **No AGI / sentience / literal consciousness.** "Collective awareness" and
   "collective consciousness" are positioning language, never literal claims.
   No singularity, no literal brain, no literal hive.
8. **Bounded scope.** Budgets, context windows, and scope are always bounded —
   never "unlimited".

---

## 3. Migration / compatibility posture

The historical `live_*` / `bank_*` / `graph_*` tool names remain callable during
migration. The canonical `short_*` / `mid_*` / `long_*` names are **additive
aliases** — a thin re-registration of the *identical* function, never a
divergent copy; historical tools are never renamed in place. Removal needs an
explicit later decision; no removal date is set. See **ADR-0005** and the
canonical per-tool mapping in [`TOOL_MAPPING.md`](TOOL_MAPPING.md).

---

## 4. Status framing rules

- Anything in section 2 may be referenced as *future / planned / not implemented*
  — never as *current behavior*.
- **Project Mesh** is the public feature name for multi-instance project
  synchronization. **Mesh Sync** is the technical protocol/action name. Do not
  use "Hivemind" alone as shorthand for the full-mesh protocol.
