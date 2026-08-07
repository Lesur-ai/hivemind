# -*- coding: utf-8 -*-
"""Owned outbound-transport helpers for the inference adapters (P12-3 parity).

The statically classified egress contract is preserved verbatim from the
P12-3 baseline: when ``PROXY_URL`` is configured, every chat, embedding, and
discovery request from every adapter uses an explicitly injected owned proxy
transport; a proxy failure fails closed (no direct fallback); and this module
never exports or honors ambient ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``/
``NO_PROXY`` variables. Without ``PROXY_URL`` the historical explicit direct
transport remains.

Import-light: the top level imports only stdlib; ``httpx`` is imported lazily
inside the client builders so auth/storage modules of both consumers keep
importing cleanly.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Any http(s) URL substring inside a free-form message. It ends only at
# whitespace or the ``<``/``>`` framing that brackets the ``<redacted>``
# placeholder (which keeps redaction idempotent). Quote characters are NOT
# terminators: a quote can appear inside URL userinfo or a path (e.g. a
# password ``pa'ss``), and terminating on it would leave the host/port/suffix
# exposed. URI schemes are case-insensitive, so an uppercase ``HTTPS://`` must
# match too. Consuming trailing framing punctuation only over-redacts (safe).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def display_proxy_url(proxy_url: str) -> str:
    """Fully-redacted, log-safe rendering of a proxy URL.

    ADR-0027 classifies the proxy ENDPOINT as sensitive configuration even
    without embedded credentials, so ordinary logs, health, and errors must
    never contain its host or port — not only its userinfo. Only the
    non-sensitive scheme (``http``/``https``) is preserved; the entire authority
    is replaced with a fixed ``<redacted>`` placeholder. A malformed proxy URL
    (whose parse could otherwise embed the configured value in a ValueError)
    still never leaks its value.
    """
    try:
        scheme = (urlsplit(proxy_url).scheme or "").lower()
    except ValueError:
        return "<redacted>"
    # Preserve ONLY a case-normalized http/https scheme; every other or
    # malformed scheme is fully redacted so a data-bearing custom scheme
    # (e.g. a token-like ``credential-token://``) cannot survive the rendering.
    if scheme not in ("http", "https"):
        return "<redacted>"
    return f"{scheme}://<redacted>"


def _redact_one_url(match: "re.Match[str]") -> str:
    # Replace the ENTIRE matched http(s) URL with a scheme-only placeholder.
    # ADR-0027 classifies provider/proxy ENDPOINTS (host and port) as sensitive,
    # so free-form log/exception text must not carry the authority, path, query,
    # fragment, or userinfo — only the non-sensitive scheme survives. Idempotent:
    # the ``<`` in the placeholder ends the URL regex, so a re-run is a no-op.
    scheme = match.group(0).split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        return "<redacted>"
    return f"{scheme}://<redacted>"


def redact_proxy_secrets(text: str) -> str:
    """Fully redact every http(s) URL embedded in ``text`` to ``scheme://<redacted>``.

    ADR-0027 treats provider and proxy endpoints (host and port) as sensitive
    even without embedded credentials, so this strips the ENTIRE authority,
    path, query, fragment, and userinfo from any URL that appears in free-form
    log/exception text — not only the credentials. Idempotent; a no-op on
    secret-free text."""
    return _URL_IN_TEXT_RE.sub(_redact_one_url, text)


def build_owned_async_http_client(proxy_url: str | None, timeout: float):
    """Owned ``httpx.AsyncClient`` — proxied when ``proxy_url`` is set,
    explicit direct otherwise.

    The caller (adapter) owns the returned client's lifecycle: it must close
    it on construction failure and at service shutdown. ``trust_env=False``
    guarantees ambient proxy variables are never honored on either path.
    """
    import httpx  # lazy: keep this module import-light

    if proxy_url:
        return httpx.AsyncClient(
            proxy=httpx.Proxy(url=proxy_url),
            timeout=timeout,
            trust_env=False,
        )
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


# Strong references to scheduled close tasks: without them a pending
# ``create_task`` result may be garbage-collected before it runs.
_PENDING_CLOSE_TASKS: set = set()


def close_owned_client_from_sync(client) -> None:
    """Close an owned async client from a *sync* constructor-failure path."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.aclose())
    else:
        task = loop.create_task(client.aclose())
        _PENDING_CLOSE_TASKS.add(task)
        task.add_done_callback(_PENDING_CLOSE_TASKS.discard)
