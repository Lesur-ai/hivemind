# -*- coding: utf-8 -*-
"""
Production middleware stack for Live Memory MCP Server.

ASGI middlewares for observability, safety, and audit:
    - RequestIdMiddleware     — unique correlation ID per request (contextvars)
    - MetricsMiddleware       — per-path request counts, error rates, latency
    - ResponseLimitMiddleware — truncates oversized responses (default 512 KB)
    - AuditMiddleware         — structured audit trail (who, what, when)

These are layered on top of the existing auth/logging/static middleware.
"""

import json
import time
import uuid
import logging
from collections import defaultdict
from contextvars import ContextVar

from .auth.context import current_token_info

# ─────────────────────────────────────────────────────────────
# Shared context: request ID accessible from any async context
# ─────────────────────────────────────────────────────────────
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")

logger = logging.getLogger("live_mem.middleware")
audit_logger = logging.getLogger("live_mem.audit")


# =============================================================================
# RequestIdMiddleware
# =============================================================================


class RequestIdMiddleware:
    """
    Generates a unique request ID (UUID4 short) for every HTTP request
    and stores it in a ContextVar for downstream correlation.

    The ID is also injected as an ``X-Request-Id`` response header so
    clients can reference it in bug reports.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = uuid.uuid4().hex[:12]
        tok = current_request_id.set(request_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            current_request_id.reset(tok)


# =============================================================================
# MetricsMiddleware
# =============================================================================


class MetricsMiddleware:
    """
    Tracks per-path request counts, error counts, and cumulative latency.

    Exposes ``/metrics`` endpoint in Prometheus exposition format (default)
    or JSON (``Accept: application/json``).
    """

    def __init__(self, app):
        self.app = app
        self._start_time = time.monotonic()
        # Counters: path -> count
        self._request_count: dict[str, int] = defaultdict(int)
        self._error_count: dict[str, int] = defaultdict(int)
        # Latency: path -> cumulative ms
        self._latency_ms: dict[str, float] = defaultdict(float)
        # Status code distribution
        self._status_codes: dict[int, int] = defaultdict(int)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Serve metrics endpoint
        if path == "/metrics":
            return await self._serve_metrics(scope, send)

        # HM-02 fix : bucketiser sur un ENSEMBLE FIXE de labels templatisés au
        # lieu du path brut. Deux problèmes réglés :
        #  1. DoS mémoire : un attaquant bouclant sur des paths distincts
        #     (/aaaa, /aaab, …) créait des clés PERMANENTES dans des dicts jamais
        #     purgés → croissance RSS non bornée jusqu'à l'OOM.
        #  2. Fuite d'info : les space-ids/filenames réels (ex
        #     /api/bank/<space>/<file>) fuyaient verbatim comme labels sur
        #     /metrics (public). La templatisation les remplace par /api/bank/*.
        # Combiné au déplacement du middleware APRÈS l'auth (server.py), la
        # cardinalité non authentifiée est éliminée.
        bucket = self._bucket(path)
        self._request_count[bucket] += 1

        t0 = time.monotonic()
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.monotonic() - t0) * 1000
            self._latency_ms[bucket] += elapsed
            self._status_codes[status_code] += 1
            if status_code >= 400:
                self._error_count[bucket] += 1

    # HM-02 : ensemble FIXE de paths connus conservés tels quels comme labels.
    # Tout le reste est templatisé ou collapsé, pour borner la cardinalité.
    _KNOWN_PATHS = frozenset(
        {
            "/metrics",
            "/health",
            "/live",
            "/live/",
            "/admin",
            "/admin/",
            "/favicon.ico",
            "/api/spaces",
            "/api/tool",
            "/api/login",
            "/api/logout",
        }
    )
    _TEMPLATE_PREFIXES = ("/api/space/", "/api/live/", "/api/bank/", "/static/")

    def _bucket(self, path: str) -> str:
        """Réduit un path à un label de métrique borné (HM-02)."""
        if path.startswith("/mcp"):
            return "/mcp"
        if path in self._KNOWN_PATHS:
            return path
        for prefix in self._TEMPLATE_PREFIXES:
            if path.startswith(prefix):
                return prefix + "*"
        # Paths inconnus / 404 (potentiellement attaquant) → bucket fixe unique.
        return "__other__"

    async def _serve_metrics(self, scope, send):
        """Serve /metrics in Prometheus or JSON format."""
        headers = dict(scope.get("headers", []))
        accept = headers.get(b"accept", b"").decode()
        uptime = round(time.monotonic() - self._start_time, 1)

        total_requests = sum(self._request_count.values())
        total_errors = sum(self._error_count.values())

        if "application/json" in accept:
            data = {
                "uptime_seconds": uptime,
                "total_requests": total_requests,
                "total_errors": total_errors,
                "by_path": {
                    path: {
                        "requests": self._request_count[path],
                        "errors": self._error_count[path],
                        "latency_ms_total": round(self._latency_ms[path], 1),
                        "latency_ms_avg": round(
                            self._latency_ms[path] / self._request_count[path], 1
                        )
                        if self._request_count[path]
                        else 0,
                    }
                    for path in sorted(self._request_count)
                },
                "status_codes": dict(sorted(self._status_codes.items())),
            }
            body = json.dumps(data).encode()
            ct = b"application/json"
        else:
            # Prometheus exposition format
            lines = []
            lines.append("# HELP livemem_uptime_seconds Server uptime in seconds")
            lines.append("# TYPE livemem_uptime_seconds gauge")
            lines.append(f"livemem_uptime_seconds {uptime}")
            lines.append("")
            lines.append("# HELP livemem_requests_total Total HTTP requests")
            lines.append("# TYPE livemem_requests_total counter")
            for path in sorted(self._request_count):
                safe = path.replace('"', '\\"')
                lines.append(
                    f'livemem_requests_total{{path="{safe}"}} {self._request_count[path]}'
                )
            lines.append("")
            lines.append("# HELP livemem_errors_total Total HTTP errors (4xx+5xx)")
            lines.append("# TYPE livemem_errors_total counter")
            for path in sorted(self._error_count):
                safe = path.replace('"', '\\"')
                lines.append(
                    f'livemem_errors_total{{path="{safe}"}} {self._error_count[path]}'
                )
            lines.append("")
            lines.append("# HELP livemem_latency_ms_total Cumulative latency in ms")
            lines.append("# TYPE livemem_latency_ms_total counter")
            for path in sorted(self._latency_ms):
                safe = path.replace('"', '\\"')
                lines.append(
                    f'livemem_latency_ms_total{{path="{safe}"}} {round(self._latency_ms[path], 1)}'
                )
            lines.append("")
            lines.append("# HELP livemem_http_status_total Responses by status code")
            lines.append("# TYPE livemem_http_status_total counter")
            for code in sorted(self._status_codes):
                lines.append(
                    f'livemem_http_status_total{{code="{code}"}} {self._status_codes[code]}'
                )
            lines.append("")
            body = "\n".join(lines).encode()
            ct = b"text/plain; version=0.0.4; charset=utf-8"

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", ct),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# =============================================================================
# ResponseLimitMiddleware
# =============================================================================


class ResponseLimitMiddleware:
    """
    Truncates HTTP response bodies that exceed ``max_bytes`` (default 512 KB).

    Adds ``X-Response-Truncated: true`` header when truncation occurs.
    Prevents oversized MCP tool responses from crashing clients.
    """

    def __init__(
        self,
        app,
        *,
        max_bytes: int = 512 * 1024,
        exclude_paths: tuple[str, ...] = ("/mcp",),
    ):
        self.app = app
        self.max_bytes = max_bytes
        self.exclude_paths = exclude_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self.exclude_paths):
            return await self.app(scope, receive, send)

        body_parts: list[bytes] = []
        total_size = 0
        truncated = False
        headers_sent = False

        async def buffered_send(message):
            nonlocal total_size, truncated, headers_sent

            if message["type"] == "http.response.start":
                # Store start message, we'll send it when we have the body
                buffered_send._start_message = message
                return

            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                more_body = message.get("more_body", False)

                if not truncated:
                    remaining = self.max_bytes - total_size
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                        truncated = True
                        more_body = False

                    total_size += len(chunk)
                    body_parts.append(chunk)

                if not more_body or truncated:
                    # Time to send everything
                    full_body = b"".join(body_parts)

                    start = buffered_send._start_message
                    headers = list(start.get("headers", []))

                    if truncated:
                        # Replace the body with a JSON error instead of
                        # returning an unreadable truncated payload.
                        ct_values = [
                            v.decode() for k, v in headers if k == b"content-type"
                        ]
                        is_json = any("json" in ct for ct in ct_values)

                        if is_json:
                            full_body = json.dumps(
                                {
                                    "_truncated": True,
                                    "_message": (
                                        f"Response exceeded {self.max_bytes} bytes "
                                        f"and was truncated. Use more specific "
                                        f"queries to reduce response size."
                                    ),
                                }
                            ).encode()

                        headers.append((b"x-response-truncated", b"true"))
                        logger.warning(
                            "Response truncated: %s %s (%d bytes > %d limit)",
                            scope.get("method", "?"),
                            scope.get("path", "?"),
                            total_size,
                            self.max_bytes,
                        )

                    # Update content-length
                    headers = [(k, v) for k, v in headers if k != b"content-length"]
                    headers.append((b"content-length", str(len(full_body)).encode()))

                    await send({**start, "headers": headers})
                    await send(
                        {
                            "type": "http.response.body",
                            "body": full_body,
                        }
                    )

        buffered_send._start_message = None
        await self.app(scope, receive, buffered_send)


# =============================================================================
# AuditMiddleware
# =============================================================================


class AuditMiddleware:
    """
    Emits structured audit log entries for MCP tool calls.

    Logs to the ``live_mem.audit`` logger at INFO level as JSON lines.
    Each entry includes: timestamp, request_id, client identity,
    HTTP method, path, status code, and latency.

    Only audits non-public, non-static paths (i.e., /mcp and /api).
    """

    # Paths that don't need auditing
    _SKIP_PATHS = {"/health", "/metrics", "/favicon.ico", "/live", "/live/"}
    _SKIP_PREFIXES = ("/static/",)

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Skip non-interesting paths
        if path in self._SKIP_PATHS:
            return await self.app(scope, receive, send)
        if any(path.startswith(p) for p in self._SKIP_PREFIXES):
            return await self.app(scope, receive, send)

        method = scope.get("method", "?")
        t0 = time.monotonic()
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = round((time.monotonic() - t0) * 1000, 1)

            # Build audit entry
            token_info = current_token_info.get()
            entry = {
                "event": "request",
                "request_id": current_request_id.get(),
                "method": method,
                "path": path,
                "status": status_code,
                "latency_ms": elapsed,
                "client": token_info.get("client_name", "anonymous")
                if token_info
                else "unauthenticated",
                "auth_type": token_info.get("type", "none") if token_info else "none",
                "permissions": token_info.get("permissions", []) if token_info else [],
            }

            # HM-13 fix : IP résolue par le MÊME helper trusted-proxy que
            # AuthMiddleware / LoggingMiddleware (avant, AuditMiddleware lisait le
            # socket brut → les deux flux d'audit divergeaient sur la même
            # requête). Import tardif pour éviter tout cycle au chargement.
            from .auth.middleware import _client_ip_from_scope

            entry["client_ip"] = _client_ip_from_scope(scope)

            audit_logger.info(json.dumps(entry, ensure_ascii=False))
