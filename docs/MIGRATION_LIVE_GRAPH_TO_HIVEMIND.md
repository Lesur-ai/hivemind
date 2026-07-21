# Migrate Live Memory and Graph Memory to Hivemind

This operator-and-agent playbook migrates projects from separate Live Memory
and Graph Memory MCP services to one Hivemind deployment. Run it **one space at
a time**. A migrated Hivemind `space_id` owns short notes, mid-memory Markdown
files, and the derived long ontology/knowledge graph behind one MCP endpoint.

Use this guide when:

- an operator is deploying Hivemind;
- existing Live Memory spaces contain live notes and project bank files;
- existing Graph Memory memories contain a derived graph or document index;
- agents currently load `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, or additional
  workflow instructions that refer to the legacy services.

This is a data-preserving cutover playbook, not a space-merge procedure. Map
one source Live Memory space and, where present, its associated Graph Memory
memory to one Hivemind `space_id`.

## 1. Resulting architecture

Before migration, agents commonly use two MCP endpoints, two credentials, and
two identifiers:

```text
Live Memory MCP                          Graph Memory MCP
live space_id                            graph memory_id
├── live notes                           └── documents/entities/relations
└── Markdown bank
```

After migration, every agent uses one endpoint and one identifier:

```text
Hivemind MCP — space_id
├── short  recent append-only notes
├── mid    consolidated Markdown project memory
├── long   derived ontology/knowledge graph
└── hive   Project Mesh coordination state
```

Repository files remain the final authority for detailed facts. Mid memory is
the compact session bootstrap, short notes are recent unconsolidated facts, and
long memory is a semantic locator. Long output never supplies commit,
rollback, audit, membership, tombstone, watermark, or recovery truth.

### Data coverage

| Source data | Hivemind destination | Migration method |
| --- | --- | --- |
| Live notes | `short` tier under the same `space_id` | Preserve the source S3 prefix in place or restore a Live Memory backup |
| Project bank files, rules, synthesis, metadata | `mid` tier under the same `space_id` | Preserve in place or restore the same backup |
| Graph documents, entities, relations | `long` tier bound to the same `space_id` | Rebuild as a derived projection; do not restore it as authoritative state |
| Agent identity and access | Hivemind token registry | Create a new unique Hivemind token for every agent, then grant its hash to each assigned space |
| Agent memory instructions | `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, custom workflows | Rewrite to one Hivemind endpoint and canonical short/mid/long tools |

Hivemind backups cover authoritative short/mid and space metadata. They do not
copy the external graph datastores. Plan the long-tier reconstruction
separately in the long-tier reconstruction step below.

## 2. Deploy Hivemind before moving a space

Follow the canonical [deployment guide](DEPLOYMENT.md). The default Compose
stack includes the WAF, Hivemind, the embedded Graph Memory runtime, Neo4j, and
Qdrant. Graph Memory is internal-only; agents connect only to Hivemind.

At minimum:

```bash
install -m 600 .env.example .env
# Configure S3, LLM, ADMIN_BOOTSTRAP_KEY, NEO4J_PASSWORD, TLS, and secrets.
# Project Mesh is enabled by default. Supply its identity settings, or set
# HIVEMIND_MESH_ENABLED=false for an intentional non-Mesh deployment.

docker compose up --build -d --wait
docker compose ps
curl -sS https://hivemind.example.com/health
```

Then mint a dedicated administrator credential with the bootstrap key and use
that administrator, or a deliberately delegated `read,write,manage` identity,
for space provisioning. Never configure an agent with the bootstrap key or a
routine administrator token.

Before migrating production data, verify:

- the Hivemind health response is OK through the WAF;
- the configured S3 bucket is correct and protected by the operator's normal
  backup/versioning controls;
- the embedded long runtime is healthy and has no host-exposed port;
- the operator knows whether each source space will use the in-place path or
  the backup/restore path described below;
- the legacy services can be placed in read-only or maintenance mode for one
  space without affecting unrelated spaces.

## 3. Build the migration manifest

Create an operator-owned manifest outside the repository. Do not put tokens,
Graph credentials, private endpoints, or note contents in it.

Record one row per source space:

| Field | Required value |
| --- | --- |
| Source Live Memory space | Exact `space_id` |
| Source Graph Memory | Associated legacy `memory_id`, or `none` |
| Target Hivemind space | Normally the same exact `space_id` |
| Transfer path | `in-place` or `backup-restore` |
| Source short inventory | Note count and last-note timestamp |
| Source mid inventory | Rules hash; bank filename/size list; backup id and archive hash |
| Source long inventory | Document/source-path list, ontology, and representative queries |
| Agents | One row per agent identity, intended permissions, and assigned spaces |
| Instruction files | Every `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, global prompt, review prompt, or workflow snippet to rewrite |
| Validation | Pending/pass/fail for short, mid, long, token, and instruction checks |

The same target `space_id` keeps project references stable. The supported
`backup_restore` API restores to the `space_id` encoded in the backup id; it
does not rename a space. If a rename is required, stop and design a separate,
reviewed storage migration instead of improvising a prefix rewrite.

## 4. Create new Hivemind tokens for agents

Every agent must receive a **new, unique Hivemind token**. Do this even if two
agents previously shared a Live Memory token or used separate Live and Graph
credentials. Do not copy legacy token registries into new client
configurations and do not give routine agents the migration operator's token.

For each agent identity:

```bash
uv run python scripts/mcp_cli.py token create <unique-agent-name> -p read,write
```

`token_create` returns the clear-text token and its canonical hash once. Store
the secret in the agent's MCP client or secret manager and keep the exact hash
for later grants. Create tokens before the data cutover if useful, but do not
run `space invite` until the target space exists:

- **Path A (in place):** after Hivemind can read the existing space, a manager
  with access to that space invites each exact token hash.
- **Path B (backup restore):** restore and commit the target space first, then
  invite each exact token hash. `space_invite_token` refuses an absent space,
  so inviting before `backup_restore` cannot prepare this path.

After the applicable point, grant each assignment explicitly:

```bash
uv run python scripts/mcp_cli.py space invite <space-id> \
  sha256:<exact-64-lowercase-hex-returned-by-token-create>
