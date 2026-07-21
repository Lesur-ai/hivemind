# -*- coding: utf-8 -*-
"""
Tests pour P5-9 (issue #103, ADR-0016) — enrôlement repo-driven + droits scopés.

Couvre :
- ``Member.scopes`` : défaut full (None), sérialisation octet-pour-octet
  (omit-when-None), liste triée déterministe, round-trip, vocabulaire fermé ;
- ``EnrollmentService.reconcile`` : enrôle un peer vierge à epoch+1, échoue
  fermé sur manifest absent/non-signé/mal-formé/space-mismatch, re-scope un peer
  actif (bump epoch), idempotence (pas de bump), révocation (bump + fail-closed
  des events ultérieurs), mono-tenant (aucun objet tenant, tenancy refusée) ;
- ``peer_scope_guard`` : read-only dénié propose/commit, commit-scope =
  précondition (n'autorise rien), tenancy non reconnue refusée ;
- garde AST : ``enrollment.py`` n'importe ni graph ni long ni mcp.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from live_mem.core.hivemind import (
    EnrollmentError,
    EnrollmentManifest,
    EnrollmentPeer,
    EnrollmentService,
    EnrollmentState,
    EventEnvelope,
    EventType,
    FULL_PEER_SCOPES,
    HivemindPeerChannel,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipService,
    MembershipView,
    NodeIdentity,
    PeerChannelError,
    PeerErrorCode,
    PeerReceiveStatus,
    PeerScope,
    SignedPeerEvent,
    generate_peer_keypair,
    peer_scope_guard,
)
from live_mem.core.hivemind import enrollment as enrollment_mod
from live_mem.core.hivemind.peer import (
    PEER_KEY_PREFIX,
    _b64encode,
    _canonical_json_bytes,
    _load_private_key,
)
from tests.test_hivemind_state import FakeStorage


NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
async def seeded(storage: FakeStorage):
    """Space Hivemind sain : un membre ACTIVE (nodeA) à epoch 1, term 2.

    Retourne (store, keys_a, enroller_keys, trusted) — l'enrôleur a sa propre
    paire de clés, distincte de l'identité du node (peer keys != enroller keys).
    """
    keys_a = generate_peer_keypair()
    enroller = generate_peer_keypair()
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
    return store, keys_a, enroller, frozenset({enroller.public_key})


def _sign_manifest(manifest: EnrollmentManifest, enroller_keypair) -> EnrollmentManifest:
    """Signe le manifest avec la clé privée de l'enrôleur (canonical headers+peers)."""
    payload = _canonical_json_bytes(
        {
            "protocol_version": manifest.protocol_version,
            "space_id": manifest.space_id,
            "peers": [p.model_dump(mode="json") for p in manifest.peers],
        }
    )
    sig = _load_private_key(enroller_keypair.private_key).sign(payload)
    return manifest.model_copy(update={"signature": _b64encode(sig)})


def _signed_raw(manifest: EnrollmentManifest, enroller_keypair) -> str:
    """Manifest signé sérialisé en JSON (l'entrée brute de reconcile)."""
    return _sign_manifest(manifest, enroller_keypair).model_dump_json()


# =============================================================================
# 1-6 — Member.scopes : sérialisation byte-for-byte + vocabulaire fermé
# =============================================================================


def test_member_defaults_to_full_scopes() -> None:
    kp = generate_peer_keypair()
    m = Member(node_id="n", public_key=kp.public_key)
    assert m.scopes is None
    assert m.effective_scopes() == FULL_PEER_SCOPES


def test_legacy_members_json_roundtrips_byte_for_byte() -> None:
    legacy = {
        "node_id": "nodeA",
        "display_name": "A",
        "endpoint": "",
        "public_key": "ed25519:abc",
        "joined_at": "2026-01-01T00:00:00+00:00",
        "status": "active",
    }
    m = Member.model_validate(legacy)
    assert m.model_dump(mode="json") == legacy
    # Le golden path bootstrap utilise model_dump_json() puis sha256(...) :
    # aucune clé scopes / null ne doit apparaître.
    dumped = m.model_dump_json()
    assert "scopes" not in dumped
    assert "null" not in dumped


def test_full_scope_member_omits_scopes_key() -> None:
    kp = generate_peer_keypair()
    m = Member(node_id="n", public_key=kp.public_key)
    assert "scopes" not in m.model_dump(mode="json")


def test_narrowed_member_serializes_sorted_list() -> None:
    kp = generate_peer_keypair()
    # Entrée en ordre non trié / non canonique → sortie triée déterministe.
    m = Member(node_id="n", public_key=kp.public_key, scopes=["commit", "read"])
    assert m.model_dump(mode="json")["scopes"] == ["commit", "read"]
    m2 = Member(node_id="n", public_key=kp.public_key, scopes=["read", "commit"])
    assert m2.model_dump(mode="json")["scopes"] == ["commit", "read"]


def test_narrowed_scopes_roundtrip() -> None:
    kp = generate_peer_keypair()
    m = Member(node_id="n", public_key=kp.public_key, scopes=["read", "propose"])
    d = m.model_dump(mode="json")
    again = Member.model_validate(d)
    assert again.scopes == ["propose", "read"]
    assert again.effective_scopes() == frozenset({"read", "propose"})


async def test_unknown_scope_rejected(storage: FakeStorage) -> None:
    from pydantic import ValidationError
    from live_mem.core.hivemind import CorruptedStateError

    kp = generate_peer_keypair()
    with pytest.raises(ValidationError):
        Member(node_id="n", public_key=kp.public_key, scopes=["admin"])

    # Un members.json corrompu (scope hors vocabulaire) remonte CorruptedStateError
    # via le store (fail-closed, jamais lu comme sain).
    store = HivemindStateStore(storage=storage, space_id="corrupt")  # type: ignore[arg-type]
    from live_mem.core.hivemind import layout

    bad = {
        "protocol_version": 1,
        "epoch": 1,
        "updated_at": NOW_ISO,
        "members": [
            {
                "node_id": "nodeA",
                "display_name": "",
                "endpoint": "",
                "public_key": kp.public_key,
                "joined_at": NOW_ISO,
                "status": "active",
                "scopes": ["admin"],
            }
        ],
    }
    await storage.put(layout.members_key("corrupt"), json.dumps(bad))
    with pytest.raises(CorruptedStateError):
        await store.get_membership()


