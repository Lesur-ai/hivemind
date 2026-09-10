# -*- coding: utf-8 -*-
"""
Per-view source-contract pins for P8-2 (issue #140): Dashboard and Spaces.

Dashboard and Spaces parity, including data availability and refresh races.
Source-inspection style, no browser/heavy imports — matches the convention in
tests/test_admin_console_security.py (§7.2.1 of the contract names this
file explicitly as the P8-2 per-view pin file).

views-dashboard.js and views-spaces.js are real views now, not
honest-placeholder stubs. The shared forbidden-sink / emoji guards in
tests/test_admin_console_security.py still cover them (they iterate
_VIEW_MODULE_FILES, which keeps every module; the stub-only assertions
skip any file that no longer declares itself a "— stub"). This file adds
the P8-2 data-honesty / behavioral / accessibility pins on top of that.
"""

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).parent.parent / "src" / "live_mem" / "static"
_DASHBOARD = _STATIC_DIR / "js" / "admin" / "views-dashboard.js"
_SPACES = _STATIC_DIR / "js" / "admin" / "views-spaces.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_body(content: str, signature_re: str) -> str:
    """Extract a top-level `function name(...) { ... }` body (brace-balance
    naive: relies on the closing brace being at column 4, matching this
    codebase's 4-space-indented function style, same technique already used
    by tests/test_admin_console_security.py)."""
    match = re.search(signature_re + r"\s*\{(.*?)\n    \}", content, re.DOTALL)
    assert match, f"pattern not found: {signature_re}"
    return match.group(1)


class TestRegistration:
    def test_dashboard_registers(self):
        content = _read(_DASHBOARD)
        assert "AdminViews.register('dashboard'" in content

    def test_spaces_registers(self):
        content = _read(_SPACES)
        assert "AdminViews.register('spaces'" in content


class TestDashboardRequestBudget:
    """§5.2: route entry = 3 `/api/tool` calls for non-admin, 4 for admin
    (system_health, space_list, bank_consolidation_queues [+ admin_list_tokens]).
    Identity is read from the shell-cached ctx.identity — zero extra request."""

    def test_system_whoami_never_called_directly(self):
        content = _read(_DASHBOARD)
        assert "callTool('system_whoami'" not in content, (
            "Dashboard must read _ctx().identity (shell-cached), never call "
            "system_whoami itself (§4.2 D7 / §5.2)."
        )

    def test_admin_list_tokens_gated_on_admin_permission(self):
        content = _read(_DASHBOARD)
        body = _function_body(content, r"async function _loadRest\(epochAtCall, identity\)")
        assert re.search(
            r"if \(admin\) \{\s*calls\.push\(callTool\('admin_list_tokens'", body,
        ), (
            "admin_list_tokens must be called only inside an `if (admin)` "
            "branch of the route-entry loader — never unconditionally."
        )
        # And the non-admin path must issue exactly the other two calls.
        assert body.count("callTool(") == 3, (
            "_loadRest must call exactly 2 unconditional tools (space_list, "
            "bank_consolidation_queues) plus 1 admin-gated one (admin_list_tokens)."
        )

    def test_no_polling(self):
        content = _read(_DASHBOARD)
        assert "setInterval(" not in content
        assert "setTimeout(" not in content

    def test_health_refresh_disables_button_while_in_flight(self):
        content = _read(_DASHBOARD)
        setter_body = _function_body(content, r"function _setHealthRefreshButton\(inFlight\)")
        assert "btn.disabled = inFlight;" in setter_body
        load_body = _function_body(content, r"async function _loadHealth\(epochAtCall\)")
        assert "_setHealthRefreshButton(true)" in load_body
        assert "_setHealthRefreshButton(false)" in load_body

    def test_health_refresh_labeled_as_llm_probe(self):
        content = _read(_DASHBOARD)
        assert 'title="Checks storage and sends a request to the language model"' in content

    def test_health_load_and_refresh_are_epoch_guarded(self):
        content = _read(_DASHBOARD)
        load_body = _function_body(content, r"async function _loadHealth\(epochAtCall\)")
        assert "AdminRouter.epoch !== epochAtCall" in load_body
        rest_body = _function_body(content, r"async function _loadRest\(epochAtCall, identity\)")
        assert "AdminRouter.epoch !== epochAtCall" in rest_body


