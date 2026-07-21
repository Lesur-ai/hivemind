"""Behavioral coverage for the console's session-identity boundary.

Codex round-7 re-review (PR #159, HIGH): the admin console binds its stale-bank
cache to a per-session owner marker. The only UNIQUE session identity is the
authenticated ``token_hash`` — ``client_name`` is explicitly non-unique. The
server therefore MUST surface ``token_hash`` for token auth independently of the
best-effort token-store enrichment; otherwise a store failure would strip the
only unique identifier and let two same-named sessions be equated (leaking the
first token's stale rows to the second operator).

These are executable tests (not source pins): they invoke ``system_whoami`` with
a real auth context and a deliberately failing / empty token store.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_fresh_auth_state():
    """P10-1 whoami now reads the same fresh state as authorization guards."""
    import live_mem.auth.context as auth_context

    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()
    yield
    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()


def _whoami_fn():
    """Register system tools on a lightweight capture-mcp and return whoami."""
    from live_mem.tools import system

    captured = {}

    class _CaptureMCP:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    system.register(_CaptureMCP())
    return captured["system_whoami"]


TOKEN_HASH = "sha256:" + "d" * 64


def _token_info(client_name="shared-name"):
    return {
        "type": "token",
        "client_name": client_name,
        "permissions": ["read", "write", "manage"],
        "allowed_resources": [],
        "token_hash": TOKEN_HASH,
    }


@pytest.mark.asyncio
async def test_whoami_exposes_token_hash_when_store_enrichment_raises():
    """Enrichment failure (store unavailable) must NOT strip token_hash."""
    from live_mem.auth.context import current_token_info

    whoami = _whoami_fn()
    svc = MagicMock()
    svc.list_tokens = AsyncMock(side_effect=RuntimeError("token store down"))

    tok = current_token_info.set(_token_info())
    try:
        with patch("live_mem.core.tokens.get_token_service", return_value=svc):
            result = await whoami()
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "ok"
    assert result["token_hash"] == TOKEN_HASH, (
        "token_hash must be present for token auth even when store enrichment "
        "fails — it is the console's only unique session identity"
    )


@pytest.mark.asyncio
async def test_whoami_exposes_token_hash_when_token_absent_from_store():
    """Token missing from the store (no enrichment match) still yields token_hash."""
    from live_mem.auth.context import current_token_info

    whoami = _whoami_fn()
    svc = MagicMock()
    svc.list_tokens = AsyncMock(return_value={"tokens": []})

    tok = current_token_info.set(_token_info())
    try:
        with patch("live_mem.core.tokens.get_token_service", return_value=svc):
            result = await whoami()
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "ok"
    assert result["token_hash"] == TOKEN_HASH


@pytest.mark.asyncio
async def test_two_same_named_tokens_have_distinct_hashes():
    """The unique marker distinguishes two sessions that share a client_name."""
    from live_mem.auth.context import current_token_info

    whoami = _whoami_fn()
    svc = MagicMock()
    svc.list_tokens = AsyncMock(return_value={"tokens": []})

    hashes = []
    for h in ("sha256:" + "a" * 64, "sha256:" + "b" * 64):
        info = _token_info(client_name="shared-name")
        info["token_hash"] = h
        tok = current_token_info.set(info)
        try:
            with patch("live_mem.core.tokens.get_token_service", return_value=svc):
                result = await whoami()
        finally:
            current_token_info.reset(tok)
        assert result["client_name"] == "shared-name"
        hashes.append(result["token_hash"])

    assert hashes[0] != hashes[1], (
        "two distinct tokens sharing a client_name must be told apart by "
        "token_hash — this is what the console owner marker relies on"
    )
