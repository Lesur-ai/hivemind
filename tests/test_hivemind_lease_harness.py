# -*- coding: utf-8 -*-
"""
Tests harnais P5-5 — le seam split-brain P5-2 plié, livré GREEN (issue #13).

Ces tests pilotent le VRAI ``LeaseRuntime`` (pas ``ProtocolModel.grant``) pour
le pas de grant, en réutilisant ``ProtocolModel`` UNIQUEMENT pour le plumbing
claim/ACK (réplication de la queue + agrégation des ACK chez le holder). Ils
scannent ensuite l'oracle cross-nœud ``assert_at_most_one_valid_holder`` /
``assert_invariants`` INCHANGÉ.

Pourquoi ne pas re-pointer ``test_hivemind_fault_injection.py`` : ses tests
pilotent ``ProtocolModel`` qui possède encore claim/ACK + réplication de queue
(que ``LeaseRuntime`` NE fournit PAS — c'est le seam #6/réplication, hors P5-5).
On livre donc des tests NEUFS derrière le même oracle ; les anciens restent
verts et intacts.

Déterminisme : horloge logique partagée (``cluster.clock``), un seul thread,
aucune horloge murale. Le ``LeaseRuntime`` bumpe le term DEPUIS
``store.get_term()`` (le harnais sème le term sur chaque store), pour que
l'oracle cross-store reste signifiant.
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    BankVersionPointer,
    CommitDenyReason,
    CommitIntent,
    CommitNotAuthorized,
    LeaseRuntime,
    QueueRuntime,
    TokenState,
)

from tests.hivemind_harness import (
    ClusterHarness,
    DeterministicClock,
    ProtocolModel,
    assert_at_most_one_valid_holder,
    assert_invariants,
)


async def _three_node_cluster() -> tuple[ClusterHarness, ProtocolModel]:
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"], clock=clock
    )
    return cluster, ProtocolModel(cluster, lease_seconds=300)


def _lease_for(cluster: ClusterHarness, node_id: str, *, ttl: int = 300) -> LeaseRuntime:
    """Construit un VRAI ``LeaseRuntime`` sur le store du nœud, partageant
    l'horloge logique du cluster."""
    store = cluster.nodes[node_id].store
    queue = QueueRuntime(store, cluster.space_id)
    return LeaseRuntime(
        store, cluster.space_id, queue, clock=cluster.clock.now, ttl_seconds=ttl
    )


@pytest.mark.asyncio
async def test_split_brain_single_holder_second_fenced() -> None:
    """H1 — sous claim concurrent, exactement UN holder HELD, le second fencé.

    Réalise le seam split-brain P5-2 : deux nœuds claim+all-ACK ; le VRAI
    ``LeaseRuntime`` accorde A (head, seq 0) ; B (non-head ET lease de A vivante)
    est refusé ``BLOCKED``. L'oracle cross-nœud reste vert (un seul HELD).
    RED-sans-G2 (head) / G3 (exclusion mutuelle)."""
    cluster, model = await _three_node_cluster()
    m = await cluster.membership()

    # A (seq 0) et B (seq 1) claim ; deliver=True réplique les deux entrées sur
    # CHAQUE store (replicate_queue_entries) -> le store de B voit l'entrée seq-0
    # de A, donc queue.head de B == A.
    await model.claim("nodeA", event_id="evt-a", sequence=0, deliver=True)
    await model.claim("nodeB", event_id="evt-b", sequence=1, deliver=True)
    await model.collect_acks("evt-a", holder="nodeA")
    await model.collect_acks("evt-b", holder="nodeB")

    lease_a = _lease_for(cluster, "nodeA")
    lease_b = _lease_for(cluster, "nodeB")

    # A acquiert (head all-ACKé) -> HELD.
    token_a = await lease_a.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )
    assert token_a.state == TokenState.HELD.value
    assert token_a.holder_node_id == "nodeA"

    # B tente : son head est l'entrée seq-0 de A (pas evt-b) -> BLOCKED.
    with pytest.raises(CommitNotAuthorized) as err:
        await lease_b.acquire(
            membership=m, holder_node_id="nodeB", event_id="evt-b"
        )
    assert err.value.reason == CommitDenyReason.BLOCKED

    # Oracle cross-nœud : un seul holder valide, le second fencé.
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)

    # Et même si B était le head, la lease vivante de A le bloquerait : on le
    # prouve en marquant l'entrée de A consommée sur le store de B (le head de B
    # devient evt-b) puis en re-tentant — toujours BLOCKED par l'exclusion
    # mutuelle (G3), pas par le head.
    from live_mem.core.hivemind import QueueEntryStatus

    store_b = cluster.nodes["nodeB"].store
    a_entry_on_b = next(
        e for e in await store_b.list_queue() if e.event_id == "evt-a"
    )
    await store_b.update_queue_entry_status(a_entry_on_b, QueueEntryStatus.GRANTED)
    # B doit aussi avoir la lease de A répliquée pour que G3 voie une lease
    # vivante : on copie le token HELD de A sur le store de B (modélise la
    # diffusion TOKEN_GRANTED que la réplication #6/#7 portera). Le term.json de
    # B doit suivre (sinon token.term > term.json viole assert_term_monotone) —
    # c'est ce que TERM_BUMPED propagerait avec le grant.
    await store_b.bump_term(token_a.term, updated_by_node_id="nodeA")
    await store_b.set_token(token_a)
    with pytest.raises(CommitNotAuthorized) as err2:
        await lease_b.acquire(
            membership=m, holder_node_id="nodeB", event_id="evt-b"
        )
    assert err2.value.reason == CommitDenyReason.BLOCKED
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)


