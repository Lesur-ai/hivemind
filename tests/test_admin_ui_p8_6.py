"""Frontend contract pins for P8-6's honest audit view.

Most tests inspect the dependency-free browser module as text. A focused Node
VM harness exercises refresh/filter state transitions with a minimal fake DOM,
without adding a production JavaScript dependency or browser persistence.
"""

from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).parent.parent
VIEW_PATH = ROOT / "src/live_mem/static/js/admin/views-audit.js"
CSS_PATH = ROOT / "src/live_mem/static/css/admin.css"
SECURITY_TEST_PATH = ROOT / "tests/test_admin_console_security.py"
RUNTIME_HARNESS_PATH = ROOT / "tests/js/admin_audit_state_runtime.mjs"


def _view() -> str:
    return VIEW_PATH.read_text(encoding="utf-8")


def _audit_css() -> str:
    css = CSS_PATH.read_text(encoding="utf-8")
    start = css.index("/* ===== view:audit (P8-6) ===== */")
    end = css.index("/* ===== view:access (P8-5) ===== */", start)
    return css[start:end]


class TestP86AuditScope:
    def test_audit_view_registers_and_scope_banner_is_first(self):
        source = _view()
        assert "AdminViews.register('audit', render);" in source
        template = re.search(
            r'contentEl\.innerHTML = `<div class="page audit-view">(.*?)</div>`;',
            source,
            re.DOTALL,
        )
        assert template, "audit root template not found"
        assert template.group(1).lstrip().startswith("${_scopeBanner()}")

    def test_scope_copy_is_static_visible_and_complete(self):
        source = _view()
        assert "In-memory audit scope" in source
        assert (
            "This instance, since restart — console and auth events only, "
            "best-effort."
        ) in source
        assert (
            "MCP (<code>/mcp</code>) tool calls are not individually audited here."
        ) in source
        assert (
            "Argument values, including space identifiers, are deliberately not "
            "stored; labels may be clipped or redacted."
        ) in source
        assert 'class="audit-scope-banner"' in source

    def test_css_is_confined_to_the_audit_banner_region(self):
        css = _audit_css()
        assert ".audit-view > .audit-scope-banner:first-child" in css
        assert ".audit-filters" in css
        assert ".audit-key-chip--overflow" in css
        assert "@media (max-width: 820px)" in css


class TestP86IdentityAndRequests:
    def test_identity_states_are_distinct_and_admin_gated(self):
        source = _view()
        assert "!identity.client_name" in source
        assert "permissions.includes('admin')" in source
        assert "stateUnavailable('Identity unavailable.')" in source
        assert "stateUnavailable('Requires admin permission')" in source

        missing = source.index("if (identityKind === 'missing')")
        non_admin = source.index("if (identityKind === 'non-admin')")
        initial_load = source.rindex("_load(root);")
        assert missing < non_admin < initial_load

    def test_one_tool_call_site_uses_the_fixed_full_ring_limit(self):
        source = _view()
        assert source.count("callTool('admin_audit_recent'") == 1
        assert "callTool('admin_audit_recent', { limit: 500 })" in source
        # One call from the manual click handler and one from initial admin
        # render. Filters only re-render the WeakMap-held payload.
        assert source.count("_load(root);") == 2

    def test_refresh_is_manual_only(self):
        source = _view()
        assert 'data-audit-action="refresh"' in source
        assert "root.addEventListener('click'" in source
        for polling_primitive in (
            "setInterval(",
            "setTimeout(",
            "requestAnimationFrame(",
        ):
            assert polling_primitive not in source


