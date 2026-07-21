# -*- coding: utf-8 -*-
"""
Modèle de référence protocole V1 + property test borné (issue #11).

Deux familles de tests :

1. ``test_v1_all_ack_policy_is_default_and_blocking`` — GARDE-FOU du non-goal
   « pas de quorum en V1 ». Épingle ``AllAckPolicy`` comme défaut et échoue si
   un progrès est accepté avec ``received < expected``. Une future bascule
   quorum casserait ce test au lieu de relâcher silencieusement la sémantique.

2. Exploration aléatoire BORNÉE et SEEDÉE des entrelacements (deliver / drop /
   duplicate / reorder / crash) avec scan d'invariants après CHAQUE pas. La
   passe par défaut est minuscule et rapide (CI) ; l'exploration étendue est
   opt-in via la variable d'environnement ``HIVEMIND_PROPERTY_RUNS`` ou le
   marqueur ``-m slow``, exclue du ``uv run pytest tests/ -v`` nominal.

Déterminisme : seeds fixes, horloge logique, livraison pull-based, un thread.
"""

from __future__ import annotations

import os
import random

import pytest

from live_mem.core.hivemind import (
    MembershipView,
    PeerChannelError,
    QueueEntryStatus,
)

from tests.hivemind_harness import (
    AllAckPolicy,
    ClusterHarness,
    DeterministicClock,
    ProtocolModel,
    assert_invariants,
)


# =============================================================================
# Garde-fou V1 all-ACK (non-goal : pas de quorum)
# =============================================================================


@pytest.mark.asyncio
async def test_v1_all_ack_policy_is_default_and_blocking() -> None:
    """
    AllAckPolicy est le défaut et BLOQUE tant qu'un membre actif n'a pas ACKé.

    La satisfaction est PAR IDENTITÉ : l'ensemble des ACKers reçus doit être un
    sur-ensemble de l'ensemble des membres actifs. On vérifie :
    - tout sous-ensemble strict des actifs  -> is_satisfied False, grant refusé ;
    - l'ensemble exact des actifs           -> is_satisfied True,  grant autorisé.
    """
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"], clock=clock
    )
    model = ProtocolModel(cluster)

    membership = await cluster.membership()
    policy = AllAckPolicy()
    expected_set = policy.expected_ack_set(membership)
    assert expected_set == {"nodeA", "nodeB", "nodeC"}, "attendu = membres actifs"
    assert policy.expected_acks(membership) == 3

    # Le défaut du cluster EST AllAckPolicy.
    assert isinstance(cluster.ack_policy, AllAckPolicy)

    # Tout sous-ensemble STRICT des actifs -> jamais satisfait.
    for missing in ("nodeA", "nodeB", "nodeC"):
        partial = expected_set - {missing}
        assert policy.is_satisfied(received=partial, membership=membership) is False
    assert policy.is_satisfied(received=set(), membership=membership) is False

    # L'ensemble exact (et un sur-ensemble) -> satisfait.
    assert policy.is_satisfied(received=expected_set, membership=membership) is True
    assert (
        policy.is_satisfied(received=expected_set | {"intrus"}, membership=membership)
        is True
    )

    # Bout-en-bout : avec un ACK manquant, le grant est refusé (pas de progrès
    # silencieux). C'est le cœur du garde-fou : si un jour le code accepte un
    # grant alors qu'un actif n'a pas ACKé, ce bloc échoue.
    await model.claim("nodeA", event_id="evt-guard")
    await model.collect_acks("evt-guard", holder="nodeA", ackers=["nodeA", "nodeB"])
    assert await model.can_grant("evt-guard", holder="nodeA") is False
    with pytest.raises(RuntimeError, match="all-ACK non satisfait"):
        await model.grant("nodeA", event_id="evt-guard")

    # Le dernier ACK (le membre actif manquant) débloque exactement le all-ACK.
    await model.ack("nodeC", event_id="evt-guard", to_holder="nodeA")
    assert await model.can_grant("evt-guard", holder="nodeA") is True
    await model.grant("nodeA", event_id="evt-guard")
    await assert_invariants(cluster)


