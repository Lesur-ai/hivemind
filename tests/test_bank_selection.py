"""Runtime and source contracts for the /live selectBank() stale-response guard (P12-2, #254)."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "src/live_mem/static/js/config.js"
API_PATH = ROOT / "src/live_mem/static/js/api.js"
BANK_PATH = ROOT / "src/live_mem/static/js/bank.js"
RUNTIME_PATH = ROOT / "tests/js/bank_selection_runtime.mjs"


def test_bank_selection_runtime_races():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the bank selection runtime harness"
    completed = subprocess.run(
        [node, str(RUNTIME_PATH), str(CONFIG_PATH), str(API_PATH), str(BANK_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "bank selection runtime: ok"


def test_select_bank_captures_identity_and_generation_before_the_await():
    source = BANK_PATH.read_text(encoding="utf-8")
    start = source.index("async function selectBank(filename)")
    body = source[start:]
    await_index = body.index("await apiLoadBankFile(")
    assert body.index("const requestedSpaceId = app.spaceId;") < await_index
    assert body.index("const requestGeneration = ++app._bankRequestGeneration;") < await_index
    assert "await apiLoadBankFile(requestedSpaceId, filename)" in body


def test_select_bank_checks_all_three_identities_before_every_post_await_mutation():
    source = BANK_PATH.read_text(encoding="utf-8")
    start = source.index("async function selectBank(filename)")
    end = source.index("\n}", start)
    body = source[start:end]

    # Three independent comparisons feed the same isStale() predicate: space,
    # filename, and request generation. Each is individually mutation-proven
    # (tests/js/bank_selection_runtime.mjs) — space alone (cross-space, same
    # filename), filename alone (cross-file, same space), and generation
    # alone (ABA: same space AND filename re-selected, e.g. alpha -> beta ->
    # alpha, Terra PR #257 review finding).
    is_stale_index = body.index("const isStale = () =>")
    predicate = body[is_stale_index:body.index(";", is_stale_index)]
    assert "app.spaceId !== requestedSpaceId" in predicate
    assert "app.currentBankFile !== filename" in predicate
    assert "app._bankRequestGeneration !== requestGeneration" in predicate

    # The success continuation must check staleness before either branch that
    # mutates #bankContent (the ok-render branch and the error-render branch).
    await_index = body.index("await apiLoadBankFile(")
    is_stale_success = body.index("if (isStale()) return;", await_index)
    ok_render = body.index("md-content", await_index)
    error_render = body.index("empty-state", is_stale_success)
    assert is_stale_success < ok_render
    assert is_stale_success < error_render

    # The catch continuation must also check staleness before it mutates
    # #bankContent, and must keep Unauthorized centrally handled either way.
    catch_index = body.index("catch (e)")
    unauthorized_check = body.index("e.message !== 'Unauthorized'", catch_index)
    is_stale_catch = body.index("if (isStale()) return;", unauthorized_check)
    catch_render = body.index("empty-state", is_stale_catch)
    assert unauthorized_check < is_stale_catch < catch_render
