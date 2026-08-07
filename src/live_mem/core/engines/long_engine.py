# -*- coding: utf-8 -*-
"""
LongEngine — downstream ontology/knowledge-graph engine port (P3-5, EPIC-P3).

This module defines the ``long`` engine boundary mandated by ADR-0006 (four
internal engine ports: ``ShortEngine`` / ``MidEngine`` / ``LongEngine`` /
``HiveEngine``) and constrained by ADR-0010 (long memory is ontology-first and
**protocol-derived only**). In Wave 2 it is a *thin, behavior-preserving*
adapter over the existing :class:`live_mem.core.graph_bridge.GraphBridgeService`
— WRAP, DON'T REWRITE. Graph Memory itself is NOT imported here; that concrete
dependency lands in P4 *behind this port*, only after this port and the ``mid``
commit boundary exist (ADR-0006 import gate).

LONG-AUTHORITY INVARIANT (ADR-0006 §"Long is strictly downstream", ADR-0010 —
encoded STRUCTURALLY, not just in prose):

- **Downstream-only.** :class:`LongEngine` is an ontology/knowledge-graph engine
  that is *strictly downstream* of an already-committed mid bank version. It is
  NEVER on the commit / rollback / audit / tombstone / watermark-authority /
  recovery path.
- **Watermarks are inputs only.** :meth:`push` consumes a ``bank/*`` state that
  corresponds to a specific applied ``bank_version`` / ``commit_id``.
  ``bank_version`` / ``commit_id`` / ``term`` / provenance are treated as
  INPUTS / derived WATERMARKS only — bookkeeping that records "this graph
  reflects mid version N". They are never read back to decide Hivemind state;
  the authoritative copies live solely in ``{space_id}/_hivemind/commits`` and
  are read by ``hive`` / ``mid``, never by ``long``.
- **No commit-path method.** The surface is the legacy graph tool surface —
  ``connect`` / ``push`` / ``status`` / ``disconnect`` — plus the P4-4 typed
  downstream projection / read methods ``ingest`` / ``list_ontologies`` /
  ``query`` / ``search`` (GM-side reads and projections only). There is
  deliberately NO method whose return value the mid/hive commit path consumes:
  a ``long`` call can only fail or return graph / projection data, never
  authorize, invalidate, roll back, audit, or recover a commit. No
  ``assert_commit_allowed`` here; nothing decides commit validity.
- **No back-edge.** Nothing in the commit path
  (``assert_commit_allowed`` / ``BANK_COMMIT`` apply) calls into
  :class:`LongEngine`, and no ``BANK_COMMIT`` auto-triggers :meth:`push`.
  Projection is explicit, idempotent, and re-runnable.
- **No WriteSink, never writes ``_hivemind/``.** Unlike ``ShortEngine`` /
  ``MidEngine``, :class:`LongEngine` takes NO :class:`~live_mem.core.write_sink.WriteSink`
  and never writes Hivemind protocol state. It only delegates to
  :class:`~live_mem.core.graph_bridge.GraphBridgeService`, whose
  ``connect`` / ``disconnect`` write the LOCAL-ONLY ``graph_memory`` block in
  ``_meta.json`` (excluded from shared commits by the ADR-0004 metadata
  allowlist).
- **Import gate held.** This module imports ONLY from ``..graph_bridge`` (plus
  typing / ``__future__``). NO ``neo4j`` / ``qdrant`` / ``mcp`` client / new
  Graph Memory dependency is imported at this layer; ``graph_bridge.py`` imports
  ``mcp`` itself, so the engine stays one indirection away (concrete Graph
  Memory import is P4, behind this port).

Wave-2 rule: ``graph_push`` behavior is UNCHANGED — every method is a verbatim
pass-through (no validation, no logging, no retry, no error re-mapping, no
reshape of the returned dict). The SSRF URL validation (``_validate_gm_url``)
lives in the tool layer (``tools/graph.py``), NOT here, and is intentionally
not replicated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..graph_bridge import GraphBridgeService, get_graph_bridge

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["LongEngine"]


class LongEngine:
    """Thin, downstream-only adapter over :class:`GraphBridgeService`.

    Ontology / knowledge-graph engine, strictly downstream of a committed mid
    bank version. It is NEVER on the commit / rollback / audit / tombstone /
    watermark-authority / recovery path (ADR-0006, ADR-0010): no method
    participates in commit validity, and no commit-path code calls into it. The
    public surface is EXACTLY the legacy graph tool surface —
    ``connect`` / ``push`` / ``status`` / ``disconnect`` — delegating VERBATIM
    to the wrapped :class:`GraphBridgeService` with zero behavior change.

    :meth:`push` consumes an ALREADY-COMMITTED ``bank/*`` state corresponding to
    a specific applied ``bank_version`` / ``commit_id``; ``bank_version`` /
    ``commit_id`` / ``term`` / provenance are INPUTS / derived WATERMARKS only,
    never read back to decide Hivemind state.

    Takes NO ``WriteSink`` (only ``ShortEngine`` / ``MidEngine`` do) and never
    writes ``{space_id}/_hivemind/`` protocol state. NO Graph Memory /
    ``neo4j`` / ``qdrant`` import in this wave — the concrete import is P4,
    behind this port (ADR-0006 import gate).

    Constructed via DI; ``bridge`` defaults lazily to the
    :func:`get_graph_bridge` singleton (resolved at construction, not at
    import/def time, mirroring ``DirectLocalWriteSink``'s lazy pattern so no
    real MCP / network client is built early). Mono-tenant: every method keys on
    ``space_id`` only — no tenant concept.
    """

    def __init__(self, bridge: GraphBridgeService | None = None) -> None:
        # Lazy default: only resolve the singleton when no bridge was injected.
        # Tests inject a fake bridge; this never builds a real MCP/network
        # client at import/def time.
        self._bridge: GraphBridgeService = (
            bridge if bridge is not None else get_graph_bridge()
        )

    @property
    def bridge(self) -> GraphBridgeService:
        return self._bridge

    async def connect(
        self,
        space_id: str,
        url: str,
        token: str,
        memory_id: str,
        ontology: str = "general",
    ) -> dict:
        """Connect ``space_id`` to a Graph Memory instance (downstream config).

        Pass-through to :meth:`GraphBridgeService.connect`. Writes only the
        LOCAL-ONLY ``graph_memory`` block in ``_meta.json`` (via the bridge);
        never touches ``_hivemind/`` protocol state.
        """
        return await self._bridge.connect(
            space_id=space_id,
            url=url,
            token=token,
            memory_id=memory_id,
            ontology=ontology,
        )

    async def push(self, space_id: str, *, include_volatile: bool = False) -> dict:
        """Project the ALREADY-COMMITTED ``bank/*`` state into the graph.

        Pass-through to :meth:`GraphBridgeService.push`. Consumes a committed
        mid version; ``bank_version`` / ``commit_id`` / ``term`` / provenance are
        INPUTS / derived watermarks only. Explicit, idempotent, re-runnable; no
        ``BANK_COMMIT`` auto-triggers it and it never decides commit validity.

        ``include_volatile`` is forwarded VERBATIM (no validation here): the
        volatile-file filter lives in the bridge; the 'manage' permission gate
        and the structured audit emit live in the tool layer (ADR-0010).
        """
        return await self._bridge.push(space_id, include_volatile=include_volatile)

    async def status(self, space_id: str, *, include_graph: bool = False) -> dict:
        """Read graph / projection status (read-only; never a commit source).

        Pass-through to :meth:`GraphBridgeService.status`. A status read can only
        fail or return graph / projection data — never authorize or invalidate a
        commit.
        """
        if include_graph:
            return await self._bridge.status(space_id, include_graph=True)
        return await self._bridge.status(space_id)

    async def disconnect(
        self, space_id: str, *, use_embedded: bool = False
    ) -> dict:
        """Remove the local Graph Memory connection config for ``space_id``.

        Pass-through to :meth:`GraphBridgeService.disconnect`. Clears only the
        local-only ``graph_memory`` block in ``_meta.json`` by default.  With
        ``use_embedded=True``, replaces an explicit override with the
        embedded/local runtime after provisioning it.  Never touches
        ``_hivemind/`` protocol state, ingests documents, or deletes graph-side
        data.
        """
        if use_embedded:
            return await self._bridge.disconnect(space_id, use_embedded=True)
        # Preserve the legacy default call byte-for-byte for injected bridges.
        return await self._bridge.disconnect(space_id)

    # ─────────────────────────────────────────────────────────────────────
    # P4-4 — Typed downstream projection / read methods.
    #
    # Same verbatim pass-through pattern as the legacy four: the engine adds NO
    # validation, NO reshape, NO error re-mapping — it forwards to the bridge,
    # which owns SSRF (applied BEFORE any client is built) and the GM tool
    # mapping. These are projection / read surfaces only: a ``long`` call can
    # only fail or return graph / projection data, never authorize, invalidate,
    # roll back, audit, or recover a commit (ADR-0006 / ADR-0010). No new import
    # is introduced here — the import gate (only ``..graph_bridge``) is held.
    # ─────────────────────────────────────────────────────────────────────

    async def ingest(
        self,
        space_id: str,
        *,
        filename: str,
        content: str | None = None,
        content_base64: str | None = None,
        source_path: str | None = None,
        source_modified_at: str | None = None,
        metadata: dict | None = None,
        force: bool = False,
    ) -> dict:
        """Ingest ONE canonical document into the graph (GM-side projection).

        Pass-through to :meth:`GraphBridgeService.ingest`. XOR(content,
        content_base64) is enforced by the bridge; SSRF is validated before any
        client is built. Pure downstream projection — never reads/writes
        ``commit_id`` / ``bank_version`` / ``term`` and never touches
        ``_hivemind/``.
        """
        return await self._bridge.ingest(
            space_id,
            filename=filename,
            content=content,
            content_base64=content_base64,
            source_path=source_path,
            source_modified_at=source_modified_at,
            metadata=metadata,
            force=force,
        )

    async def list_ontologies(self, space_id: str) -> dict:
        """List ontologies available in the connected Graph Memory.

        Pass-through to :meth:`GraphBridgeService.list_ontologies` (read-only;
        never a commit source).
        """
        return await self._bridge.list_ontologies(space_id)

    async def query(self, space_id: str, query: str, limit: int = 10) -> dict:
        """Structured (no-LLM) query over the graph.

        Pass-through to :meth:`GraphBridgeService.query` (read-only; never a
        commit source).
        """
        return await self._bridge.query(space_id, query, limit=limit)

    async def search(self, space_id: str, query: str, limit: int = 10) -> dict:
        """Graph-first search over the knowledge graph.

        Pass-through to :meth:`GraphBridgeService.search` (read-only; never a
        commit source).
        """
        return await self._bridge.search(space_id, query, limit=limit)

    async def reindex(self, space_id: str) -> dict:
        """Run explicit embedding-projection maintenance for ``space_id``.

        Pass-through to :meth:`GraphBridgeService.reindex`. The bridge enforces
        the persisted embedded-runtime boundary and issues the one internal
        maintenance call. This derived projection remains outside every
        Hivemind commit, rollback, audit, and recovery decision.
        """
        return await self._bridge.reindex(space_id)

    async def plan_ingest(
        self,
        space_id: str,
        documents: list[dict],
        *,
        mode: str = "dry-run",
        include_volatile: bool = False,
    ) -> dict:
        """Plan canonical document ingestion (PLAN-ONLY, P4-7).

        Pass-through to :meth:`GraphBridgeService.plan_ingest`. The bridge owns
        the mode dispatch (dry-run / check-remote / apply-deferred), the
        ``source_path``-keyed planning and the read-only ``document_list``
        compare. Pure downstream projection — NO ``apply``/``commit``/
        ``rollback``/``audit`` authority, never touches ``_hivemind/``. The
        volatile-file rejection, the 'manage' opt-in gate and the structured
        audit live in the tool layer (ADR-0010); ``include_volatile`` is
        forwarded VERBATIM and ignored at the bridge.
        """
        return await self._bridge.plan_ingest(
            space_id, documents, mode=mode, include_volatile=include_volatile
        )
