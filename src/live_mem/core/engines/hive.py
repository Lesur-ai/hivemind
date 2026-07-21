# -*- coding: utf-8 -*-
"""
HiveEngine — read-only coordination surface over
:class:`live_mem.core.hivemind.state.HivemindStateStore` (P3-4, EPIC-P3 /
ADR-0006).

HiveEngine (a.k.a. the HiveCoordinator port) is the single, stable read
boundary onto the Hivemind protocol state for the later coordination phases
(#6 queue ordering, #7 token/lease + ``assert_commit_allowed``, #8 staged
commit, #9 mutation protection, #11 GC). It WRAPS — never rewrites — the
already-merged ``HivemindStateStore`` (and optionally a
``HivemindPeerChannel``) with ZERO behavior change.

Wave-2 contract (ADR-0006 "wrap, don't rewrite" + EPIC-P3 In/Out of scope):

- NEW FILE ONLY. ``core/hivemind/state.py`` / ``lifecycle.py`` / ``peer.py`` /
  ``models.py`` are NOT edited by this child.
- Every method is a one-line delegation to an existing ``HivemindStateStore``
  method or to an imported ``lifecycle`` function. The facade adds NO caching,
  NO lock, NO argument pre-validation beyond what the store already does, and —
  critically — NO ``try``/``except`` around any read.
- NO coordination runtime is added: no queue driver, no lease/token state
  machine, no membership mutation, no ``assert_commit_allowed`` (those are
  #6/#7/#8). HiveEngine re-exposes existing IDEMPOTENT read primitives only.
- All monotonic guards (term / fencing_token / bank_version / epoch /
  watermark non-decreasing) are preserved by virtue of delegating to the
  unmodified store; the facade introduces no new write path.
- ``CorruptedStateError`` PROPAGATES UNCHANGED. The store raises it from
  ``_get_model`` (JSONDecodeError / ValidationError on any critical file) and
  from ``load_snapshot`` / ``resolve_hive_context``. The facade NEVER catches,
  swallows, or defaults it: a corrupt ``_hivemind/`` critical file must surface
  as unsafe/blocking, never as HEALTHY or "not shared". There is no
  catch-and-default anywhere in this module.
- Mono-tenant: the store already carries ``space_id`` (state.py public
  property); HiveEngine takes no tenant param and NO ``WriteSink`` (the hive
  surface is read-only — ``WriteSink`` is ShortEngine/MidEngine only).

The optional ``peer`` is HELD for #6/#7 wiring (a stable place to hand the
channel) and exposed read-only via :pyattr:`peer`. It is intentionally NOT
proxied: HiveEngine exposes no ``send`` / ``receive`` / ``sign_event`` — those
are transport/coordination runtime, out of scope for Wave-2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..hivemind import HivemindPeerChannel, HivemindStateStore, lifecycle

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..hivemind import (
        Ack,
        BankCommit,
        BankVersionPointer,
        EventEnvelope,
        HivemindStateSnapshot,
        MembershipView,
        NodeHealth,
        NodeIdentity,
        QueueEntry,
        TermState,
        TokenLeaseState,
        Tombstone,
        Watermark,
    )


class HiveEngine:
    """Read-only coordination surface over ``HivemindStateStore``.

    Thin DI facade. Exposes a single read aggregate :meth:`status` plus a set
    of read-through delegators (membership/epoch, term/token, queue, commits,
    tombstones, watermarks, acks, event journal). Every method delegates
    verbatim and returns the wrapped result unchanged.

    Constructed via DI::

        HiveEngine(store: HivemindStateStore,
                   peer: HivemindPeerChannel | None = None)

    The store already carries ``space_id`` (mono-tenant); ``peer`` is optional
    and held for #6/#7 only. HiveEngine takes NO ``WriteSink`` (read-only) and
    adds NO coordination runtime.

    ``CorruptedStateError`` raised by any delegated read propagates UNCHANGED —
    the facade never catches or defaults it.
    """

    def __init__(
        self,
        store: HivemindStateStore,
        peer: Optional[HivemindPeerChannel] = None,
    ) -> None:
        self._store = store
        self._peer = peer

    @property
    def space_id(self) -> str:
        """Pure forward of the wrapped store's ``space_id`` (mono-tenant)."""
        return self._store.space_id

    @property
    def peer(self) -> Optional[HivemindPeerChannel]:
        """The optional peer channel, HELD for #6/#7 wiring only.

        Exposed read-only as a stable handoff point; deliberately NOT proxied —
        HiveEngine offers no ``send`` / ``receive`` / ``sign_event`` (that would
        be coordination/transport runtime, out of scope for Wave-2).
        """
        return self._peer

    # ─────────────────────────────────────────────────────────────────
    # Aggregate read surface
    # ─────────────────────────────────────────────────────────────────

    async def status(self) -> dict:
        """Read aggregate sourced from the imported ``lifecycle.hive_status``.

        Returns the composed read-only Hivemind health dict (keys:
        ``space_id``, ``hive_status``, ``is_hive``, ``protocol_version``,
        ``membership_epoch``, ``peers``, ``expected_ack_node_ids``, ``term``,
        ``bank_version``, ``commit_id``, ``node_status``, ``reason``).

        Asymmetry (documented): ``lifecycle.hive_status`` takes a
        ``StorageService`` + ``space_id``, NOT the store. The engine is built
        FROM the store, so it reaches the store's private ``_storage`` and its
        public ``space_id`` to feed the lifecycle aggregate. This is the one
        delegation that goes "around" the store object; the aggregate is the
        EPIC-mandated source (P3-4 "consumes lifecycle.hive_status()"), so we do
        NOT rebuild the dict by hand or construct a second store.
        ``CorruptedStateError`` from ``resolve_hive_context`` / ``get_node_status``
        inside ``hive_status`` propagates unchanged.
        """
        return await lifecycle.hive_status(self._store._storage, self._store.space_id)

    async def load_snapshot(self) -> "HivemindStateSnapshot":
        """Delegate ``store.load_snapshot`` (full cold-start view; corruption on
        any sub-file propagates ``CorruptedStateError``)."""
        return await self._store.load_snapshot()

    # ─────────────────────────────────────────────────────────────────
    # Node identity / health
    # ─────────────────────────────────────────────────────────────────

    async def get_node_identity(self) -> "Optional[NodeIdentity]":
        """Delegate ``store.get_node_identity``."""
        return await self._store.get_node_identity()

    async def get_node_status(self) -> "Optional[NodeHealth]":
        """Delegate ``store.get_node_status`` (``None`` != HEALTHY — caller's
        concern; the facade just passes through)."""
        return await self._store.get_node_status()

    # ─────────────────────────────────────────────────────────────────
    # Membership / term / token
    # ─────────────────────────────────────────────────────────────────

    async def get_membership(self) -> "Optional[MembershipView]":
        """Delegate ``store.get_membership``."""
        return await self._store.get_membership()

    async def get_term(self) -> "Optional[TermState]":
        """Delegate ``store.get_term``."""
        return await self._store.get_term()

    async def get_token(self) -> "Optional[TokenLeaseState]":
        """Delegate ``store.get_token``."""
        return await self._store.get_token()

    async def get_bank_version_pointer(self) -> "Optional[BankVersionPointer]":
        """Delegate ``store.get_bank_version_pointer``."""
        return await self._store.get_bank_version_pointer()

    # ─────────────────────────────────────────────────────────────────
    # Queue (FIFO, sorted by sequence)
    # ─────────────────────────────────────────────────────────────────

    async def list_queue(self) -> "list[QueueEntry]":
        """Delegate ``store.list_queue`` (sorted by ``sequence``)."""
        return await self._store.list_queue()

    # ─────────────────────────────────────────────────────────────────
    # Bank commits
    # ─────────────────────────────────────────────────────────────────

    async def list_commits(self, since_bank_version: int = -1) -> "list[BankCommit]":
        """Delegate ``store.list_commits`` (ascending ``bank_version``; default
        ``-1`` matches the store)."""
        return await self._store.list_commits(since_bank_version)

    async def latest_commit(self) -> "Optional[BankCommit]":
        """Delegate ``store.latest_commit``."""
        return await self._store.latest_commit()

    async def get_commit(self, bank_version: int) -> "Optional[BankCommit]":
        """Delegate ``store.get_commit``."""
        return await self._store.get_commit(bank_version)

    # ─────────────────────────────────────────────────────────────────
    # Tombstones / watermarks
    # ─────────────────────────────────────────────────────────────────

    async def list_tombstones(self) -> "list[Tombstone]":
        """Delegate ``store.list_tombstones``."""
        return await self._store.list_tombstones()

    async def list_watermarks(self) -> "list[Watermark]":
        """Delegate ``store.list_watermarks``."""
        return await self._store.list_watermarks()

    async def get_watermark(self, node_id: str) -> "Optional[Watermark]":
        """Delegate ``store.get_watermark``."""
        return await self._store.get_watermark(node_id)

    # ─────────────────────────────────────────────────────────────────
    # ACKs
    # ─────────────────────────────────────────────────────────────────

    async def list_acks(self, event_id: str) -> "list[Ack]":
        """Delegate ``store.list_acks``."""
        return await self._store.list_acks(event_id)

    async def count_acks(self, event_id: str) -> int:
        """Delegate ``store.count_acks`` (which delegates to ``list_acks``, so a
        corrupt ``acks/{event_id}/{node}.json`` surfaces ``CorruptedStateError``
        instead of being miscounted into a quorum)."""
        return await self._store.count_acks(event_id)

    # ─────────────────────────────────────────────────────────────────
    # Event journal (append-only, source of truth for dedup)
    # ─────────────────────────────────────────────────────────────────

    async def list_events(
        self,
        since_ts: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> "list[EventEnvelope]":
        """Delegate ``store.list_events`` (chronological; signature matches the
        store exactly)."""
        return await self._store.list_events(since_ts=since_ts, limit=limit)

    async def get_event(self, event_id: str) -> "Optional[EventEnvelope]":
        """Delegate ``store.get_event``."""
        return await self._store.get_event(event_id)

    async def has_event(self, event_id: str) -> bool:
        """Delegate ``store.has_event``."""
        return await self._store.has_event(event_id)

    # ─────────────────────────────────────────────────────────────────
    # ACK expectations (derived, sync lifecycle helper)
    # ─────────────────────────────────────────────────────────────────

    async def expected_ack_node_ids(self) -> list[str]:
        """node_ids whose ACK is expected (conservative all-ACK).

        Delegates to the SYNC module-level ``lifecycle.expected_ack_node_ids``
        over the current membership. This method is async only because it must
        ``await store.get_membership()`` first. Calling the imported MODULE's
        function (not a same-named store method) avoids shadowing the symbol.
        """
        return lifecycle.expected_ack_node_ids(await self._store.get_membership())
