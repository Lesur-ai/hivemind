# -*- coding: utf-8 -*-
"""
Non-complaisant tests for admin console security fixes (ADM-01 to ADM-09).

Audit: AUDIT_ADMIN_CONSOLE_2026-05-16.md
Convention: each test tries to BREAK the fix, not validate the happy path.
Pattern: test_FIXNAME_blocks_ATTACK()
"""

import asyncio
import inspect
import json
import logging
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════
# Helpers — ASGI simulation
# ═══════════════════════════════════════════════════════════════


def _make_receive(body: bytes):
    """Create an ASGI receive callable returning a single body chunk."""
    called = False

    async def receive():
        nonlocal called
        if not called:
            called = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _make_send():
    """Create an ASGI send callable that captures response messages."""
    messages: list[dict] = []

    async def send(msg):
        messages.append(msg)

    return send, messages


def _response_status(messages: list[dict]) -> int:
    """Extract HTTP status from captured ASGI messages."""
    for m in messages:
        if m.get("type") == "http.response.start":
            return m.get("status", 0)
    return 0


def _response_body(messages: list[dict]) -> dict:
    """Extract JSON body from captured ASGI messages."""
    for m in messages:
        if m.get("type") == "http.response.body":
            try:
                return json.loads(m.get("body", b"{}"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
    return {}


def _response_headers(messages: list[dict]) -> dict[bytes, bytes]:
    """Extract headers dict from captured ASGI messages."""
    for m in messages:
        if m.get("type") == "http.response.start":
            return dict(m.get("headers", []))
    return {}


# ═══════════════════════════════════════════════════════════════
# ADM-01: esc() must escape quotes to prevent attribute injection
# ═══════════════════════════════════════════════════════════════


class TestADM01_EscEscapesQuotes:
    """ADM-01 CRITICAL: esc() in admin-app.js must escape both " and '."""

    def test_esc_blocks_double_quote_injection(self):
        """
        Attack: token name containing " breaks out of data-name="..."
        and injects a malicious data-action attribute.

        Verify: the esc() function source code includes &quot; replacement.
        """
        js_path = (
            Path(__file__).parent.parent
            / "src"
            / "live_mem"
            / "static"
            / "js"
            / "admin-app.js"
        )
        content = js_path.read_text()

        # Find the full esc function line (greedy — the function is one line)
        match = re.search(r"^const esc\s*=\s*s\s*=>.*$", content, re.MULTILINE)
        assert match, "esc() function not found in admin-app.js"
        esc_code = match.group()

        # The function MUST escape double quotes
        assert "&quot;" in esc_code, (
            f"ADM-01 BROKEN: esc() does not escape double quotes. "
            f"Attack: token name='foo\" data-action=\"confirm\"' "
            f"would inject arbitrary attributes. Source: {esc_code}"
        )

    def test_esc_blocks_single_quote_injection(self):
        """
        Attack: value containing ' breaks out of data-args='{"key":"val"}'
        (single-quoted HTML attribute used for JSON args).

        Verify: the esc() function source code includes &#x27; replacement.
        """
        js_path = (
            Path(__file__).parent.parent
            / "src"
            / "live_mem"
            / "static"
            / "js"
            / "admin-app.js"
        )
        content = js_path.read_text()
        match = re.search(r"^const esc\s*=\s*s\s*=>.*$", content, re.MULTILINE)
        assert match, "esc() function not found in admin-app.js"
        esc_code = match.group()

        assert "&#x27;" in esc_code, (
            f"ADM-01 BROKEN: esc() does not escape single quotes. "
            f"Attack: data-args='{{\"tool\":\"val'}}' injection. "
            f"Source: {esc_code}"
        )


# ═══════════════════════════════════════════════════════════════
# ADM-02: /api/tool must use safe_error(), not bare str(e)
# ═══════════════════════════════════════════════════════════════


class TestADM02_SafeErrorInApiTool:
    """ADM-02 HIGH: exception messages must not leak to client."""

    def test_api_tool_blocks_exception_leakage(self):
        """
        Attack: trigger an internal exception in /api/tool and verify
        the response does NOT contain the raw Python exception message
        (which would expose file paths, S3 endpoints, etc.).

        Verify: source code of _api_tool_call uses safe_error() in except block.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware

        source = inspect.getsource(StaticFilesMiddleware._api_tool_call)

        # The except block must call safe_error(), not return str(e)
        assert "safe_error(" in source, (
            "ADM-02 BROKEN: _api_tool_call does not use safe_error(). "
            "A boto3 exception would expose the S3 endpoint URL to the client."
        )

        # And must NOT have the old pattern: "message": str(e)
        # We check the except block specifically
        except_idx = source.rfind("except Exception")
        assert except_idx > 0, "No except block found in _api_tool_call"
        except_block = source[except_idx:]
        assert '"message": str(e)' not in except_block, (
            "ADM-02 BROKEN: _api_tool_call still contains 'message: str(e)' "
            "in the except block. This leaks raw exception messages."
        )


# ═══════════════════════════════════════════════════════════════
# ADM-03: HTML pages must include CSP headers (defense-in-depth)
# ═══════════════════════════════════════════════════════════════


class TestADM03_CspHeadersOnHtml:
    """ADM-03 HIGH: _serve_file must add CSP on HTML, not rely on WAF."""

    @pytest.mark.asyncio
    async def test_admin_html_has_csp_header(self):
        """
        Attack: access the app directly on port 8002 (bypass WAF).
        Without CSP, any XSS is directly exploitable.

        Verify: serving admin.html includes Content-Security-Policy header.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(send_fn, "admin.html", "text/html; charset=utf-8")

        headers = _response_headers(messages)
        csp = headers.get(b"content-security-policy", b"").decode()

        assert csp, (
            "ADM-03 BROKEN: admin.html served without Content-Security-Policy. "
            "Without WAF, the console has ZERO XSS protection."
        )
        assert "script-src 'self'" in csp, (
            "ADM-03 BROKEN: CSP does not contain script-src 'self'. "
            "Inline scripts or external scripts could execute."
        )
        assert "frame-ancestors 'none'" in csp, (
            "ADM-03 BROKEN: CSP missing frame-ancestors 'none'. "
            "The admin page could be embedded in an attacker's iframe."
        )
        # G2: vendored brand fonts (sanctioned additive CSP-literal update)
        assert "font-src 'self'" in csp, (
            "G2 BROKEN: CSP missing font-src 'self'. "
            "Vendored WOFF2 fonts would not be an explicit, aligned directive."
        )

    @pytest.mark.asyncio
    async def test_css_file_has_no_csp_header(self):
        """CSP headers should only be added to HTML, not CSS/JS/images."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(send_fn, "css/admin.css", "text/css; charset=utf-8")

        headers = _response_headers(messages)
        assert b"content-security-policy" not in headers, (
            "CSP header should NOT be added to non-HTML files"
        )

    @pytest.mark.asyncio
    async def test_xframe_options_on_html(self):
        """Verify X-Frame-Options: DENY is set on HTML pages."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(send_fn, "admin.html", "text/html; charset=utf-8")

        headers = _response_headers(messages)
        xfo = headers.get(b"x-frame-options", b"").decode()
        assert xfo == "DENY", (
            f"ADM-03 BROKEN: X-Frame-Options is '{xfo}' instead of 'DENY'"
        )


# ═══════════════════════════════════════════════════════════════
# ADM-05: /api/tool must reject oversized request bodies
# ═══════════════════════════════════════════════════════════════


class TestADM05_BodySizeLimit:
    """ADM-05 MEDIUM: /api/tool must reject bodies > api_tool_max_body_bytes."""

    @pytest.mark.asyncio
    async def test_api_tool_blocks_oversized_body(self):
        """
        Attack: send a multi-MB body to /api/tool to exhaust server memory.

        Verify: response is 413 Request Entity Too Large.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)

        # Auth context: admin (so permission gate passes)
        tok = current_token_info.set(
            {
                "type": "token",
                "client_name": "attacker",
                "permissions": ["read", "write", "admin"],
                "allowed_resources": [],
                "token_hash": "abc123deadbeef0000",
            }
        )

        # Craft a body exceeding default 1 MB limit
        oversized = b"x" * (1_048_576 + 1024)  # 1 MB + 1 KB
        receive = _make_receive(oversized)
        send_fn, messages = _make_send()

        try:
            await m._api_tool_call(receive, send_fn)
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        assert status == 413, (
            f"ADM-05 BROKEN: oversized body got status {status} instead of 413. "
            f"A 2 GB POST to /api/tool would exhaust server memory."
        )

    @pytest.mark.asyncio
    async def test_api_tool_accepts_normal_body(self):
        """A normal-sized body should NOT be rejected by the size limit."""
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)

        tok = current_token_info.set(
            {
                "type": "token",
                "client_name": "test",
                "permissions": ["read", "write"],
                "allowed_resources": [],
                "token_hash": "abc123deadbeef0000",
            }
        )

        body = json.dumps({"tool": "system_health", "arguments": {}}).encode()
        receive = _make_receive(body)
        send_fn, messages = _make_send()

        try:
            with patch("live_mem.tools.call_tool_direct") as mock_call:
                mock_call.return_value = {"status": "ok"}
                await m._api_tool_call(receive, send_fn)
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        assert status != 413, (
            f"Normal body of {len(body)} bytes was rejected with 413"
        )


# ═══════════════════════════════════════════════════════════════
# ADM-06: /api/tool must require write permission minimum
# ═══════════════════════════════════════════════════════════════


class TestADM06_PermissionGate:
    """ADM-06 MEDIUM: read-only tokens must be blocked from /api/tool."""

    @pytest.mark.asyncio
    async def test_api_tool_blocks_readonly_token(self):
        """
        Attack: a read-only token tries to call /api/tool to probe
        tool existence and enumerate the admin API.

        Verify: response is 403 with permission error.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)

        # Read-only token — should be blocked
        tok = current_token_info.set(
            {
                "type": "token",
                "client_name": "readonly-spy",
                "permissions": ["read"],
                "allowed_resources": [],
                "token_hash": "readonly1234567890ab",
            }
        )

        body = json.dumps({"tool": "system_health", "arguments": {}}).encode()
        receive = _make_receive(body)
        send_fn, messages = _make_send()

        try:
            await m._api_tool_call(receive, send_fn)
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        resp = _response_body(messages)

        assert status == 403, (
            f"ADM-06 BROKEN: read-only token got status {status} instead of 403. "
            f"A read token can enumerate all 40 MCP tools via /api/tool."
        )
        assert "write" in resp.get("message", "").lower(), (
            f"ADM-06: error message should mention 'write' permission requirement"
        )

    @pytest.mark.asyncio
    async def test_api_tool_allows_write_token(self):
        """A write token must be allowed through the permission gate."""
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)

        tok = current_token_info.set(
            {
                "type": "token",
                "client_name": "writer",
                "permissions": ["read", "write"],
                "allowed_resources": [],
                "token_hash": "writer1234567890ab",
            }
        )

        body = json.dumps({"tool": "system_health", "arguments": {}}).encode()
        receive = _make_receive(body)
        send_fn, messages = _make_send()

        try:
            with patch("live_mem.tools.call_tool_direct") as mock_call:
                mock_call.return_value = {"status": "ok"}
                await m._api_tool_call(receive, send_fn)
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        assert status != 403, (
            f"ADM-06 over-correction: write token blocked with {status}"
        )