class TestDashboardRecentActivity:
    def test_activity_bounded_to_top_10_from_queues_response_only(self):
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _recentActivityBody\(resp\)")
        assert "jobs.slice(0, 10)" in body
        assert "callTool(" not in body, (
            "Recent activity must be derived client-side from the already-"
            "fetched bank_consolidation_queues response — zero extra call."
        )

    def test_activity_row_links_to_space_detail(self):
        content = _read(_DASHBOARD)
        assert "'#/spaces/' + encodeURIComponent(job.space_id)" in content


class TestSpacesInventory:
    def test_three_columns_prioritize_full_names_and_available_memory(self):
        content = _read(_SPACES)
        assert "dataTable(['Space', 'Memory', 'Consolidation']" in content
        name = _function_body(content, r"function _idCellHtml\(id\)")
        assert "${esc(id)}</a>" in name
        assert "truncateMiddle" not in name
        assert "mono-data spaces-id-link" not in name

    def test_load_table_makes_no_per_row_calls(self):
        content = _read(_SPACES)
        body = _function_body(content, r"async function _loadTable\(epochAtCall\)")
        assert "space_info" not in body
        assert "graph_status" not in body
        assert body.count("callTool(") == 2, (
            "Spaces route-entry load must be exactly one space_list call + "
            "one bank_consolidation_queues call — no per-row N+1."
        )

    def test_row_navigation_is_a_plain_anchor(self):
        """§3.3.2 rule 1: pure navigation must be a real anchor, no
        data-action, no AdminRouter.go() indirection."""
        content = _read(_SPACES)
        body = _function_body(content, r"function _idCellHtml\(id\)")
        assert re.search(r"<a href=\"\$\{esc\(href\)\}\"", body)
        assert "AdminRouter.go(" not in body
        assert "data-action=\"spaces-open-detail\"" not in content
        # The only mentions of AdminRouter.go( in this file are in the
        # header doc-comment explaining why it is NOT used for row nav.
        code_lines = [
            line for line in content.splitlines()
            if "AdminRouter.go(" in line and not line.strip().startswith("*")
        ]
        assert not code_lines, f"AdminRouter.go( used outside comments: {code_lines}"


class TestDashboardSpaceCreateGate:
    def test_empty_dashboard_cta_requires_manage(self):
        content = _read(_DASHBOARD)
        tile = _function_body(content, r"function _spacesTileBody\(resp, canManage\)")
        assert "actionHtml: canManage" in tile
        loader = _function_body(content, r"async function _loadRest\(epochAtCall, identity\)")
        assert "_applySpaces(spacesResp, _hasManage(identity))" in loader


class TestSpacesInventoryRequests:
    """§5.3 amendment: stale diagnostics live only in Consolidation."""

    def test_stale_scan_removed_from_spaces(self):
        content = _read(_SPACES)
        assert "bank_stale_spaces" not in content
        assert "spaces-filter-tab" not in content
        assert 'label for="spacesSearch"' in content

    def test_stale_spaces_not_called_from_initial_load(self):
        content = _read(_SPACES)
        load_body = _function_body(content, r"async function _loadTable\(epochAtCall\)")
        assert "bank_stale_spaces" not in load_body
        render_body = _function_body(content, r"function render\(contentEl, params, ctx\)")
        assert "bank_stale_spaces" not in render_body


