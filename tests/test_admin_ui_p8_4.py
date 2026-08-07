# -*- coding: utf-8 -*-
"""
P8-4 (#142) source-contract pins — Consolidation view + Operator tools.

Same technique as the ADM-01 pins in test_admin_console_security.py: read the
shipped frontend source and assert the contract-critical invariants that a
browser test could regress silently. These are NOT happy-path checks; each one
targets a specific data-honesty / security / feature-parity requirement of the
frozen design contract (DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §4.6, §4.8,
§5.5, §5.8, §7.4, §8.2). Emoji-guard and forbidden-sink fences over these two
files live in test_admin_console_security.py (whole-wave scope).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "src" / "live_mem" / "static"
_CONSOL = _STATIC / "js" / "admin" / "views-consolidation.js"
_OPER = _STATIC / "js" / "admin" / "views-operator.js"
_CSS = _STATIC / "css" / "admin.css"
_GC_RUNTIME = Path(__file__).resolve().parent / "js" / "admin_gc_runtime.mjs"

# §8.2 forbidden non-claims vocabulary (the ADR-0018 8-token guardrail),
# extended by this contract to every UI string.
_FORBIDDEN_TOKENS = (
    "quorum",
    "hub topology",
    "permanent master",
    "leader runtime",
    "crdt",
    "multi-space merge",
    "parallel consolidation",
    "multi-tenant",
)


@pytest.fixture(scope="module")
def consol() -> str:
    return _CONSOL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def oper() -> str:
    return _OPER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _extract_fn(src: str, signature: str) -> str:
    """Return the source of a JS function by brace-matching from its signature."""
    i = src.index(signature)
    b = src.index("{", i)
    depth = 0
    for j in range(b, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    return src[i:]


def _css_banner(css: str, start_marker: str, end_marker: str | None) -> str:
    i = css.index(start_marker)
    j = css.index(end_marker, i) if end_marker else len(css)
    return css[i:j]


# ─────────────────────────── registration & tool binding ───────────────────────────


class TestRegistration:
    def test_consolidation_registers(self, consol):
        assert "AdminViews.register('consolidation'" in consol

    def test_operator_registers(self, oper):
        assert "AdminViews.register('operator'" in oper


def test_gc_runtime_contract():
    """Exercise the shipped view with deferred, reordered tool promises."""
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "Node.js is required for the admin GC runtime harness"
    completed = subprocess.run(
        [node, str(_GC_RUNTIME), str(_OPER)],
        cwd=_STATIC.parent.parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "admin GC runtime: ok"


class TestRealToolBinding:
    """Data honesty (§5): every widget consumes a real tool — no invented data."""

    def test_consolidation_tools(self, consol):
        for tool in (
            "bank_consolidation_queues",
            "bank_consolidation_status",
            "bank_consolidate",
            "bank_stale_spaces",
        ):
            assert f"'{tool}'" in consol, f"consolidation view missing {tool}"

    def test_operator_tools(self, oper):
        for tool in (
            "backup_list",
            "backup_create",
            "backup_restore",
            "backup_delete",
            "bank_compact",
            "bank_repair",
            "admin_gc_notes",
            "space_list",
        ):
            assert f"'{tool}'" in oper, f"operator view missing {tool}"

    def test_no_n_plus_one_or_wrong_source(self, consol, oper):
        # §5.0: list/lane views never fan out to space_info / graph_status.
        for src, name in ((consol, "consolidation"), (oper, "operator")):
            assert "space_info" not in src, f"{name} calls space_info (N+1 hazard)"
            assert "graph_status" not in src, f"{name} calls graph_status"

    def test_no_polling(self, consol, oper):
        # D8: no automatic polling / timer-based coordination anywhere.
        for src, name in ((consol, "consolidation"), (oper, "operator")):
            assert "setInterval" not in src, f"{name} uses setInterval (polling banned)"
            assert "setTimeout" not in src, f"{name} uses setTimeout (D8 / §3.3.2 r5)"


# ─────────────────────────── scope-widening guard (§4.5 E4) ───────────────────────────


class TestConsolidateScope:
    def test_mine_sends_agent(self, consol):
        # scope 'mine' MUST always send a non-empty agent.
        assert "args.agent = String(agent)" in consol

    def test_mine_hard_refuses_empty_client_name(self, consol):
        # A missing client_name must HARD-REFUSE instead of silently changing
        # the caller-scoped request.
        enqueue = _extract_fn(consol, "async function enqueue(")
        assert "if (!agent)" in enqueue, "enqueue has no empty-agent guard"
        # the guard must return before args.agent is set / the tool is called
        after_guard = enqueue.split("if (!agent)", 1)[1]
        assert "return" in after_guard[:220], "empty-agent guard does not return early"
        assert after_guard.index("return") < after_guard.index("args.agent"), (
            "guard return comes after args.agent assignment — widening still possible"
        )
        assert "Cannot determine your agent identity" in consol

    def test_all_scope_is_manage_gated(self, consol):
        assert "hasManage()" in consol
        # the consol-all action is gated behind a manage check
        assert re.search(r"consol-all'.*hasManage\(\)", consol, re.DOTALL)
        enqueue = _extract_fn(consol, "async function enqueue(")
        assert "else if (scope === 'all')" in enqueue
        assert "args.agent = '';" in enqueue

    def test_stale_paths_make_privileged_global_scope_explicit(self, consol):
        for fn in ("function confirmStaleRow(", "async function submitAllStale("):
            body = _extract_fn(consol, fn)
            assert "bank_consolidate" in body
            assert "if (hasManage()) args.agent = '';" in body

    def test_bulk_stale_confirmation_names_the_exact_scope(self, consol):
        body = _extract_fn(consol, "async function startConsolidateAllStale(")
        assert "All agents' live notes in every listed space" in body
        assert "Only your own live notes in every listed space" in body

    def test_result_modals_are_not_auto_closed(self, consol):
        # A confirm-modal onConfirm that REPLACES the shared modal with a result
        # or error must return false — returning true lets the shell's confirm
        # wrapper closeModal() the just-shown modal (regression guard for the
        # pre-commit MAJOR findings).
        assert "await submitAllStale(captured, epoch, confirmOp); return false;" in consol
        hr = _extract_fn(consol, "function handleEnqueueResult(")
        assert "return false" in hr, "handleEnqueueResult must keep the refusal modal open"

    def test_stale_epoch_guards_return_false_not_true(self, consol):
        # Codex MEDIUM (round 2): a stale continuation must NOT return true, or the
        # shell's confirm wrapper closeModal()s a newer route's modal.
        assert "AdminRouter.epoch !== epoch) return true" not in consol

    def test_all_stale_scans_before_confirmation(self, consol):
        # Codex MEDIUM (round 2) / §4.8 K5: re-scan first, confirm the exact
        # captured set, then submit only that set (never re-scan after confirm).
        body = _extract_fn(consol, "async function startConsolidateAllStale(")
        assert "bank_stale_spaces" in body, "all-stale does not re-scan before confirming"
        assert "captured" in body
        submit = _extract_fn(consol, "async function submitAllStale(")
        assert "bank_stale_spaces" not in submit, "submission must not re-scan (uses captured set)"

    def test_fifo_queue_jobs_rendered_as_inspectable_chips(self, consol):
        # MEDIUM fix: the FIFO queue payload (queued_jobs[]) renders as position +
        # scope chips that open the job inspector — not just a bare queued_count.
        body = _extract_fn(consol, "function queuedCell(")
        assert "queued_jobs" in body
        assert "consol-job" in body
        assert "queue_position" in body
        assert "scope_label" in body

    def test_job_inspector_renders_full_payload(self, consol):
        # Codex MEDIUM (round 2) / §5.5: the inspector surfaces provenance,
        # guarantee, and lifecycle timestamps, not just space/scope/progress.
        body = _extract_fn(consol, "function renderJob(")
        for field in ("requested_by", "guarantee", "requested_at", "started_at", "finished_at"):
            assert field in body, f"job inspector omits {field}"

    def test_all_notes_control_visible_but_disabled_without_manage(self, consol):
        # LOW fix (§4.5 E3): "Consolidate all notes" stays visible, disabled with
        # a manage/admin hint for non-managers, rather than being hidden.
        body = _extract_fn(consol, "function laneActions(")
        assert "disabled" in body
        assert "Requires manage or admin permission" in body


# ─────────────────────────── typed-confirmation & destructive UX (§7.4) ───────────────────────────


class TestTypedConfirmation:
    def test_uses_destructive_modal_for_backup_restore_and_delete(self, oper):
        # Backup restore/delete and GC delete all use the typed-confirmation
        # modal; GC consolidate deliberately uses the neutral modal.
        assert oper.count("showDestructiveModal(") >= 3
        assert "showModal('Consolidate orphan notes'" in oper

    def test_backup_id_challenge(self, oper):
        assert "typedConfirmation: backupId" in oper

    def test_gc_exact_count_challenge_and_warning(self, oper):
        body = _extract_fn(oper, "function confirmGcDelete(")
        assert "`delete ${captured.count} notes`" in body
        assert (
            "`Deletes ${captured.count} orphan notes WITHOUT consolidating them. "
            "Their content is lost.`"
        ) in body
        assert "typedConfirmation: challenge" in body

    def test_empty_target_guard(self, oper):
        # Never open a typed-confirm with an empty challenge (§7.4.1 nit).
        assert "if (!backupId)" in oper


# ─────────────────────────── restore fail-closed honesty (§5(a)/D7) ───────────────────────────


class TestRestoreFailClosed:
    def test_never_sends_unsafe_recovery(self, oper):
        assert "unsafe_recovery" not in oper

    def test_uniform_error_no_message_parsing(self, oper):
        # §5(a)/D7: French server messages are never parsed / pattern-matched to
        # branch. Restore errors get ONE uniform treatment.
        for bad in (".message.includes(", ".message.indexOf(", ".message.match("):
            assert bad not in oper, f"operator view parses a server message: {bad}"
        # no hardcoded French-message matching of the target-exists refusal
        assert "Supprimez" not in oper
        assert "restoreErrorHtml" in oper
        assert "unsafe-recovery path is an MCP-client operation" in oper

    def test_restore_discloses_data_only_and_bootstrap_regrant_boundary(self, oper):
        restore = _extract_fn(oper, "function confirmRestore(")
        assert restore.count("restore never restores token allowlists") == 1
        assert restore.count("Access was not restored.") == 1
        assert restore.count("space_invite_token") == 2
        assert restore.count("admin_update_token") == 2
        assert restore.count("admin_bulk_update_tokens") == 2
        assert restore.count("Never delete and recreate") == 2


# ─────────────────────────── token purge lives in Access only (§4.8 M6) ───────────────────────────


class TestPurgeCrossLink:
    def test_no_purge_control(self, oper):
        assert "admin_purge_tokens" not in oper, "operator view renders the purge control"

    def test_links_to_access(self, oper):
        assert "#/access" in oper


# ─────────────────────────── GC space-scoped only (§4.8 M5) ───────────────────────────


class TestGcConstraints:
    def test_gc_never_global(self, oper):
        # Every GC call uses a positively captured target — never an empty/global
        # literal. The delete call uses captured.spaceId from the reviewed proof.
        calls = []
        for m in re.finditer(r"callTool\('admin_gc_notes'", oper):
            calls.append(oper[m.end() : m.end() + 260])
        assert len(calls) == 3
        for window in calls:
            assert "space_id: ''" not in window
            assert "space_id: sid" in window or "space_id: captured.spaceId" in window

    def test_only_backup_all_uses_empty_space(self, oper):
        # The single legitimate empty space_id is the admin all-spaces backup.
        assert oper.count("space_id: ''") == 1
        assert "backup_create" in oper

    def test_gc_requires_space_selection(self, oper):
        assert "Select a space first" in oper

    def test_gc_is_admin_gated(self, oper):
        assert "isAdmin()" in oper
        gc_panel = _extract_fn(oper, "function gcPanel(")
        assert "isAdmin()" in gc_panel

    def test_gc_exposes_all_three_explicit_modes(self, oper):
        for action in ("op-gc-dry", "op-gc-consolidate", "op-gc-delete"):
            assert action in oper
        assert 'class="form-hint op-gc-delete-hint"' in oper
        dry = _extract_fn(oper, "function runGcDry(")
        consolidate = _extract_fn(oper, "function confirmGcConsolidate(")
        delete = _extract_fn(oper, "function confirmGcDelete(")
        assert "confirm: false" in dry
        assert "confirm: true" in consolidate and "delete_only: false" in consolidate
        assert "confirm: true" in delete and "delete_only: true" in delete
        assert "expected_eligible_set_token: captured.token" in delete
        assert "unavailable pending" not in oper.lower()

    def test_gc_proof_cache_has_exact_identity_fields(self, oper):
        dry = _extract_fn(oper, "function runGcDry(")
        cache = re.search(r"state\.gcDry\s*=\s*Object\.freeze\(\{(.*?)\}\);", dry, re.DOTALL)
        assert cache, "successful dry run does not freeze an exact delete proof"
        fields = set(
            re.findall(
                r"^\s*([A-Za-z][A-Za-z0-9]*)\s*(?:,|:)",
                cache.group(1),
                re.MULTILINE,
            )
        )
        assert fields == {"spaceId", "maxAgeDays", "count", "token", "sessionGeneration"}
        assert "eligible_set_token" in dry
        assert "typeof token === 'string'" in dry

    def test_gc_proof_is_invalidated_at_every_authority_boundary(self, oper):
        render = _extract_fn(oper, "function render(")
        dry = _extract_fn(oper, "function runGcDry(")
        begin = _extract_fn(oper, "function beginGcMutation(")
        picker = _extract_fn(oper, "function paintMaintPicker(")
        panels = _extract_fn(oper, "function paintMaintPanels(")
        assert "state.gcDry = null" in render  # session-generation transition
        assert dry.index("invalidateGcProof()") < dry.index("if (!sid)")
        assert "invalidateGcProof()" in begin  # mutation start
        assert "invalidateGcProof()" in picker  # target change
        assert "invalidateGcProof()" in panels  # max-age input
        assert "invalidateGcProof()" in _extract_fn(oper, "function paintGcDry(")

    def test_gc_proof_is_bound_to_session_target_and_age(self, oper):
        proof = _extract_fn(oper, "function gcProofForCurrentTarget(")
        assert "proof.spaceId !== sid" in proof
        assert "proof.maxAgeDays !== maxAgeDays" in proof
        assert "proof.sessionGeneration !== state.sessionGeneration" in proof
        assert "currentSessionGeneration()" in proof
        assert "sessionGenerationIsCurrent(proof.sessionGeneration)" in proof
        continuation = _extract_fn(oper, "function gcContinuationCurrent(")
        assert "state.sessionGeneration === captured.sessionGeneration" in continuation
        assert "currentSessionGeneration() === captured.sessionGeneration" in continuation
        assert "sessionGenerationIsCurrent(captured.sessionGeneration)" in continuation
        assert "maintSpace() === captured.spaceId" in continuation
        assert "gcMaxAge() === captured.maxAgeDays" in continuation

    def test_gc_threshold_is_exact_nonnegative_integer_or_refused(self, oper):
        parser = _extract_fn(oper, "function gcMaxAge(")
        assert r"/^\d+$/.test(raw)" in parser
        assert "Number.isSafeInteger(value)" in parser
        assert "parseInt" not in parser
        for fn in (
            "function runGcDry(",
            "function confirmGcConsolidate(",
            "function confirmGcDelete(",
        ):
            assert "gcMaxAge()" in _extract_fn(oper, fn)

    def test_session_change_resets_view_cache_and_all_async_lanes(self, oper):
        render = _extract_fn(oper, "function render(")
        for field in ("state.spaces = null", "state.compactDry = null", "state.repairDry = null", "state.gcDry = null", "state.gcMutation = null"):
            assert field in render
        assert "Object.keys(_maintReq).forEach" in render
        assert "_modalOp += 1" in render

    def test_gc_messages_are_never_parsed_and_always_use_server_slot(self, oper):
        for bad in (".message.includes(", ".message.indexOf(", ".message.match("):
            assert bad not in oper
        for fn in (
            "function gcFailureBlock(",
            "function paintGcDry(",
            "function gcConsolidationDetails(",
            "function gcMutationResult(",
        ):
            assert "serverMessage(" in _extract_fn(oper, fn)

    def test_gc_typed_partial_and_conflict_are_not_success(self, oper):
        result = _extract_fn(oper, "function gcMutationResult(")
        assert "status === 'partial' && reason === 'partial_delete'" in result
        assert "status === 'partial' && reason === 'partial_consolidation'" in result
        assert "status === 'conflict'" in result
        assert "No automatic retry was attempted" in result
        assert "status === 'deleted'" in result
        assert "failure_reason" in result


# ─────────────────────────── dry-run defaults (§4.8 M3/M4) ───────────────────────────


class TestDryRunDefaults:
    def test_compact_repair_two_step(self, oper):
        for action in (
            "op-compact-dry",
            "op-compact-apply",
            "op-repair-dry",
            "op-repair-apply",
        ):
            assert action in oper, f"missing maintenance action {action}"

    def test_dry_is_the_default_action(self, oper):
        # runCompact(true)/runRepair(true) are the dry paths; apply (false) is a
        # separate, explicit action — never a default write.
        assert "runCompact(true)" in oper
        assert "runRepair(true)" in oper
        # the apply (write) path is reached only through a confirm action
        assert "confirmCompactApply" in oper
        assert "confirmRepairApply" in oper

    def test_apply_bound_to_a_prior_dry_run_target(self, oper):
        # Codex MEDIUM (round 2) / §5.8.2: Apply requires a successful dry run for
        # the current space and targets that CAPTURED space, not the mutable picker.
        for fn, flag in (("function confirmCompactApply(", "state.compactDry"),
                         ("function confirmRepairApply(", "state.repairDry")):
            body = _extract_fn(oper, fn)
            assert flag in body, f"{fn} does not gate on a prior dry run"
            assert "captured" in body, f"{fn} does not bind Apply to the captured target"

    def test_no_pre_checked_confirm_control(self, oper):
        # §7.4.1 / M5: confirm is never defaulted by a pre-checked control. The
        # design uses explicit action buttons, not checkboxes — so no checkbox
        # input exists to be pre-checked (precise, not a bare 'checked' substring).
        assert 'type="checkbox"' not in oper
        assert "confirm: false" in oper  # GC dry-run path
        assert "confirm: true" in oper   # backup restore / delete typed confirm


# ─────────────────────────── escaping & static modal titles (§7.3.3) ───────────────────────────


class TestEscapingDiscipline:
    def test_dataset_derived_job_id_is_escaped(self, consol):
        # data-job-id carries a dataset value reused in HTML -> must be esc()'d.
        assert 'data-job-id="${esc(jid)}"' in consol

    def test_job_modal_title_is_static_not_raw_dataset(self, consol):
        # R3 class: the modal title must be a constant, never a raw dataset value.
        assert "showModal('Consolidation job'" in consol

    def test_space_anchors_encode_and_escape(self, consol, oper):
        for src in (consol, oper):
            assert "encodeURIComponent(sid)" in src or "encodeURIComponent(spaceId)" in src


# ─────────────────────────── round-3 review fixes ───────────────────────────


class TestRoundThreeFixes:
    def test_all_stale_summary_renders_server_messages(self, consol):
        # §5.0/§5(a): the batch summary shows each row's verbatim server message,
        # not a bare status.
        body = _extract_fn(consol, "function showAllStaleSummary(")
        assert "serverMessage(r.message)" in body

    def test_gc_dry_run_renders_message_for_every_shape(self, oper):
        # The dry-run message is rendered always, not only on the empty branch.
        body = _extract_fn(oper, "function paintGcDry(")
        assert "serverMessage(data.message)" in body
        assert "summary + msg + detail" in body

    def test_backup_not_found_is_neutral_not_error(self, oper):
        # §5(a)/§5.8.1: typed not_found is a neutral state, never red "failed".
        r = _extract_fn(oper, "function confirmRestore(")
        assert "status === 'not_found'" in r
        d = _extract_fn(oper, "function confirmDeleteBackup(")
        assert "status === 'not_found'" in d

    def test_absent_metrics_use_dash_not_fabricated_zero(self, oper):
        # numOr renders a real number (incl. 0) but "—" for an ABSENT field.
        assert "function numOr(" in oper
        for metric in ("numOr(data.files_total)", "numOr(data.spaces_backed_up)", "numOr(data.files_scanned)"):
            assert metric in oper, f"missing {metric} — a required metric may fabricate 0"

    def test_same_route_modal_instance_token_present(self, consol, oper):
        # Codex MEDIUM: a slow/out-of-order continuation must not close/overwrite
        # a newer same-route modal — guarded by a modal-instance token.
        for src in (consol, oper):
            assert "beginModalOp()" in src
            assert "modalOpCurrent(op)" in src

    def test_maintenance_dry_runs_have_target_guard(self, oper):
        # Codex MEDIUM (round 3): a dry run resolving after the picker moved must
        # not repaint under the new target — compact, repair, AND GC.
        for fn in ("function runCompact(", "function runRepair("):
            body = _extract_fn(oper, fn)
            assert "maintSpace() !== sid" in body, f"{fn} lacks the dry-run target guard"
        gc_guard = _extract_fn(oper, "function gcContinuationCurrent(")
        assert "maintSpace() === captured.spaceId" in gc_guard

    def test_session_ownership_guards_and_batch_abort(self, consol, oper):
        # Codex HIGH (round 6): logout/401 wipes the shell but doesn't bump the
        # epoch, so continuations must verify the session is still active, and the
        # sequential batch must ABORT on session loss (no cross-session mutation).
        for src in (consol, oper):
            assert "function sessionActive(" in src
            assert "sessionActive()" in src
        assert "sessionActive()" in _extract_fn(consol, "async function submitAllStale(")

    def test_all_stale_rescan_branches_on_status(self, consol):
        # Codex MEDIUM (round 6): an error/sentinel re-scan is never a false
        # "No stale banks" — only status:ok + empty spaces[] is empty.
        body = _extract_fn(consol, "async function startConsolidateAllStale(")
        assert "rate_limited" in body and "truncated" in body
        assert "scan.status !== 'ok'" in body

    def test_apply_uses_separate_generation_lane(self, oper):
        # Codex MEDIUM (round 6): a manual dry run must not invalidate a pending
        # Apply mutation — Apply has its own request-generation lane.
        assert "compactApply" in oper and "repairApply" in oper
        assert "gcConsolidate" in oper and "gcDelete" in oper

    def test_maintenance_ops_have_per_request_generation(self, oper):
        # Codex MEDIUM (round 5): same-space, different-input reordering (e.g. GC
        # max-age 7 then 0) needs a per-op request token, not just a target guard.
        assert "_maintReq" in oper
        # a lane per op (reads + separate apply lanes); compact/repair use the
        # per-lane bracket access, GC uses its own.
        for lane in (
            "compact:",
            "compactApply:",
            "repair:",
            "repairApply:",
            "gc:",
            "gcConsolidate:",
            "gcDelete:",
        ):
            assert lane in oper, f"missing _maintReq lane {lane}"
        assert "++_maintReq[lane]" in oper
        assert "++_maintReq.gc" in oper

    def test_compact_repair_apply_re_dry_runs_captured_target(self, oper):
        # §5.8.2 (line ~1425): apply → after-action re-dry-run for the captured
        # target, and the result stays bound to the visible target.
        assert "runCompact(true, sid)" in _extract_fn(oper, "function runCompact(")
        assert "runRepair(true, sid)" in _extract_fn(oper, "function runRepair(")

    def test_stale_scan_drops_out_of_order_responses(self, consol):
        # Codex MEDIUM (round 3): two scans on the same route must not let the
        # earlier response overwrite the newer result / state.staleData.
        assert "_staleGen" in consol
        body = _extract_fn(consol, "async function scanStale(")
        assert "++_staleGen" in body and "_staleGen" in body

    def test_all_stale_and_create_backup_have_modal_token(self, consol, oper):
        # Codex MEDIUM (round 3): the all-stale flow and create/all-spaces backup
        # also carry the modal-instance token (not just restore/delete).
        assert "beginModalOp()" in _extract_fn(consol, "async function startConsolidateAllStale(")
        assert "beginModalOp()" in _extract_fn(oper, "function openCreateBackup(")
        assert "beginModalOp()" in _extract_fn(oper, "function backupAll(")


class TestRoundSevenFixes:
    def test_all_spaces_backup_forwards_optional_description(self, oper):
        # Codex MEDIUM (round 7 / §4.6 B3): the fleet-backup modal must expose the
        # optional description and forward it only when non-empty.
        body = _extract_fn(oper, "function backupAll(")
        assert "opBackupAllDesc" in body
        assert "args.description = desc" in body
        # never send an empty description key
        assert "if (desc)" in body

    def test_stale_mode_scans_on_activation(self, consol):
        # Codex MEDIUM (round 7 / §5.5): entering stale mode runs an initial scan
        # and never flashes a prior activation's cached rows.
        body = _extract_fn(consol, "registerAction('consol-stale-toggle'")
        assert "scanStale()" in body
        assert "state.staleData = null" in body

    def test_dry_run_revokes_prior_apply_authorization(self, oper):
        # Codex MEDIUM (round 7): a NEW dry run must clear the prior Apply
        # authorization immediately (re-granted only on this dry run's success),
        # so a stale/failed newer preview can never leave Apply enabled.
        c = _extract_fn(oper, "function runCompact(")
        assert "if (dryRun) state.compactDry = null" in c
        r = _extract_fn(oper, "function runRepair(")
        assert "if (dryRun) state.repairDry = null" in r

    def test_stale_cache_marker_is_unique_hash_and_fails_closed(self, consol):
        # Codex HIGH (round 7 + re-review, confidentiality): the owner marker must
        # be the UNIQUE token_hash only — never the non-unique client_name — and
        # must FAIL CLOSED (drop the cache) whenever the hash is absent, so two
        # same-named sessions can never be equated and repaint prior stale rows.
        body = _extract_fn(consol, "function render(")
        assert "identity.token_hash" in body
        assert "state.owner" in body
        # no non-unique fallback in the owner computation (comments may mention
        # client_name to explain WHY it is excluded; the code must not read it)
        assert "identity.client_name" not in body, (
            "owner marker must not fall back to non-unique client_name"
        )
        # absent hash => null => unconditional clear (fail closed)
        assert "owner === null" in body
        assert "state.staleData = null" in body
        assert "state.staleMode = false" in body

    def test_duplicate_enqueue_not_fabricated(self, consol):
        # Codex LOW (round 7, ratified 2026-07-13): the coalesced-duplicate payload
        # is byte-identical to a fresh enqueue, so the console renders its TRUE
        # running/queued state and must NOT fabricate an "already queued" label.
        assert "already queued" not in consol.lower()


# ─────────────────────────── forbidden vocabulary (§8.2) ───────────────────────────


class TestForbiddenVocabulary:
    def test_js_files_clean(self, consol, oper):
        for src, name in ((consol.lower(), "consolidation"), (oper.lower(), "operator")):
            for tok in _FORBIDDEN_TOKENS:
                assert tok not in src, f"{name} view contains forbidden token {tok!r}"

    def test_css_banners_clean(self, css):
        banners = _css_banner(
            css, "/* ===== view:consolidation (P8-4) ===== */", "/* ===== view:audit (P8-6) ===== */"
        ) + _css_banner(css, "/* ===== view:operator (P8-4) ===== */", None)
        low = banners.lower()
        for tok in _FORBIDDEN_TOKENS:
            assert tok not in low, f"CSS P8-4 banner contains forbidden token {tok!r}"
