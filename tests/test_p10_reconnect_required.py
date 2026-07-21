"""P10-1 response contract for permission-aware MCP discovery refreshes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from live_mem.core.models import TokenInfo, TokensStore
from live_mem.core.tokens import TokenService


def _token(
    name: str = "target",
    hash_char: str = "a",
    *,
    permissions: list[str] | None = None,
    space_ids: list[str] | None = None,
    email: str = "old@example.test",
) -> TokenInfo:
    return TokenInfo(
        hash="sha256:" + hash_char * 64,
        name=name,
        permissions=list(permissions or ["read"]),
        space_ids=list(["alpha"] if space_ids is None else space_ids),
        email=email,
        created_at="2026-07-15T00:00:00+00:00",
    )


async def _update(token: TokenInfo, **changes: str) -> dict:
    service = TokenService()
    store = TokensStore(tokens=[token])
    with (
        patch.object(service, "_load_store", new=AsyncMock(return_value=store)),
        patch.object(service, "_save_store", new=AsyncMock()),
    ):
        return await service.update_token(token_hash=token.hash, **changes)


async def _bulk_update(
    token: TokenInfo,
    *,
    names: str = "target",
    **changes: str,
) -> dict:
    service = TokenService()
    store = TokensStore(tokens=[token])
    with (
        patch.object(service, "_load_store", new=AsyncMock(return_value=store)),
        patch.object(service, "_save_store", new=AsyncMock()),
    ):
        return await service.bulk_update_tokens(names=names, **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": "read,write"},
        {"space_ids_add": "beta"},
    ],
    ids=["permissions", "scope"],
)
async def test_update_token_requests_reconnect_after_effective_auth_change(
    changes: dict[str, str],
) -> None:
    result = await _update(_token(), **changes)

    assert result["status"] == "ok"
    assert result["mcp_reconnect_required"] is True


@pytest.mark.asyncio
async def test_update_token_email_only_does_not_request_reconnect() -> None:
    result = await _update(_token(), email="new@example.test")

    assert result["status"] == "ok"
    assert "mcp_reconnect_required" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": "read"},
        {"space_ids": "alpha"},
        {"space_ids_add": "alpha"},
    ],
    ids=["same-permissions", "same-scope", "idempotent-scope-add"],
)
async def test_update_token_noop_does_not_request_reconnect(
    changes: dict[str, str],
) -> None:
    result = await _update(_token(), **changes)

    assert result["status"] == "ok"
    assert "mcp_reconnect_required" not in result


@pytest.mark.asyncio
async def test_update_token_reordering_rights_does_not_request_reconnect() -> None:
    permission_order = await _update(
        _token(permissions=["read", "write"]),
        permissions="write,read",
    )
    scope_order = await _update(
        _token(space_ids=["alpha", "beta"]),
        space_ids="beta,alpha",
    )

    assert "mcp_reconnect_required" not in permission_order
    assert "mcp_reconnect_required" not in scope_order


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": "read,write"},
        {"space_ids_add": "beta"},
    ],
    ids=["permissions", "scope"],
)
async def test_bulk_update_requests_reconnect_after_effective_auth_change(
    changes: dict[str, str],
) -> None:
    result = await _bulk_update(_token(), **changes)

    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert result["mcp_reconnect_required"] is True


@pytest.mark.asyncio
async def test_bulk_update_email_only_does_not_request_reconnect() -> None:
    result = await _bulk_update(_token(), email="new@example.test")

    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert "mcp_reconnect_required" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": "read"},
        {"space_ids_add": "alpha"},
    ],
    ids=["same-permissions", "idempotent-scope-add"],
)
async def test_bulk_update_noop_does_not_request_reconnect(
    changes: dict[str, str],
) -> None:
    result = await _bulk_update(_token(), **changes)

    assert result["status"] == "ok"
    assert result["updated"] == 1
    assert "mcp_reconnect_required" not in result


@pytest.mark.asyncio
async def test_bulk_update_reordering_rights_does_not_request_reconnect() -> None:
    permission_order = await _bulk_update(
        _token(permissions=["read", "write"]),
        permissions="write,read",
    )
    scope_order = await _bulk_update(
        _token(space_ids=["alpha", "beta"]),
        space_ids_remove="alpha",
        space_ids_add="alpha",
    )

    assert "mcp_reconnect_required" not in permission_order
    assert "mcp_reconnect_required" not in scope_order


@pytest.mark.asyncio
async def test_bulk_update_zero_targets_does_not_request_reconnect() -> None:
    result = await _bulk_update(
        _token(),
        names="does-not-exist",
        permissions="read,write",
    )

    assert result["status"] == "ok"
    assert result["updated"] == 0
    assert "mcp_reconnect_required" not in result


@pytest.mark.asyncio
async def test_invite_requests_reconnect_only_when_scope_is_added() -> None:
    service = TokenService()
    manager = _token(
        "manager",
        "a",
        permissions=["manage"],
        space_ids=["alpha"],
    )
    target = _token(
        "target",
        "b",
        permissions=["read"],
        space_ids=[],
    )
    store = TokensStore(tokens=[manager, target])

    with (
        patch("live_mem.core.tokens.get_storage", return_value=object()),
        patch(
            "live_mem.core.space.SpaceService.classify_committed_state",
            new=AsyncMock(return_value=("committed", "")),
        ),
        patch.object(service, "_load_store", new=AsyncMock(return_value=store)),
        patch.object(service, "_save_store", new=AsyncMock()),
        patch.object(service, "_invalidate_in_fresh_store"),
        patch.object(service, "_emit_delegated_access_audit"),
    ):
        added = await service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target.hash,
        )
        idempotent = await service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target.hash,
        )

    assert added["status"] == "ok"
    assert added["added"] is True
    assert added["mcp_reconnect_required"] is True
    assert idempotent == {"status": "ok", "space_id": "alpha", "added": False}