class TestSpacesCreateForm:
    def test_space_id_regex_mirrors_server(self):
        content = _read(_SPACES)
        assert "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$" in content
        body = _function_body(content, r"async function _submitCreateSpace\(\)")
        assert "SPACE_ID_RE.test(spaceId)" in body

    def test_mono_tenant_help_text_present(self):
        content = _read(_SPACES)
        assert "space allowlist, not a tenant boundary" in content

    def test_owner_datalist_gated_on_admin_permission(self):
        content = _read(_SPACES)
        body = _function_body(
            content, r"async function _populateOwnerDatalist\(identity, epochAtOpen\)"
        )
        first_statement = body.strip().splitlines()[0].strip()
        assert first_statement == "if (!_isAdmin(identity)) return;", (
            "Owner datalist population must bail out before any tool call "
            "for a non-admin identity — never probe forbidden data."
        )
        assert "admin_list_tokens" not in first_statement

    def test_already_exists_renders_inline_not_toast(self):
        content = _read(_SPACES)
        body = _function_body(content, r"async function _submitCreateSpace\(\)")
        assert "already_exists" in body
        assert "csSpaceIdError" in body


class TestNoMockData:
    """No hardcoded numeric/string literal stands in for a tool response
    field (spot-checked keys: total, spaces_count, queued_jobs)."""

    FABRICATED_LITERAL_RE = re.compile(r"""["']?(total|spaces_count|queued_jobs)["']?\s*:\s*-?[0-9]""")

    def test_dashboard_has_no_fabricated_literals(self):
        content = _read(_DASHBOARD)
        assert not self.FABRICATED_LITERAL_RE.search(content)

    def test_spaces_has_no_fabricated_literals(self):
        content = _read(_SPACES)
        assert not self.FABRICATED_LITERAL_RE.search(content)


class TestForbiddenSinks:
    """Mirrors TestP81ForbiddenSinks (test_admin_console_security.py) for
    the two files that left _VIEW_STUB_FILES in this change."""

    def _files(self):
        return {"views-dashboard.js": _read(_DASHBOARD), "views-spaces.js": _read(_SPACES)}

    def test_no_document_write(self):
        for name, content in self._files().items():
            assert "document.write(" not in content, name

    def test_no_insertadjacenthtml(self):
        for name, content in self._files().items():
            assert "insertAdjacentHTML(" not in content, name

    def test_no_javascript_or_data_html_urls(self):
        for name, content in self._files().items():
            assert "javascript:" not in content, name
            assert "data:text/html" not in content, name


class TestEmojiGuard:
    """Mirrors TestP81EmojiGuard for the two files that left
    _VIEW_STUB_FILES in this change."""

    EMOJI_RE = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "☀-➿"
        "]"
    )
    FORBIDDEN_LITERALS = ("✕", "⚠", "❌")  # visual X, warning, cross mark

    def test_no_emoji_in_new_view_files(self):
        for path in (_DASHBOARD, _SPACES):
            text = path.read_text(encoding="utf-8")
            hits = self.EMOJI_RE.findall(text)
            assert not hits, f"{path} contains emoji/pictographic code points: {hits}"
            for lit in self.FORBIDDEN_LITERALS:
                assert lit not in text, f"{path} contains forbidden literal {lit!r}"


