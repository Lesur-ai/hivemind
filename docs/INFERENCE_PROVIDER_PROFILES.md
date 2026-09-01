# v1.4.0 inference provider matrix and operator guide

> **Active evidence rule (P13-4 simplification, 2026-08-04):** deterministic CI
> proves the complete MID/long/Graph capability matrix. The separate live gate
> proves only the hosted boundary with one chat request and one embedding
> request, zero retries, and at most two provider attempts. Its states are
> `compatible`, `live-verified`, and `blocked`; `certified` is no longer an
> active P13-4 promotion state. The older schema-v3/v4 full-stack route and its
> historical runs remain diagnostic records only. They do not gate #264 or a
> release and cannot create `live-verified`.

Hivemind's v1.4 matrix exposes three single-provider references through the
generic `openai-compatible` adapter and one explicit composite profile. The
composite uses the repository-owned native `anthropic` chat adapter and Cloud
Temple's generic embedding path. Anthropic does not provide native embeddings,
so it is never registered or represented as the embedding provider.

The repository's deterministic emulator proves the exact four v1.4 profiles
against the same normalized capability matrix used by the shared inference
boundary. That proof establishes **compatible**. For v1.4.0, only Cloud Temple
may add **live-verified** when both real-provider roles pass on the same exact
source SHA as successful deterministic CI. Gemini remains **compatible** and
its live qualification is explicitly deferred after v1.4.0.

## Current evidence snapshot

This snapshot is current as of 2026-08-04. Deterministic evidence is rebound to
the final PR SHA by ordinary CI; a checked-in document cannot truthfully embed
the SHA of the commit that contains itself. Protected-live evidence remains a
separate, immutable artifact.

| v1.4 profile | Deterministic capability | Active live verification | Effective claim |
| --- | --- | --- | --- |
| `cloud-temple-reference` | complete current-tree conformance | required on the frozen release SHA; documented separately | `compatible` |
| `openai-reference` | complete current-tree conformance | outside P13-4 | `compatible` |
| `anthropic-cloud-temple-reference` | complete current-tree conformance | outside P13-4 | `compatible` |
| `gemini-reference` | complete current-tree conformance | deferred after its recorded failure; not a v1.4.0 release prerequisite | `compatible` |

The nine obsolete Cloud Temple full-stack dispatches remain diagnostic evidence:
`30854985715.1` stopped pre-egress during cold Compose materialization;
`30861840408.1` and `30868210874.1` stopped on bounded catalogue timeouts;
`30886638350.1` crossed the first chat boundary but received no HTTP status;
`30900287347.1` crossed one chat boundary and produced `invalid_response`;
`30905230569.1` stopped pre-egress on secret-init Compose failure;
`30906361163.1` crossed one chat boundary and produced `invalid_content` from
HTTP 200; `30909893552.1` produced a valid chat result that failed the exact
marker-and-model conjunction; and `30915677903.1` returned the exact marker but
a non-exact provider-reported model. Each run stopped before promotion and
retained only bounded, redacted evidence.

`blocked` describes an execution, not an erasure of deterministic capability.
These attempts do not affect the current `compatible` claims. P13-4/#264 stays
open for Gemini's post-v1.4 qualification. The v1.4.0 release boundary requires
only Cloud Temple's passing minimal manifest on the exact accepted source SHA.

## Functional parity ledger

Every `compatible` cell below means the shared production consumer path and
the profile's exact deterministic fixture pass together on the current tree.
It does not mean a real provider was called. The same core chat interface feeds
mid consolidation and long extraction; the same embedding interface feeds long
ingestion and semantic query. Health remains discovery-only. The separate live
smoke proves hosted reachability and response normalization, not this complete
functional matrix.

| v1.4 profile | Mid consolidation | Long extraction | Embeddings/query | Probes/health | Proxy + safe errors | Model/usage/correlation observability | Active live verification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `cloud-temple-reference` | compatible | compatible | compatible, 1024 dimensions | compatible | compatible | compatible | required on the frozen release SHA |
| `openai-reference` | compatible | compatible | compatible, 1536 dimensions | compatible | compatible | compatible | outside P13-4 |
| `anthropic-cloud-temple-reference` | compatible, native Anthropic chat | compatible, native Anthropic chat | compatible, Cloud Temple, 1024 dimensions | compatible per role | compatible per role | compatible per role | outside P13-4 |
| `gemini-reference` | compatible | compatible | compatible, 3072 dimensions | compatible | compatible | compatible | deferred after v1.4.0; `compatible` only |

The composite's separate embedding credential/provider is a required part of
its parity, not a fallback. A missing role becomes `unsupported`. Any failure
in the complete `short_note` → `mid_consolidate` → `long_push` → `long_query`
journey prevents deterministic compatibility even when two direct role checks
pass.

## Exact profiles