# =============================================================================
# 7-8 — Enrôlement d'un peer vierge à epoch+1
# =============================================================================


async def test_manifest_enrolls_virgin_peer_at_epoch_plus_one(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )

    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                scopes=["read", "propose"],
            )
        ],
    )
    result = await svc.reconcile(_signed_raw(manifest, enroller))

    assert result.applied is True
    assert result.epoch_before == 1
    assert result.epoch_after == 2
    assert result.joined == ("nodeB",)

    view = await store.get_membership()
    assert view.epoch == 2
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.ACTIVE.value
    assert member_b.effective_scopes() == frozenset({"read", "propose"})

    joined = [
        e for e in await store.list_events() if e.type == EventType.PEER_JOINED.value
    ]
    assert any(e.payload["node_id"] == "nodeB" for e in joined)


async def test_full_scope_grant_serializes_like_legacy(seeded, storage) -> None:
    """Un grant des 3 scopes → Member.scopes is None → members.json
    octet-identique à un membre full legacy (via _narrow)."""
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                scopes=["read", "propose", "commit"],
            )
        ],
    )
    await svc.reconcile(_signed_raw(manifest, enroller))

    view = await store.get_membership()
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.scopes is None  # full grant normalisé en None
    assert "scopes" not in member_b.model_dump(mode="json")


# =============================================================================
# 9-13 — Fail-closed (les 5 portes), aucune ouverture d'enrôlement
# =============================================================================


async def _assert_no_change(store: HivemindStateStore, before: MembershipView) -> None:
    after = await store.get_membership()
    assert after.epoch == before.epoch
    assert {m.node_id for m in after.members} == {m.node_id for m in before.members}


async def test_reconcile_missing_manifest_fails_closed(seeded) -> None:
    store, _keys_a, _enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    for raw in ("", b""):
        with pytest.raises(EnrollmentError):
            await svc.reconcile(raw)
    await _assert_no_change(store, before)


async def test_reconcile_unsigned_or_untrusted_fails_closed(seeded) -> None:
    store, _keys_a, enroller, _trusted = seeded
    keys_b = generate_peer_keypair()
    # Enrôleur NON dans la racine de confiance.
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=frozenset()
    )
    before = await store.get_membership()
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))
    await _assert_no_change(store, before)


async def test_reconcile_bad_signature_fails_closed(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)],
    )
    signed = _sign_manifest(manifest, enroller)
    # Altère les peers APRÈS signature → signature périmée.
    keys_c = generate_peer_keypair()
    tampered = signed.model_copy(
        update={
            "peers": [
                EnrollmentPeer(node_id="nodeEVIL", public_key=keys_c.public_key)
            ]
        }
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(tampered.model_dump_json())
    await _assert_no_change(store, before)


async def test_reconcile_malformed_or_schema_invalid_fails_closed(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()

    # (a) JSON cassé.
    with pytest.raises(EnrollmentError):
        await svc.reconcile("{not json")

    # (b) Clé extra inconnue (extra=forbid).
    with pytest.raises(EnrollmentError):
        await svc.reconcile(json.dumps({"space_id": "alpha", "bogus": 1}))

    # (c) public_key non-Ed25519.
    bad_key = json.dumps(
        {
            "space_id": "alpha",
            "enroller_public_key": enroller.public_key,
            "peers": [{"node_id": "nodeB", "public_key": "!!!pas-une-cle!!!"}],
        }
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(bad_key)

    # (d) node_id dupliqué.
    kb = generate_peer_keypair()
    kc = generate_peer_keypair()
    dup = json.dumps(
        {
            "space_id": "alpha",
            "enroller_public_key": enroller.public_key,
            "peers": [
                {"node_id": "dup", "public_key": kb.public_key},
                {"node_id": "dup", "public_key": kc.public_key},
            ],
        }
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(dup)

    await _assert_no_change(store, before)


async def test_reconcile_space_mismatch_denied(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    manifest = EnrollmentManifest(
        space_id="autre-space",  # != store.space_id "alpha"
        enroller_public_key=enroller.public_key,
        peers=[EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))
    await _assert_no_change(store, before)


# =============================================================================
# 14-15 — Re-scope (bump epoch) + idempotence (pas de bump)
# =============================================================================


async def test_rescope_active_peer_bumps_epoch(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    # Enrôle nodeB full (epoch 1 -> 2).
    m1 = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                scopes=["read", "propose", "commit"],
            )
        ],
    )
    await svc.reconcile(_signed_raw(m1, enroller))
    assert (await store.get_membership()).epoch == 2

    # Narrow nodeB à read-only (epoch 2 -> 3).
    m2 = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=["read"]
            )
        ],
    )
    result = await svc.reconcile(_signed_raw(m2, enroller))
    assert result.rescoped == ("nodeB",)
    view = await store.get_membership()
    assert view.epoch == 3
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.effective_scopes() == frozenset({"read"})

    # Un re-scoping émet l'événement de membership GÉNÉRIQUE MEMBERSHIP_UPDATED
    # (ADR-0016), PAS PEER_JOINED (join) ni PEER_EVICTED (revoke).
    rescoped_events = [
        e
        for e in await store.list_events()
        if e.type == EventType.MEMBERSHIP_UPDATED.value
        and e.payload.get("rescoped") is True
    ]
    assert any(e.payload["node_id"] == "nodeB" for e in rescoped_events)
    # et AUCUN PEER_JOINED rescopé (le re-scoping n'est pas un join).
    assert not [
        e
        for e in await store.list_events()
        if e.type == EventType.PEER_JOINED.value
        and e.payload.get("rescoped") is True
    ]


