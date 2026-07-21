# -*- coding: utf-8 -*-
"""
ShortEngine — thin DI adapter over :class:`live_mem.core.live.LiveService`
(P3-6, EPIC-P3 / ADR-0006).

ShortEngine is the *short-memory* engine port: append-only, conflict-free live
notes. It WRAPS — never rewrites — the imported ``LiveService`` with ZERO
behavior change. It is a facade that delegates verbatim; it does not parse,
re-order, validate, or re-serialize anything.

Wave-2 contract (ADR-0006 "wrap, don't rewrite" + EPIC-P3 In/Out of scope):

- NEW FILE ONLY. ``core/live.py`` is NOT edited by this child.
- The engine accepts an INJECTED :class:`~live_mem.core.write_sink.WriteSink`
  via the constructor, defaulting to
  :class:`~live_mem.core.write_sink.DirectLocalWriteSink`. In Wave-2 the sink is
  HELD but NOT CONSUMED: ``write_note`` still flows through ``LiveService``'s own
  ``storage.put`` (see ``WRITE_SINK_MUTATION_CALL_SITES`` below). Routing the
  durable PUT through the injected sink is #8/#9; the per-space routing FLIP
  (DirectLocal vs Staged) is P3-7.
- Default behavior is byte-for-byte identical to today, because the default
  ``DirectLocalWriteSink`` delegates verbatim to ``StorageService`` and (this
  wave) is not yet on the write path at all.
- Mono-tenant: every method keys on ``space_id`` only; no tenant concept.
- Append-only: ``write_note`` is a single PUT with NO lock (live.py). ShortEngine
  must NOT add a lock.

The single durable-mutation call site that #8/#9 will eventually route through
the injected ``WriteSink`` is enumerated in
:data:`WRITE_SINK_MUTATION_CALL_SITES` so downstream issues inherit the
authoritative list. Reads (``read_notes`` / ``search_notes``) are NOT durable
mutations and never touch the sink.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..live import LiveService, get_live_service
from ..write_sink import DirectLocalWriteSink, WriteSink

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


# ─────────────────────────────────────────────────────────────────────────────
# WriteSink durable-mutation call-site enumeration (the #8/#9 deliverable).
#
# Anchored on SEMANTIC descriptions; line numbers are NON-ASSERTED hints only
# (they drift as live.py evolves). ShortEngine has exactly ONE eventual durable
# write — the single live/{filename}.md PUT inside LiveService.write_note — using
# StorageService's DEFAULT content_type ('text/plain; charset=utf-8', no explicit
# arg). Reads (read_notes / search_notes) are NOT on this boundary.
# ─────────────────────────────────────────────────────────────────────────────
WRITE_SINK_MUTATION_CALL_SITES: tuple[dict, ...] = (
    {
        "engine": "ShortEngine",
        "op": "put",
        "module": "live_mem.core.live",
        "method": "LiveService.write_note",
        "key_pattern": "{space_id}/live/{filename}.md",
        "content_type": "text/plain; charset=utf-8",  # StorageService default
        "line_hint": 143,
        "description": (
            "Single append-only PUT of one live note (front-matter + body); "
            "the only ShortEngine durable mutation. No lock (append-only)."
        ),
    },
)


class ShortEngine:
    """Short-memory engine port: append-only live notes over ``LiveService``.

    Thin DI facade. Wraps ``LiveService`` with ZERO behavior change — every
    method delegates verbatim and returns the wrapped result unchanged. The
    injected ``WriteSink`` is HELD for #8/#9 but NOT consumed in Wave-2:
    ``write_note`` still routes through ``LiveService.write_note``'s own
    ``storage.put`` (the single ``{space_id}/live/{filename}.md`` PUT — see
    :data:`WRITE_SINK_MUTATION_CALL_SITES`). Reads never touch the sink.

    Constructed via DI::

        ShortEngine(live: LiveService | None = None,
                    write_sink: WriteSink | None = None)

    Both default lazily (``get_live_service()`` /
    ``DirectLocalWriteSink()``) so importing this module never builds a real S3
    client — ``DirectLocalWriteSink`` itself resolves ``get_storage()`` lazily.

    Mono-tenant: keys on ``space_id`` only.
    """

    def __init__(
        self,
        live: Optional[LiveService] = None,
        write_sink: Optional[WriteSink] = None,
    ) -> None:
        # Lazy defaults — do not construct singletons / S3 clients at import time.
        self._live: LiveService = live if live is not None else get_live_service()
        # `is not None` (not `or`): a valid sink object is never falsy, but this
        # mirrors DirectLocalWriteSink's own storage-resolution guard and is
        # robust to any future sink that overrides __bool__.
        self._write_sink: WriteSink = (
            write_sink if write_sink is not None else DirectLocalWriteSink()
        )

    @property
    def write_sink(self) -> WriteSink:
        """The injected (or default) durable-write boundary. Held, not yet
        consumed (#8/#9 wire it into ``write_note``; P3-7 flips routing)."""
        return self._write_sink

    async def write_note(
        self,
        space_id: str,
        category: str,
        content: str,
        tags: str = "",
    ) -> dict:
        """Delegate verbatim to ``LiveService.write_note`` (append-only, no lock).

        Wave-2: the durable ``{space_id}/live/{filename}.md`` PUT stays inside
        ``LiveService`` (``WRITE_SINK_MUTATION_CALL_SITES[0]``); the injected sink
        is held but not consumed here. Do NOT edit live.py.
        """
        return await self._live.write_note(space_id, category, content, tags)

    async def read_notes(
        self,
        space_id: str,
        limit: int = 50,
        category: str = "",
        agent: str = "",
        since: str = "",
    ) -> dict:
        """Delegate verbatim to ``LiveService.read_notes`` (read-only; NOT a
        WriteSink path)."""
        return await self._live.read_notes(
            space_id, limit=limit, category=category, agent=agent, since=since
        )

    async def search_notes(
        self,
        space_id: str,
        query: str,
        limit: int = 20,
    ) -> dict:
        """Delegate verbatim to ``LiveService.search_notes`` (read-only; NOT a
        WriteSink path)."""
        return await self._live.search_notes(space_id, query, limit=limit)
