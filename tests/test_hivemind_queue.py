# -*- coding: utf-8 -*-
"""
Tests pour issue #6 / P5-3 — runtime de la queue durable distribuée all-ACK.

Couvre le contrat ADR-0009 (ordre total déterministe partagé par #6 et #7) :

- ordre total = ``(sequence, membership_epoch, requester_node_id, event_id)`` ;
- HEAD = min sous cet ordre, PENDING ET demandeur ACTIVE ;
- deux peers calculent un head IDENTIQUE depuis le même snapshot (cross-node) ;
- une collision de seq est ordonnée déterministiquement ET surfacée comme
  anomalie, jamais coalescée ;
- une entrée corrompue BLOQUE la sélection de head (fail-closed) ;
- all-ACK = IDENTITÉ d'ensemble sur les membres ACTIVE, PAS un compte ;
- idempotence event_id (ré-submit / ré-ACK) ;
- isolation : ``queue_runtime`` n'importe ni le consolidateur ni le graph.
"""

from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from live_mem.core.hivemind import (
    Ack,
    CorruptedStateError,
    DuplicateEventId,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    QueueEntry,
    QueueEntryStatus,
    QueueReplayConflictError,
    QueueRuntime,
    SeqCollision,
    layout,
    queue_order_key,
    select_head,
)
from live_mem.core.hivemind import queue_runtime as queue_runtime_module
from tests.test_hivemind_state import FakeStorage


# =============================================================================
# Helpers partagés
# =============================================================================


def make_store(storage: FakeStorage, space_id: str = "alpha") -> HivemindStateStore:
    return HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]


def membership(epoch: int, *active_ids: str, evicted: tuple[str, ...] = ()) -> MembershipView:
    """MembershipView avec des membres ACTIVE et (optionnellement) EVICTED.

    Un node évincé reste DANS ``members`` (record d'audit conservé) avec
    ``status == EVICTED`` — ce qui permet de tester que la sélection de head
    et l'all-ACK sourcent l'ensemble ACTIVE via ``active_members`` /
    ``expected_ack_node_ids`` et non la liste brute des membres.
    """
    members = [
        Member(node_id=nid, status=MemberStatus.ACTIVE) for nid in active_ids
    ]
    members += [
        Member(node_id=nid, status=MemberStatus.EVICTED) for nid in evicted
    ]
    return MembershipView(epoch=epoch, members=members)


def entry(
    seq: int,
    event_id: str,
    requester: str,
    *,
    epoch: int = 1,
    status: QueueEntryStatus = QueueEntryStatus.PENDING,
) -> QueueEntry:
    return QueueEntry(
        event_id=event_id,
        sequence=seq,
        requester_node_id=requester,
        membership_epoch=epoch,
        status=status,
    )


async def seed_queue(store: HivemindStateStore, entries: list[QueueEntry]) -> None:
    for e in entries:
        await store.enqueue(e)


# =============================================================================
# T1 — cross-node : deux peers calculent le MÊME head (ADR test a)
# =============================================================================


async def test_two_peers_compute_identical_head() -> None:
    """Deux stores indépendants, mêmes 3 entrées enqueued dans un ORDRE
    d'insertion différent, même membership : ``head`` retourne le même
    event_id. Pin l'order-independence cross-node de ``select_head``."""
    m = membership(1, "nodeA", "nodeB", "nodeC")
    entries = [
        entry(0, "evtA", "nodeA"),
        entry(1, "evtB", "nodeB"),
        entry(2, "evtC", "nodeC"),
    ]

    storage_a = FakeStorage()
    store_a = make_store(storage_a)
    await seed_queue(store_a, [entries[2], entries[0], entries[1]])
    rt_a = QueueRuntime(store_a, "alpha")

    storage_b = FakeStorage()
    store_b = make_store(storage_b)
    await seed_queue(store_b, [entries[1], entries[2], entries[0]])
    rt_b = QueueRuntime(store_b, "alpha")

    head_a = await rt_a.head(m)
    head_b = await rt_b.head(m)

    assert head_a is not None and head_b is not None
    assert head_a.event_id == head_b.event_id == "evtA"


# =============================================================================
# T2 — head = min sous le 4-tuple complet, pas (seq, event_id) (divergence)
# =============================================================================


