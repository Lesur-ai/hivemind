# -*- coding: utf-8 -*-
"""
Per-view source-contract pins for P8-2 (issue #140): Dashboard and Spaces.

DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §4.2 (Dashboard parity), §4.3
(Spaces parity), §5.2/§5.3 (data matrices). Source-inspection style, no
browser/heavy imports — matches the convention in
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
        assert 'title="Runs a live LLM probe"' in content

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


class TestSpacesNoN1AndLongColumn:
    def test_long_column_is_static_dash_with_tooltip(self):
        content = _read(_SPACES)
        assert 'title="Long tier state is shown in Space Detail">—<' in content

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


class TestSpacesAttentionFilterOnDemand:
    """§5.3: bank_stale_spaces is called on filter activation only, never on
    initial render/load."""

    def test_stale_spaces_not_called_from_initial_load(self):
        content = _read(_SPACES)
        load_body = _function_body(content, r"async function _loadTable\(epochAtCall\)")
        assert "bank_stale_spaces" not in load_body
        render_body = _function_body(content, r"function render\(contentEl, params, ctx\)")
        assert "bank_stale_spaces" not in render_body

    def test_stale_spaces_reachable_only_from_query_runner(self):
        content = _read(_SPACES)
        code_lines = [
            line for line in content.splitlines()
            if "bank_stale_spaces" in line and not line.strip().startswith(("*", "//"))
        ]
        assert len(code_lines) == 1, (
            f"bank_stale_spaces should appear in exactly one line of code "
            f"(the callTool(...) site inside _runStaleQuery), found: {code_lines}"
        )
        assert "callTool('bank_stale_spaces'" in code_lines[0]
        runner_body = _function_body(
            content, r"async function _runStaleQuery\(epochAtCall, minNotes, minAgeDays\)"
        )
        assert "bank_stale_spaces" in runner_body

    def test_query_runner_only_invoked_from_action_handlers(self):
        content = _read(_SPACES)
        # Every call site of _runStaleQuery( must be inside a registerAction(...)
        # callback, never inside render()/_loadTable() (checked above) or at
        # module top level.
        render_body = _function_body(content, r"function render\(contentEl, params, ctx\)")
        assert "_runStaleQuery(" not in render_body

    def test_query_runner_is_epoch_guarded(self):
        content = _read(_SPACES)
        body = _function_body(
            content, r"async function _runStaleQuery\(epochAtCall, minNotes, minAgeDays\)"
        )
        assert "AdminRouter.epoch !== epochAtCall" in body, (
            "A slow bank_stale_spaces response must be dropped if the "
            "operator has navigated away before it resolves (epoch guard)."
        )


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
    """Codex GPT-5.5 pre-commit adversarial review (local-only, this branch),
    round 1 NO-GO — findings confirmed and fixed here."""

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

    def test_stale_query_has_a_sequence_guard_against_out_of_order_completion(self):
        """[MEDIUM] Same race as above for the Attention filter: re-applying
        thresholds before an older scan resolves must not let the older,
        superseded scan overwrite the newer one."""
        content = _read(_SPACES)
        assert "let _staleSeq = 0;" in content
        body = _function_body(
            content, r"async function _runStaleQuery\(epochAtCall, minNotes, minAgeDays\)"
        )
        assert "const seq = ++_staleSeq;" in body
        assert "seq !== _staleSeq" in body

    def test_attention_filter_renders_total_and_denied_spaces(self):
        """[MEDIUM] The Attention filter must render bank_stale_spaces'
        total_stale/min_notes/min_age_days/denied_spaces — not just use the
        response to compute row membership and discard the rest (§5.3)."""
        content = _read(_SPACES)
        body = _function_body(content, r"function _staleSummaryHtml\(\)")
        assert "_staleData.total_stale" in body
        assert "_staleData.min_notes" in body
        assert "_staleData.min_age_days" in body
        assert "denied_spaces" in body
        assert "_staleSummaryHtml()" in _read(_SPACES)

    def test_attention_filter_renders_oldest_note_age_per_row(self):
        content = _read(_SPACES)
        assert "oldest_note_age_days" in content
        assert "'Oldest note'" in content

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
        lane_chip_body = _function_body(spaces, r"function _laneChip\(lane\)")
        assert "BEST_EFFORT_TOOLTIP" in lane_chip_body

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
        assert "shortCount ?? '—'" in spaces
        assert "space.bank_files_count ?? '—'" in spaces

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
    """Codex GPT-5.5 pre-commit adversarial review round 2 (local-only) —
    findings confirmed and fixed here."""

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
        assert refresh_action and "btn.disabled = true" in refresh_action.group(1)

    def test_attention_short_column_prefers_stale_scans_own_count(self):
        """[MEDIUM] The Attention scan's is_stale/age fields were derived
        from bank_stale_spaces' own live_notes_count at scan time; the Short
        column must show that count (not a possibly-stale space_list count)
        when the scan has an entry for the row."""
        content = _read(_SPACES)
        body = _function_body(content, r"function _tableRowsHtml\(rows, staleById\)")
        assert "stale.live_notes_count" in body
        assert "shortCount" in body

    def test_job_id_chip_has_full_value_tooltip_and_contract_truncation(self):
        """[LOW] copyable() itself emits no title attribute; the contract's
        truncation rule (§2.4.7) requires truncateMiddle(value, 10, 6) with
        the full value in a title tooltip."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _recentActivityBody\(resp\)")
        assert 'title="${esc(job.job_id)}"' in body
        assert "truncateMiddle(job.job_id, 10, 6)" in body


