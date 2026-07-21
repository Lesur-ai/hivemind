# -*- coding: utf-8 -*-
"""
Test harness P5-7 (folds P5-2 #3) — l'oracle anti-résurrection cross-store.

Sur un ``ClusterHarness`` réel : on écrit une VRAIE ``live/{filename}`` sur un
nœud via ``NoteReplicationRuntime.replicate_inbound`` (objet S3 réel, pas un
stub), puis on prouve que ``assert_no_tombstone_resurrection`` :

- FIRE (``AssertionError``) si un tombstone existe sans reap de la copie ;
- PASSE après ``reap_on_tombstone`` ;
- PASSE quand le tombstone-first bloque la copie en amont.

C'est ce qui rend l'oracle NON-VACUEUX (l'ancien stub ``model.replicate_note``
n'écrivait rien — l'oracle scanne le STORAGE, pas une valeur de retour).
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    HivemindStateStore,
    Tombstone,
)
from live_mem.core.hivemind.note_replication import (
    NoteReplicationRuntime,
    ReplicatedNote,
    ReplicationStatus,
)

from tests.hivemind_harness import (
    ClusterHarness,
    DeterministicClock,
    assert_invariants,
    assert_no_tombstone_resurrection,
)


# =============================================================================
# Helpers
# =============================================================================


def _note_md(*, agent: str, category: str, body: str, ts: str) -> str:
    return (
        "---\n"
        f'timestamp: "{ts}"\n'
        f'agent: "{agent}"\n'
        f'category: "{category}"\n'
        "tags: []\n"
        'space_id: "alpha"\n'
        "---\n\n"
    ) + body


def _make_note(*, stem: str, origin_node_id: str) -> ReplicatedNote:
    md = _note_md(agent="cline", category="observation", body="peer note", ts="2026-01-01T00:00:00+00:00")
    return ReplicatedNote(
        note_id=stem,
        filename=f"{stem}.md",
        origin_node_id=origin_node_id,
        origin_agent="cline",
        category="observation",
        content="peer note",
        created_at="2026-01-01T00:00:00+00:00",
        note_md=md,
    )


def _runtime_for(cluster: ClusterHarness, node_id: str) -> NoteReplicationRuntime:
    node = cluster.nodes[node_id]
    store: HivemindStateStore = node.store
    return NoteReplicationRuntime(
        store, node.storage, cluster.space_id, clock=cluster.clock.now
    )


# =============================================================================
# H1 — replicate -> tombstone (sans reap) caught by oracle ; reap -> green
# =============================================================================


@pytest.mark.asyncio
async def test_replicate_then_tombstone_then_reorder_caught_by_oracle() -> None:
    """L'oracle FIRE sur une vraie résurrection (copie live + tombstone, pas de
    reap), nomme le nœud + note_id ; après ``reap_on_tombstone`` il PASSE et
    ``assert_invariants`` passe. Prouve l'oracle NON-VACUEUX."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(node_ids=["nodeA", "nodeB"], clock=clock)
    await assert_invariants(cluster)  # cluster sain au départ

    rt_b = _runtime_for(cluster, "nodeB")
    stem = "20260101T000000_cline_observation_aa11bb22"
    note = _make_note(stem=stem, origin_node_id="nodeA")

    # Réplication : une VRAIE live/{filename} apparaît sur nodeB.
    r = await rt_b.replicate_inbound(
        note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.STORED
    key = f"{cluster.space_id}/live/{note.filename}"
    assert await cluster.nodes["nodeB"].storage.exists(key) is True
    # Cluster encore sain (aucun tombstone).
    await assert_no_tombstone_resurrection(cluster)

    # Le tombstone arrive APRÈS la copie, mais on NE reap PAS (simule le bug).
    await cluster.nodes["nodeB"].store.add_tombstone(
        Tombstone(note_id=stem, deleted_by_node_id="nodeA", bank_version=1)
    )

    # L'oracle DOIT fire, nommant nodeB + le note_id.
    with pytest.raises(AssertionError) as exc:
        await assert_no_tombstone_resurrection(cluster)
    assert "nodeB" in str(exc.value)
    assert stem in str(exc.value)

    # Reap : la copie + sidecar partent -> oracle vert + invariants verts.
    removed = await rt_b.reap_on_tombstone(stem)
    assert removed is True
    assert await cluster.nodes["nodeB"].storage.exists(key) is False
    await assert_no_tombstone_resurrection(cluster)
    await assert_invariants(cluster)


# =============================================================================
# H2 — tombstone-first blocks the later note (write-gate)
# =============================================================================


@pytest.mark.asyncio
async def test_tombstone_first_blocks_later_note() -> None:
    """``add_tombstone`` AVANT ``replicate_inbound`` -> REJECTED_TOMBSTONED, la
    ``live/{filename}`` n'est JAMAIS créée, l'oracle passe. Prouve que la
    write-gate bloque la résurrection en amont."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(node_ids=["nodeA", "nodeB"], clock=clock)

    rt_b = _runtime_for(cluster, "nodeB")
    stem = "20260101T000000_cline_observation_cc33dd44"
    note = _make_note(stem=stem, origin_node_id="nodeA")

    # Tombstone d'abord.
    await cluster.nodes["nodeB"].store.add_tombstone(
        Tombstone(note_id=stem, deleted_by_node_id="nodeA", bank_version=1)
    )

    r = await rt_b.replicate_inbound(
        note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.REJECTED_TOMBSTONED
    key = f"{cluster.space_id}/live/{note.filename}"
    assert await cluster.nodes["nodeB"].storage.exists(key) is False

    # L'oracle passe (aucune résurrection) + invariants verts.
    await assert_no_tombstone_resurrection(cluster)
    await assert_invariants(cluster)


# =============================================================================
# H3 — l'oracle attrape un objet live tombstoné SANS ``.md`` (bypass fermé)
# =============================================================================


@pytest.mark.asyncio
async def test_oracle_catches_tombstoned_non_md_live_object() -> None:
    """L'oracle anti-résurrection scanne MAINTENANT tous les objets ``live/`` (pas
    seulement ``.md``) : un objet live extensionless tombstoné — que
    ``reap_on_tombstone`` ({note_id}.md) ne supprimerait pas — est ATTRAPÉ. RED
    avec l'ancien oracle qui ``continue``-ait sur les objets non-``.md``."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(node_ids=["nodeA", "nodeB"], clock=clock)
    await assert_no_tombstone_resurrection(cluster)  # sain au départ

    # On PLANTE directement un objet live SANS extension ``.md`` (le bypass que la
    # garde d'écriture de note_id_from_filename ferme désormais ; ici on simule un
    # objet déjà présent sur disque) + un tombstone du MÊME note_id (== basename).
    stem = "foo_extensionless"
    storage = cluster.nodes["nodeB"].storage
    await storage.put(f"{cluster.space_id}/live/{stem}", "raw bytes, no front-matter")
    await cluster.nodes["nodeB"].store.add_tombstone(
        Tombstone(note_id=stem, deleted_by_node_id="nodeA", bank_version=1)
    )

    # L'oracle DOIT fire, nommant nodeB + le note_id (basename brut).
    with pytest.raises(AssertionError) as exc:
        await assert_no_tombstone_resurrection(cluster)
    assert "nodeB" in str(exc.value)
    assert stem in str(exc.value)


