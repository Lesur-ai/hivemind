# -*- coding: utf-8 -*-
"""
P4-9 (EPIC #6) — long/commit isolation RUNTIME gate (execute-and-assert).

The STATIC half (AST negative-import + graph_memory-not-shared) lives in
``tests/test_long_isolation.py`` (P4-9 static subset, PR #67). This module adds
the RUNTIME proofs that needed P4-4's ``FakeGraphTransport`` and the P3-8
consolidation harness — the anti-complacent core of the P4-9 release gate:

- a REAL consolidation against a CONNECTED space (a space WITH a ``graph_memory``
  block) makes ZERO graph contact: a tripwire Graph Memory client fails loudly if
  the consolidation path ever constructs one / attempts a push. The same proof
  shows a broken/slow graph can never block or fail a mid operation (decoupling);
- an end-to-end ``connect`` + ``push`` records the P4-5 derived watermark and the
  P4-8 ``bank_mirror`` ledger into the LOCAL ``graph_memory`` block, and the
  shared ``_meta`` projection excludes ALL of it (token / watermark / mirror).

DEFERRED (NOT a silent cap): the ``BANK_COMMIT``-apply-triggers-no-push runtime
proof needs the protocol #8 commit-apply path, which is not landed. The current
guarantee is the AST negative-import gate (the commit path imports no long/graph
module — static P4-9) PLUS the consolidation runtime proof here. When #8 lands,
add a fake commit-apply that asserts zero ``FakeGraphTransport`` calls.

Offline: stubbed LLM + frozen clock + FakeStorage + FakeGraphTransport. No real
network / S3 / Neo4j / Qdrant / LLM.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from live_mem.core.engines.long_engine import LongEngine
from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.models import meta_local_complement, meta_shared_projection
from tests.fakes import FakeGraphTransport

# Reuse the proven P3-8 offline consolidation harness (real ConsolidatorService,
# stubbed _call_llm, frozen clock, in-memory storage).
from tests.test_p3_byte_for_byte_compat import (
    ConsolidatorFakeStorage,
    _Dt,
    _NOTE_KEYS,
    _SPACE,
    _make_stubbed_consolidator,
)

_GM = {
    "url": "https://gm.example.com", "token": "tok-secret", "memory_id": "mem-1",
    "ontology": "general", "last_push": None, "push_count": 0, "files_pushed": 0,
}


class _GraphTripwire:
    """Constructing OR using this == a push attempt. The consolidation path must
    never reach it (the long tier is strictly downstream; consolidation never
    pushes — ADR-0010). Any construction fails the test loudly."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "ISOLATION VIOLATION (P4-9): the consolidation path constructed a "
            "Graph Memory client — i.e. attempted a long/graph push. The long "
            "tier is downstream-only and must never be on the mid commit path."
        )


async def _seed_connected(storage: ConsolidatorFakeStorage) -> None:
    """A space CONNECTED to Graph Memory (graph_memory block present) + 3 notes
    (one consolidatable batch, mirrors the golden harness _seed)."""
    await storage.put_json(
        f"{_SPACE}/_meta.json", {"consolidation_count": 0, "graph_memory": dict(_GM)}
    )
    await storage.put(f"{_SPACE}/_rules.md", "# Rules\n\nBe concise.\n")
    await storage.put(_NOTE_KEYS[0], "Observed: the build is green.\n")
    await storage.put(_NOTE_KEYS[1], "Decided: ship on Friday.\n")
    await storage.put(_NOTE_KEYS[2], "Observed: latency is nominal.\n")


# --------------------------------------------------------------------------- #
# RUNTIME proof 1 — consolidation of a connected space never pushes / is        #
# decoupled from a broken graph                                                 #
# --------------------------------------------------------------------------- #


async def test_consolidation_of_connected_space_makes_zero_graph_contact() -> None:
    consolidator = _make_stubbed_consolidator()
    storage = ConsolidatorFakeStorage()
    await _seed_connected(storage)
    with (
        patch("live_mem.core.consolidator.get_storage", return_value=storage),
        patch("live_mem.core.consolidator.datetime", _Dt),
        # If consolidation EVER reaches the graph, this tripwire raises.
        patch("live_mem.core.graph_bridge.GraphMemoryClient", _GraphTripwire),
    ):
        res = await consolidator.consolidate(_SPACE, enforce_cooldown=False)

    # The consolidation SUCCEEDED without ever constructing a graph client —
    # proving both: (a) the consolidator never pushes, and (b) a broken/slow
    # graph can never block or fail a mid commit (decoupling).
    assert res.get("status") == "ok", res
    assert res.get("notes_processed") == len(_NOTE_KEYS), res

    # The local graph_memory block was never mutated by consolidation (no push).
    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    assert meta.get("graph_memory", {}).get("push_count", 0) == 0


# --------------------------------------------------------------------------- #
# RUNTIME proof 2 — end-to-end: a real push records watermark + bank_mirror      #
# LOCALLY, and the shared projection excludes all of it                          #
# --------------------------------------------------------------------------- #


async def test_push_records_watermark_locally_and_shared_projection_excludes_it() -> None:
    storage = ConsolidatorFakeStorage()
    await storage.put_json(
        f"{_SPACE}/_meta.json",
        {"space_id": _SPACE, "version": 1, "graph_memory": dict(_GM)},
    )
    await storage.put(f"{_SPACE}/bank/systemPatterns.md", "patterns")

    engine = LongEngine(bridge=GraphBridgeService(client_factory=FakeGraphTransport.factory()))
    with patch("live_mem.core.graph_bridge.get_storage", return_value=storage):
        res = await engine.push(_SPACE)
    assert res["status"] == "ok", res

    meta = await storage.get_json(f"{_SPACE}/_meta.json")
    gm = meta["graph_memory"]
    # P4-8 bank_mirror ledger + P4-5 watermark recorded LOCALLY.
    assert "bank_mirror" in gm
    assert "provenance" in gm  # watermark recorded (coords null/"not available")

    # The shared projection excludes the ENTIRE graph_memory block — token,
    # watermark coords, bank_mirror ledger, push metrics — none may replicate.
    shared = meta_shared_projection(meta)
    assert "graph_memory" not in shared
    flat = json.dumps(shared)
    for leaked in ("tok-secret", "bank_mirror", "bank_version", "systemPatterns", "gm.example.com"):
        assert leaked not in flat, f"{leaked!r} leaked into the shared projection"

    # ...and it lives wholly in the local complement.
    assert "graph_memory" in meta_local_complement(meta)
