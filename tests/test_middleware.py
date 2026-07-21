# -*- coding: utf-8 -*-
"""
Unit tests for production ASGI middleware stack.

Tests: RequestIdMiddleware, MetricsMiddleware, ResponseLimitMiddleware,
       AuditMiddleware.
"""

import json
import pytest

from live_mem.middleware import (
    RequestIdMiddleware,
    MetricsMiddleware,
    ResponseLimitMiddleware,
    AuditMiddleware,
    current_request_id,
)


# ─────────────────────────────────────────────────────────────
# Helpers — minimal ASGI test harness
# ─────────────────────────────────────────────────────────────


def _make_scope(path="/test", method="GET", headers=None):
    """Create a minimal ASGI HTTP scope."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "client": ("127.0.0.1", 54321),
    }


async def _dummy_receive():
    return {"type": "http.request", "body": b""}


def _echo_app(status=200, body=b'{"ok":true}', content_type=b"application/json"):
    """ASGI app that returns a fixed response."""

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )

    return app


async def _collect_response(app, scope):
    """Run an ASGI app and collect the response parts."""
    messages = []

    async def send(msg):
        messages.append(msg)

    await app(scope, _dummy_receive, send)
    return messages


# =============================================================================
# RequestIdMiddleware
# =============================================================================


class TestRequestIdMiddleware:
    @pytest.mark.asyncio
    async def test_adds_request_id_header(self):
        app = RequestIdMiddleware(_echo_app())
        msgs = await _collect_response(app, _make_scope())

        start = msgs[0]
        headers = dict(start["headers"])
        assert b"x-request-id" in headers
        rid = headers[b"x-request-id"].decode()
        assert len(rid) == 12  # hex[:12]

    @pytest.mark.asyncio
    async def test_request_id_available_in_contextvar(self):
        captured_id = None

        async def capture_app(scope, receive, send):
            nonlocal captured_id
            captured_id = current_request_id.get()
            await _echo_app()(scope, receive, send)

        app = RequestIdMiddleware(capture_app)
        await _collect_response(app, _make_scope())

        assert captured_id is not None
        assert captured_id != "-"
        assert len(captured_id) == 12

    @pytest.mark.asyncio
    async def test_different_requests_get_different_ids(self):
        ids = []

        async def capture_app(scope, receive, send):
            ids.append(current_request_id.get())
            await _echo_app()(scope, receive, send)

        app = RequestIdMiddleware(capture_app)
        await _collect_response(app, _make_scope())
        await _collect_response(app, _make_scope())

        assert len(ids) == 2
        assert ids[0] != ids[1]

    @pytest.mark.asyncio
    async def test_passthrough_non_http(self):
        called = False

        async def inner(scope, receive, send):
            nonlocal called
            called = True

        app = RequestIdMiddleware(inner)
        await app({"type": "websocket"}, _dummy_receive, lambda m: None)
        assert called


# =============================================================================
# MetricsMiddleware
# =============================================================================


class TestMetricsMiddleware:
    @pytest.mark.asyncio
    async def test_counts_requests(self):
        app = MetricsMiddleware(_echo_app())
        await _collect_response(app, _make_scope("/mcp"))
        await _collect_response(app, _make_scope("/mcp"))
        await _collect_response(app, _make_scope("/api/spaces"))

        assert app._request_count["/mcp"] == 2
        assert app._request_count["/api/spaces"] == 1

    @pytest.mark.asyncio
    async def test_counts_errors(self):
        app = MetricsMiddleware(_echo_app(status=500))
        await _collect_response(app, _make_scope("/mcp"))

        assert app._error_count["/mcp"] == 1

    @pytest.mark.asyncio
    async def test_tracks_latency(self):
        app = MetricsMiddleware(_echo_app())
        await _collect_response(app, _make_scope("/mcp"))

        assert app._latency_ms["/mcp"] > 0

    @pytest.mark.asyncio
    async def test_normalizes_mcp_paths(self):
        """All /mcp* paths should bucket to /mcp."""
        app = MetricsMiddleware(_echo_app())
        await _collect_response(app, _make_scope("/mcp"))
        await _collect_response(app, _make_scope("/mcp/session"))

        assert app._request_count["/mcp"] == 2

    @pytest.mark.asyncio
    async def test_metrics_endpoint_json(self):
        app = MetricsMiddleware(_echo_app())
        # Generate some traffic first
        await _collect_response(app, _make_scope("/mcp"))

        # Request /metrics with JSON accept
        scope = _make_scope("/metrics", headers=[(b"accept", b"application/json")])
        msgs = await _collect_response(app, scope)

        body = msgs[1]["body"]
        data = json.loads(body)
        assert "uptime_seconds" in data
        assert "total_requests" in data
        assert "by_path" in data

    @pytest.mark.asyncio
    async def test_metrics_endpoint_prometheus(self):
        app = MetricsMiddleware(_echo_app())
        await _collect_response(app, _make_scope("/mcp"))

        # Default (no Accept header) → Prometheus format
        scope = _make_scope("/metrics")
        msgs = await _collect_response(app, scope)

        body = msgs[1]["body"].decode()
        assert "livemem_requests_total" in body
        assert "livemem_uptime_seconds" in body

    @pytest.mark.asyncio
    async def test_status_code_distribution(self):
        app = MetricsMiddleware(_echo_app(status=200))
        await _collect_response(app, _make_scope("/test"))

        assert app._status_codes[200] == 1


# =============================================================================
# ResponseLimitMiddleware
# =============================================================================


class TestResponseLimitMiddleware:
    @pytest.mark.asyncio
    async def test_small_response_passes_through(self):
        body = b'{"status": "ok"}'
        app = ResponseLimitMiddleware(_echo_app(body=body), max_bytes=1024)
        msgs = await _collect_response(app, _make_scope())

        resp_body = msgs[1]["body"]
        assert resp_body == body

    @pytest.mark.asyncio
    async def test_large_response_truncated(self):
        body = b"x" * 2000
        app = ResponseLimitMiddleware(
            _echo_app(body=body, content_type=b"application/octet-stream"),
            max_bytes=1024,
        )
        msgs = await _collect_response(app, _make_scope())

        resp_body = msgs[1]["body"]
        assert len(resp_body) <= 1024

    @pytest.mark.asyncio
    async def test_truncated_json_replaced_with_error(self):
        """When a JSON response exceeds the limit, the body is replaced
        with a structured truncation notice (not raw truncated bytes)."""
        data = {"items": ["x" * 500] * 10}
        body = json.dumps(data).encode()
        app = ResponseLimitMiddleware(_echo_app(body=body), max_bytes=1024)
        msgs = await _collect_response(app, _make_scope())

        resp_body = json.loads(msgs[1]["body"])
        assert resp_body["_truncated"] is True
        assert "_message" in resp_body

    @pytest.mark.asyncio
    async def test_truncated_header_present(self):
        body = b"x" * 2000
        app = ResponseLimitMiddleware(
            _echo_app(body=body, content_type=b"application/octet-stream"),
            max_bytes=1024,
        )
        msgs = await _collect_response(app, _make_scope())

        headers = dict(msgs[0]["headers"])
        assert headers.get(b"x-response-truncated") == b"true"

    @pytest.mark.asyncio
    async def test_exactly_at_limit(self):
        body = b"x" * 1024
        app = ResponseLimitMiddleware(
            _echo_app(body=body, content_type=b"application/octet-stream"),
            max_bytes=1024,
        )
        msgs = await _collect_response(app, _make_scope())

        resp_body = msgs[1]["body"]
        assert len(resp_body) == 1024
        headers = dict(msgs[0]["headers"])
        assert b"x-response-truncated" not in headers

    @pytest.mark.asyncio
    async def test_excluded_path_bypasses_limit(self):
        """MCP responses (/mcp) should not be truncated."""
        body = b"x" * 2000
        app = ResponseLimitMiddleware(
            _echo_app(body=body, content_type=b"application/octet-stream"),
            max_bytes=1024,
            exclude_paths=("/mcp",),
        )
        msgs = await _collect_response(app, _make_scope("/mcp"))

        resp_body = msgs[1]["body"]
        assert len(resp_body) == 2000


# =============================================================================
# AuditMiddleware
# =============================================================================


class TestAuditMiddleware:
    @pytest.mark.asyncio
    async def test_skips_health_path(self):
        logged = []
        app = AuditMiddleware(_echo_app())

        # Monkey-patch audit logger
        import live_mem.middleware as mw

        orig = mw.audit_logger.info
        mw.audit_logger.info = lambda msg: logged.append(msg)
        try:
            await _collect_response(app, _make_scope("/health"))
        finally:
            mw.audit_logger.info = orig

        assert len(logged) == 0

    @pytest.mark.asyncio
    async def test_logs_mcp_requests(self):
        logged = []
        app = AuditMiddleware(_echo_app())

        import live_mem.middleware as mw

        orig = mw.audit_logger.info
        mw.audit_logger.info = lambda msg: logged.append(msg)
        try:
            await _collect_response(app, _make_scope("/mcp", method="POST"))
        finally:
            mw.audit_logger.info = orig

        assert len(logged) == 1
        entry = json.loads(logged[0])
        assert entry["method"] == "POST"
        assert entry["path"] == "/mcp"
        assert entry["event"] == "request"
        assert "latency_ms" in entry

    @pytest.mark.asyncio
    async def test_skips_static_paths(self):
        logged = []
        app = AuditMiddleware(_echo_app())

        import live_mem.middleware as mw

        orig = mw.audit_logger.info
        mw.audit_logger.info = lambda msg: logged.append(msg)
        try:
            await _collect_response(app, _make_scope("/static/app.js"))
            await _collect_response(app, _make_scope("/metrics"))
            await _collect_response(app, _make_scope("/favicon.ico"))
        finally:
            mw.audit_logger.info = orig

        assert len(logged) == 0