# =============================================================================
# H4 — l'oracle attrape un objet live tombstoné d'extension étrangère ``*.keep``
# (skip ``.keep`` réduit à la sentinelle bootstrap exacte — Codex BLOCKING)
# =============================================================================


@pytest.mark.asyncio
async def test_oracle_catches_tombstoned_keep_extension_live_object() -> None:
    """L'oracle ne saute QUE la sentinelle bootstrap EXACTE ``live/.keep`` : un
    objet live d'extension étrangère terminant par ``.keep`` (``live/foo.keep``,
    note_id == ``foo.keep``) tombstoné DOIT être attrapé. RED avec l'ancien skip
    ``rel.endswith(".keep")`` qui ignorait tout ``*.keep`` (résurrection
    silencieuse)."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(node_ids=["nodeA", "nodeB"], clock=clock)

    # La sentinelle bootstrap réelle ``live/.keep`` est présente et tombstonée par
    # erreur : elle ne doit JAMAIS faire fire l'oracle (skip légitime, exact).
    await cluster.nodes["nodeB"].storage.put(f"{cluster.space_id}/live/.keep", "")
    await cluster.nodes["nodeB"].store.add_tombstone(
        Tombstone(note_id=".keep", deleted_by_node_id="nodeA", bank_version=1)
    )
    await assert_no_tombstone_resurrection(cluster)  # la sentinelle reste skippée

    # Objet live d'extension étrangère ``live/foo.keep`` + tombstone du MÊME
    # note_id (basename brut, == ``foo.keep``). N'est PAS la sentinelle bootstrap.
    stem = "foo.keep"
    storage = cluster.nodes["nodeB"].storage
    await storage.put(f"{cluster.space_id}/live/{stem}", "raw bytes, foreign-extension")
    await cluster.nodes["nodeB"].store.add_tombstone(
        Tombstone(note_id=stem, deleted_by_node_id="nodeA", bank_version=1)
    )

    # L'oracle DOIT fire, nommant nodeB + le note_id (basename brut ``foo.keep``).
    with pytest.raises(AssertionError) as exc:
        await assert_no_tombstone_resurrection(cluster)
    assert "nodeB" in str(exc.value)
    assert stem in str(exc.value)
