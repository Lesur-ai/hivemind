# -*- coding: utf-8 -*-
"""
Grep-guard tests for the P6-5 rebrand and unified deployment story.

These tests do NOT exercise runtime behavior. They are deterministic
filesystem assertions that protect the public artifact surface from
regressions:

  * No stale `live-mem-*` / `live-memory:latest` / "Live Memory" tokens
    leak into release-facing files after the P6 rebrand (ADR-0018).
  * `DEFAULT_RULES_FILE` in `.env.example` resolves to a real file in
    the repo. Pinning a non-existent default would silently break
    every `space_create` call that does not pass a `rules` argument.
  * `docs/DEPLOYMENT.md` documents the upgrade path from live-memory
    and ships a non-claims fenced section. The fenced section is a
    sentinel that allows the doc to spell out V1 bounds (full-mesh
    all-ACK; NOT quorum, hub, CRDT, multi-tenant, multi-space-merge)
    while keeping those exact tokens out of the rest of the doc.
  * `docker-compose.yml` ships the embedded long runtime
    (`graph-memory` + `neo4j` + `qdrant`) in the DEFAULT profile,
    built from the repository-root context with
    `services/graph-memory/Dockerfile` (no `LONG_BACKEND_IMAGE`),
    with no host ports on the internal services (ADR-0019).
  * `scripts/release_smoke.sh` treats a disabled/unbound/unreachable
    long tier as a release FAILURE (P7-5, ADR-0019). The legacy P6-5
    disabled-state acceptance (`disabled`, `long_disabled`,
    `not_configured`, `not_connected` as valid long results) must
    never return, the smoke must default to the WAF entrypoint
    (`:8080`), and it must prove the embedded runtime via a real
    `long_push` + `connected`/`reachable` `long_status` + a non-empty
    `long_ingest` dry-run plan.

Release-wide lexical checks remain stdlib-only. The Graph build-context
contract additionally loads Compose with the already-pinned dev PyYAML parser,
rejecting duplicate keys before comparing the exact semantic mapping.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Branding deny-lists
# ---------------------------------------------------------------------------

# Tokens that MUST NOT appear in any release-facing surface after the P6
# rebrand. Each tuple is (file_relative_path, forbidden_substrings_tuple).
_RELEASE_SURFACES_WITH_DENYLIST = (
    (
        "docker-compose.yml",
        (
            "live-mem-service",
            "live-memory:latest",
            "live-mem-network",
            # ADR-0019: the embedded long runtime is repository-built; the P6
            # operator-supplied-image drift pattern must never return.
            "LONG_BACKEND_IMAGE",
        ),
    ),
    (
        "waf/Caddyfile",
        (
            "live-mem-service",
            "live-mem-network",
        ),
    ),
    (
        "pyproject.toml",
        (
            'name = "live-memory"',
        ),
    ),
    (
        ".env.example",
        (
            "MCP_SERVER_NAME=Live Memory",
            # ADR-0019: no operator-supplied long backend image.
            "LONG_BACKEND_IMAGE",
        ),
    ),
    (
        "Dockerfile",
        (
            "Live Memory MCP Server",
        ),
    ),
    # docs/DEPLOYMENT.md intentionally names the legacy `live-mem-service`
    # and `live-mem-network` strings in the "Upgrading from live-memory"
    # section — that section's job is to spell out the rename. We still
    # guard against the legacy image tag `live-memory:latest` because the
    # rebrand replaces it everywhere.
    (
        "docs/DEPLOYMENT.md",
        (
            "live-memory:latest",
        ),
    ),
)


# Non-claims tokens that MUST NOT appear OUTSIDE the explicitly fenced
# non-claims section of `docs/DEPLOYMENT.md`. These are the V1-bound
# claim words from ADR-0018 (full-mesh all-ACK is the contract; quorum
# / hub / CRDT / multi-tenant / multi-space-merge are explicitly NOT
# claimed). The non-claims section is allowed to use them precisely
# because that section's job is to disclaim them.
_NON_CLAIMS_TOKENS = (
    "quorum",
    "hub topology",
    "permanent master",
    "CRDT",
    "multi-tenant",
    "multi-space merge",
)

# Tokens forbidden anywhere in `.env.example` comment text. Operators
# read `.env.example`; the file must not casually claim semantics the
# protocol does not provide.
_ENV_EXAMPLE_NON_CLAIMS_TOKENS = (
    "quorum",
    "hub topology",
    "permanent master",
    "CRDT",
    "multi-tenant",
    "multi-space merge",
)


_NON_CLAIMS_FENCE_OPEN = "<!-- non-claims -->"
_NON_CLAIMS_FENCE_CLOSE = "<!-- /non-claims -->"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_non_claims_section(text: str) -> str:
    """
    Remove the explicitly fenced non-claims block so the deny-list scan
    only inspects the rest of the document.

    The fence MUST be a balanced pair `<!-- non-claims --> ... <!-- /non-claims -->`.
    If the open marker is present without a matching close, we keep the
    whole text in scope so the scan still flags forbidden tokens.
    """

    open_idx = text.find(_NON_CLAIMS_FENCE_OPEN)
    close_idx = text.find(_NON_CLAIMS_FENCE_CLOSE)
    if open_idx == -1 or close_idx == -1 or close_idx < open_idx:
        return text
    return text[:open_idx] + text[close_idx + len(_NON_CLAIMS_FENCE_CLOSE):]


def _iter_top_level_services(compose_text: str):
    """
    Yield (service_name, service_block_text) for every top-level service
    in `docker-compose.yml`. The parser is intentionally minimal — it
    walks the `services:` block and treats every two-space-indented
    key as a service entry, then captures the lines indented deeper
    until the next service or a top-level key.

    This lexical iterator remains useful for location-scoped service checks;
    the security-critical Graph build mapping is also parsed strictly below.
    """

    lines = compose_text.splitlines()
    in_services = False
    services_indent = None
    current_name = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.rstrip()
        if not in_services:
            if stripped == "services:":
                in_services = True
            continue

        # Detect leaving the services block: a non-blank, non-comment
        # line at column 0 means we are back at top level.
        if stripped and not line.startswith(" ") and not stripped.startswith("#"):
            if current_name is not None:
                yield current_name, "\n".join(current_lines)
            return

        # Inside the services block. A two-space-indented key starts a
        # new service entry.
        if line.startswith("  ") and not line.startswith("   "):
            bare = line[2:]
            if bare and not bare.startswith("#") and bare.rstrip().endswith(":"):
                if current_name is not None:
                    yield current_name, "\n".join(current_lines)
                current_name = bare.rstrip().rstrip(":").strip()
                current_lines = [line]
                if services_indent is None:
                    services_indent = 2
                continue

        if current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        yield current_name, "\n".join(current_lines)


class _StrictComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate explicit keys."""