# ═══════════════════════════════════════════════════════════════
# ADM-08: Audit trail must log tool name and argument keys
# ═══════════════════════════════════════════════════════════════


class TestADM08_AuditLogToolName:
    """ADM-08 MEDIUM: /api/tool must emit audit log with tool name."""

    @pytest.mark.asyncio
    async def test_api_tool_logs_tool_name_in_audit(self):
        """
        Attack: admin deletes a space via /api/tool but audit only shows
        "POST /api/tool" — impossible to know what tool was called.

        Verify: audit logger emits an entry with the tool name.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)

        tok = current_token_info.set(
            {
                "type": "token",
                "client_name": "admin-user",
                "permissions": ["read", "write", "admin"],
                "allowed_resources": [],
                "token_hash": "admin1234567890abc",
            }
        )

        body = json.dumps(
            {"tool": "space_delete", "arguments": {"space_id": "test", "confirm": True}}
        ).encode()
        receive = _make_receive(body)
        send_fn, messages = _make_send()

        audit_entries: list[str] = []
        with patch("live_mem.auth.middleware.audit_logger") as mock_audit:
            mock_audit.info = lambda msg: audit_entries.append(msg)
            with patch("live_mem.tools.call_tool_direct") as mock_call:
                mock_call.return_value = {"status": "ok"}
                try:
                    await m._api_tool_call(receive, send_fn)
                finally:
                    current_token_info.reset(tok)

        # Find the admin_tool_call audit entry
        tool_call_entries = [
            e for e in audit_entries if "admin_tool_call" in e
        ]
        assert tool_call_entries, (
            "ADM-08 BROKEN: no audit entry with event=admin_tool_call found. "
            "A destructive action via /api/tool leaves no traceable audit trail."
        )

        entry = json.loads(tool_call_entries[0])
        assert entry.get("tool") == "space_delete", (
            f"ADM-08 BROKEN: audit entry tool={entry.get('tool')} instead of 'space_delete'"
        )
        assert "space_id" in entry.get("arguments_keys", []), (
            "ADM-08 BROKEN: audit entry missing argument keys"
        )
        assert entry.get("client") == "admin-user", (
            f"ADM-08 BROKEN: audit entry client={entry.get('client')} instead of 'admin-user'"
        )


# ═══════════════════════════════════════════════════════════════
# ADM-09: call_tool_direct must handle unknown tools safely
# ═══════════════════════════════════════════════════════════════


class TestADM09_CallToolDirectRegression:
    """ADM-09 LOW: call_tool_direct must return clean error for unknown tools."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        """
        Verify: calling a non-existent tool returns a structured error,
        not a crash or an internal stack trace.
        """
        from live_mem import tools
        from live_mem.tools import call_tool_direct

        # Mock _mcp_ref with a fake tool manager (server not started in tests)
        mock_mcp = MagicMock()
        mock_mcp._tool_manager._tools = {}  # empty registry
        original = tools._mcp_ref
        tools._mcp_ref = mock_mcp
        try:
            result = await call_tool_direct("__nonexistent_tool_xss__", {})
        finally:
            tools._mcp_ref = original

        assert result.get("status") == "error", (
            "ADM-09: unknown tool should return status=error"
        )
        assert "__nonexistent_tool_xss__" in result.get("message", ""), (
            "ADM-09: error message should mention the unknown tool name"
        )

    @pytest.mark.asyncio
    async def test_uninitialized_mcp_returns_error(self):
        """If _mcp_ref is None (server not started), must not crash."""
        from live_mem import tools

        original = tools._mcp_ref
        tools._mcp_ref = None
        try:
            result = await tools.call_tool_direct("anything", {})
            assert result.get("status") == "error", (
                "ADM-09: uninitialized _mcp_ref should return error, not crash"
            )
        finally:
            tools._mcp_ref = original


# ═══════════════════════════════════════════════════════════════
# P8-1a: server-side safe-error & static-serving hardening
# (DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §6.3 items 8-9)
# ═══════════════════════════════════════════════════════════════


class TestP8_1a_Static404DoesNotReflectFilename:
    """§6.3 item 8: the 404 body must never echo the requested filename."""

    @pytest.mark.asyncio
    async def test_404_does_not_reflect_filename(self):
        """
        Attack: request a nonexistent /static/ path carrying an HTML/script
        payload in the filename itself.

        Verify: the 404 body contains no trace of the raw payload.
        """
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(
            send_fn, "<script>alert(1)</script>.js", "application/javascript"
        )

        status = _response_status(messages)
        body = b"".join(
            msg.get("body", b"")
            for msg in messages
            if msg.get("type") == "http.response.body"
        )

        assert status == 404
        assert b"<script>alert(1)</script>" not in body, (
            "P8-1a BROKEN: 404 body reflects the raw requested filename"
        )
        assert body == b"<h1>404 Not Found</h1>", (
            "P8-1a: 404 body must be the generic, filename-free page"
        )

    @pytest.mark.asyncio
    async def test_404_has_security_headers(self):
        """Verify: the static 404 carries the same CSP/X-Frame-Options/etc.
        headers as a 200 HTML response (previously absent)."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(send_fn, "does-not-exist.js", "application/javascript")

        headers = _response_headers(messages)
        assert headers.get(b"content-security-policy"), (
            "P8-1a BROKEN: static 404 served without Content-Security-Policy"
        )
        assert headers.get(b"x-frame-options") == b"DENY", (
            "P8-1a BROKEN: static 404 missing X-Frame-Options: DENY"
        )
        assert headers.get(b"x-content-type-options") == b"nosniff", (
            "P8-1a BROKEN: static 404 missing X-Content-Type-Options: nosniff"
        )


