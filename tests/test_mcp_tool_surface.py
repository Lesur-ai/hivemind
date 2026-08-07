# -*- coding: utf-8 -*-
"""
Registration-surface lock for the single MCP facade.

This is the authoritative enumeration test for the whole tool surface after the
tier aliases land. ``tests/fixtures/tool_surface.json`` is the sole expected
surface/count authority; one exhaustive test below compares it with a real
in-process FastMCP built by the real ``register_all_tools`` (no S3 / no network
/ no LLM) and pins:

- every fixture-declared direct name and canonical alias is registered;
- the live registered union and returned total match the fixture exactly, and
  every name resolves through one tool manager (one MCP endpoint);
- each alias resolves to the SAME function object as its historical source, with
  full metadata parity (input schema, description, annotations, title, …);
- no fixture-declared bank-op/cross-cutting direct-only tool has a tiered alias
  (negative assertion — folds in the ops-exclusion); direct ``long_*`` tools
  without a historical twin remain separate from the alias projection;
- ``mid_delete`` routed through the ``/api/tool`` proxy denies identically to
  ``bank_delete`` under an insufficient-scope token (auth-gate parity);
- every ``long_*`` alias inherits its identical ``graph_*`` handler, so the
  non-authoritative / protocol-derived posture is preserved by identity
  (the commit-path boundary itself is ADR-0010 territory).

Frozen fixture (``tests/fixtures/tool_surface.json``):
    The fixture is the CHECKED-IN public-surface contract. Editing it is a
    DELIBERATE surface change. Any add/remove/rename of a public tool, any
    permission relevelling, or any change to the ``short_note`` Token=Agent
    contract MUST update both the fixture AND the calling code, AND the
    diff MUST be justified in the PR description (ADR-0002 grammar +
    ADR-0005 alias lifecycle). The tests below assert that the running
    registration exactly matches the fixture; CI fails RED on any drift.

Normative product mapping: ``docs/TOOL_MAPPING.md`` (ADR-0002 + ADR-0005).

AST-based permission profile:
    The previous version of ``_effective_permission_level()`` scanned raw
    handler source for substrings like ``check_manage_permission``, so an
    import line, a comment, or a dead/conditional branch was enough to
    satisfy the fixture — softening the *live* gate could go undetected.
    The new walker parses each handler's source with ``ast`` and inspects
    actual ``ast.Call`` nodes for ``check_*_permission`` / ``check_access``,
    classifying each call as:

      - **unconditional** — the call is not nested inside any ``ast.If``
        whose ``test`` references one of the handler's parameters; or
      - **conditional**   — the call IS inside such an ``If``, keyed by a
        deterministic canonical form of the ``If.test`` expression (e.g.
        ``"include_volatile=True"`` for ``graph_push``).

    Fixture entries in ``permission_level`` may therefore be EITHER:

      - a scalar string (single unconditional gate, no param-conditional
        gates), e.g. ``"manage"``; OR
      - an object ``{"default": <level>, "conditional": {<key>: <level>}}``
        where ``<key>`` is the canonical ``If.test`` form. Examples:
          - ``graph_push`` →
            ``{"default":"write","conditional":{"include_volatile=True":"manage"}}``

    Convention for condition keys (deterministic, one form per ``If``):
      - ``Name(id=p)`` where ``p`` is a parameter → ``"<p>=True"``;
      - ``UnaryOp(Not, Name(id=p))``              → ``"<p>=False"``;
      - ``Compare(p, [NotEq], [q])`` where p is a param → ``"<p>!=<q>"``;
      - any other parameter-referencing test → ``ast.unparse(test)``.

    Mutation proof (manual, documented here):
      Softening ``check_manage_permission()`` to ``check_write_permission()``
      inside ``graph_push``'s ``if include_volatile:`` branch flips the
      derived profile's ``conditional["include_volatile=True"]`` from
      ``"manage"`` to ``"write"``, which no longer equals the fixture's
      ``{"default":"write","conditional":{"include_volatile=True":"manage"}}``
      and the corresponding assertion goes RED.

Token-context auth gates without ``check_*`` helpers:
    Three handlers (``backup_list``, ``space_list``, ``system_whoami``) gate
    on token presence directly via ``current_token_info.get()`` /
    ``_get_effective_token_info()`` followed by an ``if token_info is None:
    return error`` pattern, rather than through a ``check_*_permission``
    helper. The round-1 walker only inspected ``check_*`` Call nodes, so it
    saw nothing and collapsed these handlers to ``"public"`` — but the live
    code requires a token, which is at least ``"read"`` per ``TOOL_MAPPING.md``.

    The walker now also detects this token-context auth shape:

      - any ``Call`` to ``current_token_info.get()`` (an ``ast.Attribute``
        access on the ``current_token_info`` ContextVar);
      - any ``Call`` to a token-context helper named one of
        ``_get_effective_token_info``, ``get_token_info``,
        ``get_effective_token_info``;

    AND the call's return value must be guarded by an
    ``if <var> is None: return <error>`` pattern in the same scope (the
    actual auth gate). When found, the walker adds ``"read"`` to the
    appropriate level set (unconditional or parameter-conditional, by the
    same enclosing-``If`` logic the ``check_*`` walker uses). Pure
    look-ups for logging / audit (no follow-up None-guard return) are
    intentionally ignored, so ``_emit_volatile_optin_audit``-style helpers
    do not get falsely classified as gates.

    Behavioral counterpart: ``test_backup_list_requires_auth`` exercises
    ``backup_list`` with ``current_token_info`` set to ``None`` and asserts
    the handler denies — a runtime check on the gate.

Behavioral guards around conditional authorization:
    ``bank_consolidate`` now has an unconditional ``write`` floor plus
    parameter-conditional ``manage`` calls for explicit global and cross-agent
    scope. Its full profile is visible to the AST fixture. ``long_ingest`` still
    has a real gate shape the structural walker cannot fully express:

      - ``long_ingest`` (``src/live_mem/tools/graph.py:578-597``) :
        ``check_manage_permission()`` only runs on the volatile opt-in
        path, gated by a *non-parameter* ``if offending`` test then a
        ``if not include_volatile: return`` early-out — neither of which
        the walker keys by parameter conditional. The fixture pins
        ``long_ingest`` to scalar ``"manage"``, but the default path
        (``include_volatile=False`` with non-volatile docs) only goes
        through ``check_access``.

    Together with ``test_graph_push_conditional_manage_gate_is_actually_enforced``,
    the behavioral tests below exercise the live conditional guards end-to-end
    with a write-only token. They complement, rather than replace, the AST
    profile lock.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.tools import call_tool_direct, register_all_tools
from live_mem.tools.aliases import ALIAS_MAP, register_tier_aliases
from live_mem.tools.exposure import TOOL_EXPOSURES, ToolAudience

# Frozen surface fixture (P6-3). See module docstring for the change protocol.
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tool_surface.json"
FIXTURE: dict = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# Permission-helper name → level. The ordering of LEVEL_RANK is also the
# "higher wins" order when several helpers are called in the same scope
# (e.g. an unconditional check_access AND an unconditional
# check_write_permission together collapse to "write").
_HELPER_LEVEL: dict[str, str] = {
    "check_access": "read",
    "check_write_permission": "write",
    "check_manage_permission": "manage",
    "check_admin_permission": "admin",
}
# Token-context helpers (P6-3 round-4): a Call to one of these returns the
# current token info (or None when unauthenticated). When the handler then
# guards on `if <var> is None: return <error>`, that pattern IS the auth
# gate — equivalent to a `check_access`-style ``read`` minimum.
_TOKEN_CONTEXT_HELPERS: frozenset[str] = frozenset({
    "_get_effective_token_info",
    "get_token_info",
    "get_effective_token_info",
})
_TOKEN_CONTEXT_LEVEL: str = "read"
LEVEL_RANK: dict[str, int] = {
    "public": 0, "read": 1, "write": 2, "manage": 3, "admin": 4,
}


def _max_level(levels: set[str]) -> str:
    if not levels:
        return "public"
    return max(levels, key=lambda lv: LEVEL_RANK[lv])


def _canonical_condition_key(
    test: ast.expr, params: set[str], *, branch_truthy: bool
) -> str:
    """Deterministic, one-per-(If, branch) condition key used in the fixture.

    ``branch_truthy`` is True when the check_* call sits in the ``If.body``
    (so the test must evaluate truthy for the check to run), False when it
    sits in the ``If.orelse`` branch. The key is normalised so that a body
    sitting under ``if include_volatile:`` and an orelse sitting under
    ``if not include_volatile:`` collapse to the same canonical key
    (``include_volatile=True``).

    Handles the two real-world shapes we have today (``include_volatile``
    on ``graph_push``, ``space_id`` on ``backup_create`` /
    ``backup_list``, agent-vs-caller comparisons on ``bank_consolidate``
    if a check_* ever moves into one of those branches) plus a generic
    deterministic ``ast.unparse`` fallback.
    """
    # Strip a top-level `not` and flip the branch sense so `if not x: A else: B`
    # and `if x: B else: A` produce identical keys for A and B respectively.
    inverted = False
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
        inverted = not inverted
    effective_truthy = branch_truthy != inverted  # XOR

    if isinstance(test, ast.Name) and test.id in params:
        return f"{test.id}={effective_truthy}"
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if (
            isinstance(left, ast.Name)
            and isinstance(right, ast.Name)
            and left.id in params
        ):
            # Normalise `==` / `!=` by branch sense so the orelse of
            # `if a != b:` keys as `a==b`, matching the body of `if a == b:`.
            if isinstance(op, ast.NotEq):
                return f"{left.id}!={right.id}" if effective_truthy else f"{left.id}=={right.id}"
            if isinstance(op, ast.Eq):
                return f"{left.id}=={right.id}" if effective_truthy else f"{left.id}!={right.id}"
    # Generic deterministic fallback (still stable across runs).
    body_form = ast.unparse(test)
    return body_form if effective_truthy else f"not ({body_form})"


def _test_references_a_param(test: ast.expr, params: set[str]) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Name) and node.id in params:
            return True
    return False


def _effective_permission_profile(fn) -> dict:
    """Static AST-derived permission profile for a live MCP handler.

    Returns a dict of the form::

        {"default": <level>, "conditional": {<cond_key>: <level>, ...}}

    where ``<level>`` ∈ ``{"public","read","write","manage","admin"}``.

    - ``default``     — max level over all ``check_*`` calls NOT nested in
      any ``ast.If`` whose ``test`` mentions a function parameter.
    - ``conditional`` — for each parameter-conditional ``ast.If`` that
      contains ``check_*`` call(s), the max level inside it, keyed by the
      canonical form of ``If.test`` (see ``_canonical_condition_key``).

    Identity ⇒ identical profile: alias and canonical share one ``fn``
    object, so the parse and traversal are bit-for-bit identical and
    can never silently diverge — the source IS the gate.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    module = ast.parse(src)
    # The handler is the first top-level def in the source slice.
    funcdef = next(
        (
            n for n in module.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if funcdef is None:
        return {"default": "public", "conditional": {}}

    params = {a.arg for a in funcdef.args.args}
    params |= {a.arg for a in funcdef.args.kwonlyargs}
    if funcdef.args.vararg:
        params.add(funcdef.args.vararg.arg)
    if funcdef.args.kwarg:
        params.add(funcdef.args.kwarg.arg)

    # Build parent links so we can find each Call's enclosing If chain, and
    # remember which branch (body / orelse) the child came from — we need
    # the branch sense to normalise the condition key.
    parents: dict[int, ast.AST] = {}
    branch_of: dict[int, bool] = {}  # True == came from If.body, False == orelse
    for parent in ast.walk(funcdef):
        if isinstance(parent, ast.If):
            for child in parent.body:
                parents[id(child)] = parent
                branch_of[id(child)] = True
            for child in parent.orelse:
                parents[id(child)] = parent
                branch_of[id(child)] = False
        else:
            for child in ast.iter_child_nodes(parent):
                # Only fill if not already a body/orelse child of an If we saw
                # (those took precedence above).
                if id(child) not in parents:
                    parents[id(child)] = parent

    def _ascend_to_stmt(n: ast.AST) -> ast.AST:
        """Walk up until we hit a statement node that has a recorded branch.

        Expressions inside `If.test`/conditions also have parents, but they
        cannot themselves contain a check_* gate (gates are statements). We
        only care about the *statement* that physically sits inside the
        If's body or orelse list.
        """
        cur = n
        while id(cur) in parents:
            if id(cur) in branch_of:
                return cur
            parent = parents[id(cur)]
            if parent is funcdef:
                return cur
            cur = parent
        return cur

    unconditional_levels: set[str] = set()
    conditional: dict[str, set[str]] = {}

    def _classify(call_node: ast.AST, level: str) -> None:
        """Add ``level`` to either the unconditional or the right conditional
        bucket, depending on whether ``call_node``'s enclosing statement
        sits inside a parameter-conditional ``If``. Shared between the
        ``check_*`` walker and the token-context gate walker so both honour
        the exact same conditional/unconditional classification."""
        guarding_if: ast.If | None = None
        branch_truthy: bool = True
        cur: ast.AST = call_node
        while id(cur) in parents:
            stmt = _ascend_to_stmt(cur)
            parent = parents.get(id(stmt))
            if parent is None or parent is funcdef:
                break
            if isinstance(parent, ast.If) and _test_references_a_param(parent.test, params):
                guarding_if = parent
                branch_truthy = branch_of.get(id(stmt), True)
                break
            cur = parent
        if guarding_if is None:
            unconditional_levels.add(level)
        else:
            key = _canonical_condition_key(
                guarding_if.test, params, branch_truthy=branch_truthy
            )
            conditional.setdefault(key, set()).add(level)

    for node in ast.walk(funcdef):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        helper_name = func.id if isinstance(func, ast.Name) else None
        if helper_name not in _HELPER_LEVEL:
            continue
        _classify(node, _HELPER_LEVEL[helper_name])

    # --- P6-3 round-4: token-context auth gates (no check_* helper) ----------
    # Detect the live-code shape:
    #
    #     token_info = current_token_info.get()    # or _get_effective_token_info()
    #     if token_info is None:
    #         return {"status": "error", ...}
    #
    # The Assign produces a name; the If-None-return gate is the actual
    # auth check. When both are present in the same scope, classify the
    # token-fetch Call as a ``read``-level gate (using the same
    # parameter-conditional placement logic as the ``check_*`` walker).
    token_assigns: dict[str, ast.Call] = {}  # var name -> the originating Call node
    for node in ast.walk(funcdef):
        if not isinstance(node, ast.Assign):
            continue
        # Single-target simple Name assignment (the common shape).
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        target_name = node.targets[0].id
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        is_token_context = False
        # current_token_info.get(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "current_token_info"
        ):
            is_token_context = True
        # _get_effective_token_info(...) / get_token_info(...) / get_effective_token_info(...)
        elif isinstance(func, ast.Name) and func.id in _TOKEN_CONTEXT_HELPERS:
            is_token_context = True
        if is_token_context:
            token_assigns[target_name] = value

    for node in ast.walk(funcdef):
        if not isinstance(node, ast.If):
            continue
        # Test must be `<name> is None`.
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.left, ast.Name)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            continue
        name = test.left.id
        if name not in token_assigns:
            continue
        # Body must contain a Return — the actual deny — at any depth.
        has_return = any(isinstance(n, ast.Return) for n in ast.walk(node))
        if not has_return:
            continue
        # Classify the originating token-fetch Call, NOT the If — placement
        # follows the Call's enclosing statement chain, so an If-None-return
        # inside `if space_id:` is correctly placed under that conditional
        # key just like a `check_access` call would be.
        _classify(token_assigns[name], _TOKEN_CONTEXT_LEVEL)

    return {
        "default": _max_level(unconditional_levels),
        "conditional": {k: _max_level(v) for k, v in conditional.items()},
    }


