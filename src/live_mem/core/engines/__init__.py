# -*- coding: utf-8 -*-
"""Internal engine adapters + the engine registry/factory (P3, ADR-0006/0007).

Thin, behaviour-preserving adapters that wrap the imported implementations
behind explicit ports:

- ``short`` — append-only notes over ``core/live.py`` ``LiveService``;
- ``mid`` — Markdown bank / consolidation over ``core/consolidator.py`` +
  ``core/consolidation_queue.py``;
- ``long`` — downstream-only, protocol-derived graph projection over
  ``core/graph_bridge.py`` ``GraphBridgeService`` (never on the commit path);
- ``hive`` — read-only coordination surface over ``core/hivemind/`` state +
  peer primitives.

The adapters wrap, they do not rewrite: non-Hivemind behaviour stays
byte-for-byte (ADR-0006 "wrap-don't-rewrite").

P3-7 (issue #56) — engine registry/factory + per-space ``WriteSink`` routing
==========================================================================

This module exposes :class:`EngineRegistry` plus a lazy process-wide singleton
accessor :func:`get_engine_registry`, mirroring the codebase's existing
``get_storage()`` / ``get_live_service()`` / ``get_consolidator()`` singleton
convention. The registry is the ONE place that, PER ``space_id``, resolves the
active :class:`~live_mem.core.write_sink.WriteSink` from the already-merged
read-only verdict :func:`live_mem.core.hivemind.lifecycle.resolve_write_route`
(P3-2):

- non-Hivemind  -> :class:`~live_mem.core.write_sink.DirectLocalWriteSink`
  (byte-for-byte legacy);
- Hivemind-healthy -> :class:`~live_mem.core.write_sink.StagedHivemindWriteSink`
  (fail-closed stub: every durable op raises ``StagedWriteNotImplemented``, no
  S3 write);
- corrupted -> ``CorruptedStateError`` PROPAGATES (never caught here, never
  downgraded to direct-local);
- unsafe / resync-required -> :class:`RegistryRefused` (a typed fail-closed
  refusal, distinct from the staged stub).

ROUTE-FIRST-THEN-DELEGATE: tools call :meth:`EngineRegistry.resolve_sink`
(directly, or via :meth:`short_engine` / :meth:`mid_engine` which resolve it
internally) BEFORE invoking any legacy-backed engine mutation, so a
Hivemind / corrupt / unsafe space fails closed before any legacy ``get_storage``
write runs; only ``DIRECT_LOCAL`` falls through to the verbatim legacy path.

WRAP-DON'T-REWRITE: the registry composes; it adds NO behaviour to the engine
modules (``short`` / ``mid`` / ``long_engine`` / ``hive``) and does NOT edit
``core/live.py`` / ``core/consolidator.py`` / ``core/graph_bridge.py``.

LAZY CONSTRUCTION (mandatory): ``ConsolidatorService.__init__`` builds
``AsyncOpenAI`` + ``httpx`` and ``StorageService`` builds ``boto3``. Every
dependency default is therefore resolved INSIDE the methods (never in
``__init__``) via the existing ``get_*`` singletons, so importing this package
never builds a real client. Engines are imported from their concrete modules
too, e.g.::

    from live_mem.core.engines.hive import HiveEngine
"""

from __future__ import annotations

from typing import Optional

from ..consolidation_queue import (
    ConsolidationQueueService,
    get_consolidation_queue,
)
from ..consolidator import ConsolidatorService, get_consolidator
from ..graph_bridge import GraphBridgeService, get_graph_bridge
from ..hivemind import (
    HivemindStateStore,
    WriteRoute,
    resolve_write_route,
)
from ..live import LiveService, get_live_service
from ..reservation_guard import assert_space_not_reserved
from ..storage import StorageService, get_storage
from ..write_sink import (
    DirectLocalWriteSink,
    StagedHivemindWriteSink,
    WriteSink,
)
from .hive import HiveEngine
from .long_engine import LongEngine
from .mid import MidEngine
from .short import ShortEngine

__all__ = [
    "EngineRegistry",
    "RegistryRefused",
    "get_engine_registry",
]


