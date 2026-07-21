# -*- coding: utf-8 -*-
"""
P6-6 (#92) — Public mono-tenant promise + Portal extension-point guard tests.

This module pins the **public** mono-tenant contract documented in
``docs/EXTENSION_POINTS.md``. It is deliberately NOT a duplicate of the
existing peer-channel tenancy-deny invariant test
(``tests/test_hivemind_enrollment.py::test_unrecognized_tenancy_context_denied``,
added in P5-9 / ADR-0016), which remains the authoritative behavioural
invariant on ``peer_scope_guard``. This module adds NEW coverage:

1. The auth-layer ``PolicyProvider`` default
   (``MonoTenantSpaceAllowlistProvider`` returned by
   ``default_policy_provider()``) **fail-closes** on any tenancy-shaped
   context. This is the regression pin for ADR-0003 Option 3 in the OSS
   surface.

2. The same default **allows** an empty / missing context (no tenancy
   claim made) — the seam's deny logic is scoped to recognized
   tenancy-shaped keys, not to the mere presence of a context dict.

3. The same default **allows** legitimate per-space access via the
   existing ``check_access()`` path (admin-bypass + space allowlist),
   so the seam preserves the legitimate-access behaviour byte-for-byte
   (ADR-0011: single commit-authorization point).

4. No module under ``src/live_mem/`` imports a Portal-only policy
   namespace. The guard is AST-based and also flags
   ``importlib.import_module("<name>")`` calls with a string-literal
   argument. Transitive imports and ``importlib.import_module(variable)``
   calls are **not** caught by AST alone — that residual surface is
   documented in ``docs/EXTENSION_POINTS.md`` §4 and covered by code
   review.

5-7. Doc-consistency guards on ``docs/EXTENSION_POINTS.md``: the EN-only
     V1 marker is present, the existing peer-channel test is referenced
     as authoritative, and forbidden non-claim terms appear only inside
     the dedicated ``Non-claims`` section.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from live_mem.auth.context import (
    PermissionDenied,
    PolicyProvider,
    MonoTenantSpaceAllowlistProvider,
    default_policy_provider,
    current_token_info,
)

# ─────────────────────────────────────────────────────────────────────────────
# Repo layout helpers
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_LIVE_MEM = _REPO_ROOT / "src" / "live_mem"
_EXTENSION_POINTS_DOC = _REPO_ROOT / "docs" / "EXTENSION_POINTS.md"

# Portal-only policy namespaces — never importable from the public repo.
_PORTAL_NAMESPACE_TOKENS: tuple[str, ...] = (
    "portal_policy",
    "pundit",
    "rls_policy",
    "lesur_portal",
)


# ─────────────────────────────────────────────────────────────────────────────
# 1) Default PolicyProvider denies unsupported tenancy context
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tenancy_key",
    [
        "tenant_id",
        "tenant",
        "organization_id",
        "organization",
        "workspace_id",
    ],
)
def test_default_policy_provider_denies_unsupported_tenancy_context(
    tenancy_key: str,
) -> None:
    """
    ADR-0003: the OSS edition recognizes no tenancy context. Any
    non-empty value under a known tenancy key must raise
    ``PermissionDenied`` — fail-closed — regardless of the action,
    resource or identity.

    Pinned for every tenancy key in the public deny set so adding a
    new key in code without updating this guard fails CI by design.
    """
    provider = default_policy_provider()
    assert isinstance(provider, PolicyProvider)
    assert isinstance(provider, MonoTenantSpaceAllowlistProvider)

    # Supply a legitimate admin identity so the tenancy-deny path is the
    # gate being tested (not the missing-identity / unknown-action gate
    # that P6-6 R2 added before the tenancy check).
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=admin_token,
            action="space_create",
            resource="some-space",
            context={tenancy_key: "acme"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2) Default PolicyProvider allows when no tenancy context is asserted
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "empty_context",
    [None, {}],
    ids=["context-none", "context-empty-dict"],
)
def test_default_policy_provider_allows_no_tenancy_context(
    empty_context,
) -> None:
    """
    The seam's deny logic is scoped to **recognized** tenancy-shaped
    keys with non-empty values. An empty context dict or a missing
    context must not, by itself, trigger the tenancy-deny path.

    The space-allowlist deny still applies — we use an admin token so
    the space-allowlist path is a no-op, isolating the tenancy logic.
    """
    provider = default_policy_provider()

    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    tok = current_token_info.set(admin_token)
    try:
        # Must NOT raise — empty context, admin identity, legitimate
        # space-id format.
        result = provider.authorize(
            identity=admin_token,
            action="space_create",
            resource="legitimate-space",
            context=empty_context,
        )
    finally:
        current_token_info.reset(tok)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 3) Default PolicyProvider allows legitimate space-allowlist access
# ─────────────────────────────────────────────────────────────────────────────


def test_default_policy_provider_allows_legitimate_action() -> None:
    """
    A non-admin token with ``allowed_resources=["s1"]`` must be
    allowed on resource ``"s1"`` and denied on ``"s2"`` via the
    existing ``check_access()`` path. The seam reuses that helper —
    the legitimate-access behaviour is byte-for-byte preserved
    (ADR-0011 single authorization point).
    """
    provider = default_policy_provider()

    write_token: dict = {
        "client_name": "test-agent",
        "permissions": ["write"],
        "allowed_resources": ["s1"],
    }
    tok = current_token_info.set(write_token)
    try:
        # Allowed space — no raise.
        result = provider.authorize(
            identity=write_token,
            action="live_note",
            resource="s1",
            context=None,
        )
        assert result is None

        # Disallowed space — fail-closed via check_access().
        with pytest.raises(PermissionDenied):
            provider.authorize(
                identity=write_token,
                action="live_note",
                resource="s2",
                context=None,
            )
    finally:
        current_token_info.reset(tok)


# ─────────────────────────────────────────────────────────────────────────────
# 3-bis) P6-6 R2 — fail-closed regression pins on authorize()
# ─────────────────────────────────────────────────────────────────────────────
#
# Codex re-review on PR #111 flagged the seam as failing OPEN in three
# cases (missing identity, unknown action, unrecognized context key).
# These tests pin the hardened ADR-0003 §Implementation Notes §1
# contract: each fail-closed case raises PermissionDenied; the happy
# supplied-identity path still returns None.


def test_authorize_denies_on_missing_identity() -> None:
    """ADR-0003 §Impl §1: missing identity → deny (no ambient fallback)."""
    provider = default_policy_provider()
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=None,
            action="space_create",
            resource="s1",
        )


@pytest.mark.parametrize(
    "empty_identity",
    [
        {},
        # TokenInfo-shaped dict with no usable claim fields.
        {"client_name": "", "permissions": [], "allowed_resources": None},
    ],
    ids=["empty-dict", "tokeninfo-no-claims"],
)
def test_authorize_denies_on_empty_identity(empty_identity: dict) -> None:
    """An identity dict with no usable claims is functionally missing → deny."""
    provider = default_policy_provider()
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=empty_identity,
            action="space_create",
            resource="s1",
        )


def test_authorize_denies_on_unknown_action() -> None:
    """ADR-0003 §Impl §1: action not in ALLOWED_ACTIONS → deny."""
    provider = default_policy_provider()
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=admin_token,
            action="unknown_action_xyz",
            resource="s1",
        )


@pytest.mark.parametrize(
    "missing_action",
    [None, ""],
    ids=["action-none", "action-empty"],
)
def test_authorize_denies_on_missing_action(missing_action) -> None:
    """Missing or empty action → deny (no implicit default)."""
    provider = default_policy_provider()
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=admin_token,
            action=missing_action,  # type: ignore[arg-type]
            resource="s1",
        )


def test_authorize_denies_on_unrecognized_context_key() -> None:
    """
    ADR-0003 §Impl §1: any context key not in RECOGNIZED_CONTEXT_KEYS
    → deny. Proves the broader catch beyond the 5 hardcoded tenancy
    keys — ``account_id`` is not in the tenancy deny set but is still
    rejected because the V1 implementation does not understand it.
    """
    provider = default_policy_provider()
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=admin_token,
            action="space_create",
            resource="s1",
            context={"account_id": "acme"},
        )


def test_authorize_denies_on_namespace_id_context_key() -> None:
    """
    Companion to the ``account_id`` test: ``namespace_id`` is another
    plausible policy-zone key the V1 implementation does not
    understand. ADR-0003 §Impl §1 requires deny.
    """
    provider = default_policy_provider()
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied):
        provider.authorize(
            identity=admin_token,
            action="space_create",
            resource="s1",
            context={"namespace_id": "x"},
        )


def test_authorize_uses_supplied_identity_not_ambient() -> None:
    """
    The seam ignores the ambient ``current_token_info`` contextvar
    when ``identity`` is missing. Installing a permissive admin token
    in the contextvar must NOT mask the missing-identity deny.

    ADR-0003 §Impl §1: the supplied identity is authoritative.
    """
    provider = default_policy_provider()
    permissive_ambient: dict = {
        "client_name": "ambient-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    tok = current_token_info.set(permissive_ambient)
    try:
        # Even with a permissive ambient token, identity=None must deny.
        with pytest.raises(PermissionDenied):
            provider.authorize(
                identity=None,
                action="space_read",
                resource="s1",
            )
    finally:
        current_token_info.reset(tok)


def test_authorize_allows_legitimate_call_with_supplied_identity() -> None:
    """
    Happy path: a write-scoped identity with ``allowed_resources=["s1"]``,
    a known action, no context — returns None. Pins that the
    fail-closed gates do NOT over-block legitimate calls.
    """
    provider = default_policy_provider()
    write_identity: dict = {
        "client_name": "test-agent",
        "permissions": ["write"],
        "allowed_resources": ["s1"],
    }
    result = provider.authorize(
        identity=write_identity,
        action="live_note",
        resource="s1",
        context=None,
    )
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 3-ter) P6-6 R3 — malformed (non-dict) context fails closed
# ─────────────────────────────────────────────────────────────────────────────
#
# Codex round-3 review on PR #111 flagged the seam as still failing
# OPEN (or AttributeError-crashing) for non-None non-dict context
# values. The contract is: ``context`` MUST be ``None`` or a ``dict``;
# anything else — including falsy non-dicts (``[]``, ``""``, ``0``,
# ``False``) that previously slipped through the ``if context:`` gate
# as "no context", and truthy non-dicts (a list, str, int, set, custom
# object) that previously crashed with AttributeError — must raise
# PermissionDenied at the public seam boundary.


@pytest.mark.parametrize(
    "ctx,kind",
    [
        ([], "list-empty"),
        ("", "str-empty"),
        (0, "int-zero"),
        (False, "bool-false"),
        (["tenant_id"], "list-with-str"),
        ("tenant_id", "str-non-empty"),
        (42, "int-non-zero"),
        (True, "bool-true"),
        ({"tenant_id"}, "set"),
        (object(), "object"),
    ],
    ids=lambda v: v if isinstance(v, str) else repr(v),
)
def test_authorize_denies_on_non_dict_context(ctx, kind) -> None:
    """Codex round 3: any non-None, non-dict context value fails closed."""
    provider = default_policy_provider()
    admin_token: dict = {
        "client_name": "test-admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    with pytest.raises(PermissionDenied) as exc:
        provider.authorize(
            identity=admin_token,
            action="live_note",
            resource="s1",
            context=ctx,
        )
    assert (
        "dict or None" in str(exc.value)
        or "context" in str(exc.value).lower()
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4) No public-repo module imports a Portal-only policy namespace
# ─────────────────────────────────────────────────────────────────────────────


def _iter_py_files(root: Path):
    for path in root.rglob("*.py"):
        # Skip any in-tree cache or build artefacts that might appear under
        # src/live_mem on a working checkout.
        parts = set(path.parts)
        if "__pycache__" in parts:
            continue
        yield path


def _module_name_matches_portal(module_name: str | None) -> bool:
    if not module_name:
        return False
    low = module_name.lower()
    return any(tok in low for tok in _PORTAL_NAMESPACE_TOKENS)


def _string_arg_matches_portal(call_node: ast.Call) -> bool:
    """Detect ``importlib.import_module("<name>")`` with a string literal."""
    func = call_node.func
    qualname: str | None = None
    if isinstance(func, ast.Attribute):
        # Match ``importlib.import_module`` (most common form).
        if isinstance(func.value, ast.Name) and func.value.id == "importlib":
            qualname = f"importlib.{func.attr}"
    elif isinstance(func, ast.Name):
        # Match a bare ``import_module(...)`` after
        # ``from importlib import import_module``.
        if func.id == "import_module":
            qualname = "import_module"
    if qualname not in {"importlib.import_module", "import_module"}:
        return False
    if not call_node.args:
        return False
    first = call_node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return _module_name_matches_portal(first.value)
    return False


def test_no_public_repo_module_imports_portal_only_policy() -> None:
    """
    The public Hivemind repo must never depend on a Portal-only policy
    module. We walk every ``.py`` under ``src/live_mem/`` and flag any
    direct ``import`` / ``from ... import`` of a Portal namespace, plus any
    ``importlib.import_module("<name>")`` call whose string-literal target
    matches a Portal namespace.

    Caveats (documented in docs/EXTENSION_POINTS.md §4):

    * Transitive imports — module A imports B, and B imports a forbidden
      target — are not caught by this guard.
    * ``importlib.import_module(variable)`` calls whose target is a
      runtime-built string are not caught by this guard.

    Those residual surfaces are covered by code review, not by this test.
    """
    assert _SRC_LIVE_MEM.is_dir(), (
        f"src/live_mem/ not found at {_SRC_LIVE_MEM} — this guard cannot run "
        "outside a Hivemind checkout."
    )

    offenders: list[str] = []
    for py_path in _iter_py_files(_SRC_LIVE_MEM):
        try:
            source = py_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_path))
        except SyntaxError:
            # A syntax error here would already fail the broader test
            # suite; surface a clear message rather than masking it.
            offenders.append(f"{py_path} (syntax error during AST parse)")
            continue
        rel = py_path.relative_to(_REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_name_matches_portal(alias.name):
                        offenders.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if _module_name_matches_portal(node.module):
                    names = ", ".join(a.name for a in node.names)
                    offenders.append(f"{rel}: from {node.module} import {names}")
            elif isinstance(node, ast.Call):
                if _string_arg_matches_portal(node):
                    arg0 = node.args[0]
                    # We checked it is a string Constant in the helper.
                    assert isinstance(arg0, ast.Constant)
                    offenders.append(
                        f"{rel}: importlib.import_module({arg0.value!r})"
                    )
    assert not offenders, (
        "ADR-0003 violation: the public repo imports a Portal-only policy "
        f"namespace: {offenders}. Portal RLS/Pundit enforcement is an "
        "extension layered on top of the OSS surface; it must never live "
        "in or be depended on by this repository (see "
        "docs/EXTENSION_POINTS.md §4)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5) Doc consistency — EN-only marker
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def extension_points_doc_text() -> str:
    assert (
        _EXTENSION_POINTS_DOC.is_file()
    ), f"Missing doc: {_EXTENSION_POINTS_DOC}"
    return _EXTENSION_POINTS_DOC.read_text(encoding="utf-8")


def test_extension_points_doc_marks_en_only_explicitly(
    extension_points_doc_text: str,
) -> None:
    """ADR-0018: the V1 extension-point doc is EN-only, marked explicitly."""
    assert "Language: EN only for V1" in extension_points_doc_text, (
        "docs/EXTENSION_POINTS.md must mark itself EN-only for V1 in plain "
        "text so future maintainers see the language choice is intentional "
        "and not an oversight."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6) Doc consistency — references the authoritative peer-channel test
# ─────────────────────────────────────────────────────────────────────────────


def test_extension_points_doc_references_existing_peer_scope_guard_test(
    extension_points_doc_text: str,
) -> None:
    """
    The EXTENSION_POINTS.md must cross-reference the existing P5-9
    invariant test for the peer-channel tenancy-deny behavior so a reader
    sees that the behavior is already pinned, and so this P6-6 test module
    is not mistaken for a duplicate.
    """
    assert "test_unrecognized_tenancy_context_denied" in extension_points_doc_text, (
        "docs/EXTENSION_POINTS.md must reference "
        "tests/test_hivemind_enrollment.py::test_unrecognized_tenancy_context_denied "
        "as the authoritative peer-channel tenancy-deny invariant."
    )
    assert "test_hivemind_enrollment" in extension_points_doc_text, (
        "docs/EXTENSION_POINTS.md must name the test_hivemind_enrollment "
        "module so the cross-reference is unambiguous."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7) Doc consistency — forbidden non-claim terms only inside Non-claims section
# ─────────────────────────────────────────────────────────────────────────────


_FORBIDDEN_NON_CLAIM_TERMS: tuple[str, ...] = (
    "quorum",
    "hub topology",
    "permanent master",
    "CRDT",
    "multi-tenant",
    "multi-space merge",
)


def _split_doc_around_non_claims(doc_text: str) -> tuple[str, str]:
    """
    Returns (before_non_claims, non_claims_block).

    The Non-claims section is identified by a Markdown heading whose
    leading characters are ``## Non-claims`` (with optional trailing
    whitespace). Everything from that heading to end-of-file is treated
    as the Non-claims block.
    """
    lines = doc_text.splitlines(keepends=True)
    split_idx: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("##") and "Non-claims" in stripped:
            split_idx = idx
            break
    if split_idx is None:
        return doc_text, ""
    return "".join(lines[:split_idx]), "".join(lines[split_idx:])


def test_extension_points_doc_no_forbidden_non_claims(
    extension_points_doc_text: str,
) -> None:
    """
    Same deny-list spirit as P6-5 / P6-8: ``quorum``, ``hub topology``,
    ``permanent master``, ``CRDT``, ``multi-tenant``, ``multi-space
    merge`` may only appear inside a dedicated ``## Non-claims`` section
    where they are framed as explicit non-claims of Hivemind.

    Some of these terms are unavoidable in the body when discussed
    contextually (e.g. naming the "multi-tenant" claim we deny). The
    contract is that contextual mentions must be paired with the
    dedicated Non-claims section so a release-gate scanner can match the
    full document against the deny-list deterministically.
    """
    before, non_claims = _split_doc_around_non_claims(extension_points_doc_text)
    assert non_claims, (
        "docs/EXTENSION_POINTS.md must contain a '## Non-claims' section "
        "listing the deny-list terms as explicit non-claims of Hivemind."
    )
    # Every deny-list term must appear inside the Non-claims block — that
    # is what makes the doc pass a release-gate match deterministically.
    missing_in_non_claims = [
        term for term in _FORBIDDEN_NON_CLAIM_TERMS if term.lower() not in non_claims.lower()
    ]
    assert not missing_in_non_claims, (
        "docs/EXTENSION_POINTS.md '## Non-claims' section must enumerate "
        f"every deny-list term; missing: {missing_in_non_claims}."
    )