def _normalise_fixture_entry(entry) -> dict:
    """Bring a fixture entry to the canonical {default, conditional} shape.

    Scalar entries (the common case) become ``{"default": <s>, "conditional": {}}``;
    object entries are returned as-is after a light shape check."""
    if isinstance(entry, str):
        return {"default": entry, "conditional": {}}
    assert isinstance(entry, dict), f"unexpected fixture entry type: {type(entry)!r}"
    assert set(entry.keys()) == {"default", "conditional"}, (
        f"fixture entry must have exactly keys 'default' and 'conditional', got {entry!r}"
    )
    assert isinstance(entry["default"], str)
    assert isinstance(entry["conditional"], dict)
    return entry


def _effective_permission_level(fn) -> str:
    """Backwards-compatible scalar accessor.

    Returns the profile's ``default`` level. Kept so any consumer outside
    this module that imported the old helper keeps working; the rich
    ``_effective_permission_profile`` is what the new assertions use."""
    return _effective_permission_profile(fn)["default"]


# --- The frozen surface (sole expected-surface authority) -------------------
#
# Do not restate the names or counts in Python constants. Every projection below
# is derived from the checked-in fixture, so a deliberate surface change has one
# expected-contract edit and one exhaustive live-registration comparison.
EXPECTED_HISTORICAL: frozenset[str] = frozenset(FIXTURE["historical"])
EXPECTED_ALIAS_PAIRS: dict[str, str] = dict(FIXTURE["alias_map"])
EXPECTED_ALIASES: frozenset[str] = frozenset(EXPECTED_ALIAS_PAIRS.values())
EXPECTED_TOTAL: int = FIXTURE["total"]

