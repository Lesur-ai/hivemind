# Hivemind — Public Mono-Tenant Promise & Extension-Point Contract

> This document publishes the **public mono-tenant promise** and the
> **extension-point seam contract** between the open-source Hivemind
> repository and any independently operated downstream edition. It is a
> contract statement: it does **not** re-engineer architecture
> decisions, it makes the seams those ADRs reserve visible and testable so
> contributors and downstream operators understand exactly where the OSS
> repository stops and an independently operated extension begins.
>
> **Language: EN only for V1.** English is canonical for this reference. The
> French README and migration guide summarize the user-facing contract and link
> back here.
>
<!-- non-claims -->
> **Claim hygiene.** Project Mesh V1 / Mesh Sync V1 is full-mesh all-ACK —
> no quorum, no hub, no permanent master, no CRDT, no offline-first, no
> multi-tenant, no multi-space merge. This document does not contradict
> those non-claims.
<!-- /non-claims -->

---

## 1. Mono-tenant promise (ADR-0003)

The open-source Hivemind edition is **strictly mono-tenant**. A single
deployment serves a single organizational mesh. The only isolation primitive
in the public repository is the per-token `space_ids` / `allowed_resources`
**allowlist** enforced by `check_access()` in
`src/live_mem/auth/context.py`. One token equals one agent, and an `admin`
token bypasses the per-space restriction by design.

<!-- non-claims -->
**The `space_ids` allowlist is NOT a tenant boundary.** Treating it as one
is a violation of ADR-0003. There is no tenant object, no row-level
security, no per-tenant key or bucket separation. Multi-tenant enforcement
is the responsibility of a downstream edition; it must never live in
or be depended on by this public repository.
<!-- /non-claims -->

The published MCP tool surface, locked by the surface-stability tests, is the
complete public contract. Any feature not in that surface or the public
architecture contracts is not part of the supported mono-tenant promise.

---

## 2. Documented extension seams

The seam is split into two layers. Both layers exist in the public repo
**today**; what differs is which layer the OSS edition exposes versus where
a downstream edition is expected to plug in.

### 2a. Auth-layer `PolicyProvider` seam — fail-closed mono-tenant default

The auth-layer surface is `src/live_mem/auth/context.py`. It exposes the
four legacy permission helpers consumed by every MCP tool handler today —
`check_access(resource_id)`, `check_write_permission()`,
`check_manage_permission()`, `check_admin_permission()` — and, per
ADR-0003 Option 3, a narrow **`PolicyProvider`** authorization seam:

```python
class PolicyProvider(Protocol):
    def authorize(
        self,
        identity: Optional[dict],
        action: str,
        resource: str,
        context: Optional[dict] = None,
    ) -> None: ...
```

The seam contract is intentionally narrow:

- `authorize` returns `None` to allow.
- `authorize` raises `PermissionDenied` to deny.
- The default posture is **fail-closed**: any context shape the
  implementation does not understand must be denied.

The OSS edition ships exactly one concrete implementation,
**`MonoTenantSpaceAllowlistProvider`**, exposed via the
`default_policy_provider()` factory. Its behaviour pins the
fail-closed posture from ADR-0003 §Implementation Notes §1 in code:

1. **Missing or empty identity → deny.** If `identity` is `None`, an
   empty dict, or a `TokenInfo`-shaped dict with no usable claim
   fields, the provider raises
   `PermissionDenied("OSS mono-tenant: missing or empty identity")`.
   The supplied `identity` is authoritative: the seam **never** falls
   back to the ambient `current_token_info` contextvar. A permissive
   token in the ambient store does not mask a missing-identity deny.
2. **Missing or unknown action → deny.** If `action` is `None`, an
   empty string, or any value outside the V1 closed set
   `MonoTenantSpaceAllowlistProvider.ALLOWED_ACTIONS`, the provider
   raises `PermissionDenied("OSS mono-tenant: missing action")` or
   `PermissionDenied("OSS mono-tenant: unknown action 'xyz'")`.
   `ALLOWED_ACTIONS` is a `frozenset[str]` enumerating every MCP
   tool name in the surface-stability fixture
   `tests/fixtures/tool_surface.json` (historical names + canonical
   `short_*` / `mid_*` / `long_*` tier aliases). A downstream
   subclass that exposes additional actions MUST extend this set
   explicitly (e.g. `ALLOWED_ACTIONS = base | {"portal_extra"}`).