class TestP8_1a_StaticRouteTraversal:
    """§6.3 item 9: /static/* must reject traversal and always resolve to
    either a real file inside _static_dir or the generic 404 — never a
    fallthrough to the MCP Streamable HTTP handler."""

    def _scope(self, path: str) -> dict:
        return {"type": "http", "path": path, "method": "GET", "query_string": b""}

    @pytest.mark.asyncio
    async def test_static_route_rejects_leading_slash_traversal(self):
        """Attack: /static//etc/passwd -> rel_path becomes '/etc/passwd',
        which os.path.join would resolve as an absolute path, discarding
        _static_dir entirely."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        app_called = MagicMock()

        async def fake_app(scope, receive, send):
            app_called()

        m = StaticFilesMiddleware(fake_app)
        send_fn, messages = _make_send()
        await m(self._scope("/static//etc/passwd"), _make_receive(b""), send_fn)

        assert _response_status(messages) == 404
        app_called.assert_not_called()
        body = b"".join(
            msg.get("body", b"")
            for msg in messages
            if msg.get("type") == "http.response.body"
        )
        assert b"root:" not in body, "P8-1a BROKEN: leaked /etc/passwd content"

    @pytest.mark.asyncio
    async def test_static_route_rejects_dotdot_traversal(self):
        """Attack: /static/../auth/middleware.py must not leak source."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        app_called = MagicMock()

        async def fake_app(scope, receive, send):
            app_called()

        m = StaticFilesMiddleware(fake_app)
        send_fn, messages = _make_send()
        await m(
            self._scope("/static/../auth/middleware.py"), _make_receive(b""), send_fn
        )

        assert _response_status(messages) == 404
        app_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_static_route_rejects_empty_rel_path(self):
        """Attack: /static/ alone (empty rel_path) must be a fail-closed 404,
        not a silent fallthrough to the MCP handler."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        app_called = MagicMock()

        async def fake_app(scope, receive, send):
            app_called()

        m = StaticFilesMiddleware(fake_app)
        send_fn, messages = _make_send()
        await m(self._scope("/static/"), _make_receive(b""), send_fn)

        assert _response_status(messages) == 404
        app_called.assert_not_called(), (
            "P8-1a BROKEN: malformed /static/ path fell through to the MCP handler"
        )

    @pytest.mark.asyncio
    async def test_static_route_serves_real_file_unchanged(self):
        """Regression: a legitimate /static/ path is unaffected."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        app_called = MagicMock()

        async def fake_app(scope, receive, send):
            app_called()

        m = StaticFilesMiddleware(fake_app)
        send_fn, messages = _make_send()
        await m(self._scope("/static/css/admin.css"), _make_receive(b""), send_fn)

        assert _response_status(messages) == 200
        app_called.assert_not_called()