# Direct-only projections are derived from that same authority. The two net-new
# long_* tools are historical/direct registrations with no graph_* twin.
BANK_OPS_NO_ALIAS: frozenset[str] = frozenset(
    n
    for n in EXPECTED_HISTORICAL
    if n.startswith("bank_") and n not in EXPECTED_ALIAS_PAIRS
)
CROSS_CUTTING_NO_ALIAS: frozenset[str] = frozenset({
    n for n in EXPECTED_HISTORICAL
    if n.startswith(("system_", "space_", "token_", "backup_", "admin_"))
})


def _build() -> tuple[FastMCP, int]:
    mcp = FastMCP(name="test")
    total = register_all_tools(mcp)
    return mcp, total


_ALIAS_METADATA_FIELDS = (
    "description",
    "annotations",
    "parameters",
    "title",
    "icons",
    "meta",
    "output_schema",
)


def _assert_alias_identity_and_metadata(
    tools: dict, alias_map: dict[str, str]
) -> None:
    for historical, canonical in alias_map.items():
        src, alias = tools[historical], tools[canonical]
        assert callable(src.fn), f"alias source {historical!r} is not callable"
        assert alias.fn is src.fn, (
            f"alias {canonical!r} fn is not the SAME object as {historical!r}"
        )
        for field in _ALIAS_METADATA_FIELDS:
            assert getattr(alias, field) == getattr(src, field), (
                f"{canonical} {field} drift from {historical}"
            )


