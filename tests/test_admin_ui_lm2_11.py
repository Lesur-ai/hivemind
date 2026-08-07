"""LM2-11 capability split for the admin console.

Static pins make each policy branch mutation-visible. A dependency-free Node
VM harness exercises the same shipped modules and records their actual tool
calls for write/manage/admin identities.
"""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parent.parent
ACCESS = ROOT / "src/live_mem/static/js/admin/views-access.js"
ADMIN_CSS = ROOT / "src/live_mem/static/css/admin.css"
SPACES = ROOT / "src/live_mem/static/js/admin/views-spaces.js"
RUNTIME = ROOT / "tests/js/admin_access_roles_runtime.mjs"
CREATE_RECOVERY_RUNTIME = ROOT / "tests/js/admin_space_create_recovery_runtime.mjs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(source: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\([^)]*\)\s*\{{", source)
    assert match, f"function {name} not found"
    next_top = re.search(
        r"\n    (?:async )?function |\n    registerAction\(|\n    AdminViews\.",
        source[match.end() :],
    )
    end = match.end() + next_top.start() if next_top else len(source)
    return source[match.start() : end]


class TestAccessCapabilitySplit:
    def test_manager_branch_purges_admin_cache_and_returns_before_list(self):
        source = _read(ACCESS)
        render = _body(source, "render")
        manager = re.search(
            r"if \(!hasGlobalAdmin\(identity\)\) \{(.*?)\n        \}",
            render,
            re.DOTALL,
        )
        assert manager
        assert "clearAdminTokenCache()" in manager.group(1)
        assert "return;" in manager.group(1)
        assert "loadTokens" not in manager.group(1)
        assert "admin_list_tokens" not in manager.group(1)
        clear = _body(source, "clearAdminTokenCache")
        assert "cache.tokens = []" in clear
        assert "cache.spaces = []" in clear

    def test_manager_create_is_safe_unscoped_and_cannot_mint_admin(self):
        source = _read(ACCESS)
        modal = _body(source, "openCreateModal")
        confirm = _body(source, "onCreateConfirm")
        assert "PERMISSION_PRESETS.slice(0, 3)" in modal
        assert "adminMode ?" in modal
        assert "New tokens start with no space access" in modal
        assert "if (adminMode && spaces && !targetIsAdmin) args.space_ids = spaces" in confirm
        assert "targetIsAdmin" in confirm
        assert "syncAdminScopeInvariant" in modal
        assert "scopeInput.disabled = targetAdmin" in modal
        assert "space_ids: []" in modal
        assert "safePresetValues.indexOf(selectedPermissions) === -1" in confirm
        assert "await callTool('token_create', args)" in confirm
        assert "await callTool('admin_create_token', args)" in confirm

    def test_invite_uses_a_fresh_space_list_and_exact_full_hash(self):
        source = _read(ACCESS)
        opener = _body(source, "openInviteModal")
        confirm = _body(source, "onInviteConfirm")
        assert "await callTool('space_list', {})" in opener
        assert "cache.spaces" not in opener
        assert "This list was refreshed when the dialog opened" in opener
        assert "/^sha256:[0-9a-f]{64}$/" in confirm
        assert "await callTool('space_invite_token'" in confirm
        assert "res.added === false" in confirm

    def test_admin_edit_never_sends_dormant_scope_delta(self):
        source = _read(ACCESS)
        modal = _body(source, "openEditModal")
        confirm = _body(source, "onEditConfirm")
        secret = _body(source, "showTokenSecret")
        assert "syncEditAdminScopeInvariant" in modal
        assert "editBoxes[i].disabled = targetAdmin" in modal
        assert "Saving an admin profile clears space_ids" in modal
        assert "if (!targetIsAdmin)" in confirm
        assert "var effectivePerms = newPerms || currentPerms" in confirm
        assert confirm.index("if (!targetIsAdmin)") < confirm.index("space_ids_add")
        assert "if (!uncertain && res.snapshot_taken)" in secret
        assert "New spaces will not be added automatically." in secret
        assert "serverMessage(res.info)" not in secret
        assert "if (res.info) msg = res.info" in confirm

    def test_each_admin_row_has_one_exact_hash_action_menu(self):
        source = _read(ACCESS)
        header = _body(source, "adminHeaderActions")
        manager_header = _body(source, "managerHeaderActions")
        row = _body(source, "renderRow")
        lookup = _body(source, "freshTokenForAction")

        assert 'data-action="access-open-edit"' not in header
        assert "Edit token" not in header
        assert "access-open-edit" not in manager_header
        assert '<details class="row-action-menu"' in row
        assert '<summary class="row-action-trigger"' in row
        assert 'role="button"' not in row
        assert 'role="menu"' not in row
        assert 'role="menuitem"' not in row
        assert 'data-action="access-close-menu"' in row
        for label in ("Edit token", "Create replacement", "Disable token", "Reactivate token", "Delete permanently"):
            assert label in row
        assert row.count('data-hash="') == 1
        assert "_tokenListEpoch !== AdminRouter.epoch" in lookup
        assert "tokens[i].hash === hash" in lookup
        assert "openEditModal(editDataFromToken(token))" in source

    def test_manager_actions_cannot_forge_an_admin_tool_call(self):
        source = _read(ACCESS)
        for action in (
            "access-edit",
            "access-replace",
            "access-revoke",
            "access-delete",
            "access-purge",
            "access-revoke-do",
            "access-delete-do",
        ):
            handler = re.search(
                rf"registerAction\('{re.escape(action)}', function \([^)]*\) \{{(.*?)\n    \}}\);",
                source,
                re.DOTALL,
            )
            assert handler, f"multi-line handler for {action} not found"
            assert "requireGlobalAdmin()" in handler.group(1)

    def test_row_action_menu_keeps_focus_and_table_overflow_recovery(self):
        source = _read(ACCESS)
        css = _read(ADMIN_CSS)

        assert "function closeAllActionMenus" in source
        assert "event.key === 'Escape'" in source
        assert "positionActionMenu" in source
        assert "portalActionPanel" not in source
        assert "document.body.appendChild(panel)" not in source
        assert ".access-token-table .table-scroll { overflow: visible; }" not in css
        trigger_focus = re.search(r"\.row-action-trigger:focus-visible\s*\{([^{}]*)\}", css)
        item_focus = re.findall(r"\.row-action-item:focus-visible\s*\{([^{}]*)\}", css)
        assert trigger_focus and item_focus
        assert "outline: 2px solid var(--hm-focus)" in trigger_focus.group(1)
        assert any("outline: 2px solid var(--hm-focus)" in block for block in item_focus)

    def test_replacement_never_defaults_an_unavailable_stored_permission_profile(self):
        source = _read(ACCESS)
        prefill = _body(source, "replacementPrefill")
        create = _body(source, "openCreateModal")
        confirm = _body(source, "onCreateConfirm")

        assert "permissionsUnavailable" in prefill
        assert "Object.prototype.hasOwnProperty.call(prefill, 'permissions')" in create
        assert "Stored permission profile unavailable" in create
        assert "Select a permission profile" in confirm

    def test_secret_handoff_keeps_plaintext_and_full_hash_separate(self):
        source = _read(ACCESS)
        secret = _body(source, "showTokenSecret")
        assert 'id="ctSecret"' in secret
        assert 'id="ctTokenHash"' in secret
        assert 'id="ctCopyBtn"' in secret
        assert 'id="ctCopyHashBtn"' in secret
        assert "Copy plaintext" in secret
        assert "Copy Token ID" in secret
        assert "Token ID (full hash)" in secret
        assert "holder.value = ''" in secret
        assert "hashHolder.value = ''" in secret
        assert "Creation state is uncertain" in secret
        assert "Do not discard either value" in secret
        assert "Do not assume the token is active or absent" in secret
        create = _body(source, "onCreateConfirm")
        assert "res.status === 'partial'" in create
        assert "res.recovery_required === true" in create

    def test_successful_self_downgrade_is_applied_before_refresh(self):
        body = _body(_read(ACCESS), "onEditConfirm")
        mutation = "sessionAtCall.permissions = args.permissions.split(',')"
        assert mutation in body
        assert body.index(mutation) < body.index("AdminRouter.refresh()")
        assert "clearAdminTokenCache()" in body

    def test_self_revoke_delete_and_purge_all_drop_local_privileges(self):
        source = _read(ACCESS)
        mutation = _body(source, "runMutation")
        purge = _body(source, "runPurge")
        assert "sessionAtCall.token_hash === hash" in mutation
        assert "dropKnownLocalPrivileges(sessionAtCall)" in mutation
        assert "args.revoked_only === false" in purge
        assert "dropKnownLocalPrivileges(sessionAtCall)" in purge


