"""Runtime and source contracts for admin browser-session ownership."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).parent.parent
API_PATH = ROOT / "src/live_mem/static/js/admin-api.js"
APP_PATH = ROOT / "src/live_mem/static/js/admin-app.js"
RUNTIME_PATH = ROOT / "tests/js/admin_session_generation_runtime.mjs"


def test_session_generation_runtime_races():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the session runtime harness"
    completed = subprocess.run(
        [node, str(RUNTIME_PATH), str(API_PATH), str(APP_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "admin session generation runtime: ok"


def test_call_tool_401_is_bound_to_request_generation():
    source = API_PATH.read_text(encoding="utf-8")
    assert "const requestSessionGeneration = currentSessionGeneration();" in source
    assert "sessionGenerationIsCurrent(requestSessionGeneration)" in source
    assert "throw new Error('Stale session')" in source


def test_shell_exposes_generation_to_views_and_guards_boot():
    source = APP_PATH.read_text(encoding="utf-8")
    assert "sessionGeneration: _sessionGeneration" in source
    assert "const sessionGeneration = currentSessionGeneration();" in source
    assert "if (!sessionGenerationIsCurrent(sessionGeneration)) return;" in source


def test_logout_invalidates_before_awaiting_server():
    source = APP_PATH.read_text(encoding="utf-8")
    start = source.index("async function doLogout()")
    end = source.index("// ═══════════════ SIDEBAR", start)
    body = source[start:end]
    assert body.index("showLogin();") < body.index("await adminLogout();")


def test_disabled_login_button_is_enforced_inside_handler():
    source = APP_PATH.read_text(encoding="utf-8")
    start = source.index("async function doLogin()")
    end = source.index("async function doLogout()", start)
    body = source[start:end]
    assert "if (btn.disabled) return;" in body
