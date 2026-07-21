# -*- coding: utf-8 -*-
"""
Tests d'invariants cross-nœud FOLDÉS pour P5-6 (issue #14).

Pilote le VRAI ``CommitRuntime.apply_commit`` par-nœud sur un ``ClusterHarness``
multi-nœuds, puis lève les invariants du harnais (``assert_bank_version_monotone``
contiguïté+parenté, ``assert_no_stale_term_commit``, ``assert_commits_consistent``)
pour prouver qu'un commit non-contigu / stale-term est ATTRAPÉ et que le chemin
d'apply réel converge en full-mesh all-ACK.

Chaque nœud a son storage souverain : pour P5-6 (le transport est P5-7) on STAGE
les octets sur le storage de chaque nœud avant son apply local — le pair
reconstruit son ``CommitIntent`` depuis le ``BankCommit`` + SON propre état
chargé, et gate via ``assert_commit_allowed`` (le MÊME prédicat des deux côtés).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    CommitNotAuthorized,
    CommitRuntime,
    CommitDenyReason,
    LeaseRuntime,
    QueueRuntime,
    TokenLeaseState,
    TokenState,
    build_commit_intent,
    build_manifest,
    layout,
)
from tests.hivemind_harness import (
    ClusterHarness,
    DeterministicClock,
    assert_bank_version_monotone,
    assert_commits_consistent,
    assert_invariants,
    assert_no_stale_term_commit,
)


# =============================================================================
# Helpers
# =============================================================================


def _commit_rt(cluster: ClusterHarness, nid: str) -> CommitRuntime:
    """Construit un ``CommitRuntime`` réel sur le store/storage souverain du
    nœud ``nid`` (lease + queue par-nœud, horloge logique partagée)."""
    node = cluster.nodes[nid]
    store = node.store
    queue = QueueRuntime(store, cluster.space_id)
    lease = LeaseRuntime(store, cluster.space_id, queue, clock=cluster.clock.now)
    return CommitRuntime(
        store, node.storage, cluster.space_id, lease, clock=cluster.clock.now  # type: ignore[arg-type]
    )


async def _seed_held_token(
    cluster: ClusterHarness, nid: str, *, term: int, holder: str
) -> None:
    """Pose un token HELD vivant par ``holder`` au ``term`` sur le store de
    ``nid`` (lease 300 s devant l'horloge logique)."""
    until = (cluster.clock.now() + timedelta(seconds=300)).isoformat()
    await cluster.nodes[nid].store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=holder,
            term=term,
            fencing_token=term,
            granted_at=cluster.clock.iso(),
            lease_until=until,
            membership_epoch=cluster.epoch,
            event_id="evt-seed",
        )
    )


async def _stage_on(
    cluster: ClusterHarness,
    nid: str,
    commit_rt: CommitRuntime,
    *,
    commit_id: str,
    bank_version: int,
    parent_bank_version: int,
    term: int,
    staged: dict[str, str],
    event_id: str,
    committed_by: str,
    notes_consumed: list[str] | None = None,
) -> BankCommit:
    """Stage les octets sur le storage souverain de ``nid`` (simulant l'arrivée
    via P5-7) et retourne le ``BankCommit``."""
    return await commit_rt.stage_commit(
        commit_id=commit_id,
        proposed_bank=list(staged.items()),
        bank_version=bank_version,
        parent_bank_version=parent_bank_version,
        term=term,
        membership_epoch=cluster.epoch,
        committed_by_node_id=committed_by,
        event_id=event_id,
        notes_consumed=list(notes_consumed or []),
    )


# =============================================================================
# H1 — full-mesh apply, contiguïté + parenté
# =============================================================================