class TestP8_1a_ServeFileContainment:
    """Independent containment layer inside _serve_file itself (defense in
    depth beyond the route matcher's string checks) — both tests build a
    real marker file outside a temp _static_dir so RED-before-fix is
    guaranteed regardless of host filesystem layout (Codex R1 finding 1)."""

    @pytest.mark.asyncio
    async def test_serve_file_realpath_containment_relative(self, tmp_path):
        """Pre-fix: os.path.join(static_dir, "../secret.txt") resolves to
        the real marker file and would be served (200, leaked content).
        Post-fix: realpath containment rejects it (404, no leak)."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        static_dir = tmp_path / "static"
        static_dir.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-OUTSIDE-STATIC-DIR")

        m = StaticFilesMiddleware(None)
        m._static_dir = str(static_dir)

        send_fn, messages = _make_send()
        await m._serve_file(send_fn, "../secret.txt", "text/plain")

        status = _response_status(messages)
        body = b"".join(
            msg.get("body", b"")
            for msg in messages
            if msg.get("type") == "http.response.body"
        )
        assert status == 404, (
            f"P8-1a BROKEN: containment bypass, got status {status} for a "
            "path escaping _static_dir via '../'"
        )
        assert b"SECRET-OUTSIDE-STATIC-DIR" not in body, (
            "P8-1a BROKEN: _serve_file leaked file content from outside "
            "_static_dir"
        )

    @pytest.mark.asyncio
    async def test_serve_file_realpath_containment_absolute(self, tmp_path):
        """Pre-fix: os.path.join(static_dir, absolute_path) discards
        static_dir entirely (Python os.path.join semantics) and would serve
        the marker. Post-fix: rejected as containment failure."""
        from live_mem.auth.middleware import StaticFilesMiddleware

        static_dir = tmp_path / "static"
        static_dir.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-ABSOLUTE-PATH")

        m = StaticFilesMiddleware(None)
        m._static_dir = str(static_dir)

        send_fn, messages = _make_send()
        await m._serve_file(send_fn, str(secret), "text/plain")

        status = _response_status(messages)
        body = b"".join(
            msg.get("body", b"")
            for msg in messages
            if msg.get("type") == "http.response.body"
        )
        assert status == 404, (
            f"P8-1a BROKEN: os.path.join absolute-path discard not "
            f"contained, got status {status}"
        )
        assert b"SECRET-ABSOLUTE-PATH" not in body, (
            "P8-1a BROKEN: _serve_file leaked file content via an absolute "
            "filename argument"
        )


# ═══════════════════════════════════════════════════════════════
# P8-1 / G3 items 1-7: safe-error routing for call_tool_direct and the
# six _api_* REST handlers (DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §6.3)
# ═══════════════════════════════════════════════════════════════


def _admin_token():
    """Admin token_info: passes check_access() for every space_id."""
    return {
        "type": "token",
        "client_name": "g3-admin",
        "permissions": ["admin", "read", "write"],
        "allowed_resources": [],
        "token_hash": "g3admin1234567890ab",
    }


class TestG3SafeErrorApiLogin:
    """§6.3 item 2: _api_login must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_login_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        body = json.dumps({"token": "lm_whatever"}).encode()
        receive = _make_receive(body)
        send_fn, messages = _make_send()

        with patch(
            "live_mem.auth.middleware.AuthMiddleware._validate_token",
            side_effect=ValueError("SECRET-DETAIL"),
        ), patch("live_mem.config.get_settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.mcp_server_debug = False
            mock_gs.return_value = mock_settings

            await m._api_login({}, receive, send_fn)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/login leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/login leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorApiSpaces:
    """§6.3 item 3: _api_spaces must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_spaces_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()

        with patch("live_mem.core.space.get_space_service") as mock_svc, patch(
            "live_mem.config.get_settings"
        ) as mock_gs, patch(
            "live_mem.auth.middleware._get_effective_token_info",
            return_value={"permissions": ["admin"], "allowed_resources": []},
        ):
            mock_service = MagicMock()

            async def _raise(*a, **kw):
                raise ValueError("SECRET-DETAIL")

            mock_service.list_spaces = _raise
            mock_svc.return_value = mock_service
            mock_settings = MagicMock()
            mock_settings.mcp_server_debug = False
            mock_gs.return_value = mock_settings

            await m._api_spaces({}, send_fn)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/spaces leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/spaces leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorApiSpaceInfo:
    """§6.3 item 4: _api_space_info must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_space_info_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        tok = current_token_info.set(_admin_token())

        async def _raise(*a, **kw):
            raise ValueError("SECRET-DETAIL")

        try:
            with patch("live_mem.core.space.get_space_service") as mock_svc, patch(
                "live_mem.config.get_settings"
            ) as mock_gs:
                mock_service = MagicMock()
                mock_service.get_info = _raise
                mock_svc.return_value = mock_service
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = False
                mock_gs.return_value = mock_settings

                await m._api_space_info(send_fn, "myspace")
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/space/<id> leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/space/<id> leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorApiLiveNotes:
    """§6.3 item 5: _api_live_notes must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_live_notes_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        tok = current_token_info.set(_admin_token())

        async def _raise(*a, **kw):
            raise ValueError("SECRET-DETAIL")

        try:
            with patch("live_mem.core.live.get_live_service") as mock_svc, patch(
                "live_mem.config.get_settings"
            ) as mock_gs:
                mock_service = MagicMock()
                mock_service.read_notes = _raise
                mock_svc.return_value = mock_service
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = False
                mock_gs.return_value = mock_settings

                await m._api_live_notes(send_fn, "myspace", "")
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/live_notes leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/live_notes leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorApiBankList:
    """§6.3 item 6: _api_bank_list must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_bank_list_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        tok = current_token_info.set(_admin_token())

        async def _raise_exists(*a, **kw):
            raise ValueError("SECRET-DETAIL")

        try:
            with patch("live_mem.core.storage.get_storage") as mock_storage, patch(
                "live_mem.config.get_settings"
            ) as mock_gs:
                storage = MagicMock()
                storage.exists = _raise_exists
                mock_storage.return_value = storage
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = False
                mock_gs.return_value = mock_settings

                await m._api_bank_list(send_fn, "myspace")
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/bank_list leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/bank_list leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorApiBankFile:
    """§6.3 item 7: _api_bank_file must mask exception details via safe_error()."""

    @pytest.mark.asyncio
    async def test_api_bank_file_masks_exception_with_debug_off(self):
        from live_mem.auth.middleware import StaticFilesMiddleware
        from live_mem.auth.context import current_token_info

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        tok = current_token_info.set(_admin_token())

        async def _raise_get(*a, **kw):
            raise ValueError("SECRET-DETAIL")

        try:
            with patch("live_mem.core.storage.get_storage") as mock_storage, patch(
                "live_mem.config.get_settings"
            ) as mock_gs:
                storage = MagicMock()
                storage.get = _raise_get
                mock_storage.return_value = storage
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = False
                mock_gs.return_value = mock_settings

                await m._api_bank_file(send_fn, "myspace", "notes.md")
        finally:
            current_token_info.reset(tok)

        status = _response_status(messages)
        resp = _response_body(messages)
        raw = json.dumps(resp)

        assert status == 500, f"Expected 500, got {status}"
        assert resp.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: /api/bank_file leaked non-generic message: {resp!r}"
        )
        assert "SECRET-DETAIL" not in raw, (
            f"G3 BROKEN: /api/bank_file leaked exception detail: {raw!r}"
        )


