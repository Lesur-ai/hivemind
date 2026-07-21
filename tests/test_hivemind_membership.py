# -*- coding: utf-8 -*-
"""
Tests pour issue #5 — membership (add/evict/epoch) et resync node-local.

Couvre :
- add_member bumpe l'epoch de +1, membre ACTIVE, event PEER_JOINED ;
- evict_member exige une confirmation opérateur explicite ;
- un peer évincé sort des exigences all-ACK (active_members) ;
- un bump d'epoch invalide les anciens messages protocole (peer._verify) ;
- l'observation d'un epoch futur / d'une bank_version manquée passe le node
  en RESYNC_REQUIRED, et mark_resync_complete ne flip HEALTHY qu'au rattrapage.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live_mem.core.hivemind import (
    BankVersionPointer,
    BootstrapError,
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    HivemindPeerChannel,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipService,
    MembershipView,
    NodeIdentity,
    PeerChannelError,
    PeerErrorCode,
    ResyncService,
    active_members,
    expected_ack_node_ids,
    generate_peer_keypair,
)
from tests.test_hivemind_state import FakeStorage


NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
async def seeded(storage: FakeStorage):
    """Un space Hivemind initialisé avec un seul membre ACTIVE à epoch 1."""
    keys_a = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeA", display_name="A", public_key=keys_a.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", display_name="A", public_key=keys_a.public_key)
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id="nodeA")
    return store, keys_a


# =============================================================================
# Add member
# =============================================================================


async def test_add_member_bumps_epoch_monotonically(seeded) -> None:
    store, _keys_a = seeded
    keys_b = generate_peer_keypair()
    service = MembershipService(store)

    view = await service.add_member(
        Member(node_id="nodeB", display_name="B", public_key=keys_b.public_key)
    )

    assert view.epoch == 2
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.ACTIVE.value

    events = await store.list_events()
    joined = [e for e in events if e.type == EventType.PEER_JOINED.value]
    assert len(joined) == 1
    assert joined[0].payload["node_id"] == "nodeB"
    assert joined[0].membership_epoch == 2


async def test_add_member_refuses_duplicate_active(seeded) -> None:
    store, keys_a = seeded
    service = MembershipService(store)
    with pytest.raises(BootstrapError):
        await service.add_member(
            Member(node_id="nodeA", public_key=keys_a.public_key)
        )


async def test_add_member_requires_public_key(seeded) -> None:
    store, _keys_a = seeded
    service = MembershipService(store)
    with pytest.raises(BootstrapError):
        await service.add_member(Member(node_id="nodeB"))


# =============================================================================
# Update member scopes (ADR-0016) — validation AVANT tout write
# =============================================================================


async def test_update_member_scopes_rejects_invalid_scope_before_any_write(
    seeded, storage
) -> None:
    """RED-without-fix : ``update_member_scopes`` avec un scope hors vocabulaire
    fermé (``["admin"]``) lève AVANT tout write, et members.json reste
    octet-pour-octet inchangé.

    Avec le bug, ``model_copy(update={"scopes": ["admin"]})`` n'exécutait PAS le
    validator (pydantic ne valide pas les ``update``) : un Member invalide était
    construit, un event MEMBERSHIP_UPDATED appendé et members.json corrompu
    écrit — une corruption d'état critique créée par CE helper. Le fix valide via
    ``Member.__pydantic_validator__.validate_assignment`` avant ``_append_event``.
    """
    from live_mem.core.hivemind import layout

    store, _keys_a = seeded
    keys_b = generate_peer_keypair()
    service = MembershipService(store)
    # nodeB ACTIVE re-scopable (epoch 1 -> 2) avec un jeu narrowé valide.
    await service.add_member(
        Member(node_id="nodeB", public_key=keys_b.public_key, scopes=["read"])
    )

    members_key = layout.members_key("alpha")
    members_before = await storage.get(members_key)
    epoch_before = (await store.get_membership()).epoch
    events_before = len(await store.list_events())

    # Scope hors {read,propose,commit} -> rejet AVANT tout write.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await service.update_member_scopes("nodeB", ["admin"])

    # Aucun write : members.json byte-for-byte inchangé, epoch figé, aucun
    # nouvel event MEMBERSHIP_UPDATED.
    assert await storage.get(members_key) == members_before
    assert (await store.get_membership()).epoch == epoch_before
    assert len(await store.list_events()) == events_before
    membership_updated = [
        e
        for e in await store.list_events()
        if e.type == EventType.MEMBERSHIP_UPDATED.value
    ]
    assert membership_updated == []
    # Le membre conserve EXACTEMENT son ancien scope (aucune mutation partielle).
    member_b = next(
        m for m in (await store.get_membership()).members if m.node_id == "nodeB"
    )
    assert member_b.effective_scopes() == frozenset({"read"})


async def test_update_member_scopes_valid_narrow_applies_and_bumps_epoch(
    seeded,
) -> None:
    """GREEN-with-fix complémentaire : un re-scoping VALIDE passe la validation,
    applique le nouveau jeu, bumpe l'epoch et émet MEMBERSHIP_UPDATED."""
    store, _keys_a = seeded
    keys_b = generate_peer_keypair()
    service = MembershipService(store)
    await service.add_member(
        Member(
            node_id="nodeB",
            public_key=keys_b.public_key,
            scopes=["read", "propose", "commit"],
        )
    )
    epoch_before = (await store.get_membership()).epoch

    view = await service.update_member_scopes("nodeB", ["read", "propose"])

    assert view.epoch == epoch_before + 1
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.effective_scopes() == frozenset({"read", "propose"})
    updated = [
        e
        for e in await store.list_events()
        if e.type == EventType.MEMBERSHIP_UPDATED.value
        and e.payload.get("rescoped") is True
    ]
    assert any(e.payload["node_id"] == "nodeB" for e in updated)


