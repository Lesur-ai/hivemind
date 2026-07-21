# Security Policy

Hivemind is an OSS, mono-tenant MCP service. This file is the **coordinated
vulnerability disclosure policy** for the project. The deeper public threat
model — including accepted risks and deployer responsibilities — lives in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Supported versions

Only the `main` branch and the most recent tagged release receive security
fixes. Older tags are not patched; upgrade to receive fixes.

## Reporting a vulnerability

**Please do not open public GitHub issues for suspected vulnerabilities.**

Use the repository's private
[GitHub vulnerability-report form](https://github.com/Lesur-ai/hivemind/security/advisories/new).
This is the sole supported confidential channel; it lets maintainers discuss
and remediate the report in a private draft advisory. If the form is not
available, private vulnerability reporting has not yet been enabled—do not put
the report in a public issue, discussion, pull request, or email address inferred
from a profile. Enabling and verifying this form is a publication gate.

Include in your report:

* A short description of the issue and the affected component
  (file path / endpoint / MCP tool name).
* A minimal reproducer (commands, payload, or steps) where possible.
* The Hivemind version (`VERSION` file or container tag) and deployment
  shape (Docker Compose, Kubernetes, fronted by which ingress, etc.).
* Your assessment of impact (confidentiality / integrity / availability)
  and any suggested mitigation.
* Whether you would like to be credited in the eventual advisory.

## Expected response

* **Acknowledgement**: within **72 hours** of report receipt (business
  days, Europe/Paris).
* **Initial triage**: within **7 days** — confirm whether the report is
  in scope, request additional details if needed, and indicate a target
  severity.
* **Fix timeline**: depends on severity. Critical / High issues target a
  patch within **30 days**; Medium within **90 days**; Low at the next
  convenient release. Timelines are best-effort for an OSS project with
  volunteer maintainers; we will keep you updated.
* **Coordinated disclosure**: we aim to publish a GitHub Security
  Advisory and a `CHANGELOG.md` entry once a fix is available. Default
  embargo is **90 days** from acknowledgement, extendable by mutual
  agreement.

## Scope

**In scope** — vulnerabilities in code or configuration shipped in this
repository:

* The MCP service (Python, `src/`).
* The bundled WAF / Caddy configuration (`waf/`, `docker-compose.yml`,
  `Dockerfile`).
* Documentation that misleads a deployer in a security-relevant way
  (e.g. `docs/SECURITY.md`, `docs/DEPLOYMENT.md`, `.env.example`).

**Out of scope**:

* **Accepted risks** already documented as deployer responsibilities in
  [`docs/SECURITY.md`](docs/SECURITY.md) (e.g. WAF `/mcp` bypass,
  unsalted SHA-256 on 32-byte random tokens, no built-in Docker egress
  filter). Reports re-flagging these without new exploit context will
  be closed with a pointer to the threat model.
* **Operator-owned configuration**: missing TLS, missing S3
  server-side encryption, missing bucket versioning, missing egress
  allowlist, weak operator-issued tokens, etc. These are operator
  responsibilities — see the secure-by-default checklist in
  `docs/SECURITY.md`.
* Vulnerabilities in third-party dependencies that have an upstream
  advisory and a published fix; please report those upstream, then open
  an issue here to track the bump if it is not already in `uv.lock`.
* Denial-of-service via raw request volume against an unauthenticated
  endpoint that would obviously be rate-limited at the WAF/ingress in a
  real deployment.

## Safe harbour

We will not pursue legal action against, or ask law enforcement to
investigate, security researchers who:

* Make a **good-faith** effort to comply with this policy.
* Avoid privacy violations, service disruption, and data destruction on
  third-party systems they do not own.
* Test only against deployments they own or are explicitly authorised
  to test.
* Give us a **reasonable time** to remediate before any public
  disclosure (see "Expected response" above).
* Do not exploit a finding beyond what is necessary to demonstrate it.

This safe harbour applies to research on this repository's code and on
your own deployments of it. It does **not** extend to third-party
deployments (including those of Hivemind or any third-party operator) — get that
operator's authorisation separately before testing their instance.

## Out-of-band cryptographic statements

Hivemind issues 32-byte cryptographically random bearer tokens and
stores them as unsalted SHA-256 hashes. This is a documented accepted
risk (see `docs/SECURITY.md` §3.4) because the input space has 256 bits
of entropy. Do **not** repurpose the token store for low-entropy
human-chosen passwords.

The `manage` role is a high-trust, transitive provisioning capability
(ADR-0022): it can create arbitrary new spaces and further non-admin managers.
The `space_ids` allowlist limits access/invitation for existing spaces, not
space creation. Routine agents should use `read,write`; see
[`docs/SECURITY.md`](docs/SECURITY.md#39-manage-is-transitive-provisioning-authority-adr-0022)
for the full threat contract.

`long` / graph memory credentials are **local-only** (ADR-0012). They
never enter the replicated commit log nor the audit path, and they are
masked in API responses. See `docs/SECURITY.md` §4.

## Thanks

Responsible disclosure makes Hivemind safer for every deployer.
Maintainers will credit reporters who request it in the relevant
GitHub Security Advisory and `CHANGELOG.md` entry.