# =============================================================================
# 15bis — Plancher ``read`` : un ACTIVE sans 'read' bloquerait l'all-ACK
# =============================================================================


@pytest.mark.parametrize("bad_scopes", [[], ["propose"], ["commit"]])
async def test_enroll_active_without_read_floor_fails_closed(
    seeded, bad_scopes
) -> None:
    """Enrôler un peer ACTIVE sans 'read' → EnrollmentError, aucune mutation.

    Un ACTIVE est un ACKer attendu de l'all-ACK, mais ``TOKEN_ACK`` exige
    'read' : un ACTIVE sans 'read' bloquerait à jamais la convergence. Le
    manifest est refusé fail-closed AVANT toute écriture (pas d'injection
    silencieuse de 'read').
    """
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=bad_scopes
            )
        ],
    )
    with pytest.raises(EnrollmentError, match="read"):
        await svc.reconcile(_signed_raw(manifest, enroller))
    # Aucune mutation : ni nodeB ajouté, ni bump d'epoch (rien n'est appliqué).
    await _assert_no_change(store, before)
    assert all(m.node_id != "nodeB" for m in (await store.get_membership()).members)


@pytest.mark.parametrize("bad_scopes", [[], ["propose"], ["commit"]])
async def test_rescope_active_below_read_floor_fails_closed(
    seeded, bad_scopes
) -> None:
    """Rescoper un ACTIVE déjà enrôlé sous le plancher 'read' → fail-closed.

    nodeB est d'abord enrôlé full (ACTIVE), puis un 2e manifest tente de le
    rescoper sur un jeu sans 'read' : refus EnrollmentError, scopes inchangés.
    """
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    # Enrôle nodeB full (epoch 1 -> 2).
    m1 = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                scopes=["read", "propose", "commit"],
            )
        ],
    )
    await svc.reconcile(_signed_raw(m1, enroller))
    before = await store.get_membership()
    assert before.epoch == 2

    # Tente de rescoper sous le plancher : refus, aucune mutation.
    m2 = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=bad_scopes
            )
        ],
    )
    with pytest.raises(EnrollmentError, match="read"):
        await svc.reconcile(_signed_raw(m2, enroller))
    await _assert_no_change(store, before)
    member_b = next(
        m for m in (await store.get_membership()).members if m.node_id == "nodeB"
    )
    # Full grant préservé (None ⇔ full), pas dégradé sous le plancher.
    assert member_b.effective_scopes() == FULL_PEER_SCOPES


@pytest.mark.parametrize(
    "good_scopes",
    [["read"], ["read", "propose"], ["read", "propose", "commit"]],
)
async def test_enroll_active_with_read_floor_succeeds(seeded, good_scopes) -> None:
    """Tout jeu CONTENANT 'read' (jusqu'au full) reste accepté."""
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=good_scopes
            )
        ],
    )
    result = await svc.reconcile(_signed_raw(manifest, enroller))
    assert result.applied is True
    assert result.joined == ("nodeB",)
    member_b = next(
        m for m in (await store.get_membership()).members if m.node_id == "nodeB"
    )
    assert member_b.has_scope(PeerScope.READ)
    assert member_b.effective_scopes() == frozenset(good_scopes)


async def test_reconcile_is_idempotent_no_epoch_bump(seeded) -> None:
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=["read"]
            )
        ],
    )
    raw = _signed_raw(manifest, enroller)
    first = await svc.reconcile(raw)
    assert first.applied is True
    epoch_after_first = (await store.get_membership()).epoch

    second = await svc.reconcile(raw)
    assert second.applied is False
    assert second.unchanged == ("nodeB",)
    assert (await store.get_membership()).epoch == epoch_after_first


# =============================================================================
# 16 — Révocation : bump epoch + events ultérieurs fail-closed
# =============================================================================


async def test_revoke_bumps_epoch_and_later_events_fail_closed(seeded) -> None:
    store, keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    # Enrôle nodeB (epoch 1 -> 2).
    enrol = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)],
    )
    await svc.reconcile(_signed_raw(enrol, enroller))
    assert (await store.get_membership()).epoch == 2

    # nodeB signe un event à l'epoch 2 (epoch courant) AVANT révocation.
    signer_store = HivemindStateStore(storage=FakeStorage(), space_id="alpha")  # type: ignore[arg-type]
    signer = HivemindPeerChannel(
        state=signer_store,
        local_node_id="nodeB",
        private_key=keys_b.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id="evt-from-revoked",
        type=EventType.TOKEN_CLAIM,
        origin_node_id="nodeB",
        term=2,
        membership_epoch=2,
        created_at=NOW_ISO,
    )
    message = await signer.sign_event(event, signed_at=NOW_ISO)

    # Révoque nodeB (epoch 2 -> 3).
    revoke = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                state=EnrollmentState.REVOKED,
            )
        ],
    )
    result = await svc.reconcile(_signed_raw(revoke, enroller))
    assert result.revoked == ("nodeB",)
    view = await store.get_membership()
    assert view.epoch == 3
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.EVICTED.value
    evicted = [
        e for e in await store.list_events() if e.type == EventType.PEER_EVICTED.value
    ]
    assert any(e.payload["node_id"] == "nodeB" for e in evicted)

    # L'event de nodeB (signé à epoch 2, désormais EVICTED à epoch 3) fail-closed.
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    with pytest.raises(PeerChannelError) as exc:
        await channel.receive(message)
    assert exc.value.code in (
        PeerErrorCode.UNKNOWN_PEER,
        PeerErrorCode.WRONG_MEMBERSHIP_EPOCH,
    )