def _construct_unique_mapping(
    loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False
):
    explicit_keys = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in explicit_keys
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate mapping key: {key!r}",
                key_node.start_mark,
            )
        explicit_keys.add(key)
    # Compose legitimately uses anchors/merge keys outside the Graph service.
    # After rejecting duplicate explicit keys, retain SafeLoader's standard
    # merge semantics for the actual value construction.
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictComposeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _assert_exact_graph_build_mapping(compose_text: str) -> None:
    try:
        document = yaml.load(compose_text, Loader=_StrictComposeLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise AssertionError(f"docker-compose.yml is not strict YAML: {exc}") from exc
    assert type(document) is dict
    services = document.get("services")
    assert type(services) is dict
    graph = services.get("graph-memory")
    assert type(graph) is dict
    build = graph.get("build")
    expected = {
        "context": ".",
        "dockerfile": "services/graph-memory/Dockerfile",
    }
    assert build == expected, f"Graph build mapping drifted: {build!r}"


def _service_has_profile(block: str, profile: str) -> bool:
    """
    Return True if the service block declares the given profile.

    Recognises both inline (`profiles: [long]`) and list (`profiles:\n  - long`)
    forms.
    """

    pat_inline = re.compile(
        r"^\s*profiles\s*:\s*\[([^\]]*)\]\s*$", re.MULTILINE
    )
    m = pat_inline.search(block)
    if m:
        items = [item.strip().strip('"').strip("'") for item in m.group(1).split(",")]
        return profile in items

    pat_list = re.compile(
        r"^\s*profiles\s*:\s*\n((?:\s+-\s+\S+\s*\n?)+)", re.MULTILINE
    )
    m = pat_list.search(block)
    if m:
        items = re.findall(r"-\s+(\S+)", m.group(1))
        items = [item.strip().strip('"').strip("'") for item in items]
        return profile in items

    return False


def _service_has_any_profile(block: str) -> bool:
    pat_inline = re.compile(r"^\s*profiles\s*:\s*\[", re.MULTILINE)
    pat_list = re.compile(r"^\s*profiles\s*:\s*$", re.MULTILINE)
    return bool(pat_inline.search(block) or pat_list.search(block))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_LOCKED_TAGLINE = "The open memory layer for collective agent awareness."
_LOCKED_SUPPORTING = (
    "Agents notice what others are doing, inherit what others have learned, "
    "and\nunderstand complex projects together."
)


def test_public_readme_preserves_locked_brand_hero_and_language_authority():
    """The canonical README must not paraphrase the locked public identity."""

    readme = _read(REPO_ROOT / "README.md")
    assert "# hivemind" in readme
    assert readme.count(_LOCKED_TAGLINE) >= 2
    assert _LOCKED_SUPPORTING in readme
    assert "English is the canonical contract." in readme
    assert "French README" in readme and "may trail" in readme

@pytest.mark.parametrize("rel_path,denylist", _RELEASE_SURFACES_WITH_DENYLIST)
def test_no_stale_live_mem_branding_in_release_surfaces(rel_path, denylist):
    """
    Every release-facing surface must be free of stale live-memory
    branding tokens after the P6 rebrand (ADR-0018).
    """

    path = REPO_ROOT / rel_path
    assert path.is_file(), f"missing release surface: {rel_path}"
    text = _read(path)
    found = [token for token in denylist if token in text]
    assert not found, (
        f"forbidden live-memory tokens leaked into {rel_path}: {found}"
    )


def test_default_rules_file_exists():
    """
    `.env.example` must point `DEFAULT_RULES_FILE` at a real file in the
    repo. A stale default here silently breaks every `space_create`
    that does not pass an explicit `rules` argument.
    """

    env = _read(REPO_ROOT / ".env.example")
    m = re.search(
        r"^DEFAULT_RULES_FILE\s*=\s*(\S+)\s*$", env, re.MULTILINE
    )
    assert m, "DEFAULT_RULES_FILE not set in .env.example"
    target = REPO_ROOT / m.group(1)
    assert target.is_file(), (
        f"DEFAULT_RULES_FILE points at a missing path: {target}"
    )


def test_deployment_doc_has_upgrading_section():
    """
    The deployment doc must contain a literal '## Upgrading from
    live-memory' header so operators discover the bucket-name flip
    and the service / network renames.
    """

    doc = _read(REPO_ROOT / "docs" / "DEPLOYMENT.md")
    assert re.search(
        r"^## Upgrading from live-memory\s*$", doc, re.MULTILINE
    ), "DEPLOYMENT.md is missing the '## Upgrading from live-memory' header"


def test_deployment_doc_has_what_hivemind_does_not_claim_section():
    """
    The deployment doc must ship an explicit non-claims fenced section
    so operators see the V1 bounds (ADR-0018) without having to chase
    the ADR by hand.
    """

    doc = _read(REPO_ROOT / "docs" / "DEPLOYMENT.md")
    assert re.search(
        r"^## What Hivemind does NOT claim\s*$", doc, re.MULTILINE
    ), "DEPLOYMENT.md is missing the '## What Hivemind does NOT claim' header"
    assert _NON_CLAIMS_FENCE_OPEN in doc, (
        "non-claims fence open marker missing from DEPLOYMENT.md"
    )
    assert _NON_CLAIMS_FENCE_CLOSE in doc, (
        "non-claims fence close marker missing from DEPLOYMENT.md"
    )


def test_deployment_doc_no_forbidden_non_claims_outside_fenced_section():
    """
    Forbidden non-claim tokens (quorum, hub topology, permanent master,
    CRDT, multi-tenant, multi-space merge) may only appear inside the
    explicitly fenced non-claims block. Anywhere else, they would
    misrepresent Hivemind V1's full-mesh all-ACK semantics.
    """

    doc = _read(REPO_ROOT / "docs" / "DEPLOYMENT.md")
    scoped = _strip_non_claims_section(doc)
    leaks = sorted({tok for tok in _NON_CLAIMS_TOKENS if tok in scoped})
    assert not leaks, (
        f"forbidden non-claims tokens leaked outside the fenced section "
        f"of DEPLOYMENT.md: {leaks}"
    )


def test_env_example_no_forbidden_non_claims():
    """
    `.env.example` comments must not casually claim semantics Hivemind
    does not provide. Operator-visible config files are a high-impact
    surface.
    """

    env = _read(REPO_ROOT / ".env.example")
    leaks = sorted({tok for tok in _ENV_EXAMPLE_NON_CLAIMS_TOKENS if tok in env})
    assert not leaks, (
        f"forbidden non-claims tokens leaked into .env.example: {leaks}"
    )


def test_compose_graph_memory_service_is_default_required():
    """
    The embedded long runtime (`graph-memory`) is a MANDATORY default-profile
    service built from the vendored source (ADR-0019). It must NOT be gated
    behind any profile, and it must use the Graph Dockerfile with the repository
    root context rather than pull an operator-supplied image /
    `LONG_BACKEND_IMAGE`.
    """

    compose = _read(REPO_ROOT / "docker-compose.yml")
    _assert_exact_graph_build_mapping(compose)
    graph_blocks = [
        (name, block)
        for name, block in _iter_top_level_services(compose)
        if "graph" in name.lower()
    ]
    assert graph_blocks, (
        "docker-compose.yml is missing the embedded long runtime service"
    )
    for name, block in graph_blocks:
        assert not _service_has_any_profile(block), (
            f"embedded long service `{name}` must run in the DEFAULT profile "
            f"(ADR-0019) — it must not be gated behind any profile"
        )
        assert (
            "build:" in block
            and re.search(r"(?m)^\s*context:\s*\.\s*$", block)
            and re.search(
                r"(?m)^\s*dockerfile:\s*services/graph-memory/Dockerfile\s*$",
                block,
            )
        ), (
            f"embedded long service `{name}` must use the repository-root "
            "context plus services/graph-memory/Dockerfile "
            "(no operator-supplied image)"
        )
        assert "LONG_BACKEND_IMAGE" not in block, (
            f"embedded long service `{name}` must not interpolate "
            f"LONG_BACKEND_IMAGE (P6 drift pattern forbidden by ADR-0019)"
        )


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            "      context: .\n",
            "      context: ./services/graph-memory\n",
        ),
        (
            "      dockerfile: services/graph-memory/Dockerfile\n",
            "      dockerfile: Dockerfile\n",
        ),
        (
            "      context: .\n",
            "      context: ./services/graph-memory\n"
            "      context: .\n",
        ),
        (
            "    build:\n"
            "      context: .\n"
            "      dockerfile: services/graph-memory/Dockerfile\n",
            "    build: ./services/graph-memory\n",
        ),
    ),
    ids=("old-context", "wrong-dockerfile", "duplicate-context", "scalar-build"),
)
def test_mutation_red_graph_build_mapping_must_be_exact(old: str, new: str) -> None:
    compose = _read(REPO_ROOT / "docker-compose.yml")
    assert compose.count(old) == 1
    with pytest.raises(AssertionError):
        _assert_exact_graph_build_mapping(compose.replace(old, new, 1))


