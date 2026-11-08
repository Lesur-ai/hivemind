# Selective maintenance on the 1.6 source (#1420)

This is the source-side bridge for the 1.7 upgrade workflow (EPIC #1418, M2).
It mirrors the semantics of the main-line barrier (#1419) so one consumer can
drive both. It does **not** activate AUTH17, change the registry (v2 stays),
alter any data format, or add a zero-downtime upgrade (#1378, 1.8).

## Minimum source version

The first 1.6 patch release that contains this document is the **minimum
maintenance-capable source**. The exact patch is pinned when that release is
qualified; a mutable `latest` tag is never evidence. Any earlier 1.6.x (or
1.5/older) has no `/api/maintenance` endpoint: the consumer must refuse it and
tell the operator to upgrade to the pinned patch first, **before** starting a
migration. There is no partial support for uninstrumented processes.

## Control plane

Each service (Core on `:8080` behind the WAF, Graph on its internal `:8002`)
exposes `POST /api/maintenance`. It is not an MCP tool, so it stays usable
while mutations (or reads, at `cutover`) are closed.

- Requires the existing **admin** permission and an explicit
  `Authorization: Bearer` header. An ambient console cookie is refused (401),
  so a cross-site request cannot pause a deployment. No new credential, grant
  or token type exists.
- Body: `{"action": "status"|"pause"|"resume", "operation_id": "<UUID>",
  "scopes": ["*"|"<space or memory id>", ...], "mode": "background"|"all"|"cutover"}`.
  `status` without `operation_id` lists every operation.
- Refusals are HTTP 409 with a fixed code: `maintenance_input`,
  `maintenance_conflict`, `maintenance_unclean`, `maintenance_recovery_required`.

| Mode | Refused | Still admitted |
| --- | --- | --- |
| `background` | structural/background mutations (consolidation, compaction, ingest, token, Mesh) | reads, SHORT writes |
| `all` | the above + SHORT writes | reads |
| `cutover` | everything functional, reads included | the control plane |

| State | Meaning |
| --- | --- |
| `normal` | no operation holds admission |
| `background_draining` / `all_write_draining` / `cutover_draining` | closed to new work; accepted work still running |
| `background_paused` / `read_only` / `exclusive_cutover` | drained |
| `recovery_required` | an unknown effect overlaps the scope |

`capture_ready` is true only in `read_only` with nothing pending or unknown on
**that service**. The same UUID cannot change scopes or lower its mode; scopes
may not overlap another operation; `resume` refuses while work is pending or
unknown. There is no timeout, PID check or empty listing that clears either.

## Cross-service order (consumer contract)

Core and Graph are separate containers; this barrier is per service and is not
a distributed transaction. Every Core call into Graph is made under a Core
ticket, so: **pause and drain Core to its target state first, then pause
Graph; resume in the reverse order.** A drained Core cannot have a nested call
left for a closed Graph to refuse. The consumer must read `status` from both
and require both `capture_ready` before sealing.

## Ownership and drainage

Ownership follows active, controller-owned tickets and retained references, not
asyncio task labels. A request admitted before closure keeps its scope through
nested effects. Every nested operation and SDK effect retains its own reference;
queued consolidation (Core) and ingest (Graph) jobs retain their acceptance ticket
until settlement. An inherited ticket must be the exact active object on this
controller. A copied object, completed parent or foreign controller is refused
with `maintenance_binding`. A scoped ticket cannot grant global authority or
authority over another space: that effect needs its own admission. A wider
child can finish an already accepted operation through closure only when every
closed controller overlapping the child's scope also counts the live non-read
parent. The child always records its actual scope, so a later pause on another
space sees it. A read or a SHORT parent not counted by a background pause cannot
grant that exception.

A read ticket never grants mutation authority. Immediately before a physical
write, the adapter atomically acquires a `mutation` ticket for the actual scope,
or refuses before dispatch. A valid SHORT owner retains SHORT authority, keeping
legitimate notes open through `background` and refusing new notes at `all`.
`check_physical` is only batch preflight; the actual dispatch also retains
ownership. Thus a preflight cannot race a pause into an uncounted write.

Core S3 owns executor work before submission and settles inside the SDK worker.
Cancelling or timing out its awaiter does not cancel that ownership, even if the
request has returned. Graph S3 and Qdrant retain ownership over each synchronous
SDK call (also when called in a task/thread); Neo4j retains it through auto-result
commit and session close. Legacy token writes explicitly consume their final
result through the observed boundary: the pinned driver may swallow a deferred
commit failure during close. Qdrant requires an explicit completed outcome, not an
asynchronous acknowledgement. A successful SDK completion releases the effect;
a transport failure, ambiguous result, or interrupted Neo4j completion records
an unknown before existing handler error translation. Definitive first-attempt
S3 4xx rejections (excluding request timeout), SDK pre-dispatch serialization/missing-credential refusals,
Neo4j client/constraint rejections and Qdrant 4xx rejections settle. Logical
Neo4j body refusals settle only after session exit succeeds; a different close
failure remains unknown. The session/result boundary records uncertain SDK errors
before a caller can intercept or translate them. S3 5xx, timeouts and responses after SDK retries remain
uncertain: a later response does not certify that an earlier attempt stopped.
A successful retried call returns its successful result while recording that
uncertainty on the active ticket; it does not become a caller-visible refusal.
A cancelled mutation request retains the existing conservative unknown rule.
Neither an SDK timeout nor an unchanged inventory certifies that a remote write
stopped. No automatic resume or reset clears it.

Mesh peer requests and admin Mesh mutations remain mutations; a closed barrier
returns an explicit retryable 503 and peer retry retains the event (all-ACK is
unchanged, nothing is dropped).

## Restart and persistence

State is a private (`0600`) file written atomically and fsynced.

- Core: `/var/lib/hivemind-maintenance/` (`MAINTENANCE_STATE_DIR`), created by the
  image. It is deliberately outside the `/data/secrets` volume, whose
  initializer accepts an exact entry grammar.
- Graph: `/app/.maintenance/` (`HIVEMIND_MAINTENANCE_STATE_DIR`).

Neither service has a dedicated volume on 1.6 (Compose is unchanged), so a pause
survives a process or `docker restart` but **not a container re-creation**. The
consumer must re-read `status` of both services immediately before sealing and
treat a missing operation as a failure.

The state directory must be writable by the service user: an unwritable one
refuses startup (fail closed) rather than silently dropping durability. Set the
variable empty only in tests (memory-only, no restart survival).

A pause is loaded before serving and never reopens automatically. A process
that did not release cleanly (crash, or shutdown with work pending) records a
deployment-wide `unknown`: reads and writes continue, but capture and `resume`
refuse. A corrupt state file refuses startup; never delete it to bypass it.
Clearing an `unknown` is an operator attestation: stop the service, verify the
affected effects independently, then remove the state file. This bridge ships
no automated recovery command.

## Controlled-producer assumption

Every writer of the namespace must be an instrumented Hivemind process. Neither
this barrier nor an S3 listing can discover an external writer or a stolen
credential. S3 is accessed only through the standard client; no provider
control plane is used.

## Physical effects and read availability (#1523)

The source inventory includes actual SDK dispatches, not just tool names:

| Captured state | Physical entry points / boundary | Read behavior |
| --- | --- | --- |
| Core SHORT/MID, rules/meta, registry/audit, Mesh state/staging/commit, backups | `StorageService.put`/`put_json`, `delete`/`delete_many`, `copy_object` → `_run_write` → controller `run_physical` | GET/list readers remain open; a hidden write requires fresh mutation admission |
| Graph originals and exact extracted text on S3 | upload/delete document, batch/prefix delete, put/delete extracted text → `StorageService._write_call` | document GET, retained-text reads, signed URLs and inventories remain reads |
| Graph S3 backup artifacts and restored originals | backup create/restore/delete raw PUT/DELETE → the same `_write_call` | backup list/download only read retained artifacts |
| Neo4j captured memories, ontology calibration, document properties, entities/relations/occurrences | create/update/delete, calibration, ingest/promotion and import use `mutation_session` through close | query/context/stats/export sessions remain reads |
| Graph legacy token nodes and named ontology files | global `mutation_session("*")` for token writes; global `physical("*")` through file write/delete | validation/list/get remain reads; Core registry v2 is unchanged |
| Neo4j legacy document normalization and migration marker | global `mutation_session("*")` at startup / marker publication | normalization is mandatory mutation work; refused before dispatch if a pause overlaps; no startup bypass |
| Qdrant vectors, collection identity and alias switch | `_write_call` covers create/index, upsert/delete/import and reindex SDK workers | `ensure_collection` and search resolve existing collections; this 1.6 source does not create collections on reads |

Core backup destinations `_backups/<space>/...` and Graph's configured backup
prefix map to the owning space/memory. Other private S3 roots are global.
Copy ownership follows its destination. A global effect overlaps every scoped
pause; a global ticket contains all scopes, while mere overlap grants nothing.

The read call paths were also inspected: Core live/bank/space readers perform
GET/list and read-only Hive status inspection; Graph bridge configuration uses
`provision=False` on reads; auth `last_used_at` updates are in-memory only.
Graph memory/document/query/status/backup readers do not mutate captured data.
Synthetic read-to-write adapter regressions deliberately exercise the formerly
permitted escape even when a currently classified read has no captured write.
A mandatory captured-state repair is mutation work and may be refused; it is
never silently treated as completed.

The only read-side exclusions are:

- The exact `_health_check/` S3 diagnostic prefix contains fixed probe bytes,
  is outside space and backup inventories, and cannot alter the recovery set.
- Neo4j lookup/fulltext indexes and uniqueness constraints are schema only,
  not logical backup records; creation never rewrites node properties. Optional
  fulltext failure falls back to CONTAINS. Global document normalization and
  its migration marker are separately guarded; they are not a schema exemption.
- In-memory auth, parsed ontology and client caches disappear on restart and
  never supply captured-state or protocol truth. Named ontology file edits are
  explicit global mutation tools, not a read-side cache repair. Per-memory
  ontology documents/calibration are covered by S3/Neo4j boundaries above.

Neither derived Graph status nor inventory/fingerprint equality proves an
in-flight effect settled. Keep the M4 seal/cutover drift checks as defense in
depth. This correction is specific to the 1.6 controller twins; main uses its
own maintenance engine, and a port requires a separate authorized ticket/PR.
There is no zero-downtime claim.

The selective-maintenance regression tests cover these ownership and drainage contracts.

## Not provided

No durable buffering of new requests, no blue/green or cross-version serving,
no automatic recovery, no Graph volume, no AUTH17 backport.