_PUBLIC_SCHEMA_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "legacy work-item identifier": re.compile(r"\bLM2-\d+\b"),
    "private project phase": re.compile(r"(?<![A-Za-z0-9])P\d+-\d+\b"),
    "private issue or review reference": re.compile(
        r"\b(?:issue|pull request|PR|review(?:\s+PR)?)\s*#?\s*\d+\b",
        re.IGNORECASE,
    ),
    "private design path": re.compile(r"\bDESIGN/"),
    "legacy product identity": re.compile(r"\blive[- ]memory\b", re.IGNORECASE),
    "internal version claim": re.compile(
        r"(?<![A-Za-z0-9])v\d+\.\d+(?:\.\d+)?(?:\+)?",
        re.IGNORECASE,
    ),
    "internal architecture identifier": re.compile(r"\b(?:ADR|HM)-\d+\b"),
    "internal decision identifier": re.compile(
        r"\b(?:D\d+|C-Q\d+(?:\.[a-z])?|codex-gated)\b", re.IGNORECASE
    ),
    "private logger name": re.compile(r"\blive_mem\.audit\b"),
    "obsolete future promise": re.compile(
        r"\b(?:future (?:revision|version|release)|planned for|apply lands)\b",
        re.IGNORECASE,
    ),
}

_CORE_FRENCH_COPY = re.compile(
    r"\b(?:identifiant|espace|espaces|fichier|fichiers|retourne|retourné|"
    r"lire|lit|écrit|écriture|outil|nécessite|défaut|filtrer|contenu|"
    r"règles|synthèse|données|supprime|crée|planifie|requête|résultat|"
    r"résultats|nombre|autorisé|autorise|doit|après|avant|aucun|tous|"
    r"toutes|nouveau|nouvelle|courant)\b",
    re.IGNORECASE,
)


def _registered_public_schema_texts(mcp: FastMCP) -> dict[str, str]:
    """Render exactly what FastMCP exposes: tool copy plus input JSON Schema."""
    return {
        name: "\n".join(
            (
                tool.description or "",
                json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True),
            )
        )
        for name, tool in mcp._tool_manager._tools.items()
    }


def _assert_public_schema_hygiene(
    schemas: dict[str, str], *, agent_core_names: set[str]
) -> None:
    findings: list[str] = []
    for name, text in schemas.items():
        for label, pattern in _PUBLIC_SCHEMA_FORBIDDEN.items():
            match = pattern.search(text)
            if match:
                findings.append(f"{name}: {label}: {match.group(0)!r}")
        if name in agent_core_names:
            match = _CORE_FRENCH_COPY.search(text)
            if match:
                findings.append(
                    f"{name}: agent-core copy is not canonical English: "
                    f"{match.group(0)!r}"
                )
    assert not findings, "FastMCP public-schema hygiene failures:\n" + "\n".join(
        findings
    )


def _token(name: str, permissions: list[str], allowed: list[str]) -> dict:
    return {
        "client_name": name,
        "permissions": permissions,
        "allowed_resources": allowed,
    }


# --- Structural and behavioral guards ---------------------------------------


def test_every_registered_fastmcp_description_and_input_schema_is_public_safe():
    """Audit every tool in the real registered contract, including aliases."""
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    agent_core_names = {
        entry.canonical_name
        for entry in TOOL_EXPOSURES
        if entry.audience is ToolAudience.AGENT_CORE
    }

    assert agent_core_names <= set(tools)

    missing_copy: list[str] = []
    for name, tool in tools.items():
        if not (tool.description or "").strip():
            missing_copy.append(f"{name}: tool description")
        for field_name, schema in tool.parameters.get("properties", {}).items():
            if not (schema.get("description") or "").strip():
                missing_copy.append(f"{name}.{field_name}: Field description")
    assert not missing_copy, "Missing FastMCP public copy:\n" + "\n".join(
        missing_copy
    )

    _assert_public_schema_hygiene(
        _registered_public_schema_texts(mcp), agent_core_names=agent_core_names
    )


@pytest.mark.parametrize(
    "mutant",
    [
        "LM2-31",
        "P4-7",
        "issue #13",
        "review PR #14",
        "DESIGN/live-mem/ARCHITECTURE.md",
        "Live Memory",
        "v2.7.0+",
        "ADR-0010",
        "HM-10",
        "D13",
        "C-Q2.a",
        "live_mem.audit",
        "future revision",
        "Identifiant de l'espace cible",
    ],
)
def test_fastmcp_public_schema_hygiene_guard_is_mutation_proven(mutant: str):
    """Each forbidden class independently trips the dynamic schema guard."""
    with pytest.raises(AssertionError, match="FastMCP public-schema hygiene"):
        _assert_public_schema_hygiene(
            {"short_note": f"Append a note. {mutant}"},
            agent_core_names={"short_note"},
        )