class RegistryRefused(RuntimeError):
    """Fail-closed refusal for a non-serviceable durable-write route.

    Raised by :meth:`EngineRegistry.resolve_sink` when
    :func:`resolve_write_route` returns :attr:`WriteRoute.REFUSE` (a Hivemind
    space in ``UNSAFE`` / ``RESYNC_REQUIRED``). This is DELIBERATELY DISTINCT
    from :class:`~live_mem.core.write_sink.StagedWriteNotImplemented`
    (lifecycle.py mandate): ``REFUSE`` is non-serviceable, whereas ``STAGED``
    is a deferred-but-serviceable seam (#8) that returns a sink which refuses at
    WRITE time. Merging them would lose the contract. A ``RuntimeError``
    subclass for consistency with the codebase's other domain errors
    (``CorruptedStateError`` / ``BootstrapError`` / ``StagedWriteNotImplemented``
    are all ``RuntimeError``). Carries ``space_id`` + ``route`` so a misrouted
    write is self-explaining; surfaced to callers via the tool's existing
    ``except Exception: return safe_error(...)`` path.
    """

    def __init__(self, space_id: str, route: WriteRoute) -> None:
        self.space_id = space_id
        self.route = route
        super().__init__(
            f"Durable write refused for space {space_id!r}: route={route.value} "
            "(Hivemind space is UNSAFE/RESYNC_REQUIRED — fail-closed, never "
            "degraded to direct-local). Resolve the node state (resync / "
            "re-import) before mutating."
        )