async def test_head_is_min_under_full_4tuple() -> None:
    """Entrées telles que l'ordre ``(sequence, event_id)`` du harness et le
    4-tuple complet ADR-0009 désignent des heads DIFFÉRENTS. Même seq ; le
    candidat à plus bas ``membership_epoch`` porte un ``event_id`` lexicalement
    plus grand. Le 4-tuple gagne via membership_epoch ; le (seq, event_id) du
    harness gagnerait via event_id."""
    storage = FakeStorage()
    store = make_store(storage)
    # même seq=5 ; e_low a un epoch plus bas mais un event_id lexicalement
    # plus grand que e_high.
    e_low = entry(5, "evtZ", "nodeA", epoch=1)
    e_high = entry(5, "evtA", "nodeB", epoch=2)
    await seed_queue(store, [e_low, e_high])
    rt = QueueRuntime(store, "alpha")
    m = membership(2, "nodeA", "nodeB")

    head = await rt.head(m)
    assert head is not None

    # 4-tuple ADR : membership_epoch (1 < 2) départage AVANT requester/event_id.
    full_min = min([e_low, e_high], key=queue_order_key)
    assert head.event_id == full_min.event_id == "evtZ"

    # Ordre (seq, event_id) du harness : event_id départage -> evtA.
    harness_min = min(
        [e_low, e_high], key=lambda e: (e.sequence, e.event_id)
    )
    assert harness_min.event_id == "evtA"
    assert head.event_id != harness_min.event_id


# =============================================================================
# T3 — collision de seq : un head + une anomalie, jamais coalescée (ADR test b)
# =============================================================================


async def test_seq_collision_one_head_and_anomaly() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    e1 = entry(5, "evtB", "nodeB")
    e2 = entry(5, "evtA", "nodeA")
    await seed_queue(store, [e1, e2])
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA", "nodeB")

    # (i) un seul head, déterministe : min sur (requester_node_id, event_id)
    # à seq/epoch égaux -> nodeA/evtA.
    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evtA"
    assert head.requester_node_id == "nodeA"

    # (ii) l'anomalie est surfacée avec les DEUX event_ids et requesters, triés.
    anomalies = await rt.queue_anomalies()
    assert anomalies == [
        SeqCollision(
            sequence=5,
            event_ids=("evtA", "evtB"),
            requester_node_ids=("nodeA", "nodeB"),
        )
    ]

    # (iii) head stable sur appel répété (pas de non-déterminisme / coalesce).
    head_again = await rt.head(m)
    assert head_again is not None
    assert head_again.event_id == head.event_id

    # Les deux entrées restent durablement présentes (jamais coalescées).
    assert len(await store.list_queue()) == 2


# =============================================================================
# T4 — même event_id deux fois n'est PAS une collision (dedup != split-brain)
# =============================================================================


async def test_same_event_id_twice_is_not_a_collision() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    # Même seq, même event_id : ré-écriture idempotente (même clé store).
    await store.enqueue(entry(7, "evtSAME", "nodeA"))
    await store.enqueue(entry(7, "evtSAME", "nodeA"))
    rt = QueueRuntime(store, "alpha")

    assert len(await store.list_queue()) == 1
    assert await rt.queue_anomalies() == []


# =============================================================================
# T5 — head PENDING uniquement (granted/cancelled skippés)
# =============================================================================


async def test_pending_only_head() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    # Entrée d'ordre inférieur mais GRANTED : doit être sautée.
    await store.enqueue(
        entry(0, "evtGranted", "nodeA", status=QueueEntryStatus.GRANTED)
    )
    await store.enqueue(entry(1, "evtPending", "nodeA"))
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evtPending"
    assert head.status == QueueEntryStatus.PENDING.value


# =============================================================================
# T6 — le demandeur doit être ACTIVE (source = expected_ack_node_ids)
# =============================================================================


async def test_requester_must_be_active() -> None:
    """L'entrée PENDING d'ordre minimal a un demandeur EVICTED ; le head est la
    prochaine PENDING d'un demandeur ACTIVE. Pin la source de l'ensemble ACTIVE
    sur ``expected_ack_node_ids`` (membres ACTIVE), pas la liste brute."""
    storage = FakeStorage()
    store = make_store(storage)
    await store.enqueue(entry(0, "evtEvicted", "nodeEvil"))
    await store.enqueue(entry(1, "evtActive", "nodeA"))
    rt = QueueRuntime(store, "alpha")
    # nodeEvil est dans members mais EVICTED -> hors ensemble ACTIVE.
    m = membership(1, "nodeA", evicted=("nodeEvil",))

    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evtActive"
    assert head.requester_node_id == "nodeA"


# =============================================================================
# T7 — entrée corrompue BLOQUE la sélection de head (ADR test d, fail-closed)
# =============================================================================


async def test_corrupt_queue_entry_blocks_head() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    await store.enqueue(entry(1, "evtOK", "nodeA"))
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    # Injecte un objet corrompu (JSON cassé) dans le préfixe queue/.
    storage.objects[
        "alpha/_hivemind/queue/00000000000000000005_evtX.json"
    ] = "{not json"

    with pytest.raises(CorruptedStateError):
        await rt.head(m)

    # queue_anomalies passe par le même list_queue -> propage aussi.
    with pytest.raises(CorruptedStateError):
        await rt.queue_anomalies()


# =============================================================================
# T8 — all-ACK identité : bloque tant qu'un membre ACTIVE manque
# =============================================================================


