# Inference providers and configuration

Hivemind separates chat (mid consolidation and long extraction) from embeddings
(long ingestion and semantic query). Choose one complete configuration for each
role. Anthropic does not provide native embeddings; its native chat adapter
must be paired with a separate embedding provider.

The four reference profiles introduced in Hivemind 1.4 remain the compatibility
baseline for 1.5. They are exact, frozen examples, not a recommendation to change
a working deployment or a claim about every model a provider offers. The
deployment example in `.env.example` is a separate profile; see
[Exact split configuration](#exact-split-configuration) before copying settings.

## Current evidence snapshot

The evidence states are `compatible`, `live-verified`, and `blocked`.
Deterministic tests establish compatibility with Hivemind's shared inference
boundary; they do not call a hosted provider. Live verification, when available,
is separate and applies only to its exact source, models and configuration.
This guide does not imply certification, provider availability or account
entitlement merely because a profile appears in the table.

| Reference profile | Deterministic capability | Live evidence |
| --- | --- | --- |
| `cloud-temple-reference` | compatible | separate exact-release evidence required for a live claim |
| `openai-reference` | compatible | no live claim in this guide |
| `anthropic-cloud-temple-reference` | compatible | no live claim in this guide |
| `gemini-reference` | compatible | no live claim in this guide |

Check the provider's current model availability and pricing for your account.
Use non-sensitive data to verify your configured chat and embedding roles
before entrusting a project to them. Keep credentials out of logs and support
reports.

## Functional parity ledger

Every `compatible` cell means the production consumer path and the exact
profile's deterministic fixture pass together. It does not mean a real provider
was called. Health remains discovery-only; a live smoke checks hosted
reachability and response normalization, not the complete functional matrix.

| Reference profile | Mid consolidation | Long extraction | Embeddings/query | Probes/health | Proxy + safe errors | Model/usage/correlation observability | Active live verification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `cloud-temple-reference` | compatible | compatible | compatible, 1024 dimensions | compatible | compatible | compatible | separate exact-release evidence required for a live claim |
| `openai-reference` | compatible | compatible | compatible, 1536 dimensions | compatible | compatible | compatible | no live claim in this guide |
| `anthropic-cloud-temple-reference` | compatible, native Anthropic chat | compatible, native Anthropic chat | compatible, Cloud Temple, 1024 dimensions | compatible per role | compatible per role | compatible per role | no live claim in this guide |
| `gemini-reference` | compatible | compatible | compatible, 3072 dimensions | compatible | compatible | compatible | no live claim in this guide |

The composite's separate embedding credential/provider is required, not a
fallback. A missing role becomes `unsupported`. The deterministic journey
covers `short_note` → `mid_consolidate` → `long_push` → `long_query`;
two direct role checks alone do not establish that complete compatibility.

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

Cloud Temple frozen reference:

The named reference below retains its frozen Qwen3.6 model. The deployment
recipe in `.env.example` instead selects Qwen3.8-27B with low effort and its
operator-selected limits. Its split chat block uses `openai-compatible`, because
the named `cloud-temple` provider does not advertise reasoning-effort support.
This deployment example does not redefine or certify the named reference.

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
| OpenAI chat | `model`, `messages`, `max_completion_tokens`, optional `reasoning_effort` | `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Cloud Temple chat | `model`, `messages`, `max_tokens` | `max_completion_tokens`, `reasoning_effort`, `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Gemini chat | `model`, `messages`, `max_tokens` | `max_completion_tokens`, `reasoning_effort`, `thinking`, `top_k`, `top_p`, `stop`, `seed`, `tools`, `stream`, `n`, penalties |
| Native Anthropic chat | `model`, `messages`, `max_tokens`, conditional top-level `system` | `max_completion_tokens`, `reasoning_effort`, `thinking`, `top_k`, `top_p`, tools, streaming, penalties |
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

For the frozen Cloud Temple reference, `unsupported` is a route-specific
discovery declaration, not a claim that the provider has no model-list endpoint.
Its verification relies on exact provider-reported model identities and embedding
dimensions instead of a successful catalogue listing. This limitation must
remain explicit in any corresponding evidence; it does not establish provider
availability on its own.

Ordinary operator-composed runtime health continues to use the generic adapter
probe. The frozen catalogue constrains reference-profile conformance; it does
not silently override an operator-composed configuration.

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

| State | Meaning |
| --- | --- |
| `compatible` | Complete deterministic adapter/profile and mid/long/Graph checks pass for the source. |
| `live-verified` | `compatible`, plus one real chat and one real embedding request passed with zero retry on the same exact source. |
| `blocked` | A prerequisite or live role failed; deterministic compatibility is unchanged. |

Role-level `discovery=unsupported` is an adapter capability declaration,
distinct from these evidence states. A green chat result cannot hide a failed
embedding result. Evidence for a different source, model or profile is not
automatically valid for your configuration. Neither checking out a release nor
running deterministic tests starts a paid provider operation.

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

A release verification record, when available, uses the
`hivemind.provider-live-verification.v1` schema. It describes one chat invocation
and one single-input embedding invocation, both with synthetic data and zero
retry, bound to the exact source and configured models. It records normalized
results, safe usage, dimensions and explicit cost limits, not prompts,
completions, vectors or credentials.

This bounded hosted check is different from the operator
[`inference_self_test`](#readiness-without-health-side-spending) and does not
replace a test of your own deployment. Historical engineering attempts and
internal execution infrastructure are not configuration instructions or
evidence of current service availability.

## Cost and troubleshooting

- Check current provider pricing and account limits before enabling inference.
  The links in [Exact profiles](#exact-profiles) are starting points, not frozen
  billing promises. Configure token budgets suitable for your workload.
- A discovered model is not proof of usable credentials, quota or successful
  inference. Use the explicit bounded self-test, then a non-sensitive end-to-end
  test in a disposable space.
- Consolidation has its own per-batch retry policy. With the default timeout
  and all retries plus a model correction, generation and waits can reach
  **2 h 38 min per batch**, before auxiliary work. Calls may be billed even when
  they time out and return no usage. See the
  [consolidation tool reference](MCP_TOOLS_SPEC.md).
- Keep `LLMAAS_*` and split `INFERENCE_*` families separate; do not solve a
  failed startup by mixing partial configurations.
- On `reindex_required`, keep writers stopped and follow the
  [bounded reindex procedure](#embedding-identity-and-bounded-reindex).
  Do not delete the old collection as a troubleshooting shortcut.
- Report the Hivemind version, adapter/model names, normalized error category,
  relevant non-secret limits and a minimal synthetic reproduction. Do not post
  credentials, private endpoints, source notes or bank contents. See
  [Support](../SUPPORT.md).

## Experimental Mistral profile

`mistral-reference` remains experimental reference tooling. It is not part of
the four-profile compatibility baseline and carries no live-verification or
certification claim here. Its presence does not promise a release date.

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
send only `model` and `input`. Relevant provider references are the
[chat endpoint](https://docs.mistral.ai/api/endpoint/chat),
[embeddings endpoint](https://docs.mistral.ai/api/endpoint/embeddings), and
[models endpoint](https://docs.mistral.ai/api/endpoint/models).
