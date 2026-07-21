# -*- coding: utf-8 -*-
"""
Tests for the `bank_stale_spaces` MCP tool.

Validates the supervision tool that flags memory banks accumulating
unconsolidated live notes (≥ min_notes notes whose oldest is ≥ min_age_days old).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth.context import current_token_info
from live_mem.tools.bank import _parse_live_note_timestamp, register as register_bank_tools


def _token(name: str, permissions: list[str], allowed: list[str]) -> dict:
    return {
        "client_name": name,
        "permissions": permissions,
        "allowed_resources": allowed,
    }


def _bank_tool(name: str):
    mcp = FastMCP(name="test")
    register_bank_tools(mcp)
    tool = mcp._tool_manager._tools[name]
    for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    raise AssertionError(f"Tool {name} has no callable")


def _note_key(space_id: str, ts: datetime, agent: str = "agent", category: str = "observation") -> dict:
    fname = ts.strftime("%Y%m%dT%H%M%S") + f"_{agent}_{category}_deadbeef.md"
    return {"Key": f"{space_id}/live/{fname}", "Size": 100, "LastModified": ts}


def _make_storage_mock(per_space: dict[str, list[dict]]):
    """Build an AsyncMock storage whose `list_objects(prefix)` returns the right shard."""
    storage = AsyncMock()

    async def _list(prefix: str, max_keys: int = 0):
        for sid, items in per_space.items():
            if prefix == f"{sid}/live/":
                return items
        return []

    storage.list_objects.side_effect = _list
    return storage


# ─────────────────────────────────────────────────────────────
# _parse_live_note_timestamp helper
# ─────────────────────────────────────────────────────────────


def test_parse_timestamp_valid():
    ts = _parse_live_note_timestamp("20260521T180000_CLR_observation_a1b2c3d4.md")
    assert ts == datetime(2026, 5, 21, 18, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_missing_prefix():
    assert _parse_live_note_timestamp("not-a-timestamped-file.md") is None


def test_parse_timestamp_malformed_date():
    # Right shape, wrong values (month 99 is invalid)
    assert _parse_live_note_timestamp("20269921T180000_x_y_z.md") is None


def test_parse_timestamp_short_string():
    assert _parse_live_note_timestamp("short.md") is None


# ─────────────────────────────────────────────────────────────
# bank_stale_spaces tool
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_spaces_requires_auth():
    """No token → auth error, no storage call."""
    tok = current_token_info.set(None)
    try:
        result = await _bank_tool("bank_stale_spaces")()
    finally:
        current_token_info.reset(tok)
    assert result["status"] == "error"
    assert "Authentification" in result["message"]


@pytest.mark.asyncio
async def test_stale_spaces_flags_space_above_thresholds():
    """A space with ≥5 notes and oldest ≥5 days old is flagged stale."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=7)
    items = [_note_key("alpha", old + timedelta(hours=i)) for i in range(6)]

    storage = _make_storage_mock({"alpha": items})

    tok = current_token_info.set(_token("reader", ["read"], ["alpha"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(space_ids="alpha")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "ok"
    assert result["total_stale"] == 1
    assert result["total_spaces"] == 1
    assert result["spaces"][0]["space_id"] == "alpha"
    assert result["spaces"][0]["live_notes_count"] == 6
    # Computed age MUST match the fixture (~7 days), not just clear the
    # threshold. A loose `>= 5` would let a buggy impl returning a
    # constant `5.0` pass — assert the actual value.
    assert 6.9 <= result["spaces"][0]["oldest_note_age_days"] <= 7.1
    assert result["spaces"][0]["is_stale"] is True
    assert result["denied_spaces"] == []


@pytest.mark.asyncio
async def test_stale_spaces_filters_out_recent_notes():
    """A space with enough notes but all RECENT is NOT flagged."""
    now = datetime.now(timezone.utc)
    items = [
        _note_key("recent", now - timedelta(hours=i)) for i in range(10)
    ]
    storage = _make_storage_mock({"recent": items})

    tok = current_token_info.set(_token("r", ["read"], ["recent"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(space_ids="recent")
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 0
    assert result["spaces"] == []
    assert result["scanned"][0]["live_notes_count"] == 10
    assert result["scanned"][0]["is_stale"] is False


@pytest.mark.asyncio
async def test_stale_spaces_filters_out_below_note_threshold():
    """A space with old notes but TOO FEW is NOT flagged."""
    now = datetime.now(timezone.utc)
    items = [
        _note_key("sparse", now - timedelta(days=30 + i)) for i in range(2)
    ]
    storage = _make_storage_mock({"sparse": items})

    tok = current_token_info.set(_token("r", ["read"], ["sparse"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(space_ids="sparse")
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 0
    assert result["spaces"] == []


@pytest.mark.asyncio
async def test_stale_spaces_empty_space_is_scanned_not_stale():
    """A space with 0 live notes appears in `scanned` with is_stale=False."""
    storage = _make_storage_mock({"empty": []})

    tok = current_token_info.set(_token("r", ["read"], ["empty"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(space_ids="empty")
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 0
    assert result["total_spaces"] == 1
    assert result["scanned"][0]["space_id"] == "empty"
    assert result["scanned"][0]["live_notes_count"] == 0
    assert result["scanned"][0]["is_stale"] is False


@pytest.mark.asyncio
async def test_stale_spaces_denied_when_space_not_allowed():
    """A space outside the token's allowed_resources is reported in denied_spaces."""
    storage = _make_storage_mock({})

    tok = current_token_info.set(_token("r", ["read"], ["only-this"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="only-this,forbidden"
            )
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "ok"
    denied_ids = [d["space_id"] for d in result["denied_spaces"]]
    assert "forbidden" in denied_ids
    assert "only-this" not in denied_ids
    # Each denied entry MUST carry both `space_id` AND a non-empty `message`.
    # A bug returning `[{"space_id": "x"}]` (no reason) would mislead operators.
    for entry in result["denied_spaces"]:
        assert "space_id" in entry and entry["space_id"]
        assert "message" in entry and entry["message"], (
            f"denied_spaces entry missing message: {entry}"
        )


@pytest.mark.asyncio
async def test_stale_spaces_custom_thresholds():
    """Lowering thresholds flags spaces that the default wouldn't."""
    now = datetime.now(timezone.utc)
    items = [
        _note_key("borderline", now - timedelta(days=2, hours=i)) for i in range(3)
    ]
    storage = _make_storage_mock({"borderline": items})

    tok = current_token_info.set(_token("r", ["read"], ["borderline"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            default = await _bank_tool("bank_stale_spaces")(space_ids="borderline")
            custom = await _bank_tool("bank_stale_spaces")(
                space_ids="borderline", min_notes=3, min_age_days=1
            )
    finally:
        current_token_info.reset(tok)

    assert default["total_stale"] == 0  # 3 notes < default 5
    assert custom["total_stale"] == 1
    assert custom["min_notes"] == 3
    assert custom["min_age_days"] == 1


@pytest.mark.asyncio
async def test_stale_spaces_sorted_by_count_then_age():
    """Stale list is sorted by notes_count DESC then age DESC."""
    now = datetime.now(timezone.utc)
    storage = _make_storage_mock(
        {
            "alpha": [_note_key("alpha", now - timedelta(days=10)) for _ in range(6)],
            "beta": [_note_key("beta", now - timedelta(days=30)) for _ in range(20)],
            "gamma": [_note_key("gamma", now - timedelta(days=8)) for _ in range(6)],
        }
    )

    tok = current_token_info.set(
        _token("r", ["read"], ["alpha", "beta", "gamma"])
    )
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="alpha,beta,gamma"
            )
    finally:
        current_token_info.reset(tok)

    ordered = [s["space_id"] for s in result["spaces"]]
    # beta first (20 notes), then alpha+gamma (6 notes each) — alpha older than gamma
    assert ordered[0] == "beta"
    assert ordered[1] == "alpha"
    assert ordered[2] == "gamma"


@pytest.mark.asyncio
async def test_stale_spaces_skips_malformed_filenames():
    """Notes with non-conforming filenames are silently skipped (don't count)."""
    now = datetime.now(timezone.utc)
    items = [
        _note_key("alpha", now - timedelta(days=10)),  # valid
        {"Key": "alpha/live/totally-malformed.md"},     # skipped
        {"Key": "alpha/live/.keep"},                    # skipped
    ]
    storage = _make_storage_mock({"alpha": items})

    tok = current_token_info.set(_token("r", ["read"], ["alpha"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="alpha", min_notes=1, min_age_days=1
            )
    finally:
        current_token_info.reset(tok)

    assert result["scanned"][0]["live_notes_count"] == 1


@pytest.mark.asyncio
async def test_stale_spaces_admin_uses_space_service_when_no_ids_passed():
    """Without space_ids, admin enumerates all spaces via the space service."""
    now = datetime.now(timezone.utc)
    items = [_note_key("auto", now - timedelta(days=20)) for _ in range(8)]
    storage = _make_storage_mock({"auto": items})

    space_service = AsyncMock()
    space_service.list_spaces.return_value = {
        "status": "ok",
        "spaces": [{"space_id": "auto"}],
    }

    tok = current_token_info.set(_token("admin", ["admin"], []))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage), patch(
            "live_mem.core.space.get_space_service", return_value=space_service
        ):
            result = await _bank_tool("bank_stale_spaces")()
    finally:
        current_token_info.reset(tok)

    space_service.list_spaces.assert_awaited_once_with(allowed_space_ids=None)
    assert result["total_stale"] == 1
    assert result["spaces"][0]["space_id"] == "auto"


# ─────────────────────────────────────────────────────────────
# Non-complacent tests — try to break the contract
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_spaces_boundary_is_inclusive_on_both_axes():
    """
    Spec: a space is stale if notes_count >= min_notes AND age >= min_age_days.

    The thresholds MUST be inclusive (>=), not strict (>). A bug switching
    to `>` would silently miss banks exactly at the limit.

    Strategy: build exactly min_notes notes (=5) whose oldest is exactly
    min_age_days old (5 days, with a 1-minute safety margin to dodge
    sub-second drift between fixture setup and tool's `datetime.now()`).
    """
    now = datetime.now(timezone.utc)
    # Oldest exactly at min_age_days, plus a small margin so the tool's
    # own now() doesn't drift below the threshold during execution.
    oldest = now - timedelta(days=5, minutes=1)
    items = [oldest + timedelta(seconds=i) for i in range(5)]  # exactly 5
    storage = _make_storage_mock(
        {"edge": [_note_key("edge", ts) for ts in items]}
    )

    tok = current_token_info.set(_token("r", ["read"], ["edge"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="edge", min_notes=5, min_age_days=5
            )
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 1, (
        "Boundary value (notes=5, age=5d) MUST be flagged — `>=` not `>`. "
        f"Got: {result}"
    )
    assert result["spaces"][0]["live_notes_count"] == 5
    assert result["spaces"][0]["oldest_note_age_days"] >= 5.0


@pytest.mark.asyncio
async def test_stale_spaces_just_below_age_threshold_is_not_flagged():
    """
    Mirror of the boundary test: exactly 0.01 day below the threshold MUST NOT flag.

    Catches a `>` → `>` mistake in the other direction (off-by-one in epsilon).
    """
    now = datetime.now(timezone.utc)
    # Just below 5 days — never enough.
    oldest = now - timedelta(days=4, hours=23, minutes=58)
    items = [_note_key("near", oldest + timedelta(seconds=i)) for i in range(10)]
    storage = _make_storage_mock({"near": items})

    tok = current_token_info.set(_token("r", ["read"], ["near"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="near", min_notes=5, min_age_days=5
            )
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 0
    assert result["scanned"][0]["live_notes_count"] == 10
    assert result["scanned"][0]["is_stale"] is False
    # Displayed age must NEVER exceed the real age (otherwise the operator
    # sees "5.0 days, not stale" with threshold=5 — incoherent). We truncate
    # rather than round-to-nearest.
    displayed = result["scanned"][0]["oldest_note_age_days"]
    assert displayed < 5.0, (
        f"Displayed age ({displayed}) climbed above threshold while is_stale=False — "
        "round-to-nearest UI artifact. Must use truncation."
    )
    assert displayed > 4.9


@pytest.mark.asyncio
async def test_stale_spaces_does_not_query_storage_for_denied_spaces():
    """
    A denied space MUST NOT trigger a storage listing (information leak +
    wasted S3 calls). If the impl forgets to `continue` after denial,
    list_objects would be called for the forbidden prefix.
    """
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])

    tok = current_token_info.set(_token("r", ["read"], ["allowed-only"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="allowed-only,forbidden,secret"
            )
    finally:
        current_token_info.reset(tok)

    # Exactly one storage call for the only allowed space.
    called_prefixes = [c.args[0] for c in storage.list_objects.await_args_list]
    assert called_prefixes == ["allowed-only/live/"], (
        f"Storage was queried for denied spaces! Prefixes: {called_prefixes}"
    )
    denied_ids = sorted(d["space_id"] for d in result["denied_spaces"])
    assert denied_ids == ["forbidden", "secret"]


@pytest.mark.asyncio
async def test_stale_spaces_storage_failure_returns_safe_error_not_500():
    """
    If `list_objects` raises (network blip, S3 timeout), the tool MUST
    return a safe error dict — not propagate the exception or leak
    internal details.

    A bug removing the try/except would crash the MCP server with a 500.
    """
    storage = AsyncMock()
    storage.list_objects = AsyncMock(
        side_effect=RuntimeError("S3 internal: secret-bucket-name leak")
    )

    tok = current_token_info.set(_token("r", ["read"], ["broken"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(space_ids="broken")
    finally:
        current_token_info.reset(tok)

    assert result["status"] == "error", "Tool must catch and convert exceptions."
    # safe_error must redact the raw exception text — secret strings must NOT leak.
    assert "secret-bucket-name" not in (result.get("message") or "")


@pytest.mark.asyncio
async def test_stale_spaces_non_admin_passes_allowed_resources_to_space_service():
    """
    Non-admin without `space_ids` arg MUST scope the listing to
    `allowed_resources`. A bug passing None would expose all spaces.
    """
    space_service = AsyncMock()
    space_service.list_spaces.return_value = {
        "status": "ok",
        "spaces": [{"space_id": "mine-a"}, {"space_id": "mine-b"}],
    }
    storage = _make_storage_mock({"mine-a": [], "mine-b": []})

    tok = current_token_info.set(
        _token("user", ["read"], ["mine-a", "mine-b"])
    )
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage), patch(
            "live_mem.core.space.get_space_service", return_value=space_service
        ):
            await _bank_tool("bank_stale_spaces")()
    finally:
        current_token_info.reset(tok)

    space_service.list_spaces.assert_awaited_once_with(
        allowed_space_ids=["mine-a", "mine-b"]
    )


@pytest.mark.asyncio
async def test_stale_spaces_non_admin_with_empty_allowed_sees_nothing():
    """
    Per v1.5.0 semantics: a non-admin token with `allowed_resources=[]`
    must see ZERO spaces — NOT all spaces. The tool MUST pass `[]` to
    `list_spaces(allowed_space_ids=...)`, not None.

    A bug treating `[]` as "no restriction" would leak every space's
    note counts to a freshly-created (unrestricted) token.
    """
    space_service = AsyncMock()
    space_service.list_spaces.return_value = {"status": "ok", "spaces": []}
    storage = _make_storage_mock({})

    tok = current_token_info.set(_token("fresh", ["read"], []))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage), patch(
            "live_mem.core.space.get_space_service", return_value=space_service
        ):
            result = await _bank_tool("bank_stale_spaces")()
    finally:
        current_token_info.reset(tok)

    space_service.list_spaces.assert_awaited_once_with(allowed_space_ids=[])
    assert result["total_spaces"] == 0
    assert result["total_stale"] == 0


@pytest.mark.asyncio
async def test_stale_spaces_count_and_age_are_AND_not_OR():
    """
    Direct test of the boolean: `stale := notes>=N AND age>=D`.

    Build TWO spaces:
      - `many-but-recent`: 50 notes, 1h old → satisfies count, fails age
      - `old-but-sparse`: 1 note, 100 days old → satisfies age, fails count

    A buggy `OR` impl would flag BOTH. The correct `AND` flags neither.
    """
    now = datetime.now(timezone.utc)
    storage = _make_storage_mock(
        {
            "many-but-recent": [
                _note_key("many-but-recent", now - timedelta(minutes=i))
                for i in range(50)
            ],
            "old-but-sparse": [
                _note_key("old-but-sparse", now - timedelta(days=100)),
            ],
        }
    )

    tok = current_token_info.set(
        _token("r", ["read"], ["many-but-recent", "old-but-sparse"])
    )
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="many-but-recent,old-but-sparse"
            )
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 0, (
        "If a buggy impl flagged either side it would be using OR, not AND. "
        f"Got: {result['spaces']}"
    )
    # Both should appear in `scanned` to confirm they WERE actually inspected
    # (not silently skipped).
    scanned_ids = {s["space_id"] for s in result["scanned"]}
    assert scanned_ids == {"many-but-recent", "old-but-sparse"}


@pytest.mark.asyncio
async def test_stale_spaces_age_display_is_truncated_never_rounded_up():
    """
    Explicit truncation invariant: the displayed `oldest_note_age_days` MUST
    NEVER exceed the actual age. `round-to-nearest` would violate this and
    surface a "5.0 days, is_stale=False" line when the threshold is 5 — an
    incoherent UX state.

    Strategy: pick an age that round-to-nearest WOULD inflate (e.g., 4.998d
    rounds to 5.0). Verify the displayed value stays strictly below.
    """
    now = datetime.now(timezone.utc)
    # 4.998 days = 4d 23h 57m 7.2s. round(4.998, 2) = 5.0. Truncation = 4.99.
    oldest = now - timedelta(days=4, hours=23, minutes=57, seconds=7)
    items = [_note_key("trunc", oldest + timedelta(seconds=i)) for i in range(10)]
    storage = _make_storage_mock({"trunc": items})

    tok = current_token_info.set(_token("r", ["read"], ["trunc"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="trunc", min_notes=5, min_age_days=5
            )
    finally:
        current_token_info.reset(tok)

    displayed = result["scanned"][0]["oldest_note_age_days"]
    # If `round(x, 2)` was used, displayed would be 5.0 and the next assert
    # would fail. With truncation, it stays < 5.0.
    assert displayed < 5.0, (
        f"Displayed age {displayed} >= threshold while is_stale=False — "
        "the impl is using round-to-nearest, not truncation. UI incoherent."
    )
    assert result["scanned"][0]["is_stale"] is False


@pytest.mark.asyncio
async def test_stale_spaces_raising_thresholds_drops_previously_stale_space():
    """
    Inverse of `custom_thresholds` (which only tested LOWERING).

    A space that's stale at the defaults (≥5 notes / ≥5 days) MUST drop
    out of the stale set when thresholds are raised above its values.
    Catches a bug where thresholds are ignored or hard-coded.
    """
    now = datetime.now(timezone.utc)
    items = [
        _note_key("middling", now - timedelta(days=7, hours=i)) for i in range(6)
    ]  # 6 notes, ~7 days old → stale at defaults, NOT at min_notes=10/min_age=14
    storage = _make_storage_mock({"middling": items})

    tok = current_token_info.set(_token("r", ["read"], ["middling"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            default = await _bank_tool("bank_stale_spaces")(space_ids="middling")
            raised = await _bank_tool("bank_stale_spaces")(
                space_ids="middling", min_notes=10, min_age_days=14
            )
    finally:
        current_token_info.reset(tok)

    assert default["total_stale"] == 1, "Sanity: should be stale at defaults"
    assert raised["total_stale"] == 0, (
        "Raising thresholds above the space's values must drop it from the stale set. "
        f"Got: {raised['spaces']}"
    )
    # The space still appears in `scanned` — only the FILTER changed.
    assert raised["scanned"][0]["space_id"] == "middling"
    assert raised["scanned"][0]["is_stale"] is False
    # The returned thresholds match what was requested (no silent clamp/swap).
    assert raised["min_notes"] == 10
    assert raised["min_age_days"] == 14


@pytest.mark.asyncio
async def test_stale_spaces_oldest_filename_actually_corresponds_to_oldest():
    """
    `oldest_note_filename` MUST be the filename whose embedded timestamp
    matches `oldest_note_timestamp`. A naive impl that picks the first
    listed key would fail when S3 returns keys out of order.
    """
    now = datetime.now(timezone.utc)
    # Build keys in REVERSE chronological order — the oldest must NOT be the first listed.
    items = [
        _note_key("ord", now - timedelta(days=2)),
        _note_key("ord", now - timedelta(days=10)),  # ← actual oldest
        _note_key("ord", now - timedelta(days=5)),
        _note_key("ord", now - timedelta(days=1)),
        _note_key("ord", now - timedelta(days=8)),
        _note_key("ord", now - timedelta(days=3)),
    ]
    storage = _make_storage_mock({"ord": items})

    tok = current_token_info.set(_token("r", ["read"], ["ord"]))
    try:
        with patch("live_mem.core.storage.get_storage", return_value=storage):
            result = await _bank_tool("bank_stale_spaces")(
                space_ids="ord", min_notes=5, min_age_days=5
            )
    finally:
        current_token_info.reset(tok)

    assert result["total_stale"] == 1
    entry = result["spaces"][0]
    assert entry["oldest_note_age_days"] >= 10.0
    # Verify the filename truly matches the 10-day-old fixture (not the first listed)
    expected_prefix = (now - timedelta(days=10)).strftime("%Y%m%dT%H%M%S")
    assert entry["oldest_note_filename"].startswith(expected_prefix), (
        f"Picked wrong note. Expected prefix {expected_prefix}, "
        f"got filename {entry['oldest_note_filename']}"
    )