# =============================================================================
# Evict member
# =============================================================================


async def test_evict_requires_operator_confirmation(seeded) -> None:
    store, _keys_a = seeded
    keys_b = generate_peer_keypair()
    service = MembershipService(store)
    await service.add_member(Member(node_id="nodeB", public_key=keys_b.public_key))

    # Sans confirmation -> refus, epoch inchangé.
    with pytest.raises(BootstrapError):
        await service.evict_member("nodeB", operator="ops", confirm=False)
    assert (await store.get_membership()).epoch == 2

    # Sans opérateur -> refus.
    with pytest.raises(BootstrapError):
        await service.evict_member("nodeB", operator="", confirm=True)

    # Avec confirmation -> EVICTED, epoch +1, event PEER_EVICTED.
    view = await service.evict_member(
        "nodeB", operator="ops", confirm=True, reason="down"
    )
    assert view.epoch == 3
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.EVICTED.value

    events = await store.list_events()
    evicted = [e for e in events if e.type == EventType.PEER_EVICTED.value]
    assert len(evicted) == 1
    assert evicted[0].payload["operator"] == "ops"
    assert evicted[0].payload["reason"] == "down"
    assert evicted[0].payload["confirmed"] is True


async def test_evicted_peer_dropped_from_ack_expectations(seeded) -> None:
    store, _keys_a = seeded
    keys_b = generate_peer_keypair()
    service = MembershipService(store)
    await service.add_member(Member(node_id="nodeB", public_key=keys_b.public_key))

    before = expected_ack_node_ids(await store.get_membership())
    assert set(before) == {"nodeA", "nodeB"}

    await service.evict_member("nodeB", operator="ops", confirm=True)

    membership = await store.get_membership()
    assert expected_ack_node_ids(membership) == ["nodeA"]
    assert [m.node_id for m in active_members(membership)] == ["nodeA"]
    # Le record EVICTED survit pour l'audit.
    assert any(m.node_id == "nodeB" for m in membership.members)


async def test_evict_unknown_member_refused(seeded) -> None:
    store, _keys_a = seeded
    service = MembershipService(store)
    with pytest.raises(BootstrapError):
        await service.evict_member("ghost", operator="ops", confirm=True)


# =============================================================================
# Epoch change invalide les anciens messages protocole (via peer channel)
# =============================================================================


