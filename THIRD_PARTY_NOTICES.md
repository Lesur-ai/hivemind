# Third-Party Notices

Hivemind is licensed under the Apache License, Version 2.0 (see [`LICENSE`](LICENSE)).
This file records third-party source vendored into this repository, with its
provenance and license, as required by the Apache-2.0 redistribution terms and
the embedded-runtime provenance contract (ADR-0019).

---

## Graph Memory (embedded `long` runtime) — `services/graph-memory/`

The mandatory embedded `long` ontology / knowledge-graph engine (ADR-0019) is
vendored from the upstream **Graph Memory / MCP Memory Service** project.

| Field | Value |
| --- | --- |
| Upstream repository | https://github.com/cloud-temple/graph-memory |
| Vendored commit (pinned) | `ae9afb0b95d449b68a8fb3ca3e70674b8f26eeb8` |
| Upstream release | v3.2.0 |
| Import date | 2026-06-29 |
| Upstream license | Apache License, Version 2.0 (see [`services/graph-memory/LICENSE`](services/graph-memory/LICENSE)) |
| Copyright | Copyright (c) Cloud Temple |
| Hivemind integration | Imported and locally adapted for the embedded runtime |

Both Hivemind and the upstream project are licensed under Apache-2.0, so this
vendoring is license-compatible. The upstream `LICENSE` is preserved verbatim
inside the vendored tree.

### Vendored subset (minimal runtime)

Only the source required to build and run the embedded long runtime is vendored;
this is **not** a full mirror of the upstream repository:

- `Dockerfile` — locally hardened service image build; see Local modifications.
- `requirements.txt` — locally pinned direct Python dependency input (including
  `neo4j`, `qdrant-client`, and `boto3`); see Local modifications.
- `requirements.lock` — Hivemind-generated, hash-locked complete resolution;
  this file is not an upstream artifact.
- `VERSION` — upstream version marker `3.2.0` (verbatim).
- `LICENSE` — upstream Apache-2.0 license (verbatim).
- `ONTOLOGIES/` — bundled ontology YAML files (verbatim).
- `src/mcp_memory/` — the full runtime package, including `server.py`, `core/`,
  `auth/`, `tools/`, and `static/` (the admin console assets mounted by the
  service's `StaticFilesMiddleware`). This is distinct from Hivemind's own
  `src/live_mem/static/` (including `src/live_mem/static/fonts/`; see
  "Vendored fonts" below) — that tree is Hivemind-owned, not
  inherited from the `services/graph-memory/` import.
- `.env.example` — configuration reference (see Local modifications below).

### Excluded from the import (not vendored)

The following upstream paths are intentionally **not** vendored because they are
not part of the runtime, or because Hivemind provides its own equivalent:

- The two upstream Git submodules `product_sheets/` and `docs/` (and
  `.gitmodules`) — Hivemind vendors no submodule for the runtime source.
- `screenshoot/`, `DESIGN/` — non-runtime documentation/media (~4.7 MB).
- `scripts/`, `.github/` — upstream tooling/CI.
- Upstream `README*.md`, `CHANGELOG.md`, `.gitignore`.
- Upstream `docker-compose.yml` — Hivemind's root `docker-compose.yml` owns the
  embedded topology; the upstream compose would otherwise re-expose host
  ports for Neo4j/Qdrant.
- Upstream `waf/` — Hivemind ships its own WAF; the embedded service stays on the
  internal Docker network behind Hivemind's WAF, with no public route.

No upstream runtime import path reaches an excluded directory.

### Local modifications

Modifications applied to the vendored tree are tracked here so the import can be
refreshed against a future upstream release:

- **`.env.example`** — rewritten as an internal-runtime reference with blank
  credentials and an explicit warning that agents must use Hivemind's public
  endpoint. Root Compose remains the supported configuration owner.

- **`Dockerfile`, `requirements.txt`, `requirements.lock` — immutable build
  inputs.** The base image is pinned by patch tag and multi-architecture digest;
  the image installs no compiler or `curl`; its healthcheck uses the Python
  standard library. Direct requirements are exact pins and the complete
  universal resolution is generated with `uv pip compile --generate-hashes`.
  Docker installs it with `pip --require-hashes`; every dependency update
  therefore requires an explicit lock regeneration, review, and image build.

- **Unified token authority.** The embedded
  service validates the SAME tokens as Hivemind by reading Hivemind's S3 token
  store instead of its own Neo4j store:
  - **`src/mcp_memory/auth/s3_token_validator.py`** — NEW. Import-light validator
    that reads Hivemind's `_system/tokens.json` from the shared S3 bucket on
    EVERY call (no positive cache that could grant on stale data — a revoked,
    deleted, or unreadable store fails closed immediately). Signature mode reads
    Hivemind's own `S3_SIGNATURE_MODE` env directly (default `dual`, `sigv4`
    opt-in). Normalizes the `sha256:` hash prefix, reads `space_ids`/`permissions`
    (projecting Hivemind `manage` → `write`), presents `memory_ids=[]`
    (mono-tenant), and fails closed on missing/unknown/revoked/expired as well as
    on corrupt token fields (non-boolean `revoked`, unparseable `expires_at`).
