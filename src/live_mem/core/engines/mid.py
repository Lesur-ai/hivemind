# -*- coding: utf-8 -*-
"""
MidEngine — thin DI adapter over the consolidation surface (P3-6, EPIC-P3 /
ADR-0006): :class:`live_mem.core.consolidator.ConsolidatorService` +
:class:`live_mem.core.consolidation_queue.ConsolidationQueueService`.

MidEngine is the *mid-memory* engine port: the LLM-driven consolidation of
append-only live notes into the durable Memory Bank, plus the async FIFO
consolidation queue and direct mid maintenance (compaction). It WRAPS — never
rewrites — the imported implementations. #394 adds only a private route-proof
handoff around mutating consolidation/compaction calls.

Original Wave-2 contract (ADR-0006 "wrap, don't rewrite" + EPIC-P3
In/Out of scope), with the narrow #394 compaction-authority exception below:

- The original child was NEW FILE ONLY. #394 changes the compaction
  prepare/apply seam and hands the registry's space-bound DirectLocal proof to
  the consolidator. Queue mechanics remain unchanged.
- The engine accepts an INJECTED :class:`~live_mem.core.write_sink.WriteSink`
  via the constructor, defaulting to
  :class:`~live_mem.core.write_sink.DirectLocalWriteSink`. Ordinary
  consolidation keeps its legacy storage calls, but #394 consumes the resolved
  sink only as a DirectLocal route proof before its compaction-capable pipeline
  can read or write. Routing the general write surface remains #8/#9.
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
from contextlib import contextmanager
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

from ..consolidation_queue import (
    ConsolidationQueueService,
    get_consolidation_queue,
)
from ..consolidator import (
    ConsolidatorService,
    _bound_direct_local_compaction_sink,
    _direct_local_compaction_authority,
    get_consolidator,
)
from ..write_sink import DirectLocalWriteSink, StagedWriteNotImplemented, WriteSink

if TYPE_CHECKING:  # pragma: no cover - typing only
    ProgressCallback = Callable[[dict], Union[Awaitable[None], None]]


# ─────────────────────────────────────────────────────────────────────────────
# WriteSink durable-mutation call-site enumeration (the #8/#9 deliverable).
#
# Anchored on SEMANTIC descriptions. The seven normal-consolidation line hints
# are maintained source anchors (and pinned by the focused engine test); the
# remaining branch hints are advisory. This is the FULL eventual WriteSink
# mutation set for the mid / bank write path, in TWO branches:
#
#   CONSOLIDATOR branch — ConsolidatorService.{consolidate,
#       _apply_prepared_normal_batch, compact_bank} (core/consolidator.py). These
#       are the calls the consolidate run / compaction perform directly today and
#       that #8/#9 will route through the injected sink. ``_write_results`` stays
#       as a validation/compatibility delegator, not a mutation call site.
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
        "method": "ConsolidatorService._apply_prepared_normal_batch",
        "key_pattern": "{space_id}/bank/<unicode-dup>",
        "line_hint": 5209,
        "description": (
            "Unicode-duplicate bank cleanup DELETE after every canonical bank "
            "write readback succeeds."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._apply_prepared_normal_batch",
        "key_pattern": "{space_id}/bank/{filename}",
        "line_hint": 5191,
        "description": (
            "Prepared normal bank-file PUT for every validated create, edit, or "
            "rewrite candidate."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._apply_prepared_normal_batch",
        "key_pattern": "{space_id}/_synthesis.md",
        "line_hint": 5222,
        "description": "Prepared normal synthesis markdown PUT (_synthesis.md).",
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put_json",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._apply_prepared_normal_batch",
        "key_pattern": "{space_id}/_meta.json",
        "line_hint": 5236,
        "description": (
            "Private direct-application metadata JSON PUT when ``skip_meta`` is "
            "false."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "delete_many",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService._apply_prepared_normal_batch",
        "key_pattern": "{space_id}/live/* (consumed notes)",
        "line_hint": 5281,
        "description": (
            "Private direct-application consumed-note DELETE_MANY when "
            "``defer_note_finalization`` is false, after bank/synthesis and "
            "applicable metadata verification."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "put_json",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService.consolidate",
        "key_pattern": "{space_id}/_meta.json",
        "line_hint": 3983,
        "description": (
            "Run-level metadata JSON PUT after all completed prepared batches "
            "are verified."
        ),
    },
    {
        "engine": "MidEngine",
        "branch": "consolidator",
        "op": "delete_many",
        "module": "live_mem.core.consolidator",
        "method": "ConsolidatorService.consolidate",
        "key_pattern": "{space_id}/live/* (consumed notes)",
        "line_hint": 3999,
        "description": (
            "Deferred consumed-note DELETE_MANY after the run-level metadata "
            "write/readback; this is normal consolidate() finalization."
        ),
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

    Thin DI facade over ``ConsolidatorService`` + ``ConsolidationQueueService``.
    Every operation except the narrowly routed #394 manual-compaction
    DirectLocal-authority hand-off delegates verbatim and returns the wrapped
    result unchanged.

    The injected ``WriteSink`` remains inert for ordinary durable writes. #394
    consumes a registry-issued DirectLocal capability only for manual
    compaction; ordinary consolidation re-resolves at its time of use. The
    wider write-sink migration remains #8/#9.

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
        direct_local_compaction_authority: object | None = None,
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
        # Only EngineRegistry supplies this private, space-bound proof after a
        # successful DIRECT_LOCAL route.  A default/injected bare sink remains
        # useful for DI, but never becomes route authority by itself.
        self._direct_local_compaction_authority = direct_local_compaction_authority

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
        """Delegate to ``ConsolidatorService.consolidate`` under route proof.

        Full signature preserved, including ``progress_callback`` and the
        ``enforce_cooldown=True`` default (matching the wrapped service). Durable
        bank/synthesis/meta writes + consumed-notes delete stay inside the
        consolidator (``WRITE_SINK_MUTATION_CALL_SITES``). Even a
        registry-built engine leaves the route proof to the service: this
        engine can be retained after its space changes lifecycle state, so a
        stale DirectLocal capability must not cross into consolidation. An
        unrouted engine likewise cannot donate its bare default sink.
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
        if not isinstance(self._write_sink, DirectLocalWriteSink):
            raise StagedWriteNotImplemented(
                op="consolidate", key=f"{space_id}/bank/"
            )
        # An engine instance cannot donate a possibly stale sink/capability.
        # The service performs a fresh registry route before it reads or writes.
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

    @contextmanager
    def _tool_compaction_authority(self, space_id: str):
        """Scope a registry verdict to the immediate ``bank_compact`` call.

        The public :meth:`compact_bank` method intentionally does *not* bind
        this capability: a caller can retain a ``MidEngine`` beyond a lifecycle
        transition, and a stale DirectLocal verdict must then be re-resolved by
        the consolidator. The MCP tool opens this narrow context only for the
        call it has just authorized; the compactor separately re-resolves its
        final DirectLocal route after planning and before persistent apply.
        """

        authority = self._direct_local_compaction_authority
        if authority is None:
            raise RuntimeError("bank_compact requires a registry-issued route")
        with _direct_local_compaction_authority(authority):
            bound_sink = _bound_direct_local_compaction_sink(space_id)
            if bound_sink is None or bound_sink is not self._write_sink:
                raise RuntimeError("bank_compact route authority does not match space")
            yield

    async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
        """Delegate a scan or mutating compaction.

        A tool-scoped DirectLocal authority, if present, lets the immediately
        preceding route decision flow into the consolidator without a second
        resolver call.  Ordinary/public invocations never bind it and therefore
        make the service resolve the current lifecycle route before any effect.
        A dry run stays a read-only verbatim scan.
        """
        if dry_run:
            return await self._consolidator.compact_bank(space_id, dry_run=True)
        if not isinstance(self._write_sink, DirectLocalWriteSink):
            raise StagedWriteNotImplemented(op="compact", key=f"{space_id}/bank/")
        # See consolidate(): an engine's sink (including one obtained earlier
        # from the registry) does not by itself prove the current space route.
        # Unless the MCP tool opened the one-call context above, the
        # consolidator resolves freshly and fails closed after a transition.
        return await self._consolidator.compact_bank(space_id, dry_run=False)