```

Repeat `space invite` for every space assigned to that agent.

Use `read,write` for routine agents. `manage` is transitive provisioning
authority: it can create spaces and further managers, so grant it only to a
trusted provisioner. Token identity becomes short-note provenance; one token
must never represent multiple agents.

Do not activate the tokens in agent clients until the target space reaches
phase 6 validation.

## 5. Space-by-space migration playbook

Complete every phase for space A before starting space B. If a phase fails,
keep the legacy services read-only for that space, record the failure, and do
not decommission or revoke anything.

### Phase 1 — Inventory and back up the source space

While the source is still available:

1. Read and save the exact consolidation rules with `space_rules`.
2. Record `space_info` counts and metadata timestamps.
3. Record the short-note inventory. Use `live_read`/`short_read` only for a
   bounded validation sample; use the backup or S3 inventory for full coverage
   when there are more notes than a single read returns.
4. Record every mid filename and size with `bank_list`/`mid_list`. Read the
   project files needed for spot verification.
5. Run `backup_create(space_id="<space-id>")`, download the resulting archive,
   and record its cryptographic hash in the private migration manifest.
6. Record the legacy Graph Memory ontology, document/source-path inventory,
   status, and a small set of representative queries and expected source
   references. Preserve the canonical source documents themselves; graph
   entities and relations are not a replacement for those documents.
7. Enumerate every agent and every instruction file that can direct it.

Do not consolidate merely to make the migration look tidy. Unconsolidated
notes are valid short-tier data and the backup preserves them. If the operator
chooses to consolidate before cutover, let the job finish and repeat the entire
inventory and backup afterwards so the manifest describes one stable state.

### Phase 2 — Quiesce the source space

Stop all writes for this one space:

- pause agents, automations, note writers, consolidation jobs, repairs, GC,
  restore jobs, and graph pushes;
- prevent Project Mesh or other peers from mutating the space during the copy;
- wait for already accepted work to reach a terminal state;
- take a final source inventory and backup after quiescence.

If the final inventory differs from phase 1, replace the manifest values and
backup id. Never validate against a pre-quiescence snapshot.

### Phase 3 — Move short and mid memory

Choose exactly one path.

#### Path A — Reuse the existing storage prefix in place

Use this path when Hivemind will use the S3 backend that already contains the
Live Memory space.

1. Configure Hivemind with the existing bucket name and endpoint.
2. Reuse the source `space_id` exactly.
3. **Do not call `space_create`** for that id. Its `_meta.json`, rules, live
   notes, bank files, and synthesis already exist and become the unified
   space's short and mid tiers.
4. Start Hivemind and perform the read-only verification in phase 4 before
   allowing any new write.

Keep the final backup even though no object copy occurs. It is the recovery
anchor for the pre-cutover state.

#### Path B — Restore into a new Hivemind storage backend

Use this path when Hivemind uses a different bucket or storage endpoint.

1. Transfer the source `_backups/<space-id>/` prefix into the configured
   Hivemind bucket using the operator's reviewed S3 copy procedure.
2. Confirm that the target has no `<space-id>/_meta.json` and has not been
   created. **Do not call `space_create` first.**
3. Authenticate the restore with the bootstrap identity, a global admin, or a
   `manage` token that a global admin explicitly pre-scoped to this target
   `space_id`. A newly manager-created token starts with no space access and
   cannot restore an absent target.
4. Call:

   ```text
   backup_restore(
     backup_id="<space-id>/<timestamp>",
     confirm=True
   )
   ```

5. Record the restore result. Once the restored space exists, invite the new
   agent token hashes as described in section 4, then perform phase 4 before
   enabling writes.

This path restores to the original `space_id`; there is no supported target-id
parameter. A missing backup prefix returns `not_found`. Do not hand-edit
`_meta.json` or silently fall back to an unreviewed prefix copy.

### Phase 4 — Verify short and mid memory

Use an operator token first, then the new agent token:

1. Compare `space_rules` with the saved source rules.
2. Compare `space_info` note counts with the final manifest. Compare the
   last-note timestamp from the short-note/object inventory, not from
   `space_info` (which does not expose that field).
3. Compare `mid_list` filenames and sizes with the final mid inventory.
4. Read several mid files, including the active context and one stable project
   file, and compare content with the source snapshot.
5. Use `short_read` and, where useful, `short_search` to confirm representative
   recent notes, categories, timestamps, and agent provenance.
6. Confirm that `mid_read_all` completes successfully and returns the expected
   project bootstrap.

Counts and samples are operational checks, not proof that omitted objects are
safe to discard. Retain the hashed backup and source storage until the complete
migration is accepted. If any result is missing, ambiguous, access-denied, or
corrupt, fail the space and investigate; never interpret an incomplete listing
as an empty tier.

### Phase 5 — Rebuild and verify long memory

The old graph is a derived index, not a backup. Hivemind V1 does not merge or
restore a legacy graph datastore into the authoritative space state.

Use this sequence:

1. Keep the old Graph Memory instance read-only as a validation reference.
2. Confirm that every graph fact you must preserve has a canonical source
   document or a stable non-volatile mid file. If a fact exists only as an
   entity or relation in the old graph, preserve the legacy graph and recover
   its source document before decommissioning it.
3. For the one-time migration bootstrap, call
   `long_push(space_id="<space-id>", include_volatile=False)`. The first long
   write auto-binds the space to Hivemind's embedded long runtime and derives a
   deterministic internal memory id. No `long_connect` is required.
4. Verify that the result reports zero errors and that
   `activeContext.md`/`progress.md` appear in `skipped_volatile` when present.
   Never set `include_volatile=True` for routine migration.
5. Call `long_status` and compare document counts and ontology expectations.
6. Run the representative queries saved in phase 1 with `long_query`. Re-read
   the referenced canonical files before accepting the result.

`long_push` is allowed here only as an explicit one-off migration/bootstrap of
a stabilized bank. It is not a session-end synchronization channel. After
cutover, ingest stable canonical repository documents through an approved,
source-path-keyed ingestion workflow; never ingest raw mid-memory summaries or
volatile context files. The current `long_ingest` `apply` mode is deferred in
V1, so a `dry-run` or `check-remote` plan does not claim that documents were
written.

There is no automatic transfer for graph-only content. If canonical source
documents cannot yet be ingested into the embedded runtime, keep the old graph
read-only and record the space as partially migrated; do not claim completion
and do not promote graph output to recovery truth.

### Phase 6 — Rewrite and activate agent configuration

For each agent assigned to the space:

1. Remove or disable its separate Live Memory and Graph Memory MCP entries.
2. Add one Hivemind MCP entry using that agent's new unique token.
3. Replace the legacy Live `space_id` and Graph `memory_id` in instructions
   with the one migrated Hivemind `space_id`.
4. Rewrite `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, global instructions,
   review prompts, and additional workflows using the
   [unified agent setup contract](AGENT_MEMORY_SETUP.md).
5. Preserve non-memory project rules for testing, Git, safety, review, and
   deployment.

The required memory-tool changes are:

| Legacy instruction | Hivemind instruction |
| --- | --- |
| Target `LIVE_MCP_SERVER` and `GRAPH_MCP_SERVER` | Target one `HIVEMIND_MCP_SERVER` |
| Use Live `space_id` plus Graph `memory_id` | Use one Hivemind `space_id` |
| Startup `bank_read_all` | Startup `mid_read_all` |
| Startup or handoff `live_read` | Startup or handoff `short_read` |
| Write `live_note` | Write `short_note` |
| Consolidate with `bank_consolidate` | Consolidate with `mid_consolidate` |
| Query a separate Graph MCP server | Call `long_query` through Hivemind |
| Run `graph_push` at every session end | No routine long push; explicit canonical ingestion only |

The old names remain compatibility aliases, but migrated instructions should
use canonical names because Hivemind's normal tool discovery is canonical and
permission-aware.

Do not replace customized **space consolidation rules** with generic agent
instructions. `space_rules` controls how short notes become mid files;
`AGENTS.md`, `CLAUDE.md`, and related files control agent behavior. Preserve
the content and purpose of both while changing their service/tool references.

### Phase 7 — End-to-end validation and cutover

With each agent's new token and only the Hivemind endpoint:

1. `system_whoami()` returns the expected unique agent identity and
   `read,write` permissions.
2. `space_rules`, `mid_read_all`, and `short_read` succeed for the migrated
   space.
3. The same calls fail for an unassigned space.
4. A small test `short_note` is attributed to the expected agent. Remove the
   test through normal consolidation/retention flow; do not edit storage
   directly.
5. `long_status` and representative `long_query` calls succeed for the same
   `space_id`.
6. The agent demonstrates that it loaded the rewritten instruction file.
7. A post-write backup succeeds and the source legacy services remain
   unchanged in read-only mode.

Mark the manifest row complete only when short, mid, long, token scope, and
instruction loading all pass. Then allow normal Hivemind writes for that space
and proceed to the next one.

## 6. Shared-space restore caveat

The normal Path B restore targets a space that does not yet exist. Do not use
shared-space recovery as a shortcut for migration.

If a target already carries Project Mesh coordination state, `backup_restore`
refuses by default. `unsafe_recovery=True` is a disaster-recovery operation:
it forces coordination state forward, publishes restored mid memory as new
history, emits recovery audit events, reduces membership to the local node,
and leaves the node `resync_required`. It does not finish migration and does not
restore long memory. Use it only under an explicit, reviewed recovery plan,
then re-enroll and resync peers before returning the node to commit traffic.

Corrupt or unclassifiable coordination state still fails closed; the unsafe
flag is not a corruption bypass.

## 7. Rollback and decommissioning

Before the first Hivemind write, rollback means keeping clients on the
read-only legacy services and correcting the target. After Hivemind accepts
writes, the two sides have diverged; do not switch writers back and forth or
attempt to merge the histories. Treat rollback as a reviewed restore/recovery
operation using the retained pre-cutover backup.

Decommission legacy services only after every space and agent passes:

- the migration manifest is complete and independently reviewed;
- retained backups and their hashes are available;
- no client, global instruction file, CI prompt, or automation references the
  legacy endpoints;
- every agent has a new dedicated Hivemind token;
- long queries have been validated or the manifest explicitly records a
  retained read-only legacy graph dependency;
- an operator has approved token revocation and service retirement.

Revoke legacy agent tokens only after their Hivemind replacements pass. Remove
legacy Graph credentials from secret stores only after no retained validation
or source-recovery path needs them.

## 8. V1 boundaries

<!-- non-claims -->
This playbook preserves Hivemind's V1 boundaries: no multi-space merge, no
parallel consolidation for one space, no quorum, no hub topology, no permanent
master, no leader runtime, no CRDT or offline-first reconciliation, and no
multi-tenant isolation in the OSS service. Project Mesh V1 / Mesh Sync V1 uses
full-mesh all-ACK coordination. Long memory remains derived and
non-authoritative.
<!-- /non-claims -->

## 9. References

- [Agent memory setup](AGENT_MEMORY_SETUP.md) — canonical reusable
  `AGENTS.md`/`CLAUDE.md`/Cline instruction contract.
- [Deployment guide](DEPLOYMENT.md) — production topology, credentials,
  Project Mesh defaults, backup, and embedded long runtime.
- [Tool mapping](TOOL_MAPPING.md) — historical aliases and canonical tool
  names.
- [MCP tools reference](MCP_TOOLS_SPEC.md) — exact parameters, permissions,
  and response contracts.
- ADR-0004 — one `space_id` owns short, mid, long, and hive concerns.
- ADR-0010 — long memory is ontology-first, derived, and non-authoritative.
- ADR-0014 — shared-space restore and forward-forcing recovery.
- ADR-0019 — mandatory embedded long runtime.
- ADR-0022 — manager delegation and per-space token grants.