@pytest.mark.asyncio
async def test_v1_all_ack_is_identity_not_count() -> None:
    """
    RÉGRESSION (Codex P2 #1) : all-ACK doit valider par IDENTITÉ, pas par
    comptage. Un ACK d'un nœud non-actif/évincé ne peut PAS se substituer à un
    membre actif manquant, même si le COMPTE des ACKers atteint ``expected``.
    """
    from live_mem.core.hivemind import Ack, MemberStatus

    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"], clock=clock
    )
    model = ProtocolModel(cluster)
    policy = AllAckPolicy()
    membership = await cluster.membership()

    # 3 ACKers en COMPTE, mais nodeC actif manquant et un "ghost" non-membre :
    # le comptage naïf (3 >= 3) passerait, l'identité doit refuser.
    decoy = {"nodeA", "nodeB", "ghost-evicted"}
    assert len(decoy) == policy.expected_acks(membership)  # même cardinalité
    assert policy.is_satisfied(received=decoy, membership=membership) is False

    # Bout-en-bout sur l'état réel : A et B ACKent, puis un ACK d'un nœud
    # FANTÔME (jamais dans la membership active) est persisté sur le holder.
    # Le compte atteint 3 mais nodeC (actif) n'a pas ACKé -> grant refusé.
    await model.claim("nodeA", event_id="evt-ghost")
    await model.collect_acks("evt-ghost", holder="nodeA", ackers=["nodeA", "nodeB"])
    await cluster.nodes["nodeA"].store.record_ack(
        Ack(
            event_id="evt-ghost",
            ack_by_node_id="ghost-evicted",
            term=cluster.term,
            membership_epoch=cluster.epoch,
        )
    )
    received = {
        a.ack_by_node_id
        for a in await cluster.nodes["nodeA"].store.list_acks("evt-ghost")
    }
    assert received == {"nodeA", "nodeB", "ghost-evicted"}
    assert len(received) == policy.expected_acks(membership)  # compte == expected
    assert await model.can_grant("evt-ghost", holder="nodeA") is False  # identité
    with pytest.raises(RuntimeError, match="all-ACK non satisfait"):
        await model.grant("nodeA", event_id="evt-ghost")

    # Le vrai actif manquant (nodeC) ACK -> all-ACK satisfait par identité.
    await model.ack("nodeC", event_id="evt-ghost", to_holder="nodeA")
    assert await model.can_grant("evt-ghost", holder="nodeA") is True
    assert MemberStatus.EVICTED.value == "evicted"  # ancrage de l'intention
    await assert_invariants(cluster)


@pytest.mark.asyncio
async def test_eviction_relaxes_expected_but_never_to_quorum() -> None:
    """
    L'éviction RÉTRÉCIT l'ensemble all-ACK (expected baisse), mais reste
    all-ACK sur l'ensemble actif : ce n'est PAS un quorum. Un membre actif
    manquant bloque toujours.
    """
    from live_mem.core.hivemind import MemberStatus

    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC", "nodeD"], clock=clock
    )
    policy = AllAckPolicy()

    membership = await cluster.membership()
    assert policy.expected_acks(membership) == 4

    # Éviction de D -> expected = 3 (actifs), pas un quorum de 4.
    evicted = MembershipView(
        epoch=membership.epoch + 1,
        members=[
            m.model_copy(update={"status": MemberStatus.EVICTED.value})
            if m.node_id == "nodeD"
            else m
            for m in membership.members
        ],
    )
    assert policy.expected_acks(evicted) == 3
    assert policy.expected_ack_set(evicted) == {"nodeA", "nodeB", "nodeC"}
    # 2 actifs sur 3 -> toujours bloqué (all-ACK par identité, pas quorum 2/3).
    assert policy.is_satisfied(received={"nodeA", "nodeB"}, membership=evicted) is False
    # Même un ACK de l'évincé D ne comble pas l'absence de nodeC actif.
    assert (
        policy.is_satisfied(received={"nodeA", "nodeB", "nodeD"}, membership=evicted)
        is False
    )
    assert (
        policy.is_satisfied(received={"nodeA", "nodeB", "nodeC"}, membership=evicted)
        is True
    )


# =============================================================================
# Property test borné : entrelacements aléatoires seedés
# =============================================================================


def _property_runs() -> int:
    """
    Nombre de runs de property : minuscule par défaut (CI rapide), étendu via
    l'env ``HIVEMIND_PROPERTY_RUNS``. Borné dur pour éviter une CI lente.
    """
    raw = os.environ.get("HIVEMIND_PROPERTY_RUNS")
    if raw is None:
        return 8  # défaut minuscule, toujours-on
    try:
        return max(1, min(int(raw), 2000))
    except ValueError:
        return 8


