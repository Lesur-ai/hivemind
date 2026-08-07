# -*- coding: utf-8 -*-
"""
Pytest bootstrap shared by the whole Hivemind suite.

P7-4 (#120) needs to unit-test the vendored embedded Graph Memory auth surface
(`services/graph-memory/src/mcp_memory/...`). That tree is NOT an installed
package — Hivemind's `pyproject` only exposes `src/` (the `live_mem` package) —
so a plain `import mcp_memory...` from `tests/` would not resolve and every P7-4
lock would silently fail to collect.

This conftest puts the vendored GM `src/` on `sys.path` so the **import-light**
GM modules (`mcp_memory.auth.context`, `mcp_memory.auth.s3_token_validator`,
`mcp_memory.core.validators`) are importable from the Hivemind test venv.

IMPORTANT: the Hivemind test venv ships `boto3` but NOT `neo4j` / `qdrant_client`.
Tests must therefore only import the import-light GM modules above (the
`s3_token_validator` lazy-imports boto3/config inside methods). Heavy modules
(`mcp_memory.auth.middleware`, `mcp_memory.auth.token_manager`) pull `neo4j`
and must be asserted via source inspection, never imported here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.test_quality_policy import (
    ACTIVE_RUNNER_ENV,
    ORTHOGONAL_MARKERS,
    PRIMARY_MARKERS,
    NestedTestRunnerError,
    assert_nested_pytest_allowed,
    classify_path,
    runner_identity,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GM_SRC = _REPO_ROOT / "services" / "graph-memory" / "src"

# Prepend so the vendored GM `mcp_memory` package resolves before anything else.
# Guarded so repeated collection does not stack duplicates.
_gm_src_str = str(_GM_SRC)
if _GM_SRC.is_dir() and _gm_src_str not in sys.path:
    sys.path.insert(0, _gm_src_str)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a complete nested pytest run and publish this runner to children."""

    try:
        assert_nested_pytest_allowed(tuple(str(arg) for arg in config.args))
    except NestedTestRunnerError as exc:
        raise pytest.UsageError(str(exc)) from exc
    config._hivemind_previous_test_runner = os.environ.get(ACTIVE_RUNNER_ENV)  # type: ignore[attr-defined]
    os.environ[ACTIVE_RUNNER_ENV] = runner_identity("pytest")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore a caller's runner marker for safe in-process focused pytest use."""

    previous = getattr(config, "_hivemind_previous_test_runner", None)
    if previous is None:
        os.environ.pop(ACTIVE_RUNNER_ENV, None)
    else:
        os.environ[ACTIVE_RUNNER_ENV] = previous


@pytest.hookimpl(trylast=True)
def pytest_itemcollected(item: pytest.Item) -> None:
    """Classify each item before pytest's built-in ``-m`` deselection runs."""

    try:
        relative = Path(str(item.path)).resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        relative = Path(str(item.path)).as_posix()
    marker = classify_path(relative)
    existing = {
        declared.name
        for declared in item.iter_markers()
        if declared.name in PRIMARY_MARKERS
    }
    conflicts = existing - {marker}
    if conflicts:
        raise pytest.UsageError(
            f"{item.nodeid} declares conflicting primary markers "
            f"{sorted(conflicts)!r}; path policy requires {marker!r}"
        )
    if marker not in existing:
        item.add_marker(marker)
    resolved_markers = (marker,) + tuple(
        orthogonal
        for orthogonal in ORTHOGONAL_MARKERS
        if item.get_closest_marker(orthogonal) is not None
    )
    for resolved in resolved_markers:
        evidence = ("hivemind_marker", resolved)
        if evidence not in item.user_properties:
            item.user_properties.append(evidence)


@pytest.fixture(autouse=True)
def _isolate_inference_holders():
    """Keep the two process-wide inference holders from leaking across tests.

    P13-1C binds the inference lifecycle to the process ASGI lifespan, so the
    holders are module singletons carrying a resolved runtime AND a terminal
    shutdown flag. A test that starts a serving window, or one that closes it,
    therefore changes state every later test sees: a raised flag makes the next
    `get()` raise `InferenceRuntimeClosed`, and a runtime left holding an
    unreleased adapter makes the next `validate_startup()` REFUSE to open a
    window over it. Neither failure points at the test that caused it.

    Snapshot/restore rather than reset: restoring the exact prior tuple leaves a
    test that deliberately installed a runtime exactly as it found things.
    """

    stateful = []
    for module_name, attribute in (
        ("live_mem.core.inference_runtime", "_holder"),
        ("mcp_memory.core.inference_runtime", "_holder"),
        # The process window gate is process-global for exactly the same
        # reason and leaks exactly the same way: a test that claims a window
        # and does not release it would refuse every later test's startup.
        ("live_mem.server", "_process_window"),
        ("mcp_memory.server", "_process_window"),
    ):
        try:
            module = __import__(module_name, fromlist=[attribute])
        except Exception:  # noqa: BLE001 - an absent consumer is not a failure
            continue
        target = getattr(module, attribute, None)
        if target is not None:
            stateful.append((target, target.snapshot_for_tests()))

    try:
        yield
    finally:
        for target, snapshot in stateful:
            target.restore_for_tests(snapshot)