def test_direct_only_tools_have_no_tiered_alias():
    mcp, _ = _build()
    names = set(mcp._tool_manager._tools)
    # None of the bank-op/cross-cutting direct tools is an alias source.
    for hist in BANK_OPS_NO_ALIAS | CROSS_CUTTING_NO_ALIAS:
        assert hist not in ALIAS_MAP, f"{hist} must not be aliased"
    # Bank-ops would-be aliases must not exist (their suffixes are unique, unlike
    # e.g. `*_list` which legitimately collides with bank_list -> mid_list).
    for missing in (
        "mid_consolidation_status", "mid_consolidation_queues",
        "mid_stale_spaces", "mid_repair", "mid_compact",
    ):
        assert missing not in names, f"unexpected alias {missing}"
    # Globally, the only short_/mid_/long_ aliases are fixture-declared aliases
    # (the net-new long_* historical tools long_ingest / long_query, P4-7, have
    # no graph_* twin and are excluded via EXPECTED_HISTORICAL), so no
    # cross-cutting tool gained a tiered alias either.
    tiered = {
        n
        for n in names
        if n.startswith(("short_", "mid_", "long_")) and n not in EXPECTED_HISTORICAL
    }
    assert tiered == EXPECTED_ALIASES


@pytest.mark.parametrize(
    ("alias_map", "message", "source_without_callable"),
    [
        ({"does_not_exist_tool": "zzz_alias"}, "not registered", None),
        ({"live_note": "short_note"}, "already registered", None),
        (
            {"live_note": "x_duplicate", "live_read": "x_duplicate"},
            "duplicate canonical",
            None,
        ),
        ({"live_note": "x_no_callable"}, "has no callable", "live_note"),
    ],
    ids=(
        "missing-source",
        "registered-canonical",
        "intra-map-collision",
        "source-without-callable",
    ),
)
def test_alias_registration_fails_closed(
    alias_map, message, source_without_callable
):
    """Keep each fail-closed branch distinguishable under mutation.

    Removing any one production guard changes the raised branch/message or
    removes the exception, so the corresponding case goes RED instead of being
    accidentally satisfied by a sibling collision check.
    """
    mcp, _ = _build()
    source = (
        mcp._tool_manager._tools[source_without_callable]
        if source_without_callable
        else None
    )
    original_fn = source.fn if source is not None else None
    try:
        if source is not None:
            source.fn = None
        with pytest.raises(RuntimeError, match=message):
            register_tier_aliases(mcp, alias_map)
    finally:
        if source is not None:
            source.fn = original_fn


def test_alias_metadata_parity_guard_rejects_a_mutated_field():
    """Mutation proof for the exhaustive metadata-parity helper."""
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    original = tools["short_note"].description
    try:
        tools["short_note"].description = f"{original} drift"
        with pytest.raises(AssertionError, match="short_note description drift"):
            _assert_alias_identity_and_metadata(tools, EXPECTED_ALIAS_PAIRS)
    finally:
        tools["short_note"].description = original


# --- Folded P1-4: destructive auth-gate parity through the proxy -------------

@pytest.mark.asyncio
@pytest.mark.parametrize("perms", [["read"], ["write"]])
async def test_mid_delete_denies_identically_to_bank_delete_via_proxy(perms):
    mcp, _ = _build()  # sets the module _mcp_ref used by call_tool_direct
    args = {"space_id": "proj", "filename": "x.md", "confirm": True}
    tok = current_token_info.set(_token("low", perms, ["proj"]))
    try:
        bank_res = await call_tool_direct("bank_delete", dict(args))
        mid_res = await call_tool_direct("mid_delete", dict(args))
    finally:
        current_token_info.reset(tok)
    # The space is allowed (check_access passes) but the token lacks 'manage',
    # so the in-handler gate denies before storage — identically for source &
    # alias. The 'write' case also guards against a manage->write softening of
    # the gate (write < manage, so a 'write' token must still be denied).
    assert bank_res.get("status") == "error"
    assert mid_res == bank_res


def test_tool_mapping_doc_lists_all_canonical_alias_pairs():
    # Machine-link doc <-> code: every aliased name (historical source + its
    # canonical target) must appear in the normative TOOL_MAPPING.md, so a
    # code/doc drift on every mapped name is caught in CI (tool naming consistent
    # in code, docs, and tests).
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[1]
        / "docs" / "TOOL_MAPPING.md"
    ).read_text(encoding="utf-8")
    for historical, canonical in EXPECTED_ALIAS_PAIRS.items():
        assert historical in doc, f"{historical} missing from TOOL_MAPPING.md"
        assert canonical in doc, f"{canonical} missing from TOOL_MAPPING.md"


# --- P6-3: frozen fixture surface lock + alias permission/identity parity ----

def test_fixture_is_internally_consistent():
    """The fixture alone must be self-coherent before we compare it to the
    live registration. This catches a malformed edit before it can mask a
    real drift."""
    historical = list(FIXTURE["historical"])
    alias_map = FIXTURE["alias_map"]
    aliases = set(alias_map.values())

    assert FIXTURE["historical_count"] == len(historical)
    assert FIXTURE["alias_count"] == len(alias_map)
    assert FIXTURE["total"] == len(set(historical) | aliases)
    assert FIXTURE["historical_count"] + FIXTURE["alias_count"] == FIXTURE["total"]
    assert len(set(historical)) == FIXTURE["historical_count"]
    assert len(aliases) == FIXTURE["alias_count"]

    # Tier buckets must exactly partition the alias set.
    by_tier = FIXTURE["tier_aliases"]
    counts = FIXTURE["tier_alias_counts"]
    assert set(counts) == set(by_tier) == {"short", "mid", "long"}
    assert sum(counts.values()) == FIXTURE["alias_count"]
    for tier, names in by_tier.items():
        assert len(names) == counts[tier]
        for n in names:
            assert n.startswith(f"{tier}_"), n
    bucketed = set().union(*by_tier.values())
    assert bucketed == aliases

    # Permission-level coverage must match the historical surface exactly.
    # An entry is either a scalar level OR a {default, conditional} object.
    perms = FIXTURE["permission_level"]
    assert set(perms.keys()) == set(historical)
    allowed_levels = {"public", "read", "write", "manage", "admin"}
    for name, entry in perms.items():
        profile = _normalise_fixture_entry(entry)
        assert profile["default"] in allowed_levels, (name, profile)
        for cond_key, cond_level in profile["conditional"].items():
            assert isinstance(cond_key, str) and cond_key, (name, cond_key)
            assert cond_level in allowed_levels, (name, cond_key, cond_level)