# =============================================================================
# 17-19 — peer_scope_guard : narrowing, précondition, mono-tenant
# =============================================================================


def test_read_only_peer_denied_propose_and_commit() -> None:
    kp = generate_peer_keypair()
    read_only = Member(node_id="ro", public_key=kp.public_key, scopes=["read"])

    # READ passe.
    peer_scope_guard(read_only, PeerScope.READ)

    # PROPOSE et COMMIT déniés (INSUFFICIENT_SCOPE).
    for scope in (PeerScope.PROPOSE, PeerScope.COMMIT):
        with pytest.raises(PeerChannelError) as exc:
            peer_scope_guard(read_only, scope)
        assert exc.value.code == PeerErrorCode.INSUFFICIENT_SCOPE

    # Un membre [read, propose] passe PROPOSE mais échoue COMMIT.
    rp = Member(node_id="rp", public_key=kp.public_key, scopes=["read", "propose"])
    peer_scope_guard(rp, PeerScope.PROPOSE)
    with pytest.raises(PeerChannelError) as exc2:
        peer_scope_guard(rp, PeerScope.COMMIT)
    assert exc2.value.code == PeerErrorCode.INSUFFICIENT_SCOPE


def test_commit_scope_is_precondition_not_bypass() -> None:
    """Un peer commit-scope passe le guard, mais le guard ne fait qu'une vérif
    de scope : il retourne None, n'importe ni n'appelle aucune couche
    lease/token — le scope SEUL n'autorise jamais une écriture (ADR-0011)."""
    kp = generate_peer_keypair()
    committer = Member(node_id="c", public_key=kp.public_key, scopes=["commit"])
    assert peer_scope_guard(committer, PeerScope.COMMIT) is None

    # Le module enrollment n'importe AUCUNE couche lease/commit (précondition
    # amont uniquement) : pas de assert_commit_allowed, pas de lease engine.
    src = Path(inspect.getsourcefile(enrollment_mod)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    blob = " ".join(imported)
    assert "assert_commit_allowed" not in blob
    assert "lease" not in blob


def test_unrecognized_tenancy_context_denied() -> None:
    kp = generate_peer_keypair()
    # Membre PLEIN : même full-scope, une tenancy non reconnue est refusée.
    full = Member(node_id="f", public_key=kp.public_key)
    with pytest.raises(PeerChannelError) as exc:
        peer_scope_guard(full, PeerScope.COMMIT, tenancy_context={"tenant": "x"})
    assert exc.value.code == PeerErrorCode.INSUFFICIENT_SCOPE


# =============================================================================
# 20-21 — Mono-tenant by construction + garde AST no-graph/long
# =============================================================================


async def test_enrollment_no_tenant_object_constructed(seeded) -> None:
    """La réconciliation ne construit aucun objet tenant/RLS ; EnrollmentService
    n'expose aucun attribut tenant ; les membres créés n'ont pas de champ
    tenant (Member.extra=forbid)."""
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)],
    )
    await svc.reconcile(_signed_raw(manifest, enroller))

    # Aucun attribut "tenant"/"rls" sur le service.
    attrs = " ".join(dir(svc)).lower()
    assert "tenant" not in attrs
    assert "rls" not in attrs

    # Aucun champ tenant sur les membres / le manifest (extra=forbid garantit
    # déjà le rejet, on vérifie l'absence positive dans le schéma).
    view = await store.get_membership()
    for m in view.members:
        assert "tenant" not in m.model_dump(mode="json")
    assert "tenant" not in EnrollmentPeer.model_fields
    assert "tenant" not in EnrollmentManifest.model_fields


def test_enrollment_module_has_no_graph_or_long_import() -> None:
    """Garde AST (miroir tests/test_engine_long.py:157-193) : enrollment.py
    n'importe ni graph_bridge, ni engines.long_engine, ni neo4j/qdrant/mcp."""
    src_path = Path(inspect.getsourcefile(enrollment_mod))
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            statements.append("import " + ", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            statements.append(
                f"from {prefix}{node.module or ''} import "
                + ", ".join(a.name for a in node.names)
            )
    blob = "\n".join(statements)
    for forbidden in (
        "graph_bridge",
        "engines.long_engine",
        "long_engine",
        "neo4j",
        "qdrant",
        "mcp",
    ):
        assert forbidden not in blob, f"import interdit détecté: {forbidden}"


def test_private_key_never_in_enrollment_models() -> None:
    """Aucun modèle d'enrôlement ne porte de champ private_key : seules les clés
    PUBLIQUES entrent dans le manifest / la membership view."""
    for model in (EnrollmentPeer, EnrollmentManifest):
        assert "private_key" not in model.model_fields
        assert all("private" not in f for f in model.model_fields)


# =============================================================================
# 22-24 — Scopes ENFORCÉS sur le peer channel (Finding 1, ADR-0016)
# =============================================================================


async def _enroll_node_b(seeded, *, scopes: list[str]):
    """Enrôle nodeB avec ``scopes`` via le service et renvoie sa paire de clés.

    Passe par ``reconcile`` (pas un ``set_membership`` direct) pour que la
    membership view porte EXACTEMENT la forme narrowée qu'un manifest produit.
    """
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=scopes
            )
        ],
    )
    await svc.reconcile(_signed_raw(manifest, enroller))
    return keys_b


async def _signed_event_from_b(keys_b, event_type: EventType, *, epoch: int):
    """nodeB signe un event ``event_type`` à l'epoch courant (pour réception)."""
    signer_store = HivemindStateStore(storage=FakeStorage(), space_id="alpha")  # type: ignore[arg-type]
    signer = HivemindPeerChannel(
        state=signer_store,
        local_node_id="nodeB",
        private_key=keys_b.private_key,
        clock=lambda: NOW,
    )
    event = EventEnvelope(
        event_id=f"evt-{event_type.value}",
        type=event_type,
        origin_node_id="nodeB",
        term=2,
        membership_epoch=epoch,
        bank_version=(0 if event_type == EventType.BANK_COMMITTED else -1),
        created_at=NOW_ISO,
    )
    return await signer.sign_event(event, signed_at=NOW_ISO)