async def _run_one_property_episode(seed: int, *, steps: int) -> None:
    """
    Un épisode : construit un cluster 3 nœuds, applique une séquence
    déterministe (seedée) d'actions de protocole entrelacées avec des fautes
    réseau, et scanne les invariants après CHAQUE pas.

    Toute violation lève une AssertionError nommant le fautif (via le harnais).
    Les rejets attendus (PeerChannelError, RuntimeError de garde) sont avalés :
    ils sont le comportement CORRECT, pas un échec.
    """
    rng = random.Random(seed)
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"], clock=clock
    )
    model = ProtocolModel(cluster)
    await assert_invariants(cluster)

    node_ids = cluster.node_ids()
    counter = 0

    for _ in range(steps):
        action = rng.choice(
            ["claim", "deliver", "drop", "duplicate", "reorder", "ack_and_grant", "tick"]
        )
        try:
            if action == "claim":
                origin = rng.choice(node_ids)
                counter += 1
                await model.claim(
                    origin,
                    event_id=f"evt-{seed}-{counter}",
                    sequence=counter,
                    deliver=False,
                )

            elif action == "deliver":
                target = rng.choice(node_ids)
                if cluster.transport.pending(target):
                    await cluster.transport.deliver_next(target)
                    # ``deliver_next`` ne persiste QUE l'event (channel.receive) ;
                    # la dérivation des entrées de queue (rôle du futur #6) est
                    # portée par le modèle. Sans ce pas, un claim distant livré
                    # n'entre JAMAIS dans la queue du receiver, et la garde
                    # head-of-queue de ``ack_and_grant`` raisonnerait sur une
                    # queue locale incomplète : l'oracle distribué d'ordre serait
                    # contourné (HIVEMIND.md §5.3 : tous les pairs dérivent le
                    # MÊME ordre des MÊMES events).
                    await model.replicate_queue_entries()

            elif action == "drop":
                target = rng.choice(node_ids)
                if cluster.transport.pending(target):
                    cluster.transport.drop(target, index=0)

            elif action == "duplicate":
                target = rng.choice(node_ids)
                if cluster.transport.pending(target):
                    cluster.transport.duplicate(target, index=0)

            elif action == "reorder":
                target = rng.choice(node_ids)
                n = len(cluster.transport.pending(target))
                if n > 1:
                    order = list(range(n))
                    rng.shuffle(order)
                    cluster.transport.reorder(target, order)

            elif action == "ack_and_grant":
                # Tente un grant sous all-ACK pour une entrée de la queue. On
                # dérive d'abord la queue COMPLÈTE sur tous les nœuds (les claims
                # déjà livrés mais pas encore répliqués entrent ici) pour que la
                # garde head-of-queue raisonne sur l'ordre distribué réel.
                await model.replicate_queue_entries()
                holder = rng.choice(node_ids)
                queue = await cluster.nodes[holder].store.list_queue()
                pending = [
                    e for e in queue if e.status == QueueEntryStatus.PENDING.value
                ]
                if pending:
                    # ACK de TOUTES les entrées PENDING vues par le holder (pas
                    # seulement la cible) : ainsi, dès qu'au moins deux entrées
                    # atteignent all-ACK, viser une entrée NON-head fait jouer la
                    # garde head-of-queue de façon DÉTERMINISTE sous
                    # entrelacements aléatoires (sinon le reject n'est jamais
                    # atteint car seul le head, livré le plus tôt, est
                    # all-ACKé). Une entrée pas encore livrée partout reste non
                    # all-ACKée (collect_acks n'invente pas d'ACK).
                    for entry in pending:
                        await model.collect_acks(entry.event_id, holder=holder)
                    # On vise une entrée PENDING ARBITRAIRE (pas forcément le
                    # head) : si la cible n'est pas le head distribué, le grant
                    # DOIT être rejeté par la garde head-of-queue. Si un autre
                    # nœud tient déjà une lease active non expirée, le grant DOIT
                    # être rejeté par la garde d'exclusion mutuelle. Les deux
                    # rejets sont des RuntimeError AVALÉS par le handler (rejet
                    # ATTENDU), invariants scannés ensuite. Plus de workaround
                    # ``held_elsewhere`` ici : c'est le MODÈLE qui enforce
                    # l'exclusion mutuelle (cf. ``grant``), pas l'orchestration
                    # du test. L'épisode laisse ainsi un release/expiration faire
                    # progresser le grant suivant à un pas ultérieur.
                    target = rng.choice(pending)
                    if await model.can_grant(target.event_id, holder=holder):
                        await model.grant(holder, event_id=target.event_id)

            elif action == "tick":
                clock.tick(seconds=rng.choice([1, 30, 600]))

        except (PeerChannelError, RuntimeError, AssertionError) as exc:
            # AssertionError du HARNAIS = vrai bug ; AssertionError du transport
            # (deliver/drop sur file vide) = bug de scénario, pas de protocole.
            # On distingue par le message du harnais d'invariants.
            msg = str(exc)
            if isinstance(exc, AssertionError) and (
                "Invariant" in msg or "SPLIT-BRAIN" in msg
            ):
                raise  # violation d'invariant -> remonter
            # sinon : rejet attendu / garde-fou / file vide -> comportement OK.

        # Scan d'invariants après CHAQUE pas (le cœur du gate).
        await assert_invariants(cluster)


@pytest.mark.asyncio
async def test_bounded_random_interleavings_preserve_invariants() -> None:
    """Passe par défaut : minuscule, toujours-on, rapide pour la CI."""
    runs = _property_runs()
    for seed in range(runs):
        await _run_one_property_episode(seed, steps=12)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_bounded_random_interleavings_extended_optin() -> None:
    """
    Exploration étendue, OPT-IN uniquement (``-m slow``). Exclue du run CI
    nominal pour garder ``uv run pytest tests/ -v`` rapide.
    """
    if not os.environ.get("HIVEMIND_PROPERTY_SLOW"):
        pytest.skip(
            "exploration étendue opt-in: poser HIVEMIND_PROPERTY_SLOW=1 et -m slow"
        )
    for seed in range(200):
        await _run_one_property_episode(seed, steps=40)