class TestSpacesCreateGate:
    def test_header_and_empty_state_ctas_require_manage(self):
        source = _read(SPACES)
        render = _body(source, "render")
        empty = _body(source, "_renderBody")
        assert "const createAction = _hasManage(_identity)" in render
        assert "const canCreate = _hasManage(_identity)" in empty
        assert "actionHtml: canCreate" in empty

    def test_forged_create_action_and_submit_both_fail_closed(self):
        source = _read(SPACES)
        assert re.search(
            r"registerAction\('spaces-open-create', \(\) => \{\s*"
            r"if \(!_hasManage\(_liveIdentity\(\)\)\)",
            source,
        )
        submit = _body(source, "_submitCreateSpace")
        assert "if (!_hasManage(_liveIdentity()))" in submit
        assert submit.index("if (!_hasManage(_liveIdentity()))") < submit.index(
            "await callTool('space_create'"
        )

    def test_partial_space_creation_keeps_values_and_renders_typed_recovery(self):
        source = _read(SPACES)
        submit = _body(source, "_submitCreateSpace")
        lock = _body(source, "_lockCreateRetryForAdminRecovery")
        assert "resp.status === 'partial'" in submit
        assert "resp.recovery_required === true" in submit
        assert "String(recovery.retry_safe)" in submit
        assert "String(recovery.action ?? '')" in submit
        assert "recovery.retry_safe:</strong> <code>" in submit
        assert "recovery.action:</strong>" in submit
        assert "data-recovery-required" in submit
        assert "Grant-recovery retry is MCP/CLI-only" in submit
        assert "This console never sends recover_access_grants" in submit
        assert "No automatic cleanup or rollback was performed" in submit
        assert "Identical manual retry is permitted" in submit
        assert "Admin recovery required" in submit
        assert "recovery.retry_safe !== true" in submit
        assert "_lockCreateRetryForAdminRecovery()" in submit
        assert "cloneNode(true)" in lock
        assert "replaceWith(lockedButton)" in lock
        assert "lockedButton.disabled = true" in lock
        assert "lockedButton.textContent = 'Admin recovery required'" in lock
        assert "showToast('ok', 'Space created')" in submit
        assert submit.index("resp.status === 'created'") < submit.index(
            "resp.status === 'partial'"
        )
        partial_branch = submit[submit.index("resp.status === 'partial'") :]
        assert "showToast('ok'" not in partial_branch

    def test_create_recovery_boundary_runtime_is_mutation_proven(
        self, tmp_path: Path
    ):
        node = shutil.which("node") or shutil.which("nodejs")
        if node is None:
            pytest.skip("Node.js runtime unavailable; source contract remains pinned")

        def run(subject: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [node, str(CREATE_RECOVERY_RUNTIME), str(subject)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        completed = run(SPACES)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "admin space create recovery runtime: ok" in completed.stdout

        source = _read(SPACES)
        concatenation = "                    accessRecoveryBoundary +\n"
        assert source.count(concatenation) == 1
        mutant_path = tmp_path / "views-spaces-without-recovery-boundary.js"
        mutant_path.write_text(
            source.replace(concatenation, "", 1),
            encoding="utf-8",
        )
        mutant = run(mutant_path)
        assert mutant.returncode != 0, (
            "runtime must kill removal of the recovery-boundary rendering"
        )


def test_admin_access_roles_node_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    result = subprocess.run(
        [node, str(RUNTIME), str(ACCESS), str(SPACES)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin access roles runtime: ok" in result.stdout


def test_space_partial_retry_safety_runtime_kills_both_branch_mutants(
    tmp_path: Path,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    source = _read(SPACES)
    lock_call = (
        "if (recovery.retry_safe !== true) "
        "_lockCreateRetryForAdminRecovery();"
    )
    assert source.count(lock_call) == 1
    assert source.count("recovery.retry_safe !== true") == 1
    mutants = {
        "unsafe-retry-left-enabled.js": source.replace(
            lock_call,
            "if (recovery.retry_safe !== true) {}",
            1,
        ),
        "safe-retry-locked.js": source.replace(
            "recovery.retry_safe !== true",
            "recovery.retry_safe === true",
            1,
        ),
    }

    for filename, mutant in mutants.items():
        subject = tmp_path / filename
        subject.write_text(mutant, encoding="utf-8")
        result = subprocess.run(
            [node, str(RUNTIME), str(ACCESS), str(subject)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, f"runtime survived {filename}"