- **`src/mcp_memory/auth/__init__.py`** — made the heavy re-exports lazy
  (PEP 562 `__getattr__`) so the import-light auth submodules load without
  pulling `token_manager` (→ neo4j). Public attribute API preserved.
- **`src/mcp_memory/core/__init__.py`** — made the heavy core-service
  re-exports lazy for the same reason. Importing the S3 storage adapter no
  longer initializes the Neo4j or OpenAI clients; the public attribute API is
  preserved.
- **`src/mcp_memory/core/storage.py`** — replaced the upstream hard-coded
  SigV2 data client with Hivemind's shared `S3_SIGNATURE_MODE` contract:
  `dual` retains SigV2 data operations plus SigV4 metadata operations for Dell
  ECS, while `sigv4` uses SigV4 throughout for MinIO, AWS S3, and modern
  compatible providers.
  - **`src/mcp_memory/auth/middleware.py`** — fail-closed corrections: the live
    auth path and the web-login path validate via the S3 validator (Neo4j
    `token_manager.validate_token` removed from the live path; `token_manager`
    stays vendored-but-dormant for admin CRUD); `LOCALHOST_AUTH_BYPASS` default
    flipped `true` → `false`; the wildcard `access-control-allow-origin: *`
    header removed; `current_auth` is reset after each request (no
    cross-session contextvar bleed).
  - **`src/mcp_memory/auth/context.py`** — fail-closed: the permission checks
    (`check_memory_access`, `check_admin_permission`, `check_write_permission`)
    now DENY when there is no auth context (was: allow); and
    `get_allowed_memory_ids()` returns an explicit `DENY_ALL` sentinel on no-auth
    (distinct from the admin/bootstrap `None`) so the list helpers cannot
    fail open.
- **`src/mcp_memory/server.py`** — `memory_list` and `backup_list` deny on the
  `DENY_ALL` sentinel (no-auth list fail-closed; import updated).
- **`src/mcp_memory/server.py` — embedded identity.** `system_about` describes
  this component as Hivemind's derived, non-authoritative long runtime, makes
  no multi-tenant claim, links to the Hivemind repository while retaining an
  explicit upstream attribution, and withholds service/configuration details
  from unauthenticated callers.
  - **`src/mcp_memory/config.py`** — added `hivemind_tokens_s3_key`
    (default `_system/tokens.json`). The S3 signature mode for the token-store
    read is read from Hivemind's own `S3_SIGNATURE_MODE` env by the validator
    (single source of truth) — no separate GM-only knob.

- **Destructive-tool write gate.**
  - **`src/mcp_memory/server.py`** — `document_delete` now requires
    `check_write_permission` (after `check_memory_access`, before any
    deletion), aligning it with the other mutating tools (`memory_ingest`,
    `memory_update`, ...). Read-only tokens can no longer delete documents.