class TestPreCommitReviewFixes:
    """Overlapping loads and stale-session results must not replace current UI.
    These tests pin ordering, identity and rendering boundaries."""

    def test_health_load_has_a_sequence_guard_against_out_of_order_completion(self):
        """[MEDIUM] Two overlapping system_health calls issued in the SAME
        epoch (e.g. route-entry load racing a fast manual-refresh click)
        must resolve deterministically — only the most recently *issued*
        call may ever apply its result, even if an older call happens to
        resolve last."""
        content = _read(_DASHBOARD)
        assert "let _healthSeq = 0;" in content
        body = _function_body(content, r"async function _loadHealth\(epochAtCall\)")
        assert "const seq = ++_healthSeq;" in body
        assert "seq !== _healthSeq" in body


    def test_guarantee_badge_uses_verbatim_value_and_tooltip(self):
        """[MEDIUM] §5(d): `guarantee: "in_memory_best_effort"` is surfaced
        verbatim (not paraphrased as "best-effort") with the mandated
        tooltip, on Dashboard activity and on Spaces lane chips."""
        dash = _read(_DASHBOARD)
        assert "in_memory_best_effort" in dash
        assert "does not survive a restart and history is trimmed" in dash
        assert "'best-effort'" not in dash
        spaces = _read(_SPACES)
        assert "does not survive a restart and history is trimmed" in spaces
        rows = _function_body(spaces, r"function _tableRowsHtml\(rows\)")
        assert "BEST_EFFORT_TOOLTIP" in rows

    def test_activity_row_includes_job_id(self):
        """[MEDIUM] §5.2 names job_id as a consumed field of the activity
        widget; it must be present (copyable), not silently dropped."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _recentActivityBody\(resp\)")
        assert "job.job_id" in body

    def test_missing_count_fields_render_dash_not_zero(self):
        """[MEDIUM] §2.7/§5.0: never render unknown/missing data as a fake
        0 — an em dash (or unavailable state) only."""
        dash = _read(_DASHBOARD)
        assert "resp.total_spaces ?? '—'" in dash
        assert "resp.total_spaces ?? 0" not in dash
        spaces = _read(_SPACES)
        assert "_number(space.live_notes_count) ?? '—'" in spaces
        assert "_number(space.bank_files_count) ?? '—'" in spaces

    def test_changelog_records_the_dashboard_and_spaces_release(self):
        """The released public history must retain this user-facing change."""
        changelog = (Path(__file__).parent.parent / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )
        current_line = changelog.split("## Inherited Live Memory history", 1)[0]
        assert "Dashboard and Spaces views now show real data." in current_line

    def test_activity_href_escaped_at_the_html_sink(self):
        """[LOW] §7.3.3 R1/R2: every dynamic value interpolated into an
        attribute must pass through esc() at the sink, even when a URI
        encoder already neutralizes breakout characters."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _recentActivityBody\(resp\)")
        assert "esc('#/spaces/' + encodeURIComponent(job.space_id))" in body

    def test_spaces_has_a_manual_refresh_trigger(self):
        """[LOW] §5.3 lists "load + manual" as the Spaces table's refresh
        triggers; the view must expose an explicit manual refresh action."""
        content = _read(_SPACES)
        assert 'data-action="spaces-refresh"' in content
        render_body = _function_body(content, r"function render\(contentEl, params, ctx\)")
        assert 'data-action="spaces-refresh"' in render_body

    def test_token_message_shown_via_server_message_slot_not_toast(self):
        """[LOW] §5.3: space_create's conditional token_message is shown
        verbatim in the server-message slot, not a plain toast."""
        content = _read(_SPACES)
        body = _function_body(content, r"async function _submitCreateSpace\(\)")
        assert "serverMessage(resp.token_message)" in body
        assert "showToast('ok', resp.token_message)" not in body