3. **Malformed or unrecognized context → deny.** The `context`
   argument **must** be `None` or a `dict`. Any other type (`list`,
   `str`, `int`, `bool`, `set`, custom object, …) — whether falsy
   (`[]`, `""`, `0`, `False`) or truthy (`["tenant_id"]`,
   `"tenant_id"`, `42`, `True`, `object()`, …) — is malformed and
   rejected with
   `PermissionDenied("OSS mono-tenant: context must be a dict or None, got <type>")`
   **before** any key inspection. This closes the dynamic-argument
   seam against both the "falsy non-dict silently treated as no
   context" hole and the "truthy non-dict crashes with
   `AttributeError`" hole.
   Once the shape check passes, if `context` is a non-empty `dict`
   containing any key not in the V1 recognized set
   `MonoTenantSpaceAllowlistProvider.RECOGNIZED_CONTEXT_KEYS`, the
   provider raises
   `PermissionDenied("OSS mono-tenant: unrecognized context key '…'")`.
   `RECOGNIZED_CONTEXT_KEYS` is the **empty `frozenset`** for the OSS
   default: the mono-tenant edition consumes no structured context,
   so any key (`tenant_id`, `account_id`, `namespace_id`,
   `policy_zone`, …) is unrecognized and denied. This is **broader**
   than the legacy 5-tenancy-key check below: it catches any
   policy-zone shape a future contributor might invent without
   updating the deny set.
4. **Belt-and-suspenders tenancy deny.** As a defensive second layer,
   the provider also raises
   `PermissionDenied("OSS mono-tenant: unsupported tenancy context")`
   on any non-empty value under
   `tenant_id`, `tenant`, `organization_id`, `organization`,
   `workspace_id`. For the default config this is subsumed by (3),
   but it ensures a downstream subclass that widens
   `RECOGNIZED_CONTEXT_KEYS` cannot accidentally let a tenancy claim
   through without re-implementing the tenancy gate.
5. **Space-allowlist + admin-bypass on the SUPPLIED identity.**
   Otherwise the provider evaluates the per-space allowlist + admin
   bypass via the shared `_evaluate_access(identity, resource)`
   helper using the explicitly supplied `identity`. The ambient
   `check_access()` path uses the same helper, so the two paths
   cannot diverge; the legitimate-access behaviour is byte-for-byte
   preserved (ADR-0011: single commit-authorization point).

**Forward pointer.** ADR-0003 Implementation Notes §1 names this
`authorize(identity, action, resource, context)` shape as the canonical
seam. The seam **exists** in the OSS surface today; a future decision may
add broader policy semantics or new context keys — never a downstream-only
import in the public repo (see §4).

### 2b. Protocol-layer peer-channel scope — `peer_scope_guard` is the unit-test seam, not the runtime gate

The protocol-layer surface is
`src/live_mem/core/hivemind/enrollment.py::peer_scope_guard(member, required_scope, *, tenancy_context=None)`.
The **runtime peer-channel gate** is enforced inline on receive via
`required_scope_for_event()` / `member.has_scope()`. `peer_scope_guard()`
is the **direct unit-test seam**: it pins the tenancy-deny invariant
referenced by
`tests/test_hivemind_enrollment.py::test_unrecognized_tenancy_context_denied`
and gives downstream contributors a single
function whose contract is testable in isolation.

If `tenancy_context` is non-empty, `peer_scope_guard` raises
`PeerChannelError(INSUFFICIENT_SCOPE)`. The OSS edition recognizes no
tenancy context; any value is rejected as "unrecognized" by construction.
This is the mono-tenant posture in code (ADR-0003).

**Authoritative invariant test.** The deny-on-unrecognized-tenancy
behavior is pinned by
`tests/test_hivemind_enrollment.py::test_unrecognized_tenancy_context_denied`.
This document references that test as the authoritative peer-channel
tenancy-deny invariant; other guards deliberately do **not** duplicate it.

---

## 3. Shared-vs-local metadata allowlist (ADR-0012)

The `_meta.json` document for a space is split by an explicit allowlist,
`SHARED_META_FIELDS`, declared in `src/live_mem/core/models.py`. The
partition functions are `meta_shared_projection(meta)` (shareable subset)
and `meta_local_complement(meta)` (local-only complement). They satisfy
`{**meta_local_complement(m), **meta_shared_projection(m)} == m` — no
field is silently dropped, no field is silently shared.

**The whole `graph_memory` block is local-only.** Endpoints, token,
`memory_id`, and push metrics are never replicated to a peer; an instance
that joins the mesh learns nothing about another node's Graph Memory
configuration. A downstream edition that wants cross-instance graph state
must not widen this allowlist in the OSS repo — it must layer its own
shared-projection on top in its own codebase.

**Fail-closed posture on unclassifiable fields.** A metadata field that is
not in `SHARED_META_FIELDS` falls into `meta_local_complement(...)` by
default and is therefore **blocked** from the shared snapshot. The OSS
edition does **not** silently treat an unknown field as either shared or
local for replication; the default-deny on the shared side is the
fail-closed posture. A future contributor who adds a new `SpaceMeta` field
must update `SHARED_META_FIELDS` explicitly, or it stays local by design.