class EngineRegistry:
    """Process-wide engine factory + per-space ``WriteSink`` routing seam.

    Constructs the four internal engines and resolves, PER ``space_id``, the
    active :class:`~live_mem.core.write_sink.WriteSink` via the read-only
    verdict :func:`resolve_write_route`. Mirrors the codebase's lazy-singleton
    convention (``get_storage()`` / ``get_live_service()`` / ...).

    DI for tests: every dependency is injectable; production passes nothing and
    each default is resolved lazily inside the methods (NOT in ``__init__``) so
    importing this module never builds boto3 / AsyncOpenAI / httpx clients::

        reg = EngineRegistry(
            storage=FakeStorage(), live=fake, consolidator=fake,
            queue=fake, bridge=fake,
        )
        sink = await reg.resolve_sink("space-a")

    Caching strategy:
    - ``long_engine`` is space-agnostic (no ``WriteSink``, never writes
      ``_hivemind/``) — built once and cached on the registry.
    - ``short_engine`` / ``mid_engine`` are NOT cached across spaces: the active
      sink is a function of ``(space_id, current durable _hivemind/ state)`` and
      MUST be re-resolved on each mutation (state can change between calls).
      Construction is cheap (the engines only hold references); the lazy
      ``get_*`` singletons underneath stay process-wide, so no real client is
      rebuilt per call.
    - ``hive_engine`` builds a fresh ``HivemindStateStore`` (it carries
      ``space_id``) + ``HiveEngine`` per call — read-only and idempotent.
    """

    def __init__(
        self,
        *,
        storage: Optional[StorageService] = None,
        live: Optional[LiveService] = None,
        consolidator: Optional[ConsolidatorService] = None,
        queue: Optional[ConsolidationQueueService] = None,
        bridge: Optional[GraphBridgeService] = None,
    ) -> None:
        # Injected dependencies (None => resolve lazily inside the methods via
        # the get_* singletons). NEVER resolve a default here: that would build
        # a real client at construction time.
        self._storage = storage
        self._live = live
        self._consolidator = consolidator
        self._queue = queue
        self._bridge = bridge
        # Space-agnostic engine cache (no WriteSink).
        self._long: Optional[LongEngine] = None

    # ──────────────────────────────────────────────────────────────────
    # Lazy dependency resolvers — injected instance OR the get_* singleton.
    #
    # When a dependency was DI-injected we return it verbatim. Otherwise we
    # resolve the process-wide singleton FRESH on each call (NOT cached on the
    # registry): this keeps the existing test patch seams reachable — a test
    # that patches ``live_mem.core.storage.get_storage`` AFTER the singleton
    # registry was constructed must still be honoured. The singletons are
    # themselves process-wide, so resolving fresh does not rebuild any client.
    # ──────────────────────────────────────────────────────────────────

    def _storage_dep(self) -> StorageService:
        return self._storage if self._storage is not None else get_storage()

    def _live_dep(self) -> LiveService:
        return self._live if self._live is not None else get_live_service()

    def _consolidator_dep(self) -> ConsolidatorService:
        return (
            self._consolidator
            if self._consolidator is not None
            else get_consolidator()
        )

    def _queue_dep(self) -> ConsolidationQueueService:
        return self._queue if self._queue is not None else get_consolidation_queue()

    def _bridge_dep(self) -> GraphBridgeService:
        return self._bridge if self._bridge is not None else get_graph_bridge()

    # ──────────────────────────────────────────────────────────────────
    # THE SINGLE ROUTING SEAM — WriteRoute -> WriteSink.
    # ──────────────────────────────────────────────────────────────────

    async def resolve_sink(self, space_id: str) -> WriteSink:
        """Resolve the active durable-write sink for ``space_id`` (the seam).

        Delegates to the already-merged read-only resolver
        :func:`resolve_write_route` (P3-2) and maps the 3-valued verdict:

        - ``DIRECT_LOCAL`` (non-Hivemind, the ONLY direct-local path)
          -> :class:`DirectLocalWriteSink`;
        - ``STAGED`` (Hivemind-healthy) -> :class:`StagedHivemindWriteSink`
          (fail-closed stub; refuses at WRITE time, never at resolve time, so a
          healthy hive resolves a sink successfully — the seam exists for #8);
        - ``REFUSE`` (Hivemind unsafe/resync) -> raise :class:`RegistryRefused`
          (never returns a sink, never falls back to direct-local).

        ``CorruptedStateError`` from ``resolve_write_route`` is NOT caught here:
        it PROPAGATES through ``short_engine`` / ``mid_engine`` into the tool's
        ``except Exception: return safe_error(...)``. A corrupt Hivemind space
        therefore surfaces as a safe_error and CRUCIALLY never reaches
        ``DirectLocalWriteSink`` (only ``is_hive == False`` yields
        ``DIRECT_LOCAL``).

        For ``DIRECT_LOCAL`` the sink is built so the existing tool/storage
        patch seams keep working: when no storage was DI-injected we construct
        ``DirectLocalWriteSink()`` with no storage arg, so it resolves
        ``get_storage()`` lazily at the ``live_mem.core.write_sink`` seam that
        existing tests patch. When a storage was injected (tests) we pass it
        explicitly so routing is deterministic against the same fake.
        """
        await assert_space_not_reserved(space_id)
        storage = self._storage_dep()
        route = await resolve_write_route(storage, space_id)  # CorruptedStateError propagates

        if route is WriteRoute.DIRECT_LOCAL:
            # Honour the existing patch seam in production (no injected storage):
            # let DirectLocalWriteSink resolve get_storage() lazily. In the DI
            # path, bind the SAME injected storage for deterministic routing.
            if self._storage is not None:
                return DirectLocalWriteSink(storage=self._storage)
            return DirectLocalWriteSink()

        if route is WriteRoute.STAGED:
            # P5-8 (#16) CAPSTONE: build the real staged-commit sink. Imports are
            # LAZY/in-branch (no-client-at-import rule: these modules pull in the
            # Hivemind runtime). The sink BUFFERS put/put_json and drives ONE
            # atomic CommitRuntime.apply_commit on commit(), whose G0
            # assert_commit_allowed is the SINGLE authorization.
            from ..hivemind.commit_runtime import CommitRuntime
            from ..hivemind.lease_runtime import LeaseRuntime
            from ..hivemind.models import MemberStatus, TokenState
            from ..hivemind.note_replication import NoteReplicationRuntime
            from ..hivemind.queue_runtime import QueueRuntime
            from ..hivemind.state import HivemindStateStore

            store = HivemindStateStore(storage=storage, space_id=space_id)
            queue = QueueRuntime(store, space_id)
            lease = LeaseRuntime(store, space_id, queue)
            # Inject the note-replication runtime so apply_commit -> reap_on_tombstone
            # closes the P5-7 note-first-reorder window (a commit that tombstones a
            # note reaps its live copy). Short-tier only — no graph/long import.
            reaper = NoteReplicationRuntime(store, storage, space_id)
            crt = CommitRuntime(
                store, storage, space_id, lease, note_replication=reaper
            )

            node = await store.get_node_identity()
            token = await store.get_token()
            membership = await store.get_membership()
            # FAIL-CLOSED: a STAGED route with no identity / no HELD token / no
            # membership cannot commit — surface a typed refusal, NEVER a direct
            # write. This is the chosen fail-closed deferral of the lease
            # ACQUISITION ceremony: the sink assumes the LOCAL node already HOLDS
            # the token; assert_commit_allowed is the sole verifier at commit time
            # (token/term/pointer are re-read live there, so a lease that expires
            # between resolve and commit is caught FENCED — no TOCTOU).
            #
            # CRITICAL: requiring HELD is NOT enough — the local node must be the
            # ACTUAL holder. A token HELD by ANOTHER node would otherwise build a
            # sink bound to OUR identity; that sink would fail at commit time
            # (assert_commit_allowed -> NOT_HOLDER), but only AFTER staging bytes
            # were written. We fail closed HERE so a non-holder never even reaches
            # the staged-commit body.
            #
            # MEMBERSHIP GATE (P5-8 review fix): assert_commit_allowed is the SOLE
            # commit-auth verifier but is DELIBERATELY NOT a membership gate
            # (lease_runtime: "AUCUN contrôle de membership/permission") — it only
            # checks token/term/pointer. So a local HELD token alone is NOT proof
            # the local node still belongs to the hive: an EVICTED node, or a node
            # whose token was granted under a SUPERSEDED membership epoch, could
            # otherwise commit (token/term/pointer still match) and violate the
            # ACTIVE-membership / all-ACK model. There is no later defense. We fail
            # closed HERE unless the local node is an ACTIVE member of the CURRENT
            # membership AND the token was granted at the CURRENT membership epoch.
            local_is_active_member = membership is not None and any(
                m.node_id == node.node_id
                and m.status == MemberStatus.ACTIVE.value
                for m in membership.members
            ) if node is not None else False
            if (
                node is None
                or token is None
                or token.state != TokenState.HELD.value
                or token.holder_node_id != node.node_id
                or membership is None
                or not local_is_active_member
                or token.membership_epoch != membership.epoch
            ):
                raise RegistryRefused(space_id, route)

            return StagedHivemindWriteSink(
                space_id,
                storage,
                store=store,
                commit_runtime=crt,
                lease=lease,
                local_node_id=node.node_id,
                fencing_token=token.fencing_token,
                membership_epoch=membership.epoch,
            )

        # WriteRoute.REFUSE — non-serviceable; never returns a sink, never
        # direct-local. Distinct from STAGED (see RegistryRefused docstring).
        raise RegistryRefused(space_id, route)

    # ──────────────────────────────────────────────────────────────────
    # Per-space engines (sink-bearing) — sink resolved FIRST, not cached.
    # ──────────────────────────────────────────────────────────────────

    async def short_engine(self, space_id: str) -> ShortEngine:
        """Construct a :class:`ShortEngine` for ``space_id`` with the resolved
        per-space sink.

        Resolves the sink FIRST (the gate): a Hivemind unsafe/resync space
        raises :class:`RegistryRefused` and a corrupt space propagates
        ``CorruptedStateError`` BEFORE the engine (and the legacy
        ``LiveService.write_note`` it wraps) ever runs. A healthy hive returns
        a :class:`ShortEngine` carrying a :class:`StagedHivemindWriteSink`; a
        non-Hivemind space returns one carrying a :class:`DirectLocalWriteSink`.

        NOTE (route-first contract): ``ShortEngine.write_note`` still delegates
        to ``LiveService.write_note``, which calls ``get_storage()`` directly —
        the held sink is inert. Callers MUST therefore drive the STAGED refusal
        themselves (see ``tools/live.py`` ``live_note``): for a non-DIRECT_LOCAL
        sink, do not call ``write_note``. The registry does not edit
        ``live.py`` (wrap-don't-rewrite).
        """
        sink = await self.resolve_sink(space_id)
        return ShortEngine(live=self._live_dep(), write_sink=sink)

    async def mid_engine(self, space_id: str) -> MidEngine:
        """Construct a :class:`MidEngine` for ``space_id`` with the resolved
        per-space sink.

        Resolves the sink FIRST (the gate): unsafe/resync -> ``RegistryRefused``,
        corrupt -> ``CorruptedStateError`` propagates, BEFORE the consolidator's
        own ``get_storage()`` writes run. Not cached across spaces.
        """
        sink = await self.resolve_sink(space_id)
        return MidEngine(
            consolidator=self._consolidator_dep(),
            queue=self._queue_dep(),
            write_sink=sink,
        )

    # ──────────────────────────────────────────────────────────────────
    # Space-agnostic engine (no WriteSink) — cached.
    # ──────────────────────────────────────────────────────────────────

    def long_engine(self) -> LongEngine:
        """Return the cached space-agnostic :class:`LongEngine`.

        Takes NO ``WriteSink`` and never writes ``_hivemind/`` (downstream-
        derived projection, ADR-0010). Built once and cached; constructed lazily
        over the bridge dependency so importing this module builds no MCP /
        network client.
        """
        if self._long is None:
            self._long = LongEngine(bridge=self._bridge_dep())
        return self._long

    # ──────────────────────────────────────────────────────────────────
    # Read-only coordination surface — fresh per call.
    # ──────────────────────────────────────────────────────────────────

    def hive_engine(self, space_id: str) -> HiveEngine:
        """Return a read-only :class:`HiveEngine` over a fresh
        :class:`HivemindStateStore` for ``space_id``.

        Read-only and idempotent; takes NO ``WriteSink``. The store carries
        ``space_id`` (mono-tenant). Built per call (cheap; reads are idempotent).
        """
        store = HivemindStateStore(storage=self._storage_dep(), space_id=space_id)
        return HiveEngine(store)


# =============================================================================
# Lazy process-wide singleton — mirrors get_storage() / get_live_service() etc.
# =============================================================================

_REGISTRY: Optional[EngineRegistry] = None


def get_engine_registry() -> EngineRegistry:
    """Return the lazily-constructed process-wide :class:`EngineRegistry`.

    Production passes nothing -> all dependencies resolve lazily on first use,
    so calling this never builds a real client by itself. Tests construct
    ``EngineRegistry(...)`` directly with fakes and never touch this singleton.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EngineRegistry()
    return _REGISTRY
