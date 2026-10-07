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
API_PATH = ROOT / "src/live_mem/static/js/admin-api.js"
HTML_PATH = ROOT / "src/live_mem/static/admin.html"
DELETE_RUNTIME_PATH = ROOT / "tests/js/admin_space_delete_runtime.mjs"
PRELOAD_RUNTIME_PATH = ROOT / "tests/js/admin_space_detail_preload_runtime.mjs"
COMPACTION_RUNTIME_PATH = ROOT / "tests/js/admin_space_detail_compaction_runtime.mjs"
MESH_RUNTIME_PATH = ROOT / "tests/js/admin_space_detail_mesh_runtime.mjs"


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
    for loader in ("startMemory(view)", "startLong(view)", "loadRules(view)"):
        assert loader in preload
    assert "if (view.tab === 'memory')" in preload
    assert "view.tab === 'long'" in preload
    assert "view.tab === 'rules'" in preload
    assert "loadBackups(view)" not in preload  # The scoped Operator owns this read.
    assert "view.tab === 'access' && hasPermission(view, 'admin')" in preload


def test_route_entry_preloads_only_the_active_permitted_section_once() -> None:
    source = _source()
    _assert_initial_preload_contract(source)

    # Mutation proof: removing the launch of the preload pass must break the
    # contract instead of leaving the active page without its initial data.
    mutant = source.replace("if (shouldPreload) startPreload(view);", "", 1)
    assert mutant != source
    with pytest.raises(AssertionError):
        _assert_initial_preload_contract(mutant)


def test_long_panels_choose_status_or_graph_without_invisible_hydration() -> None:
    source = _source()
    assert source.count("callTool('graph_status'") == 1
    loader = _function("loadLong", source)
    assert "include_graph: includeGraph" in loader
    assert "loadLong(view, true)" not in loader
    assert "view.tab === 'long') startLong(view)" in _function("startPreload", source)
    assert "loadLong(view, view.longPanel === 'graph')" in _function("refreshLongPanel", source)
    select_start = source.index("registerAction('sd-select-tier'")
    tier_action = source[select_start:source.index("registerAction('sd-select-tab'", select_start)]
    assert "view.tab !== 'memory'" in tier_action
    assert "loadLong" not in tier_action
    assert "requestMemoryRefresh(view)" in tier_action
    assert "loadShort" not in tier_action and "loadMid" not in tier_action


def test_permission_gate_mirrors_server_hierarchy() -> None:
    body = _function("hasPermission")
    assert "['read', 'write', 'manage', 'admin']" in body
    assert "hierarchy.indexOf(candidate) >= required" in body


def test_mesh_readiness_is_targeted_authoritative_and_navigation_only() -> None:
    source = _source()
    api = API_PATH.read_text(encoding="utf-8")
    loader = _function("loadMeshReadiness", source)
    action = _function("meshReadinessAction", source)

    assert "async function meshAdminSourceReadiness(spaceId)" in api
    assert "_meshFetch('source-readiness/' + encodeURIComponent(spaceId))" in api
    assert "meshReadinessAvailable(view)" in loader
    assert "meshAdminSourceReadiness(view.spaceId)" in loader
    assert "authoritativeMeshSource(view, result)" in loader
    assert "hive_status_label" not in loader
    assert "hive_status_label" not in action
    assert "source.state === 'local_only_can_prepare'" in action
    assert "source.source_initializable === true" in action
    assert "source.state === 'preparing'" in action
    assert "source.resumable === true" in action
    assert "meshAdminAction(" not in source
    assert "prepare-source" not in source
    assert 'href="#/mesh"' in _function("renderMeshReadiness", source)


