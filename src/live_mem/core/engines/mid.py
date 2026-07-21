# -*- coding: utf-8 -*-
"""
MidEngine — thin DI adapter over the consolidation surface (P3-6, EPIC-P3 /
ADR-0006): :class:`live_mem.core.consolidator.ConsolidatorService` +
:class:`live_mem.core.consolidation_queue.ConsolidationQueueService`.

MidEngine is the *mid-memory* engine port: the LLM-driven consolidation of
append-only live notes into the durable Memory Bank, plus the async FIFO
consolidation queue and direct mid maintenance (compaction). It WRAPS — never
rewrites — the imported implementations with ZERO behavior change. Every method
is pure delegation that returns the wrapped result unchanged.

Wave-2 contract (ADR-0006 "wrap, don't rewrite" + EPIC-P3 In/Out of scope):

- NEW FILE ONLY. ``core/consolidator.py`` and ``core/consolidation_queue.py``
  are NOT edited by this child.
- The engine accepts an INJECTED :class:`~live_mem.core.write_sink.WriteSink`
  via the constructor, defaulting to
  :class:`~live_mem.core.write_sink.DirectLocalWriteSink`. In Wave-2 the sink is
  HELD but NOT CONSUMED: consolidation / compaction keep their own
  ``storage.put`` / ``put_json`` / ``delete`` / ``delete_many`` calls (the full
  set is enumerated in :data:`WRITE_SINK_MUTATION_CALL_SITES`). Wiring those
  through the injected sink is #8/#9; the routing FLIP is P3-7.
- Per-space single-writer semantics are PRESERVED by HAND-OFF, not reimplemented:
  ``enqueue_consolidation`` delegates to the ``ConsolidationQueueService``
  singleton, whose worker already runs ``consolidate`` under
  ``get_lock_manager().consolidation(space_id)`` with ``enforce_cooldown=False``
  (one worker per space, FIFO, ``QUEUE_GUARANTEE == 'in_memory_best_effort'``).
  MidEngine adds NO worker, NO lock, NO queue of its own — reimplementing any of
  these would break the single-writer-per-space invariant.
- Mono-tenant: every method keys on ``space_id`` only; no tenant concept.

Bank-read scope (EPIC-vs-SHARED-CONTRACT reconciliation — recorded for the PR):
The EPIC-P3 P3-6 acceptance criterion says "MidEngine exposes bank read", but
the authoritative SHARED CONTRACT (task brief) lists ONLY the consolidation /
queue surface for MidEngine, and bank reads live in the TOOL layer
(``tools/bank.py`` ``bank_read`` / ``bank_read_all`` over
``StorageService.list_and_get`` — interleaved with ``check_access`` / Unicode
fallback / ``safe_error``), NOT as a ``ConsolidatorService`` method. Adding a
read-through here would re-implement tool-layer logic and break WRAP-DON'T-
REWRITE. Per DESIGN open-question #1, the thin bank-read delegator is DEFERRED to
P3-7 (the registry, which owns the StorageService read seam). This module keeps
MidEngine to the SHARED-CONTRACT consolidation/queue surface. The reviewer/pilot
must confirm the EPIC checkbox reconciliation in the PR.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

from ..consolidation_queue import (
    ConsolidationQueueService,
    get_consolidation_queue,
)
from ..consolidator import ConsolidatorService, get_consolidator
from ..write_sink import DirectLocalWriteSink, WriteSink

if TYPE_CHECKING:  # pragma: no cover - typing only
    ProgressCallback = Callable[[dict], Union[Awaitable[None], None]]


# ─────────────────────────────────────────────────────────────────────────────
# WriteSink durable-mutation call-site enumeration (the #8/#9 deliverable).
#
# Anchored on SEMANTIC descriptions; line numbers are NON-ASSERTED hints only
# (they drift). This is the FULL eventual WriteSink mutation set for the mid /
# bank write path, in TWO branches:
#
#   CONSOLIDATOR branch — ConsolidatorService.{consolidate, _write_results,
#       compact_bank} (core/consolidator.py). These are the calls the consolidate
#       run / compaction perform directly today and that #8/#9 will route through
#       the injected sink.
#
#   BANK-TOOL branch — bank_repair / bank_write / bank_compact mutations in
#       tools/bank.py. These bank-tool writers are in eventual WriteSink scope
#       per ADR-0007 / the shared contract even though MidEngine does NOT surface
#       the bank tools; they are documented HERE so #8/#9 inherit the COMPLETE
#       picture rather than only the consolidator subset.
#
# NOTE: ``long_*`` / ``graph_push`` is NEVER a WriteSink write (downstream-derived
# projection, ADR-0010) and is intentionally absent.
# ─────────────────────────────────────────────────────────────────────────────
WRITE_SINK_MUTATION_CALL_SITES: tuple[dict, ...] = (
    # ---- CONSOLIDATOR branch -------------------------------------------------
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "delete",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/bank/<unicode-dup>",
        "line_hint": 1345,
        "description": "Unicode-duplicate bank file cleanup delete (rename fix).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 1361,
        "description": "Bank file PUT — create branch (new bank file).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 1405,
        "description": "Bank file PUT — replace branch (overwrite bank file).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 1451,
        "description": "Bank file PUT — append/merge branch (updated content).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/_synthesis.md",
        "line_hint": 1477,
        "description": "Synthesis markdown PUT (_synthesis.md).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put_json",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/_meta.json",
        "line_hint": 1488,
        "description": "Metadata JSON PUT (_meta.json) inside _write_results.",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "delete_many",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._write_results",
        "key_pattern": "{space_id}/live/* (consumed notes)",
        "line_hint": 1491,
        "description": (
            "Consumed live notes deleted LAST (atomicity: bank written before "
            "notes removed). Order must be preserved."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put_json",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService.consolidate",
        "key_pattern": "{space_id}/_meta.json",
        "line_hint": 809,
        "description": "End-of-run _meta.json PUT (consolidate epilogue).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService.compact_bank",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 1977,
        "description": "compact_bank effective bank-file PUT (manual compaction).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._compact_bank_if_needed/_compact_single_file",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 1817,
        "description": "Auto-compaction bank-file PUT inside the consolidate run.",
    },
    # ---- BANK-TOOL branch (documented for #8/#9; not surfaced by MidEngine) ---
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "put",
        "module": "live_mem.tools.bank",
        "method": "bank_repair",
        "key_pattern": "{space_id}/bank/<canonical>",
        "line_hint": 879,
        "description": "bank_repair canonical-key PUT (repaired filename).",
    },
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "delete",
        "module": "live_mem.tools.bank",
        "method": "bank_repair",
        "key_pattern": "{space_id}/bank/<original-or-dup>",
        "line_hint": 881,
        "description": "bank_repair original-key delete (post-rename).",
    },
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "delete",
        "module": "live_mem.tools.bank",
        "method": "bank_repair",
        "key_pattern": "{space_id}/bank/<duplicate>",
        "line_hint": 887,
        "description": "bank_repair duplicate-key delete.",
    },
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "put",
        "module": "live_mem.tools.bank",
        "method": "bank_write",
        "key_pattern": "{space_id}/bank/<canonical>",
        "line_hint": 1005,
        "description": "bank_write canonical-key PUT (direct bank write/replace).",
    },
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "delete",
        "module": "live_mem.tools.bank",
        "method": "bank_write",
        "key_pattern": "{space_id}/bank/<raw>",
        "line_hint": 1017,
        "description": "bank_write raw-key delete (stale/non-canonical cleanup).",
    },
    {
        "engine": "MidEngine",
        "branch": "bank_tool",
        "op": "delete_many",
        "module": "live_mem.tools.bank",
        "method": "bank_delete",
        "key_pattern": "{space_id}/bank/<keys_to_delete>",
        "line_hint": 1126,
        "description": (
            "bank_delete destructive direct multi-key delete "
            "(storage.delete_many of selected bank files). NOTE: bank_compact "
            "does NOT delete directly — it delegates to "
            "ConsolidatorService.compact_bank (see the consolidator branch), so "
            "its writes are enumerated there, not as a bank-tool delete."
        ),
    },
)


class MidEngine:
    """Mid-memory engine port: consolidation + queue over the imported services.

    Thin DI facade. Wraps ``ConsolidatorService`` + ``ConsolidationQueueService``
    with ZERO behavior change — every method delegates verbatim (async
    await-delegation) and returns the wrapped result unchanged.

    The injected ``WriteSink`` is HELD for #8/#9 but NOT consumed in Wave-2:
    consolidation / compaction keep their own ``storage`` calls (the full set is
    enumerated in :data:`WRITE_SINK_MUTATION_CALL_SITES`).

    Per-space single-writer semantics are preserved by HAND-OFF to the queue
    singleton (one worker per space, FIFO, ``in_memory_best_effort``); MidEngine
    adds no worker / lock / queue of its own.

    Constructed via DI::

        MidEngine(consolidator: ConsolidatorService | None = None,
                  queue: ConsolidationQueueService | None = None,
                  write_sink: WriteSink | None = None)

    All three default lazily (``get_consolidator()`` /
    ``get_consolidation_queue()`` / ``DirectLocalWriteSink()``) so importing this
    module never builds a real LLM / S3 client.

    Mono-tenant: keys on ``space_id`` only.
    """

    def __init__(
        self,
        consolidator: Optional[ConsolidatorService] = None,
        queue: Optional[ConsolidationQueueService] = None,
        write_sink: Optional[WriteSink] = None,
    ) -> None:
        # Lazy defaults — do not construct singletons / LLM / S3 clients at
        # import time (ConsolidatorService.__init__ builds AsyncOpenAI + httpx).
        self._consolidator: ConsolidatorService = (
            consolidator if consolidator is not None else get_consolidator()
        )
        self._queue: ConsolidationQueueService = (
            queue if queue is not None else get_consolidation_queue()
        )
        self._write_sink: WriteSink = (
            write_sink if write_sink is not None else DirectLocalWriteSink()
        )

    @property
    def write_sink(self) -> WriteSink:
        """The injected (or default) durable-write boundary. Held, not yet
        consumed (#8/#9 wire it into the consolidator write path; P3-7 flips
        routing)."""
        return self._write_sink

    async def consolidate(
        self,
        space_id: str,
        agent: str = "",
        enforce_cooldown: bool = True,
        progress_callback: "Optional[ProgressCallback]" = None,
        note_keys: Iterable[str] | None = None,
    ) -> dict:
        """Delegate verbatim to ``ConsolidatorService.consolidate``.

        Full signature preserved, including ``progress_callback`` and the
        ``enforce_cooldown=True`` default (matching the wrapped service). Durable
        bank/synthesis/meta writes + consumed-notes delete stay inside the
        consolidator (``WRITE_SINK_MUTATION_CALL_SITES``); the injected sink is
        held but not consumed here.
        """
        kwargs = {
            "agent": agent,
            "enforce_cooldown": enforce_cooldown,
            "progress_callback": progress_callback,
        }
        # Preserve compatibility with injected consolidators that implement the
        # historical signature. The exact-key extension is opt-in and is only
        # forwarded when a caller actually supplies it.
        if note_keys is not None:
            kwargs["note_keys"] = note_keys
        return await self._consolidator.consolidate(space_id, **kwargs)

    async def enqueue_consolidation(
        self,
        space_id: str,
        agent: str,
        requested_by: str,
    ) -> dict:
        """Delegate verbatim to ``ConsolidationQueueService.enqueue``.

        Args are forwarded UNCHANGED (no normalization / coercion) so the
        queue's agent-coalescing branch is preserved (non-empty ``agent``
        coalesces duplicate pending jobs; ``agent=''`` stays distinct). MidEngine
        does NOT reimplement the worker/lock — the singleton's worker already
        runs ``consolidate`` under ``get_lock_manager().consolidation(space_id)``
        with ``enforce_cooldown=False`` (one worker per space, FIFO,
        ``in_memory_best_effort``).
        """
        return await self._queue.enqueue(space_id, agent, requested_by)

    async def get_job(self, job_id: str) -> dict:
        """Delegate verbatim to ``ConsolidationQueueService.get_job`` (read-only;
        NOT a WriteSink path)."""
        return await self._queue.get_job(job_id)

    async def get_space_summary(self, space_id: str) -> dict:
        """Delegate verbatim to ``ConsolidationQueueService.get_space_summary``
        (read-only; NOT a WriteSink path)."""
        return await self._queue.get_space_summary(space_id)

    async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
        """Delegate verbatim to ``ConsolidatorService.compact_bank``.

        ``dry_run=True`` default matches the wrapped service (scan-only). The
        effective bank-file PUTs (``dry_run=False``) stay inside the consolidator
        (``WRITE_SINK_MUTATION_CALL_SITES``); the injected sink is held but not
        consumed here.
        """
        return await self._consolidator.compact_bank(space_id, dry_run)
