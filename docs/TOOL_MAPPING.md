# Canonical tier aliases — Hivemind

This document is the definitive mapping between the historical
`live_*`/`bank_*`/`graph_*` names and Hivemind's canonical
`short_*`/`mid_*`/`long_*` grammar. It is deliberately an **alias-only**
contract: current permissions, audiences, discovery profiles, operations, and
the complete 61-name surface are generated in
[`TOOL_EXPOSURE.md`](TOOL_EXPOSURE.md) and specified in
[`MCP_TOOLS_SPEC.md`](MCP_TOOLS_SPEC.md). They are not duplicated here.

Every canonical name below is an additive registration of the **same function**
as its historical name. The historical name remains callable; an alias never
changes parameters, authorization, confirmation gates, side effects, or return
shape. A change to this table is therefore an intentional compatibility change
and must update `tests/fixtures/tool_surface.json` and the alias-parity tests.

## Mapping

| Tier | Historical name | Canonical name | Role |
| --- | --- | --- | --- |
| short | `live_note` | `short_note` | Append a token-attributed note. |
| short | `live_read` | `short_read` | Read recent notes. |
| short | `live_search` | `short_search` | Search notes. |
| mid | `bank_read` | `mid_read` | Read one mid file. |
| mid | `bank_read_all` | `mid_read_all` | Read the complete mid bank. |
| mid | `bank_list` | `mid_list` | List mid files. |
| mid | `bank_write` | `mid_write` | Write one mid file. |
| mid | `bank_consolidate` | `mid_consolidate` | Enqueue short-to-mid consolidation. |
| mid | `bank_delete` | `mid_delete` | Delete one mid file under the same destructive contract. |
| long | `graph_connect` | `long_connect` | Configure an advanced explicit long destination. |
| long | `graph_push` | `long_push` | Project eligible mid content into derived long memory. |
| long | `graph_status` | `long_status` | Read long binding and projection status. |
| long | `graph_disconnect` | `long_disconnect` | Remove an explicit binding or return to the managed embedded binding. |

## Names that intentionally have no tier alias

The following mid supervision and maintenance names remain historical-only:

- `bank_consolidation_status`
- `bank_consolidation_queues`
- `bank_stale_spaces`
- `bank_repair`
- `bank_compact`

Cross-cutting `system_*`, `space_*`, `backup_*`, `admin_*`, `token_create`,
`space_invite_token`, and direct canonical additions such as `long_query` and
`long_ingest` keep their registered names. They are not missing aliases and are
not on an implied deprecation track.

## Invariants

1. `short` maps only to historical notes (`live_*`), `mid` to bank state
   (`bank_*`), and `long` to the ontology/graph projection (`graph_*`).
2. Alias and historical name resolve to one implementation and enforce the
   same fresh call-time authorization.
3. The `long` tier remains derived and non-authoritative. No alias can become a
   commit, rollback, audit, tombstone, watermark, membership, or recovery
   authority.
4. Destructive semantics do not soften behind an alias. In particular,
   `mid_delete` is exactly `bank_delete` and retains its current manage and
   confirmation contract as specified by the live handler and API reference.
5. The frozen fixture currently records 48 direct registry entries plus these
   13 aliases, for 61 registered names. The generated exposure inventory must
   remain consistent with that fixture.
