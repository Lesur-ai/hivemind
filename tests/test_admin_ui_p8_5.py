# -*- coding: utf-8 -*-
"""
P8-5 (#143) — Access view contract pins.

MOST tests here are static source-inspection (import-light, per tests/conftest.py
venv constraints and contract §7.2.1): they read the shipped frontend source and
assert the data-honesty, security, destructive-UX, and copy invariants of the
Access view WITHOUT a browser. Convention (like ADM-01): each test tries to
break an invariant, not validate a happy path.

Those static pins fix the *shape* of the one-time-secret create/secret
async-lifecycle guards (statement ordering, which signal each branch reads). The
Terra R2–R4 adversarial reviews flagged that they cannot fix the *behaviour* —
that the deferred `admin_create_token` promise, resolving at a hostile moment,
actually does or doesn't surface the secret / re-enable the modal / revert the
route. TestP85AsyncLifecycleRuntime closes that residual with an EXECUTABLE
deferred-promise regression harness: scenarios A–G (browser-proofed but never
committed in commits 27d559e / 0a2fc0b / 140d054 on the P8-5 branch), plus H
(a `created` response resolving after a session boundary must suppress the prior
session's one-time token — covers the create-branch `_sessionEnded` guard) and I
(async-queued hashchange fidelity — the queued nav-lock revert pins the route
while a create is pending). H and I were added per the Terra PR #167 review.

DECISION — pytest via a subprocess `node` runner, NOT a separate JS test target.
The repo has no package.json / npm test surface; the `pytest tests` CI job runs
with Node 24 on PATH (actions/setup-node@v6). A dependency-free `node:vm` harness
(tests/js/admin_access_lifecycle_runtime.mjs, no jsdom) loads the REAL
views-access.js with faithful shell-global stubs and drives the async lifecycle;
this wrapper only shells out, so the import-light §7.2.1 constraint is preserved.
This mirrors the two sibling harnesses already wired the same way
(tests/js/admin_session_generation_runtime.mjs via test_admin_session_generation,
tests/js/admin_audit_state_runtime.mjs via test_admin_ui_p8_6).

A node:vm harness stubs the shell, so it cannot prove behaviours the real shell
owns (e.g. single in-flight create — the shell disables the confirm button before
awaiting onConfirm). That real-shell layer is proved by the complementary
Playwright integration test in tests/test_admin_ui_p8_5_e2e.py
(tests/e2e/admin_access_create.spec.mjs), added per the Terra PR #167 review.

Contract: DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §3.1.4, §3.3.2, §4.4, §5.0,
§5.7, §6.4, §6.5, §7.1, §7.4, §8.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


_STATIC = Path(__file__).parent.parent / "src" / "live_mem" / "static"
_ACCESS_JS = _STATIC / "js" / "admin" / "views-access.js"
_ADMIN_CSS = _STATIC / "css" / "admin.css"
_RUNTIME_HARNESS = (
    Path(__file__).parent / "js" / "admin_access_lifecycle_runtime.mjs"
)


def _access() -> str:
    return _ACCESS_JS.read_text(encoding="utf-8")


def _css() -> str:
    return _ADMIN_CSS.read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """Return the source of the named top-level view function, from its
    declaration up to the next 4-space-indented function / registerAction /
    AdminViews declaration (the IIFE's top-level boundary)."""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(", src)
    if not m:
        return ""
    nxt = re.search(
        r"\n    (?:async )?function |\n    registerAction\(|\n    AdminViews\.",
        src[m.end():],
    )
    end = m.end() + nxt.start() if nxt else len(src)
    return src[m.start():end]


# ═══════════════════════════════════════════════════════════════
# Registration & routing wiring
# ═══════════════════════════════════════════════════════════════


class TestRegistration:
    def test_registers_access_view(self):
        assert "AdminViews.register('access'" in _access(), (
            "views-access.js must register the 'access' view with the shell registry."
        )

    def test_admin_html_loads_the_module(self):
        html = (_STATIC / "admin.html").read_text(encoding="utf-8")
        assert "js/admin/views-access.js" in html, (
            "admin.html must load the Access view module."
        )


# ═══════════════════════════════════════════════════════════════
# Data honesty (§5.0 / §5.7 / D7 / D8): only whitelisted tools, no invented data
# ═══════════════════════════════════════════════════════════════


class TestDataHonesty:
    # The exhaustive set of tools the Access view is permitted to call (§5.7).
    ALLOWED = {
        "admin_list_tokens",
        "admin_create_token",
        "admin_update_token",
        "admin_revoke_token",
        "admin_delete_token",
        "admin_purge_tokens",
        "token_create",
        "space_invite_token",
        "space_list",
    }
    FORBIDDEN = {
        "admin_bulk_update_tokens",  # §4.10 — not exposed
        "admin_rotate_token",        # D-rotate §6.4 — does not exist
        "admin_gc_notes",            # Maintenance view, not Access
        "admin_audit_recent",        # Audit view
    }

    def _tools_called(self, src: str) -> set[str]:
        # Tools invoked directly as callTool('name', ...) and indirectly through
        # the runMutation('name', ...) helper.
        tools = set(re.findall(r"callTool\(\s*'([a-z_]+)'", src))
        tools |= set(re.findall(r"runMutation\(\s*'([a-z_]+)'", src))
        return tools

    def test_only_whitelisted_tools_are_called(self):
        called = self._tools_called(_access())
        assert called, "No tool calls found — the source pattern may have drifted."
        extra = called - self.ALLOWED
        assert not extra, (
            f"Access view calls non-whitelisted tool(s): {sorted(extra)}. "
            f"§5.7 permits only {sorted(self.ALLOWED)}."
        )

    def test_forbidden_tools_absent(self):
        src = _access()
        for tool in self.FORBIDDEN:
            assert tool not in src, (
                f"Access view references forbidden tool {tool!r} "
                f"(§4.10 / D-rotate §6.4)."
            )

    def test_last_used_never_rendered(self):
        """D-lastused §6.5: last_used is dead data — no column, no label, no
        fabricated timestamp. The token must not reference it at all."""
        src = _access().lower()
        assert "last_used" not in src, (
            "Access view references last_used — it is dead data (D-lastused §6.5) "
            "and must never be read or displayed."
        )
        assert "last used" not in src, (
            "Access view renders a 'last used' label — forbidden by D-lastused §6.5."
        )

    def test_load_is_single_list_call(self):
        """§5.7: the Access view load is exactly one live call
        (admin_list_tokens); space_list is on-demand for the edit modal only."""
        src = _access()
        assert "admin_list_tokens" in src
        # The whoami-derived data (identity) is read from ctx, never re-fetched.
        assert "callTool('system_whoami'" not in src, (
            "Access view must read identity from ctx.identity (cached whoami), "
            "never issue a second system_whoami request (§5.1/§5.7)."
        )


# ═══════════════════════════════════════════════════════════════
# Rotate honesty (D-rotate §6.4): documented, never a control
# ═══════════════════════════════════════════════════════════════


class TestRotateHonesty:
    def test_no_rotate_tool_or_control(self):
        src = _access()
        assert "admin_rotate_token" not in src
        assert "rotate-token" not in src  # no data-action rotate control
        # No data-action whose name implies a rotate operation.
        actions = set(re.findall(r"data-action=\"([a-z-]+)\"", src))
        actions |= set(re.findall(r"registerAction\(\s*'([a-z-]+)'", src))
        assert not any("rotate" in a for a in actions), (
            f"A rotate control is wired: {sorted(a for a in actions if 'rotate' in a)}. "
            "D-rotate §6.4 forbids any rotate control."
        )

    def test_verbose_rotation_guidance_is_not_persistent_page_slop(self):
        src = _access()
        assert "Rotating a token:" not in src
        assert "access-rotate" not in src


# ═══════════════════════════════════════════════════════════════
# Copy & invariant constraints (§8): mono-tenant phrase; forbidden vocab
# ═══════════════════════════════════════════════════════════════


class TestCopyConstraints:
    def test_allowlist_copy_explains_the_user_effect(self):
        assert "Tokens can only access spaces listed in their allowlist." in _access()

    def test_forbidden_non_claims_vocab_absent(self):
        """§8.2: forbidden non-claims substrings must not appear in UI strings."""
        forbidden = [
            "quorum", "hub topology", "permanent master", "leader runtime",
            "crdt", "multi-space merge", "parallel consolidation", "multi-tenant",
        ]
        low = _access().lower()
        hits = [tok for tok in forbidden if tok in low]
        assert not hits, f"Forbidden §8.2 vocabulary present in Access view: {hits}"

    def test_read_only_console_policy_stated(self):
        assert (
            "Read-only tokens cannot use the admin console" in _access()
        ), "§7.1.4: the Access view must state the read-only-console consequence."

    def test_empty_state_copy(self):
        assert "No tokens — the bootstrap key still works" in _access(), (
            "§5.7: the empty-token state uses the mandated bootstrap-key copy."
        )

    def test_bootstrap_session_banner(self):
        # Tolerant of JS string-concatenation seams (`' + '`) in the source.
        assert re.search(
            r"bootstrap key.*does not appear.*in this list",
            _access(), re.IGNORECASE | re.DOTALL,
        ), "§5.7: bootstrap sessions get the 'does not appear in this list' banner."


# ═══════════════════════════════════════════════════════════════
# Permission model (§5.7): four valid chains, no per-tier rights
# ═══════════════════════════════════════════════════════════════


class TestPermissionModel:
    def test_only_valid_permission_chains_offered(self):
        """The create/edit forms offer exactly the four inclusive chains built
        from VALID_PERMISSIONS = {read, write, manage, admin}; no other value."""
        src = _access()
        m = re.search(r"PERMISSION_PRESETS\s*=\s*\[(.*?)\];", src, re.DOTALL)
        assert m, "PERMISSION_PRESETS array not found."
        values = re.findall(r"value:\s*'([a-z,]+)'", m.group(1))
        assert values == [
            "read", "read,write", "read,write,manage", "read,write,manage,admin",
        ], f"Unexpected permission presets: {values}"

    def test_no_per_tier_rights(self):
        """§5.7 / Appendix A: no short/mid/long per-tier rights exist — the view
        must not invent tier-scoped permission controls."""
        src = _access()
        # Guard against a 'permissions' control keyed on tier vocabulary.
        assert not re.search(r"(short|mid|long)[_-]?(read|write|permission)", src, re.IGNORECASE), (
            "Access view appears to render per-tier rights — none exist."
        )


# ═══════════════════════════════════════════════════════════════
# Destructive UX (§7.4): typed purge, non-typed red revoke/delete, internal-long
# ═══════════════════════════════════════════════════════════════


class TestDestructiveUX:
    def test_purge_modes_are_typed(self):
        """§7.4.1: both purge modes require typed challenges via the shell's
        showDestructiveModal (disabled until the exact literal is typed)."""
        src = _access()
        assert "showDestructiveModal(" in src
        assert "typedConfirmation: 'purge revoked'" in src
        assert "typedConfirmation: 'purge all'" in src

    def test_purge_server_flags(self):
        """§7.1.5 / §7.4.1: purge-all sends confirm:true + revoked_only:false;
        purge-revoked sends revoked_only:true. No pre-checked control."""
        src = _access()
        assert re.search(r"revoked_only:\s*false,\s*confirm:\s*true", src), (
            "purge-all must send {revoked_only:false, confirm:true} (LM2-31)."
        )
        assert re.search(r"revoked_only:\s*true", src), (
            "purge-revoked must send {revoked_only:true}."
        )

    def test_purge_all_states_both_consequences(self):
        """§7.4.3: the purge-all modal states BOTH verbatim consequences."""
        src = _access()
        assert "including the" in src and "internal-long" in src and "embedded long runtime" in src
        assert "graph_bridge.py:497" in src, (
            "§7.4.3 consequence #1 (embedded re-bind reference) missing."
        )
        assert "reachable only via the" in src and "bootstrap key" in src, (
            "§7.4.3 consequence #2 (bootstrap-only remainder) missing."
        )

    def test_revoke_delete_are_not_typed(self):
        """§7.4.1 frozen baseline: revoke/delete are custom-modal confirms with
        an explicit red Confirm button but NO typed challenge (the typed-revoke/
        delete proposal, B3.5, is a panel decision left unadopted)."""
        src = _access()
        # The revoke/delete confirm actions exist and are wired to the shell's
        # data-action delegation (the button markup is built dynamically:
        # `class="btn btn-danger" data-action="' + esc(doAction) + '"`).
        assert "'access-revoke-do'" in src
        assert "'access-delete-do'" in src
        assert re.search(r'btn btn-danger" data-action="\'\s*\+\s*esc\(doAction\)', src), (
            "revoke/delete confirm buttons must use the Critical-Red btn-danger "
            "class with a data-action delegation target (§7.4.4)."
        )
        # ... and are NOT wired as typed challenges (frozen §7.4.1 baseline).
        assert "typedConfirmation: 'revoke" not in src
        assert "typedConfirmation: 'delete" not in src

    def test_internal_long_is_hidden_from_list_but_purge_warning_remains(self):
        src = _access()
        assert "internal-long" in src
        load_tokens = _fn_body(src, "loadTokens")
        assert "!isInternalLong(token)" in load_tokens
        assert load_tokens.index("!isInternalLong(token)") < load_tokens.index("cache.tokens = tokens")
        assert "including the <code>internal-long</code>" in src

    def test_space_allowlist_rows_are_capped_with_overflow(self):
        body = _fn_body(_access(), "spacesCell")
        assert "ids.slice(0, 3)" in body
        assert "ids.slice(3)" in body
        assert "hidden.length + ' more</span>'" in body

    def test_all_destructive_calls_go_through_calltool(self):
        """§7.1.7: every destructive action is audited because it goes through
        POST /api/tool (callTool). No direct fetch bypass."""
        src = _access()
        assert "fetch(" not in src, (
            "Access view must not call fetch() directly — all tool calls go "
            "through the shell callTool() (audited /api/tool path)."
        )


# ═══════════════════════════════════════════════════════════════
# One-time secret (§7.1.6): shown once, never persisted/logged/attribute'd
# ═══════════════════════════════════════════════════════════════


class TestOneTimeSecret:
    def test_secret_uses_select_all_block(self):
        assert "mono-block secret" in _access(), (
            "§7.1.6: the raw token is shown in a user-select:all mono block."
        )
        assert ".mono-block.secret" in _css() and "user-select: all" in _css()

    def test_secret_never_persisted_or_logged(self):
        src = _access()
        for sink in ("localStorage", "sessionStorage", "indexedDB", "console.log"):
            assert sink not in src, (
                f"§7.1.6: the Access view must never touch {sink} (token secrecy)."
            )

    def test_raw_token_not_placed_in_attribute(self):
        """§7.1.6: the raw token is held only in the `holder` closure object —
        never in a data-* attribute (the copyable() affordance is NOT used for
        it, since copyable() stores its value in a data-value attribute)."""
        src = _access()
        assert not re.search(r"data-[a-z-]+=\"[^\"]*holder\.value", src)
        assert "copyable(holder.value" not in src
        # Copy goes through the session-aware _copySecret helper (which takes the
        # live holder, see its test), not a raw value written into any attribute.
        assert "_copySecret(holder," in src


# ═══════════════════════════════════════════════════════════════
# Codex R1 (PR #158) findings — regression pins for the fixes
# ═══════════════════════════════════════════════════════════════


class TestCodexR1Fixes:
    def test_created_response_never_dropped_on_navigation(self):
        """Finding 1 (HIGH): a successful `admin_create_token` response must
        surface the one-time secret UNCONDITIONALLY — the epoch stale-drop must
        never run before the `created` check, or navigating away mid-request
        would orphan the only plaintext of the credential (§7.1.6, never-orphan).
        """
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        assert body, "onCreateConfirm not found."
        created_idx = body.index("status === 'created'")
        secret_idx = body.index("showTokenSecret(res)")
        # The error path's ownership bail guards only the non-created branch. It
        # no longer keys on epoch (Terra-R2 f1 — see TestTerraR2Fixes), but the
        # ORDERING invariant is unchanged: created-handling comes first, so a
        # created response is never dropped for a route change (never-orphan).
        stale_drop_idx = body.index("_modalGen !== genAtCall")
        assert created_idx < stale_drop_idx, (
            "onCreateConfirm drops the response as stale before checking "
            "status === 'created' — a created token could be orphaned (finding 1)."
        )
        assert secret_idx < stale_drop_idx, (
            "showTokenSecret must be reachable for a created response regardless "
            "of the stale-drop guard (finding 1)."
        )

    def test_created_secret_guarded_against_session_end_or_change(self):
        """Codex R2+R3 finding 1 (HIGH): a `created` response must not repaint the
        secret if the session ended (logout, overlay visible) OR changed
        (logout+re-login, identity reference changed). The created branch drops
        via _sessionEnded, before showTokenSecret; route changes still show it."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        # Live session identity captured before the create await.
        assert "var sessionAtCall = _sessionIdentity()" in body
        assert body.index("var sessionAtCall") < body.index("await callTool("), (
            "the session identity must be captured BEFORE the create await."
        )
        m = re.search(r"if \(returnedCredential\) \{(.*?)\n        \}", body, re.DOTALL)
        branch = m.group(1)
        assert "_sessionEnded(sessionAtCall)" in branch and "showTokenSecret(res)" in branch, (
            "the created branch must drop via _sessionEnded (overlay OR identity "
            "change) before repainting the secret."
        )
        assert branch.index("_sessionEnded(sessionAtCall)") < branch.index("showTokenSecret(res)"), (
            "the session-end guard must run BEFORE showTokenSecret."
        )

    def test_session_signal_covers_logout_and_relogin(self):
        """_sessionEnded is the ONLY signal for a wipe (route epoch and modal
        generation are unchanged by wipeSession): it must catch both the
        logged-out-now (overlay visible) and logout+re-login (identity reference
        changed) orderings."""
        src = _access()
        m = re.search(r"function _sessionEnded\([^)]*\)\s*\{(.*?)\n    \}", src, re.DOTALL)
        assert m, "_sessionEnded not found."
        body = m.group(1)
        assert "loginOverlay" in body, "_sessionEnded must detect the visible login overlay."
        assert "_ctx().identity !== sessionAtCall" in body, (
            "_sessionEnded must detect an identity-reference change (logout+re-login)."
        )

    def test_all_awaiting_continuations_are_session_aware(self):
        """Codex R4 finding 1 (HIGH): EVERY awaiting modal/mutation continuation
        must be session-bound (capture a live identity and route it through the
        session-aware _isStale / _sessionEnded), so a response that resolves
        while logged out cannot resurrect a modal, toast, or table from the dead
        session (§3.1.4). wipeSession changes neither route epoch nor generation,
        so a bare epoch/generation guard is insufficient."""
        src = _access()
        # Functions that consume a captured session identity in their guard.
        for fn in (
            "loadTokens", "onCreateConfirm", "openEditModal",
            "onEditConfirm", "runMutation", "runPurge",
        ):
            body = _fn_body(src, fn)
            captured = "_sessionIdentity()" in body
            # The captured session identity must flow into a session-aware guard:
            # _isStale(..., session...) for modal continuations, or _sessionEnded
            # directly (loadTokens content-load / create success path).
            used = bool(re.search(r"_isStale\([^)]*session\w*\)", body)) or \
                   bool(re.search(r"_sessionEnded\(session\w*\)", body))
            assert captured and used, (
                f"{fn}() is not session-aware: it must capture _sessionIdentity() "
                f"and route it through _isStale/_sessionEnded (captured={captured}, "
                f"used={used})."
            )

    def test_edit_space_list_await_guarded_by_generation(self):
        """Codex R3 finding 2 (MEDIUM): openEditModal awaits space_list BEFORE
        opening its modal; the post-await guard must check modal generation (via
        _isStale), not only route epoch, or a pending space_list can pop a stale
        edit modal over a newer same-view modal."""
        src = _access()
        body = _fn_body(src, "openEditModal")
        assert "await callTool('space_list'" in body
        # The guard immediately after the space_list await is _isStale (epoch+gen).
        after = body[body.index("await callTool('space_list'"):]
        m = re.search(r"if \((.*?)\) return;", after)
        assert m, "no guard found after the space_list await."
        assert "_isStale(" in m.group(1), (
            "the space_list guard must use _isStale (route epoch AND modal "
            "generation), not a bare epoch check (finding 2 R3)."
        )

    def test_secret_destroyed_on_every_exit_path(self):
        """Finding 2 (HIGH): the plaintext must be zeroed on acknowledge, Cancel,
        and the × close — DOM node emptied AND the closure value neutralized so
        the Copy button can no longer recover it."""
        src = _access()
        m = re.search(r"function showTokenSecret\(res\)\s*\{.*?\n    \}", src, re.DOTALL)
        assert m, "showTokenSecret not found."
        body = m.group(0)
        assert "function destroySecret()" in body
        assert "holder.value = ''" in body, "destroySecret must zero the closure value."
        assert "if (secret) secret.textContent = ''" in body, "destroySecret must empty the DOM node."
        assert re.search(r"if \(btn\) btn\.disabled = true", body), (
            "destroySecret must disable the Copy button so it can't recover the token."
        )
        # ack path calls destroySecret.
        assert re.search(r"async function \(\) \{\s*destroySecret\(\);", body), (
            "the acknowledge handler must call destroySecret()."
        )
        # Cancel/× close controls are wired to destroySecret.
        assert 'querySelectorAll(\'[data-action="close-modal"]\')' in body
        assert "addEventListener('click', destroySecret)" in body, (
            "the Cancel and × controls must run destroySecret on dismissal (finding 2)."
        )

    def test_secret_copy_gated_on_liveness_and_full_staleness(self):
        """Codex R1 f4 + R5 f2 + Terra-R1 f1: the one-time-secret Copy must have a
        non-secure-context execCommand fallback (§2.4.7) AND gate every completion
        effect (toast, fallback textarea) on BOTH the secret still being live
        (holder.value non-empty — destroySecret zeroes it on dismiss) AND full
        staleness (route epoch + modal generation + session). A late Clipboard
        rejection after dismiss/navigation/modal-swap/wipe must therefore rebuild
        neither the toast nor the plaintext textarea."""
        src = _access()
        assert "function _copySecret(holder" in src, (
            "_copySecret must take the live holder, not a captured string, so a "
            "dismissed secret (holder.value zeroed) cannot be rebuilt."
        )
        assert "_copySecret(holder, AdminRouter.epoch, _modalGen, _sessionIdentity())" in src, (
            "the Copy button must pass holder + epoch + generation + session."
        )
        body = _fn_body(src, "_copySecret")
        assert "document.execCommand('copy')" in body, "fallback must use execCommand (§2.4.7)."
        # The completion gate checks liveness AND full staleness.
        assert "!holder.value || _isStale(epochAtCopy, genAtCopy, sessionAtCopy)" in body, (
            "every completion effect must drop if the secret was destroyed OR the "
            "route/modal/session changed (Terra-R1 f1)."
        )
        # The gate (stale()) runs before the fallback textarea is built.
        assert body.index("function stale()") < body.index("createElement('textarea')")
        assert "if (stale()) return;" in body

    def test_pending_create_is_exclusive_non_dismissible(self):
        """Terra-R1 f2: while an admin_create_token request is in flight, the
        Create modal is locked non-dismissible (×/Cancel disabled) so no newer
        modal can open before the response — a `created` response can never
        replace newer UI, while never-orphan is preserved (the secret is shown,
        never dropped). Dismissal is restored on the error path for retry, and on
        the Terra-R3 operator "Stop waiting" escape — but NEVER inside the created
        branch, which transitions straight to the secret step."""
        src = _access()
        assert "function _setModalDismissible(on)" in src
        body = _fn_body(src, "onCreateConfirm")
        # Locked before the await.
        assert "_setModalDismissible(false)" in body
        assert body.index("_setModalDismissible(false)") < body.index("await callTool("), (
            "the modal must be locked BEFORE the create request is awaited."
        )
        # The created branch itself must NOT re-enable dismissal (it hands off to
        # the secret step). Extract just that branch and assert the absence.
        created = re.search(
            r"if \(returnedCredential\) \{(.*?)\n        \}", body, re.DOTALL
        ).group(1)
        assert "_setModalDismissible(true)" not in created, (
            "the created branch must not re-enable dismissal — it transitions to "
            "the secret step, which owns its own modal."
        )
        # Dismissal is re-enabled only where the create flow ends WITHOUT showing
        # a secret: the error path and the operator "Stop waiting" escape.
        assert body.count("_setModalDismissible(true)") == 2, (
            "exactly two re-enable sites (error path + Stop-waiting escape)."
        )


# ═══════════════════════════════════════════════════════════════
# Edit is delta-mode only (§5.7): replacement mode never offered
# ═══════════════════════════════════════════════════════════════


class TestEditDeltaOnly:
    def test_update_uses_delta_keys(self):
        src = _access()
        assert "admin_update_token" in src
        assert "space_ids_add" in src and "space_ids_remove" in src

    def test_no_replacement_space_ids_in_update(self):
        """The replacement mode (a full space_ids list) re-introduces the silent-
        revocation hazard and must not be offered. The edit path sets only the
        delta keys, never args.space_ids."""
        src = _access()
        m = re.search(r"async function onEditConfirm\(.*?\n    \}", src, re.DOTALL)
        assert m, "onEditConfirm not found."
        body = m.group(0)
        assert "space_ids_add" in body and "space_ids_remove" in body
        assert not re.search(r"args\.space_ids\s*=", body), (
            "Edit path assigns a full space_ids replacement — forbidden (§5.7)."
        )


# ═══════════════════════════════════════════════════════════════
# Gating & epoch guard (§5.7 / §3.3.2)
# ═══════════════════════════════════════════════════════════════


class TestGatingAndEpoch:
    def test_writer_and_manager_never_reach_admin_list(self):
        """LM2-11: write is refused; manage gets scoped delegation without ever
        reaching admin_list_tokens (client gate on cached whoami)."""
        src = _access()
        assert "hasManage(identity)" in src
        assert "Requires manage permission" in src
        writer = re.search(r"if \(!hasManage\(identity\)\) \{(.*?)\n        \}", src, re.DOTALL)
        assert writer and "return;" in writer.group(1)
        manager = re.search(r"if \(!hasGlobalAdmin\(identity\)\) \{(.*?)\n        \}", src, re.DOTALL)
        assert manager and "return;" in manager.group(1)
        assert "loadTokens" not in writer.group(1)
        assert "loadTokens" not in manager.group(1)

    def test_every_awaiting_continuation_is_guarded(self):
        """§3.3.2 rule 3 + Codex R2 finding 3: every function that awaits a tool
        call must, before touching the DOM/modal, drop stale continuations —
        either an inline route-epoch check, the combined _isStale() guard (route
        epoch OR modal generation), or, for the create success path, the
        session-wipe (login-overlay) guard that also prevents a post-wipe secret
        repaint. Mutation-proof: removing the guard from ANY awaiting function
        makes that function's assertion go RED (a global count could not).
        """
        src = _access()
        awaiting = [
            fn for fn in re.findall(r"(?:async )?function (\w+)\(", src)
            if "await callTool(" in _fn_body(src, fn)
        ]
        # The Access view's awaiting continuations (guard against silent drift).
        assert set(awaiting) >= {
            "loadTokens", "onCreateConfirm", "openEditModal",
            "onEditConfirm", "runMutation", "runPurge",
        }, f"awaiting-continuation set changed: {sorted(awaiting)}"
        for fn in awaiting:
            body = _fn_body(src, fn)
            guarded = (
                "_isStale(" in body
                or "AdminRouter.epoch !==" in body
                or "loginOverlay" in body  # create success path: session-wipe guard
                or "_sessionEnded(" in body  # create path: gen+session ownership bail
            )
            assert guarded, (
                f"awaiting continuation {fn}() has no stale-drop guard — a stale "
                f"response could paint over a navigated-away view or a newer modal."
            )

    def test_isstale_checks_both_epoch_and_generation(self):
        """Codex R2 finding 3: route epoch alone cannot detect a same-view modal
        swap. _isStale must also compare the modal generation, and every modal
        this view opens must bump it (via the _openModal/_openDestructive
        wrappers), so a stale continuation can only ever close its own modal."""
        src = _access()
        m = re.search(r"function _isStale\([^)]*\)\s*\{(.*?)\n    \}", src, re.DOTALL)
        assert m, "_isStale not found."
        body = m.group(1)
        assert "AdminRouter.epoch !==" in body and "_modalGen !==" in body, (
            "_isStale must compare BOTH the route epoch and the modal generation."
        )
        assert "_sessionEnded(sessionAtCall)" in body, (
            "_isStale must ALSO include the session signal (Codex R4) — wipeSession "
            "changes neither epoch nor generation."
        )
        # No modal is opened by the view except through the generation wrappers,
        # so the counter can never drift from reality.
        assert src.count("_modalGen += 1") == 2, (
            "exactly the two wrappers (_openModal/_openDestructive) may bump "
            "_modalGen."
        )
        direct = re.findall(r"(?<!function )(?<!\w)(showModal|showDestructiveModal)\(", src)
        # Only the two wrapper bodies call the shell primitives directly.
        assert len(direct) == 2, (
            f"the view opens modals outside the generation wrappers: {direct} — "
            "every open must go through _openModal/_openDestructive."
        )

    def test_stale_mutation_does_not_close_a_newer_modal(self):
        """Codex R2 finding 3: the stale branches must NOT call closeModal() or
        return true (which makes the shell close the — possibly newer — modal)."""
        src = _access()
        # runMutation's stale branch drops without closeModal().
        rm = _fn_body(src, "runMutation")
        assert re.search(r"if \(_isStale\([^)]*\)\) return;", rm), (
            "runMutation stale branch must `return;` (no closeModal)."
        )
        # onConfirm-style flows return false (not true) when stale, so the shell
        # never closes a newer modal on their behalf.
        for fn in ("onEditConfirm", "runPurge"):
            body = _fn_body(src, fn)
            assert re.search(r"if \(_isStale\([^)]*\)\) return false;", body), (
                f"{fn} must return false (not true) on a stale continuation."
            )
        # onCreateConfirm's guard is the gen+session OWNERSHIP bail (Terra-R2 f1:
        # epoch is deliberately excluded because the nav-lock's hash-revert churns
        # it). It must also return false — never true, never closeModal — so a
        # stale cross-session continuation cannot close a newer session's modal.
        cc = _fn_body(src, "onCreateConfirm")
        assert re.search(
            r"if \(_modalGen !== genAtCall \|\| _sessionEnded\(sessionAtCall\)\) "
            r"\{ _navLockRelease\(navLock\); return false; \}",
            cc,
        ), "onCreateConfirm ownership bail must release the nav lock and return false."
        assert "closeModal(" not in cc, (
            "onCreateConfirm must never call closeModal on a stale continuation."
        )
        assert "return true" not in cc, (
            "onCreateConfirm must never return true (which would close a newer modal)."
        )

    def test_no_settimeout_sequencing(self):
        """§3.3.2 rule 5: setTimeout as a coordination mechanism is banned."""
        assert "setTimeout" not in _access()


# ═══════════════════════════════════════════════════════════════
# Escaping discipline (§7.3.3) — forbidden sinks in the Access view
# ═══════════════════════════════════════════════════════════════


class TestEscaping:
    def test_no_forbidden_sinks(self):
        src = _access()
        assert "document.write(" not in src
        assert "insertAdjacentHTML(" not in src
        assert "javascript:" not in src
        assert "data:text/html" not in src

    def test_uses_esc_at_interpolation(self):
        src = _access()
        assert "esc(" in src, "The Access view must use esc() at HTML interpolation sites."

    def test_icon_bearing_buttons_have_accessible_name(self):
        """§2.8.2: no icon-only control ships without an accessible name.

        Positive pins nail the current icon-bearing buttons, and a general
        scan makes the invariant mutation-proof: every `<button>` fragment that
        embeds an icon() must also carry a visible <span> text label OR an
        aria-label. A future `'<button ...>' + icon('x') + '</button>'` with
        neither goes RED.
        """
        src = _access()
        # Current icon-bearing buttons pair the glyph with a visible text label.
        assert "icon('plus') + '<span>Create token</span>" in src
        assert "icon('refresh') + '<span>Refresh</span>" in src
        assert "icon('copy') + '<span>Copy plaintext</span>" in src  # ctCopyBtn
        assert "icon('copy') + '<span>Copy Token ID</span>" in src  # ctCopyHashBtn
        # General guard over every button fragment in the source.
        buttons = re.findall(r"<button\b.*?</button>", src, re.DOTALL)
        icon_buttons = [b for b in buttons if "icon(" in b]
        assert icon_buttons, "expected at least one icon-bearing button to scan."
        for b in icon_buttons:
            assert ("<span" in b) or ("aria-label" in b), (
                f"icon-only <button> lacks an accessible name (no <span> label, "
                f"no aria-label): {b[:140]}"
            )


# ═══════════════════════════════════════════════════════════════
# Terra-R2 async-lifecycle fixes (create flow owns modal AND navigation)
# ═══════════════════════════════════════════════════════════════


class TestTerraR2Fixes:
    """Terra (gpt-5.6-terra, high) round 2 — the exclusive create flow owns the
    modal AND navigation. f1: the error path runs the ownership check BEFORE
    re-enabling dismissal (else a stale cross-session failure re-enables a newer
    session's locked modal). f2: browser Back/Forward and address-bar hash edits
    bypassed the modal lock, so a late `created` could surface the secret over a
    different route — a per-request navigation lock pins the route instead."""

    def test_error_path_checks_ownership_before_re_enabling_dismissal(self):
        """f1: the non-created (error) path runs the gen+session ownership check
        BEFORE any modal mutation — a stale cross-session continuation must never
        re-enable a newer session's still-locked modal (×/Cancel)."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        bail_idx = body.index("_modalGen !== genAtCall || _sessionEnded(sessionAtCall)")
        # The ERROR-path re-enable is the LAST _setModalDismissible(true); the
        # earlier one is the Terra-R3 "Stop waiting" escape handler, not the
        # error path, so compare against the last occurrence.
        unlock_idx = body.rindex("_setModalDismissible(true)")
        assert bail_idx < unlock_idx, (
            "the gen+session ownership bail must run BEFORE the error-path "
            "_setModalDismissible(true) (f1)."
        )
        assert re.search(
            r"if \(_modalGen !== genAtCall \|\| _sessionEnded\(sessionAtCall\)\) "
            r"\{ _navLockRelease\(navLock\); return false; \}",
            body,
        ), "the ownership bail must release the nav lock and return false without unlocking."

    def test_error_path_ownership_check_excludes_epoch(self):
        """f1 corollary: the ERROR path's ownership check must NOT key on route
        epoch — the nav-lock hash-revert churns epoch (Back → dispatch → revert →
        dispatch) while the modal remains ours, so keying on epoch would strand a
        still-owned modal. (The abandoned-created path DOES use full staleness,
        but only after the escape releases the nav lock so the route can genuinely
        change there — see TestTerraR3Fixes.)"""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        err_region = body[body.index("// Non-created"):]
        assert "_isStale(" not in err_region, (
            "the error path must not use _isStale (epoch) — gen+session only (f1)."
        )
        assert "AdminRouter.epoch" not in err_region, (
            "the error path must not read route epoch — the nav-lock revert churns it."
        )

    def test_nav_lock_is_an_ownership_token(self):
        """f2: the navigation lock is a per-request OWNERSHIP token, so a stale
        cross-session continuation can only ever release its OWN lock — never a
        newer request's lock (the companion invariant to the f1 ordering fix)."""
        src = _access()
        assert "var _navLock = null;" in src, "module-level nav-lock state missing."
        acquire = _fn_body(src, "_navLockAcquire")
        assert "hash: location.hash" in acquire and "_navLock = lock;" in acquire, (
            "_navLockAcquire must capture the current route into a fresh token."
        )
        assert "return lock;" in acquire, "_navLockAcquire must return the ownership token."
        release = _fn_body(src, "_navLockRelease")
        assert "if (_navLock === lock) _navLock = null;" in release, (
            "_navLockRelease must be OWNER-SCOPED (identity compare) so a stale "
            "continuation cannot release a newer request's lock."
        )

    def test_hashchange_reverts_locked_route(self):
        """f2: while the lock is held and its owning session is current, an
        off-route hash change is reverted to the locked route, pinning the flow's
        context so a late secret cannot surface over a navigated-to route."""
        src = _access()
        m = re.search(
            r"window\.addEventListener\('hashchange', function \(\) \{(.*?)\n    \}\);",
            src, re.DOTALL,
        )
        assert m, "nav-lock hashchange listener not found."
        body = m.group(1)
        assert "location.hash === _navLock.hash" in body, (
            "the listener must no-op when unlocked or already on the locked route."
        )
        assert "location.hash = _navLock.hash;" in body, (
            "the listener must revert an off-route hash change back to the locked route."
        )

    def test_create_acquires_nav_lock_before_await(self):
        """f2: the lock must be taken BEFORE the create request is awaited (in the
        same synchronous run as disabling dismissal), so no navigation can slip in
        between the request starting and the response arriving."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        assert "var navLock = _navLockAcquire();" in body
        assert body.index("var navLock = _navLockAcquire();") < body.index("await callTool("), (
            "the nav lock must be acquired BEFORE the create await."
        )
        # Acquired alongside the modal lock (both before the await).
        assert body.index("var navLock = _navLockAcquire();") < body.index("_setModalDismissible(false)")


# ═══════════════════════════════════════════════════════════════
# Terra-R3 async-lifecycle fixes (lock cannot outlive its session or a hung req)
# ═══════════════════════════════════════════════════════════════


class TestTerraR3Fixes:
    """Terra round 3 — the navigation lock must not survive a session boundary or
    an indefinitely-pending request and trap a later session on the old route.
    (1) The lock is bound to its owning session and self-heals across any wipe.
    (2) A timer-free operator escape recovers a stalled create without orphaning
    a still-deliverable secret."""

    def test_nav_lock_captures_owning_session(self):
        """The lock records the session identity that owns it, so a stale lock
        can be recognized and dropped after a wipe (wipeSession removes the modal
        WITHOUT running the teardown that would otherwise release the lock)."""
        src = _access()
        acquire = _fn_body(src, "_navLockAcquire")
        assert "session: _sessionIdentity()" in acquire, (
            "the lock must capture its owning session so it can self-heal."
        )

    def test_hashchange_self_heals_orphaned_lock(self):
        """After logout / 401-expiry / re-login the lock can be orphaned; the
        listener must DROP it (and NOT revert) once its owning session is no
        longer current, so it never pins a new — or logged-out — session on the
        dead flow's route. The self-heal check precedes the revert."""
        src = _access()
        cur = _fn_body(src, "_navLockSessionCurrent")
        assert "loginOverlay" in cur, (
            "the session-current check must treat a visible login overlay as not-current."
        )
        assert "_sessionIdentity() === lock.session" in cur, (
            "the session-current check must compare the live identity to the captured one."
        )
        m = re.search(
            r"window\.addEventListener\('hashchange', function \(\) \{(.*?)\n    \}\);",
            src, re.DOTALL,
        )
        body = m.group(1)
        assert re.search(
            r"if \(!_navLockSessionCurrent\(_navLock\)\) \{.*?_navLock = null;\s*return;",
            body, re.DOTALL,
        ), "the listener must drop an orphaned lock and let navigation proceed."
        assert body.index("_navLockSessionCurrent") < body.index("location.hash = _navLock.hash;"), (
            "the self-heal check must precede the revert."
        )

    def test_pending_create_has_timer_free_escape(self):
        """An indefinitely pending request must be recoverable WITHOUT a timer
        (setTimeout coordination is banned) and WITHOUT aborting the live promise.
        The 'Stop waiting' escape is revealed only while in flight and releases
        both the nav lock and dismissal so the operator can leave/retry."""
        src = _access()
        assert 'id="ctPendingEscape"' in src and 'id="ctStopWaiting"' in src, (
            "the create modal must include the hidden Stop-waiting escape."
        )
        body = _fn_body(src, "onCreateConfirm")
        assert "escape.hidden = false" in body, "the escape must be revealed while in flight."
        # Assigned (not addEventListener) so a retry-after-error replaces rather
        # than stacks the handler.
        stop = re.search(
            r"stopBtn\.onclick = function \(\) \{(.*?)\n            \};",
            body, re.DOTALL,
        )
        assert stop, "Stop-waiting click handler not found."
        s = stop.group(1)
        assert "abandoned = true" in s and "_navLockRelease(navLock)" in s and "_setModalDismissible(true)" in s, (
            "Stop waiting must set the abandoned flag, release the nav lock, and "
            "re-enable dismissal."
        )
        # No timer coordinates any of this (the module-wide ban still holds).
        assert "setTimeout" not in src

    def test_abandoned_created_delivers_in_context_else_drops(self):
        """The escape must NOT orphan a still-deliverable secret: while abandoned,
        a `created` response is still delivered if the operator is still in the
        exact create context (full staleness clean), and only dropped — pre-warned
        — if they navigated or opened another modal (which would be a Terra-R2 f2
        out-of-context pop)."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        created = re.search(
            r"if \(returnedCredential\) \{(.*?)\n        \}", body, re.DOTALL
        ).group(1)
        assert "if (abandoned)" in created, "the created branch must handle the abandoned case."
        assert "_isStale(epochAtCall, genAtCall, sessionAtCall)" in created, (
            "the abandoned-created path must gate on FULL staleness (epoch+gen+session), "
            "since the escape released the nav lock and the route can now change."
        )
        # In-context (not stale) still reaches the secret step; stale returns first.
        assert created.index("if (abandoned)") < created.index("showTokenSecret(res)")

    def test_abandoned_created_dropped_after_dialog_dismissed(self):
        """Terra-R4: after 'Stop waiting' re-enables ×/Cancel, a dialog dismissal
        only HIDES the modal (closeModal → display:none) without changing epoch or
        _modalGen, so the staleness gate alone would let a late `created` REOPEN
        the secret after explicit dismissal. The abandoned-created path must also
        gate on live modal visibility so any dismissal drops the late secret."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        created = re.search(
            r"if \(returnedCredential\) \{(.*?)\n        \}", body, re.DOTALL
        ).group(1)
        assert re.search(r"style\.display !== 'none'", created), (
            "the abandoned-created path must check live modal visibility (a hidden "
            "dialog means the operator dismissed — drop the late secret)."
        )
        assert re.search(r"if \(!dialogOpen \|\| _isStale\(", created), (
            "delivery must require BOTH the dialog still open AND not stale."
        )

    def test_abandoned_error_drops_silently(self):
        """An abandoned error result drops silently — the operator already has the
        recovery notice, and the modal was re-enabled by the escape."""
        src = _access()
        body = _fn_body(src, "onCreateConfirm")
        err_region = body[body.index("// Non-created"):]
        assert re.search(r"if \(abandoned\) \{ _navLockRelease\(navLock\); return false; \}", err_region), (
            "an abandoned error result must drop silently."
        )

    def test_secret_step_self_pins_and_releases_nav_lock(self):
        """The secret display takes its OWN fresh nav lock (so it is protected
        even when the create's pending lock was released early by the escape) and
        releases it in destroySecret on every teardown path (acknowledge/Cancel/×).
        A session wipe self-heals the lock via its captured session."""
        src = _access()
        assert "function showTokenSecret(res)" in src, (
            "showTokenSecret must self-pin (no handed-in lock parameter)."
        )
        body = _fn_body(src, "showTokenSecret")
        assert "var navLock = _navLockAcquire();" in body, (
            "showTokenSecret must acquire its own nav lock for the secret display."
        )
        assert body.index("var navLock = _navLockAcquire();") < body.index("_openModal("), (
            "the secret's nav lock must be taken before the secret modal opens."
        )
        destroy = re.search(r"function destroySecret\(\) \{(.*?)\n        \}", body, re.DOTALL)
        assert destroy, "destroySecret not found."
        assert "_navLockRelease(navLock);" in destroy.group(1), (
            "destroySecret must release the secret's nav lock on every teardown path."
        )


# ═══════════════════════════════════════════════════════════════
# Executable async-lifecycle regression harness (P8-7 integration proof)
# ═══════════════════════════════════════════════════════════════


class TestP85AsyncLifecycleRuntime:
    """The static pins above fix the SHAPE of the create/secret async guards; the
    Terra R2–R4 reviews GO'd the PR but flagged that no COMMITTED test drives the
    deferred `admin_create_token` promise to a hostile resolution and asserts the
    resulting behaviour. tests/js/admin_access_lifecycle_runtime.mjs is that
    test: a dependency-free node:vm harness that loads the real views-access.js
    with faithful shell-global stubs and exercises the browser-proof scenarios
    A–G (commits 27d559e / 0a2fc0b / 140d054, branch
    claude/p8-5-implementation-3cb723, PR #158) plus H (cross-session created-token
    suppression) and I (async-queued hashchange fidelity), both added per the
    Terra PR #167 review — every assertion black-box, each proven RED when its
    guard in views-access.js is reverted, the fake DOM renders only the IDs the
    modal body actually declares (no phantom nodes), and hashchange is modeled as
    a queued browser task.

    See the module docstring for the pytest-vs-JS-target decision and rationale.
    """

    def test_deferred_promise_lifecycle_invariants_a_through_i(self):
        node = shutil.which("node")
        assert node is not None, (
            "Node.js is required for the Access async-lifecycle harness "
            "(CI installs Node 24 via actions/setup-node@v6)."
        )
        completed = subprocess.run(
            [node, str(_RUNTIME_HARNESS), str(_ACCESS_JS)],
            cwd=_ACCESS_JS.parents[5],  # repo root
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.strip() == "admin access lifecycle runtime: ok"