| Profile | Provider / adapter | Chat | Embeddings | Dimensions | Endpoint |
| --- | --- | --- | --- | ---: | --- |
| `cloud-temple-reference` | `cloud-temple` / `openai-compatible` | `Qwen/Qwen3.6-27B-FP8` | `bge-m3:567m` | 1024 | `https://api.ai.cloud-temple.com/v1` |
| `openai-reference` | `openai` / `openai-compatible` | `gpt-5.6-luna` | `text-embedding-3-small` | 1536 | `https://api.openai.com/v1` |
| `anthropic-cloud-temple-reference` | chat: `anthropic` / `anthropic`; embedding: `cloud-temple` / `openai-compatible` | `claude-sonnet-5` | `bge-m3:567m` | 1024 | chat: `https://api.anthropic.com`; embedding: `https://api.ai.cloud-temple.com/v1` |
| `gemini-reference` | `gemini` / `openai-compatible` | `gemini-3.6-flash` | `gemini-embedding-001` | 3072 | `https://generativelanguage.googleapis.com/v1beta/openai` |

All four profiles use the common operational ceiling of 131,072 total context
tokens and 16,384 output tokens. These are Hivemind request ceilings, not
claims about the providers' full model capacities. Temperature is omitted so
the upstream default applies.

The selected model ids are exact. Hivemind does not substitute a `latest`
alias, successor, or fallback. Relevant upstream references:

- [OpenAI GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [Anthropic Models API](https://platform.claude.com/docs/en/api/models/list)
- [Claude Sonnet 5 exact API id](https://platform.claude.com/docs/en/about-claude/models/whats-new-sonnet-5)
- [Anthropic embeddings boundary](https://platform.claude.com/docs/en/build-with-claude/embeddings)
- [Cloud Temple LLMaaS model catalogue](https://www.cloud-temple.com/en/products/large-language-model-as-a-service-llmaas/)
- [Cloud Temple live identity for the `qwen3.6:27b` catalogue id](https://llmaas.status.cloud-temple.app/api/platform-status?model=qwen3.6%3A27b)
- [Cloud Temple public rates](https://www.cloud-temple.com/en/our-public-rates/)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini 3.6 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)
- [Gemini embeddings](https://ai.google.dev/gemini-api/docs/embeddings)
- [Gemini model lifecycle](https://ai.google.dev/gemini-api/docs/deprecations)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini logs policy](https://ai.google.dev/gemini-api/docs/logs-policy)
- [Gemini API terms](https://ai.google.dev/gemini-api/terms)

## Strict migration from `LLMAAS_*`

The legacy family remains supported through the 1.x line, but it is one
generic OpenAI-compatible endpoint shared by both roles. Migration is an
atomic environment replacement, never field-by-field fallback.

| Legacy variable | Mechanical split replacement |
| --- | --- |
| `LLMAAS_API_URL` | copy to both `INFERENCE_CHAT_API_URL` and `INFERENCE_EMBEDDING_API_URL` |
| `LLMAAS_API_KEY` | copy to both role keys only when the same credential is valid for both roles |
| `LLMAAS_MODEL` | `INFERENCE_CHAT_MODEL` |
| `LLMAAS_CONTEXT_WINDOW` | `INFERENCE_CHAT_CONTEXT_WINDOW` |
| `LLMAAS_MAX_TOKENS` | `INFERENCE_CHAT_MAX_OUTPUT_TOKENS` |
| `LLMAAS_TEMPERATURE` | optional `INFERENCE_CHAT_TEMPERATURE` |
| `LLMAAS_EMBEDDING_MODEL` | `INFERENCE_EMBEDDING_MODEL` |
| `LLMAAS_EMBEDDING_DIMENSIONS` | `INFERENCE_EMBEDDING_DIMENSIONS` |

Both token variables are provider generation budgets and may include hidden
reasoning. Hivemind accepts values through **1,000,000**, provided the selected
value remains strictly below the matching context window. Higher values fail
startup during configuration resolution. This does not raise the
provider-response memory boundary: chat bodies are still streamed with identity
encoding and capped independently at 8 MiB before JSON parsing.

Add `INFERENCE_CHAT_PROVIDER=openai-compatible` and
`INFERENCE_EMBEDDING_PROVIDER=openai-compatible` for an identity-preserving
mechanical migration. Then remove **every** `LLMAAS_*` assignment, including
empty, differently cased, or misspelled variants. Hivemind deliberately fails
startup on a mixed family, an unknown inference variable, a partial role, or a
case-colliding spelling; it never fills a split field from legacy values.

Use this maintenance sequence:

1. Stop writers and preserve a mode-`0600` copy of the complete current
   environment outside the repository.
2. Create both complete split-role blocks in a new secret/config revision.
   Validate that the URL and credential really serve each role; a native
   Anthropic composite is not a one-key mechanical migration.
3. Remove all legacy assignments and atomically activate the new revision.
4. Run startup validation, authenticated `system_health`, then the manage-only
   `inference_self_test`. Health discovery never spends chat or embedding
   tokens; self-test is the explicit bounded readiness operation.
5. Run `long_status` for every existing space before allowing writes. If the
   embedding identity changed, keep writers stopped and follow
   [Embedding identity and bounded reindex](#embedding-identity-and-bounded-reindex).
6. Exercise mid consolidation, long extraction/push, and semantic query with
   non-sensitive test data before reopening normal traffic.

Changing from legacy `openai-compatible` identity to the named
`cloud-temple` profile is **not** mechanical, even when URL, model and
dimensions stay the same. Provider identity participates in the Qdrant
fingerprint, so existing collections correctly become `reindex_required`.
Likewise, switching any embedding provider, endpoint, model, evidence, or
dimension requires the explicit maintenance path. Hivemind never rewrites,
truncates, pads, adopts, or deletes vectors automatically.

Rollback before any split-profile vector write may restore the complete legacy
family while the service is stopped. After new writes or a reindex, an
environment-only rollback can itself create identity drift: keep writers
stopped, inspect `long_status`, and use the same bounded procedure. Never mix
families as a temporary rollback technique.

## Exact split configuration

Remove every active `LLMAAS_*` assignment before using one of these blocks.
The legacy and split families intentionally fail when they coexist.

OpenAI:

```dotenv
INFERENCE_CHAT_PROVIDER=openai
INFERENCE_CHAT_API_URL=https://api.openai.com/v1
INFERENCE_CHAT_API_KEY=<secret>
INFERENCE_CHAT_MODEL=gpt-5.6-luna
INFERENCE_CHAT_CONTEXT_WINDOW=131072
INFERENCE_CHAT_MAX_OUTPUT_TOKENS=16384
INFERENCE_EMBEDDING_PROVIDER=openai
INFERENCE_EMBEDDING_API_URL=https://api.openai.com/v1
INFERENCE_EMBEDDING_API_KEY=<secret>
INFERENCE_EMBEDDING_MODEL=text-embedding-3-small
INFERENCE_EMBEDDING_DIMENSIONS=1536
```

Cloud Temple:

```dotenv
INFERENCE_CHAT_PROVIDER=cloud-temple
INFERENCE_CHAT_API_URL=https://api.ai.cloud-temple.com/v1
INFERENCE_CHAT_API_KEY=<secret>
INFERENCE_CHAT_MODEL=Qwen/Qwen3.6-27B-FP8
INFERENCE_CHAT_CONTEXT_WINDOW=131072
INFERENCE_CHAT_MAX_OUTPUT_TOKENS=16384
INFERENCE_EMBEDDING_PROVIDER=cloud-temple
INFERENCE_EMBEDDING_API_URL=https://api.ai.cloud-temple.com/v1
INFERENCE_EMBEDDING_API_KEY=<same-secret>
INFERENCE_EMBEDDING_MODEL=bge-m3:567m
INFERENCE_EMBEDDING_DIMENSIONS=1024
```

Gemini exact stable reference:

```dotenv
INFERENCE_CHAT_PROVIDER=gemini
INFERENCE_CHAT_API_URL=https://generativelanguage.googleapis.com/v1beta/openai
INFERENCE_CHAT_API_KEY=<secret>
INFERENCE_CHAT_MODEL=gemini-3.6-flash
INFERENCE_CHAT_CONTEXT_WINDOW=131072
INFERENCE_CHAT_MAX_OUTPUT_TOKENS=16384
INFERENCE_EMBEDDING_PROVIDER=gemini
INFERENCE_EMBEDDING_API_URL=https://generativelanguage.googleapis.com/v1beta/openai
INFERENCE_EMBEDDING_API_KEY=<same-secret>
INFERENCE_EMBEDDING_MODEL=gemini-embedding-001
INFERENCE_EMBEDDING_DIMENSIONS=3072
```

Native Anthropic chat plus Cloud Temple embeddings:

```dotenv
INFERENCE_CHAT_PROVIDER=anthropic
INFERENCE_CHAT_API_URL=https://api.anthropic.com
INFERENCE_CHAT_API_KEY=<anthropic-secret>
INFERENCE_CHAT_MODEL=claude-sonnet-5
INFERENCE_CHAT_CONTEXT_WINDOW=131072
INFERENCE_CHAT_MAX_OUTPUT_TOKENS=16384
INFERENCE_EMBEDDING_PROVIDER=cloud-temple
INFERENCE_EMBEDDING_API_URL=https://api.ai.cloud-temple.com/v1
INFERENCE_EMBEDDING_API_KEY=<cloud-temple-secret>
INFERENCE_EMBEDDING_MODEL=bge-m3:567m
INFERENCE_EMBEDDING_DIMENSIONS=1024
```

The Anthropic role uses the native Messages API directly: `POST /v1/messages`
with `x-api-key` and `anthropic-version: 2023-06-01`. It does not use a
Bearer header, Chat Completions payload, OpenAI SDK, or compatibility shim.
Leading normalized system messages become the native top-level `system`
field. The separate Cloud Temple role alone receives embedding and query
inputs.

The named profiles intentionally omit `INFERENCE_CHAT_TEMPERATURE`; their exact
wire contract therefore emits no temperature. The generic adapter does expose
and forward that optional setting for operator-composed configurations, so it
is not classified as boundary-rejected. Such a configuration is outside the
named reference profile and its certification evidence. OpenAI, Cloud Temple,
and Gemini use one provider credential for both roles. The
Anthropic composite intentionally uses separate role credentials and does not
inherit either one across roles.

## Accepted and rejected parameters

“Rejected” below means Hivemind's normalized boundary does not expose or
forward the field. It does not claim the upstream API lacks that feature.
Requests that need a rejected field require a reviewed boundary extension;
the adapter never silently mutates or invents one.

| Profile role | Sent fields | Boundary-rejected examples |
| --- | --- | --- |
| OpenAI chat | `model`, `messages`, `max_completion_tokens` | `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Cloud Temple chat | `model`, `messages`, `max_tokens` | `max_completion_tokens`, `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Gemini chat | `model`, `messages`, `max_tokens` | `max_completion_tokens`, `thinking`, `top_k`, `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Native Anthropic chat | `model`, `messages`, `max_tokens`, conditional top-level `system` | `max_completion_tokens`, `thinking`, `top_k`, `top_p`, tools, streaming, penalties |
| All named embeddings | `model`, `input` | `dimensions`, `encoding_format`, `output_dimension`, `output_dtype`, `user` |

The Gemini embedding profile additionally rejects `task_type`; it does not
invent a native-only task classification on the OpenAI-compatible wire.

`INFERENCE_EMBEDDING_DIMENSIONS` is validation metadata and the Qdrant vector
size. It never creates a wire dimension override. A provider response whose
vector length differs fails as `invalid_response`; Hivemind does not truncate,
pad, or rebuild a collection automatically.

Named profiles freeze the model-list operation independently for each role. An
`available` role uses `GET /models`; a successful list must report whether the
exact configured model is present. An observed 404, 405, or 501 records
`discovery=unsupported` with reachable connectivity. An observed timeout is
always an error and is never dynamically reclassified as unsupported.

For Cloud Temple, `unsupported` is instead a route-specific frozen declaration,
not a claim that the provider has no model-list endpoint or that it can never
answer. In two protected exact-main attempts, both chat and embedding model-list
calls failed to complete within the reviewed total deadlines: 15 seconds in run
`30861840408.1` and 60 seconds in run `30868210874.1`. Cloud Temple's dated
public catalogue names the exact chat and embedding models, but P13-4 has no
reviewed source that supplies a response-time or availability contract making
that listing a bounded prerequisite for this workflow.

The identity-bound protected Cloud Temple route therefore omits its four runner
and Graph catalogue calls. This removes the zero-token, pre-egress model-list
guard. As compensating certification evidence after provider egress, the direct
chat and embedding responses must each provider-report the exact configured
model; chat must return the synthetic marker, and embedding must return exactly
two vectors of the frozen 1,024 dimensions. Certification remains incomplete
until every paid check passes. The manifest records `discovery=unsupported` and
the `discovery-unsupported` limitation as the frozen route declaration even
when a later operation fails; those values have no standalone promotion
authority. For this profile they do not describe an observed HTTP method
response or a provider-wide capability fact.

Ordinary operator-composed runtime health continues to use the generic adapter
probe. The protected omission requires a complete ledger whose profile, role,
provider, endpoint, and model match the frozen declaration; partial or
mismatched strict state fails before provider egress. Gemini and every role
declared `available` retain their zero-retry catalogue probes.

For the explicit `gemini` provider only, discovery accepts exactly the bare
configured slug or Google's native resource representation
`models/<configured-slug>`. The dated Google compatibility guide documents
`GET /v1beta/openai/models`, iterating `model.id`, and retrieval by the bare
slug; the native Models API separately documents `models/{model}` resource
names, but the public compatibility guide does not publish a raw list payload.
Accepting those two exact forms removes that response-shape ambiguity without
changing the configured request model. Aliases, suffix matches, other prefixes,
and this normalization for any non-Gemini provider remain forbidden.

The frozen catalogue constrains named-profile construction, deterministic
conformance, and certification manifests. The general environment resolver
does not select a named profile id; an operator-composed configuration remains
governed by the generic adapter contract rather than silently inheriting this
catalogue.

Google REST 429 responses remain fail-safe: only the closed structured reason
`RATE_LIMIT_EXCEEDED` together with a valid `Retry-After` of at most five
seconds can authorize the one bounded retry, and only when the explicit
provider identity is `gemini`. Existing code/type candidates retain their
historical exact, case-sensitive matching for every provider. Exact quota codes
or a typed `google.rpc.QuotaFailure` map to terminal `quota_exhausted` without
reading quota identifiers; ambiguous `RESOURCE_EXHAUSTED` maps to non-retryable
`rate_limited`. Provider messages and quota identifiers never enter logs,
errors, or evidence.

## Capability and evidence states

The active P13-4 evidence vocabulary is:

| State | Meaning |
| --- | --- |
| `compatible` | Complete deterministic adapter/profile and MID/long/Graph evidence passes on the current source. |
| `live-verified` | `compatible`, plus one real chat and one real embedding request passed with zero retry on the same exact source SHA. |
| `blocked` | A prerequisite or live role failed. Deterministic compatibility is unchanged. |

Role-level `discovery=unsupported` remains an adapter capability declaration;
it is distinct from the three active profile-evidence states.

Cloud Temple and Gemini have deterministic `compatible` evidence. A Cloud
Temple minimal manifest is required for v1.4.0 and is valid only for its exact
profile, configured models, adapter, source SHA, price schedule, and live run.
Gemini has no v1.4.0 live-verification claim: its qualification is deferred
after its recorded failure and needs a separately authorized future source
change. Any live claim requires a new exact-SHA run; there is no cross-SHA
freshness window.

A green chat cannot hide a failed embedding: both role records and an exact
two-invocation total are required for `live-verified`. Conversely, two green
direct roles do not replace the deterministic `short_note` → `mid_consolidate`
→ `long_push` → `long_query` proof. The two evidence layers are deliberately
small and complementary.

The deterministic suite uses
`tests/fixtures/p13_provider_certification_v1.json`, a fabricated bilingual
fixture bound to SHA-256
`2986dc49a51d29df2484b17c4721fa6eda2ad82423ed5e8c60f73544e43b59d3`.
It makes no external request and uses no provider credential.

## Readiness without health-side spending

Public `/health` and authenticated `system_health` perform discovery only.
They never send a chat or embedding request. A successful model listing proves
connectivity/discovery, not authentication, quota, or inference readiness.

An operator with `manage` permission may explicitly call the hidden,
zero-argument `inference_self_test` tool. It tests only the process-frozen
configured roles, accepts no provider/model/endpoint/key/prompt input, uses
fixed synthetic content, issues at most one zero-retry request per role, and
caps chat output at eight tokens. It returns only normalized role readiness,
safe model/usage metadata, correlation identifiers, and timestamps; completion
text and vectors are discarded.

One operation is single-flight per serving event loop and exact role-profile
fingerprint. Its safe result is cached for five minutes and repeated calls
during that cooldown do not issue another paid request. Authenticated
`system_health` may project a matching fresh result as
`readiness=ready|not_ready` and `evidence=inference` for a configured role; a
non-configured role remains `evidence=none`. It never starts or refreshes the
test. Public health never projects that cache. Missing, expired, or
changed-profile evidence is `readiness=unknown`.

Self-test may spend provider budget. Grant `manage` narrowly and invoke it only
under the operator's provider/cost policy; its bounded shape is not a free-call
or zero-cost claim.

## Embedding identity and bounded reindex

Startup validates and freezes the inference configuration. It does not scan
every lazy per-space Qdrant collection and therefore does not claim global
collection readiness. `long_status`, ingestion, query, backup/restore, and the
explicit maintenance operation enforce the exact per-space embedding identity.

For each existing space after migration:

1. call `long_status(space_id)` and inspect `embedding_collection`;
2. keep writers stopped when the state is `reindex_required`;
3. use one manage-authorized `long_reindex(space_id)` call in an explicit
   maintenance window;
4. require its bounded result to be `status=ok`, `phase=verified`,
   `activated=true`, and `active_state=ready`;
5. call `long_status` again, then perform one non-sensitive ingest/query smoke.

Reindex reads retained Graph/S3 source, builds an attributable shadow, checks
exact document/chunk/vector accounting and identity, and makes one atomic
active-alias switch only after validation. Failure before the switch leaves the
old target active; uncertainty after the switch never attempts rollback. The
previous target and abandoned shadows remain intact. This is a single-process,
maintenance-mode path, not online HA migration, crash resumption, or cleanup.

## Minimal live verification

The active P13-4 live operation is maintainer-controlled, manual, and
synthetic-data-only. It runs through a private protected workflow on an
already-registered self-hosted Linux x86 runner selected by the exact labels
`self-hosted`, `Linux`, `X64`, and `gle-ghrunners02`. Runner registration and
host lifecycle remain operator-owned and outside this minimal workflow.
Private workflow paths, environment names, secret-variable names, and raw
operator diagnostics are not part of this public contract.

Before a provider secret enters the selected step, the workflow requires:

- dispatch from the exact current `main` SHA;
- a successful exact-attempt `Private CI` run on that SHA;
- locked dependency installation;
- an allowlisted Cloud Temple or Gemini profile; and
- a source-computed technical cost ceiling no greater than the explicit
  operator maximum, which itself cannot exceed USD 1.

The credential-bearing step performs exactly one chat invocation capped at 8096
output tokens and one single-input embedding invocation. Both use
`retry_policy="none"`; discovery, health, fallback, MID, long, Graph, Docker,
Compose, and provider-side writes are absent. The current conservative ceilings
are USD 0.261376 for Cloud Temple and USD 0.061508 for Gemini, using the dated
price schedules recorded in source; the workflow default authorizes at most
USD 0.27. Reported usage above the source-owned ceilings blocks the manifest.
A role failure stops immediately. The
first RCA-grounded correction may be tried once on the same contract; a second
failure remains `blocked` instead of starting another loop.

The active schema is `hivemind.provider-live-verification.v1`. It records only
the exact profile/models, adapter/provider identifiers, SHA, deterministic and
live run identities, dated price-schedule id, authorized and technical cost
ceilings, role invocation counts, normalized role facts, bounded safe usage,
cleanup status, dimensions, timestamp, and safe normalized failure fields. It
has no field for an endpoint, credential, prompt, completion, vector, provider
body, exception text, bank content, source code, or project data. The temporary
manifest is uploaded even on a provider failure and then removed from the
runner.

### Historical protected full-stack route (diagnostic only)

The schema-v3/v4 route below documents earlier P13-4 engineering and retained
diagnostic tooling. As of 2026-08-04 it is not the active release gate, does not
establish any profile state, and is not required to close #264.

Schema v3 remains deliberately non-promotable: it cannot read a credential,
start the certification stack, make a paid call, or derive `certified`. The
schema-v3 limitations `certification-contract-incomplete` and
`token-ceiling-unproven` therefore remain explicit and blocking. The
schema-v4 successor is a separate, strict route limited to
`cloud-temple-reference` and `gemini-reference`. Its private operator route
requires a controller-provisioned execution environment; VM cleanliness,
one-job registration, and destruction are operator controls, not manifest or
GitHub jobs-API facts. The route requires the exact protected `main` SHA plus a
successful deterministic CI run on that SHA and gives no dispatch path to
ordinary PR or push CI. Before provider egress, a bounded preparation phase
whose step environment contains no provider credential builds the exact
repository-owned images, pulls the digest-pinned runtime images, proves the
complete local image inventory, and leaves no project container or source-tree
change. The selected credential-bearing step can then use only those local
images: it may neither build nor pull, so missing preparation blocks before
provider egress. Trust in the self-hosted execution environment is established
separately by the operator before job registration; the in-job preparation and
hygiene checks are not a credential-isolation boundary against a compromised
runner. The trusted job may receive every protected provider secret referenced
by its mutually exclusive paid steps; only the selected step environment
projects one of them.

Every allowed OpenAI-compatible `GET /models`, `POST /chat/completions`, and
`POST /embeddings` attempt reserves its role, request, conservative input-token
upper bound, and chat output reservation in one shared SQLite transaction
before transport. The runner, Core, and Graph Memory use the same exact-run,
profile, SHA, provider, and model-bound ledger. Reservations are never refunded
for retries, failures, cancellation, or timeouts. Atomic sealing refuses
unsettled work or any ceiling violation, prevents later provider egress, and
supplies the manifest's aggregate totals. Missing or partial strict-mode state
fails closed; normal runtime behavior is unchanged when certification mode is
absent. A refused ceiling transaction commits a durable poison before raising,
strict mode disables adapter retries, and protected readiness polls `/live`
rather than the provider-probing `/health` endpoint. The exact journey inventory
is profile-bound: `cloud-temple-reference` requires four chat and four embedding
attempts, while `gemini-reference` requires six chat and six embedding attempts.
Both reserve exactly 4,096 chat output tokens. The independent hard ceilings
remain 12 chat requests, 20 embedding requests, 125,000 chat JSON bytes/tokens,
50,000 embedding input tokens, and 4,096 protected chat output tokens. Ordinary
schema-v3 certification evidence remains capped at 4,000 chat output tokens.

The allowlisted price sources are
[`https://openai.com/api/pricing/`](https://openai.com/api/pricing/) for
OpenAI;
[`https://mistral.ai/pricing/api/`](https://mistral.ai/pricing/api/) for
Mistral;
[`https://platform.claude.com/docs/en/about-claude/pricing`](https://platform.claude.com/docs/en/about-claude/pricing)
for Anthropic;
[`https://www.cloud-temple.com/en/our-public-rates/`](https://www.cloud-temple.com/en/our-public-rates/)
for Cloud Temple; and
[`https://ai.google.dev/gemini-api/docs/pricing`](https://ai.google.dev/gemini-api/docs/pricing)
for Gemini. A live composite manifest requires both distinct provider
entries. Changing an evidence source requires a reviewed code change; an
arbitrary URL cannot enter a manifest.

On the 2026-08-03 evidence date, Cloud Temple published EUR 1.80 per million
input tokens, EUR 8.00 per million generated output tokens, and EUR 8.00 per
million reasoning tokens. Google's standard paid rates were USD 1.50 per
million input tokens and USD 7.50 per million output tokens, including
thinking, for Gemini 3.6 Flash, plus USD 0.15 per million input tokens for
`gemini-embedding-001`. These dated figures are evidence, not authorization to
spend; every live run still requires its separately confirmed bounded estimate
and maximum cost.

The executable schedule charges Cloud Temple input and embedding reservations
at EUR 1.80/M, charges every output reservation at both EUR 8/M generated and
EUR 8/M reasoning, then applies a deliberately conservative USD 2 per EUR
conversion ceiling. The 4,096-token protected output inventory makes the exact
Cloud Temple envelope USD 0.761072. The corresponding Gemini Standard envelope
is USD 0.225720. Before provider egress, the entered estimate must cover the
applicable complete envelope and
remain within the separately confirmed maximum of USD 1. After sealing, the
manifest records a recalculated reservation cost that must exactly match the
same versioned schedule and remain below the estimate. Current taxes, account
plan, provider billing state, and actual exchange rate must still be checked at
dispatch time; these bounds authorize nothing by themselves.

The 2026-08-03 evidence snapshot records `gemini-3.6-flash` as stable since
2026-07-21 with provider limits of 1,048,576 input and 65,536 output tokens,
while Hivemind deliberately retains its smaller ceilings. It records
`gemini-embedding-001` as the stable text-only 3,072-dimensional model released
2025-07-14 and currently scheduled through 2028-05-14. The paid Gemini terms
and logging policy are provider declarations: paid-service prompts/responses
are not used for product improvement, but limited abuse-monitoring retention
and optional project logging may still apply. Certification therefore uses
synthetic content, records the declared retention boundary, and never claims
provider-side deletion.

Passing evidence requires proven cleanup of every run-scoped synthetic
resource. Runner loss or ambiguous absence blocks certification. No
provider-side retention deletion is claimed.

## Historical schema-v3/v4 manifest contract

This section is retained only so old artifacts and source remain explainable.
It does not describe the active `hivemind.provider-live-verification.v1`
manifest above.

The certification artifact contains one canonical JSON manifest and no logs or
raw response artifact. Its allowlisted content includes:

- schema and capability-matrix ids;
- profile plus role-scoped provider, adapter, endpoint fingerprint, and exact
  configured/provider-reported model identities;
- separate chat, embedding, mid, long, and cleanup results;
- full source SHA, fixture id/hash, run id/URL, execution time, and live expiry;
- the sealed shared-ledger request/input/output ceilings and aggregate
  observations, including provider-reported safe usage when present, one
  canonical price source per distinct provider, pricing schedule id,
  recalculated sealed-reservation cost, conservative full-ceiling estimate,
  and authorized maximum;
- the deterministic run URL and exact attempt, suite hash, synthetic-data assertion,
  region/data-boundary, license and retention declarations, dimensions,
  duration, coarse latency, and proxy-path evidence;
- safe normalized error categories and allowlisted limitation ids.

The paid runner writes only a non-promotable factual candidate. After teardown,
a secret-free phase purges the mutable virtual environment and bytecode,
rebinds the clean source tree, then runs the finalizer with an isolated system
interpreter. The finalizer re-fetches the exact attempt-scoped Private CI and
current paid-run records, strictly scans the candidate, publishes the unchanged
canonical facts durably, and exposes the exact byte digest. The sole uploadable
artifact name binds profile, source SHA, paid run id/attempt, and that digest.
It does not mint an attestation.

Only `scripts/verify_provider_certification.py` is a supported certifying
reader. After the paid run completes, it performs authenticated GitHub API
readback pinned to `github.com`: the private-repository `main` ref before and
after evidence collection, both exact attempts, the paid attempt's dedicated
job assignment, and the paid run's artifact inventory. It refuses
if `main` changes during readback and requires both runs to be successful, the
single certification job to match the required dedicated Linux ARM64
group/name/labels. It also requires current authenticated group policy to allow
only the private repository and exact protected workflow, to forbid public
repositories, and to contain zero registered runners after the job. Exactly one
non-expired artifact must commit to the supplied bytes and belong to the private
workflow. These GitHub readbacks still do not attest VM freshness, one-job
registration, or disk destruction. The verifier then downloads that immutable
artifact by id, checks the GitHub size and SHA-256, safely extracts exactly one
bounded manifest member, and requires a byte-for-byte match. Status, freshness,
release scope, validity, deterministic conformance, and redaction are then
derived; none is serialized authority and the caller cannot supply its own
notion of the current source. The empty restricted group is retained for as
long as any retained artifact must remain verifiable; deletion or policy drift
makes later verification fail closed.

The schema has no field capable of storing a credential, raw endpoint, prompt,
completion, vector, provider error body, MCP response, or container log.
Deterministic evidence cannot carry a live run URL or cost fields and is capped
at `compatible`. No schema-v3 `protected-live` manifest computes to
`certified`. Raw schema-v4 JSON is always experimental. Authenticated readback
can compute current `certified` only when every role, complete-Hivemind,
cleanup, deterministic-run, paid-run, artifact identity/redaction, and sealed
technical-budget requirement is green. `discovery-unsupported` and
`usage-partially-reported` are disclosure-only; neither permits a missing or
exceeded technical bound.

The protected workflow never runs automatically. Each paid dispatch still
requires a separate human GO naming the exact SHA, selected profile, exact
deterministic CI run attempt, current
price evidence, conservative estimate, and maximum cost. Implementing or
testing this repository locally does not imply that authorization, and route
availability alone is not certification evidence. Checking out or exporting
this revision creates no new `certified` manifest or paid call.

## v1.4.1 release preparation boundary

The runtime identity is now `1.4.1` for the separately assembled private RC
candidate. That identity does not create a Git tag, public image, deployment,
GitHub Release, or provider call. The immutable private candidate suffix stays
only in `rc-v1.4.1-rcN`, where `N >= 1`; it is not part of the runtime or
package version.

A later release-cut decision must start from one exact final source SHA and
recheck all of the following:

1. deterministic parity, the complete private and staged-public suites, public
   audit, documentation links, Compose renders, and both image builds are green
   on that SHA;
2. Cloud Temple has a passing minimal manifest with
   `profile_status=live-verified` on that same SHA; Gemini is deliberately not
   a v1.4.1 live-verification or release prerequisite and remains `compatible`;
3. the Cloud Temple live manifest retains exact request inventory, zero retry,
   price-schedule, cost ceiling, redaction, model identity, and dimension
   validity, while deterministic CI remains green for complete Hivemind;
4. the assembled source has a fresh favorable independent review and all
   findings are adjudicated; and
5. the maintainer gives separate explicit approval for the exact tag, images,
   release publication, and any deployment.

Cloud Temple live verification, a private-image dispatch, and the digest-pinned
smoke each require their own explicit human GO on the frozen final SHA. Gemini
is deliberately not a v1.4.1 live-verification or release prerequisite; its
profile remains `compatible` and a later qualification needs a new source SHA
and its own authorization. Reusing or replacing an existing tag is never an
execution step.

## Deferred Mistral tooling (not v1.4)

`mistral-reference` remains in the general catalogue and protected runner for
a post-v1.4 provider wave. Its deterministic fixtures are experimental
groundwork, not v1.4 compatibility, certification, or release evidence.

| Profile | Chat | Embeddings | Dimensions | Endpoint |
| --- | --- | --- | ---: | --- |
| `mistral-reference` | `mistral-small-2603` | `mistral-embed` | 1024 | `https://api.mistral.ai/v1` |

```dotenv
INFERENCE_CHAT_PROVIDER=mistral
INFERENCE_CHAT_API_URL=https://api.mistral.ai/v1
INFERENCE_CHAT_API_KEY=<secret>
INFERENCE_CHAT_MODEL=mistral-small-2603
INFERENCE_CHAT_CONTEXT_WINDOW=131072
INFERENCE_CHAT_MAX_OUTPUT_TOKENS=16384
INFERENCE_EMBEDDING_PROVIDER=mistral
INFERENCE_EMBEDDING_API_URL=https://api.mistral.ai/v1
INFERENCE_EMBEDDING_API_KEY=<secret>
INFERENCE_EMBEDDING_MODEL=mistral-embed
INFERENCE_EMBEDDING_DIMENSIONS=1024
```

The Mistral chat wire sends `model`, `messages`, and `max_tokens`; embeddings
send only `model` and `input`. Relevant future-wave references are the
[chat endpoint](https://docs.mistral.ai/api/endpoint/chat),
[embeddings endpoint](https://docs.mistral.ai/api/endpoint/embeddings), and
[models endpoint](https://docs.mistral.ai/api/endpoint/models).