def test_compose_default_profile_brings_full_embedded_stack():
    """
    The set of services without ANY `profiles` key (i.e. the services that run
    under the default `docker compose up -d`) must be exactly
    `{waf, hivemind-secrets-init, hivemind, graph-memory, neo4j, qdrant}`
    (ADR-0019 / issue #183). The embedded long
    runtime and its datastores are mandatory; MinIO stays dev-profile-only.
    """

    compose = _read(REPO_ROOT / "docker-compose.yml")
    default_services = {
        name
        for name, block in _iter_top_level_services(compose)
        if not _service_has_any_profile(block)
    }
    expected = {
        "waf",
        "hivemind-secrets-init",
        "hivemind",
        "graph-memory",
        "neo4j",
        "qdrant",
    }
    assert default_services == expected, (
        f"default compose profile must be exactly {sorted(expected)}; "
        f"got {sorted(default_services)}"
    )


def test_compose_internal_services_have_no_host_ports():
    """
    The embedded long runtime, Neo4j and Qdrant must stay on the internal
    network with NO host `ports:` mapping (ADR-0019 — only Hivemind is the public
    MCP entrypoint, behind the WAF). A commented `# ports:` debug hint is allowed.
    """

    compose = _read(REPO_ROOT / "docker-compose.yml")
    internal_only = {"graph-memory", "neo4j", "qdrant"}
    port_line = re.compile(r"^\s+ports\s*:", re.MULTILINE)
    for name, block in _iter_top_level_services(compose):
        if name in internal_only:
            # strip commented lines so a `# ports:` debug hint does not trip it
            live = "\n".join(
                ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
            )
            assert not port_line.search(live), (
                f"internal service `{name}` must not publish host ports "
                f"(ADR-0019: internal network only)"
            )


