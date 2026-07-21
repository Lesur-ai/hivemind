# -*- coding: utf-8 -*-
"""P8-3 Space Detail source contract and state-safety regression pins."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
VIEW_PATH = ROOT / "src/live_mem/static/js/admin/views-space-detail.js"
CSS_PATH = ROOT / "src/live_mem/static/css/admin.css"
APP_PATH = ROOT / "src/live_mem/static/js/admin-app.js"
HTML_PATH = ROOT / "src/live_mem/static/admin.html"
DELETE_RUNTIME_PATH = ROOT / "tests/js/admin_space_delete_runtime.mjs"
PRELOAD_RUNTIME_PATH = ROOT / "tests/js/admin_space_detail_preload_runtime.mjs"


def _source() -> str:
    return VIEW_PATH.read_text(encoding="utf-8")


def _function(name: str, source: str | None = None) -> str:
    text = source or _source()
    start = text.index(f"function {name}(")
    boundaries = [
        boundary
        for marker in ("\n    function ", "\n    async function ")
        if (boundary := text.find(marker, start + 1)) >= 0
    ]
    end = min(boundaries) if boundaries else len(text)
    return text[start:end]


def test_space_detail_registers_and_validates_id_before_calling_tools() -> None:
    source = _source()
    render = _function("render", source)
    assert "AdminViews.register('space-detail', render)" in source
    assert "SPACE_ID_RE.test(spaceId)" in render
    assert render.index("SPACE_ID_RE.test(spaceId)") < render.index("loadSpace(view)")


def _assert_initial_preload_contract(source: str) -> None:
    load_space = _function("loadSpace", source)
    assert load_space.count("callTool(") == 1
    assert "callTool('space_info'" in load_space
    assert "preparePreload(view)" in load_space
    assert "renderLoadedView(view)" in load_space
    assert "startPreload(view)" in load_space
    assert load_space.index("preparePreload(view)") < load_space.index("renderLoadedView(view)")
    assert load_space.index("renderLoadedView(view)") < load_space.index("startPreload(view)")

    preload = _function("startPreload", source)
    for loader in ("loadShort(view)", "loadMid(view)", "loadLong(view)", "loadRules(view)", "loadBackups(view)"):
        assert loader in preload
    assert "if (hasPermission(view, 'admin')) void loadAccess(view);" in preload


def test_route_entry_preloads_all_permitted_detail_surfaces_once() -> None:
    source = _source()
    _assert_initial_preload_contract(source)

    # Mutation proof: removing the launch of the preload pass must break the
    # contract instead of silently restoring the former lazy-only page.
    mutant = source.replace("if (shouldPreload) startPreload(view);", "", 1)
    assert mutant != source
    with pytest.raises(AssertionError):
        _assert_initial_preload_contract(mutant)


def test_long_status_preloads_lightweight_then_hydrates_graph_only_when_visible() -> None:
    source = _source()
    assert source.count("callTool('graph_status'") == 1
    loader = _function("loadLong", source)
    assert "include_graph: true" in loader
    assert "includeGraph ?" in loader
    assert "void loadLong(view);" in _function("startPreload", source)
    select_start = source.index("registerAction('sd-select-tier'")
    select_end = source.index("registerAction('sd-refresh-space'", select_start)
    assert "loadLong(view, true)" in source[select_start:select_end]


def test_permission_gate_mirrors_server_hierarchy() -> None:
    body = _function("hasPermission")
    assert "['read', 'write', 'manage', 'admin']" in body
    assert "hierarchy.indexOf(candidate) >= required" in body


def test_payload_heavy_and_out_of_scope_tools_never_appear() -> None:
    source = _source()
    for forbidden in (
        "bank_read_all",
        "space_summary",
        "bank_consolidation_queues",
        "space_list",
        "bank_stale_spaces",
        "system_health",
        "graph_connect",
        "backup_restore",
        "backup_download",
        "marked",
        "DOMPurify",
        "prompt(",
    ):
        assert forbidden not in source


def test_graph_push_never_opts_volatile_bank_files_in() -> None:
    source = _source()
    graph_push = _function("graphPush", source)
    assert "callTool('graph_push', { space_id: view.spaceId })" in graph_push
    assert "include_volatile" not in source


def test_long_counts_and_native_graph_use_whitelisted_display_fields() -> None:
    source = _source()
    stats = _function("graphStatsSection", source)
    graph = _function("mountLongGraph", source)
    assert "graphStats.entity_count" in stats
    for field in ("node.label", "node.type", "node.description", "node.mentions", "node.filename"):
        assert field in graph
    for forbidden in ("node.uri", "node.hash", "node.source_path", "node.source_docs"):
        assert forbidden not in graph
    assert "JSON.stringify" not in _function("renderLongData", source)
    assert "createElementNS" in graph
    assert "textContent" in graph


def test_hive_status_table_is_exhaustive_and_unknown_fails_closed() -> None:
    body = _function("hiveStatus")
    for value in (
        "not_a_space",
        "local_only",
        "hivemind_healthy",
        "hivemind_blocked",
        "unsafe",
        "resync_required",
    ):
        assert f"case '{value}'" in body
    default = body[body.index("default:") :]
    assert "failClosedBanner(" in default
    assert "pill('error'" in default
    assert "statusDot('ok'" not in default
    assert "statusDot('neutral'" not in default


def test_fail_closed_copy_is_pinned_while_long_doctrine_slop_is_removed() -> None:
    source = _source()
    long_renderer = _function("renderLongData", source)
    assert "data.binding === 'embedded'" in long_renderer
    assert "data.binding === 'explicit'" in long_renderer
    for exact_copy in (
        "This space is fail-closed. Treat local state as unsafe until a clean resync completes. Do not restore backups over it.",
        "Corrupted or diverged critical state detected. This space is unsafe until a clean resync completes.",
        "Embedded long runtime unreachable — this deployment is out of contract (ADR-0019).",
        "The explicitly configured Graph Memory runtime cannot be reached. Check its URL and credentials.",
    ):
        assert exact_copy in source
    for removed in (
        "Long memory is derived, never authoritative",
        "Explicit projection, not a routine flow; long data is derived.",
        "Disconnect binding",
        "Top entities",
        "Pushed documents",
    ):
        assert removed not in source


def test_access_query_is_admin_gated_and_unfiltered() -> None:
    body = _function("loadAccess")
    assert body.index("hasPermission(view, 'admin')") < body.index(
        "callTool('admin_list_tokens'"
    )
    assert "{ include_revoked: true }" in body
    assert "has_space" not in body


def test_initial_manual_load_ctas_are_replaced_by_preloaded_states() -> None:
    source = _source()
    for old_cta in (
        "Load recent notes",
        "Load bank files",
        "Load long status",
        "Load rules",
        "Load access summary",
        "List backups",
    ):
        assert old_cta not in source
    for retired_action in (
        "sd-load-short",
        "sd-load-mid",
        "sd-load-long",
        "sd-load-rules",
        "sd-load-access",
        "sd-load-backups",
    ):
        assert retired_action not in source
    for retry in (
        "sd-retry-short",
        "sd-retry-mid",
        "sd-retry-long",
        "sd-retry-rules",
        "sd-retry-access",
        "sd-retry-backups",
    ):
        assert retry in source
    assert "sd-apply-short-filters" in source


def test_consolidation_and_mid_to_long_push_are_confirmed_and_scoped() -> None:
    source = _source()
    consolidate_confirm = _function("confirmConsolidate", source)
    consolidate = _function("consolidate", source)
    graph_confirm = _function("confirmGraphPush", source)
    graph_push = _function("graphPush", source)

    assert "data-action=\"sd-confirm-consolidate\"" in source
    assert "showModal(" in consolidate_confirm
    assert "all agents' live notes" in consolidate_confirm
    assert "only your own live notes" in consolidate_confirm
    assert "const args = { space_id: view.spaceId };" in consolidate
    assert "if (hasPermission(view, 'manage')) args.agent = '';" in consolidate
    assert "callTool('bank_consolidate', args)" in consolidate
    assert "result.status === 'running' || result.status === 'queued'" in consolidate
    assert "await loadSpace(view)" in consolidate

    assert "data-action=\"sd-confirm-graph-push\"" in source
    assert "showModal(" in graph_confirm
    assert "Volatile bank files are not included." in graph_confirm
    assert "callTool('graph_push', { space_id: view.spaceId })" in graph_push
    assert "include_volatile" not in graph_push
    assert "await loadLong(view, true)" in graph_push


def test_preload_and_tier_actions_runtime_are_single_flight_and_confirmed() -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract remains pinned")

    completed = subprocess.run(
        [node, str(PRELOAD_RUNTIME_PATH), str(VIEW_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin space detail preload runtime: ok" in completed.stdout


def test_destructive_calls_use_typed_exact_identifiers_and_server_confirm() -> None:
    source = _source()
    backup = _function("confirmBackupDelete", source)
    space = _function("confirmSpaceDelete", source)
    assert "confirmBankDelete" not in source
    assert "callTool('bank_delete'" not in source
    assert "sd-confirm-bank-delete" not in source
    assert "typedConfirmation: backup.backup_id" in backup
    assert "backup_id: backup.backup_id, confirm: true" in backup
    assert "typedConfirmation: view.spaceId" in space
    assert "space_id: view.spaceId, confirm: true" in space
    assert "label !== 'not_a_space' && label !== 'local_only'" in space
    assert "Normal deletion is refused by the server" in space
    assert "Advanced unsafe recovery is MCP-only" in space
    assert "unsafe_recovery" not in space
    assert "Quiescence required before deletion" not in space
    assert "Access grants for this space remain on tokens" in space


def test_rules_and_mid_are_sanitized_markdown_readers() -> None:
    source = _source()
    rules = _function("renderRules", source)
    mid = _function("readBankFile", source)
    assert "renderMarkdown(rules)" in rules
    assert 'data-action="sd-edit-rules"' in rules
    assert "showModal(" in _function("openRulesEditor", source)
    assert "renderMarkdown(result.content)" in mid
    assert "if (files.length) void readBankFile(view, 0);" in _function("loadMid", source)
    assert 'class="sd-file-row' in _function("renderMidData", source)


def test_admin_markdown_boundary_is_vendored_sanitized_and_fail_closed() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    app = APP_PATH.read_text(encoding="utf-8")
    renderer = app[
        app.index("function renderMarkdown(") : app.index("\nfunction fmtSize(")
    ]
    server = app[
        app.index("function serverMessage(") : app.index("\nfunction pageHeader(")
    ]
    assert html.index("/static/vendor/marked.min.js") < html.index(
        "/static/vendor/purify.min.js"
    ) < html.index("/static/js/admin-app.js")
    assert "marked.parse" in renderer
    assert "DOMPurify.sanitize" in renderer
    assert "ALLOWED_TAGS" in renderer
    assert "'img'" not in renderer
    assert renderer.count("esc(text)") >= 2
    assert "renderMarkdown" not in server
    assert "serverMessage" not in renderer


def test_typed_delete_challenge_is_quoted_without_case_transform() -> None:
    app = APP_PATH.read_text(encoding="utf-8")
    css = CSS_PATH.read_text(encoding="utf-8")
    destructive = app[
        app.index("function showDestructiveModal(") : app.index("\n// ═══════════════ TOASTS")
    ]
    assert 'class="typed-challenge">&quot;${challenge}&quot;' in destructive
    assert ".typed-challenge { text-transform: none;" in css


def test_space_delete_partial_is_a_typed_non_success_branch() -> None:
    source = _source()
    confirm = _function("confirmSpaceDelete", source)
    renderer = _function("renderSpaceDeleteRecovery", source)
    partial_start = confirm.index("result.status === 'partial'")
    success_start = confirm.index("result.status === 'deleted'")
    partial_branch = confirm[partial_start:success_start]

    assert partial_start < success_start
    assert "result.recovery_required === true" in partial_branch
    assert "showSpaceDeleteRecovery(result)" in partial_branch
    assert "return false" in partial_branch
    assert "showToast('ok'" not in partial_branch
    assert "AdminRouter.go(" not in partial_branch
    for field in (
        "result.files_total",
        "result.files_deleted",
        "result.failed_keys",
        "result.marker_preserved",
        "recovery.retry_safe",
        "recovery.action",
    ):
        assert field in renderer
    assert 'data-recovery-required="true"' in renderer
    assert "No automatic retry" in renderer


def test_space_delete_partial_runtime_keeps_modal_and_never_auto_retries(
    tmp_path: Path,
) -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract remains pinned")

    def run(subject: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [node, str(DELETE_RUNTIME_PATH), str(subject)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    completed = run(VIEW_PATH)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin space delete recovery runtime: ok" in completed.stdout

    branch = """                if (result.status === 'partial' && result.recovery_required === true) {
                    showSpaceDeleteRecovery(result);
                    return false;
                }