async def test_read_only_peer_token_claim_rejected_at_receive(seeded) -> None:
    """Un membre narrowé read-only voit son TOKEN_CLAIM signé REFUSÉ à la
    réception (INSUFFICIENT_SCOPE), pas accepté au journal d'events.

    C'est l'enforcement protocole réel (via ``HivemindPeerChannel.receive``),
    pas un appel direct au helper ``peer_scope_guard``.
    """
    store, keys_a, _enroller, _trusted = seeded
    keys_b = await _enroll_node_b(seeded, scopes=["read"])
    epoch = (await store.get_membership()).epoch

    message = await _signed_event_from_b(
        keys_b, EventType.TOKEN_CLAIM, epoch=epoch
    )
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    with pytest.raises(PeerChannelError) as exc:
        await channel.receive(message)
    assert exc.value.code == PeerErrorCode.INSUFFICIENT_SCOPE
    # Fail-closed : rien n'a été persisté.
    assert await store.get_event(message.event_id) is None


async def test_propose_scope_peer_claim_accepted_but_commit_rejected(
    seeded,
) -> None:
    """Un membre [read, propose] : son TOKEN_CLAIM est ACCEPTÉ, mais son
    BANK_COMMITTED (commit-class) est REFUSÉ — le scope commit reste exigé et
    ne peut être contourné par le simple passage de propose."""
    store, keys_a, _enroller, _trusted = seeded
    keys_b = await _enroll_node_b(seeded, scopes=["read", "propose"])
    epoch = (await store.get_membership()).epoch
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )

    claim = await _signed_event_from_b(keys_b, EventType.TOKEN_CLAIM, epoch=epoch)
    result = await channel.receive(claim)
    assert result.persisted is True

    commit = await _signed_event_from_b(
        keys_b, EventType.BANK_COMMITTED, epoch=epoch
    )
    with pytest.raises(PeerChannelError) as exc:
        await channel.receive(commit)
    assert exc.value.code == PeerErrorCode.INSUFFICIENT_SCOPE
    assert await store.get_event(commit.event_id) is None


async def test_full_scope_peer_commit_passes_scope_check(seeded) -> None:
    """Un membre commit-scope passe le check de scope sur BANK_COMMITTED (le
    scope est une PRÉCONDITION : il n'ouvre rien de plus, mais ne bloque pas non
    plus un membre légitimement scopé). Rétro-compat : un full-scope passe."""
    store, keys_a, _enroller, _trusted = seeded
    keys_b = await _enroll_node_b(seeded, scopes=["read", "propose", "commit"])
    epoch = (await store.get_membership()).epoch
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    commit = await _signed_event_from_b(
        keys_b, EventType.BANK_COMMITTED, epoch=epoch
    )
    result = await channel.receive(commit)
    assert result.persisted is True


async def test_read_only_peer_token_ack_accepted_at_receive(seeded) -> None:
    """RED-without-fix : un membre narrowé read-only voit son TOKEN_ACK signé
    ACCEPTÉ et persisté à la réception.

    Un read-only ACTIVE est un ACKer VALIDE de l'all-ACK full-mesh
    (``expected_ack_node_ids`` attend un ACK de CHAQUE membre ACTIVE). Avec le
    bug (TOKEN_ACK -> PROPOSE), ``receive`` rejetterait cet ACK signé en
    INSUFFICIENT_SCOPE et l'all-ACK ne convergerait jamais (blocage permanent).
    Le fix mappe TOKEN_ACK -> read : l'ACK passe la porte de scope.
    """
    store, keys_a, _enroller, _trusted = seeded
    keys_b = await _enroll_node_b(seeded, scopes=["read"])
    epoch = (await store.get_membership()).epoch

    ack = await _signed_event_from_b(keys_b, EventType.TOKEN_ACK, epoch=epoch)
    channel = HivemindPeerChannel(
        state=store,
        local_node_id="nodeA",
        private_key=keys_a.private_key,
        clock=lambda: NOW,
    )
    result = await channel.receive(ack)
    assert result.status == PeerReceiveStatus.ACCEPTED.value
    assert result.persisted is True
    assert await store.get_event(ack.event_id) is not None


def test_required_scope_mapping_floor_is_read() -> None:
    """Map EventType -> scope : TOKEN_CLAIM (propose-class) -> propose,
    commit-class -> commit, et tout event membership/cycle-de-vie + TOKEN_ACK
    (acte de réception/service) retombe sur le plancher read (accepté comme
    avant P5-9)."""
    from live_mem.core.hivemind import required_scope_for_event

    assert required_scope_for_event(EventType.TOKEN_CLAIM) == PeerScope.PROPOSE
    # TOKEN_ACK est un acte de RÉCEPTION/SERVICE (un membre read-only ACTIVE est
    # un ACKer valide de l'all-ACK) -> plancher read, JAMAIS propose.
    assert required_scope_for_event(EventType.TOKEN_ACK) == PeerScope.READ
    assert required_scope_for_event(EventType.BANK_COMMITTED) == PeerScope.COMMIT
    # Events opérateur / cycle de vie + TOKEN_ACK : plancher read.
    for et in (
        EventType.TOKEN_ACK,
        EventType.PEER_JOINED,
        EventType.PEER_EVICTED,
        EventType.MEMBERSHIP_UPDATED,
        EventType.RESYNC_REQUIRED,
        EventType.BOOTSTRAP_SNAPSHOT_IMPORTED,
    ):
        assert required_scope_for_event(et) == PeerScope.READ