def test_mcp_server_name_default_is_hivemind():
    """
    Regression: the Pydantic `Settings` default for `mcp_server_name`
    must be `"Hivemind"` (ADR-0018). The `.env.example` default flip is
    not enough on its own — a non-Docker / no-`.env` start would still
    pick up the in-code default. Codex review (PR #110, finding #2)
    caught the prior `"Live Memory"` default.
    """

    # Import path: the package is `live_mem`. Make `src/` importable.
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from live_mem.config import Settings  # noqa: WPS433 (local import is intentional)

    # Construct without env-var override.
    saved = os.environ.pop("MCP_SERVER_NAME", None)
    try:
        s = Settings()
        assert s.mcp_server_name == "Hivemind", (
            f"Settings.mcp_server_name default must be 'Hivemind' per "
            f"ADR-0018; got {s.mcp_server_name!r}"
        )
    finally:
        if saved is not None:
            os.environ["MCP_SERVER_NAME"] = saved


# ---------------------------------------------------------------------------
# P7-5 — release smoke MUST require the embedded long runtime (ADR-0019)
# ---------------------------------------------------------------------------
#
# These guards are ANCHORED on the shell constructs of
# `scripts/release_smoke.sh` (case-branch patterns, `if [ ... ]` guards,
# variable-assignment lines) rather than on bare substrings, so a comment
# mentioning "disabled" cannot satisfy them and a re-acceptance of the
# disabled-state cannot hide behind one.