def test_registered_surface_mapping_and_metadata_match_canonical_fixture():
    """The one exhaustive live MCP surface/mapping/metadata authority.

    RED on any unplanned name/count drift, source↔alias mapping drift, alias
    implementation split, metadata mismatch, permission-profile change, or
    second tool-manager surface.
    """
    mcp, total = _build()
    tools = mcp._tool_manager._tools
    names = set(tools)
    expected_names = EXPECTED_HISTORICAL | EXPECTED_ALIASES

    assert ALIAS_MAP == EXPECTED_ALIAS_PAIRS
    assert total == len(names) == EXPECTED_TOTAL
    assert names == expected_names

    # call_tool_direct reads the same single manager the protocol surface uses.
    import live_mem.tools as tools_pkg

    assert tools_pkg._mcp_ref is mcp
    _assert_alias_identity_and_metadata(tools, EXPECTED_ALIAS_PAIRS)

    # Tier buckets partition exactly the fixture aliases. Net-new direct
    # long_ingest/long_query stay in EXPECTED_HISTORICAL and are excluded.
    for tier in ("short", "mid", "long"):
        live = {
            n
            for n in names
            if n.startswith(f"{tier}_") and n not in EXPECTED_HISTORICAL
        }
        expected = set(FIXTURE["tier_aliases"][tier])
        assert live == expected, f"{tier} alias bucket drift: {live ^ expected}"
        assert len(live) == FIXTURE["tier_alias_counts"][tier]

    # Identity also means one permission profile. The AST-derived source
    # profile must match the fixture so a live authorization relevel cannot be
    # hidden behind metadata parity.
    perms = FIXTURE["permission_level"]
    for historical, canonical in EXPECTED_ALIAS_PAIRS.items():
        src, alias = tools[historical], tools[canonical]
        profile_src = _effective_permission_profile(src.fn)
        profile_alias = _effective_permission_profile(alias.fn)
        assert profile_src == profile_alias, (
            f"{historical} and alias {canonical} report different permission "
            f"profiles ({profile_src!r} vs {profile_alias!r}) — impossible "
            f"since they share one fn, indicates AST-walker drift"
        )
        expected = _normalise_fixture_entry(perms[historical])
        assert profile_src == expected, (
            f"{historical} effective permission profile {profile_src!r} != "
            f"fixture {expected!r} — relevel deliberately by editing the "
            f"fixture and justifying the change. Note: this comparison is "
            f"AST-based; softening a conditional gate (e.g. manage->write "
            f"inside `if include_volatile:`) flips a value in `conditional` "
            f"and trips this assertion."
        )


def test_all_historical_tools_match_fixture_permission_profiles():
    """Every historical/direct tool, including alias sources, must match the
    fixture permission profile. Catches a silent gate softening on any path
    (default or parameter-conditional) independently of alias parity."""
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    perms = FIXTURE["permission_level"]
    drift = []
    for name in FIXTURE["historical"]:
        actual = _effective_permission_profile(tools[name].fn)
        expected = _normalise_fixture_entry(perms[name])
        if actual != expected:
            drift.append((name, actual, expected))
    assert not drift, (
        "permission profile drift vs fixture (name, actual, fixture):\n"
        + "\n".join(f"  {n}: {a!r} != {e!r}" for n, a, e in drift)
    )


def test_short_note_alias_preserves_live_note_contract():
    """``short_note`` is the Token=Agent surface for agent-written notes
    (live_note v0.8.1). The alias MUST inherit the exact fixed category
    set and MUST NOT introduce an ``agent`` parameter (the agent identity
    is always taken from the auth token's client_name; a param would let
    a caller forge a note as someone else and break consolidator joins)."""
    mcp, _ = _build()
    tools = mcp._tool_manager._tools
    short_note = tools["short_note"]
    live_note = tools["live_note"]

    # 1. Same impl object — no separate parameter validation path.
    assert short_note.fn is live_note.fn

    # 2. No ``agent`` parameter on the alias (or its source).
    contract = FIXTURE["live_note_contract"]
    assert contract["agent_parameter_forbidden"] is True
    alias_params = set(short_note.parameters.get("properties", {}).keys())
    source_params = set(live_note.parameters.get("properties", {}).keys())
    assert alias_params == source_params, (
        f"short_note parameters drifted from live_note: "
        f"{alias_params ^ source_params}"
    )
    assert "agent" not in alias_params, (
        "short_note exposes an 'agent' parameter — breaks Token=Agent "
        "(agent identity must come from the auth token, never the caller)"
    )
    # 3. Exact parameter set matches the fixture (no silent add/remove).
    assert alias_params == set(contract["parameters"])

    # 4. The frozen category set is enforced by the shared core constant
    # the handler calls into. Asserting the constant guarantees both
    # live_note and short_note (same fn) refuse any other category.
    from live_mem.core.live import VALID_CATEGORIES

    assert list(VALID_CATEGORIES) == list(contract["categories"]), (
        "live_note VALID_CATEGORIES drifted from the frozen fixture set — "
        "any category change is a wire-format change and must update both "
        "the consolidator's category index and this fixture deliberately"
    )

    # 5. The category set must also appear verbatim in the alias's schema
    # description (no copy/paste split between source and alias).
    alias_cat_desc = (
        short_note.parameters["properties"]["category"].get("description", "")
    )
    for cat in contract["categories"]:
        assert cat in alias_cat_desc, (
            f"category {cat!r} missing from short_note category schema "
            f"description: {alias_cat_desc!r}"
        )