@pytest.mark.asyncio
async def test_stale_holder_return_rejected_after_term_bump() -> None:
    """H2 — un holder revenant après bump de term + expiration est rejeté.

    A acquiert (term T, HELD). L'horloge dépasse sa lease ET le term monte à T+1
    sur tous les stores (B a pris la main). A revient et appelle
    ``assert_commit_allowed`` avec un intent au term T -> STALE_TERM (le term a
    bougé). Pins le cas stale-holder-return bout-en-bout sur l'horloge
    injectable. RED si un holder expiré/superseded est auto-ré-autorisé."""
    cluster, model = await _three_node_cluster()
    m = await cluster.membership()

    await model.claim("nodeA", event_id="evt-a", sequence=0, deliver=True)
    await model.collect_acks("evt-a", holder="nodeA")

    lease_a = _lease_for(cluster, "nodeA", ttl=300)
    token_a = await lease_a.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )
    term_t = token_a.term
    # Pose un pointeur bank_version "aucun commit" (bank_version=-1) sur le store
    # de A : l'étape 0 (BLOCKED) exige un pointeur présent, mais on n'atteint
    # jamais le CAS (étape 4) car STALE_TERM (étape 2) refuse d'abord ; un
    # pointeur à -1 est ignoré par assert_bank_version_monotone (pas de commit).
    store_a = cluster.nodes["nodeA"].store
    await store_a.set_bank_version_pointer(
        BankVersionPointer(bank_version=-1, commit_id="")
    )

    # L'horloge dépasse la lease de A ET le term monte à T+1 sur TOUS les stores
    # (B a pris la main). Le token de A reste HELD à T (pas encore réconcilié) :
    # son intent au term T passe l'étape NOT_HOLDER (il EST le holder du token
    # local) mais bute sur l'étape STALE_TERM (le term.json vivant est à T+1).
    cluster.clock.tick(seconds=301)
    for nid in cluster.node_ids():
        await cluster.nodes[nid].store.bump_term(term_t + 1, updated_by_node_id="nodeB")
    cluster.term = term_t + 1

    stale_intent = CommitIntent(
        holder_node_id="nodeA",
        term=term_t,
        fencing_token=term_t,
        bank_version=1,
        previous_bank_version=0,
        commit_id="stale-a",
    )
    with pytest.raises(CommitNotAuthorized) as err:
        await lease_a.assert_commit_allowed(stale_intent)
    # Le term a bougé -> STALE_TERM (et non FENCED : la supersession par term
    # est surfacée AVANT l'expiration par horloge, cf. §F de la spec).
    assert err.value.reason == CommitDenyReason.STALE_TERM

    # On réconcilie ENSUITE le holder stale de A pour que l'oracle cross-nœud
    # reste vert (sinon STALE-HOLDER actif sous le term max).
    await lease_a.reconcile_stale_holder()
    await assert_invariants(cluster)