_SMOKE_PATH = "scripts/release_smoke.sh"

_DISABLED_STATE_TOKENS = (
    "disabled",
    "long_disabled",
    "not_configured",
    "not_connected",
)


def _smoke_text() -> str:
    path = REPO_ROOT / _SMOKE_PATH
    assert path.is_file(), f"missing release smoke script: {_SMOKE_PATH}"
    return _read(path)


def _case_branch_patterns(text: str):
    """
    Yield (pattern_line, first_statement_line) for every `case` branch
    pattern in the script. A branch pattern is a line whose stripped form
    ends with `)` and is composed of `|`-separated bare words (including
    `*`). The first following non-blank line is the branch's first
    statement.
    """

    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not re.fullmatch(r"[A-Za-z0-9_*|-]+\)", stripped):
            continue
        first_statement = ""
        for follow in lines[i + 1:]:
            if follow.strip():
                first_statement = follow.strip()
                break
        yield stripped[:-1], first_statement


def test_release_smoke_disabled_state_branch_fails_closed():
    """
    The long_status `case` must carry a branch that enumerates ALL four
    legacy disabled-state tokens and whose first statement is `fail`
    (P7-5 AC: disabled/long_disabled/not_configured/not_connected are
    release failures, ADR-0019).
    """

    text = _smoke_text()
    branches = list(_case_branch_patterns(text))
    assert branches, f"{_SMOKE_PATH} must validate long_status via a case statement"

    disabled_branches = [
        (pattern, first)
        for pattern, first in branches
        if any(tok in pattern.split("|") for tok in _DISABLED_STATE_TOKENS)
    ]
    assert disabled_branches, (
        f"{_SMOKE_PATH} must enumerate the legacy disabled-state tokens "
        f"{_DISABLED_STATE_TOKENS} in an explicit case branch"
    )
    for pattern, first in disabled_branches:
        alts = pattern.split("|")
        missing = [tok for tok in _DISABLED_STATE_TOKENS if tok not in alts]
        assert not missing, (
            f"disabled-state case branch `{pattern})` must enumerate all "
            f"legacy tokens; missing: {missing}"
        )
        assert first.startswith("fail"), (
            f"disabled-state case branch `{pattern})` must fail closed; "
            f"its first statement is `{first}`"
        )