The ADR-0012 invariant is pinned by `tests/test_meta_allowlist.py`.

---

## 4. Downstream authorization enforcement is an extension, never a dependency

An independently operated downstream edition may add tenant-aware row-level or
application policy on top of the OSS surface. The public contract is
one-directional:

- **The OSS repo does not import a downstream-only policy module.** No file
  under `src/live_mem/` is allowed to `import` (or `importlib.import_module`
  with a string-literal argument naming) modules in the namespaces
  `portal_policy`, `pundit`, `rls_policy`, or `lesur_portal`. The public
  guard is
  `tests/test_policy_mono_tenant.py::test_no_public_repo_module_imports_portal_only_policy`.
- **The OSS repo never optionally calls a downstream policy hook.** No
  feature flag, no environment variable, no conditional import path
  reaches a downstream namespace from this repository.

**Downstream override pattern.** A downstream edition extends the seam by
defining its own `PolicyProvider` subclass in its private codebase and
replacing the singleton returned by `default_policy_provider()` during
its own bootstrap. The override pattern is intentionally minimal: the
OSS repo never reads from the downstream namespace, and the downstream edition
never patches the OSS module's source. Concretely its bootstrap
does the equivalent of:

```python
# In downstream bootstrap code — NEVER in this repo:
from live_mem.auth import context as auth_context
from portal_policy.tenant_aware import TenantAwarePolicyProvider

auth_context._DEFAULT_POLICY_PROVIDER = TenantAwarePolicyProvider()
```

The OSS edition does **not** ship a feature flag, environment variable,
or conditional import that reaches into a downstream namespace. The seam is
the extension point; downstream wiring is out of scope of this
repository.

**AST guard scope caveat.** The static guard catches direct top-level
`import` / `from ... import` statements and `importlib.import_module("…")`
calls whose argument is a string literal. Transitive imports (a module
imported by an allowed module that itself imports a forbidden one), lazy
imports inside a function body whose target is a runtime-built string, and
`importlib.import_module(variable)` calls are **not** caught by AST alone.
That residual surface is covered by code review, not by automated test.
The combination — AST guard + reviewer attention — is the public
contract; do not rely on the test as a complete static proof.

---

## 5. References

- **ADR-0003** — Open-source mono-tenant scope and downstream extension seams.
  Implementation Notes §1 defines the canonical `PolicyProvider` seam
  shape (`authorize(identity, action, resource, context)`); the seam is
  landed in this repository under §2a above.
- **ADR-0011** — Single commit-authorization point. `assert_commit_allowed()`
  is the one code-path that authorizes a shared commit. The
  `PolicyProvider` default reuses `check_access()` rather than introducing
  a parallel authorization path; downstream subclasses that layer tenant
  policy still sit **in front of and call** `assert_commit_allowed()`, not
  parallel to it.
- **ADR-0012** — Shared-vs-local metadata allowlist. `SHARED_META_FIELDS`
  + `meta_shared_projection` + `meta_local_complement` in
  `src/live_mem/core/models.py`. Default-deny on the shared side.
- **ADR-0016** — Repo-driven enrollment and scoped peer rights. Source of
  the `peer_scope_guard` mono-tenant posture pinned by
  `tests/test_hivemind_enrollment.py::test_unrecognized_tenancy_context_denied`.
- **ADR-0018** — Public release naming, versioning, and language policy.
  Source of the EN-only stance for V1 extension-point documentation.
- **Related code paths.**
  - `src/live_mem/auth/context.py` — `PolicyProvider`,
    `MonoTenantSpaceAllowlistProvider`, `default_policy_provider`,
    `check_access`, `check_write_permission`, `check_manage_permission`,
    `check_admin_permission`.
  - `src/live_mem/core/hivemind/enrollment.py` — `peer_scope_guard` (the
    unit-test seam pinning the tenancy-deny invariant; runtime
    enforcement on receive happens via `required_scope_for_event()` /
    `member.has_scope()`).
  - `src/live_mem/core/models.py` — `SHARED_META_FIELDS`,
    `meta_shared_projection`, `meta_local_complement`.

---

## Non-claims

The following words appear here only as **explicit non-claims** of
Hivemind. The public repository does **not** ship, implement, or promise
any of them; they are listed verbatim to make the boundary unambiguous:

- **quorum** — Project Mesh V1 / Mesh Sync V1 is full-mesh all-ACK, not a
  quorum protocol.
- **hub topology** — there is no central hub sequencing events.
- **permanent master** — critical state is protocol-derived; `long` /
  graph memory is a derived view, never authoritative.
- **CRDT** — conflicting offline writes are not reconciled at the
  protocol level.
- **multi-tenant** — a single deployment serves a single organizational
  mesh; this document is precisely the public mono-tenant promise.
- **multi-space merge** — there is no protocol-level merge of two spaces
  into one shared space.
