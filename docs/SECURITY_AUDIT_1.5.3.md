# Hivemind 1.5.3 security audit report

Hivemind 1.5.3 corrects **all seven application findings** identified by the source security assessment started on **16 September 2026**. It also updates vendored DOMPurify to **3.4.15**. Broader dependency and deployment coverage follow-up remains open; these corrections do not constitute an external security certification.

## Scope and method

The initial assessment reviewed a pre-fix 1.5.3 development snapshot. It examined application entry points, authentication and authorization boundaries, storage ownership, derived Graph memory, document references, backup and restore, request handling, and the administration interface. Findings were traced through source code, callers, error paths, and existing tests.

The initial assessment was source-driven; it did not include production exploitation or a deployment-wide penetration test. The fixes subsequently received focused regression testing, code review, and CI validation. This report summarizes those corrections without publishing internal tracking records or details of outstanding findings.

## Corrections included in 1.5.3

| Area | Corrected behavior | Public source and test references |
| --- | --- | --- |
| Graph deletion ownership | Derived Graph cleanup confines document deletion to the selected memory's document namespace. Backup deletion checks manifest identity and targets the defined backup artifacts. Authoritative Hivemind storage must remain outside these deletion operations; failed document cleanup preserves the Graph record for retry. | [Storage implementation](../services/graph-memory/src/mcp_memory/core/storage.py); [maintenance integration coverage](../tests/test_p13_maintenance_integration.py). |
| Graph static files | Static-file handling confines resolved paths to the packaged asset directory, rejects path and symlink escapes, and returns a constant not-found response. | [Middleware](../services/graph-memory/src/mcp_memory/auth/middleware.py); [static-file regression tests](../tests/test_graph_static_security.py). |
| Graph document references | Document references must match the configured bucket and the selected memory's document namespace before reads, imports, or backups. Validation also covers previously stored references. | [Reference validation](../services/graph-memory/src/mcp_memory/core/validators.py); [document ownership tests](../tests/test_graph_document_ownership.py). |
| MCP request budgets | Application middleware limits the actual request bytes before SDK buffering, including chunked requests. Oversized requests are rejected while accepted bodies and server-sent event responses retain their expected behavior. | [Request middleware](../src/live_mem/middleware.py); [request-budget tests](../tests/test_mcp_request_budget.py). |
| Graph archive budgets | Restore admission and restore processing use bounded archive readers. Limits cover compressed and expanded bytes, member size and count, metadata, and structured data. Unsupported sparse entries, links, and duplicate members are rejected. | [Backup and restore implementation](../services/graph-memory/src/mcp_memory/core/backup.py); [archive-limit tests](../tests/test_graph_archive_limits.py). |
| Derived Graph namespaces | Deterministic Graph names are reserved against conflicting Hivemind space creation, and Graph operations check ownership. Ambiguous existing collisions fail closed instead of selecting storage through a raw space-ID fallback. | [Identifier rules](../src/live_mem/core/memory_id.py), [Graph bridge](../src/live_mem/core/graph_bridge.py); [identifier tests](../tests/test_derive_memory_id.py), [binding tests](../tests/test_long_auto_bind.py). |
| Commands from admin Markdown | Rules and MID document rendering strips command attributes. An independent event boundary refuses console commands originating inside document readers, including when attributes are restored after sanitization. Ordinary links and actual console controls remain usable. | [Admin renderer and dispatcher](../src/live_mem/static/js/admin-app.js); [browser regression tests](../tests/e2e/admin_visual_qa.spec.mjs). |

## Dependency maintenance

DOMPurify 3.1.6 is replaced by the unmodified upstream 3.4.15 browser bundle in both `/admin` and `/live`. The [vendor inventory](../src/live_mem/static/vendor/README.md) records its exact source and checksum; the [third-party notices](../THIRD_PARTY_NOTICES.md) and [upstream license](../src/live_mem/static/vendor/purify.LICENSE) accompany it. Dependency advisory matches against the older pin are not evidence of that many exploitable Hivemind vulnerabilities. The application command-boundary fix above is independent of this dependency update.

A fresh OSV query on 17 September 2026 returned 20 advisory records for the npm package `dompurify` at 3.1.6 and none for 3.4.15. This is a dated dependency inventory result, not a guarantee against unknown vulnerabilities. The shipped bundle and license were checked against the official GitHub source and integrity-verified npm package; the bundle also matches the pinned CDN distribution.

The [admin](../tests/e2e/admin_visual_qa.spec.mjs) and [legacy reader](../tests/e2e/live_bank_selection.spec.mjs) browser tests check formatting, tables, code blocks, ordinary links and active-content removal against the actual vendored libraries. The legacy reader's safe-image support remains intact. [Inventory tests](../tests/test_security_hardening_v2.py) verify the documented bundle versions and checksums.

## Evidence and limits

Release qualification included focused regression runs on Python 3.11 and 3.14, checks that selected guards detect regressions when removed, independent review of the corrective patches, and CI. The linked tests provide inspectable public evidence, including rejection before backend effects and compatibility checks for legitimate inputs. The maintenance suite linked for deletion covers integration behavior; additional deletion-specific fixtures used during qualification are not included in this public source distribution.

These checks do not establish exhaustive vulnerability coverage or constitute an external security certification. Archive limits bound accepted input, not total process memory or aggregate concurrent work. Operations spanning storage backends are not atomic. Existing storage collisions may still require operator recovery. Deployment configuration and the exposure of internal Graph endpoints affect reachable attack surfaces; the OSS deployment remains mono-tenant. See the [security model](SECURITY.md) for supported assumptions and the [security policy](../SECURITY.md) for reporting guidance.

## Remaining follow-up

All seven application findings are addressed in this version. This does not close the broader audit program: the existing Graph CDN dependency inventory and pinning work remains outstanding. Ordinary document links retain native navigation, and the legacy reader retains its broader HTML policy; the admin fix establishes a document-to-command boundary, not a general redesign of all console interactions.

Coverage of operating-system packages, container images, plugins, unpinned tools and CDN dependencies remains incomplete. The assessment and patch reviews do not replace an independent external deployment assessment.

This report concerns the assessed development snapshot and the seven corrections included in 1.5.3. It does not establish the affected range of earlier releases, cover every dependency or deployment component, or assert that Hivemind is free of security defects.