# =============================================================================
# 25-26 — Reconcile ATOMIQUE / fail-closed (Finding 2, ADR-0008)
# =============================================================================


async def test_reconcile_duplicate_active_public_key_raises_and_applies_nothing(
    seeded,
) -> None:
    """Un manifest qui enrôle un NOUVEAU node réutilisant la clé publique d'un
    membre déjà ACTIVE (ici nodeA) est REFUSÉ en EnrollmentError — JAMAIS avalé
    en no-op — et n'applique AUCUNE mutation."""
    store, keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()

    # nodeB réutilise la clé publique de nodeA (déjà ACTIVE) -> identité ambiguë.
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(node_id="nodeB", public_key=keys_a.public_key)
        ],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))
    await _assert_no_change(store, before)


async def test_reconcile_later_semantic_failure_leaves_view_unchanged(
    seeded,
) -> None:
    """Un manifest multi-peers dont une entrée TARDIVE a un conflit sémantique
    (clé active dupliquée) ne doit RIEN appliquer : l'entrée valide qui précède
    ne doit PAS être committée (pas d'application partielle)."""
    store, keys_a, enroller, trusted = seeded
    keys_good = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()

    # Premier peer parfaitement valide, SECOND peer en collision de clé (nodeA).
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(node_id="nodeGOOD", public_key=keys_good.public_key),
            EnrollmentPeer(node_id="nodeBAD", public_key=keys_a.public_key),
        ],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))

    # Aucune application partielle : nodeGOOD ne doit PAS avoir été joint.
    await _assert_no_change(store, before)
    after = await store.get_membership()
    assert all(m.node_id != "nodeGOOD" for m in after.members)
    # Et aucun PEER_JOINED pour nodeGOOD au journal.
    joined = [
        e
        for e in await store.list_events()
        if e.type == EventType.PEER_JOINED.value
        and e.payload.get("node_id") == "nodeGOOD"
    ]
    assert joined == []


async def test_reconcile_revoke_last_active_member_raises(seeded) -> None:
    """Un manifest qui révoquerait le DERNIER membre ACTIVE est refusé en
    EnrollmentError (preflight de la garde « dernier actif »), sans mutation —
    pas avalé après une éviction partielle."""
    store, keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    # nodeA est le seul membre ACTIVE : le révoquer viderait la membership.
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeA",
                public_key=keys_a.public_key,
                state=EnrollmentState.REVOKED,
            )
        ],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))
    await _assert_no_change(store, before)


async def test_reconcile_active_member_public_key_change_raises_and_keeps_key(
    seeded,
) -> None:
    """Un manifest qui re-déclare un node_id DÉJÀ ACTIVE avec une AUTRE clé
    publique Ed25519 est une rotation de clé implicite : REFUSÉE en
    EnrollmentError (jamais avalée en unchanged/rescope), et n'applique AUCUNE
    mutation — le membre persisté CONSERVE sa clé d'origine (sinon runtime
    `_verify` continuerait à faire confiance à une clé périmée/compromise)."""
    store, keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()
    original_key = next(
        m.public_key for m in before.members if m.node_id == "nodeA"
    )

    # nodeA (déjà ACTIVE) re-déclaré ENROLLED avec une clé publique DIFFÉRENTE.
    rotated = generate_peer_keypair()
    assert rotated.public_key != original_key
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(node_id="nodeA", public_key=rotated.public_key)
        ],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))

    # Aucune mutation : epoch/membres inchangés ET la clé persistée d'origine.
    await _assert_no_change(store, before)
    after = await store.get_membership()
    member_a = next(m for m in after.members if m.node_id == "nodeA")
    assert member_a.public_key == original_key
    assert member_a.public_key != rotated.public_key


async def test_reconcile_revoke_active_member_with_mismatched_key_raises(
    seeded,
) -> None:
    """Une révocation déclarée pour un membre ACTIVE mais avec une public_key
    qui NE correspond PAS à sa clé persistée est un tuple d'identité incohérent :
    REFUSÉE en EnrollmentError fail-closed, AUCUNE mutation (le membre reste
    ACTIVE avec sa clé d'origine, aucun PEER_EVICTED). Le manifest est la source
    de vérité de (node_id, public_key, state) ; appliquer ce revoke serait une
    éviction destructive décidée depuis une identité non vérifiée — même posture
    que le refus de rotation de clé implicite côté ENROLLED.

    On enrôle d'abord nodeB ACTIVE pour que le revoke ne bute PAS sur la garde
    « dernier membre actif » : le SEUL motif de refus possible est donc le
    mismatch de clé (discriminateur RED-sans-fix — l'ancien chemin évinçait
    nodeB depuis le tuple incohérent)."""
    store, _keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    # Enrôle nodeB ACTIVE avec SA clé (epoch 1 -> 2).
    keys_b = generate_peer_keypair()
    await svc.reconcile(
        _signed_raw(
            EnrollmentManifest(
                space_id="alpha",
                enroller_public_key=enroller.public_key,
                peers=[
                    EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)
                ],
            ),
            enroller,
        )
    )
    before = await store.get_membership()  # epoch 2, nodeA + nodeB ACTIVE
    assert before.epoch == 2

    # Révoque nodeB MAIS avec une AUTRE clé publique valide (mismatch).
    wrong = generate_peer_keypair()
    assert wrong.public_key != keys_b.public_key
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=wrong.public_key,
                state=EnrollmentState.REVOKED,
            )
        ],
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(_signed_raw(manifest, enroller))

    # Aucune mutation : nodeB reste ACTIVE avec SA clé, epoch inchangé, aucun
    # PEER_EVICTED au journal pour nodeB.
    await _assert_no_change(store, before)
    after = await store.get_membership()
    member_b = next(m for m in after.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.ACTIVE.value
    assert member_b.public_key == keys_b.public_key
    evicted = [
        e
        for e in await store.list_events()
        if e.type == EventType.PEER_EVICTED.value
        and e.payload.get("node_id") == "nodeB"
    ]
    assert evicted == []


# =============================================================================
# 27-31 — Reconcile ATOMIQUE single-write : tout-ou-rien + concurrence
# =============================================================================


async def _seed_second_active(seeded, *, scopes=None):
    """Enrôle nodeB ACTIVE (epoch 1 -> 2) et renvoie (svc, keys_b)."""
    store, _keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB", public_key=keys_b.public_key, scopes=scopes
            )
            if scopes is not None
            else EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key)
        ],
    )
    await svc.reconcile(_signed_raw(manifest, enroller))
    return svc, keys_b


