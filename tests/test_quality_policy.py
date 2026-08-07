"""Shared pytest taxonomy and nested-runner policy for TQ-7.

This module is deliberately import-light: ``tests/conftest.py`` loads it before
test collection in both the private checkout and the staged public tree.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Literal, Mapping, Sequence, TypedDict


ACTIVE_RUNNER_ENV = "HIVEMIND_ACTIVE_TEST_RUNNER"

PRIMARY_MARKERS: tuple[str, ...] = (
    "unit",
    "integration",
    "contract",
    "security_protocol",
    "e2e",
)
ORTHOGONAL_MARKERS: tuple[str, ...] = ("slow", "optional")

Distribution = Literal["private", "public"]
DISTRIBUTIONS: tuple[Distribution, ...] = ("private", "public")


class DedicatedSuite(TypedDict):
    """One first-class runner and the repository distributions that ship it."""

    paths: tuple[str, ...]
    command: str
    category: str
    distributions: tuple[Distribution, ...]

# Ordered, mutually exclusive primary classifications. A new ordinary test
# intentionally falls back to unit; every non-default family has an explicit
# path rule and its aggregate remains visible in CI evidence.
CLASSIFICATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("e2e", ("tests/e2e/*", "tests/test_*_e2e.py")),
    (
        "integration",
        (
            "tests/test_*integration*.py",
            "tests/test_*runtime*.py",
            "tests/test_*docker*.py",
            "tests/test_*smoke*.py",
        ),
    ),
    (
        "contract",
        (
            "tests/test_adr_*.py",
            "tests/test_architecture_contracts.py",
            "tests/test_*contract*.py",
            "tests/test_*lint*.py",
            "tests/test_*policy*.py",
            "tests/test_*quality*.py",
            "tests/test_*workflow*.py",
            "tests/test_*surface*.py",
            "tests/test_*exposure*.py",
            "tests/test_*rehearsal*.py",
            "tests/test_*topology*.py",
            "tests/test_*convergence*.py",
            "tests/test_*ci*.py",
            "tests/test_*documentation*.py",
            "tests/test_check_doc_links.py",
            "tests/test_public_release_*.py",
            "tests/test_cli_surface_sync.py",
        ),
    ),
    (
        "security_protocol",
        (
            "tests/test_hivemind_*.py",
            "tests/test_mesh_*.py",
            "tests/test_*hive*.py",
            "tests/test_*peer*.py",
            "tests/test_*lease*.py",
            "tests/test_*commit*.py",
            "tests/test_*enrollment*.py",
            "tests/test_*recovery*.py",
            "tests/test_*backup*.py",
            "tests/test_*reservation*.py",
            "tests/test_*security*.py",
            "tests/test_*token*.py",
            "tests/test_*access*.py",
            "tests/test_*auth*.py",
            "tests/test_*permission*.py",
            "tests/test_*routing*.py",
            "tests/test_p13_protected_certification.py",
            "tests/test_*write_sink*.py",
            "tests/test_writesink_*.py",
            "tests/test_unified_space.py",
            "tests/test_space_*.py",
        ),
    ),
)

DEDICATED_SUITES: dict[str, DedicatedSuite] = {
    "playwright": {
        "paths": ("tests/e2e/*.spec.mjs",),
        "command": "cd tests/e2e && npm ci && npx playwright test",
        "category": "e2e",
        "distributions": ("private", "public"),
    },
    "reviewer_tooling": {
        "paths": ("scripts/*.test.js",),
        "command": "for test_file in scripts/*.test.js; do node \"$test_file\"; done",
        "category": "unit",
        "distributions": ("private",),
    },
    "embedded_secret_docker": {
        "paths": (
            "scripts/verify_embedded_secret_docker.sh",
            "scripts/verify_embedded_secret_container.py",
        ),
        "command": "bash scripts/verify_embedded_secret_docker.sh",
        "category": "integration",
        "distributions": ("private", "public"),
    },
    "manual_recipe": {
        "paths": ("scripts/test_recette.py",),
        "command": "uv run python scripts/test_recette.py --list",
        "category": "manual",
        "distributions": ("private", "public"),
    },
}

# ``scripts/test_*.py`` is reserved for an explicit manual suite or a listed
# repository-quality tool. A new unclassified script makes the policy test RED.
TEST_TOOLING_SCRIPTS: frozenset[str] = frozenset(
    {
        "scripts/test_quality_ci.py",
        "scripts/test_suite_baseline.py",
    }
)


class NestedTestRunnerError(RuntimeError):
    """Raised before a complete child suite can start below another runner."""


def classify_path(path: str) -> str:
    """Return the single primary pytest marker for a repository-relative path."""

    normalized = path.replace(os.sep, "/")
    for marker, patterns in CLASSIFICATION_RULES:
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns):
            return marker
    return "unit"


def is_complete_pytest_selection(selectors: Sequence[str]) -> bool:
    """Whether pytest selectors would launch a suite rather than focused files.

    Nested pytest remains available for a focused file or node-id isolation
    proof. No selector, a directory, or an expression-only invocation is a
    suite launch and is refused below an active runner.
    """

    if not selectors:
        return True
    for selector in selectors:
        path = selector.split("::", 1)[0]
        if "::" in selector or path.endswith(".py"):
            continue
        return True
    return False


def assert_nested_pytest_allowed(
    selectors: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail closed when a complete pytest suite is nested under any runner."""

    environment = os.environ if environ is None else environ
    active = environment.get(ACTIVE_RUNNER_ENV, "").strip()
    if active and is_complete_pytest_selection(selectors):
        rendered = " ".join(selectors) if selectors else "<default testpaths>"
        raise NestedTestRunnerError(
            f"refusing nested complete pytest selection {rendered!r}; "
            f"active runner is {active!r}. Select a focused .py file or node id."
        )


def runner_identity(kind: str, *, pid: int | None = None) -> str:
    """Return the process-scoped identity inherited by child processes."""

    clean_kind = kind.strip().lower()
    if not clean_kind or ":" in clean_kind:
        raise ValueError("runner kind must be a non-empty token without ':'")
    return f"{clean_kind}:{os.getpid() if pid is None else pid}"


def manual_suite_paths() -> frozenset[str]:
    """Return the explicit repository-relative manual-suite manifest."""

    return frozenset(
        path
        for suite in DEDICATED_SUITES.values()
        if suite["category"] == "manual"
        for path in suite["paths"]
    )


def dedicated_suites_for_distribution(
    distribution: str,
) -> dict[str, DedicatedSuite]:
    """Return suites shipped by one explicit repository distribution."""

    if distribution not in DISTRIBUTIONS:
        expected = ", ".join(DISTRIBUTIONS)
        raise ValueError(
            f"unknown repository distribution {distribution!r}; expected {expected}"
        )
    return {
        name: suite
        for name, suite in DEDICATED_SUITES.items()
        if distribution in suite["distributions"]
    }


def unclassified_test_scripts(root: Path) -> list[str]:
    """List script-shaped tests missing a manual or tooling classification."""

    known = manual_suite_paths() | TEST_TOOLING_SCRIPTS
    discovered = {
        path.relative_to(root).as_posix()
        for path in (root / "scripts").glob("test_*.py")
    }
    return sorted(discovered - known)
