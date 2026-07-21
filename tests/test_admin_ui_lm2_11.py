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
SPACES = ROOT / "src/live_mem/static/js/admin/views-spaces.js"
RUNTIME = ROOT / "tests/js/admin_access_roles_runtime.mjs"


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

    def test_admin_header_has_a_clear_token_edit_entrypoint(self):
        source = _read(ACCESS)
        header = _body(source, "adminHeaderActions")
        manager_header = _body(source, "managerHeaderActions")
        picker = _body(source, "openEditPickerModal")
        action = next(
            line for line in source.splitlines()
            if "registerAction('access-open-edit'" in line
        )

        assert 'data-action="access-open-edit"' in header
        assert "Edit token" in header
        assert "access-open-edit" not in manager_header
        assert "_tokenListEpoch !== AdminRouter.epoch" in picker
        assert "!isInternalLong(token) && !token.revoked" in picker
        assert "Select by full token ID" in picker
        assert "openEditModal(editDataFromToken(selected))" in picker
        assert "requireGlobalAdmin()" in action

    def test_manager_actions_cannot_forge_an_admin_tool_call(self):
        source = _read(ACCESS)
        for action in (
            "access-open-edit",
            "access-edit",
            "access-revoke",
            "access-delete",
            "access-purge",
            "access-revoke-do",
            "access-delete-do",
        ):
            line = next(
                line for line in source.splitlines() if f"registerAction('{action}'" in line
            )
            if line.rstrip().endswith("{"):
                # Multi-line destructive handler: its first executable line is
                # pinned separately below.
                continue
            assert "requireGlobalAdmin()" in line
        assert source.count("if (!requireGlobalAdmin()) return;") == 2

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
