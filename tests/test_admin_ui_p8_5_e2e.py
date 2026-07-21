# -*- coding: utf-8 -*-
"""P8-5 (#143) Access view — REAL-SHELL Playwright integration proof (P8-7 gate).

The node:vm harness (tests/test_admin_ui_p8_5.py::TestP85AsyncLifecycleRuntime)
proves views-access.js's async LOGIC against a stubbed shell — fast and
mutation-proof. This wrapper runs the complementary Playwright spec
(tests/e2e/admin_access_create.spec.mjs), which drives the REAL admin-app.js
shell in headless chromium to prove the shell-owned invariant a stub cannot:
SINGLE IN-FLIGHT CREATE — the shell disables #modalConfirmBtn before awaiting
onConfirm, so a re-click issues no duplicate admin_create_token — plus end-to-end
one-time-secret delivery. Raised by the Terra PR #167 review ([high]).

DECISION: still pytest-driven via a subprocess node runner, consistent with the
node:vm harness and the two sibling `.mjs` harnesses. But the browser toolchain
is heavy and not part of the Python venv, so this SKIPS cleanly when Playwright +
its chromium build are not installed under tests/e2e (a minimal checkout, or the
Python-only `test` CI job). CI exercises it for real in the dedicated Playwright
container job (.github/workflows/build.yml, job `e2e`). To run locally:

    cd tests/e2e && npm ci && npx playwright install chromium && npx playwright test
"""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
E2E_DIR = ROOT / "tests" / "e2e"
_PLAYWRIGHT_PKG = E2E_DIR / "node_modules" / "@playwright" / "test"

_NPX = shutil.which("npx")

pytestmark = [
    pytest.mark.skipif(_NPX is None, reason="npx/Node.js required for the Playwright integration proof"),
    pytest.mark.skipif(
        not _PLAYWRIGHT_PKG.is_dir(),
        reason=(
            "Playwright not installed under tests/e2e — run "
            "`cd tests/e2e && npm ci && npx playwright install chromium`"
        ),
    ),
]


def test_real_shell_create_flow_single_in_flight_and_secret():
    """Drive the real admin shell: an in-flight create locks the modal, a
    duplicate submit is blocked, and the one-time secret is delivered."""
    completed = subprocess.run(
        [_NPX, "playwright", "test", "--reporter=line"],
        cwd=str(E2E_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