async def test_all_ack_identity_blocks_on_missing_active() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    await store.enqueue(entry(0, "evtE", "nodeA"))
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeB"))
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA", "nodeB", "nodeC")  # C n'a pas ACK

    # C est ACTIVE mais absent des ACKs -> identité d'ensemble non satisfaite.
    assert await rt.is_fully_acked("evtE", m) is False

    # L'op reste visiblement bloquée : head pointe toujours sur evtE (PENDING).
    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evtE"


# =============================================================================
# T9 — all-ACK identité, PAS compte : un ACKeur évincé ne se substitue pas
# =============================================================================


async def test_all_ack_evicted_acker_does_not_substitute() -> None:
    """Membership {A,B,C} ACTIVE, C manquant. ACKs de A, B ET d'un X
    évincé/inconnu : le COMPTE atteint 3 = len(expected), mais l'IDENTITÉ
    d'ensemble échoue (X ∉ expected, C absent). Pin set-identity sur count."""
    storage = FakeStorage()
    store = make_store(storage)
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeB"))
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeX"))
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA", "nodeB", "nodeC")

    # Le compte vaut 3 == len(expected) -> un test count-based passerait True.
    assert await store.count_acks("evtE") == 3
    # Mais l'identité d'ensemble échoue : nodeC manque, nodeX ne le remplace pas.
    assert await rt.is_fully_acked("evtE", m) is False


# =============================================================================
# T10 — ré-submit / ré-ACK idempotents (idempotence event_id)
# =============================================================================


async def test_re_ack_and_re_submit_are_idempotent() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
        sequence=3,
    )
    await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
        sequence=3,
    )
    assert len(await store.list_queue()) == 1

    await rt.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    await rt.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    assert len(await store.list_acks("evtE")) == 1


async def test_re_submit_same_event_id_default_seq_no_duplicate() -> None:
    """Ré-submit du MÊME ``event_id`` avec ``sequence`` OMIS (défaut caller) :
    exactement UN objet de queue, et le second ``submit`` RETOURNE l'entrée
    existante (pas un doublon à ``max(seq)+1``).

    RED-without : sans le garde d'idempotence par ``event_id``, le 2ᵉ
    ``submit(sequence=None)`` ré-alloue ``allocate_sequence() == max+1`` et écrit
    un SECOND objet store ``{seq}_{event_id}`` -> ``list_queue()`` vaut 2.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    first = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )
    second = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )

    entries = await store.list_queue()
    assert len(entries) == 1
    # Le second submit retourne l'entrée DÉJÀ persistée (même seq que la 1ʳᵉ).
    assert second.event_id == first.event_id
    assert second.sequence == first.sequence


async def test_re_submit_does_not_overwrite_existing_status() -> None:
    """Un ré-submit ne ressuscite PAS une entrée déjà accordée : il retourne
    l'entrée existante (ici ``GRANTED``) sans réécrire un objet ``PENDING`` —
    sinon un rejeu du même ``event_id`` rejouerait un claim de token déjà
    consommé.

    RED-without : sans le garde, le ré-submit écrit un nouvel objet
    ``PENDING`` à ``max(seq)+1`` ; ``select_head`` le verrait éligible et le
    head régresserait sur un claim déjà accordé.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    first = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )
    await rt.mark_granted(first)

    resubmitted = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )

    assert resubmitted.status == QueueEntryStatus.GRANTED.value
    assert len(await store.list_queue()) == 1
    # Aucune entrée PENDING ressuscitée -> pas de head éligible.
    assert await rt.head(m) is None


async def test_re_submit_same_event_id_same_identity_is_idempotent() -> None:
    """Ré-submit du MÊME ``event_id`` avec une identité logique IDENTIQUE
    (mêmes ``requester_node_id`` / ``term`` / ``membership_epoch`` /
    ``bank_version``) -> EXACTEMENT UNE entrée, le second retourne la première.

    C'est la branche idempotente du garde de replay-conflict : un rejeu fidèle
    n'est PAS un conflit. (Garde la sémantique idempotente verte une fois la
    comparaison d'identité ajoutée.)
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    first = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=2,
        membership_epoch=5,
        bank_version=7,
    )
    second = await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=2,
        membership_epoch=5,
        bank_version=7,
    )

    entries = await store.list_queue()
    assert len(entries) == 1
    assert second.event_id == first.event_id
    assert second.sequence == first.sequence
    assert second.requester_node_id == first.requester_node_id


async def test_re_submit_same_event_id_divergent_requester_raises() -> None:
    """Ré-submit du MÊME ``event_id`` avec un ``requester_node_id`` DIVERGENT
    -> ``QueueReplayConflictError``, et AUCUNE seconde écriture durable.

    Un ``event_id`` identifie UN seul événement logique : le rejouer avec un
    autre demandeur est une ERREUR PROTOCOLE (même sémantique que le
    ``REPLAY_CONFLICT`` de ``peer.py``), jamais un succès silencieux qui
    retourne l'entrée d'un autre node.

    RED-without : sans la comparaison d'identité dans le garde de duplicata,
    ``submit`` retourne silencieusement l'entrée de ``nodeA`` au lieu de lever
    -> ``pytest.raises`` échoue (DID NOT RAISE).
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )

    with pytest.raises(QueueReplayConflictError):
        await rt.submit(
            event_id="evtE",
            requester_node_id="nodeB",  # DIVERGENT
            term=1,
            membership_epoch=1,
        )

    # Fail-closed : la requête divergente n'a écrit AUCUN objet durable.
    entries = await store.list_queue()
    assert len(entries) == 1
    assert entries[0].requester_node_id == "nodeA"


