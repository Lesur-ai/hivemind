# -*- coding: utf-8 -*-
"""Bounded, process-local admin-console audit snapshot (P8-6 / G1).

This module is deliberately *not* an audit authority.  It keeps a best-effort
in-memory window for the local admin console only: no S3/file writes, no
backup/recovery semantics, no mesh journal, and no Graph/long-memory path.
The current server runs a single uvicorn worker; deployments with multiple
workers or replicas therefore have one independent ring per process.

The capture set is closed to the four existing console/auth events below.
Request, bulk-token-service, and volatile-long events are intentionally not
fed here: they either flood the window or carry fields that do not belong in
the console view.  ``record_event`` is a hard never-raise boundary because two
of its middleware call sites are outside any surrounding exception handler.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from itertools import islice
from typing import Any

from ..config import get_settings


CAPTURED_EVENTS = frozenset(
    {"auth_rejected", "admin_tool_call", "login_failed", "login_success"}
)
AUDIT_SCOPE_NOTE = (
    "Per-instance in-memory buffer: console and auth events only, since last "
    "restart, best-effort. Not a persistent or complete audit trail."
)

MAX_ENTRY_JSON_BYTES = 900
MAX_ARGUMENT_KEYS = 16
MAX_ARGUMENT_KEY_JSON_BYTES = 32
MAX_TOOL_JSON_BYTES = 64
MAX_CLIENT_JSON_BYTES = 64
MAX_AUTH_TYPE_JSON_BYTES = 24

_SAFE_ARGUMENT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_LIKE_KEY_PREFIXES = ("lm_", "sk_", "ghp_", "github_pat_")
_SECRET_LIKE_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "secret_key",
        "token",
    }
)
_UNKNOWN_TOOL = "[unknown/redacted]"
_REDACTED_KEY = "[redacted key]"

_ring: deque[dict[str, Any]] | None = None


def _get_ring() -> deque[dict[str, Any]]:
    global _ring
    if _ring is None:
        _ring = deque(maxlen=get_settings().admin_audit_ring_size)
    return _ring


def _normalized_text(value: object) -> str:
    """Return text that cannot contain control/format/surrogate spoofing."""

    text = str(value)
    return "".join(
        "�" if unicodedata.category(char).startswith("C") else char
        for char in text
    )


def _json_content_size(char_or_text: str) -> int:
    encoded = json.dumps(char_or_text, ensure_ascii=False)
    return len(encoded[1:-1].encode("utf-8"))


def _clip_json_text(value: object, budget: int) -> str:
    """Clip on code-point boundaries using JSON-content UTF-8 byte cost."""

    text = _normalized_text(value)
    parts: list[str] = []
    costs: list[int] = []
    used = 0
    clipped = False

    for char in text:
        cost = _json_content_size(char)
        if used + cost > budget:
            clipped = True
            break
        parts.append(char)
        costs.append(cost)
        used += cost

    if not clipped:
        return text

    ellipsis = "…"
    ellipsis_cost = _json_content_size(ellipsis)
    while parts and used + ellipsis_cost > budget:
        parts.pop()
        used -= costs.pop()
    return "".join(parts) + ellipsis


def _known_tool_label(value: object | None) -> str | None:
    if value is None:
        return None
    text = _normalized_text(value)

    # Lazy import avoids a core<->auth import cycle at module import time.
    try:
        from ..auth.context import MonoTenantSpaceAllowlistProvider

        known = text in MonoTenantSpaceAllowlistProvider.ALLOWED_ACTIONS
    except Exception:
        known = False
    if not known:
        return _UNKNOWN_TOOL
    return _clip_json_text(text, MAX_TOOL_JSON_BYTES)


def _safe_argument_key(value: object) -> str:
    text = _normalized_text(value)
    lowered = text.lower()
    if (
        not _SAFE_ARGUMENT_KEY.fullmatch(text)
        or lowered.startswith(_SECRET_LIKE_KEY_PREFIXES)
        or lowered in _SECRET_LIKE_KEYS
        or lowered.endswith(("_password", "_secret", "_credential"))
    ):
        return _REDACTED_KEY
    return _clip_json_text(text, MAX_ARGUMENT_KEY_JSON_BYTES)


def _overflow_marker(total: int, kept: int) -> str:
    return _clip_json_text(f"+{max(0, total - kept)} more", 24)


def _entry_size(entry: dict[str, Any]) -> int:
    # Match StaticFilesMiddleware._send_json exactly.
    return len(
        json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8")
    )


def record_event(
    *,
    event: str,
    tool: object | None = None,
    arguments: Mapping[object, object] | None = None,
    client: object | None = None,
    auth_type: object | None = None,
) -> None:
    """Append one redacted event without ever affecting request processing."""

    try:
        if event not in CAPTURED_EVENTS:
            return

        argument_total = 0
        real_keys: list[str] = []
        if event == "admin_tool_call" and isinstance(arguments, Mapping):
            argument_total = len(arguments)
            real_keys = [
                _safe_argument_key(key)
                for key in islice(arguments.keys(), MAX_ARGUMENT_KEYS)
            ]

        def arguments_with_marker() -> list[str] | None:
            if event != "admin_tool_call" or not isinstance(arguments, Mapping):
                return None
            values = list(real_keys)
            if argument_total > len(real_keys):
                values.append(_overflow_marker(argument_total, len(real_keys)))
            return values

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
            "tool": _known_tool_label(tool) if event == "admin_tool_call" else None,
            "arguments_keys": arguments_with_marker(),
            "client": (
                _clip_json_text(client, MAX_CLIENT_JSON_BYTES)
                if client is not None
                else None
            ),
            "auth_type": (
                _clip_json_text(auth_type, MAX_AUTH_TYPE_JSON_BYTES)
                if auth_type is not None
                else None
            ),
        }

        # Final serialized-size guard.  Preserve all six normative fields and
        # shrink only the bounded argument-key list.
        while _entry_size(entry) > MAX_ENTRY_JSON_BYTES and real_keys:
            real_keys.pop()
            entry["arguments_keys"] = arguments_with_marker()

        if _entry_size(entry) > MAX_ENTRY_JSON_BYTES:
            return
        _get_ring().append(entry)
    except Exception:
        # Audit capture is best-effort and must never break authentication or
        # an admin request, including for hostile Mapping implementations.
        return


def snapshot() -> list[dict[str, Any]]:
    """Return an oldest-to-newest deep copy of the current process window."""

    return deepcopy(list(_get_ring()))


def capacity() -> int:
    """Return the configured maximum entry count for this process."""

    return int(_get_ring().maxlen or 0)


def reset_for_tests() -> None:
    """Clear lazy module state; production code never calls this seam."""

    global _ring
    _ring = None