async def test_epoch_change_invalidates_old_protocol_messages(
    storage: FakeStorage,
) -> None:
    keys_a = generate_peer_keypair()
    keys_b = generate_peer_keypair()

    receiver = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    await receiver.set_node_identity(
        NodeIdentity(node_id="nodeA", public_key=keys_a.public_key)
    )
    await receiver.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id="nodeA", public_key=keys_a.public_key),
                Member(node_id="nodeB", public_key=keys_b.public_key),
            ],
        )
    )
    await receiver.bump_term(2, updated_by_node_id="nodeA")

    # nodeB signe un event à l'epoch 1 (epoch courant).
    signer_store = HivemindStateStore(storage=FakeStorage(), space_id="alpha")  # type: ignore[arg-type]
    signer = HivemindPeerChannel(
        state=signer_store,
        local_node_id="nodeB",
        private_key=keys_b.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-epoch1",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeB",
        term=2,
        membership_epoch=1,
        created_at=NOW_ISO,
    )
    message = await signer.sign_event(event, signed_at=NOW_ISO)

    # L'opérateur ajoute nodeC -> epoch bump à 2.
    keys_c = generate_peer_keypair()
    await MembershipService(receiver).add_member(
        Member(node_id="nodeC", public_key=keys_c.public_key)
    )
    assert (await receiver.get_membership()).epoch == 2

    # Le message signé à l'epoch 1 est désormais fencé.
    channel = HivemindPeerChannel(
        state=receiver,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    with pytest.raises(PeerChannelError) as exc:
        await channel.receive(message)
    assert exc.value.code == PeerErrorCode.WRONG_MEMBERSHIP_EPOCH


# =============================================================================
# Resync : epoch futur / bank_version manquée
# =============================================================================


async def test_future_epoch_sets_resync_required(seeded) -> None:
    store, _keys_a = seeded  # local epoch = 1
    resync = ResyncService(store)

    health = await resync.observe_remote(observed_epoch=5)
    assert HiveNodeStatus(health.status) == HiveNodeStatus.RESYNC_REQUIRED
    assert health.observed_epoch == 5

    events = await store.list_events()
    assert any(e.type == EventType.RESYNC_REQUIRED.value for e in events)

    # mark_resync_complete refuse tant que l'epoch local n'a pas rattrapé.
    with pytest.raises(BootstrapError):
        await resync.mark_resync_complete()

    # On simule l'APPLICATION d'un resync (snapshot/commit-range) qui rattrape
    # l'epoch local à 5 — c'est ce que fait un vrai resync. PAS add_member, qui
    # est désormais refusé tant que node_status n'est pas sain (un node en
    # RESYNC_REQUIRED ne doit pas muter la membership partagée).
    view = await store.get_membership()
    await store.set_membership(MembershipView(epoch=5, members=view.members))
    assert (await store.get_membership()).epoch == 5

    healed = await resync.mark_resync_complete()
    assert HiveNodeStatus(healed.status) == HiveNodeStatus.HEALTHY
    assert any(
        e.type == EventType.RESYNC_COMPLETED.value for e in await store.list_events()
    )


async def test_missed_bank_version_sets_resync_required(seeded) -> None:
    store, _keys_a = seeded
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=2, commit_id="c2")
    )
    resync = ResyncService(store)

    health = await resync.observe_remote(observed_bank_version=7)
    assert HiveNodeStatus(health.status) == HiveNodeStatus.RESYNC_REQUIRED
    assert health.observed_bank_version == 7


async def test_observe_remote_noop_when_not_ahead(seeded) -> None:
    store, _keys_a = seeded  # epoch 1, no bank pointer (-1)
    resync = ResyncService(store)
    health = await resync.observe_remote(observed_epoch=1, observed_bank_version=-1)
    # Pas en avance -> pas de RESYNC_REQUIRED ; aucun fichier de santé écrit.
    assert HiveNodeStatus(health.status) != HiveNodeStatus.RESYNC_REQUIRED
    assert await store.get_node_status() is None


async def test_resync_target_is_monotone(seeded) -> None:
    """[re-review P2] La cible de resync ne descend jamais : observer 7 puis 5
    garde la cible à 7, sinon mark_resync_complete repasserait HEALTHY avant
    d'avoir rattrapé la plus haute version déjà observée."""
    store, _ = seeded  # pointeur bank local absent (-1)
    resync = ResyncService(store)

    first = await resync.observe_remote(observed_bank_version=7)
    assert first.observed_bank_version == 7

    # Observation ultérieure plus basse (toujours > local -1) : la cible NE
    # descend PAS à 5.
    lower = await resync.observe_remote(observed_bank_version=5)
    assert HiveNodeStatus(lower.status) == HiveNodeStatus.RESYNC_REQUIRED
    assert lower.observed_bank_version == 7

    # Rattraper jusqu'à 5 ne suffit pas : la cible reste 7.
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=5, commit_id="c5")
    )
    with pytest.raises(BootstrapError):
        await resync.mark_resync_complete()

    # Rattraper jusqu'à 7 débloque.
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=7, commit_id="c7")
    )
    healed = await resync.mark_resync_complete()
    assert HiveNodeStatus(healed.status) == HiveNodeStatus.HEALTHY