async def test_re_submit_same_event_id_divergent_term_raises() -> None:
    """Ré-submit du MÊME ``event_id`` avec un ``term`` DIVERGENT (même
    demandeur) -> ``QueueReplayConflictError``, AUCUNE seconde écriture.

    Couvre un champ d'identité autre que ``requester_node_id`` pour prouver que
    la comparaison porte sur le tuple complet ``(requester_node_id, term,
    membership_epoch, bank_version)``, pas seulement le demandeur.

    RED-without : sans la comparaison d'identité, le second ``submit`` retourne
    l'entrée existante (term=1) au lieu de lever -> ``pytest.raises`` échoue.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    await rt.submit(
        event_id="evtE",
        requester_node_id="nodeA",
        term=1,
        membership_epoch=1,
    )

    with pytest.raises(QueueReplayConflictError):
        await rt.submit(
            event_id="evtE",
            requester_node_id="nodeA",
            term=9,  # DIVERGENT
            membership_epoch=1,
        )

    entries = await store.list_queue()
    assert len(entries) == 1
    assert entries[0].term == 1


async def test_submit_scans_all_same_event_id_returns_canonical_earliest() -> None:
    """DEUX objets durables préexistants du MÊME ``event_id`` à des seq
    DIFFÉRENTS mais d'identité logique IDENTIQUE -> ``submit`` les voit TOUS,
    n'écrit AUCUN troisième objet, et RETOURNE l'entrée CANONIQUE (seq le plus
    bas sous ``queue_order_key``).

    La clé store étant ``{sequence}_{event_id}``, une course distribuée passée
    (pas de CAS S3) peut déjà avoir écrit le même ``event_id`` à deux seq. On
    les seede DIRECTEMENT via ``store.enqueue`` (le garde d'idempotence de
    ``submit`` empêcherait sinon le second objet).

    NB : ce cas (deux duplicatas FIDÈLES) ne distingue pas à lui seul l'ancien
    du nouveau garde sur la VALEUR retournée — ``list_queue`` étant trié par seq
    ascendant, l'ancien « return premier match » et le nouveau ``min`` rendent
    tous deux seq 3. C'est un GARDE DE CONTRAT (roll-forward idempotent : entrée
    canonique déterministe + aucun 3ᵉ objet écrit), pas une preuve RED-without —
    la preuve de scan-complet vit dans les deux tests de divergence ci-dessous,
    où l'ancien garde s'arrête AVANT l'entrée divergente et ne lève jamais.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    # Deux objets durables, même event_id, même identité, seq 7 PUIS 3.
    await store.enqueue(entry(7, "evtDUP", "nodeA"))
    await store.enqueue(entry(3, "evtDUP", "nodeA"))
    assert len(await store.list_queue()) == 2

    result = await rt.submit(
        event_id="evtDUP",
        requester_node_id="nodeA",
        term=0,
        membership_epoch=1,
        bank_version=-1,
    )

    # Aucun 3ᵉ objet écrit ; entrée canonique = seq le plus bas (3).
    assert len(await store.list_queue()) == 2
    assert result.event_id == "evtDUP"
    assert result.sequence == 3