def test_mesh_readiness_runtime_gates_and_stale_response_pass() -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract remains pinned")

    completed = subprocess.run(
        [node, str(MESH_RUNTIME_PATH), str(VIEW_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin space detail mesh readiness runtime: ok" in completed.stdout


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
        "backup_list",
        "backup_create",
        "backup_delete",
        "bank_consolidation_status",
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
    assert "stats.entity_count" in stats
    assert "stats.entity_types" in stats
    assert "Number.isSafeInteger(value)" in stats
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
    long_renderer = _function("renderLongHealth", source)
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
    ):
        assert retry in source
    assert "sd-apply-short-filters" in source


def test_consolidation_and_mid_to_long_push_are_confirmed_and_scoped() -> None:
    source = _source()
    consolidate_confirm = _function("confirmConsolidate", source)
    graph_confirm = _function("confirmGraphPush", source)
    graph_push = _function("graphPush", source)

    assert "data-action=\"sd-confirm-consolidate\"" in source
    assert "openConsolidationLauncher({" in consolidate_confirm
    assert "spaces: [view.info]" in consolidate_confirm
    assert "lanes: [{ ...view.info.consolidation_queue, space_id: view.spaceId }]" in consolidate_confirm
    assert "spaceId: view.spaceId, ctx: view.ctx" in consolidate_confirm
    assert "onSubmitted:" in consolidate_confirm
    assert "if (guarded(view, view.ctx.epoch)) AdminRouter.go(" in consolidate_confirm
    assert "callTool(" not in consolidate_confirm

    assert "data-action=\"sd-confirm-graph-push\"" in source
    assert "showModal(" in graph_confirm
    assert "Volatile bank files are not included." in graph_confirm
    assert "callTool('graph_push', { space_id: view.spaceId })" in graph_push
    assert "include_volatile" not in graph_push
    assert "if (view.tab === 'long') await loadLong(view, view.longPanel === 'graph')" in graph_push


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


@pytest.mark.parametrize("old,new", [
    ("function startPreload(view) {", "function startPreload(view) { void loadRules(view);"),
    ("else if (view.tab === 'access' && hasPermission(view, 'admin'))", "else if (hasPermission(view, 'admin'))"),
    ("else if (view.tab === 'backups' && hasPermission(view, 'write')) {", "else if (view.tab === 'backups') {"),
    ("view.memoryReadRevision !== view.memoryRevision", "true"),
    ("if (view.consolidationMounted) return;", ""),
    ("if (view.backupsMounted) return;", ""),
    ("spaceId: view.spaceId, embedded: true", "spaceId: 'wrong-space', embedded: true"),
    ("        if (view.tab === 'consolidation') {", "        if (false) {"),
    ("AdminViews.register('space-detail', render);", "registerAction('sd-load-job', () => {}); AdminViews.register('space-detail', render);"),
    ("AdminViews.register('space-detail', render);", "registerAction('sd-confirm-backup-delete', () => {}); AdminViews.register('space-detail', render);"),
])
def test_section_loading_guards_are_mutation_proven(tmp_path, old, new):
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "Node.js is required for the section-loading proof"
    source = _source()
    assert source.count(old) == 1
    mutant = tmp_path / "views-space-detail.js"
    mutant.write_text(source.replace(old, new, 1), encoding="utf-8")
    completed = subprocess.run(
        [node, str(PRELOAD_RUNTIME_PATH), str(mutant)],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    assert completed.returncode != 0, "section-loading mutation survived: " + old
    assert "AssertionError" in completed.stderr


@pytest.mark.parametrize("old,new", [
    ("space_id: view.spaceId, ...view.shortFilters", "space_id: view.spaceId, ...view.shortDraft"),
    ("if (row.renderedContent !== note.content)", "if (true)"),
    ("view.midSelectedFilename === null && result.files.length", "result.files.length"),
    ("if (view.midSelectedFilename) await readBankFile", "if (view.midSelectedFilename && !view.midFileData) await readBankFile"),
    ("view.midSelectedFilename !== filename || !isCurrent()", "view.midSelectedFilename !== filename"),
    ("if (!initial) reads.push(readMemoryMetadata(view, current))", "reads.push(readMemoryMetadata(view, current))"),
    ("data.has_more === true", "notes.length === view.shortFilters.limit"),
    ("return current() ? { follow: true } : { skipped: true };", "return { follow: true };"),
    ("        } catch (error) {\n            if (!current()) return { skipped: true };", "        } catch (error) {"),
    ("memoryHtml(document.getElementById('sdMidGraphPushActions'), renderGraphPushAction(view));", ""),
    ("if (!sessionGenerationIsCurrent(view.ctx.sessionGeneration)) return;", ""),
    ("view.midFileData = null; view.midPreviewHtml = ''; view.midFileSuccess = null;\n            view.midRenderedContent = null; view.midRenderedFilename = null;", "view.midFileData = null; view.midPreviewHtml = ''; view.midFileSuccess = null;"),
])
def test_memory_reader_guards_are_mutation_proven(tmp_path, old, new):
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "Node.js is required for the Memory-reader proof"
    source = _source()
    target = _function("refreshMemory", source) if "return current() ?" in old else source
    assert target.count(old) == 1
    mutant = tmp_path / "views-space-detail.js"
    mutant.write_text(source.replace(target, target.replace(old, new, 1), 1), encoding="utf-8")
    result = subprocess.run(
        [node, str(PRELOAD_RUNTIME_PATH), str(mutant)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0, "Memory mutation survived: " + old
    assert "AssertionError" in result.stderr, result.stdout + result.stderr


def test_manual_compaction_is_mid_only_manage_gated_two_step_and_bound() -> None:
    source = _source()
    mid = _function("renderMid", source)
    short = _function("renderShort", source)
    action = _function("renderCompactionAction", source)
    run = _function("runCompaction", source)
    confirm = _function("confirmCompact", source)

    assert "renderCompactionAction(view)" in mid
    assert 'data-action="sd-compact-dry"' in action
    assert 'data-action="sd-compact-dry"' not in short
    assert "if (!hasPermission(view, 'manage')) return '';" in action
    assert "if (!hasPermission(view, 'manage') || view.compacting) return false;" in run
    assert "callTool('bank_compact', { space_id: view.spaceId, dry_run: dryRun })" in run
    assert "result.space_id !== view.spaceId" in run
    assert "view.compactDry === data" in _function("renderCompactionReport", source)
    assert "view.compactResult !== view.compactDry" in confirm
    assert "view.compactApplying" in run
    assert "view.compactApplying) return;" in _function("invalidateCompaction", source)
    assert "compactApplySeq" in run
    assert "Compaction recovery required" in _function("compactFailureMarkup", source)
    assert "Target resolution" in _function("compactFailureMarkup", source)
    assert "showModal(" in confirm
    assert "view.spaceId !== captured" in confirm
    assert "await loadMid(view, { preserveCompaction: true })" in run
    assert "result.status === 'ok' || result.status === 'partial'" in run
    assert "setInterval(" not in run


def test_manual_compaction_runtime_and_mutation_guards(tmp_path: Path) -> None:
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("Node.js runtime unavailable; source contract remains pinned")

    def run(candidate: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [node, str(COMPACTION_RUNTIME_PATH), str(candidate)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    source = _source()
    completed = run(VIEW_PATH)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "admin space detail compaction runtime: ok" in completed.stdout

    gate = "        if (!hasPermission(view, 'manage') || view.compacting) return false;\n"
    assert source.count(gate) == 1
    gate_mutant = tmp_path / "views-space-detail-without-compaction-gate.js"
    gate_mutant.write_text(source.replace(gate, "", 1), encoding="utf-8")
    mutant = run(gate_mutant)
    assert mutant.returncode != 0, "runtime must kill removal of the manage gate"

    revoke = "        view.compacting = true;\n        view.compactApplying = !dryRun;\n        view.compactDry = null;\n"
    assert source.count(revoke) == 1
    binding_mutant = tmp_path / "views-space-detail-without-dry-run-binding.js"
    binding_mutant.write_text(
        source.replace(revoke, "        view.compacting = true;\n        view.compactApplying = !dryRun;\n", 1),
        encoding="utf-8",
    )
    mutant = run(binding_mutant)
    assert mutant.returncode != 0, "runtime must kill Apply without the successful dry-run binding"

    target_guard = "        if (result && result.status === 'ok' && result.space_id !== view.spaceId) {\n            result = { status: 'error', message: 'Compaction target did not match this space.' };\n        }\n"
    assert source.count(target_guard) == 1
    target_mutant = tmp_path / "views-space-detail-without-target-guard.js"
    target_mutant.write_text(source.replace(target_guard, "", 1), encoding="utf-8")
    mutant = run(target_mutant)
    assert mutant.returncode != 0, "runtime must kill an out-of-scope compaction result"

    apply_refresh_guard = "        if (view.compactApplying) return;\n"
    assert source.count(apply_refresh_guard) == 1
    apply_refresh_mutant = tmp_path / "views-space-detail-without-apply-refresh-guard.js"
    apply_refresh_mutant.write_text(source.replace(apply_refresh_guard, "", 1), encoding="utf-8")
    mutant = run(apply_refresh_mutant)
    assert mutant.returncode != 0, "runtime must kill a concurrent dry run during Apply"


def test_space_delete_uses_typed_exact_identifier_and_server_confirm() -> None:
    source = _source()
    space = _function("confirmSpaceDelete", source)
    actions = _function("renderDeleteAction", source)
    assert "confirmBankDelete" not in source
    assert "callTool('bank_delete'" not in source
    assert "sd-confirm-bank-delete" not in source
    # Scoped backups use Operator; its real confirmation and server arguments
    # are exercised in admin_gc_runtime, not in a parallel Space implementation.
    assert "typedConfirmation: view.spaceId" in space
    assert "space_id: view.spaceId, confirm: true" in space
    assert "label !== 'not_a_space' && label !== 'local_only'" in space
    assert "Normal deletion is refused by the server" in space
    assert "Advanced unsafe recovery is MCP-only" in space
    assert "unsafe_recovery" not in space
    assert (
        "callTool('space_delete', { space_id: view.spaceId, confirm: true })"
        in space
    )
    assert "recover_access_grants:" not in space
    assert "If grant recovery is required, it is MCP/CLI-only" in space
    assert "this console never sends recover_access_grants" in space
    assert "Quiescence required before deletion" not in space
    assert "removes the space from every token allowlist" in space
    assert "never restores previous access" in space
    assert "Existing token grants must be removed" not in actions
    assert "removes the space from every token allowlist" in actions
    assert "never restores previous access" in actions
    assert "result.files_deleted" in space
    assert "result.access_grants_removed" in space
    assert "result.status === 'grants_cleaned'" in space
    assert "The space was not deleted by this result." in space


def test_rules_and_mid_are_sanitized_markdown_readers() -> None:
    source = _source()
    rules = _function("renderRules", source)
    mid = _function("readBankFile", source)
    assert "renderMarkdown(rules)" in rules
    assert 'data-action="sd-edit-rules"' in rules
    assert "showModal(" in _function("openRulesEditor", source)
    assert "renderMarkdown(result.content)" in mid
    assert "await readBankFile(view, view.midSelectedFilename, options.isCurrent)" in _function("loadMid", source)
    assert "tab.dataset.filename = file.filename" in _function("renderMidData", source)
    assert "setAttribute('role', 'tab')" in _function("renderMidData", source)


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
        "result.access_grants_pending",
        "recovery.retry_safe",
        "recovery.action",
    ):
        assert field in renderer
    assert 'data-recovery-required="true"' in renderer
    assert "No automatic retry" in renderer
    assert "Grant-recovery retry is MCP/CLI-only" in renderer
    assert "This console never sends recover_access_grants" in renderer


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

    deleted_branch = """                if (result.status === 'deleted' || result.status === 'ok') {
                    const filesDeleted = Number(result.files_deleted || 0);
                    const grantsRemoved = Number(result.access_grants_removed || 0);
                    showToast('ok', `Space deleted (${filesDeleted} files, ${grantsRemoved} token grants removed).`);
                    AdminRouter.go('/spaces');
                    return true;
                }
"""
    assert source.count(deleted_branch) == 1
    deleted_mutant_path = tmp_path / "views-space-detail-without-deleted-branch.js"
    deleted_mutant_path.write_text(
        source.replace(deleted_branch, "", 1),
        encoding="utf-8",
    )
    deleted_mutant = run(deleted_mutant_path)
    assert deleted_mutant.returncode != 0, (
        "runtime must kill removal of the successful deletion branch"
    )


def test_every_tool_await_has_a_captured_epoch_guard() -> None:
    source = _source()
    call_sites = [match.start() for match in re.finditer(r"await callTool\(", source)]
    assert call_sites  # Check every retained call, not a minimum of retired paths.
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
  shortFilters: { limit: 50, category: 'observation', agent: '', since: '' },
  shortData: null, shortLoading: false, shortSeq: 0,
};
globalThis.__spaceDetailTest.setCurrentView(view);

(async () => {
  const older = globalThis.__spaceDetailTest.loadShort(view);
  view.shortFilters.category = 'decision';
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


def test_consolidation_delegates_jobs_and_keeps_space_tools_separate() -> None:
    source = _source()
    auxiliary = _function("renderAuxiliary", source)
    assert "AdminViews.get('consolidation')" in auxiliary
    assert "spaceId: view.spaceId, embedded: true" in auxiliary
    assert "initialLane: view.info.consolidation_queue" in auxiliary
    assert 'id="sdConsolidationJobs"></div>${renderConsolidationTools(view)}' in auxiliary
    assert "renderActivity(view)" not in auxiliary
    assert "renderLane(view)" not in auxiliary
    for retired in (
        "renderLane", "renderActivity", "renderSpaceActions", "activityJobIds",
        "renderJobProgress", "renderJobResult", "renderJobInspector", "loadJob",
        "renderBackups", "loadBackups", "createBackup", "confirmBackupDelete",
        "guaranteeBadge", "renderBankSizeAdvisory", "laneSeverity",
    ):
        assert f"function {retired}(" not in source
    tier = _function("renderTier", source)
    assert "report.innerHTML = renderCompactionReport(view)" in tier
    assert "action.innerHTML = renderCompactionAction(view)" in tier
    # The shared renderer's payload, guarantee and auto-compaction contracts are
    # tested in test_admin_ui_p8_4 and admin_consolidation_live_refresh_runtime.
    # Space's runtime below proves scoped mounting and non-remount on late data.


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
    # #629 adds phone cards only for Documents; other Space sections retain
    # their existing responsive contract.
    mobile_documents = section.split("@media (max-width: 767px) {", 1)[1].split("\n}", 1)[0]
    assert all(selector.strip().startswith((".sd-document", ".sd-documents"))
               for selector in re.findall(r"^\s*([^{}]+)\{", mobile_documents, re.M))
