# -*- coding: utf-8 -*-
"""
Outbound-egress proxy helpers for the embedded Graph Memory runtime.

LOCAL MODIFICATION to the vendored Graph Memory runtime (P12-3, Hivemind #268 —
see THIRD_PARTY_NOTICES.md). The vendored v3.2.0 baseline had no ``PROXY_URL``
support; this module gives the embedded service the same outbound-proxy
contract as the Hivemind core.

Frozen external-vs-internal classification (never runtime DNS/IP heuristics):

- proxied when ``PROXY_URL`` is set: the ExtractorService and EmbeddingService
  LLM clients (including the provider-health probes of ``system_health``),
  both document-storage botocore clients, and the shared token-store S3
  reader;
- always direct: Neo4j (bolt), Qdrant, the Hivemind→GM MCP bridge, the local
  health surface, and every unclassified library. That guarantee is
  structural: this module never exports ``HTTP_PROXY``/``HTTPS_PROXY``-style
  environment variables, and the proxy is injected only at the classified
  client construction sites.

Design constraints:

- **Import-light.** Top level imports only stdlib; ``httpx`` is imported
  lazily inside :func:`build_owned_async_http_client` so auth/storage modules
  (and their unit tests) keep importing cleanly in environments without the
  full inference stack.
- **Credential hygiene.** ``PROXY_URL`` is potentially credential-bearing
  (``http://user:password@host:port``). :func:`display_proxy_url` renders the
  origin only, and :func:`redact_proxy_secrets` strips userinfo and query
  strings from arbitrary messages before they reach logs, health output, or
  client-facing errors.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

# Any http(s) URL substring inside a free-form message. Conservative on
# purpose: a URL token ends at whitespace or a quote, which is how proxy and
# storage libraries embed URLs in their exception text.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+")
# userinfo between the scheme separator and the LAST '@' of the authority:
# URL parsers treat the final '@' as the delimiter, so a password containing
# raw '@' characters (R5) must be stripped through the last one — the greedy
# `[^/\s]*` backtracks to the last '@' before any path segment.
_USERINFO_RE = re.compile(r"^(https?://)[^/\s]*@")


def redact_proxy_errors_async(func):
    """Rewrite an escaping exception's message when redaction changes it.

    Shared boundary decorator for the classified egress clients (S3 storage,
    extraction, embeddings): botocore ``ProxyConnectionError`` embeds the raw
    proxy URL, and a provider/proxy error body relayed by the OpenAI client
    may embed it too. ``e.args`` is rewritten only when
    :func:`redact_proxy_secrets` changes the text — exception type, traceback,
    and attributes (e.g. ``ClientError.response``) are preserved, the nominal
    path is untouched, and the exception always propagates (fail-closed).
    Downstream consumers (server handlers, ingestion pipeline, async-job
    serialization) format ``str(e)`` and therefore inherit the sanitized text.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            redacted = redact_proxy_secrets(str(e))
            if redacted != str(e):
                e.args = (redacted,)
            raise

    return wrapper


def botocore_proxies(proxy_url: Optional[str]) -> Optional[dict]:
    """botocore/requests-style proxies mapping for a configured proxy.

    Returns ``None`` when no proxy is configured so callers can omit the
    ``proxies`` key entirely and keep the direct baseline byte-compatible.
    """
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_owned_async_http_client(proxy_url: str, timeout):
    """Owned ``httpx.AsyncClient`` routing every request through the proxy.

    The caller owns the returned client's lifecycle (same convention as the
    Hivemind core consolidator/probe clients): AsyncOpenAI does not take
    ownership of an injected ``http_client``, so the owning service must close
    it on construction failure and at service shutdown.
    """
    import httpx  # lazy: keep this module import-light for auth/storage tests

    return httpx.AsyncClient(
        proxy=httpx.Proxy(url=proxy_url),
        timeout=timeout,
    )


# Strong references to scheduled close tasks: without them a pending
# ``create_task`` result may be garbage-collected before it runs.
_PENDING_CLOSE_TASKS: set = set()


def close_owned_client_from_sync(client) -> None:
    """Close an owned async client from a *sync* constructor failure path.

    Outside any event loop the close is executed to completion; inside a
    running loop (service constructed lazily from an async tool handler) it is
    scheduled on that loop — ``__init__`` cannot await.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.aclose())
    else:
        task = loop.create_task(client.aclose())
        _PENDING_CLOSE_TASKS.add(task)
        task.add_done_callback(_PENDING_CLOSE_TASKS.discard)


def display_proxy_url(proxy_url: str) -> str:
    """Log-safe rendering of a proxy URL: scheme://host[:port] only.

    Userinfo, path, query, and fragment are dropped — never log the raw
    ``PROXY_URL`` value.
    """
    parts = urlsplit(proxy_url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _redact_one_url(match: re.Match) -> str:
    url = match.group(0)
    # Strip any query string AND fragment (presigned-URL signatures, tokens —
    # R4: a valid PROXY_URL may carry a credential in its fragment too).
    url = re.split(r"[?#]", url, maxsplit=1)[0]
    # Strip userinfo from the authority.
    return _USERINFO_RE.sub(r"\1", url)


def redact_proxy_secrets(text: str) -> str:
    """Strip credentials from every http(s) URL embedded in ``text``.

    Removes URL userinfo (``user:password@``) and query strings while keeping
    the scheme/host/port/path so messages stay actionable. Idempotent and a
    no-op on secret-free text; safe to apply at every outward choke point
    (health output, storage exceptions, log lines).
    """
    return _URL_IN_TEXT_RE.sub(_redact_one_url, text)