- **Outbound proxy (`PROXY_URL`) support (Hivemind P12-3, #268).** The
  vendored baseline had no outbound-proxy support; the embedded service now
  honors the same Hivemind `PROXY_URL` contract as the core, with a static
  per-client classification (never runtime DNS/IP heuristics) and no
  `HTTP_PROXY`/`HTTPS_PROXY` export that could reroute unclassified
  libraries (Qdrant, Neo4j tooling, urllib healthchecks):
  - **`src/mcp_memory/core/egress.py`** — NEW. Import-light egress helpers:
    botocore proxies mapping, owned proxied `httpx.AsyncClient` factory,
    log-safe proxy-origin rendering, and proxy-secret redaction (userinfo and
    query strings stripped from outward messages).
  - **`src/mcp_memory/config.py`** — added `proxy_url` with the core's exact
    normalization (strip, empty → unset) and accepted schemes
    (`http://`/`https://`); an invalid value refuses service startup
    (fail-closed).
  - **`src/mcp_memory/core/extractor.py`**, **`core/embedder.py`** — when
    `PROXY_URL` is set, an owned proxied transport is injected into
    `AsyncOpenAI` (extraction, Q&A, embeddings, provider-health probes,
    including retry attempts); it is closed on constructor failure and at
    service shutdown via `close()`; startup logs show only the proxy origin.
  - **`src/mcp_memory/core/storage.py`** — both document-storage botocore
    configs (SigV2 data / SigV4 metadata, and the single `sigv4`-mode client)
    carry the proxies mapping; outward storage exceptions are redacted
    (botocore `ProxyConnectionError` embeds the raw proxy URL).
  - **`src/mcp_memory/auth/s3_token_validator.py`** — the per-call
    token-store reader carries the same proxies mapping; a proxy outage keeps
    the existing fail-closed deny.
  - **`src/mcp_memory/server.py`** — `system_health` messages are redacted;
    an outermost ASGI lifespan shim closes the owned inference transports on
    service shutdown.

---

## Vendored fonts (Hivemind admin console) — `src/live_mem/static/fonts/`

The admin console visual system vendors prebuilt WOFF2
**latin subset** font binaries, downloaded once at vendoring time from Google
Fonts and committed unmodified — no runtime CDN fetch (`docs/SECURITY.md`
§3.5; CSP `default-src 'self'`), no self-subsetting or renaming. All three
families are licensed under the SIL Open Font License, Version 1.1. Following
this file's existing convention of linking to an in-repo/upstream license file
rather than reproducing full license text per entry (see the Graph Memory
section above, which links
[`services/graph-memory/LICENSE`](services/graph-memory/LICENSE)), the OFL 1.1
text for each family is not duplicated here — see each family's upstream
repository (linked below), which carries its `OFL.txt` verbatim.

### Space Grotesk

| Field | Value |
| --- | --- |
| Files vendored | `space-grotesk-600.woff2`, `space-grotesk-700.woff2` |
| Upstream repository | https://github.com/floriankarsten/space-grotesk |
| Upstream copyright (verbatim from `OFL.txt`) | `Copyright 2020 The Space Grotesk Project Authors (https://github.com/floriankarsten/space-grotesk)` |
| License | SIL Open Font License, Version 1.1 (full text at the upstream repository's `OFL.txt`; also mirrored at http://scripts.sil.org/OFL) |
| Reserved Font Name declared? | No |
| Modifications | None — WOFF2 latin subset (`U+0000–00FF` plus `œ`/`Œ`, curly quotes, dashes, ellipsis) as distributed by Google Fonts, unmodified, no self-subsetting |

### Hanken Grotesk

| Field | Value |
| --- | --- |
| Files vendored | `hanken-grotesk-400.woff2`, `hanken-grotesk-500.woff2`, `hanken-grotesk-600.woff2` |
| Upstream repository | https://github.com/marcologous/hanken-grotesk |
| Upstream copyright (verbatim from `OFL.txt`) | `Copyright 2021 The Hanken Grotesk Project Authors (https://github.com/marcologous/hanken-grotesk)` |
| License | SIL Open Font License, Version 1.1 (full text at the upstream repository's `OFL.txt`; also mirrored at http://scripts.sil.org/OFL) |
| Reserved Font Name declared? | No |
| Modifications | None — WOFF2 latin subset (`U+0000–00FF` plus `œ`/`Œ`, curly quotes, dashes, ellipsis) as distributed by Google Fonts, unmodified, no self-subsetting |

### JetBrains Mono

| Field | Value |
| --- | --- |
| Files vendored | `jetbrains-mono-400.woff2`, `jetbrains-mono-600.woff2` |
| Upstream repository | https://github.com/JetBrains/JetBrainsMono |
| Upstream copyright (verbatim from `OFL.txt`) | `Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono)` |
| License | SIL Open Font License, Version 1.1 (full text at the upstream repository's `OFL.txt`; also mirrored at https://openfontlicense.org) |
| Reserved Font Name declared? | No |
| Modifications | None — WOFF2 latin subset (`U+0000–00FF` plus `œ`/`Œ`, curly quotes, dashes, ellipsis) as distributed by Google Fonts, unmodified, no self-subsetting |

No italic styles are vendored (the console does not use italic styles). Total
vendored size: 110,188 bytes (~107.6 KiB) across all seven files; no individual
font exceeds 45 KiB.
