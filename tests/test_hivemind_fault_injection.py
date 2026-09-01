# -*- coding: utf-8 -*-
"""
Tests d'injection de fautes Hivemind V1 (issue #11) — le safety gate.

Un test lisible par scénario de panne du scope d'implémentation. Chaque test
pilote le ``ClusterHarness`` + ``ProtocolModel`` (tests/hivemind_harness),
injecte UNE faute explicite, et scanne les invariants cross-nœud après chaque
pas via ``assert_invariants`` (qui nomme le nœud / term / bank_version fautif).

Déterminisme : horloge logique (``DeterministicClock``), livraison pull-based
(le test contrôle l'ordre), un seul thread, aucune horloge murale ni sleep.

Distinction des échecs :
- ``RuntimeError`` / ``PeerChannelError`` = garde-fou ATTENDU (rejet correct) ;
- ``AssertionError`` depuis le harnais = VIOLATION d'invariant (vrai bug).
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    MemberStatus,
    MembershipView,
    PeerChannelError,
    PeerErrorCode,
    PeerReceiveStatus,
    QueueEntryStatus,
    TokenLeaseState,
    TokenState,
    layout,
)

from tests.hivemind_harness import (
    ClusterHarness,
    CommitFencedError,
    DeterministicClock,
    HiveStatus,
    ProtocolModel,
    assert_invariants,
    assert_at_most_one_valid_holder,
    assert_bank_version_monotone,
    assert_no_stale_term_commit,
)


# =============================================================================
# Fixtures
# =============================================================================


async def _three_node_cluster(
    *, replay_window_seconds: int = 300
) -> tuple[ClusterHarness, ProtocolModel]:
    clock = DeterministicClock()
    cluster = await ClusterHarness.create(
        node_ids=["nodeA", "nodeB", "nodeC"],
        clock=clock,
        replay_window_seconds=replay_window_seconds,
    )
    return cluster, ProtocolModel(cluster, lease_seconds=300)


async def _full_cycle(
    cluster: ClusterHarness,
    model: ProtocolModel,
    *,
    holder: str,
    event_id: str,
    bank_version: int,
    commit_id: str,
) -> None:
    """Cycle complet claim -> all-ACK -> grant -> commit -> release."""
    await model.claim(holder, event_id=event_id)
    await model.collect_acks(event_id, holder=holder)
    await model.grant(holder, event_id=event_id)
    await model.apply_commit(holder, bank_version=bank_version, commit_id=commit_id)
    await model.release(holder)


# =============================================================================
# 1. Happy path baseline (valide le harnais avant les fautes)
# =============================================================================


@pytest.mark.asyncio
async def test_happy_path_claim_grant_commit_release_holds_all_invariants() -> None:
    cluster, model = await _three_node_cluster()
    await assert_invariants(cluster)

    await model.claim("nodeA", event_id="evt-claim")
    await assert_invariants(cluster)
    # Les pairs ont persisté l'event (chemin receive() réel).
    for nid in ("nodeB", "nodeC"):
        assert await cluster.nodes[nid].store.get_event("evt-claim") is not None

    received = await model.collect_acks("evt-claim", holder="nodeA")
    assert received == 3  # all-ACK (3 membres actifs)
    assert await model.can_grant("evt-claim", holder="nodeA") is True

    token = await model.grant("nodeA", event_id="evt-claim")
    assert token.state == TokenState.HELD.value
    assert token.fencing_token == token.term == cluster.term
    await assert_invariants(cluster)

    commit = await model.apply_commit("nodeA", bank_version=0, commit_id="c0")
    assert commit.term == cluster.term
    await assert_invariants(cluster)

    freed = await model.release("nodeA")
    assert freed.state == TokenState.FREE.value
    await assert_invariants(cluster)


# =============================================================================
# 2. Duplicate delivery is a no-op
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_message_is_noop() -> None:
    cluster, model = await _three_node_cluster()

    # Claim émis, mais on N'arme PAS la livraison auto.
    await model.claim("nodeA", event_id="evt-dup", deliver=False)
    # Duplique le message en transit vers nodeB (réseau qui livre 2x).
    cluster.transport.duplicate("nodeB", index=0)

    # Première livraison : acceptée et persistée.
    first = await cluster.transport.deliver_next("nodeB")
    assert first.status == PeerReceiveStatus.ACCEPTED.value
    snap_before = cluster.nodes["nodeB"].storage.snapshot()

    # Deuxième livraison (le doublon) : DUPLICATE, aucun write.
    second = await cluster.transport.deliver_next("nodeB")
    assert second.status == PeerReceiveStatus.DUPLICATE.value
    assert second.persisted is False
    assert cluster.nodes["nodeB"].storage.objects == snap_before

    await assert_invariants(cluster)


# =============================================================================
# 3. Reordered messages converge to the same state
# =============================================================================


@pytest.mark.asyncio
async def test_reordered_messages_converge_to_same_state() -> None:
    cluster, model = await _three_node_cluster()

    # Deux claims FIFO distincts émis par A, en transit vers B, sans livraison.
    e1 = await model.claim("nodeA", event_id="evt-1", sequence=0, deliver=False)
    e2 = await model.claim("nodeA", event_id="evt-2", sequence=1, deliver=False)
    assert e1.event_id == "evt-1" and e2.event_id == "evt-2"

    # Le réseau livre dans le désordre (evt-2 avant evt-1).
    pending = cluster.transport.pending("nodeB")
    assert [p.event_id for p in pending] == ["evt-1", "evt-2"]
    cluster.transport.reorder("nodeB", [1, 0])

    await cluster.transport.deliver_all("nodeB")

    # L'event journal de B est trié chronologiquement (la clé porte le ts) ;
    # la queue durable de A reste FIFO par sequence indépendamment de l'ordre
    # réseau. L'état final est indépendant de l'ordre de livraison.
    events_b = await cluster.nodes["nodeB"].store.list_events()
    assert {e.event_id for e in events_b} == {"evt-1", "evt-2"}
    queue_a = await cluster.nodes["nodeA"].store.list_queue()
    assert [q.sequence for q in queue_a] == [0, 1]
    await assert_invariants(cluster)


# =============================================================================
# 3b. deliver_all préserve les messages restants si un handler lève
# =============================================================================


@pytest.mark.asyncio
async def test_deliver_all_preserves_pending_when_handler_raises() -> None:
    """
    RÉGRESSION (Codex P2 #2) : ``deliver_all`` livre INCRÉMENTALEMENT. Si un
    receiver rejette un message (ici epoch erroné -> ``PeerChannelError``),
    l'exception se propage SANS jeter les messages encore en file. L'ancien
    code snapshotait+vidait la file avant d'itérer, perdant silencieusement le
    bon message situé APRÈS le mauvais et masquant les bugs de retry/ordre.

    Scénario : un mauvais message (mauvais ``membership_epoch``) est mis en
    transit vers nodeB AVANT un bon message. ``deliver_all`` lève sur le
    mauvais ; le bon DOIT rester pending, puis se livrer à la passe suivante.
    """
    cluster, model = await _three_node_cluster()

    # Message MAUVAIS : epoch incompatible -> le receiver lèvera
    # WRONG_MEMBERSHIP_EPOCH au moment de la livraison.
    bad_event = EventEnvelope(
        event_id="evt-bad-epoch",
        request_id="evt-bad-epoch",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=cluster.term,
        membership_epoch=cluster.epoch + 7,  # epoch futur inconnu du receiver
        payload={"kind": "queue_request", "sequence": 0},
        created_at=cluster.clock.iso(),
    )
    # Message BON : epoch courant -> accepté normalement.
    good_event = EventEnvelope(
        event_id="evt-good",
        request_id="evt-good",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=cluster.term,
        membership_epoch=cluster.epoch,
        payload={"kind": "queue_request", "sequence": 1},
        created_at=cluster.clock.iso(),
    )

    # Le mauvais est mis en transit AVANT le bon, dans la MÊME boîte (nodeB).
    await cluster.send_to("nodeA", "nodeB", bad_event)
    await cluster.send_to("nodeA", "nodeB", good_event)
    pending_ids = [p.event_id for p in cluster.transport.pending("nodeB")]
    assert pending_ids == ["evt-bad-epoch", "evt-good"]

    # deliver_all lève sur le mauvais message...
    with pytest.raises(PeerChannelError) as exc:
        await cluster.transport.deliver_all("nodeB")
    assert exc.value.code == PeerErrorCode.WRONG_MEMBERSHIP_EPOCH

    # ...mais le bon message reste PENDING (non perdu par le vidage anticipé).
    still_pending = [p.event_id for p in cluster.transport.pending("nodeB")]
    assert still_pending == ["evt-good"]
    # Le mauvais a bien été retiré (consommé puis rejeté), pas persisté.
    assert await cluster.nodes["nodeB"].store.has_event("evt-bad-epoch") is False
    assert await cluster.nodes["nodeB"].store.has_event("evt-good") is False

    # Une seconde passe livre le bon message restant : il est accepté+persisté.
    results = await cluster.transport.deliver_all("nodeB")
    assert len(results) == 1
    assert results[0].status == PeerReceiveStatus.ACCEPTED.value
    assert cluster.transport.pending("nodeB") == []
    assert await cluster.nodes["nodeB"].store.has_event("evt-good") is True
    await assert_invariants(cluster)


# =============================================================================
# 4. Dropped ACK blocks progress (all-ACK)
# =============================================================================


@pytest.mark.asyncio
async def test_dropped_ack_blocks_progress() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-block")

    # nodeC's ACK is dropped: only A and B ACK to the holder.
    received = await model.collect_acks(
        "evt-block", holder="nodeA", ackers=["nodeA", "nodeB"]
    )
    assert received == 2  # < 3 expected

    assert await model.can_grant("evt-block", holder="nodeA") is False
    status = await model.hive_status("nodeA", pending_event="evt-block")
    assert status == HiveStatus.BLOCKED

    # Le grant est refusé (pas de progrès silencieux).
    with pytest.raises(RuntimeError, match="all-ACK non satisfait"):
        await model.grant("nodeA", event_id="evt-block")

    # Aucun token accordé, aucun commit.
    assert await cluster.nodes["nodeA"].store.get_token() is None
    assert await cluster.nodes["nodeA"].store.list_commits() == []
    await assert_invariants(cluster)


# =============================================================================
# 4b. Un pair qui n'a JAMAIS reçu le claim ne peut pas ACK (livraison perdue)
# =============================================================================


@pytest.mark.asyncio
async def test_undelivered_peer_cannot_ack_blocks_grant() -> None:
    """
    RÉGRESSION (Codex P2 #1) : un ACK n'est légitime QUE si le pair a persisté
    l'event en local (HIVEMIND.md §6.1 : ACK après écriture durable du journal).

    Ici nodeC ne reçoit JAMAIS le claim (``deliver=False``, jamais livré). Même
    si le test appelle ``collect_acks`` pour TOUS les nœuds, nodeC ne produit
    aucun ACK : il n'a pas l'event. all-ACK reste donc insatisfait et le grant
    est bloqué — exactement le scénario de livraison perdue que le gate existe
    pour attraper. Sans la garde ``has_event`` dans ``ack``, un ACK fantôme de
    nodeC rendrait ``can_grant`` vrai et accorderait le token à tort.
    """
    cluster, model = await _three_node_cluster()

    # Claim émis par A, mis en transit vers B et C, mais JAMAIS livré.
    await model.claim("nodeA", event_id="evt-lost", deliver=False)

    # On ne livre QUE vers nodeB ; nodeC reste sans l'event.
    await cluster.transport.deliver_all("nodeB")

    # Pré-conditions : A (holder) et B ont l'event, C ne l'a pas.
    assert await cluster.nodes["nodeA"].store.has_event("evt-lost") is True
    assert await cluster.nodes["nodeB"].store.has_event("evt-lost") is True
    assert await cluster.nodes["nodeC"].store.has_event("evt-lost") is False

    # ``ack`` direct pour nodeC est REFUSÉ (retourne False, n'enregistre rien).
    acked_c = await model.ack("nodeC", event_id="evt-lost", to_holder="nodeA")
    assert acked_c is False

    # On sollicite TOUS les nœuds : seuls A et B ACKent réellement.
    received = await model.collect_acks("evt-lost", holder="nodeA")
    assert received == 2  # nodeC absent malgré la sollicitation

    # nodeC est bien hors de l'ensemble d'ACK enregistré (cross-nœud).
    ack_set = await cluster.received_ack_set("evt-lost")
    assert ack_set == {"nodeA", "nodeB"}
    assert "nodeC" not in ack_set

    # all-ACK insatisfait -> grant bloqué, aucun token, aucun commit.
    assert await model.can_grant("evt-lost", holder="nodeA") is False
    with pytest.raises(RuntimeError, match="all-ACK non satisfait"):
        await model.grant("nodeA", event_id="evt-lost")
    assert await cluster.nodes["nodeA"].store.get_token() is None
    assert await cluster.nodes["nodeA"].store.list_commits() == []
    await assert_invariants(cluster)

    # Quand nodeC reçoit enfin le claim, son ACK devient légitime et débloque.
    await cluster.transport.deliver_all("nodeC")
    assert await cluster.nodes["nodeC"].store.has_event("evt-lost") is True
    acked_c = await model.ack("nodeC", event_id="evt-lost", to_holder="nodeA")
    assert acked_c is True
    assert await model.can_grant("evt-lost", holder="nodeA") is True
    await model.grant("nodeA", event_id="evt-lost")
    await assert_invariants(cluster)


# =============================================================================
# 5a. Crash after ACK -> restart consistent
# =============================================================================


@pytest.mark.asyncio
async def test_crash_after_ack_restart_consistent() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-crash-ack")
    await model.collect_acks("evt-crash-ack", holder="nodeA")

    # Crash AVANT le grant (ACKs persistés sur le storage de A).
    cluster.nodes["nodeA"].crash()
    snap = await cluster.nodes["nodeA"].restart()
    assert snap.token is None  # aucun grant n'a eu lieu

    # Post-restart, on peut compléter sans double effet.
    assert await model.can_grant("evt-crash-ack", holder="nodeA") is True
    await model.grant("nodeA", event_id="evt-crash-ack")
    await assert_invariants(cluster)
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 5b. Crash after token persist, before broadcast
# =============================================================================


@pytest.mark.asyncio
async def test_crash_after_token_persist_before_broadcast() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-crash-tok")
    await model.collect_acks("evt-crash-tok", holder="nodeA")
    token = await model.grant("nodeA", event_id="evt-crash-tok")
    assert token.state == TokenState.HELD.value

    # Crash juste après token.json persisté, AVANT toute diffusion TOKEN_GRANTED.
    cluster.nodes["nodeA"].crash()
    snap = await cluster.nodes["nodeA"].restart()

    # Le holder reste le SEUL holder valide après restart ; les pairs n'ont vu
    # aucun grant prématuré (token toujours absent chez B/C).
    assert snap.token is not None
    assert snap.token.holder_node_id == "nodeA"
    assert snap.token.state == TokenState.HELD.value
    for nid in ("nodeB", "nodeC"):
        assert await cluster.nodes[nid].store.get_token() is None
    await assert_invariants(cluster)
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 5c. Crash after commit, before release
# =============================================================================


@pytest.mark.asyncio
async def test_crash_after_commit_before_release() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-crash-commit")
    await model.collect_acks("evt-crash-commit", holder="nodeA")
    await model.grant("nodeA", event_id="evt-crash-commit")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")

    # Crash après le commit + pointeur, AVANT le release.
    cluster.nodes["nodeA"].crash()
    snap = await cluster.nodes["nodeA"].restart()

    # Le commit est durable, le token toujours releasable, pas de second holder.
    assert len(snap.commits) == 1
    assert snap.bank_version_pointer.bank_version == 0
    assert snap.token.state == TokenState.HELD.value
    await assert_invariants(cluster)

    freed = await model.release("nodeA")
    assert freed.state == TokenState.FREE.value
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 6. Lease expiry then higher term wins; old holder fenced
# =============================================================================


@pytest.mark.asyncio
async def test_lease_expiry_then_higher_term_wins() -> None:
    cluster, model = await _three_node_cluster()

    # A obtient le token (term grows to 2), lease 300s.
    await model.claim("nodeA", event_id="evt-A")
    await model.collect_acks("evt-A", holder="nodeA")
    token_a = await model.grant("nodeA", event_id="evt-A")
    assert token_a.term == 2

    # Le temps logique dépasse la lease -> expiration observable.
    cluster.tick(seconds=600)
    assert model.is_lease_expired(token_a, cluster.clock.iso()) is True

    # B gagne un term supérieur (nouveau grant). Le store de B porte ce token.
    await model.claim("nodeB", event_id="evt-B")
    await model.collect_acks("evt-B", holder="nodeB")
    token_b = await model.grant("nodeB", event_id="evt-B")
    assert token_b.term == 3 and token_b.holder_node_id == "nodeB"

    # L'ancien holder A tient ENCORE son token HELD au term 2 dans son store.
    stale_token_a = await cluster.nodes["nodeA"].store.get_token()
    assert stale_token_a is not None
    assert stale_token_a.state == TokenState.HELD.value
    assert stale_token_a.holder_node_id == "nodeA"
    assert stale_token_a.term == token_a.term == 2

    # À ce stade précis, A (HELD@2) et B (HELD@3) sont DEUX holders valides à des
    # terms différents : c'est exactement la divergence cross-term que §6.2/§6.3
    # interdit. L'invariant renforcé DOIT l'attraper (false negative comblé).
    with pytest.raises(AssertionError, match="STALE-HOLDER"):
        await assert_at_most_one_valid_holder(cluster)

    # Le term supérieur (3) se propage au store de A (event TERM_BUMPED reçu),
    # modélisant que A apprend qu'un nouveau holder a pris la main.
    await cluster.nodes["nodeA"].store.bump_term(token_b.term, updated_by_node_id="nodeB")

    # A apprend le term supérieur : il DOIT réconcilier son token stale hors de
    # HELD (§6.2/§6.3 : un holder superseded ne reste pas un HELD silencieux).
    reconciled = await model.reconcile_stale_holder("nodeA")
    assert reconciled is not None
    assert reconciled.state == TokenState.FREE.value
    assert reconciled.holder_node_id is None
    # Fencing monotone : le term/fencing du token de A a MONTÉ au term courant.
    assert reconciled.term == token_b.term == 3
    assert reconciled.fencing_token == 3

    # Après réconciliation : B reste le SEUL holder valide, au term max (3), et
    # aucun holder actif ne subsiste sous ce max -> l'invariant tient.
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)

    # L'ANCIEN holder A tente quand même de committer à SON term périmé (2) : le
    # commit-apply guard le fence AVANT append_commit. La réconciliation n'a PAS
    # affaibli le fencing — A reste incapable de committer son ancien term.
    commits_before = {
        nid: len(await cluster.nodes[nid].store.list_commits())
        for nid in cluster.node_ids()
    }
    with pytest.raises(CommitFencedError):
        await model.apply_commit(
            "nodeA", bank_version=0, commit_id="stale-a", commit_term=token_a.term
        )

    # Le commit stale de A n'a atteint AUCUN store : préfixe commits/ propre
    # partout, et aucun nouveau commit nulle part.
    for nid in cluster.node_ids():
        commits = await cluster.nodes[nid].store.list_commits()
        assert len(commits) == commits_before[nid]
        assert all(c.commit_id != "stale-a" for c in commits)

    await assert_no_stale_term_commit(cluster)
    await assert_invariants(cluster)
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 7. Stale holder return is fenced at the peer channel
# =============================================================================


@pytest.mark.asyncio
async def test_stale_holder_return_is_fenced() -> None:
    cluster, model = await _three_node_cluster()

    # Term avance à 5 sur tous les nœuds (un nouveau holder a pris la main).
    for nid in cluster.node_ids():
        await cluster.nodes[nid].store.bump_term(5, updated_by_node_id="nodeB")
    cluster.term = 5

    # L'ancien holder A revient et tente de diffuser un BANK_COMMITTED au term 2.
    stale_event = EventEnvelope(
        event_id="evt-stale-commit",
        type=EventType.BANK_COMMITTED,
        origin_node_id="nodeA",
        term=2,  # term périmé
        membership_epoch=cluster.epoch,
        bank_version=0,
        created_at=cluster.clock.iso(),
    )
    # La diffusion vers B est rejetée fail-closed (STALE_TERM) à la réception.
    await cluster.send_to("nodeA", "nodeB", stale_event)
    with pytest.raises(PeerChannelError) as err:
        await cluster.transport.deliver_next("nodeB")
    assert err.value.code == PeerErrorCode.STALE_TERM

    # Le commit stale n'a jamais atteint append_commit : aucun commit chez B.
    assert await cluster.nodes["nodeB"].store.list_commits() == []
    await assert_no_stale_term_commit(cluster)
    await assert_invariants(cluster)


# =============================================================================
# 8. Concurrent queue requests serialize deterministically
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_queue_requests_serialize() -> None:
    cluster, model = await _three_node_cluster()

    # Deux claims concurrents (B et C) ; l'allocateur déterministe donne le
    # head au plus petit sequence, tie-break par event_id.
    eb = await model.claim("nodeB", event_id="evt-claim-b", sequence=0, deliver=True)
    ec = await model.claim("nodeC", event_id="evt-claim-c", sequence=1, deliver=True)
    assert eb.payload["sequence"] == 0
    assert ec.payload["sequence"] == 1

    # Le head (B, sequence 0) gagne le token d'abord ; un seul holder à la fois.
    await model.collect_acks("evt-claim-b", holder="nodeB")
    await model.grant("nodeB", event_id="evt-claim-b")
    await assert_at_most_one_valid_holder(cluster)
    await model.apply_commit("nodeB", bank_version=0, commit_id="cb")
    await model.release("nodeB")

    # Puis C (sequence 1) prend la main au term suivant.
    await model.collect_acks("evt-claim-c", holder="nodeC")
    token_c = await model.grant("nodeC", event_id="evt-claim-c")
    assert token_c.holder_node_id == "nodeC"
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)


# =============================================================================
# 8b. Out-of-order grant rejected: non-head of queue cannot be granted
# =============================================================================


@pytest.mark.asyncio
async def test_out_of_order_grant_rejected() -> None:
    """
    RÉGRESSION (Codex P2 #2) : un grant doit viser le HEAD déterministe
    ``(sequence, event_id)`` de la queue. Deux claims FIFO sur le MÊME store ;
    accorder le second (sequence 1) avant le head (sequence 0) est rejeté AVANT
    tout bump de term / écriture de token.
    """
    cluster, model = await _three_node_cluster()

    # Deux claims du MÊME requester -> deux entrées dans SA queue durable.
    await model.claim("nodeA", event_id="evt-head", sequence=0)
    await model.claim("nodeA", event_id="evt-tail", sequence=1)
    await model.collect_acks("evt-head", holder="nodeA")
    await model.collect_acks("evt-tail", holder="nodeA")

    queue = await cluster.nodes["nodeA"].store.list_queue()
    assert [q.sequence for q in queue] == [0, 1]
    assert all(q.status == QueueEntryStatus.PENDING.value for q in queue)
    term_before = cluster.term

    # all-ACK est satisfait pour les deux, mais evt-tail N'EST PAS le head.
    assert await model.can_grant("evt-tail", holder="nodeA") is True
    with pytest.raises(RuntimeError, match="n'est pas le head de queue"):
        await model.grant("nodeA", event_id="evt-tail")

    # Aucun effet de bord : pas de token, pas de bump de term, queue intacte.
    assert await cluster.nodes["nodeA"].store.get_token() is None
    assert cluster.term == term_before
    queue_after = await cluster.nodes["nodeA"].store.list_queue()
    assert all(q.status == QueueEntryStatus.PENDING.value for q in queue_after)
    await assert_invariants(cluster)

    # Le head (evt-head) s'accorde ; puis evt-tail devient le head et s'accorde.
    await model.grant("nodeA", event_id="evt-head")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c-head")
    await model.release("nodeA")
    token_tail = await model.grant("nodeA", event_id="evt-tail")
    assert token_tail.state == TokenState.HELD.value
    await assert_invariants(cluster)


# =============================================================================
# 8c. Double-grant of the same queue entry rejected (consume-once)
# =============================================================================


@pytest.mark.asyncio
async def test_double_grant_same_entry_rejected() -> None:
    """
    RÉGRESSION (Codex P2 #2) : une entrée de queue accordée est CONSOMMÉE
    (GRANTED). Un second grant du même event_id est rejeté AVANT tout effet de
    bord (pas de double bump de term, pas d'écrasement de token).
    """
    cluster, model = await _three_node_cluster()

    await model.claim("nodeA", event_id="evt-once", sequence=0)
    await model.collect_acks("evt-once", holder="nodeA")
    token = await model.grant("nodeA", event_id="evt-once")
    assert token.state == TokenState.HELD.value
    term_after_first = cluster.term

    # L'entrée a été consommée (GRANTED) par le premier grant.
    queue = await cluster.nodes["nodeA"].store.list_queue()
    target = next(q for q in queue if q.event_id == "evt-once")
    assert target.status == QueueEntryStatus.GRANTED.value

    # Second grant du même event_id -> rejeté, pas de double effet.
    with pytest.raises(RuntimeError, match="n'est plus PENDING"):
        await model.grant("nodeA", event_id="evt-once")

    assert cluster.term == term_after_first  # term inchangé (pas de re-bump)
    token_now = await cluster.nodes["nodeA"].store.get_token()
    assert token_now is not None and token_now.term == token.term
    await assert_invariants(cluster)
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 8d. Distributed out-of-order grant rejected: head-of-queue is a REAL
#     distributed oracle (queue répliquée sur tous les nœuds actifs)
# =============================================================================


@pytest.mark.asyncio
async def test_distributed_out_of_order_grant_rejected() -> None:
    """
    RÉGRESSION (Codex P3 finding 1) : la garde head-of-queue doit s'appuyer sur
    la queue RÉPLIQUÉE (HIVEMIND.md §5.3 : « all peers derive the same queue
    order from the same events »), pas seulement sur la queue locale du
    requester.

    Deux requesters DIFFÉRENTS claim : nodeB à la sequence 0, nodeC à la
    sequence 1. Les deux claims sont répliqués à TOUS les nœuds actifs. Chaque
    nœud dérive donc la MÊME queue [seq0=B, seq1=C].

    Sans la réplication de queue, accorder nodeC (seq 1) réussirait à tort :
    la queue locale de nodeC ne contiendrait que SA propre entrée (seq 1), donc
    elle serait « head » localement. Avec la queue répliquée, nodeC voit
    l'entrée plus ancienne de nodeB (seq 0) et le grant hors-ordre est rejeté.
    """
    cluster, model = await _three_node_cluster()

    # Deux requesters distincts ; livraison armée -> claims répliqués partout.
    await model.claim("nodeB", event_id="evt-b", sequence=0, deliver=True)
    await model.claim("nodeC", event_id="evt-c", sequence=1, deliver=True)

    # CHAQUE nœud actif a dérivé la MÊME queue des MÊMES events (§5.3).
    for nid in cluster.node_ids():
        queue = await cluster.nodes[nid].store.list_queue()
        assert [(q.sequence, q.event_id) for q in queue] == [
            (0, "evt-b"),
            (1, "evt-c"),
        ], f"queue de {nid} non répliquée / divergente"
        assert all(q.status == QueueEntryStatus.PENDING.value for q in queue)

    # all-ACK satisfait pour les deux claims (3 membres actifs).
    await model.collect_acks("evt-b", holder="nodeB")
    await model.collect_acks("evt-c", holder="nodeC")
    assert await model.can_grant("evt-c", holder="nodeC") is True
    term_before = cluster.term

    # Accorder nodeC (seq 1) AVANT le head (seq 0 de nodeB) est rejeté grâce à
    # la queue répliquée — sans elle, ce grant hors-ordre distribué passerait.
    with pytest.raises(RuntimeError, match="n'est pas le head de queue"):
        await model.grant("nodeC", event_id="evt-c")
    assert await cluster.nodes["nodeC"].store.get_token() is None
    assert cluster.term == term_before  # aucun effet de bord (pas de bump)
    await assert_invariants(cluster)

    # Le head (nodeB, seq 0) s'accorde. nodeC consomme son entrée GRANTED chez
    # nodeB ; nodeC reste PENDING (entrée distincte). On committe puis release.
    token_b = await model.grant("nodeB", event_id="evt-b")
    assert token_b.holder_node_id == "nodeB"
    await assert_at_most_one_valid_holder(cluster)
    await model.apply_commit("nodeB", bank_version=0, commit_id="cb")
    await model.release("nodeB")

    # Après le release de nodeB, evt-c devient le head PENDING : nodeC peut être
    # accordé au term suivant.
    assert await model.can_grant("evt-c", holder="nodeC") is True
    token_c = await model.grant("nodeC", event_id="evt-c")
    assert token_c.holder_node_id == "nodeC"
    assert token_c.term > token_b.term
    await assert_invariants(cluster)
    await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 9. Concurrent token acquire -> single holder per term across all stores
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_token_acquire_single_holder() -> None:
    cluster, model = await _three_node_cluster()

    # nodeB enfile AUSSI un claim concurrent (sequence 1) : il est all-ACKé et
    # head de SA branche, mais nodeA (sequence 0) prend la lease d'abord.
    await model.claim("nodeA", event_id="evt-a", sequence=0, deliver=True)
    await model.claim("nodeB", event_id="evt-b", sequence=1, deliver=True)
    await model.collect_acks("evt-a", holder="nodeA")
    await model.collect_acks("evt-b", holder="nodeB")
    token_a = await model.grant("nodeA", event_id="evt-a")
    await assert_at_most_one_valid_holder(cluster)

    # Tentative concurrente de B via le VRAI chemin ``grant`` PENDANT que nodeA
    # tient une lease HELD non expirée : le modèle enforce désormais l'exclusion
    # mutuelle AVANT tout bump de term / écriture de token. Sans cette garde, B
    # obtiendrait un token HELD au term suivant -> DEUX détenteurs valides.
    term_before = cluster.term
    with pytest.raises(RuntimeError, match="lease active"):
        await model.grant("nodeB", event_id="evt-b")
    # Aucun effet de bord : pas de token chez B, pas de bump de term.
    assert await cluster.nodes["nodeB"].store.get_token() is None
    assert cluster.term == term_before
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)

    # Une tentative concurrente de B d'écrire un token HELD au MÊME term est
    # rejetée par la monotonie du modèle Pydantic (fencing_token != term).
    with pytest.raises(ValueError):
        # fencing_token != term au même term est déjà rejeté par le modèle.
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="nodeB",
            term=token_a.term,
            fencing_token=token_a.term - 1,
        )

    # Même en CONTOURNANT le modèle pour écrire directement un token HELD valide
    # pour B au même term sur son propre store, l'invariant cross-nœud détecte
    # le split-brain : preuve que l'invariant reste un vrai oracle indépendant
    # de la garde de ``grant``.
    await cluster.nodes["nodeB"].store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="nodeB",
            term=token_a.term,
            fencing_token=token_a.term,
        )
    )
    with pytest.raises(AssertionError, match="SPLIT-BRAIN"):
        await assert_at_most_one_valid_holder(cluster)


# =============================================================================
# 9b. Grant refusé tant qu'une lease active est tenue ; autorisé après
#     release OU expiration de lease (exclusion mutuelle dans le modèle)
# =============================================================================


@pytest.mark.asyncio
async def test_grant_blocked_while_lease_held_then_allowed_after_release() -> None:
    """
    RÉGRESSION (Codex P2 finding) : ``grant`` doit refuser un second détenteur
    tant qu'un autre nœud tient une lease HELD/RELEASING NON expirée
    (HIVEMIND.md §5.3/§6.2 : exclusion mutuelle V1). La garde s'applique AVANT
    tout bump de term / écriture de token. Le grant suivant ne progresse
    qu'après ``release`` (token FREE) — ici la voie release.

    Sans la garde, B obtiendrait un token HELD au term suivant pendant que A
    tient encore le sien : deux détenteurs valides, exactement le split-brain
    que le gate existe pour interdire.
    """
    cluster, model = await _three_node_cluster()

    # Deux claims concurrents, répliqués partout ; A (seq 0) est le head.
    await model.claim("nodeA", event_id="evt-hold", sequence=0, deliver=True)
    await model.claim("nodeB", event_id="evt-wait", sequence=1, deliver=True)
    await model.collect_acks("evt-hold", holder="nodeA")
    await model.collect_acks("evt-wait", holder="nodeB")

    # A obtient la lease (HELD, non expirée).
    token_a = await model.grant("nodeA", event_id="evt-hold")
    assert token_a.state == TokenState.HELD.value
    assert token_a.holder_node_id == "nodeA"
    assert model.is_lease_expired(token_a, cluster.clock.iso()) is False
    await assert_at_most_one_valid_holder(cluster)

    # B est all-ACKé MAIS la lease de A est encore active : grant REFUSÉ, aucun
    # effet de bord. evt-wait devient le head après que A consomme evt-hold.
    term_after_a = cluster.term
    assert await model.can_grant("evt-wait", holder="nodeB") is True
    with pytest.raises(RuntimeError, match="lease active"):
        await model.grant("nodeB", event_id="evt-wait")
    assert await cluster.nodes["nodeB"].store.get_token() is None
    assert cluster.term == term_after_a  # pas de bump
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)

    # A relâche (token FREE) -> la lease n'est plus active : B peut acquérir.
    await model.apply_commit("nodeA", bank_version=0, commit_id="c-hold")
    freed = await model.release("nodeA")
    assert freed.state == TokenState.FREE.value
    token_b = await model.grant("nodeB", event_id="evt-wait")
    assert token_b.state == TokenState.HELD.value
    assert token_b.holder_node_id == "nodeB"
    assert token_b.term > token_a.term  # term a monté au nouveau grant
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)


@pytest.mark.asyncio
async def test_grant_blocked_while_lease_held_then_allowed_after_expiry() -> None:
    """
    Pendant complémentaire : le grant suivant progresse aussi via EXPIRATION de
    lease (sans release explicite). L'horloge logique dépasse ``lease_until`` ;
    la garde d'exclusion mutuelle ignore alors le détenteur expiré et le grant à
    term supérieur passe (le holder stale est réconcilié hors HELD ensuite, cf.
    ``reconcile_stale_holder``). Confirme que la garde se compose avec
    l'expiration ET ne fige pas le cluster sur un détenteur disparu.
    """
    cluster, model = await _three_node_cluster()

    await model.claim("nodeA", event_id="evt-lease", sequence=0, deliver=True)
    await model.claim("nodeB", event_id="evt-next", sequence=1, deliver=True)
    await model.collect_acks("evt-lease", holder="nodeA")
    await model.collect_acks("evt-next", holder="nodeB")

    token_a = await model.grant("nodeA", event_id="evt-lease")
    assert token_a.state == TokenState.HELD.value

    # Lease NON expirée -> grant de B refusé (garde d'exclusion mutuelle).
    with pytest.raises(RuntimeError, match="lease active"):
        await model.grant("nodeB", event_id="evt-next")
    await assert_at_most_one_valid_holder(cluster)

    # L'horloge logique dépasse la lease (300s) -> A est expiré, plus actif au
    # sens de la garde. B peut acquérir un term supérieur.
    cluster.tick(seconds=600)
    assert model.is_lease_expired(token_a, cluster.clock.iso()) is True
    token_b = await model.grant("nodeB", event_id="evt-next")
    assert token_b.holder_node_id == "nodeB"
    assert token_b.term > token_a.term

    # A tient toujours son HELD@ancien term dans son store : c'est le holder
    # stale cross-term que §6.2/§6.3 interdit -> l'invariant l'attrape jusqu'à
    # réconciliation (cohérent avec test_lease_expiry_then_higher_term_wins).
    with pytest.raises(AssertionError, match="STALE-HOLDER"):
        await assert_at_most_one_valid_holder(cluster)

    # A apprend le term supérieur puis réconcilie son token stale hors HELD.
    await cluster.nodes["nodeA"].store.bump_term(
        token_b.term, updated_by_node_id="nodeB"
    )
    reconciled = await model.reconcile_stale_holder("nodeA")
    assert reconciled is not None and reconciled.state == TokenState.FREE.value
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)


# =============================================================================
# 9d. Le HOLDER COURANT ne peut PAS être ré-accordé une SECONDE entrée queue
#     (différente) tant que sa propre lease est active (exclusion mutuelle
#     INCLUANT sa propre lease) ; autorisé après release
# =============================================================================


@pytest.mark.asyncio
async def test_holder_cannot_be_granted_second_entry_while_holding() -> None:
    """
    RÉGRESSION (Codex round-6 P2) : l'exclusion mutuelle V1 doit INCLURE la
    lease du requérant lui-même. Le cycle est strictement claim → grant →
    commit → release. Si le HOLDER COURANT soumet une SECONDE entrée queue
    (différente) AVANT de relâcher sa première lease, ``grant`` ne doit PAS la
    lui accorder : sinon il bumperait le term et obtiendrait un second token
    pendant que sa première consolidation n'a pas franchi commit/release —
    deux consolidations chevauchantes par un même détenteur (gate affaibli).

    La garde ``_active_lease_holder`` (sans exclusion du requérant) attrape ce
    cas APRÈS la garde head-of-queue/PENDING : l'entrée visée EST le head
    PENDING (donc ce n'est PAS un double-grant de la même entrée), mais la
    lease active du holder bloque. Après ``release`` (token FREE), la
    ré-acquisition est de nouveau permise.

    Distinct de ``test_double_grant_same_entry_rejected`` (même entrée déjà
    GRANTED -> « n'est plus PENDING ») : ici la seconde entrée est DIFFÉRENTE
    et toujours PENDING -> rejet par « lease active ».
    """
    cluster, model = await _three_node_cluster()

    # Le MÊME requérant nodeA enfile DEUX entrées distinctes (seq 0 puis 1).
    await model.claim("nodeA", event_id="evt-first", sequence=0, deliver=True)
    await model.claim("nodeA", event_id="evt-second", sequence=1, deliver=True)
    await model.collect_acks("evt-first", holder="nodeA")
    await model.collect_acks("evt-second", holder="nodeA")

    # A obtient la lease sur le head (evt-first). evt-second reste PENDING et
    # devient le head après consommation de evt-first.
    token_first = await model.grant("nodeA", event_id="evt-first")
    assert token_first.state == TokenState.HELD.value
    assert token_first.holder_node_id == "nodeA"
    assert model.is_lease_expired(token_first, cluster.clock.iso()) is False
    term_after_first = cluster.term
    await assert_at_most_one_valid_holder(cluster)

    # evt-second est désormais le head PENDING (evt-first est GRANTED) et est
    # all-ACKé : la garde head/PENDING passerait. Mais A tient ENCORE sa propre
    # lease active -> le second grant est REFUSÉ par l'exclusion mutuelle, qui
    # inclut désormais la lease du requérant lui-même.
    queue = await cluster.nodes["nodeA"].store.list_queue()
    head_pending = next(
        q for q in queue if q.status == QueueEntryStatus.PENDING.value
    )
    assert head_pending.event_id == "evt-second"
    assert await model.can_grant("evt-second", holder="nodeA") is True

    with pytest.raises(RuntimeError, match="lease active"):
        await model.grant("nodeA", event_id="evt-second")

    # Aucun effet de bord : pas de second token, pas de bump de term, evt-second
    # toujours PENDING. assert_at_most_one_valid_holder tient (un seul holder).
    assert cluster.term == term_after_first  # pas de re-bump
    token_now = await cluster.nodes["nodeA"].store.get_token()
    assert token_now is not None
    assert token_now.term == token_first.term
    assert token_now.event_id == "evt-first"
    queue_after = await cluster.nodes["nodeA"].store.list_queue()
    second_after = next(q for q in queue_after if q.event_id == "evt-second")
    assert second_after.status == QueueEntryStatus.PENDING.value
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)

    # A complète son cycle (commit + release) -> token FREE : la lease n'est
    # plus active, A peut enfin se voir accorder evt-second au term suivant.
    await model.apply_commit("nodeA", bank_version=0, commit_id="c-first")
    freed = await model.release("nodeA")
    assert freed.state == TokenState.FREE.value
    token_second = await model.grant("nodeA", event_id="evt-second")
    assert token_second.state == TokenState.HELD.value
    assert token_second.holder_node_id == "nodeA"
    assert token_second.term > token_first.term  # term a monté au nouveau grant
    await assert_at_most_one_valid_holder(cluster)
    await assert_invariants(cluster)


# =============================================================================
# 10. Duplicate commit replay is a no-op (append_commit idempotent)
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_commit_replay_is_noop() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-c")
    await model.collect_acks("evt-c", holder="nodeA")
    await model.grant("nodeA", event_id="evt-c")
    first = await model.apply_commit("nodeA", bank_version=0, commit_id="c0")

    # Replay du MÊME commit_id à la même bank_version : no-op.
    store = cluster.nodes["nodeA"].store
    before = cluster.nodes["nodeA"].storage.snapshot()
    second = await store.append_commit(first)
    assert second.commit_id == first.commit_id
    assert cluster.nodes["nodeA"].storage.objects == before
    assert len(await store.list_commits()) == 1
    await assert_invariants(cluster)


# =============================================================================
# 11. Divergent commit at same bank_version conflicts
# =============================================================================


@pytest.mark.asyncio
async def test_divergent_commit_same_bank_version_conflicts() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-d")
    await model.collect_acks("evt-d", holder="nodeA")
    await model.grant("nodeA", event_id="evt-d")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")

    store = cluster.nodes["nodeA"].store
    with pytest.raises(RuntimeError, match="Commit conflict"):
        await store.append_commit(
            BankCommit(
                bank_version=0,
                term=cluster.term,
                commit_id="DIFFERENT",
                committed_by_node_id="nodeA",
            )
        )
    await assert_invariants(cluster)


# =============================================================================
# 11b. Gapped / mal-parentée commit chain is caught by the invariant
# =============================================================================


@pytest.mark.asyncio
async def test_gapped_commit_chain_violates_bank_version_invariant() -> None:
    """
    RÉGRESSION (Codex P4 finding 3) : la chaîne de commits de bank doit être
    CONTIGUË (``[0, 1, 2, …]``, sans trou). Une chaîne ``[0, 2]`` (trou à 1)
    passait l'ancien check (qui ne vérifiait que le tri croissant) — false
    negative. On injecte directement un historique troué dans un store et on
    vérifie que l'invariant le RÉVÈLE désormais.
    """
    cluster, model = await _three_node_cluster()

    # Cycle légitime : commit contigu bank_version 0 (parent -1) sur nodeA.
    await model.claim("nodeA", event_id="evt-g0")
    await model.collect_acks("evt-g0", holder="nodeA")
    await model.grant("nodeA", event_id="evt-g0")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")

    # L'historique contigu [0] passe l'invariant (contrôle positif).
    await assert_bank_version_monotone(cluster)

    # Injection d'un commit À TROU : bank_version 2 SANS bank_version 1. Le
    # parent référencé (1) n'existe pas -> chaîne non contiguë. On écrit AUSSI
    # le pointeur à 2 (pour passer la garde « pointeur <= dernier commit »), de
    # sorte que SEUL le trou de contiguïté reste à détecter.
    store = cluster.nodes["nodeA"].store
    await store.append_commit(
        BankCommit(
            bank_version=2,
            parent_bank_version=1,  # parent inexistant (trou à 1)
            term=cluster.term,
            commit_id="c2-gapped",
            committed_by_node_id="nodeA",
        )
    )
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=2, commit_id="c2-gapped")
    )

    versions = [c.bank_version for c in await store.list_commits()]
    assert versions == [0, 2]  # tri croissant OK, MAIS trou à 1

    # L'invariant renforcé attrape le trou (l'ancien tri seul l'aurait laissé).
    with pytest.raises(AssertionError, match="NON CONTIGUË"):
        await assert_bank_version_monotone(cluster)
    with pytest.raises(AssertionError, match="NON CONTIGUË"):
        await assert_invariants(cluster)


# =============================================================================
# 11c. Wrong parent_bank_version is caught by the invariant
# =============================================================================


@pytest.mark.asyncio
async def test_wrong_parent_bank_version_violates_invariant() -> None:
    """
    RÉGRESSION (Codex P4 finding 3) : chaque commit doit chaîner sur
    ``parent_bank_version == bank_version - 1``. Une chaîne CONTIGUË mais
    mal-parentée (commit 1 pointant son parent vers -1 au lieu de 0) passait
    l'ancien check. On injecte ce commit et on vérifie que l'invariant échoue.
    """
    cluster, model = await _three_node_cluster()

    # Commit contigu 0 (parent -1), légitime.
    await model.claim("nodeA", event_id="evt-p0")
    await model.collect_acks("evt-p0", holder="nodeA")
    await model.grant("nodeA", event_id="evt-p0")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")
    await assert_bank_version_monotone(cluster)

    # Injection : bank_version 1 CONTIGU mais avec un parent ERRONÉ (-1 au lieu
    # de 0). Versions = [0, 1] (contiguës), donc seul le parent rompu reste.
    store = cluster.nodes["nodeA"].store
    await store.append_commit(
        BankCommit(
            bank_version=1,
            parent_bank_version=-1,  # devrait être 0
            term=cluster.term,
            commit_id="c1-badparent",
            committed_by_node_id="nodeA",
        )
    )
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=1, commit_id="c1-badparent")
    )

    versions = [c.bank_version for c in await store.list_commits()]
    assert versions == [0, 1]  # contiguës, MAIS parent du commit 1 rompu

    with pytest.raises(AssertionError, match="chaînage de parenté rompu"):
        await assert_bank_version_monotone(cluster)
    with pytest.raises(AssertionError, match="chaînage de parenté rompu"):
        await assert_invariants(cluster)


# =============================================================================
# 12. Corrupted critical file surfaces, not silently repaired
# =============================================================================


@pytest.mark.asyncio
async def test_corrupted_manifest_surfaces_not_repaired() -> None:
    cluster, model = await _three_node_cluster()
    await _full_cycle(
        cluster, model, holder="nodeA", event_id="evt-x", bank_version=0, commit_id="c0"
    )

    # Corruption d'un fichier critique (term.json) sur le storage de B.
    storage_b = cluster.nodes["nodeB"].storage
    storage_b.objects[layout.term_key(cluster.space_id)] = "{not valid json"
    raw_corrupt = storage_b.objects[layout.term_key(cluster.space_id)]

    # Le store SIGNALE (CorruptedStateError), il ne répare PAS silencieusement.
    with pytest.raises(CorruptedStateError):
        await cluster.nodes["nodeB"].store.get_term()
    # Le reload complet propage aussi la corruption (statut unsafe).
    with pytest.raises(CorruptedStateError):
        await cluster.nodes["nodeB"].store.load_snapshot()

    # Aucune réparation silencieuse : le fichier corrompu est inchangé.
    assert storage_b.objects[layout.term_key(cluster.space_id)] == raw_corrupt


# =============================================================================
# 13. Stale-term commit rejected BEFORE append_commit
# =============================================================================


@pytest.mark.asyncio
async def test_stale_term_commit_rejected() -> None:
    cluster, model = await _three_node_cluster()
    await model.claim("nodeA", event_id="evt-grant")
    await model.collect_acks("evt-grant", holder="nodeA")
    granted = await model.grant("nodeA", event_id="evt-grant")  # term 2

    # Un term plus récent est persisté ensuite (un autre holder a avancé).
    await cluster.nodes["nodeA"].store.bump_term(7, updated_by_node_id="nodeC")
    cluster.term = 7

    store = cluster.nodes["nodeA"].store
    commits_before = len(await store.list_commits())

    # Un commit portant l'ancien term 2 est rejeté AVANT append_commit.
    with pytest.raises(CommitFencedError):
        await model.apply_commit(
            "nodeA", bank_version=0, commit_id="stale", commit_term=granted.term
        )

    # append_commit n'a jamais été atteint : aucun commit ajouté.
    assert len(await store.list_commits()) == commits_before
    # Le préfixe commits/ ne contient pas le commit stale.
    commit_objs = await store._storage.list_objects(  # type: ignore[attr-defined]
        layout.commit_prefix(cluster.space_id)
    )
    for obj in commit_objs:
        raw = await store._storage.get(obj["Key"])  # type: ignore[attr-defined]
        assert '"commit_id": "stale"' not in (raw or "")
    await assert_no_stale_term_commit(cluster)
    await assert_invariants(cluster)


# =============================================================================
# 14. Delayed live-note replication after tombstone
# =============================================================================


@pytest.mark.asyncio
async def test_delayed_livenote_replication_after_tombstone() -> None:
    cluster, model = await _three_node_cluster()

    # nodeA consomme la note n1 dans un commit (bank_version 0) et la tombe.
    await model.claim("nodeA", event_id="evt-cons")
    await model.collect_acks("evt-cons", holder="nodeA")
    await model.grant("nodeA", event_id="evt-cons")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")
    await model.add_tombstone("nodeA", note_id="n1", bank_version=0)
    await model.release("nodeA")

    # Réplication TARDIVE de la note n1 par un pair en retard : le modèle
    # rejette la résurrection d'un origin_note_id tombé (replication guard #12).
    assert await model.is_tombstoned("nodeA", note_id="n1") is True
    assert await model.replicate_note("nodeA", note_id="n1") is False
    # Une note jamais tombée serait acceptée (contrôle positif).
    assert await model.replicate_note("nodeA", note_id="n-fresh") is True

    # GC croisée : un pair sous le min watermark DOIT garder la tombstone.
    # min watermark across peers = 0 -> aucune tombstone (bank_version 0) GCée.
    deleted = await cluster.nodes["nodeA"].store.garbage_collect_tombstones(
        min_bank_version_across_watermarks=0
    )
    assert deleted == 0
    assert await model.is_tombstoned("nodeA", note_id="n1") is True

    # Quand TOUS les peers ont dépassé bank_version 0 (min = 1), GC autorisée.
    deleted = await cluster.nodes["nodeA"].store.garbage_collect_tombstones(
        min_bank_version_across_watermarks=1
    )
    assert deleted == 1
    await assert_invariants(cluster)


# =============================================================================
# 15. Membership epoch mismatch fails closed
# =============================================================================


@pytest.mark.asyncio
async def test_membership_epoch_mismatch_fails_closed() -> None:
    cluster, model = await _three_node_cluster()

    # Un event signé avec un epoch périmé (0) alors que la membership est à 1.
    event = EventEnvelope(
        event_id="evt-epoch",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=cluster.term,
        membership_epoch=0,  # mismatch
        created_at=cluster.clock.iso(),
    )
    await cluster.send_to("nodeA", "nodeB", event)
    with pytest.raises(PeerChannelError) as err:
        await cluster.transport.deliver_next("nodeB")
    assert err.value.code == PeerErrorCode.WRONG_MEMBERSHIP_EPOCH

    # L'event n'a pas été persisté chez B (fail-closed).
    assert await cluster.nodes["nodeB"].store.get_event("evt-epoch") is None
    await assert_invariants(cluster)


# =============================================================================
# 15b. Network partition is bidirectional (no message crosses either way)
# =============================================================================


@pytest.mark.asyncio
async def test_partition_is_bidirectional() -> None:
    """
    RÉGRESSION (Codex P2 #4) : une partition coupe le trafic dans LES DEUX
    sens. Un nœud isolé ne peut ni recevoir DU cluster, ni livrer AU cluster
    (pas de coupure à sens unique). On vérifie les deux directions.
    """
    cluster, model = await _three_node_cluster()

    # Isole nodeC du reste du cluster.
    cluster.transport.partition({"nodeC"})

    # Sens ENTRANT (cluster -> nodeC) coupé : A ne peut pas envoyer à C.
    inbound = EventEnvelope(
        event_id="evt-to-c",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=cluster.term,
        membership_epoch=cluster.epoch,
        created_at=cluster.clock.iso(),
    )
    with pytest.raises(PeerChannelError) as err_in:
        await cluster.send_to("nodeA", "nodeC", inbound)
    assert err_in.value.code == PeerErrorCode.TRANSPORT_UNAVAILABLE
    assert cluster.transport.pending("nodeC") == []  # rien n'a traversé

    # Sens SORTANT (nodeC -> cluster) coupé : C ne peut pas envoyer à A ni B.
    outbound = EventEnvelope(
        event_id="evt-from-c",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeC",
        term=cluster.term,
        membership_epoch=cluster.epoch,
        created_at=cluster.clock.iso(),
    )
    for dest in ("nodeA", "nodeB"):
        with pytest.raises(PeerChannelError) as err_out:
            await cluster.send_to("nodeC", dest, outbound)
        assert err_out.value.code == PeerErrorCode.TRANSPORT_UNAVAILABLE
        assert cluster.transport.pending(dest) == []  # rien n'a traversé

    # Aucun message n'est en transit nulle part : la frontière est étanche.
    assert cluster.transport.total_pending() == 0

    # Le trafic A <-> B (hors partition) reste possible.
    intra = EventEnvelope(
        event_id="evt-a-to-b",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeA",
        term=cluster.term,
        membership_epoch=cluster.epoch,
        created_at=cluster.clock.iso(),
    )
    await cluster.send_to("nodeA", "nodeB", intra)
    assert [m.event_id for m in cluster.transport.pending("nodeB")] == ["evt-a-to-b"]

    # Après cicatrisation, les deux sens redeviennent ouverts.
    cluster.transport.heal_partition()
    await cluster.send_to("nodeC", "nodeA", outbound)
    assert [m.event_id for m in cluster.transport.pending("nodeA")] == ["evt-from-c"]
    await assert_invariants(cluster)


# =============================================================================
# 15c. Network partition is a COMPONENT split, not isolation of individuals:
#      intra-component traffic flows, only cross-boundary traffic is cut
# =============================================================================


@pytest.mark.asyncio
async def test_partition_isolates_component_not_individuals() -> None:
    """
    RÉGRESSION (Codex round-6 P3) : ``partition(node_set)`` modélise un SPLIT
    de composant, pas l'isolation d'individus. La coupure suit la FRONTIÈRE
    entre ``node_set`` et le reste du cluster : seul le trafic croisant la
    frontière est coupé (dans les deux sens) ; le trafic INTRA-composant (deux
    nœuds du même côté) continue de circuler.

    Cluster {A, B, C}, ``partition({A, B})`` :
    - INTRA-composant {A, B} : A↔B circulent (les deux du même côté) ;
    - CROISANT la frontière : A↔C et B↔C coupés dans les deux sens ;
    - ``heal_partition`` restaure tout.

    Sans la correction (ancienne garde : refuser si l'un OU l'autre endpoint
    est dans ``_partitioned``), A→B serait coupé à tort (les deux dans le set),
    modélisant des individus isolés au lieu d'un composant isolé.
    """
    cluster, _ = await _three_node_cluster()

    # Isole le composant {A, B} du reste (C).
    cluster.transport.partition({"nodeA", "nodeB"})

    def _evt(event_id: str, origin: str) -> EventEnvelope:
        return EventEnvelope(
            event_id=event_id,
            type=EventType.TOKEN_CLAIM,
            origin_node_id=origin,
            term=cluster.term,
            membership_epoch=cluster.epoch,
            created_at=cluster.clock.iso(),
        )

    # INTRA-composant : A->B et B->A circulent (les deux du même côté).
    await cluster.send_to("nodeA", "nodeB", _evt("evt-a-to-b", "nodeA"))
    assert [m.event_id for m in cluster.transport.pending("nodeB")] == ["evt-a-to-b"]
    await cluster.send_to("nodeB", "nodeA", _evt("evt-b-to-a", "nodeB"))
    assert [m.event_id for m in cluster.transport.pending("nodeA")] == ["evt-b-to-a"]

    # CROISANT la frontière : A<->C coupé dans les deux sens.
    for src, dst, eid in (
        ("nodeA", "nodeC", "evt-a-to-c"),
        ("nodeC", "nodeA", "evt-c-to-a"),
        ("nodeB", "nodeC", "evt-b-to-c"),
        ("nodeC", "nodeB", "evt-c-to-b"),
    ):
        with pytest.raises(PeerChannelError) as err:
            await cluster.send_to(src, dst, _evt(eid, src))
        assert err.value.code == PeerErrorCode.TRANSPORT_UNAVAILABLE
        # Rien n'a traversé la frontière (pas de message ajouté à la cible).
        assert all(m.event_id != eid for m in cluster.transport.pending(dst))

    # Aucun message croisant n'a été enfilé : seuls les deux intra-composant
    # subsistent (un dans la boîte de A, un dans celle de B).
    assert cluster.transport.total_pending() == 2

    # Cicatrisation : toutes les directions, y compris croisantes, rouvrent.
    cluster.transport.heal_partition()
    await cluster.send_to("nodeA", "nodeC", _evt("evt-a-to-c-healed", "nodeA"))
    assert "evt-a-to-c-healed" in [
        m.event_id for m in cluster.transport.pending("nodeC")
    ]
    await cluster.send_to("nodeC", "nodeB", _evt("evt-c-to-b-healed", "nodeC"))
    assert "evt-c-to-b-healed" in [
        m.event_id for m in cluster.transport.pending("nodeB")
    ]
    await assert_invariants(cluster)


# =============================================================================
# 16. Peer eviction changes the all-ACK expected set
# =============================================================================


@pytest.mark.asyncio
async def test_peer_eviction_changes_all_ack_set() -> None:
    cluster, model = await _three_node_cluster()

    # Avant éviction : 3 ACK attendus.
    membership_before = await cluster.membership()
    assert cluster.ack_policy.expected_acks(membership_before) == 3

    # Éviction de nodeC : bump epoch + statut EVICTED, sur tous les stores.
    new_epoch = cluster.epoch + 1
    cluster.epoch = new_epoch
    for nid in cluster.node_ids():
        view = await cluster.nodes[nid].store.get_membership()
        assert view is not None
        new_members = [
            m.model_copy(
                update={"status": MemberStatus.EVICTED.value}
            )
            if m.node_id == "nodeC"
            else m
            for m in view.members
        ]
        await cluster.nodes[nid].store.set_membership(
            MembershipView(epoch=new_epoch, members=new_members)
        )

    # Après éviction : 2 ACK attendus (C retiré de l'ensemble all-ACK).
    membership_after = await cluster.membership()
    assert cluster.ack_policy.expected_acks(membership_after) == 2

    # Les pairs restants (A, B) complètent un cycle sans C.
    await model.claim("nodeA", event_id="evt-after-evict")
    received = await model.collect_acks(
        "evt-after-evict", holder="nodeA", ackers=["nodeA", "nodeB"]
    )
    assert received == 2
    assert await model.can_grant("evt-after-evict", holder="nodeA") is True
    await model.grant("nodeA", event_id="evt-after-evict")
    await assert_invariants(cluster, min_epoch=new_epoch)


# =============================================================================
# 16b. all_acked(on_node=holder) valide contre la membership DU HOLDER
# =============================================================================


@pytest.mark.asyncio
async def test_all_acked_uses_holder_membership_when_views_diverge() -> None:
    """
    RÉGRESSION (Codex P4 finding 2) : ``all_acked(on_node=holder)`` doit valider
    les ACK reçus par le holder contre la membership VUE PAR LE HOLDER, pas
    contre la vue d'un autre nœud. En resync / éviction partielle les vues
    divergent intentionnellement ; valider les ACK du holder contre l'ensemble
    actif d'un AUTRE nœud autoriserait ou bloquerait un grant à tort.

    Scénario : le holder ``nodeC`` apprend l'éviction de ``nodeB`` (sa vue
    locale passe à epoch+1, B EVICTED -> actifs = {A, C}). ``nodeA`` (le PREMIER
    nœud trié, donc celui lu par ``cluster.membership()``) reste sur l'ancienne
    vue (A, B, C tous actifs). Avec A et C qui ACKent (mais PAS B), all-ACK est
    satisfait DANS LA VUE DU HOLDER (actifs {A, C}) mais PAS dans la vue de
    nodeA (actifs {A, B, C}). La méthode doit suivre la vue du holder.
    """
    cluster, model = await _three_node_cluster()

    # nodeC (le holder) apprend l'éviction de nodeB : sa membership locale passe
    # à epoch+1 avec B EVICTED. Les autres nœuds NE l'apprennent PAS.
    base_view = await cluster.nodes["nodeC"].store.get_membership()
    assert base_view is not None
    holder_epoch = base_view.epoch + 1
    holder_view = MembershipView(
        epoch=holder_epoch,
        members=[
            m.model_copy(update={"status": MemberStatus.EVICTED.value})
            if m.node_id == "nodeB"
            else m
            for m in base_view.members
        ],
    )
    await cluster.nodes["nodeC"].store.set_membership(holder_view)

    # Les vues DIVERGENT : nodeA (premier nœud trié) voit toujours B actif.
    view_a = await cluster.nodes["nodeA"].store.get_membership()
    assert view_a is not None
    assert cluster.ack_policy.expected_ack_set(view_a) == {"nodeA", "nodeB", "nodeC"}
    assert cluster.ack_policy.expected_ack_set(holder_view) == {"nodeA", "nodeC"}

    # Un claim porté par le holder nodeC ; seuls A et C ACKent (B est évincé de
    # la vue du holder et ne participe plus).
    await model.claim("nodeC", event_id="evt-diverge", deliver=True)
    await model.collect_acks("evt-diverge", holder="nodeC", ackers=["nodeA", "nodeC"])
    holder_acks = {
        a.ack_by_node_id
        for a in await cluster.nodes["nodeC"].store.list_acks("evt-diverge")
    }
    assert holder_acks == {"nodeA", "nodeC"}

    # Validation contre la vue du HOLDER ({A, C}) -> satisfait (le grant peut
    # progresser). Si la méthode validait contre la vue de nodeA ({A, B, C}),
    # elle bloquerait à tort (B manquant).
    assert await cluster.all_acked("evt-diverge", on_node="nodeC") is True
    assert await model.can_grant("evt-diverge", holder="nodeC") is True

    # Contrôle de divergence : valider les MÊMES ACK contre la vue de nodeA (où
    # B est encore actif) DOIT échouer — c'est précisément le faux positif que
    # la correction évite en lisant la vue du holder.
    assert (
        cluster.ack_policy.is_satisfied(received=holder_acks, membership=view_a)
        is False
    )

    # Le grant progresse sur la vue du holder, sans relâcher all-ACK : un actif
    # manquant DANS LA VUE DU HOLDER bloquerait toujours. On le vérifie : si C
    # ne s'auto-ACK plus (vue holder = {A, C}), A seul ne suffit pas.
    assert (
        cluster.ack_policy.is_satisfied(received={"nodeA"}, membership=holder_view)
        is False
    )

    await model.grant("nodeC", event_id="evt-diverge")
    await assert_invariants(cluster, min_epoch=base_view.epoch)


# =============================================================================
# 17. Resync marker on future epoch / missed bank_version
# =============================================================================


@pytest.mark.asyncio
async def test_resync_marker_on_future_epoch_or_missed_bank_version() -> None:
    cluster, model = await _three_node_cluster()

    # Le cluster avance d'un epoch (membership mutée) ; nodeC reste en arrière.
    new_epoch = cluster.epoch + 1
    cluster.epoch = new_epoch
    for nid in ("nodeA", "nodeB"):
        view = await cluster.nodes[nid].store.get_membership()
        assert view is not None
        await cluster.nodes[nid].store.set_membership(
            MembershipView(epoch=new_epoch, members=view.members)
        )
    # nodeC reste à l'epoch précédent -> il doit se marquer resync_required.

    status_c = await model.hive_status("nodeC")
    assert status_c == HiveStatus.RESYNC_REQUIRED

    # Les nœuds à jour sont healthy.
    status_a = await model.hive_status("nodeA")
    assert status_a == HiveStatus.HEALTHY
    await assert_invariants(cluster, min_epoch=cluster.epoch - 1)


@pytest.mark.asyncio
async def test_resync_marker_on_missed_bank_version_same_epoch() -> None:
    """
    RÉGRESSION (Codex P2 #3) : RESYNC_REQUIRED doit aussi se déclencher sur un
    retard de bank_version committé, PAS seulement sur l'epoch. Un nœud à
    l'epoch courant mais dont le pointeur committé est en arrière du cluster
    NE DOIT PAS lire HEALTHY.
    """
    cluster, model = await _three_node_cluster()

    # nodeA exécute un cycle complet et committe bank_version 0 ; son pointeur
    # avance à 0. nodeB/nodeC n'appliquent pas ce commit -> pointeur en retard.
    await model.claim("nodeA", event_id="evt-bv")
    await model.collect_acks("evt-bv", holder="nodeA")
    await model.grant("nodeA", event_id="evt-bv")
    await model.apply_commit("nodeA", bank_version=0, commit_id="c0")
    await model.release("nodeA")

    # Tous les nœuds sont au MÊME epoch (aucune mutation de membership).
    for nid in cluster.node_ids():
        view = await cluster.nodes[nid].store.get_membership()
        assert view is not None and view.epoch == cluster.epoch

    # Le cluster a committé bank_version 0 (porté par nodeA).
    assert await model.cluster_committed_bank_version() == 0

    # nodeA est à jour -> HEALTHY ; nodeB/nodeC ont un pointeur en retard
    # (aucun, donc -1 < 0) malgré un epoch courant -> RESYNC_REQUIRED.
    assert await model.hive_status("nodeA") == HiveStatus.HEALTHY
    for lagging in ("nodeB", "nodeC"):
        pointer = await cluster.nodes[lagging].store.get_bank_version_pointer()
        assert pointer is None or pointer.bank_version < 0
        assert await model.hive_status(lagging) == HiveStatus.RESYNC_REQUIRED

    # Une fois nodeB rattrapé (term + commit + pointeur à 0), il redevient
    # HEALTHY. On propage d'abord le term du commit (modélise le TERM_BUMPED
    # appris au rattrapage), sinon le commit term=2 dépasserait le term local.
    nodeA_commit = await cluster.nodes["nodeA"].store.get_commit(0)
    assert nodeA_commit is not None
    await cluster.nodes["nodeB"].store.bump_term(
        nodeA_commit.term, updated_by_node_id="nodeA"
    )
    await cluster.nodes["nodeB"].store.append_commit(nodeA_commit)
    from live_mem.core.hivemind import BankVersionPointer

    await cluster.nodes["nodeB"].store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id="c0")
    )
    assert await model.hive_status("nodeB") == HiveStatus.HEALTHY
    await assert_invariants(cluster)