def test_release_smoke_never_accepts_disabled_state_as_success():
    """
    No case branch may mix an accepting `ok`/`connected` alternative with
    a disabled-state token (the pre-P7 line was
    `ok|connected|disabled|long_disabled|not_configured|not_connected)`
    followed by a `log` — that acceptance shape must never return).
    """

    text = _smoke_text()
    for pattern, first in _case_branch_patterns(text):
        alts = pattern.split("|")
        has_accept = any(tok in alts for tok in ("ok", "connected"))
        has_disabled = any(tok in alts for tok in _DISABLED_STATE_TOKENS)
        assert not (has_accept and has_disabled), (
            f"case branch `{pattern})` mixes an accepting token with a "
            f"disabled-state token — disabled-state must be a failure "
            f"(P7-5, ADR-0019)"
        )
        if has_disabled:
            assert first.startswith("fail"), (
                f"case branch `{pattern})` matches a disabled-state token "
                f"but does not fail closed (first statement: `{first}`)"
            )


def test_release_smoke_defaults_to_waf_entrypoint():
    """
    The smoke's default URLs must target the WAF entrypoint (`:8080`,
    docker-compose.yml — the only public entry). The pre-P7 default
    `localhost:8000` bypassed the shipped topology and made the gate
    fail on a nominal stack.
    """

    text = _smoke_text()
    assert re.search(
        r'^HEALTH_URL="\$\{HIVEMIND_HEALTH_URL:-http://localhost:8080/health\}"$',
        text,
        re.MULTILINE,
    ), f"{_SMOKE_PATH} HEALTH_URL default must be the WAF entrypoint :8080"
    assert re.search(
        r'^API_URL="\$\{HIVEMIND_API_URL:-http://localhost:8080/api/tool\}"$',
        text,
        re.MULTILINE,
    ), f"{_SMOKE_PATH} API_URL default must be the WAF entrypoint :8080"
    assert "localhost:8000" not in text, (
        f"{_SMOKE_PATH} must not default to the internal hivemind port "
        f"(localhost:8000) — the WAF is the only public entrypoint"
    )