class TestPrLevelReviewFixes:
    """Codex GPT-5.5 PR-level adversarial review of PR #160 (published,
    NO-GO): 6 MEDIUM + 1 LOW findings, confirmed and fixed here."""

    def test_f1_consolidating_filter_degrades_when_lanes_unavailable(self):
        """[MEDIUM] A failed bank_consolidation_queues refresh sets
        _lanesById=null; if the operator was on Consolidating, every row was
        filtered out. Rows must stay usable: _computeRows returns all spaces
        when lanes are null, and _loadTable resets the active filter to All."""
        content = _read(_SPACES)
        compute_body = _function_body(content, r"function _computeRows\(\)")
        assert "if (_lanesById === null) return spaces;" in compute_body
        load_body = _function_body(content, r"async function _loadTable\(epochAtCall\)")
        assert "_activeFilter === 'consolidating'" in load_body
        assert "_activeFilter = 'all';" in load_body

    def test_f2_stale_create_space_does_not_close_a_newer_modal(self):
        """[MEDIUM] The epoch-mismatch path returned true, and the shared
        confirm handler calls closeModal() on any truthy result — which, with
        one global modal, closes whatever modal is now open. Must return
        false (drop silently) on epoch mismatch."""
        content = _read(_SPACES)
        body = _function_body(content, r"async function _submitCreateSpace\(\)")
        assert "if (AdminRouter.epoch !== epochAtSubmit) return false;" in body
        assert "if (AdminRouter.epoch !== epochAtSubmit) return true;" not in body

    def test_f3_attention_rescans_on_every_activation(self):
        """[MEDIUM] bank_stale_spaces ran only while _staleData===null, so a
        second activation showed a cached scan. §5.3 names filter activation
        as a refresh trigger — re-scan on every transition into Attention."""
        content = _read(_SPACES)
        handler = re.search(
            r"registerAction\('spaces-filter-tab', \(data\) => \{(.*?)\n    \}\);",
            content, re.DOTALL,
        )
        assert handler, "spaces-filter-tab handler not found"
        body = handler.group(1)
        assert "if (filter === 'attention') {" in body
        assert "_staleData === null" not in body

    def test_f4_identity_card_renders_expiry_chip(self):
        """[MEDIUM] §5.1/§5.2: the identity card consumes the same fields as
        the sidebar, including the conditional expires_at chip."""
        content = _read(_DASHBOARD)
        body = _function_body(content, r"function _identityCardBody\(identity\)")
        assert "identity.expires_at" in body
        assert "fmtTimestamp(identity.expires_at)" in body

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

    def test_f7_filter_toggles_expose_pressed_state(self):
        """[LOW] The filter group declared role=tablist but its buttons had
        no role=tab / aria-selected. Replaced with a labeled group of
        aria-pressed toggle buttons (a complete pattern)."""
        content = _read(_SPACES)
        assert 'role="group"' in content
        assert 'aria-label="Filter spaces"' in content
        assert 'aria-pressed="${active ? \'true\' : \'false\'}"' in content
        assert 'role="tablist"' not in content

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