async def test_submit_detects_later_divergent_same_event_id_duplicate() -> None:
    """Le PIÈGE exact du finding Codex : une PREMIÈRE entrée à seq bas dont
    l'identité est FIDÈLE à l'incoming, et une SECONDE entrée à seq plus haut
    dont l'identité DIVERGE. ``submit`` doit scanner TOUTES les entrées et
    LEVER ``QueueReplayConflictError`` — pas s'arrêter au 1ᵉ match fidèle et
    retourner un faux succès.

    RED-without : l'ancien garde itère, voit d'abord seq 3 (identité ==
    incoming), retourne immédiatement et n'inspecte JAMAIS seq 7 (divergent).
    -> ``pytest.raises`` échoue (DID NOT RAISE) et une entrée divergente reste
    pending, candidate à devenir head indépendamment.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    # seq 3 : identité FIDÈLE à l'incoming (nodeA, term 0). Itérée en PREMIER.
    await store.enqueue(entry(3, "evtDUP", "nodeA"))
    # seq 7 : MÊME event_id, requester DIVERGENT (nodeB). Itérée APRÈS.
    await store.enqueue(entry(7, "evtDUP", "nodeB"))
    assert len(await store.list_queue()) == 2

    with pytest.raises(QueueReplayConflictError):
        await rt.submit(
            event_id="evtDUP",
            requester_node_id="nodeA",  # FIDÈLE à seq 3, DIVERGENT vs seq 7
            term=0,
            membership_epoch=1,
            bank_version=-1,
        )

    # Fail-closed : aucune écriture ; les deux objets préexistants intacts.
    entries = await store.list_queue()
    assert len(entries) == 2
    assert {e.requester_node_id for e in entries} == {"nodeA", "nodeB"}


async def test_submit_raises_on_two_divergent_preexisting_same_event_id() -> None:
    """DEUX objets durables préexistants du même ``event_id`` qui divergent
    ENTRE EUX (même si l'incoming coïncide avec l'un d'eux) -> état déjà
    corrompu : ``submit`` LÈVE ``QueueReplayConflictError`` plutôt que de
    retourner l'un des deux comme un succès.

    RED-without : l'ancien garde retournait la première entrée rencontrée dont
    l'identité == incoming, masquant la divergence interne de la queue.
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    await store.enqueue(entry(2, "evtDUP", "nodeA"))
    await store.enqueue(entry(9, "evtDUP", "nodeB"))  # diverge de seq 2

    with pytest.raises(QueueReplayConflictError):
        await rt.submit(
            event_id="evtDUP",
            requester_node_id="nodeA",  # coïncide avec seq 2 seulement
            term=0,
            membership_epoch=1,
            bank_version=-1,
        )

    assert len(await store.list_queue()) == 2


# =============================================================================
# Duplicata FIDÈLE même event_id à des seq distincts (course S3 passée) :
#   (a) ``queue_anomalies`` le SURFACE (DuplicateEventId), jamais coalescé ;
#   (b) ``select_head`` ne laisse PAS le duplicata non-canonique devenir un
#       head indépendant après grant/cancel du canonique.
# C'est le finding BLOCKING Codex (pr97) : faithful same-event_id duplicates
# at different sequences remain independent PENDING entries.
# =============================================================================


async def test_faithful_same_event_id_duplicates_surfaced_as_anomaly() -> None:
    """Deux objets durables FIDÈLES (même identité) du même ``event_id`` à des
    ``sequence`` DISTINCTES -> ``queue_anomalies`` rapporte un
    ``DuplicateEventId`` avec les deux seq, jamais coalescé/supprimé.

    RED-without : avant le fix, ``queue_anomalies`` n'appelait que
    ``detect_seq_collisions`` (groupé par ``sequence``, exige >= 2 event_ids
    DISTINCTS au même seq). Ici les deux entrées partagent l'``event_id`` à des
    seq DIFFÉRENTS -> aucune SeqCollision -> ``queue_anomalies() == []`` : l'état
    résiduel restait SILENCIEUX. Le test échoue alors (liste vide != attendue).
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    # Course S3 passée : même event_id FIDÈLE écrit à seq 3 PUIS 7 (pas de CAS).
    # Seedé directement (le garde d'idempotence de ``submit`` bloquerait sinon).
    await store.enqueue(entry(3, "evtDUP", "nodeA"))
    await store.enqueue(entry(7, "evtDUP", "nodeA"))
    assert len(await store.list_queue()) == 2

    # Aucune SeqCollision (event_id partagé, pas de seq partagé par >= 2 ids).
    anomalies = await rt.queue_anomalies()
    assert anomalies == [
        DuplicateEventId(
            event_id="evtDUP",
            sequences=(3, 7),
            requester_node_ids=("nodeA",),
        )
    ]

    # Jamais coalescé ni supprimé : les deux objets durables restent présents.
    assert len(await store.list_queue()) == 2


async def test_non_canonical_duplicate_never_becomes_independent_head() -> None:
    """Le PIÈGE liveness exact du finding : ``evtDUP`` FIDÈLE à seq 3 et seq 7,
    tous deux ``PENDING``. On accorde (grant) le canonique (seq 3). Le duplicata
    non-canonique (seq 7) ne doit PAS devenir un head indépendant pour le MÊME
    événement logique déjà accordé.

    RED-without : avant le fix, ``select_head`` ne considérait que ``status ==
    PENDING`` brut. Après grant de seq 3, seq 7 restait PENDING -> ``head()``
    retournait seq 7 (faux second head, double-grant du même event_id). Le test
    échoue alors (head non-None / sequence == 7).
    """
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    await store.enqueue(entry(3, "evtDUP", "nodeA"))
    await store.enqueue(entry(7, "evtDUP", "nodeA"))

    # Head = entrée canonique (seq le plus bas) de ``evtDUP``.
    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evtDUP"
    assert head.sequence == 3

    # Le duplicata non-canonique (seq 7) n'est JAMAIS sélectionnable comme head
    # tant que le canonique est PENDING (un seul head pour un seul event_id).
    assert head.sequence != 7

    # Grant du canonique -> l'événement logique est consommé. Le duplicata
    # non-canonique resté PENDING ne doit PAS prendre le relais comme head.
    await rt.mark_granted(head)

    next_head = await rt.head(m)
    assert next_head is None, (
        "le duplicata non-canonique (seq 7) ne doit pas devenir un head "
        "indépendant après grant du canonique (seq 3) — un event_id == un "
        "seul événement logique, jamais double-grant"
    )

    # L'anomalie reste surfacée pour recovery (#10) APRÈS le grant du canonique.
    # C'est le finding BLOCKING Codex (pr97, head cd9ff95) : un duplicata durable
    # non-canonique ne doit pas devenir INVISIBLE à l'observabilité dès que le
    # canonique quitte PENDING — sinon recovery perd l'état résiduel exactement
    # après la transition que la PR est censée protéger.
    #
    # RED-without : avant le fix, ``detect_event_id_duplicates`` ne groupait que
    # les entrées PENDING. Après grant de seq 3, seul seq 7 reste PENDING ->
    # ``len(sequences) < 2`` -> ``queue_anomalies() == []``. Le test échoue alors
    # (liste vide != le DuplicateEventId attendu).
    anomalies_after_grant = await rt.queue_anomalies()
    assert anomalies_after_grant == [
        DuplicateEventId(
            event_id="evtDUP",
            sequences=(3, 7),
            requester_node_ids=("nodeA",),
        )
    ], (
        "queue_anomalies() doit ENCORE rapporter le DuplicateEventId(evtDUP, "
        "(3, 7)) après le grant du canonique — le duplicata résiduel reste "
        "surfacé pour recovery, jamais masqué par la transition de status"
    )

    # Aucune coalescence/suppression : les deux objets restent durablement là.
    assert len(await store.list_queue()) == 2


def test_select_head_excludes_non_canonical_duplicate_pure_layer() -> None:
    """Couche PURE (ce que #7 importe verbatim) : ``select_head`` ne rend que
    l'entrée canonique (min sous ``queue_order_key``) d'un ``event_id`` dupliqué,
    quel que soit l'ordre de la liste et même si le canonique n'est PLUS PENDING.

    RED-without : sans le filtre canonical-par-event_id, le duplicata seq 7
    (PENDING) deviendrait head dès que le canonique seq 3 n'est plus PENDING.
    """
    m = membership(1, "nodeA")
    canon = entry(3, "evtDUP", "nodeA")
    dup = entry(7, "evtDUP", "nodeA")

    # Les deux PENDING : head = canonique (seq 3), ordre de liste indifférent.
    assert select_head([canon, dup], m).sequence == 3
    assert select_head([dup, canon], m).sequence == 3

    # Canonique GRANTED : le duplicata non-canonique reste INÉLIGIBLE -> None.
    granted_canon = entry(
        3, "evtDUP", "nodeA", status=QueueEntryStatus.GRANTED
    )
    assert select_head([granted_canon, dup], m) is None
    assert select_head([dup, granted_canon], m) is None


class _YieldOnFirstQueueListStorage(FakeStorage):
    """``FakeStorage`` qui SUSPEND (``await asyncio.sleep(0)``) au tout premier
    ``list_objects`` sur le préfixe queue, APRÈS avoir capturé le snapshot.

    But : rendre DÉTERMINISTE l'interleaving async décrit par Codex. Avec un
    ``FakeStorage`` pur (aucune coroutine ne suspend réellement), deux
    ``submit`` lancés via ``asyncio.gather`` s'exécutent l'un APRÈS l'autre
    (le 1ᵉ va jusqu'au bout avant que le 2ᵉ ne démarre) — le bug de course ne
    se manifeste alors jamais.

    La suspension a lieu APRÈS le calcul du snapshot (et non avant) : c'est la
    fenêtre vulnérable exacte. submit A scanne la queue, OBTIENT un snapshot
    vide, puis se suspend ; submit B s'exécute entièrement (scanne vide,
    alloue seq 0, enqueue) ; submit A reprend avec son snapshot « absent »
    DÉJÀ figé. Sans verrou, A tombe alors dans ``allocate_sequence`` ->
    ``max(seq)+1 == 1`` et écrit un SECOND objet ``1_evtE`` (deux objets pour
    un seul ``event_id``). Si la suspension précédait le snapshot, A relirait
    l'état frais et verrait l'entrée de B — ça masquerait le bug.
    """

    def __init__(self) -> None:
        super().__init__()
        self._yielded_on_queue_list = False

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        result = await super().list_objects(prefix, max_keys=max_keys)
        if "/queue/" in prefix and not self._yielded_on_queue_list:
            self._yielded_on_queue_list = True
            await asyncio.sleep(0)
        return result


async def test_concurrent_submit_same_event_id_yields_single_durable_entry() -> None:
    """Deux ``submit`` CONCURRENTS (``asyncio.gather``) du MÊME ``event_id``
    avec ``sequence=None`` sur le même store -> EXACTEMENT UN objet de queue
    durable.

    RED-without : sans la sérialisation de toute la section critique de
    ``submit`` (scan + ``allocate_sequence`` + ``enqueue``) sous le verrou
    par-space, l'interleaving forcé par ``_YieldOnFirstQueueListStorage`` fait
    que les deux ``submit`` scannent « absent », allouent des seq distincts
    (0 et 1) et écrivent DEUX objets store ``{seq}_{event_id}`` (la clé store
    est ``sequence + event_id``) -> ``list_queue()`` vaut 2. Avec le verrou,
    le 2ᵉ ``submit`` attend, relit la queue, voit l'entrée du 1ᵉ et la RETOURNE
    -> ``list_queue()`` vaut 1 et les deux résultats partagent le même seq.
    """
    storage = _YieldOnFirstQueueListStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")

    first, second = await asyncio.gather(
        rt.submit(
            event_id="evtE",
            requester_node_id="nodeA",
            term=1,
            membership_epoch=1,
        ),
        rt.submit(
            event_id="evtE",
            requester_node_id="nodeA",
            term=1,
            membership_epoch=1,
        ),
    )

    entries = await store.list_queue()
    assert len(entries) == 1
    # Les deux soumissions convergent sur la même entrée durable (même seq).
    assert first.event_id == "evtE"
    assert second.event_id == "evtE"
    assert first.sequence == second.sequence == entries[0].sequence


# =============================================================================
# T10bis — all-ACK fail-closed : ensemble ACTIVE vide LÈVE (jamais True)
# =============================================================================


async def test_is_fully_acked_raises_on_none_membership() -> None:
    """``membership=None`` est de l'état critique incomplet, PAS un all-ACK
    valide à zéro peer.

    RED-without : ``active_requester_ids(None)`` est l'ensemble vide et
    ``frozenset().issubset(received)`` vaut ``True`` -> faux « fully acked ».
    Le fix LÈVE ``CorruptedStateError`` (fail-closed).
    """
    storage = FakeStorage()
    store = make_store(storage)
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    rt = QueueRuntime(store, "alpha")

    with pytest.raises(CorruptedStateError):
        await rt.is_fully_acked("evtE", None)


async def test_is_fully_acked_raises_on_all_evicted_membership() -> None:
    """Une membership présente mais SANS aucun membre ACTIVE (tous évincés)
    donne aussi un ensemble ACTIVE vide -> fail-closed identique à ``None``.

    RED-without : l'ensemble ACTIVE vide rendait ``issubset`` trivialement vrai.
    """
    storage = FakeStorage()
    store = make_store(storage)
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    rt = QueueRuntime(store, "alpha")
    # Aucun ACTIVE, deux nodes EVICTED -> active_requester_ids == frozenset().
    m = membership(2, evicted=("nodeA", "nodeB"))

    with pytest.raises(CorruptedStateError):
        await rt.is_fully_acked("evtE", m)


async def test_is_fully_acked_valid_membership_still_computes_identity() -> None:
    """Garde-fou anti-régression : avec une membership ACTIVE valide, l'all-ACK
    calcule TOUJOURS l'identité d'ensemble (le fail-closed ne casse pas le cas
    nominal)."""
    storage = FakeStorage()
    store = make_store(storage)
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeA"))
    await store.record_ack(Ack(event_id="evtE", ack_by_node_id="nodeB"))
    rt = QueueRuntime(store, "alpha")

    # Identité complète {A,B} ⊆ {A,B} -> True.
    assert await rt.is_fully_acked("evtE", membership(1, "nodeA", "nodeB")) is True
    # Un membre ACTIVE manquant (C) -> False (pas de raise, pas de True).
    assert (
        await rt.is_fully_acked("evtE", membership(1, "nodeA", "nodeB", "nodeC"))
        is False
    )


# =============================================================================
# T11 — mark_granted fait avancer le head (handoff vers #7)
# =============================================================================


async def test_mark_granted_advances_head() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    await store.enqueue(entry(0, "evt0", "nodeA"))
    await store.enqueue(entry(1, "evt1", "nodeA"))
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    head = await rt.head(m)
    assert head is not None
    assert head.event_id == "evt0"

    await rt.mark_granted(head)

    next_head = await rt.head(m)
    assert next_head is not None
    assert next_head.event_id == "evt1"


# =============================================================================
# T12 — isolation : queue_runtime n'importe ni consolidateur ni graph
# =============================================================================


def test_queue_runtime_does_not_import_consolidation() -> None:
    """Assertion d'import-graph statique (sans instancier le worker réel) :
    ``queue_runtime`` ne référence ni ``consolidation_queue`` /
    ``consolidator`` ni ``graph_push`` ; et ``consolidation_queue`` n'a aucune
    référence inverse vers ``queue_runtime``."""
    qr_source = inspect.getsource(queue_runtime_module)
    forbidden = ("consolidation_queue", "consolidator", "graph_push")

    # Aucune importation interdite (vérifié sur l'AST des imports, plus robuste
    # qu'un substring dans une docstring).
    imported_modules: list[str] = []
    tree = ast.parse(qr_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported_modules += [alias.name for alias in node.names]
    for mod in imported_modules:
        for needle in forbidden:
            assert needle not in mod, (
                f"queue_runtime importe interdit: {mod!r} (contient {needle!r})"
            )

    # Le module consolidation_queue ne référence pas queue_runtime (pas de
    # couplage inverse qui pourrait câbler le FIFO existant sur le runtime).
    from live_mem.core import consolidation_queue

    cq_source = inspect.getsource(consolidation_queue)
    assert "queue_runtime" not in cq_source


# =============================================================================
# T13 — space_id mismatch rejeté à la construction
# =============================================================================


def test_space_id_mismatch_rejected() -> None:
    storage = FakeStorage()
    store = make_store(storage, space_id="alpha")
    with pytest.raises(ValueError):
        QueueRuntime(store, "beta")


# =============================================================================
# T14 — head None sur queue vide / tout consommé
# =============================================================================


async def test_head_none_on_empty_and_all_consumed() -> None:
    storage = FakeStorage()
    store = make_store(storage)
    rt = QueueRuntime(store, "alpha")
    m = membership(1, "nodeA")

    # Queue vide.
    assert await rt.head(m) is None

    # Une seule entrée, GRANTED -> plus aucune PENDING éligible.
    await store.enqueue(
        entry(0, "evtDone", "nodeA", status=QueueEntryStatus.GRANTED)
    )
    assert await rt.head(m) is None


# =============================================================================
# Sanity : le comparateur pur est réutilisable par #7 (import direct)
# =============================================================================


def test_select_head_is_pure_and_order_independent() -> None:
    """``select_head`` (couche pure) opère sur une liste passée, sans I/O, et
    est indépendant de l'ordre de la liste. C'est ce que #7 importe verbatim."""
    m = membership(1, "nodeA", "nodeB")
    e0 = entry(0, "evt0", "nodeA")
    e1 = entry(1, "evt1", "nodeB")

    assert select_head([e0, e1], m).event_id == "evt0"
    assert select_head([e1, e0], m).event_id == "evt0"
    assert select_head([], m) is None


# =============================================================================
# Précédence du 4-tuple : chaque terme du milieu DÉCIDE avant le suivant.
# Ces deux tests isolent requester_node_id (3e) et membership_epoch (2e) en
# rendant les champs de tie-break ANTI-corrélés, de sorte qu'un comparateur
# dégénéré (l'Option 1 rejetée par l'ADR : `event_id` seul, ou tout sous-tuple
# qui sauterait un terme) désignerait un AUTRE head et échouerait ici.
# =============================================================================


def test_order_requester_precedes_event_id() -> None:
    """À ``(sequence, membership_epoch)`` ÉGAL, ``requester_node_id`` (3e terme)
    décide AVANT ``event_id`` (4e). Données anti-corrélées : le candidat au plus
    petit requester porte le plus GRAND event_id. Le 4-tuple choisit par
    requester ; un comparateur `event_id`-seul (Option 1 rejetée, ADR-0009:115)
    choisirait l'autre."""
    m = membership(3, "nodeA", "nodeZ")
    # même seq=5, même epoch=3 ; nodeA (requester min) <-> evtZZZ (event_id max).
    lower_requester = entry(5, "evtZZZ", "nodeA", epoch=3)
    higher_requester = entry(5, "evtAAA", "nodeZ", epoch=3)

    head = select_head([higher_requester, lower_requester], m)
    assert head is not None
    assert head.requester_node_id == "nodeA", "requester_node_id must decide before event_id"
    assert head.event_id == "evtZZZ"
    # ordre de liste inverse -> même head (déterminisme).
    assert select_head([lower_requester, higher_requester], m).event_id == "evtZZZ"


def test_order_epoch_precedes_requester_and_event_id() -> None:
    """À ``sequence`` ÉGAL, ``membership_epoch`` (2e terme) décide AVANT
    ``requester_node_id`` (3e) ET ``event_id`` (4e). Le candidat au plus petit
    epoch porte le plus GRAND requester ET le plus GRAND event_id : seul l'epoch
    le désigne comme head. Un comparateur qui sauterait l'epoch choisirait
    l'autre."""
    m = membership(9, "nodeA", "nodeZ")
    # même seq=7 ; epoch 1 (min) <-> nodeZ (requester max) <-> evtZZZ (event max).
    lower_epoch = entry(7, "evtZZZ", "nodeZ", epoch=1)
    higher_epoch = entry(7, "evtAAA", "nodeA", epoch=5)

    head = select_head([higher_epoch, lower_epoch], m)
    assert head is not None
    assert head.membership_epoch == 1, "membership_epoch must decide before requester/event_id"
    assert head.requester_node_id == "nodeZ"
    assert select_head([lower_epoch, higher_epoch], m).membership_epoch == 1