async def test_load_snapshot_includes_node_status(seeded) -> None:
    """[re-review P2] node_status figure dans la vue de cold-start : un restart
    après import échoué / resync ne doit pas rater UNSAFE/RESYNC_REQUIRED."""
    from live_mem.core.hivemind import NodeHealth

    store, _ = seeded
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="import partiel")
    )
    snap = await store.load_snapshot()
    assert snap.node_status is not None
    assert HiveNodeStatus(snap.node_status.status) == HiveNodeStatus.UNSAFE


async def test_observe_remote_does_not_downgrade_unsafe(seeded) -> None:
    """[re-review P2] UNSAFE (fail-closed) n'est jamais downgradé en
    RESYNC_REQUIRED par une observation distante : un import partiel / une
    corruption ne doit pas pouvoir redevenir HEALTHY sans réparation explicite."""
    from live_mem.core.hivemind import NodeHealth

    store, _ = seeded
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="import partiel")
    )
    resync = ResyncService(store)

    health = await resync.observe_remote(observed_epoch=99, observed_bank_version=99)
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE
    persisted = await store.get_node_status()
    assert persisted is not None
    assert HiveNodeStatus(persisted.status) == HiveNodeStatus.UNSAFE


async def test_evict_refuses_last_active_member(seeded) -> None:
    """[re-review P2] Évincer le dernier membre ACTIVE est refusé : une
    membership à zéro actif ferait paraître le space non-Hivemind et
    bypasserait le chemin de sûreté partagé (teardown explicite hors V1)."""
    store, _ = seeded  # un seul membre ACTIVE : nodeA
    svc = MembershipService(store)
    with pytest.raises(BootstrapError):
        await svc.evict_member("nodeA", operator="ops", confirm=True, reason="last")
    # Membership inchangée : nodeA reste l'unique membre ACTIVE.
    view = await store.get_membership()
    assert [m.node_id for m in active_members(view)] == ["nodeA"]


async def test_add_member_rejects_duplicate_public_key(seeded) -> None:
    """[re-review P2] Deux membres ACTIVE ne peuvent partager une public_key :
    une même clé authentifierait plusieurs nodes et rendrait ambigu le lookup
    d'identité par clé publique au bootstrap."""
    store, keys_a = seeded
    svc = MembershipService(store)
    with pytest.raises(BootstrapError):
        await svc.add_member(
            Member(node_id="nodeDup", public_key=keys_a.public_key)
        )


async def test_add_member_rejects_malformed_public_key(seeded) -> None:
    """[re-review P2] add_member rejette une public_key non-vide mais qui ne
    parse pas en Ed25519 : sinon le membre ACTIVE serait compté dans les ACK
    attendus alors que le peer channel échouerait INVALID_KEY (cluster bloqué)."""
    store, _ = seeded
    svc = MembershipService(store)
    with pytest.raises(BootstrapError):
        await svc.add_member(
            Member(node_id="bad", public_key="!!!pas-une-cle-ed25519!!!")
        )


async def test_add_member_rejects_empty_node_id(seeded) -> None:
    """[re-review P2] add_member rejette un node_id vide (que le modèle Member
    accepte) : sinon un membre ACTIVE inutilisable et une attente d'ACK pour ''
    bloqueraient les flux all-ACK."""
    store, _ = seeded
    svc = MembershipService(store)
    kp = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await svc.add_member(Member(node_id="", public_key=kp.public_key))


async def test_add_member_recoverable_when_set_membership_fails(seeded) -> None:
    """[re-review P2] add_member écrit l'event AVANT la membership avec un
    event_id DÉTERMINISTE : si set_membership échoue (S3 transitoire), un retry
    recalcule le même event (dédupliqué) et applique la membership — l'event ne
    manque jamais alors que la membership est commitée, et il n'est pas
    dupliqué."""
    store, _ = seeded
    svc = MembershipService(store)
    kp = generate_peer_keypair()

    original = store.set_membership
    calls = {"n": 0}

    async def flaky(view):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("S3 transitoire")
        return await original(view)

    store.set_membership = flaky  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await svc.add_member(Member(node_id="nodeB", public_key=kp.public_key))

    # L'event PEER_JOINED a été écrit AVANT le set_membership échoué.
    joined = [
        e for e in await store.list_events() if e.type == EventType.PEER_JOINED.value
    ]
    assert len(joined) == 1

    # Retry : réussit, et NE duplique PAS l'event (event_id déterministe).
    await svc.add_member(Member(node_id="nodeB", public_key=kp.public_key))
    store.set_membership = original  # type: ignore[assignment]

    joined_after = [
        e for e in await store.list_events() if e.type == EventType.PEER_JOINED.value
    ]
    assert len(joined_after) == 1  # dédup : un seul event malgré le retry
    view = await store.get_membership()
    assert any(
        m.node_id == "nodeB" and m.status == MemberStatus.ACTIVE.value
        for m in view.members
    )
    assert view.epoch == 2


