# -*- coding: utf-8 -*-
"""P10-4 (#192) Mesh view — source contract pins and runtime regression proof."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
VIEW_PATH = ROOT / "src/live_mem/static/js/admin/views-mesh.js"
APP_PATH = ROOT / "src/live_mem/static/js/admin-app.js"
API_PATH = ROOT / "src/live_mem/static/js/admin-api.js"
HTML_PATH = ROOT / "src/live_mem/static/admin.html"
RUNTIME_PATH = ROOT / "tests/js/admin_mesh_runtime.mjs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_view_registers_both_mesh_routes() -> None:
    source = _read(VIEW_PATH)
    assert "AdminViews.register('mesh', render)" in source
    assert "AdminViews.register('mesh-detail', render)" in source


def test_view_validates_space_id_before_dispatching_detail() -> None:
    source = _read(VIEW_PATH)
    detail = source[source.index("function renderDetail("):source.index("function detailOverviewPanel")]
    assert "SPACE_ID_RE.test(rawSpaceId)" in detail
    assert detail.index("SPACE_ID_RE.test(rawSpaceId)") < detail.index("loadStatus(epoch).then(")


def test_view_never_calls_mesh_admin_tools_via_calltool_funnel() -> None:
    # mesh_admin.py's own docstring: "never an MCP mesh_* tool" — the console
    # must consume /api/admin/mesh/* directly, not the /api/tool funnel.
    source = _read(VIEW_PATH)
    assert "callTool('space_list'" in source  # the one legitimate /api/tool call (space picker)
    assert "callTool('mesh" not in source


def test_view_gates_admin_before_any_network_call() -> None:
    source = _read(VIEW_PATH)
    overview = source[source.index("function renderOverviewShell"):source.index("function overviewActions")]
    assert "isAdmin(state.identity)" in overview
    assert overview.index("isAdmin(state.identity)") < overview.index("loadStatus(epoch)")
    detail = source[source.index("function renderDetail("):source.index("function detailOverviewPanel")]
    assert "isAdmin(state.identity)" in detail
    assert detail.index("isAdmin(state.identity)") < detail.index("loadStatus(epoch)")


def test_router_wires_mesh_routes_and_nav_gating() -> None:
    source = _read(APP_PATH)
    assert "raw === '/mesh'" in source
    assert "_matchMeshDetail(segments[1], raw)" in source
    assert "function _refreshMeshNav(" in source
    probe = source[source.index("async function _refreshMeshNav("):]
    assert "meshAdminAvailability()" in probe
    assert "meshAdminStatus()" not in probe.split("\n}", 1)[0]
    # Nav must be probed only when the session already resolves admin, and the
    # sidebar item must be ABSENT (never a disabled control) otherwise.
    assert "isAdmin" in source[source.index("function _refreshMeshNav("):]


def test_admin_api_exposes_a_direct_rest_client_not_calltool() -> None:
    source = _read(API_PATH)
    assert "async function meshAdminAvailability()" in source
    assert "_meshFetch('availability')" in source
    assert "async function meshAdminStatus()" in source
    assert "async function meshAdminMembers(" in source
    assert "async function meshAdminAction(" in source
    assert "/api/admin/mesh/" in source
    assert "confirm: true" in source


def test_admin_html_loads_the_mesh_view_script() -> None:
    assert '<script src="/static/js/admin/views-mesh.js"></script>' in _read(HTML_PATH)


def test_mesh_runtime_scenarios_pass() -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract pins remain")
    completed = subprocess.run(
        [node, str(RUNTIME_PATH), str(VIEW_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin mesh runtime: ok" in completed.stdout


def test_mesh_runtime_kills_readiness_and_stale_guard_mutants(tmp_path: Path) -> None:
    """The runtime must fail if any client-side fail-closed guard is weakened."""

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract pins remain")
    source = _read(VIEW_PATH)

    token_guard = "\n            && SOURCE_STATE_TOKEN_RE.test(entry.state_token)"
    coherence_guard = "\n            && sourceReadinessIsCoherent(entry)"
    string_projection = """            if (typeof item !== 'string' || !SPACE_ID_RE.test(item)) return;
            const spaceId = item;"""
    object_projection = """            const spaceId = typeof item === 'string'
                ? item
                : (item && typeof item.space_id === 'string' ? item.space_id : '');
            if (!SPACE_ID_RE.test(spaceId)) return;"""
    close_guard = "if (_modalGen === generation) _modalGen += 1;"
    prepare_guard = "if (prepareContextIsStale(ctx)) return false;"

    mutants = {
        "token-format": source.replace(token_guard, "", 1),
        "semantic-coherence": source.replace(coherence_guard, "", 1),
        "string-only-eligible-ids": source.replace(string_projection, object_projection, 1),
        "explicit-close-generation": source.replace(close_guard, "void generation;", 1),
    }
    last_prepare_guard = source.rfind(prepare_guard)
    assert last_prepare_guard >= 0
    mutants["post-await-prepare-guard"] = (
        source[:last_prepare_guard]
        + "/* mutant removed post-await preparation guard */"
        + source[last_prepare_guard + len(prepare_guard):]
    )

    for name, mutant in mutants.items():
        assert mutant != source, f"{name} mutation did not apply"
        mutant_path = tmp_path / f"views-mesh-{name}.js"
        mutant_path.write_text(mutant, encoding="utf-8")
        completed = subprocess.run(
            [node, str(RUNTIME_PATH), str(mutant_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0, f"runtime survived {name} mutant"