def test_release_smoke_proves_embedded_long_end_to_end():
    """
    The smoke must prove the MANDATORY embedded long runtime (ADR-0019):

      * a real `long_push` (binds the embedded runtime, P7-3) asserted
        with pushed >= 1 AND errors == 0;
      * `long_status` asserted `connected` AND `reachable`;
      * a `long_ingest` dry-run over a NON-EMPTY `documents` payload,
        asserted to return a non-empty `planned` list echoing the
        `source_path`.
    """

    text = _smoke_text()

    # Real push, not just a status read.
    assert re.search(r'mcp_call "long_push"', text), (
        f"{_SMOKE_PATH} must perform a real long_push (P7-3 binds only on push)"
    )
    # Type-safe numeric predicates (jq -e): a malformed non-numeric field must
    # FAIL the gate — shell `[ ... -lt 1 ]` on a non-numeric value prints an
    # error but does NOT fail the script inside an `if` under `set -e`
    # (Codex round-1 MEDIUM).
    assert re.search(
        r"jq -e '\(\.pushed \| type == \"number\"\) and \(\.pushed >= 1\)'",
        text,
    ), f"{_SMOKE_PATH} must assert pushed is a NUMBER >= 1 via jq -e"
    assert re.search(
        r"jq -e '\(\.errors \| type == \"number\"\) and \(\.errors == 0\)'",
        text,
    ), f"{_SMOKE_PATH} must assert errors is a NUMBER == 0 via jq -e"
    # No shell arithmetic on jq-derived gate values (the health-poll deadline
    # arithmetic on `date +%s` output is fine — that value is shell-produced).
    assert not re.search(
        r'"\$(long_pushed|long_push_errors|planned_count)"', text
    ), (
        f"{_SMOKE_PATH} must not run type-unsafe shell comparisons on "
        f"jq-derived gate values — use jq -e predicates"
    )

    # Bound AND reachable.
    assert re.search(
        r'^if \[ "\$long_connected" != "true" \]; then$', text, re.MULTILINE
    ), f"{_SMOKE_PATH} must fail when long_status is not connected"
    assert re.search(
        r'^if \[ "\$long_reachable" != "true" \]; then$', text, re.MULTILINE
    ), f"{_SMOKE_PATH} must fail when long_status is not reachable"

    # Non-empty dry-run plan with a stable source_path.
    ingest_call = re.search(r'mcp_call "long_ingest" "(.*)"', text)
    assert ingest_call, f"{_SMOKE_PATH} must perform a long_ingest dry-run"
    assert '\\"mode\\":\\"dry-run\\"' in ingest_call.group(1), (
        f"{_SMOKE_PATH} long_ingest must use mode=dry-run"
    )
    assert '\\"source_path\\"' in ingest_call.group(1), (
        f"{_SMOKE_PATH} long_ingest must send a non-empty documents payload "
        f"keyed by source_path"
    )
    assert re.search(
        r"jq -e '\(\.planned \| type == \"array\"\) and \(\(\.planned \| length\) >= 1\)'",
        text,
    ), f"{_SMOKE_PATH} must assert the dry-run plan is a non-empty ARRAY via jq -e"


def test_release_smoke_space_create_sends_required_description():
    """
    `space_create` requires a `description` argument (tools/space.py).
    Without it the smoke fails before ever reaching the long tier —
    a pre-P7 gap flagged by the P7-5 plan review.
    """

    text = _smoke_text()
    create_call = re.search(r'mcp_call "space_create" "(.*)"', text)
    assert create_call, f"{_SMOKE_PATH} must create the smoke space"
    assert '\\"description\\"' in create_call.group(1), (
        f"{_SMOKE_PATH} space_create must send the required description field"
    )


def test_release_smoke_space_create_accepts_real_service_statuses():
    """
    The REAL `SpaceService.create()` contract (core/space.py) returns
    `created` for a new space and `already_exists` for reuse — never
    `ok`/`exists`. Accepting the wrong statuses makes the release gate
    fail on a nominal stack before the long tier is even reached
    (Codex round-1 BLOCKING).
    """

    text = _smoke_text()
    assert re.search(
        r'^if \[ "\$status" != "created" \] && \[ "\$status" != "already_exists" \]; then$',
        text,
        re.MULTILINE,
    ), (
        f"{_SMOKE_PATH} must accept exactly the real space_create success "
        f"statuses: created | already_exists"
    )
    assert not re.search(
        r'"\$status" != "ok"|"\$status" != "exists"', text
    ), (
        f"{_SMOKE_PATH} must not gate space_create on the wrong ok/exists "
        f"statuses (real contract: created/already_exists)"
    )