"""
    source = _source()
    assert source.count(branch) == 1
    mutant_path = tmp_path / "views-space-detail-without-partial-branch.js"
    mutant_path.write_text(source.replace(branch, "", 1), encoding="utf-8")
    mutant = run(mutant_path)
    assert mutant.returncode != 0, "runtime must kill removal of the partial branch"


def test_every_tool_await_has_a_captured_epoch_guard() -> None:
    source = _source()
    call_sites = [match.start() for match in re.finditer(r"await callTool\(", source)]
    assert len(call_sites) >= 12
    for call_site in call_sites:
        before = source[max(0, call_site - 220) : call_site]
        after = source[call_site : call_site + 260]
        assert "const epochAtCall =" in before
        assert re.search(r"if \(!guarded\(view, epochAtCall\)\)\s*(?:return|\{)", after)


def test_short_filter_race_is_sequence_guarded_and_mutation_proven(tmp_path: Path) -> None:
    source = _source()
    guard = "if (seqAtCall !== view.shortSeq) return;"
    assert guard in _function("loadShort", source)
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source guard remains pinned")

    harness = tmp_path / "short-race-harness.js"
    harness.write_text(
        """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const inputs = {
  sdShortLimit: { value: '50' },
  sdShortCategory: { value: 'observation' },
  sdShortAgent: { value: '' },
  sdShortSince: { value: '' },
  sdTierPanel: { innerHTML: '' },
};
const pending = [];
globalThis.esc = value => String(value ?? '');
globalThis.AdminRouter = { epoch: 7 };
globalThis.document = {
  getElementById: id => inputs[id] || null,
  addEventListener: () => {},
};
globalThis.registerAction = () => {};
globalThis.callTool = (_tool, args) => new Promise(resolve => pending.push({ args, resolve }));
globalThis.stateEmpty = () => '<empty>';
globalThis.stateLoading = () => '<loading>';
globalThis.stateError = () => '<error>';
globalThis.renderTimestamp = value => String(value ?? '');
globalThis.pill = (_kind, label) => String(label ?? '');
globalThis.icon = () => '';
globalThis.fmtSize = value => String(value ?? '');
globalThis.dataTable = () => '<table></table>';
vm.runInThisContext(source, { filename: process.argv[2] });

