# -*- coding: utf-8 -*-
"""P8-7 (#145) — integration-gate release-surface lints.

The final P8 gate ships no new console behaviour. Its committed guards are
*release-surface* lints, not middleware behaviour pins, so they live in this
dedicated file rather than being appended to
``tests/test_admin_console_security.py`` (P8-1 plan §13 recommends sibling test
files; the integration proof itself is the Playwright harness
``scripts/admin_console_proof.py``, which is operator-run and never collected
by pytest — ``testpaths = ["tests"]``).

Design constraints (mirrors the other release lints):

* **Import-light.** The only non-stdlib import is the ``FORBIDDEN_TOKENS``
  tuple reused from :mod:`tests.test_release_non_claims_lint` so the ADR-0018
  banned-token list stays single-sourced. Nothing here imports ``live_mem``,
  ``playwright``, boto3, the network, S3 or an LLM. The Playwright harness is
  *read as text*, never imported.
* **Offline / deterministic.** Every check resolves paths from ``__file__`` and
  reads the working copy.

Guards (each has a RED-without / GREEN-with mutation candidate, noted inline):

* T-P87-1 — the admin-console UI string sources carry none of the 8 ADR-0018
  forbidden V1 non-claims tokens (the "UI strings are unswept today" gap).
* T-P87-2 — the README console section describes the *shipped* redesigned
  console, not the retired "inherited implementation" disclaimer.
* T-P87-3 — no retired emoji-labelled console section names survive in the
  operator docs.
* T-P87-4 — the proof harness pins the agreed viewports/views and the
  env-only bootstrap-key intake.
* T-P87-5 — the orphaned Cloud Temple logo asset (removed by P8-1) stays
  removed and unreferenced under ``src/``.
* T-P87-6 — live-proof synchronization keeps strong fake-data sentinels,
  keyword-only Playwright arguments, the exact typed-confirm input, and bounded
  deliberate-auth checkpoints.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.test_release_non_claims_lint import FORBIDDEN_TOKENS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STATIC = _REPO_ROOT / "src" / "live_mem" / "static"


def _read(rel_or_path) -> str:
    path = rel_or_path if isinstance(rel_or_path, Path) else _REPO_ROOT / rel_or_path
    return path.read_text(encoding="utf-8")


def _normalise_markdown(text: str) -> str:
    """Collapse markdown emphasis/blockquote wrapping so a phrase split across
    a hard line break — ``**inherited\n> implementation**`` — is still matched.

    Strips ``*``, backticks and leading ``>`` blockquote markers, then folds
    every run of whitespace (including newlines) to a single space and
    lowercases. This is why the disclaimer check below cannot be evaded by
    re-wrapping the paragraph.
    """
    stripped = text.replace("*", "").replace("`", "").replace(">", " ")
    return " ".join(stripped.split()).lower()


# ---------------------------------------------------------------------------
# T-P87-1 — admin-console UI strings carry no forbidden V1 non-claims token.
# ---------------------------------------------------------------------------

# Every UI string source the operator sees in the admin console. The router,
# shared shell (admin-app.js), the fetch/proxy layer (admin-api.js) and every
# per-view module. ``/live`` assets are EPIC-out-of-scope and excluded.
_ADMIN_UI_SOURCES: tuple[str, ...] = (
    "src/live_mem/static/admin.html",
    "src/live_mem/static/js/admin-app.js",
    "src/live_mem/static/js/admin-api.js",
) + tuple(
    str(p.relative_to(_REPO_ROOT))
    for p in sorted((_STATIC / "js" / "admin").glob("views-*.js"))
)


def test_no_forbidden_claims_in_admin_ui_sources() -> None:
    """The console UI string sources must not smuggle a banned V1 claim.

    Closes the mapped gap "UI strings are unswept today": the release
    non-claims lint sweeps Markdown surfaces, but no guard covered the strings
    a reader actually sees rendered in ``/admin``. Case-insensitive bare
    substring — UI strings have no non-claims fence mechanism.

    Mutation: add ``"quorum"`` to any view-module string -> RED.
    """
    # Guard against a silently-empty sweep (glob typo, moved directory).
    view_sources = [s for s in _ADMIN_UI_SOURCES if "/views-" in s]
    assert len(view_sources) >= 5, (
        "expected the seven redesigned view modules under "
        f"src/live_mem/static/js/admin/; found only {view_sources!r} — the "
        "sweep would be vacuous"
    )

    violations: list[str] = []
    for rel in _ADMIN_UI_SOURCES:
        path = _REPO_ROOT / rel
        assert path.exists(), f"admin UI source missing: {rel}"
        lowered = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            if token.lower() in lowered:
                violations.append(f"{rel}: forbidden non-claims token {token!r}")

    assert not violations, (
        "ADR-0018 forbidden V1 non-claims token(s) leaked into admin-console "
        "UI strings (long-tier authority / tenancy / quorum-family claims are "
        "banned in operator-visible copy):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# T-P87-2 — README console section reflects the shipped redesign.
# ---------------------------------------------------------------------------

# The retired disclaimer that framed the redesigned /admin console as inherited,
# non-target implementation. English + French, matched on the normalised
# (unwrapped) text so re-wrapping the paragraph cannot restore it silently.
_RETIRED_DISCLAIMER_EN = "inherited implementation, not the target"
_RETIRED_DISCLAIMER_FR = "implémentation héritée, pas la surface produit cible"

# The shipped admin console information architecture (P8 IA). These labels must
# appear in the README console description — English and French.
_SHIPPED_CONSOLE_LABELS = (
    "Dashboard", "Spaces", "Consolidation", "Audit", "Access", "Operator tools",
)
_SHIPPED_CONSOLE_LABELS_FR = (
    "Dashboard", "Spaces", "Consolidation", "Audit", "Access", "Outils opérateur",
)


def _console_section(raw: str, marker: str) -> str | None:
    """Return the admin-console section body (from ``marker`` to the next
    top-level heading / horizontal rule), or None if the marker is absent."""
    idx = raw.find(marker)
    if idx == -1:
        return None
    rest = raw[idx + len(marker):]
    end = len(rest)
    for stop in ("\n## ", "\n---"):
        pos = rest.find(stop)
        if pos != -1:
            end = min(end, pos)
    return rest[:end]


def test_readme_console_section_matches_shipped_console() -> None:
    """README (EN+FR) must describe the shipped console, not the old disclaimer,
    and BOTH language console sections must carry the shipped IA labels.

    Mutations: restore the "inherited implementation, not the target …"
    paragraph to README.md -> RED; remove a French IA label from
    README.fr.md's "Console d'administration" section -> RED.
    """
    en = _normalise_markdown(_read("README.md"))
    fr = _normalise_markdown(_read("README.fr.md"))

    assert _RETIRED_DISCLAIMER_EN not in en, (
        "README.md still frames the redesigned /admin console as an "
        "'inherited implementation, not the target' surface — P8 redesigned it "
        "into the target Hivemind product surface; drop the disclaimer (the "
        "inherited-viewer note may stay scoped to /live only)."
    )
    assert _RETIRED_DISCLAIMER_FR not in fr, (
        "README.fr.md porte encore l'avertissement 'implémentation héritée, "
        "pas la surface produit cible' pour /admin — la console a été refondue "
        "en P8 ; retirer l'avertissement (la note d'héritage peut rester "
        "cantonnée à /live)."
    )

    # Positive (EN): the shipped IA labels must be present in the console section.
    section_en = _console_section(_read("README.md"), "### Admin Console")
    assert section_en is not None, "README.md must keep an '### Admin Console' section"
    missing_en = [lbl for lbl in _SHIPPED_CONSOLE_LABELS if lbl not in section_en]
    assert not missing_en, (
        "README.md '### Admin Console' section is missing shipped IA "
        f"label(s): {missing_en}."
    )

    # Positive (FR): the French console section must carry the French IA labels —
    # T-P87-2 claims an EN+FR contract, so the FR section is validated too.
    section_fr = _console_section(_read("README.fr.md"), "### Console d'administration")
    assert section_fr is not None, (
        "README.fr.md must keep a '### Console d'administration' section"
    )
    missing_fr = [lbl for lbl in _SHIPPED_CONSOLE_LABELS_FR if lbl not in section_fr]
    assert not missing_fr, (
        "README.fr.md '### Console d'administration' section is missing shipped "
        f"IA label(s): {missing_fr}. Elle doit décrire Dashboard, Spaces, "
        "Space Detail, Consolidation, Audit, Access et Outils opérateur."
    )


# ---------------------------------------------------------------------------
# T-P87-3 — no retired emoji-labelled console section names in operator docs.
# ---------------------------------------------------------------------------

# The prototype/legacy console labelled its sections with emoji. The redesign
# uses plain text section names. These exact emoji+label strings must never
# resurface. The README's own decorative H2 emoji ('## 📂 Project Structure',
# '## 🔍 Troubleshooting') are NOT console section names and are intentionally
# not in this list.
_RETIRED_EMOJI_CONSOLE_LABELS: tuple[str, ...] = (
    "🚨 Stale Banks",
    "📊 Dashboard",
    "📂 Explorer",
    "🔑 Tokens",
    "🔍 Explorer",
    "💾 Backups",
    "🌉 Long Tier",
    "🌉 Long",
    "🧹 Maintenance",
)

_EMOJI_LABEL_DOCS = (
    "README.md",
    "README.fr.md",
    "FAQ.md",
    "FAQ.fr.md",
    "scripts/README.md",
    "scripts/README.fr.md",
)


def test_no_emoji_console_section_names_in_docs() -> None:
    """Retired emoji-labelled console section names must not resurface.

    Mutation: re-add '🚨 Stale Banks' to FAQ.md -> RED.
    """
    violations: list[str] = []
    for rel in _EMOJI_LABEL_DOCS:
        text = _read(rel)
        for label in _RETIRED_EMOJI_CONSOLE_LABELS:
            if label in text:
                violations.append(f"{rel}: retired emoji console label {label!r}")
    assert not violations, (
        "retired emoji-labelled console section name(s) resurfaced in the docs "
        "(the redesigned console uses plain-text section names):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# T-P87-4 — the proof harness pins the agreed viewports / views / key intake.
# ---------------------------------------------------------------------------

_HARNESS = "scripts/admin_console_proof.py"

# The agreed proof contract (issue #145 + P8-0): desktop 1440×900 and the agreed
# narrow viewport 768×1024 (P8-1 icon-rail breakpoint), across EXACTLY these
# eight shipped routes. '#/spaces/{space}' is the templated Space Detail deep
# link. This is matched by EXACT list equality (not substrings) so a mutated
# route like '#/audit-bogus' — which the router silently normalizes to Dashboard
# — cannot pass.
_EXPECTED_VIEWS = [
    "#/dashboard",
    "#/spaces",
    "#/spaces/{space}",
    "#/consolidation",
    "#/audit",
    "#/access",
    "#/operator/backups",
    "#/operator/maintenance",
]
_EXPECTED_VIEWPORTS = [(1440, 900), (768, 1024)]
# The env var the bootstrap key is read from (must match the harness constant).
_KEY_ENV_VAR_NAME = "HIVEMIND_PROOF_BOOTSTRAP_KEY"
# Substrings that mark a secret-bearing CLI option. The bootstrap key must be
# env-only, never argv (argv is visible in `ps`), so parse_args must define none.
_SECRET_OPT_TOKENS = ("key", "token", "secret", "bootstrap", "password", "credential")


def _is_env_key_expr(node: ast.AST) -> bool:
    """True iff ``node`` is EXACTLY ``os.environ.get(BOOTSTRAP_KEY_ENV|<literal>, …)``,
    optionally wrapped in a single trailing ``.strip()`` — NOT merely containing
    it. A nested read like ``("hard-coded", os.environ.get(...))[0]`` returns the
    hard-coded value at runtime and must therefore be rejected (Terra R9)."""
    # Unwrap a single trailing .strip(): os.environ.get(...).strip().
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "strip" and not node.args:
        node = node.func.value
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"):
        return False
    env = node.func.value
    if not (isinstance(env, ast.Attribute) and env.attr == "environ"):
        return False
    if not node.args:
        return False
    first = node.args[0]
    return (isinstance(first, ast.Name) and first.id == "BOOTSTRAP_KEY_ENV") or \
           (isinstance(first, ast.Constant) and first.value == _KEY_ENV_VAR_NAME)


def _harness_assignments(tree: ast.AST) -> dict:
    """Map assigned Name -> value AST node (both `x = …` and `x: T = …`)."""
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            out[node.target.id] = node.value
    return out


def test_proof_harness_pins_agreed_viewports_and_views() -> None:
    """The committed harness must STRUCTURALLY encode the agreed viewports/views
    and read the bootstrap key from the environment only — proven with ``ast``
    (stdlib) so a mere substring cannot satisfy the guard.

    Mutations that must go RED: drop the (768, 1024) viewport; remove a route
    from VIEWS; add a secret-bearing CLI option (e.g. ``--key``) to parse_args.
    """
    path = _REPO_ROOT / _HARNESS
    assert path.exists(), (
        f"{_HARNESS} must exist — the committed, reproducible P8-7 proof harness"
    )
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assigns = _harness_assignments(tree)

    # VIEWPORTS must equal EXACTLY desktop 1440×900 + narrow 768×1024.
    assert "VIEWPORTS" in assigns, "harness must define VIEWPORTS"
    viewports = ast.literal_eval(assigns["VIEWPORTS"])
    assert viewports == _EXPECTED_VIEWPORTS, (
        f"VIEWPORTS must be exactly {_EXPECTED_VIEWPORTS}, got {viewports!r}"
    )

    # VIEWS must equal EXACTLY the eight agreed routes (exact list — a mutated
    # or reordered route, e.g. '#/audit-bogus', fails; substrings would not).
    assert "VIEWS" in assigns, "harness must define VIEWS"
    views = ast.literal_eval(assigns["VIEWS"])
    assert views == _EXPECTED_VIEWS, (
        f"VIEWS must be exactly the eight shipped routes {_EXPECTED_VIEWS}, got {views!r}"
    )

    # The bootstrap key must be sourced from os.environ reading the pinned env
    # var, and EVERY assignment to the harness credential variable `key` must be
    # EXACTLY that expression — so a hard-coded literal (even nested next to an
    # unrelated os.environ.get) or an argv source fails.
    # Collect the RHS of EVERY assignment to `key` — plain (`key = …`) AND
    # annotated (`key: str = …`), so an annotated reassignment like
    # `key: str = sys.argv[1]` cannot slip past the guard (Terra R10).
    key_values: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "key" for t in node.targets):
            key_values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and isinstance(node.target, ast.Name) and node.target.id == "key":
            key_values.append(node.value)
    assert key_values, "the harness must assign a `key` credential variable"
    bad_key_assigns = [
        ast.dump(v)[:80] for v in key_values if not _is_env_key_expr(v)
    ]
    assert not bad_key_assigns, (
        "every assignment to the harness credential variable `key` must be EXACTLY "
        f"os.environ.get(BOOTSTRAP_KEY_ENV, …)[.strip()] reading {_KEY_ENV_VAR_NAME} "
        "— no hard-coded literal, nested expression, or argv source; offending "
        f"RHS: {bad_key_assigns}"
    )
    # The env var constant must resolve to the pinned name.
    assert "BOOTSTRAP_KEY_ENV" in assigns and \
        ast.literal_eval(assigns["BOOTSTRAP_KEY_ENV"]) == _KEY_ENV_VAR_NAME, (
        f"BOOTSTRAP_KEY_ENV must be pinned to {_KEY_ENV_VAR_NAME!r}"
    )

    # parse_args must define NO secret-bearing CLI option (env-only — never argv).
    secret_opts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("-"):
                    low = arg.value.lower()
                    if any(tok in low for tok in _SECRET_OPT_TOKENS):
                        secret_opts.append(arg.value)
    assert not secret_opts, (
        "parse_args must define NO secret-bearing CLI option — the bootstrap key "
        f"is env-only, never argv (visible in `ps`); found {secret_opts}"
    )


def test_proof_harness_live_synchronization_is_fail_closed() -> None:
    """Pin the corrections discovered by the P9-7 integrated live proof.

    Mutations that must go RED: restore the legitimate standalone ``TODO``
    false positive; pass the selector as a second positional Playwright
    argument; target a generic modal input instead of the destructive contract
    input; or close the cookie-wipe checkpoint before the positive login.
    """
    src = _read(_HARNESS)
    tree = ast.parse(src)
    assigns = _harness_assignments(tree)

    assert ast.literal_eval(assigns["FAKE_DATA_SENTINELS"]) == [
        "undefined",
        "NaN",
        "[object Object]",
        "Invalid Date",
        "lorem ipsum",
        "mock data",
    ]

    wait_helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_wait_for_view_loaded"
    )
    wait_call = next(
        node
        for node in ast.walk(wait_helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for_function"
    )
    assert len(wait_call.args) == 1, (
        "Page.wait_for_function must receive only its expression positionally; "
        "Playwright's arg is keyword-only"
    )
    assert any(
        keyword.arg == "arg"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "selector"
        for keyword in wait_call.keywords
    )

    assert 'page.locator("#destructiveConfirmInput")' in src
    assert "auth_recovery_checkpoint = crawl(" in src
    assert (
        'report.console_errors[auth_recovery_checkpoint:], ("/api/spaces", "/api/tool")'
        in src
    )


# ---------------------------------------------------------------------------
# T-P87-5 — the orphaned Cloud Temple logo stays removed and unreferenced.
# ---------------------------------------------------------------------------


def test_cloudtemple_asset_removed_and_unreferenced() -> None:
    """The inherited Cloud Temple logo (removed by P8-1, contract §2.2.4) must
    not resurrect, and nothing under ``src/`` may reference it.

    This is a *verification* pin — the deletion itself belongs to P8-1; P8-7
    freezes the removal so a rebrand regression cannot bring it back.

    Mutation: restore the file (or any 'cloudtemple' reference under src/) -> RED.
    """
    logo = _STATIC / "img" / "logo-cloudtemple.svg"
    assert not logo.exists(), (
        "src/live_mem/static/img/logo-cloudtemple.svg reappeared — the "
        "inherited Cloud Temple asset was removed by P8-1 and must stay gone."
    )

    src_root = _REPO_ROOT / "src"
    referencing: list[str] = []
    for path in src_root.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes().lower()
        if b"cloudtemple" in blob:
            referencing.append(str(path.relative_to(_REPO_ROOT)))
    assert not referencing, (
        "files under src/ still reference the retired 'cloudtemple' asset "
        f"(rebrand regression): {referencing}"
    )