async def test_reconcile_apply_failure_on_later_delta_leaves_view_unchanged(
    seeded,
) -> None:
    """RED-without-fix (atomicité tout-ou-rien).

    Un plan multi-deltas (revoke nodeB + add nodeC). On force l'écriture
    membership à ÉCHOUER au moment où la vue cible contient le NOUVEL ajout
    (nodeC). L'application atomique single-write construit la vue COMPLÈTE
    (revoke + add) en une seule ``set_membership`` : l'échec frappe ce write
    unique ⇒ AUCUNE mutation ⇒ nodeB reste ACTIVE, nodeC absent.

    Sur l'ancien chemin per-delta, la révocation de nodeB était committée par
    SON PROPRE ``set_membership`` (vue SANS nodeC, donc non interceptée) AVANT
    que l'ajout de nodeC n'échoue : nodeB se retrouvait EVICTED — application
    partielle. Ce test l'interdit."""
    store, _keys_a, enroller, trusted = seeded
    svc, keys_b = await _seed_second_active(seeded)
    keys_c = generate_peer_keypair()
    before = await store.get_membership()  # epoch 2, nodeA + nodeB ACTIVE
    assert before.epoch == 2

    real_set = store.set_membership

    async def failing_set(view):
        # Échoue UNIQUEMENT quand la vue cible contient déjà nodeC (l'ajout) :
        # sur le chemin atomique c'est le write unique combiné ; sur l'ancien
        # chemin per-delta, la révocation seule (sans nodeC) serait passée.
        if any(m.node_id == "nodeC" for m in view.members):
            raise RuntimeError("write injecté en échec sur la vue avec l'ajout")
        return await real_set(view)

    store.set_membership = failing_set  # type: ignore[assignment]
    try:
        manifest = EnrollmentManifest(
            space_id="alpha",
            enroller_public_key=enroller.public_key,
            peers=[
                EnrollmentPeer(
                    node_id="nodeB",
                    public_key=keys_b.public_key,
                    state=EnrollmentState.REVOKED,
                ),
                EnrollmentPeer(node_id="nodeC", public_key=keys_c.public_key),
            ],
        )
        with pytest.raises(Exception):
            await svc.reconcile(_signed_raw(manifest, enroller))
    finally:
        store.set_membership = real_set  # type: ignore[assignment]

    # Tout-ou-rien : nodeB TOUJOURS ACTIVE (pas de révocation partielle), nodeC
    # absent, epoch inchangé.
    after = await store.get_membership()
    assert after.epoch == 2
    member_b = next(m for m in after.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.ACTIVE.value
    assert all(m.node_id != "nodeC" for m in after.members)


async def test_reconcile_multi_delta_single_epoch_bump(seeded) -> None:
    """Un plan multi-deltas (revoke nodeB + add nodeC) ne bumpe l'epoch que de
    +1 (une seule ``set_membership``), pas une fois par delta. Sur l'ancien
    chemin per-delta, revoke puis add produisaient DEUX bumps (epoch 2 -> 4)."""
    store, _keys_a, enroller, trusted = seeded
    svc, keys_b = await _seed_second_active(seeded)
    keys_c = generate_peer_keypair()
    assert (await store.get_membership()).epoch == 2

    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeB",
                public_key=keys_b.public_key,
                state=EnrollmentState.REVOKED,
            ),
            EnrollmentPeer(node_id="nodeC", public_key=keys_c.public_key),
        ],
    )
    result = await svc.reconcile(_signed_raw(manifest, enroller))
    assert result.epoch_before == 2
    assert result.epoch_after == 3  # +1 UNIQUE pour tout le plan
    assert result.revoked == ("nodeB",)
    assert result.joined == ("nodeC",)
    view = await store.get_membership()
    assert view.epoch == 3
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_b.status == MemberStatus.EVICTED.value
    member_c = next(m for m in view.members if m.node_id == "nodeC")
    assert member_c.status == MemberStatus.ACTIVE.value
    # Audit : les DEUX events au MÊME epoch 3 (PEER_EVICTED + PEER_JOINED).
    evicted = [
        e
        for e in await store.list_events()
        if e.type == EventType.PEER_EVICTED.value
        and e.payload.get("node_id") == "nodeB"
    ]
    joined = [
        e
        for e in await store.list_events()
        if e.type == EventType.PEER_JOINED.value
        and e.payload.get("node_id") == "nodeC"
    ]
    assert evicted and evicted[0].payload["epoch"] == 3
    assert joined and joined[0].payload["epoch"] == 3