async def test_membership_mutation_refused_when_node_unsafe(seeded) -> None:
    """[re-review P2] Un node UNSAFE/RESYNC_REQUIRED ne peut pas muter la
    membership (il écraserait la composition partagée depuis une vue locale
    stale) — un resync est requis d'abord."""
    from live_mem.core.hivemind import NodeHealth

    store, _ = seeded
    svc = MembershipService(store)
    kp = generate_peer_keypair()
    for bad in (HiveNodeStatus.UNSAFE, HiveNodeStatus.RESYNC_REQUIRED):
        await store.set_node_status(NodeHealth(status=bad, reason="x"))
        with pytest.raises(BootstrapError):
            await svc.add_member(Member(node_id="nodeZ", public_key=kp.public_key))
        with pytest.raises(BootstrapError):
            await svc.evict_member("nodeA", operator="ops", confirm=True)


async def test_membership_mutation_refused_when_context_incomplete(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Muter la membership exige un contexte COMPLET : sans
    node.json (état partiel, restore partiel), c'est refusé même sans marqueur
    UNSAFE — sinon un node corrompu écraserait la composition partagée."""
    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="partialmut")  # type: ignore[arg-type]
    # Membership avec un membre ACTIVE mais AUCUN node.json.
    await store.set_membership(
        MembershipView(
            epoch=1, members=[Member(node_id="nodeA", public_key=keys.public_key)]
        )
    )
    svc = MembershipService(store)
    kp = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await svc.add_member(Member(node_id="nodeB", public_key=kp.public_key))


async def test_evict_inactive_member_is_idempotent(seeded) -> None:
    """[re-review P2] Ré-évincer un node déjà EVICTED est un no-op : pas de bump
    d'epoch ni de nouvel event (sinon un retry opérateur fencerait des messages
    valides en vol et forcerait un resync sans changement de composition)."""
    store, _ = seeded
    svc = MembershipService(store)
    kb = generate_peer_keypair()
    await svc.add_member(Member(node_id="nodeB", public_key=kb.public_key))  # epoch 2
    await svc.evict_member("nodeB", operator="ops", confirm=True, reason="down")  # epoch 3

    epoch_after_first = (await store.get_membership()).epoch
    evicted_events_before = len(
        [e for e in await store.list_events() if e.type == EventType.PEER_EVICTED.value]
    )

    # Ré-éviction du même node déjà EVICTED : no-op.
    await svc.evict_member("nodeB", operator="ops", confirm=True, reason="retry")
    assert (await store.get_membership()).epoch == epoch_after_first
    evicted_events_after = len(
        [e for e in await store.list_events() if e.type == EventType.PEER_EVICTED.value]
    )
    assert evicted_events_after == evicted_events_before


async def test_concurrent_add_members_no_lost_update(seeded) -> None:
    """[re-review P2] Deux add_member concurrents sont sérialisés par le verrou
    par-space : aucun n'écrase l'autre (set_membership est un RMW sans CAS qui
    n'accepte que les epochs supérieurs). Les deux joins sont appliqués et
    l'epoch progresse de +2."""
    import asyncio

    store, _ = seeded  # epoch 1, nodeA actif
    svc = MembershipService(store)
    kb = generate_peer_keypair()
    kc = generate_peer_keypair()

    await asyncio.gather(
        svc.add_member(Member(node_id="nodeB", public_key=kb.public_key)),
        svc.add_member(Member(node_id="nodeC", public_key=kc.public_key)),
    )

    view = await store.get_membership()
    assert {m.node_id for m in active_members(view)} == {"nodeA", "nodeB", "nodeC"}
    assert view.epoch == 3  # 1 + 2 : aucun lost-update