# --- P6-3 fix-up: behavioral mutation-proof on graph_push's conditional gate --

@pytest.mark.asyncio
async def test_graph_push_conditional_manage_gate_is_actually_enforced():
    """Behavioral counterpart to the AST-based fixture lock.

    Even if a future refactor preserved the AST shape of the conditional
    gate but somehow no-op'd it (e.g. by routing through a wrapper that
    swallows the deny), this test confirms the run-time behavior:

      - a token with ``write`` permission but NOT ``manage`` MUST receive
        a permission error when calling ``graph_push`` with
        ``include_volatile=True``;
      - a deny on the conditional path is therefore observable end-to-end,
        not just structurally. Pair with
        ``test_registered_surface_mapping_and_metadata_match_canonical_fixture``
        (AST shape + alias identity) — together they fail RED on either a
        structural softening of the gate (manage → write inside the
        ``include_volatile=True`` branch) or a behavioral no-op of it.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct
    tok = current_token_info.set(_token("scoped-writer", ["write"], ["proj"]))
    try:
        # include_volatile=True forces the conditional manage check AFTER
        # the unconditional write check passes — so a write-only token
        # MUST deny on the manage gate, not the write gate.
        res = await call_tool_direct(
            "graph_push",
            {"space_id": "proj", "include_volatile": True},
        )
    finally:
        current_token_info.reset(tok)
    assert res.get("status") == "error", (
        f"graph_push(include_volatile=True) with a write-only token must "
        f"deny on the conditional manage gate, got: {res!r}"
    )
    # The error message must point at the missing permission, not an
    # engine/connection failure — that's what proves the gate fired.
    msg = (res.get("message") or "").lower()
    assert "manage" in msg or "permission" in msg, (
        f"graph_push deny message does not look like a permission error; "
        f"the conditional manage gate may have been silently softened. "
        f"Got: {res!r}"
    )


# --- Behavioral mutation-proofs on conditional authorization paths

@pytest.mark.asyncio
async def test_bank_consolidate_cross_agent_denied_for_write_only_token():
    """Behavioral counterpart to the rich ``bank_consolidate`` AST profile.

    The handler has a default ``write`` floor and conditional ``manage`` gates
    for explicit global/cross-agent scope. The fixture locks that structure;
    this test proves the cross-agent branch also denies at runtime.

    This test pins the runtime behavior end-to-end:

      - a write-only token (``client_name="alice"``, no manage) calling
        ``bank_consolidate(space_id=..., agent="other-agent")`` MUST be
        denied with the literal ``"manage"`` permission error from
        ``bank.py``;
      - deleting / softening the manage gate flips this assertion RED.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct
    tok = current_token_info.set(_token("alice", ["write"], ["proj"]))
    try:
        res = await call_tool_direct(
            "bank_consolidate",
            {"space_id": "proj", "agent": "other-agent"},
        )
    finally:
        current_token_info.reset(tok)
    assert res.get("status") == "error", (
        f"bank_consolidate(agent='other-agent') with a write-only token "
        f"whose client_name='alice' must be denied by the cross-agent "
        f"deny branch (bank.py:405-414), got: {res!r}"
    )
    msg = (res.get("message") or "").lower()
    assert "manage" in msg or "permission" in msg, (
        f"bank_consolidate deny message does not look like the cross-agent "
        f"manage deny; the literal `if agent and agent != caller` branch "
        f"may have been silently softened. Got: {res!r}"
    )


