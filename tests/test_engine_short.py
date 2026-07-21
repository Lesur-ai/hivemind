# -*- coding: utf-8 -*-
"""
Tests for P3-6 (issue #55) — ShortEngine adapter over LiveService.

Deterministic and offline: backed by an in-memory FakeStorage (the WriteSink
fake idiom, extended with ``list_and_get`` for read/search) and a real
``LiveService``. No real S3 / network / LLM.

What is verified (Wave-2 WRAP-DON'T-REWRITE contract):
- write_note via ShortEngine is BYTE-IDENTICAL to write_note via the legacy
  LiveService (datetime + uuid + agent frozen so the filename / front-matter are
  deterministic).
- The default ``write_sink`` is a ``DirectLocalWriteSink``; injected sink / live
  are held verbatim (identity).
- read_notes / search_notes delegate and are read-only (never touch the sink).
- write_note signature (keyword defaults) matches LiveService.

The injected WriteSink is HELD but NOT consumed in Wave-2: write_note still
flows through LiveService's own storage.put (live.py). These tests assert that
behavior is unchanged; routing the PUT through the sink is #8/#9, the flip is
P3-7.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from live_mem.auth.context import current_token_info
from live_mem.core.engines.short import (
    WRITE_SINK_MUTATION_CALL_SITES,
    ShortEngine,
)
from live_mem.core.live import LiveService
from live_mem.core.write_sink import DirectLocalWriteSink, WriteSink
from tests.test_write_sink import WriteSinkFakeStorage


# =============================================================================
# Local fake — FakeStorage + delete_many (via WriteSinkFakeStorage) + the reads
# LiveService.read_notes / search_notes need (list_and_get).
# =============================================================================


class LiveFakeStorage(WriteSinkFakeStorage):
    """``WriteSinkFakeStorage`` + ``list_and_get`` (used by read/search).

    Mirrors ``StorageService.list_and_get``: returns ``[{"key", "content"}]`` for
    every object under ``prefix``. The shared FakeStorage stores plain strings in
    ``self.objects`` keyed by full key, so this is a thin filtered view.
    """

    async def list_and_get(self, prefix: str) -> list[dict]:
        out = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append({"key": key, "content": self.objects[key]})
        return out


@pytest.fixture(autouse=True)
def _no_real_s3_for_default_sink():
    """Guarantee the default ``DirectLocalWriteSink()`` (constructed whenever an
    engine is built without an explicit sink) never builds a real boto3 client.

    ``DirectLocalWriteSink.__init__`` resolves ``get_storage()`` (the storage
    singleton) when no storage is injected; in an offline test env that
    constructs a real ``StorageService`` and raises on the empty S3 endpoint.
    Patching the write_sink namespace's ``get_storage`` keeps the default-sink
    path deterministic and offline. (Tests that assert byte-output still patch
    ``live_mem.core.live.get_storage`` separately for the LiveService path.)
    """
    with patch(
        "live_mem.core.write_sink.get_storage", return_value=LiveFakeStorage()
    ):
        yield


def _set_token(name: str = "cline") -> None:
    current_token_info.set(
        {
            "client_name": name,
            "permissions": ["read", "write"],
            "allowed_resources": [],
        }
    )


class _FrozenDatetime:
    """datetime stand-in whose now() is fixed (filename/front-matter determinism)."""

    _FIXED = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirrors datetime.now signature
        return cls._FIXED


class _FrozenUUID:
    """uuid stand-in returning a fixed uuid4().hex (note filename determinism)."""

    class _U:
        hex = "deadbeefcafef00d"

    @staticmethod
    def uuid4():
        return _FrozenUUID._U()


# =============================================================================
# write_note — byte-identical golden pass-through (engine vs legacy)
# =============================================================================


@pytest.mark.asyncio
async def test_short_engine_write_note_byte_identical_to_legacy() -> None:
    """A note written via ShortEngine produces byte-for-byte identical stored
    content + identical return dict to one written via the legacy LiveService,
    once datetime / uuid / agent are frozen."""
    storage = LiveFakeStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    _set_token("cline")

    with patch("live_mem.core.live.get_storage", return_value=storage), patch(
        "live_mem.core.live.datetime", _FrozenDatetime
    ), patch("live_mem.core.live.uuid", _FrozenUUID):
        # Legacy path.
        legacy = LiveService()
        legacy_result = await legacy.write_note(
            "space-a", "observation", "hello world", tags="a,b"
        )
        legacy_key = f"space-a/live/{legacy_result['filename']}"
        legacy_bytes = storage.objects[legacy_key]

        # Reset stored objects (keep _meta) so the engine path writes the same key.
        storage.objects = {"space-a/_meta.json": "{}"}

        # Engine path — default sink, real LiveService.
        engine = ShortEngine(live=LiveService())
        engine_result = await engine.write_note(
            "space-a", "observation", "hello world", tags="a,b"
        )
        engine_key = f"space-a/live/{engine_result['filename']}"
        engine_bytes = storage.objects[engine_key]

    # Same filename (frozen ts + uuid + agent), same stored bytes, same dict.
    assert engine_result["filename"] == legacy_result["filename"]
    assert engine_key == legacy_key
    assert engine_bytes == legacy_bytes
    assert engine_result == legacy_result
    # Sanity: the stored content really is the front-matter + body.
    assert engine_bytes.endswith("hello world")
    assert engine_bytes.startswith("---\n")


@pytest.mark.asyncio
async def test_live_note_round_trips_exact_non_filename_safe_agent_identity() -> None:
    storage = LiveFakeStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    agent = 'a---."b\nc'
    _set_token(agent)

    with patch("live_mem.core.live.get_storage", return_value=storage), patch(
        "live_mem.core.live.datetime", _FrozenDatetime
    ), patch("live_mem.core.live.uuid", _FrozenUUID):
        created = await LiveService().write_note(
            "space-a", "observation", "identity-safe"
        )
        read_back = await LiveService().read_notes("space-a", agent=agent)

    assert created["status"] == "created"
    assert created["agent"] == agent
    assert created["filename"] == (
        "20260618T120000_a---bc_observation_deadbeef.md"
    )
    assert f'agent: "a---.\\"b\\nc"' in next(
        value for key, value in storage.objects.items() if "/live/" in key
    )
    assert read_back["total"] == 1
    assert read_back["notes"][0]["agent"] == agent


@pytest.mark.asyncio
async def test_live_note_rejects_empty_agent_identity_without_write() -> None:
    storage = LiveFakeStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    _set_token("")

    with patch("live_mem.core.live.get_storage", return_value=storage):
        result = await LiveService().write_note(
            "space-a", "observation", "must-not-write"
        )

    assert result["status"] == "error"
    assert "client_name" in result["message"]
    assert not any("/live/" in key for key in storage.objects)


@pytest.mark.asyncio
async def test_short_engine_write_note_omits_explicit_content_type() -> None:
    """The durable PUT behind write_note (live.py) is a single
    ``{space_id}/live/{filename}.md`` write with NO explicit content_type — so
    StorageService's own default ('text/plain; charset=utf-8') applies. We assert
    via a recording fake that exactly one PUT happens and that the call site does
    NOT override the content_type (the recorded value is the FAKE's default, which
    proves no explicit arg was forwarded). The enumerated call site documents the
    real StorageService default the eventual WriteSink will carry."""
    captured: list[dict] = []

    class CapturingLiveStorage(LiveFakeStorage):
        async def put(self, key, content, content_type="text/plain") -> None:
            # Capture how MANY positional args live.py actually passed by
            # recording the default the fake itself supplied (live.py omits it).
            captured.append({"key": key, "content_type": content_type})
            await super().put(key, content, content_type)

    storage = CapturingLiveStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    _set_token("cline")

    with patch("live_mem.core.live.get_storage", return_value=storage):
        engine = ShortEngine(live=LiveService())
        await engine.write_note("space-a", "observation", "x")

    assert len(captured) == 1, "expected exactly one durable PUT"
    assert captured[0]["key"].startswith("space-a/live/")
    # live.py omits content_type -> the fake's own default is recorded, proving
    # the call site does not override it; StorageService would apply
    # 'text/plain; charset=utf-8'. The enumerated site records that real default.
    assert captured[0]["content_type"] == "text/plain"
    assert WRITE_SINK_MUTATION_CALL_SITES[0]["content_type"] == (
        "text/plain; charset=utf-8"
    )


# =============================================================================
# Constructor DI — default sink + injected sink/live held verbatim
# =============================================================================


def test_short_engine_default_write_sink_is_direct_local() -> None:
    """ShortEngine() with no sink defaults to a DirectLocalWriteSink (lazy:
    constructing the engine must not need a real S3 client)."""
    # DirectLocalWriteSink() resolves get_storage() lazily; patch it so no boto3.
    storage = LiveFakeStorage()
    with patch("live_mem.core.write_sink.get_storage", return_value=storage):
        engine = ShortEngine(live=LiveService())
    assert isinstance(engine.write_sink, DirectLocalWriteSink)
    assert isinstance(engine.write_sink, WriteSink)


def test_short_engine_accepts_injected_write_sink_and_live() -> None:
    """Injected WriteSink + injected LiveService are held verbatim (identity)."""
    storage = LiveFakeStorage()
    injected_sink = DirectLocalWriteSink(storage=storage)
    injected_live = LiveService()

    engine = ShortEngine(live=injected_live, write_sink=injected_sink)

    assert engine.write_sink is injected_sink
    assert engine._live is injected_live


# =============================================================================
# read_notes / search_notes — delegate, read-only, sink untouched
# =============================================================================


@pytest.mark.asyncio
async def test_short_engine_read_notes_passthrough() -> None:
    """read_notes delegates to LiveService and returns the same dict; the sink is
    never touched (no put/delete calls)."""
    storage = LiveFakeStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    _set_token("cline")

    with patch("live_mem.core.live.get_storage", return_value=storage):
        legacy = LiveService()
        # Seed one note via legacy so there is something to read.
        await legacy.write_note("space-a", "observation", "note-1")

        injected_sink = DirectLocalWriteSink(storage=storage)
        before_puts, before_deletes = storage.put_calls, storage.delete_calls

        engine = ShortEngine(live=LiveService(), write_sink=injected_sink)
        engine_out = await engine.read_notes("space-a", limit=10, category="observation")
        legacy_out = await LiveService().read_notes(
            "space-a", limit=10, category="observation"
        )

    assert engine_out == legacy_out
    assert engine_out["status"] == "ok"
    # read is NOT a durable mutation: no extra put/delete beyond the seeding write.
    assert storage.put_calls == before_puts
    assert storage.delete_calls == before_deletes


@pytest.mark.asyncio
async def test_short_engine_search_notes_passthrough() -> None:
    """search_notes delegates, is read-only, and leaves the sink untouched."""
    storage = LiveFakeStorage()
    storage.objects["space-a/_meta.json"] = "{}"
    _set_token("cline")

    with patch("live_mem.core.live.get_storage", return_value=storage):
        await LiveService().write_note("space-a", "observation", "find-me please")

        before_puts, before_deletes = storage.put_calls, storage.delete_calls
        engine = ShortEngine(live=LiveService())
        engine_out = await engine.search_notes("space-a", "find-me", limit=5)
        legacy_out = await LiveService().search_notes("space-a", "find-me", limit=5)

    assert engine_out == legacy_out
    assert engine_out["status"] == "ok"
    assert engine_out["total"] >= 1
    assert storage.put_calls == before_puts
    assert storage.delete_calls == before_deletes


# =============================================================================
# Signature parity + call-site enumeration deliverable
# =============================================================================


def test_short_engine_write_note_signature_matches_liveservice() -> None:
    """ShortEngine.write_note's params + defaults match LiveService.write_note."""
    engine_sig = inspect.signature(ShortEngine.write_note)
    legacy_sig = inspect.signature(LiveService.write_note)
    # Same parameter names + same defaults (ignoring 'self' on both).
    assert list(engine_sig.parameters) == list(legacy_sig.parameters)
    assert engine_sig.parameters["tags"].default == ""
    assert legacy_sig.parameters["tags"].default == ""