class TestPreCommitReviewRound2Fixes:
    """REST loads and space-detail actions keep their own sequence guards.
    Older continuations must not overwrite newer results."""

    def test_dashboard_rest_load_has_a_sequence_guard(self):
        """[MEDIUM] _loadRest (space_list/queues/tokens) had only an epoch
        guard: a route-entry load racing a fast manual-refresh click shares
        the same epoch and could resolve out of order."""
        content = _read(_DASHBOARD)
        assert "let _restSeq = 0;" in content
        body = _function_body(content, r"async function _loadRest\(epochAtCall, identity\)")
        assert "const seq = ++_restSeq;" in body
        assert "seq !== _restSeq" in body

    def test_spaces_table_load_has_a_sequence_guard(self):
        """[MEDIUM] Same race as above for _loadTable, plus the Refresh
        button did not disable itself while a load was in flight."""
        content = _read(_SPACES)
        assert "let _tableSeq = 0;" in content
        body = _function_body(content, r"async function _loadTable\(epochAtCall\)")
        assert "const seq = ++_tableSeq;" in body
        assert "seq !== _tableSeq" in body
        assert 'id="spacesRefreshBtn"' in content
        refresh_action = re.search(
            r"registerAction\('spaces-refresh', \(\) => \{(.*?)\n    \}\);", content, re.DOTALL,
        )
        assert refresh_action and "_loadTable(AdminRouter.epoch)" in refresh_action.group(1)
        assert "_setRefreshing(true)" in body
        assert "if (_pending || !_current(epochAtCall)) return;" in body


    def test_job_id_chip_has_full_value_tooltip_and_contract_truncation(self):
        """[LOW] copyable() itself emits no title attribute; the contract's
        truncation rule (§2.4.7) requires truncateMiddle(value, 10, 6) with
        the full value in a title tooltip."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _recentActivityBody\(resp\)")
        assert 'title="${esc(job.job_id)}"' in body
        assert "truncateMiddle(job.job_id, 10, 6)" in body


class TestPrLevelReviewFixes:
    """Unavailable data and failed requests retain usable, honest UI states.
    These tests cover filtering, errors and guarded rendering."""


    def test_f2_stale_create_space_does_not_close_a_newer_modal(self):
        """[MEDIUM] The epoch-mismatch path returned true, and the shared
        confirm handler calls closeModal() on any truthy result — which, with
        one global modal, closes whatever modal is now open. Must return
        false (drop silently) on epoch mismatch."""
        content = _read(_SPACES)
        body = _function_body(content, r"async function _submitCreateSpace\(\)")
        assert "if (AdminRouter.epoch !== epochAtSubmit) return false;" in body
        assert "if (AdminRouter.epoch !== epochAtSubmit) return true;" not in body


    def test_identity_expiry_remains_in_sidebar_without_duplicate_card(self):
        """Identity, including expiry, stays available in the shared shell."""
        dashboard = _read(_DASHBOARD)
        assert 'dashIdentityCard' not in dashboard
        shell = _read(_STATIC_DIR / "js/admin-app.js")
        body = re.search(r"function renderIdentityBlock\(identity\) \{(.*?)\n\}", shell, re.DOTALL)
        assert body
        assert "identity.expires_at" in body.group(1)
        assert "fmtTimestamp(identity.expires_at)" in body.group(1)

    def test_f5_spaces_tile_shows_short_and_mid_aggregates(self):
        """[MEDIUM] §5.2/#140 plan: the Spaces tile shows total plus the
        client-side sums of live_notes_count / bank_files_count, with honest
        unavailable handling (never a fabricated 0)."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _spacesTileBody\(resp, canManage\)")
        assert "live_notes_count" in body
        assert "bank_files_count" in body
        assert "shortSum ?? '—'" in body
        assert "midSum ?? '—'" in body


    def test_f7_space_id_error_wired_via_aria_describedby(self):
        """[LOW] The Space ID input's validation error must be programmatically
        associated with the field (aria-describedby) and announced (role=alert)."""
        content = _read(_SPACES)
        assert 'aria-describedby="csSpaceIdHint csSpaceIdError"' in content
        assert 'id="csSpaceIdHint"' in content
        assert 'id="csSpaceIdError" role="alert"' in content

    def test_f7_health_card_has_no_nested_interactive_controls(self):
        """[LOW] The health card was a <button> whose error state rendered
        stateError's Retry <button> inside it (invalid nested interactive
        controls). The card is now a <div>; only the success state carries a
        single full-bleed <button>, and the error state's Retry stands alone."""
        content = _read(_DASHBOARD)
        # The card container is a div, not a button.
        assert '<div class="metric-card" id="dashHealthCard">' in content
        assert 'class="metric-card" id="dashHealthCard" data-action=' not in content
        # Success body is a single button; error body has no wrapping button.
        success = _function_body(content, r"function _healthCardBody\(health\)")
        assert 'class="dash-card-btn" data-action="dash-open-health"' in success
        error = _function_body(content, r"function _healthCardError\(resp\)")
        assert "<button" not in error  # only stateError's own Retry, rendered by the shell helper


class TestForbiddenVocabulary:
    """§8.2: forbidden non-claims tokens must never appear in UI strings."""

    FORBIDDEN = ("quorum", "hub topology", "permanent master", "leader runtime",
                 "CRDT", "multi-space merge", "parallel consolidation", "multi-tenant")

    def test_dashboard_and_spaces_avoid_forbidden_tokens(self):
        for path in (_DASHBOARD, _SPACES):
            text = path.read_text(encoding="utf-8").lower()
            for token in self.FORBIDDEN:
                assert token.lower() not in text, f"{path} contains forbidden token {token!r}"