@pytest.mark.asyncio
async def test_long_ingest_default_path_does_not_require_manage_and_volatile_does():
    """Behavioral counterpart to the AST-based fixture for ``long_ingest``.

    The live handler (``src/live_mem/tools/graph.py:578-597``) is path-
    dependent: the default ingestion path only calls ``check_access``,
    and ``check_manage_permission`` is gated by an ``if offending`` test
    then an ``if not include_volatile: return`` early-out — neither
    keyable as a parameter-conditional ``check_*`` Call. The AST walker
    therefore pins ``long_ingest`` to scalar ``"manage"`` in the fixture,
    which is the strictest gate but NOT the gate the default path hits.

    Two sub-asserts using a single write-only token (no manage):

      (a) ``long_ingest(include_volatile=False)`` with a non-volatile
          source_path MUST pass the permission gate (it may still fail
          downstream on missing engines / wiring — that is acceptable;
          only the auth gate matters here). Concretely, the response
          must NOT be the volatile-rejection error and must NOT be the
          ``check_manage_permission`` deny.

      (b) ``long_ingest(include_volatile=True)`` with a volatile
          source_path (``activeContext.md``) MUST deny on the manage
          gate after the volatile guard accepts the opt-in.

    Softening the manage gate inside the ``if not include_volatile:
    return`` branch (e.g. removing ``check_manage_permission`` after
    opt-in) flips sub-assert (b) RED.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct

    # (a) Default path: include_volatile=False, non-volatile doc → the
    # auth gate (check_access) is the only thing the handler runs before
    # delegating to the engine. A write-only token MUST get past auth.
    tok = current_token_info.set(_token("scoped-writer", ["write"], ["proj"]))
    try:
        res_default = await call_tool_direct(
            "long_ingest",
            {
                "space_id": "proj",
                "documents": [
                    {"source_path": "docs/notes.md", "content": "hello"}
                ],
                "mode": "dry-run",
            },
        )
    finally:
        current_token_info.reset(tok)
    # The auth gate must NOT fire: any error here must NOT be the
    # ``check_manage_permission`` deny (would mean the default path got
    # silently lifted to manage). The handler may legitimately fail
    # downstream (engine wiring), but not on the manage gate.
    if res_default.get("status") == "error":
        msg = (res_default.get("message") or "").lower()
        assert "manage" not in msg, (
            f"long_ingest default path (include_volatile=False) denied on "
            f"the manage gate for a write-only token — the default path "
            f"must not require 'manage'. Got: {res_default!r}"
        )

    # (b) Opt-in path: include_volatile=True, volatile basename → after
    # the volatile guard accepts the opt-in, check_manage_permission()
    # MUST deny a write-only token.
    tok = current_token_info.set(_token("scoped-writer", ["write"], ["proj"]))
    try:
        res_volatile = await call_tool_direct(
            "long_ingest",
            {
                "space_id": "proj",
                "documents": [
                    {"source_path": "memory-bank/activeContext.md",
                     "content": "x"}
                ],
                "mode": "dry-run",
                "include_volatile": True,
            },
        )
    finally:
        current_token_info.reset(tok)
    assert res_volatile.get("status") == "error", (
        f"long_ingest(include_volatile=True) on a volatile basename with "
        f"a write-only token must deny on the manage gate, got: "
        f"{res_volatile!r}"
    )
    msg_volatile = (res_volatile.get("message") or "").lower()
    assert "manage" in msg_volatile or "permission" in msg_volatile, (
        f"long_ingest volatile-opt-in deny message does not look like a "
        f"permission error; the manage gate after the volatile opt-in may "
        f"have been silently softened. Got: {res_volatile!r}"
    )


# --- P6-3 round-4 fix-up: behavioral mutation-proof on token-context auth gate

@pytest.mark.asyncio
async def test_backup_list_requires_auth():
    """Behavioral counterpart to the AST-based fixture for ``backup_list``.

    ``backup_list`` (``src/live_mem/tools/backup.py:191``) gates on
    ``current_token_info.get()`` then ``if token_info is None: return
    {"status": "error", ...}`` — a token-context auth check that does
    NOT route through a ``check_*_permission`` helper. The round-4
    walker now detects this shape and classifies ``backup_list`` as
    ``read``-minimum, matching ``TOOL_MAPPING.md``.

    This test pins the runtime behavior end-to-end: with no token in
    ``current_token_info``, calling ``backup_list`` MUST deny.
    Removing or no-op'ing the ``if token_info is None: return`` branch
    in the live handler flips this assertion RED.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct
    # Force the ContextVar to None (no auth). The token-context gate must fire.
    tok = current_token_info.set(None)
    try:
        res = await call_tool_direct("backup_list", {})
    finally:
        current_token_info.reset(tok)
    assert res.get("status") == "error", (
        f"backup_list with no token in current_token_info must deny on "
        f"the token-context auth gate (backup.py:191-193), got: {res!r}"
    )
    msg = (res.get("message") or "").lower()
    assert "authentification" in msg or "auth" in msg or "permission" in msg, (
        f"backup_list deny message does not look like an auth-required "
        f"error; the `if token_info is None: return` branch may have "
        f"been silently softened. Got: {res!r}"
    )


@pytest.mark.asyncio
async def test_space_list_requires_auth():
    """Behavioral counterpart to the AST-based fixture for ``space_list``.

    ``space_list`` (``src/live_mem/tools/space.py:260``) gates on
    ``_get_effective_token_info()`` then
    ``if token_info is None: return {"status": "error", ...}`` — a
    token-context auth check that does NOT route through a
    ``check_*_permission`` helper. The AST walker classifies
    ``space_list`` as ``read``-minimum based on this shape.

    The structural detector only verifies that a ``Return`` exists
    inside the ``if token_info is None`` branch; it does NOT prove
    the return is an auth-deny payload. A regression that flipped
    the branch to ``return {"status": "ok"}`` would still pass the
    AST check. This behavioral test pins the runtime contract:
    with no token in ``current_token_info``, ``space_list`` MUST
    deny with an auth-required error.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct
    # Force the ContextVar to None (no auth). The token-context gate must fire.
    tok = current_token_info.set(None)
    try:
        res = await call_tool_direct("space_list", {})
    finally:
        current_token_info.reset(tok)
    assert res.get("status") == "error", (
        f"space_list with no token in current_token_info must deny on "
        f"the token-context auth gate (space.py:260-262), got: {res!r}"
    )
    msg = (res.get("message") or "").lower()
    assert "authentification" in msg or "auth" in msg or "permission" in msg, (
        f"space_list deny message does not look like an auth-required "
        f"error; the `if token_info is None: return` branch may have "
        f"been silently softened to return ok. Got: {res!r}"
    )


@pytest.mark.asyncio
async def test_system_whoami_requires_auth():
    """Behavioral counterpart to the AST-based fixture for ``system_whoami``.

    ``system_whoami`` (``src/live_mem/tools/system.py:178``) gates on
    ``current_token_info.get()`` then
    ``if token_info is None: return {"status": "error", ...}`` — a
    token-context auth check that does NOT route through a
    ``check_*_permission`` helper. The AST walker classifies
    ``system_whoami`` as ``read``-minimum based on this shape.

    The structural detector only verifies that a ``Return`` exists
    inside the ``if token_info is None`` branch; it does NOT prove
    the return is an auth-deny payload. A regression that flipped
    the branch to ``return {"status": "ok"}`` would still pass the
    AST check. This behavioral test pins the runtime contract:
    with no token in ``current_token_info``, ``system_whoami`` MUST
    deny with an auth-required error.
    """
    mcp, _ = _build()  # also sets the module _mcp_ref used by call_tool_direct
    # Force the ContextVar to None (no auth). The token-context gate must fire.
    tok = current_token_info.set(None)
    try:
        res = await call_tool_direct("system_whoami", {})
    finally:
        current_token_info.reset(tok)
    assert res.get("status") == "error", (
        f"system_whoami with no token in current_token_info must deny on "
        f"the token-context auth gate (system.py:178-180), got: {res!r}"
    )
    msg = (res.get("message") or "").lower()
    assert "authentification" in msg or "auth" in msg or "permission" in msg, (
        f"system_whoami deny message does not look like an auth-required "
        f"error; the `if token_info is None: return` branch may have "
        f"been silently softened to return ok. Got: {res!r}"
    )