const view = {
  ctx: { epoch: 7, identity: { permissions: ['read'] } },
  info: {}, spaceId: 'demo', tier: 'short',
  shortFilters: { limit: 50, category: '', agent: '', since: '' },
  shortData: null, shortLoading: false, shortSeq: 0,
};
globalThis.__spaceDetailTest.setCurrentView(view);

(async () => {
  const older = globalThis.__spaceDetailTest.loadShort(view);
  inputs.sdShortCategory.value = 'decision';
  const newer = globalThis.__spaceDetailTest.loadShort(view);
  pending[1].resolve({ status: 'ok', notes: [], category: pending[1].args.category });
  await newer;
  pending[0].resolve({ status: 'ok', notes: [], category: pending[0].args.category });
  await older;
  process.stdout.write(JSON.stringify({
    filter: view.shortFilters.category,
    data: view.shortData.category,
    loading: view.shortLoading,
  }));
})().catch(error => { console.error(error); process.exit(1); });
""",
        encoding="utf-8",
    )

    def run(candidate: str, name: str) -> dict[str, object]:
        instrumented = candidate.replace(
            "AdminViews.register('space-detail', render);",
            "globalThis.__spaceDetailTest = { loadShort, setCurrentView: value => { currentView = value; } };",
        )
        subject = tmp_path / name
        subject.write_text(instrumented, encoding="utf-8")
        completed = subprocess.run(
            [node, str(harness), str(subject)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    assert run(source, "guarded.js") == {
        "filter": "decision",
        "data": "decision",
        "loading": False,
    }
    mutant = source.replace(guard, "", 1)
    assert mutant != source
    assert run(mutant, "unguarded-mutant.js")["data"] == "observation"


def test_job_guarantee_badges_use_literal_value_and_required_tooltip() -> None:
    source = _source()
    assert "Job state lives in server memory: it does not survive a restart and history is trimmed." in source
    assert "guaranteeBadge(queue.guarantee)" in _function("renderLane", source)
    assert "guaranteeBadge((view.info.consolidation_queue || {}).guarantee)" in _function("renderAuxiliary", source)
    assert "pill('neutral', 'in-memory best effort')" not in source


def test_activity_job_drilldown_is_field_mapped_manual_and_guarded() -> None:
    source = _source()
    loader = _function("loadJob", source)
    inspector = _function("renderJobInspector", source)
    result = _function("renderJobResult", source)
    assert "activityJobIds(view).has(normalizedJobId)" in loader
    assert "callTool('bank_consolidation_status', { job_id: normalizedJobId })" in loader
    assert "if (seqAtCall !== view.jobSeq) return;" in loader
    assert source.count("callTool('bank_consolidation_status'") == 1
    assert 'data-action="sd-load-job"' in _function("renderActivity", source)
    assert "The server restarted or trimmed its history (100-job cap)." in inspector
    for field in (
        "job.scope_label",
        "job.requested_by",
        "job.requested_at",
        "job.queued_at",
        "job.started_at",
        "job.finished_at",
        "job.progress",
        "job.error",
    ):
        assert field in source
    for field in (
        "result.notes_processed",
        "result.bank_files_updated",
        "result.bank_files_created",
        "result.bank_files_unchanged",
        "result.operations_applied",
        "result.operations_failed",
        "result.synthesis_size",
        "result.llm_tokens_used",
        "result.llm_prompt_tokens",
        "result.llm_completion_tokens",
        "result.batches_total",
        "result.batches_completed",
        "result.batch_size",
        "result.duration_seconds",
    ):
        assert field in result


def test_forbidden_sinks_and_mock_markers_are_absent() -> None:
    source = _source()
    for forbidden in (
        "document.write(",
        "insertAdjacentHTML(",
        "javascript:",
        "data:text/html",
        "mockData",
        "fixtureData",
    ):
        assert forbidden not in source


def test_css_changes_stay_inside_space_detail_banner() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    start = css.index("/* ===== view:space-detail (P8-3) ===== */")
    end = css.index("/* ===== view:consolidation (P8-4) ===== */")
    section = css[start:end]
    assert ".sd-page" in section
    assert ".sd-banner--error" in section
    assert "@media (max-width: 1100px)" in section
    assert "@media (max-width: 1023px)" in section
    assert "@media (max-width: 767px)" not in section