class TestP86LifecycleAndDataRetention:
    def test_payload_state_is_weakly_keyed_to_the_current_root(self):
        source = _view()
        assert "const viewState = new WeakMap();" in source
        assert "viewState.set(root, state);" in source
        assert source.count("viewState.get(root)") >= 2
        for persistent_store in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
        ):
            assert persistent_store not in source

    def test_success_and_error_paths_drop_stale_or_detached_roots(self):
        source = _view()
        guard = (
            "if (!root.isConnected || state.epoch !== AdminRouter.epoch) return;"
        )
        assert source.count(guard) == 2
        assert "epoch: ctx ? ctx.epoch : AdminRouter.epoch" in source

    def test_refresh_phase_and_failure_reset_are_mutation_pinned(self):
        source = _view()
        assert "phase: 'idle'" in source
        assert "state.phase = 'loading';" in source
        assert "state.phase = 'loaded';" in source
        assert "state.phase = 'error';" in source
        assert "if (state.phase !== 'loaded') return;" in source
        assert "function _clearPayload(state)" in source
        assert source.count("_finishFailedLoad(root, state);") == 4

    def test_failed_and_pending_refreshes_cannot_repaint_stale_payload(self):
        node = shutil.which("node")
        assert node is not None, "Node.js is required for the audit state harness"
        completed = subprocess.run(
            [node, str(RUNTIME_HARNESS_PATH), str(VIEW_PATH)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.strip() == "admin audit state runtime: ok"


class TestP86HonestFieldsAndFilters:
    def test_table_has_only_the_six_contract_columns(self):
        source = _view()
        assert (
            "['Time', 'Event', 'Requested tool', 'Argument keys', "
            "'Client', 'Auth type']"
        ) in source
        for field in (
            "entry.ts",
            "entry.event",
            "entry.tool",
            "entry.arguments_keys",
            "entry.client",
            "entry.auth_type",
        ):
            assert field in source
        assert "outcome" not in source.lower()

    def test_argument_keys_are_rendered_without_argument_values(self):
        source = _view()
        assert "_argumentKeyChips(entry.arguments_keys)" in source
        assert "audit-key-chip--overflow" in source
        assert "_isOverflowMarker(value)" in source
        assert "value.startsWith('+')" in source
        assert "value.endsWith(' more')" in source
        assert not re.search(r"entry\.arguments(?!_keys)", source)

    def test_only_real_event_options_and_literal_text_filters_are_present(self):
        source = _view()
        option_values = re.findall(r'<option value="([^"]*)">', source)
        assert option_values == [
            "",
            "admin_tool_call",
            "login_success",
            "login_failed",
            "auth_rejected",
        ]
        assert 'data-audit-filter="tool"' in source
        assert 'placeholder="Literal tool text"' in source
        assert 'data-audit-filter="client"' in source
        assert 'placeholder="Literal client text"' in source
        assert ".includes(String(query).toLowerCase())" in source
        assert "RegExp(" not in source

    def test_unsupported_controls_and_invented_event_options_are_absent(self):
        source = _view()
        assert "Export" not in source
        assert "download" not in source.lower()
        for unsupported in (
            "commit",
            "consolidate",
            "rollback",
            "tombstone",
            "recovery",
        ):
            assert f'<option value="{unsupported}"' not in source


class TestP86StatesAndEscaping:
    def test_sentinel_error_empty_and_filtered_empty_states_are_explicit(self):
        source = _view()
        for sentinel in ("read_only", "rate_limited", "truncated"):
            assert f"'{sentinel}'" in source
        assert "stateError({" in source
        assert "No events recorded since last restart" in source
        assert "No events match these filters" in source

    def test_scope_envelope_and_all_six_api_fields_use_escaping_helpers(self):
        source = _view()
        assert "esc(String(state.total))" in source
        assert "esc(String(state.capacity))" in source
        assert "esc(String(state.scopeNote))" in source
        assert "renderTimestamp(entry.ts)" in source
        assert "_eventPill(entry.event)" in source
        assert "_nullableMono(entry.tool" in source
        assert "_argumentKeyChips(entry.arguments_keys)" in source
        assert "_nullableMono(entry.client" in source
        assert "_nullableMono(entry.auth_type" in source
        assert "${esc(String(value))}" in source
        assert "${esc(value)}" in source

    def test_forbidden_html_sinks_are_absent(self):
        source = _view()
        assert "document.write(" not in source
        assert "insertAdjacentHTML(" not in source
        assert "javascript:" not in source
        assert "data:text/html" not in source


class TestP86StubLifecycleRefactor:
    def test_global_guards_keep_all_modules_but_stub_checks_exclude_audit(self):
        source = SECURITY_TEST_PATH.read_text(encoding="utf-8")
        assert "_VIEW_MODULE_FILES = [" in source
        assert '"js/admin/views-audit.js",' in source
        assert (
            'path for path in _VIEW_MODULE_FILES if path != '
            '"js/admin/views-audit.js"'
        ) in source
        assert "files = [_ADMIN_APP_JS] + _VIEW_MODULE_FILES" in source
        assert "for f in _VIEW_MODULE_FILES" in source
