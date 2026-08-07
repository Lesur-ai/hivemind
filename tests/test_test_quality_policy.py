"""RED/GREEN contracts for the TQ-7 taxonomy and nested-runner guard."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

from tests.test_quality_policy import (
    ACTIVE_RUNNER_ENV,
    DEDICATED_SUITES,
    DISTRIBUTIONS,
    ORTHOGONAL_MARKERS,
    PRIMARY_MARKERS,
    NestedTestRunnerError,
    assert_nested_pytest_allowed,
    classify_path,
    dedicated_suites_for_distribution,
    is_complete_pytest_selection,
    manual_suite_paths,
    runner_identity,
    unclassified_test_scripts,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("tests/test_json_repair.py", "unit"),
        ("tests/test_asgi_lifespan_integration.py", "integration"),
        ("tests/test_adr_registry.py", "contract"),
        ("tests/test_p9_7_rehearsal.py", "contract"),
        ("tests/test_p9_5_topology.py", "contract"),
        ("tests/test_p10_tool_exposure.py", "contract"),
        ("tests/test_mesh_router.py", "security_protocol"),
        ("tests/test_p13_protected_certification.py", "security_protocol"),
        ("tests/test_mesh_pairing_e2e.py", "e2e"),
    ),
)
def test_primary_taxonomy_classifies_representative_paths(
    path: str, expected: str
) -> None:
    assert classify_path(path) == expected


@pytest.mark.contract
def test_collection_hook_assigns_exactly_one_primary_marker(
    request: pytest.FixtureRequest,
) -> None:
    present = {
        marker
        for marker in PRIMARY_MARKERS
        if request.node.get_closest_marker(marker) is not None
    }
    assert present == {"contract"}
    assert len(list(request.node.iter_markers(name="contract"))) == 1
    assert ("hivemind_marker", "contract") in request.node.user_properties


def test_pytest_registers_every_policy_marker() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = config["tool"]["pytest"]["ini_options"]["markers"]
    names = {item.split(":", 1)[0] for item in declarations}
    assert names == {*PRIMARY_MARKERS, *ORTHOGONAL_MARKERS}


@pytest.mark.parametrize(
    ("marker", "selected", "counterexample"),
    (
        (
            "unit",
            "tests/test_config_embedded_long.py::test_defaults_valid",
            "tests/test_p9_rules_contract.py::test_templates_use_hivemind_identity_and_bounded_authority",
        ),
        (
            "integration",
            "tests/test_long_runtime_isolation.py::test_consolidation_of_connected_space_makes_zero_graph_contact",
            "tests/test_config_embedded_long.py::test_defaults_valid",
        ),
        (
            "contract",
            "tests/test_p9_rules_contract.py::test_templates_use_hivemind_identity_and_bounded_authority",
            "tests/test_config_embedded_long.py::test_defaults_valid",
        ),
        (
            "security_protocol",
            "tests/test_mesh_secret.py::test_generated_secret_is_high_entropy_and_unique",
            "tests/test_config_embedded_long.py::test_defaults_valid",
        ),
        (
            "e2e",
            "tests/test_mesh_pairing_e2e.py::test_two_tcp_asgi_admins_pair_without_in_process_peer_transport",
            "tests/test_config_embedded_long.py::test_defaults_valid",
        ),
    ),
)
def test_documented_marker_selection_happens_before_core_deselection(
    marker: str, selected: str, counterexample: str
) -> None:
    environment = os.environ.copy()
    environment.pop(ACTIVE_RUNNER_ENV, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            marker,
            selected,
            counterexample,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert selected in completed.stdout
    assert counterexample not in completed.stdout


@pytest.mark.parametrize(
    "selectors",
    ((), ("tests",), ("tests/",), ("tests/unit",)),
)
def test_complete_nested_pytest_selections_fail_closed(
    selectors: tuple[str, ...]
) -> None:
    assert is_complete_pytest_selection(selectors)
    with pytest.raises(NestedTestRunnerError, match="refusing nested complete pytest"):
        assert_nested_pytest_allowed(
            selectors,
            environ={ACTIVE_RUNNER_ENV: "pytest:123"},
        )


@pytest.mark.parametrize(
    "selectors",
    (
        ("tests/test_config.py",),
        ("tests/test_config.py::test_defaults",),
        ("tests/test_config.py", "tests/test_proxy.py::test_proxy"),
    ),
)
def test_focused_nested_pytest_selection_remains_available(
    selectors: tuple[str, ...]
) -> None:
    assert not is_complete_pytest_selection(selectors)
    assert_nested_pytest_allowed(
        selectors,
        environ={ACTIVE_RUNNER_ENV: "pytest:123"},
    )


def test_top_level_pytest_has_no_false_nested_failure() -> None:
    assert_nested_pytest_allowed(("tests",), environ={})
    assert runner_identity("pytest", pid=42) == "pytest:42"
    with pytest.raises(ValueError, match="non-empty token"):
        runner_identity("bad:kind", pid=42)


def test_dedicated_and_manual_suites_are_explicit_and_complete() -> None:
    assert set(DEDICATED_SUITES) == {
        "embedded_secret_docker",
        "manual_recipe",
        "playwright",
        "reviewer_tooling",
    }
    assert DISTRIBUTIONS == ("private", "public")
    assert set(dedicated_suites_for_distribution("public")) == {
        "embedded_secret_docker",
        "manual_recipe",
        "playwright",
    }
    with pytest.raises(ValueError, match="unknown repository distribution"):
        dedicated_suites_for_distribution("unknown")
    assert manual_suite_paths() == frozenset({"scripts/test_recette.py"})
    assert unclassified_test_scripts(ROOT) == []
    for suite in DEDICATED_SUITES.values():
        assert suite["command"]
        assert suite["distributions"]
        assert set(suite["distributions"]) <= set(DISTRIBUTIONS)
    for suite in dedicated_suites_for_distribution("public").values():
        for pattern in suite["paths"]:
            assert list(ROOT.glob(pattern)), f"dedicated suite path missing: {pattern}"


def test_optional_external_proofs_are_explicitly_marked() -> None:
    expected = {
        "tests/test_mesh_caddy_contract.py": "test_real_caddy_adapts_both_feature_states",
        "tests/test_p13_qdrant_vector_store.py": (
            "test_pinned_qdrant_server_contract_when_available"
        ),
    }
    for relative, function_name in expected.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ]
        assert len(functions) == 1
        decorators = {ast.unparse(item) for item in functions[0].decorator_list}
        assert "pytest.mark.optional" in decorators


def test_first_class_non_pytest_runners_refuse_nesting() -> None:
    playwright_config = (ROOT / "tests/e2e/playwright.config.mjs").read_text(
        encoding="utf-8"
    )
    playwright_guard = (ROOT / "tests/e2e/runner-guard.mjs").read_text(
        encoding="utf-8"
    )
    recipe = (ROOT / "scripts/test_recette.py").read_text(encoding="utf-8")
    docker = (ROOT / "scripts/verify_embedded_secret_docker.sh").read_text(
        encoding="utf-8"
    )
    assert "claimPlaywrightRunner" in playwright_config
    for source in (playwright_guard, recipe, docker):
        assert "HIVEMIND_ACTIVE_TEST_RUNNER" in source
        assert "refusing nested" in source


def test_embedded_secret_docker_suite_refuses_before_touching_docker() -> None:
    environment = dict(os.environ)
    environment[ACTIVE_RUNNER_ENV] = "pytest:123"
    result = subprocess.run(
        ["bash", "scripts/verify_embedded_secret_docker.sh"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "refusing nested embedded-secret Docker suite" in result.stderr


def test_playwright_guard_allows_owned_workers_but_rejects_nested_runner() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for repository test harnesses"

    probe = """
import { claimPlaywrightRunner } from './tests/e2e/runner-guard.mjs';
const fresh = {};
if (claimPlaywrightRunner({ environment: fresh, pid: 100, parentPid: 10 }) !== 'playwright:100')
    process.exit(1);
const worker = { HIVEMIND_ACTIVE_TEST_RUNNER: 'playwright:100', TEST_WORKER_INDEX: '0' };
if (claimPlaywrightRunner({ environment: worker, pid: 101, parentPid: 100 }) !== 'playwright:100')
    process.exit(2);
for (const environment of [
    { HIVEMIND_ACTIVE_TEST_RUNNER: 'pytest:50' },
    { HIVEMIND_ACTIVE_TEST_RUNNER: 'playwright:100', TEST_WORKER_INDEX: '0' },
]) {
    let rejected = false;
    try {
        claimPlaywrightRunner({ environment, pid: 102, parentPid: 101 });
    } catch (error) {
        rejected = error.message.includes('refusing nested Playwright suite');
    }
    if (!rejected)
        process.exit(3);
}
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