def test_short_engine_read_search_signatures_match_liveservice() -> None:
    for name in ("read_notes", "search_notes"):
        eng = inspect.signature(getattr(ShortEngine, name))
        leg = inspect.signature(getattr(LiveService, name))
        assert list(eng.parameters) == list(leg.parameters), name
    # spot-check defaults
    rn = inspect.signature(ShortEngine.read_notes).parameters
    assert rn["limit"].default == 50
    assert rn["category"].default == ""
    assert rn["agent"].default == ""
    assert rn["since"].default == ""
    sn = inspect.signature(ShortEngine.search_notes).parameters
    assert sn["limit"].default == 20


def test_short_engine_methods_are_async() -> None:
    for name in ("write_note", "read_notes", "search_notes"):
        assert inspect.iscoroutinefunction(getattr(ShortEngine, name)), name


def test_short_engine_enumerates_single_writesink_call_site() -> None:
    """The #8/#9 deliverable: ShortEngine documents exactly ONE eventual durable
    WriteSink mutation — the single live/{filename}.md PUT in
    LiveService.write_note — anchored semantically (line number is a hint only)."""
    sites = WRITE_SINK_MUTATION_CALL_SITES
    assert len(sites) == 1
    site = sites[0]
    assert site["engine"] == "ShortEngine"
    assert site["op"] == "put"
    assert site["module"] == "live_mem.core.live"
    assert site["method"] == "LiveService.write_note"
    assert "{space_id}/live/" in site["key_pattern"]
    assert site["content_type"] == "text/plain; charset=utf-8"