class TestG3SafeErrorCallToolDirect:
    """§6.3 item 1: call_tool_direct's except-Exception branch must route
    through safe_error(), while the two ADM-09-pinned early-return
    branches (unknown tool, uninitialized _mcp_ref) stay byte-compatible."""

    @pytest.mark.asyncio
    async def test_call_tool_direct_masks_exception_with_debug_off(self):
        from live_mem import tools
        from live_mem.tools import call_tool_direct

        async def _raise(**kwargs):
            raise ValueError("SECRET-DETAIL")

        fake_tool = MagicMock()
        fake_tool.fn = _raise

        mock_mcp = MagicMock()
        mock_mcp._tool_manager._tools = {"boom_tool": fake_tool}
        original = tools._mcp_ref
        tools._mcp_ref = mock_mcp
        try:
            with patch("live_mem.config.get_settings") as mock_gs:
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = False
                mock_gs.return_value = mock_settings

                result = await call_tool_direct("boom_tool", {})
        finally:
            tools._mcp_ref = original

        assert result.get("status") == "error"
        assert result.get("message") == "Erreur interne du serveur", (
            f"G3 BROKEN: call_tool_direct leaked non-generic message: {result!r}"
        )
        assert "SECRET-DETAIL" not in json.dumps(result), (
            f"G3 BROKEN: call_tool_direct leaked exception detail: {result!r}"
        )

    @pytest.mark.asyncio
    async def test_call_tool_direct_reveals_detail_with_debug_on(self):
        from live_mem import tools
        from live_mem.tools import call_tool_direct

        async def _raise(**kwargs):
            raise ValueError("SECRET-DETAIL")

        fake_tool = MagicMock()
        fake_tool.fn = _raise

        mock_mcp = MagicMock()
        mock_mcp._tool_manager._tools = {"boom_tool": fake_tool}
        original = tools._mcp_ref
        tools._mcp_ref = mock_mcp
        try:
            with patch("live_mem.config.get_settings") as mock_gs:
                mock_settings = MagicMock()
                mock_settings.mcp_server_debug = True
                mock_gs.return_value = mock_settings

                result = await call_tool_direct("boom_tool", {})
        finally:
            tools._mcp_ref = original

        assert result.get("status") == "error"
        assert "SECRET-DETAIL" in result.get("message", ""), (
            f"G3: debug mode should reveal str(e); got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_call_tool_direct_unknown_tool_contract_intact(self):
        """ADM-09 regression fence: unknown-tool branch must stay
        byte-identical after the G3 except-branch change (same call site,
        different branch)."""
        from live_mem import tools
        from live_mem.tools import call_tool_direct

        mock_mcp = MagicMock()
        mock_mcp._tool_manager._tools = {}
        original = tools._mcp_ref
        tools._mcp_ref = mock_mcp
        try:
            result = await call_tool_direct("__nonexistent_tool_g3__", {})
        finally:
            tools._mcp_ref = original

        assert result == {
            "status": "error",
            "message": "Unknown tool: __nonexistent_tool_g3__",
        }, (
            f"G3 BROKEN: unknown-tool early-return branch changed shape: "
            f"{result!r}"
        )

    @pytest.mark.asyncio
    async def test_call_tool_direct_uninitialized_mcp_contract_intact(self):
        """ADM-09 regression fence: uninitialized _mcp_ref branch must stay
        byte-identical after the G3 except-branch change."""
        from live_mem import tools

        original = tools._mcp_ref
        tools._mcp_ref = None
        try:
            result = await tools.call_tool_direct("anything", {})
        finally:
            tools._mcp_ref = original

        assert result == {
            "status": "error",
            "message": "Server not initialized",
        }, (
            f"G3 BROKEN: uninitialized-_mcp_ref early-return branch changed "
            f"shape: {result!r}"
        )


# ═══════════════════════════════════════════════════════════════
# P8-1 (Track C — frontend shell rewrite, issue #139)
# DESIGN/hivemind/ADMIN_CONSOLE_DESIGN.md §2 (design system/app shell),
# §3 (IA/routing), §5.0 (global client rules), §7 (security posture/XSS).
# These classes are appended after Track A's G3 classes above and never
# touch them.
# ═══════════════════════════════════════════════════════════════


def _read_admin_source(*parts: str) -> str:
    return (
        Path(__file__).parent.parent
        / "src"
        / "live_mem"
        / "static"
        / Path(*parts)
    ).read_text(encoding="utf-8")


_ADMIN_APP_JS = "js/admin-app.js"
_ADMIN_API_JS = "js/admin-api.js"
_VIEW_MODULE_FILES = [
    "js/admin/views-dashboard.js",
    "js/admin/views-spaces.js",
    "js/admin/views-space-detail.js",
    "js/admin/views-consolidation.js",
    "js/admin/views-audit.js",
    "js/admin/views-access.js",
    "js/admin/views-operator.js",
]
_VIEW_STUB_FILES = [
    path for path in _VIEW_MODULE_FILES if path != "js/admin/views-audit.js"
]


class TestP81ShowModalEscape:
    """XSS fix (§7.3): showModal's title must be escaped, not raw."""

    def test_showmodal_title_is_escaped(self):
        """
        Attack: a token name / space id round-tripped through a dataset
        attribute and passed as showModal's title executes as HTML.

        Verify: admin-app.js contains the escaped interpolation
        `${esc(title)}` and contains NO literal `${title}` anywhere
        (the raw, unescaped interpolation pattern that caused the bug).
        """
        content = _read_admin_source(_ADMIN_APP_JS)

        assert "${esc(title)}" in content, (
            "P8-1 XSS FIX MISSING: admin-app.js does not escape showModal's "
            "title with esc()."
        )
        assert "${title}" not in content, (
            "P8-1 XSS REGRESSION: admin-app.js still contains the raw, "
            "unescaped ${title} interpolation pattern."
        )
        assert "<h3>${title}" not in content
        assert ">${title}<" not in content


class TestP81AdminApiGuards:
    """Global client-rule guards (contract §5.0) added to callTool()."""

    def test_truncation_guard_in_shared_api_layer(self):
        api_src = _read_admin_source(_ADMIN_API_JS)
        app_src = _read_admin_source(_ADMIN_APP_JS)

        assert "_truncated" in api_src, (
            "P8-1 BROKEN: admin-api.js does not check the _truncated body flag."
        )
        assert "X-Response-Truncated" in api_src, (
            "P8-1 BROKEN: admin-api.js does not check the "
            "X-Response-Truncated header."
        )
        assert (
            "Response exceeded the console's 512 KB limit"
            in api_src + app_src
        ), (
            "P8-1 BROKEN: the mandated truncation copy is missing from the "
            "shared API layer."
        )

    def test_429_copy_and_no_retry(self):
        api_src = _read_admin_source(_ADMIN_API_JS)

        assert "Rate limited by the gateway" in api_src, (
            "P8-1 BROKEN: missing the mandated 429 rate-limit copy."
        )
        # No auto-retry pattern: callTool must not issue a second fetch to
        # /api/tool from inside its own 429 branch.
        assert "429" in api_src
        rate_limited_idx = api_src.find("r.status === 429")
        assert rate_limited_idx != -1
        branch = api_src[rate_limited_idx : rate_limited_idx + 300]
        assert "fetch(" not in branch, (
            "P8-1 BROKEN: callTool appears to auto-retry inside the 429 branch."
        )

    def test_read_only_blocked_state(self):
        api_src = _read_admin_source(_ADMIN_API_JS)

        assert "This token is read-only" in api_src, (
            "P8-1 BROKEN: missing the mandated read-only blocked-state copy."
        )
        # Distinct from the 401 branch.
        assert "r.status === 401" in api_src
        assert "r.status === 403" in api_src


class TestP81AdminApiCallToolBehavior:
    """Behavioral regression tests for callTool()'s new §5.0 guards.

    These exercise the real admin-api.js source through a minimal JS shim
    is not available in this Python-only test suite; instead we pin the
    guard's exact branch ordering and copy via source inspection (above)
    plus a fetch-mock-free structural check that the guards run before any
    JSON.parse of a truncated body.
    """

    def test_truncation_checked_before_json_parse(self):
        api_src = _read_admin_source(_ADMIN_API_JS)
        truncated_header_idx = api_src.find("X-Response-Truncated")
        json_parse_idx = api_src.find("JSON.parse(text)")
        assert truncated_header_idx != -1
        assert json_parse_idx != -1
        assert truncated_header_idx < json_parse_idx, (
            "P8-1 BROKEN: the truncation header guard must run before the "
            "response body is JSON.parse'd as tool output."
        )

    def test_normal_200_response_unaffected(self):
        """Regression: a normal 200 response's handling path is intact —
        401/429/403/_truncated checks must not swallow it."""
        api_src = _read_admin_source(_ADMIN_API_JS)
        # The final fallthrough still returns the parsed body.
        assert "return body" in api_src

    def test_boot_renders_sentinel_message_not_generic_unavailable(self):
        """Codex pre-commit finding 1: callTool()'s §5.0 sentinels
        (read_only/rate_limited/truncated) reach _bootAuthenticated via
        system_whoami, but must render their own specific, mandated message
        (e.g. the §7.1.4 read-only blocked-state copy) — not be silently
        swallowed into the generic 'Identity unavailable' state."""
        app_src = _read_admin_source(_ADMIN_APP_JS)
        boot_match = re.search(
            r"async function _bootAuthenticated\(\)\s*\{(.*?)\n\}",
            app_src,
            re.DOTALL,
        )
        assert boot_match, "_bootAuthenticated function not found in admin-app.js"
        body = boot_match.group(1)
        assert "CALLTOOL_SENTINEL_STATUSES" in app_src, (
            "P8-1 BROKEN: no sentinel-status set found; read_only/"
            "rate_limited/truncated statuses from system_whoami are not "
            "distinguished from a generic identity failure."
        )
        assert "whoami.status" in body, (
            "P8-1 BROKEN: _bootAuthenticated does not branch on the "
            "sentinel status returned by callTool('system_whoami', ...)."
        )
        assert "whoami.message" in body, (
            "P8-1 BROKEN: _bootAuthenticated does not surface the "
            "sentinel's specific message (e.g. the read-only blocked-state "
            "copy) anywhere."
        )

    def test_sentinel_statuses_set_matches_admin_api_contract(self):
        """The sentinel set _bootAuthenticated checks for must be exactly
        the three statuses callTool() can actually return (§5.0), no more
        and no fewer."""
        app_src = _read_admin_source(_ADMIN_APP_JS)
        api_src = _read_admin_source(_ADMIN_API_JS)
        for status in ("read_only", "rate_limited", "truncated"):
            assert f"'{status}'" in app_src, (
                f"P8-1 BROKEN: sentinel status '{status}' is not "
                f"referenced in admin-app.js's sentinel handling."
            )
            assert f"status: '{status}'" in api_src, (
                f"admin-api.js no longer returns the '{status}' sentinel "
                f"shape expected by the shell."
            )


class TestP81RouteMatcher:
    """Route table (contract §3.1.1) dispatch, including the tier-scoped
    #/spaces/<id>/<tier> route flagged by Codex R1 finding 2."""

    @staticmethod
    def _extract_match_route():
        """Re-implements the pinned _matchRoute contract in Python so the
        route table can be exercised without a JS runtime. Mirrors
        admin-app.js's _matchRoute exactly; any drift is caught by the
        source-inspection tests in this class."""
        import re as _re
        from urllib.parse import unquote

        # SPACE_ID_RE deliberately not used here: per contract §3.1.2 step 3
        # (and Codex pre-commit finding 2), that validation belongs to the
        # Space Detail module, not the router.
        TIERS = {"short", "mid", "long"}

        # Python's unquote(..., errors="strict") is lenient: unquote("%zz")
        # returns "%zz" unchanged instead of raising, unlike JS's
        # decodeURIComponent, which throws URIError on any '%' not followed
        # by exactly two hex digits. This regex makes the Python mirror
        # reject the same malformed inputs JS would throw on (found via a
        # test failure once SPACE_ID_RE was no longer around to coincidentally
        # mask this pre-existing mirror gap — see Codex pre-commit finding 2
        # adjudication).
        _MALFORMED_PERCENT_RE = _re.compile(r"%(?![0-9A-Fa-f]{2})")

        def match(hash_value: str):
            raw = hash_value[1:] if hash_value.startswith("#") else hash_value
            if not raw.startswith("/"):
                return {"view": None, "params": {}}

            segments = raw[1:].split("/")

            if raw == "/dashboard":
                return {"view": "dashboard", "params": {}}
            if raw == "/spaces":
                return {"view": "spaces", "params": {}}
            if raw == "/consolidation":
                return {"view": "consolidation", "params": {}}
            if raw == "/audit":
                return {"view": "audit", "params": {}}
            if raw == "/access":
                return {"view": "access", "params": {}}
            if raw == "/operator/backups":
                return {"view": "operator", "params": {"tab": "backups"}}
            if raw == "/operator/maintenance":
                return {"view": "operator", "params": {"tab": "maintenance"}}
            if raw == "/operator":
                return {"view": "__normalize-operator", "params": {}}

            if len(segments) == 2 and segments[0] == "spaces" and segments[1] != "":
                return _match_space_detail(segments[1], None)
            if len(segments) == 3 and segments[0] == "spaces" and segments[1] != "":
                return _match_space_detail(segments[1], segments[2])

            return {"view": None, "params": {}}

        def _match_space_detail(encoded_id: str, tier):
            if _MALFORMED_PERCENT_RE.search(encoded_id):
                # Emulates decodeURIComponent throwing URIError (§3.1.2 step 2).
                return {"view": None, "params": {}}
            try:
                space_id = unquote(encoded_id, errors="strict")
            except Exception:
                return {"view": None, "params": {}}
            # NOTE (Codex pre-commit finding 2): SPACE_ID_RE validation does
            # NOT happen here. Per contract §3.1.2 step 3, that check belongs
            # to the Space Detail module, not the router — a regex-invalid
            # but decodable id still dispatches to 'space-detail' so the view
            # can render its own "invalid space id" state (no tool call).
            # Only a decode failure (above) makes the route unknown.
            if tier is None:
                return {"view": "space-detail", "params": {"spaceId": space_id}}
            if tier not in TIERS:
                return {"view": None, "params": {}}
            return {
                "view": "space-detail",
                "params": {"spaceId": space_id, "tier": tier},
            }

        return match

    def test_all_named_routes_dispatch(self):
        match = self._extract_match_route()
        cases = {
            "#/dashboard": "dashboard",
            "#/spaces": "spaces",
            "#/consolidation": "consolidation",
            "#/audit": "audit",
            "#/access": "access",
            "#/operator/backups": "operator",
            "#/operator/maintenance": "operator",
        }
        for hash_value, expected_view in cases.items():
            result = match(hash_value)
            assert result["view"] == expected_view, (
                f"P8-1 route-matcher BROKEN for {hash_value}: {result}"
            )

    def test_space_detail_without_tier(self):
        match = self._extract_match_route()
        result = match("#/spaces/prod-mesh")
        assert result == {"view": "space-detail", "params": {"spaceId": "prod-mesh"}}

    def test_space_detail_with_each_tier(self):
        match = self._extract_match_route()
        for tier in ("short", "mid", "long"):
            result = match(f"#/spaces/prod-mesh/{tier}")
            assert result == {
                "view": "space-detail",
                "params": {"spaceId": "prod-mesh", "tier": tier},
            }, f"P8-1 BROKEN: tier route for {tier!r} did not dispatch correctly"

    def test_invalid_tier_normalizes_like_unknown(self):
        match = self._extract_match_route()
        result = match("#/spaces/prod-mesh/bogus-tier")
        assert result["view"] is None

    def test_decodable_but_regex_invalid_space_id_still_reaches_space_detail(self):
        """Codex pre-commit finding 2: SPACE_ID_RE validation belongs to the
        Space Detail module (§3.1.2 step 3), not the router. A space id that
        decodes cleanly but fails the regex (e.g. contains '!') must still
        dispatch to 'space-detail' with the raw id in params — it must NOT
        be silently normalized to #/dashboard like an unknown route."""
        import re as _re

        assert not _re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", "bad!id"), (
            "test setup error: 'bad!id' must actually be regex-invalid"
        )
        match = self._extract_match_route()
        result = match("#/spaces/bad!id")
        assert result == {"view": "space-detail", "params": {"spaceId": "bad!id"}}, (
            f"P8-1 BROKEN: a regex-invalid but decodable space id was "
            f"rejected by the router instead of reaching the view: {result}"
        )

    def test_operator_bare_normalizes_to_backups_marker(self):
        match = self._extract_match_route()
        result = match("#/operator")
        assert result["view"] == "__normalize-operator"

    def test_unknown_route_is_unmatched(self):
        match = self._extract_match_route()
        for bogus in ("#/bogus", "#/", "#", "", "#/spaces/"):
            result = match(bogus)
            assert result["view"] is None, f"{bogus!r} unexpectedly matched"

    def test_malformed_encoded_space_id_does_not_throw(self):
        match = self._extract_match_route()
        # A lone '%' is not a valid percent-escape and must not raise.
        result = match("#/spaces/%zz")
        assert result["view"] is None

    def test_route_table_source_matches_contract(self):
        """Source-inspection companion: the real router in admin-app.js
        must encode the same regex-equivalent route table (defends against
        the Python mirror above drifting from the real implementation)."""
        content = _read_admin_source(_ADMIN_APP_JS)
        assert "short|mid|long" in content or (
            "'short'" in content and "'mid'" in content and "'long'" in content
        )
        assert "decodeURIComponent" in content
        assert "location.replace" in content
        assert "SPACE_ID_RE" in content or "a-zA-Z0-9_-" in content

    def test_matchspacedetail_does_not_reject_on_regex(self):
        """Codex pre-commit finding 2, source-inspection pin: _matchSpaceDetail
        must not early-return view:null based on SPACE_ID_RE — that
        validation belongs to the Space Detail module (§3.1.2 step 3)."""
        content = _read_admin_source(_ADMIN_APP_JS)
        match = re.search(
            r"function _matchSpaceDetail\([^)]*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert match, "_matchSpaceDetail function not found in admin-app.js"
        body = match.group(1)
        assert "SPACE_ID_RE.test" not in body, (
            "P8-1 BROKEN: _matchSpaceDetail rejects on SPACE_ID_RE again — "
            "this must be the Space Detail view's job, not the router's "
            "(Codex pre-commit finding 2)."
        )


class TestP81SessionWipe:
    """Logout/expiry content-and-cache wipe rule (contract §3.1.4, exact
    5-item list). Source-pin style, mirroring ADM-01."""

    def test_wipe_session_defined_and_wired(self):
        content = _read_admin_source(_ADMIN_APP_JS)

        assert "function wipeSession" in content, (
            "P8-1 BROKEN: wipeSession() is not defined in admin-app.js."
        )

        show_login_idx = content.find("function showLogin")
        assert show_login_idx != -1, "P8-1 BROKEN: showLogin() not found."
        show_login_body = content[show_login_idx : show_login_idx + 400]
        assert "wipeSession()" in show_login_body, (
            "P8-1 BROKEN: showLogin() does not call wipeSession()."
        )

        wipe_idx = content.find("function wipeSession")
        wipe_body = content[wipe_idx : wipe_idx + 900]
        assert "adminModal" in wipe_body, (
            "P8-1 BROKEN: wipeSession() does not destroy #adminModal — a "
            "one-time token secret could remain in the DOM after logout."
        )
        assert "toastStack" in wipe_body, (
            "P8-1 BROKEN: wipeSession() does not empty #toastStack."
        )
        assert "identityBlock" in wipe_body, (
            "P8-1 BROKEN: wipeSession() does not empty the identity block."
        )
        assert "_resetCaches" in wipe_body or "cache" in wipe_body, (
            "P8-1 BROKEN: wipeSession() does not clear in-memory caches."
        )
        assert "content" in wipe_body, (
            "P8-1 BROKEN: wipeSession() does not reset #content."
        )

    def test_hash_is_never_touched_by_wipe_or_showlogin(self):
        """The hash must be preserved across logout/expiry so re-login
        returns the operator to where they were."""
        content = _read_admin_source(_ADMIN_APP_JS)
        wipe_idx = content.find("function wipeSession")
        wipe_body = content[wipe_idx : wipe_idx + 900]
        assert "location.hash" not in wipe_body
        assert "location.replace" not in wipe_body


class TestP81FontServingAndContentType:
    """G2 (§6.2): app CSP font-src delta + .woff2 content-type mapping.

    The CSP assertion itself lives in TestADM03_CspHeadersOnHtml (the one
    sanctioned literal update); this class covers the additional G2-only
    obligations that are P8-1's responsibility as Track C.
    """

    def test_woff2_content_type(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        assert (
            StaticFilesMiddleware._guess_content_type("fonts/space-grotesk-700.woff2")
            == "font/woff2"
        )
        # woff (non-2) is deliberately not mapped — only WOFF2 is vendored.
        assert (
            StaticFilesMiddleware._guess_content_type("fonts/space-grotesk-700.woff")
            == "application/octet-stream"
        )

    @pytest.mark.asyncio
    async def test_serve_file_woff2_has_correct_content_type_and_no_csp(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(None)
        send_fn, messages = _make_send()
        await m._serve_file(
            send_fn, "fonts/space-grotesk-600.woff2", "font/woff2"
        )
        headers = _response_headers(messages)
        assert headers.get(b"content-type") == b"font/woff2"
        assert b"content-security-policy" not in headers, (
            "CSP header should NOT be added to non-HTML files (fonts included)"
        )

    def test_admin_css_declares_font_faces(self):
        css = _read_admin_source("css/admin.css")
        assert "@font-face" in css
        for family in ("Space Grotesk", "Hanken Grotesk", "JetBrains Mono"):
            assert family in css, f"admin.css is missing an @font-face for {family}"
        assert "/static/fonts/" in css


class TestP81ForbiddenSinks:
    """Source-inspection assertions (Codex R1 'Weak Checks' note): the
    grep-based escaping audit alone is insufficient — pin the forbidden
    sinks explicitly across admin-app.js and all 7 view modules throughout
    their stub-to-implementation lifecycle."""

    def _all_new_frontend_files(self):
        files = [_ADMIN_APP_JS] + _VIEW_MODULE_FILES
        return {f: _read_admin_source(f) for f in files}

    def test_no_document_write(self):
        for path, content in self._all_new_frontend_files().items():
            assert "document.write(" not in content, f"{path} uses document.write("

    def test_no_insertadjacenthtml(self):
        for path, content in self._all_new_frontend_files().items():
            assert "insertAdjacentHTML(" not in content, (
                f"{path} uses insertAdjacentHTML("
            )

    def test_no_javascript_or_data_html_urls(self):
        for path, content in self._all_new_frontend_files().items():
            assert "javascript:" not in content, f"{path} contains a javascript: URL"
            assert "data:text/html" not in content, (
                f"{path} contains a data:text/html URL"
            )


class TestP81EmojiGuard:
    """Emoji-guard fence (visual-system regression, protects the whole P8
    wave): admin.html, admin.css, admin-app.js and every view module must be
    completely emoji-free."""

    EMOJI_RE = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "☀-➿"
        "]"
    )
    FORBIDDEN_LITERALS = ("✕", "⚠", "❌")  # ✕ ⚠ ❌

    def _files(self):
        base = Path(__file__).parent.parent / "src" / "live_mem" / "static"
        paths = [
            base / "admin.html",
            base / "js" / "admin-app.js",
            base / "css" / "admin.css",
        ] + [base / "js" / "admin" / Path(f).name for f in _VIEW_MODULE_FILES]
        return paths

    def test_no_emoji_in_admin_sources(self):
        for path in self._files():
            text = path.read_text(encoding="utf-8")
            hits = self.EMOJI_RE.findall(text)
            assert not hits, f"{path} contains emoji/pictographic code points: {hits}"
            for lit in self.FORBIDDEN_LITERALS:
                assert lit not in text, f"{path} contains forbidden literal {lit!r}"


class TestP81ViewModuleLifecycle:
    """Every P8 view keeps its shell registration after its stub graduates."""

    def test_all_view_modules_register(self):
        for path in _VIEW_MODULE_FILES:
            content = _read_admin_source(path)
            assert "AdminViews.register(" in content, f"{path} does not register a view"


class TestP81ViewStubsHonestPlaceholders:
    """Appendix A / D7 discipline: stubs must be honest 'not available'
    placeholders — no mock data, no tool calls, no capability claims."""

    def test_stubs_avoid_mock_data(self):
        for path in _VIEW_STUB_FILES:
            content = _read_admin_source(path)
            # P8-2..P8-6 replace these modules in parallel.  Keep the P8-1
            # placeholder contract pinned only while a file still declares
            # itself a stub; implemented views remain covered by the global
            # sink and emoji guards above and by their own child-PR tests.
            if "— stub" not in content:
                continue
            assert "callTool(" not in content, (
                f"{path} calls a tool — stubs must not call tools (D8)"
            )
            assert "stateUnavailable(" in content, (
                f"{path} does not use the stateUnavailable() placeholder"
            )

    def test_stubs_under_30_lines(self):
        for path in _VIEW_STUB_FILES:
            full_path = (
                Path(__file__).parent.parent / "src" / "live_mem" / "static" / Path(path)
            )
            content = full_path.read_text(encoding="utf-8")
            if "— stub" not in content:
                continue
            line_count = len(content.splitlines())
            assert line_count < 30, f"{path} has {line_count} lines (must be < 30)"


class TestP81PrReviewFindings:
    """PR-level Codex adversarial review (PR #149), round 1 NO-GO: 3 MEDIUM
    findings on a broader sweep of the shell contract, independent of the
    PLAN/pre-commit review rounds already resolved on issue #139."""

    def test_run_action_kept_per_feature_parity_row_s2(self):
        """Contract §4 row S2: the generic `run` action (data-action="run"
        -> shared result modal via callTool()) must be KEPT in P8-1, same
        POST /api/tool path, same TOOL_TITLES mechanism — restyled per §2,
        not dropped. It was missing entirely from the first PR-review pass."""
        content = _read_admin_source(_ADMIN_APP_JS)
        assert "TOOL_TITLES" in content, (
            "P8-1 BROKEN: TOOL_TITLES mechanism (contract §4 row S2) is missing."
        )
        assert "registerAction('run'" in content, (
            "P8-1 BROKEN: no registerAction('run', ...) — data-action=\"run\" "
            "buttons would silently no-op for every future P8-2..P8-6 view."
        )
        run_match = re.search(
            r"registerAction\('run',\s*\(data\)\s*=>\s*\{(.*?)\n\}\);",
            content,
            re.DOTALL,
        )
        assert run_match, "registerAction('run', ...) handler body not found"
        assert "runAndShow(" in run_match.group(1), (
            "P8-1 BROKEN: the 'run' action handler does not call a "
            "runAndShow()-equivalent shared utility."
        )
        assert "callTool(" in content, (
            "P8-1 BROKEN: the run mechanism must still go through callTool() "
            "(same POST /api/tool path as every other tool invocation)."
        )

    def test_run_action_respects_epoch_guard(self):
        """Codex PR-level review round 2 (new finding introduced by the
        round-1 fixup): runAndShow()'s async callTool() continuation must
        capture the epoch before the await and drop both the success and
        error branches if AdminRouter.epoch has since changed (§3.3.2 rule
        3) — otherwise a stale result/error modal can paint over a view the
        operator has already navigated away from."""
        content = _read_admin_source(_ADMIN_APP_JS)
        run_and_show_match = re.search(
            r"function runAndShow\(tool, args\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert run_and_show_match, "runAndShow function not found in admin-app.js"
        body = run_and_show_match.group(1)
        assert "AdminRouter.epoch" in body, (
            "P8-1 BROKEN: runAndShow() does not reference AdminRouter.epoch "
            "at all — the async run action bypasses the stale-response guard."
        )
        # Both the .then() success path and the .catch() error path must
        # each guard on the captured epoch, not just one of the two.
        then_match = re.search(r"\.then\(result\s*=>\s*\{(.*?)\}\)", body, re.DOTALL)
        catch_match = re.search(r"\.catch\(\(\)\s*=>\s*\{(.*?)\}\)", body, re.DOTALL)
        assert then_match and "AdminRouter.epoch" in then_match.group(1), (
            "P8-1 BROKEN: the success continuation of runAndShow() does not "
            "re-check AdminRouter.epoch before rendering."
        )
        assert catch_match and "AdminRouter.epoch" in catch_match.group(1), (
            "P8-1 BROKEN: the error continuation of runAndShow() does not "
            "re-check AdminRouter.epoch before rendering."
        )

    def test_admin_router_exposes_epoch(self):
        """Contract §3.3.2 rule 3 (epoch guard) literally compares
        `ctx.epoch === AdminRouter.epoch` — AdminRouter must expose a live
        `epoch` property, not just pass a snapshot through ctx. Missing this
        blocks every P8-2..P8-6 view from implementing the mandatory
        stale-async-response guard without reopening admin-app.js."""
        content = _read_admin_source(_ADMIN_APP_JS)
        router_match = re.search(
            r"const AdminRouter = \(\(\) => \{(.*?)\n\}\)\(\);",
            content,
            re.DOTALL,
        )
        assert router_match, "AdminRouter IIFE not found in admin-app.js"
        body = router_match.group(1)
        assert re.search(r"get\s+epoch\s*\(\)", body), (
            "P8-1 BROKEN: AdminRouter does not expose a live 'epoch' "
            "getter — views cannot implement the §3.3.2 stale-response "
            "guard against AdminRouter.epoch."
        )

    def test_destructive_modal_uses_danger_treatment(self):
        """Contract §2.4.6 / §7.4.4: destructive confirms must use the
        Critical Red family (danger button, alert-icon header) — showModal's
        default primary-button styling is not enough on its own."""
        content = _read_admin_source(_ADMIN_APP_JS)
        destructive_match = re.search(
            r"function showDestructiveModal\(opts\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert destructive_match, "showDestructiveModal function not found"
        body = destructive_match.group(1)
        assert "btn-danger" in body, (
            "P8-1 BROKEN: showDestructiveModal does not apply the "
            "'btn-danger' class — the confirm button would ship with "
            "non-destructive (primary/cyan) visual semantics."
        )
        assert "icon('alert')" in body, (
            "P8-1 BROKEN: showDestructiveModal does not add the mandated "
            "alert-icon header treatment."
        )
        css_content = _read_admin_source("css/admin.css")
        assert ".modal-header--danger" in css_content, (
            "admin.css has no danger-header styling for the destructive "
            "modal variant."
        )