def test_workflow_doc_smoke_section_blocks_disabled_long():
    """
    The release workflow doc's smoke section (release-gate:smoke fence)
    must state that a disabled long tier blocks the release (P7-5 AC)
    and must not describe long as opt-in anymore.
    """

    epic_path = REPO_ROOT / "docs" / "WORKFLOW_GIT_EPIC.md"
    if not epic_path.exists():
        pytest.skip("docs/WORKFLOW_GIT_EPIC.md is private-only (absent from the public release tree)")
    doc = _read(epic_path)
    start = doc.find("<!-- release-gate:smoke -->")
    assert start != -1, "WORKFLOW_GIT_EPIC.md lost the release-gate:smoke fence"
    next_fence = doc.find("<!-- release-gate:", start + 1)
    section = doc[start:next_fence] if next_fence != -1 else doc[start:]

    assert "blocks the release" in section, (
        "the smoke section must state that a disabled long tier blocks "
        "the release (P7-5 AC)"
    )
    for token in _DISABLED_STATE_TOKENS:
        assert f"`{token}`" in section, (
            f"the smoke section must name the legacy disabled-state shape "
            f"`{token}` as a failure"
        )
    assert "long is opt-in" not in section, (
        "the smoke section must not describe the long tier as opt-in "
        "(ADR-0019: mandatory embedded runtime)"
    )


def test_compose_config_parses_clean_with_no_long_backend_image():
    """
    Regression (ADR-0019): `docker-compose.yml` must (1) never reference
    `LONG_BACKEND_IMAGE` — the embedded long runtime is repository-built with
    `context: .` and `dockerfile: services/graph-memory/Dockerfile`, so there is
    no operator-supplied image to interpolate — and (2) parse cleanly with no
    operator `.env`. The static
    LONG_BACKEND_IMAGE check runs everywhere; the `docker compose config --quiet`
    parse runs when docker is on PATH.
    """

    compose_text = _read(REPO_ROOT / "docker-compose.yml")
    _assert_exact_graph_build_mapping(compose_text)
    assert "LONG_BACKEND_IMAGE" not in compose_text, (
        "docker-compose.yml must not reference LONG_BACKEND_IMAGE (ADR-0019)"
    )
    graph_block = next(
        block
        for name, block in _iter_top_level_services(compose_text)
        if name == "graph-memory"
    )
    assert re.search(r"(?m)^\s*context:\s*\.\s*$", graph_block)
    assert re.search(
        r"(?m)^\s*dockerfile:\s*services/graph-memory/Dockerfile\s*$",
        graph_block,
    )
    assert "context: ./services/graph-memory" not in graph_block

    if shutil.which("docker") is None:
        pytest.skip("docker not available — static LONG_BACKEND_IMAGE check still ran")

    env = {k: v for k, v in os.environ.items() if k != "LONG_BACKEND_IMAGE"}
    # Ensure subprocess can still find docker / its deps.
    env.setdefault("PATH", os.environ.get("PATH", ""))

    compose_path = REPO_ROOT / "docker-compose.yml"

    # `docker compose` insists on reading a `.env` next to the project
    # directory. To exercise the "operator never wrote a .env" path
    # cleanly, point `--project-directory` at a tmp dir holding an empty
    # `.env`; the test then proves the file parses without any operator
    # input at all (no .env, no LONG_BACKEND_IMAGE).
    import tempfile  # local import: test-only

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text("", encoding="utf-8")
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                tmp,
                "-f",
                str(compose_path),
                "config",
                "--quiet",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=tmp,
        )
    assert result.returncode == 0, (
        f"`docker compose config --quiet` failed with LONG_BACKEND_IMAGE "
        f"unset (exit {result.returncode}); stderr=\n{result.stderr}"
    )