async def test_full_mesh_apply_contiguity_and_parentage() -> None:
    """H1 — le holder committe V=0 puis V=1 ; chaque pair applique les deux (il
    reconstruit son CommitIntent depuis le BankCommit + son propre état). Après
    chaque pas, ``assert_invariants`` passe : contiguïté [0,1], parent==bv-1,
    pas de commit stale, même commit_id à la même version partout."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"], space_id="alpha", epoch=1, term=2
    )
    holder = "nodeA"
    rts = {nid: _commit_rt(cluster, nid) for nid in cluster.node_ids()}

    for nid in cluster.node_ids():
        await _seed_held_token(cluster, nid, term=2, holder=holder)
        # le cluster ne sème pas le pointeur : on pose l'état initial -1 (aucun
        # commit) requis par le gate (fail-closed si bank_version.json absent).
        await cluster.nodes[nid].store.set_bank_version_pointer(
            BankVersionPointer(bank_version=-1)
        )

    for bank_version in (0, 1):
        parent = bank_version - 1
        commit_id = f"commit-{bank_version}"
        event_id = f"evt-{bank_version}"
        for nid in cluster.node_ids():
            rt = rts[nid]
            commit = await _stage_on(
                cluster, nid, rt,
                commit_id=commit_id, bank_version=bank_version,
                parent_bank_version=parent, term=2,
                staged={"activeContext.md": f"v{bank_version}"},
                event_id=event_id, committed_by=holder,
                notes_consumed=[f"note-{bank_version}"],
            )
            # Chaque nœud reconstruit son intent depuis le BankCommit + son state.
            intent = build_commit_intent(
                commit, holder_node_id=holder, fencing_token=2
            )
            await rt.apply_commit(
                commit, intent, local_node_id=holder, fencing_token=2
            )
            # Ré-armer un token HELD pour le pas suivant : le holder release après
            # chaque apply ; on le ré-acquiert (term inchangé) pour V=1.
            await _seed_held_token(cluster, nid, term=2, holder=holder)
            await assert_invariants(cluster)

    # État final : chaque nœud à bank_version 1, journal [0,1], commit_id partagé.
    for nid in cluster.node_ids():
        store = cluster.nodes[nid].store
        assert (await store.get_bank_version_pointer()).bank_version == 1
        versions = [c.bank_version for c in await store.list_commits()]
        assert versions == [0, 1]
    await assert_commits_consistent(cluster)


# =============================================================================
# H2 — trou non-contigu attrapé par l'invariant
# =============================================================================


async def test_non_contiguous_hole_is_caught() -> None:
    """H2 — on force un pair à matérialiser V=0 puis un V=2 hand-built (saute 1)
    DIRECTEMENT dans son store (bypass du gate) ; ``assert_bank_version_monotone``
    DOIT lever en nommant le trou — preuve que l'invariant folde bien le chemin
    d'apply réel."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB"], space_id="alpha", epoch=1, term=2
    )
    holder = "nodeA"
    rt = _commit_rt(cluster, holder)
    await _seed_held_token(cluster, holder, term=2, holder=holder)
    await cluster.nodes[holder].store.set_bank_version_pointer(
        BankVersionPointer(bank_version=-1)
    )

    # V=0 par le chemin réel.
    c0 = await _stage_on(
        cluster, holder, rt, commit_id="commit-0", bank_version=0,
        parent_bank_version=-1, term=2, staged={"a.md": "A"}, event_id="evt-0",
        committed_by=holder,
    )
    await rt.apply_commit(
        c0, build_commit_intent(c0, holder_node_id=holder, fencing_token=2),
        local_node_id=holder, fencing_token=2,
    )

    # V=2 hand-injecté DANS commits/ (saute V=1) — bypass total du gate.
    store = cluster.nodes[holder].store
    c2 = BankCommit(
        bank_version=2, parent_bank_version=1, term=2, commit_id="commit-2",
        committed_by_node_id=holder, manifest=build_manifest([("a.md", "A2")]),
    )
    await store.append_commit(c2)

    with pytest.raises(AssertionError) as e:
        await assert_bank_version_monotone(cluster)
    assert "CONTIGU" in str(e.value).upper() or "trou" in str(e.value).lower()


# =============================================================================
# H3 — commit stale-term : ne peut pas se matérialiser via le gate ;
#       mais si hand-injecté, l'invariant le révèle
# =============================================================================


async def test_stale_term_commit_cannot_materialize_via_gate() -> None:
    """H3a — un pair au term T+1 reçoit un commit auto-autorisé à T ;
    ``apply_commit`` lève ``CommitNotAuthorized(STALE_TERM)`` et n'écrit rien ;
    ``assert_no_stale_term_commit`` passe (aucun commit stale dans commits/)."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB"], space_id="alpha", epoch=1, term=2
    )
    peer = "nodeB"
    rt = _commit_rt(cluster, peer)
    # le pair a avancé son term à 3 et tient un token HELD au term 3.
    await cluster.nodes[peer].store.bump_term(3, updated_by_node_id=peer)
    await _seed_held_token(cluster, peer, term=3, holder=peer)
    await cluster.nodes[peer].store.set_bank_version_pointer(
        BankVersionPointer(bank_version=-1)
    )

    # commit auto-autorisé à l'ANCIEN term 2 par nodeA.
    stale_commit = await _stage_on(
        cluster, peer, rt, commit_id="commit-stale", bank_version=0,
        parent_bank_version=-1, term=2, staged={"a.md": "A"}, event_id="evt-stale",
        committed_by="nodeA",
    )
    # le pair reconstruit l'intent depuis le commit (term 2) MAIS son état vivant
    # est au term 3 -> STALE_TERM.
    intent = build_commit_intent(stale_commit, holder_node_id=peer, fencing_token=2)
    with pytest.raises(CommitNotAuthorized) as e:
        await rt.apply_commit(
            stale_commit, intent, local_node_id=peer, fencing_token=2
        )
    assert e.value.reason == CommitDenyReason.STALE_TERM
    # rien matérialisé.
    assert await cluster.nodes[peer].store.get_commit(0) is None
    await assert_no_stale_term_commit(cluster)


async def test_hand_injected_stale_term_commit_is_caught_by_invariant() -> None:
    """H3b — on injecte un commit stale-term DIRECTEMENT dans commits/ (bypass du
    gate) ; ``assert_no_stale_term_commit`` DOIT lever — preuve que l'ordre
    gate-avant-append est load-bearing (sans lui, ce commit existerait)."""
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB"], space_id="alpha", epoch=1, term=2
    )
    store = cluster.nodes["nodeB"].store
    # term courant 2 ; on injecte deux commits dont le second RÉGRESSE le term.
    c0 = BankCommit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="c0",
        committed_by_node_id="nodeA", manifest=build_manifest([("a.md", "A")]),
    )
    c1_stale = BankCommit(
        bank_version=1, parent_bank_version=0, term=1, commit_id="c1",  # term régresse
        committed_by_node_id="nodeA", manifest=build_manifest([("a.md", "B")]),
    )
    await store.append_commit(c0)
    await store.append_commit(c1_stale)

    with pytest.raises(AssertionError) as e:
        await assert_no_stale_term_commit(cluster)
    assert "stale" in str(e.value).lower() or "fencing" in str(e.value).lower()