async def test_reconcile_detects_stale_plan_and_fails_closed(seeded) -> None:
    """RED-without-fix (atomicité concurrentielle).

    Simule une mutation membership CONCURRENTE qui se glisse entre la lecture
    de preflight (qui produit ``base_view`` à l'epoch E) et l'application : on
    patche ``_plan`` pour qu'il MUTE réellement la membership (add nodeX, epoch
    E -> E+1) APRÈS avoir capturé ``base_view``, puis renvoie un plan calculé
    sur la vue PÉRIMÉE. L'application re-lit la vue VERROUILLÉE, détecte
    ``epoch != base_view.epoch`` et REFUSE fail-closed (EnrollmentError) sans
    appliquer le plan périmé.

    Sur l'ancien chemin (read hors verrou, pas de re-validation), ce plan
    périmé se serait appliqué tel quel."""
    store, _keys_a, enroller, trusted = seeded
    svc, keys_b = await _seed_second_active(seeded)
    keys_c = generate_peer_keypair()
    keys_x = generate_peer_keypair()
    base = await store.get_membership()
    assert base.epoch == 2

    from live_mem.core.hivemind import layout

    original_plan = svc._plan
    injected = {"done": False}
    members_key = layout.members_key(store._space_id)

    def plan_with_concurrent_mutation(view, manifest):
        plan = original_plan(view, manifest)
        if not injected["done"]:
            injected["done"] = True
            # Mutation concurrente SIMULÉE : on écrit DIRECTEMENT le members.json
            # in-memory (epoch E -> E+1) après avoir capturé base_view mais avant
            # l'application. La vue verrouillée re-lue par apply_membership_plan
            # diverge alors de base_view -> détection fail-closed.
            new_view = MembershipView(
                epoch=view.epoch + 1,
                members=list(view.members)
                + [Member(node_id="nodeX", public_key=keys_x.public_key)],
            )
            store._storage.objects[members_key] = json.dumps(
                new_view.model_dump(mode="json"), indent=2, ensure_ascii=False
            )
        return plan

    svc._plan = plan_with_concurrent_mutation  # type: ignore[assignment]
    try:
        manifest = EnrollmentManifest(
            space_id="alpha",
            enroller_public_key=enroller.public_key,
            peers=[EnrollmentPeer(node_id="nodeC", public_key=keys_c.public_key)],
        )
        with pytest.raises(EnrollmentError):
            await svc.reconcile(_signed_raw(manifest, enroller))
    finally:
        svc._plan = original_plan  # type: ignore[assignment]

    # Le plan périmé (add nodeC) n'a PAS été appliqué : nodeC absent. La mutation
    # concurrente injectée (nodeX) est, elle, présente (epoch 3).
    after = await store.get_membership()
    assert all(m.node_id != "nodeC" for m in after.members)


async def test_reconcile_replaces_sole_active_member_succeeds(seeded) -> None:
    """MINOR (Codex) : remplacer le SEUL membre actif (revoke nodeA + add nodeB
    avec une clé DIFFÉRENTE) réussit désormais — l'application atomique pose la
    vue cible {nodeB ACTIVE, nodeA EVICTED} en une fois, sans jamais traverser
    un état « zéro actif » qui déclencherait la garde dernier-actif."""
    store, keys_a, enroller, trusted = seeded
    keys_b = generate_peer_keypair()
    assert keys_b.public_key != keys_a.public_key
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    manifest = EnrollmentManifest(
        space_id="alpha",
        enroller_public_key=enroller.public_key,
        peers=[
            EnrollmentPeer(
                node_id="nodeA",
                public_key=keys_a.public_key,
                state=EnrollmentState.REVOKED,
            ),
            EnrollmentPeer(node_id="nodeB", public_key=keys_b.public_key),
        ],
    )
    result = await svc.reconcile(_signed_raw(manifest, enroller))
    assert result.epoch_after == 2
    assert result.revoked == ("nodeA",)
    assert result.joined == ("nodeB",)
    view = await store.get_membership()
    member_a = next(m for m in view.members if m.node_id == "nodeA")
    member_b = next(m for m in view.members if m.node_id == "nodeB")
    assert member_a.status == MemberStatus.EVICTED.value
    assert member_b.status == MemberStatus.ACTIVE.value


async def test_reconcile_sole_member_same_key_swap_rejected(seeded) -> None:
    """Cas IRRÉSOLUBLE en V1 (limite transaction multi-clés) : remplacer le SEUL
    membre actif (nodeA) par un nouveau node réutilisant SA clé publique est
    refusé fail-closed AVANT toute écriture.

    Première ligne de défense : le SCHÉMA du manifest interdit déjà deux peers
    partageant une public_key (``_validate_peers`` — identité ambiguë), donc un
    tel manifest ne peut PAS être construit/désérialisé. On le prouve au niveau
    construction (ValidationError) ET au niveau reconcile (EnrollmentError via la
    porte schéma), sans aucune mutation. La garde ``_plan`` du cas irrésoluble
    reste une défense en profondeur si un futur schéma relâchait cette unicité."""
    from pydantic import ValidationError

    store, keys_a, enroller, trusted = seeded
    svc = EnrollmentService(
        store, MembershipService(store), trusted_enroller_keys=trusted
    )
    before = await store.get_membership()

    # (a) Construction directe refusée par le validator de schéma du manifest.
    with pytest.raises(ValidationError):
        EnrollmentManifest(
            space_id="alpha",
            enroller_public_key=enroller.public_key,
            peers=[
                EnrollmentPeer(
                    node_id="nodeA",
                    public_key=keys_a.public_key,
                    state=EnrollmentState.REVOKED,
                ),
                EnrollmentPeer(node_id="nodeNEW", public_key=keys_a.public_key),
            ],
        )

    # (b) Même JSON brut passé à reconcile -> porte schéma -> EnrollmentError,
    # aucune mutation.
    raw = json.dumps(
        {
            "space_id": "alpha",
            "enroller_public_key": enroller.public_key,
            "peers": [
                {
                    "node_id": "nodeA",
                    "public_key": keys_a.public_key,
                    "state": "revoked",
                },
                {"node_id": "nodeNEW", "public_key": keys_a.public_key},
            ],
        }
    )
    with pytest.raises(EnrollmentError):
        await svc.reconcile(raw)
    await _assert_no_change(store, before)
