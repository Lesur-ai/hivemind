# -*- coding: utf-8 -*-
"""
Tests pour issue #12 / P5-4 — surface d'observabilité read-only & déclencheurs
de récupération manuelle.

Couvre :
- le vocabulaire typé ``HiveStatus`` miroir de la référence test ;
- le verdict par scénario (disabled/healthy/blocked/unsafe/resync_required), y
  compris corruption -> unsafe JAMAIS disabled (fail-closed) ;
- le payload : expected-vs-received ACKs (identité sur ACTIVE) + block_reason ;
- ZÉRO écriture prouvée deux fois (compteurs put/delete ET snapshot byte-égal) ;
- les codes d'erreur structurés distinguant permission/protocole/read ;
- les déclencheurs de récupération DÉLÈGUENT aux services P5-1 existants
  (aucune primitive de mutation propre, aucun nouvel outil MCP) ;
- l'isolation (AST : pas d'import graph/long) et l'absence de timer.

``FakeStorage`` (compteurs ``put_calls``/``delete_calls`` + ``snapshot``) est
réutilisé depuis ``tests/test_hivemind_state.py``. Les hives sont amorcés via
les VRAIS setters du store (``set_node_identity`` / ``set_membership`` / ...),
pas reconstruits à la main.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import live_mem.core.hivemind.recovery as recovery_module
import live_mem.core.hivemind.status as status_module
from live_mem.core.hivemind import (
    Ack,
    BankVersionPointer,
    EventEnvelope,
    EventType,
    HiveEventView,
    HiveNodeStatus,
    HivePeerView,
    HiveStatus,
    HiveStatusReport,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipService,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    PeerChannelError,
    PeerErrorCode,
    QueueEntry,
    QueueEntryStatus,
    RecoveryTriggers,
    ResyncService,
    TokenLeaseState,
    TokenState,
    compute_hive_status,
    generate_peer_keypair,
    layout,
)
from tests.hivemind_harness.model import HiveStatus as HarnessHiveStatus
from tests.test_hivemind_state import FakeStorage


SPACE = "alpha"
NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(at: datetime):
    return lambda: at


# =============================================================================
# Storage comptant — prouve qu'un appel n'écrit RIEN
# =============================================================================


class CountingStorage(FakeStorage):
    """``FakeStorage`` qui interdit aussi ``put_json`` non compté (déjà compté
    via ``put``) — sert uniquement à durcir l'intention : tout write passe par
    ``put`` (compté) ou ``delete`` (compté). Aucun override de comportement."""


# =============================================================================
# Helpers de seeding — via les VRAIS setters du store
# =============================================================================


async def _seed_meta(storage: FakeStorage) -> None:
    """Écrit un ``_meta.json`` minimal (sinon un hive orphelin -> unsafe)."""
    await storage.put(
        f"{SPACE}/_meta.json", json.dumps({"space_id": SPACE, "version": 1})
    )


async def _seed_healthy(storage: FakeStorage, *, n_members: int = 1):
    """Hive sain : node.json + N membres ACTIVE + _meta.json. Retourne
    (store, [keypairs])."""
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = [generate_peer_keypair() for _ in range(n_members)]
    await store.set_node_identity(
        NodeIdentity(node_id="nodeA", display_name="A", public_key=keys[0].public_key)
    )
    members = [
        Member(
            node_id=f"node{i}",
            display_name=f"n{i}",
            public_key=keys[i].public_key,
        )
        for i in range(n_members)
    ]
    # node_id du membre 0 == nodeA (identité locale).
    members[0] = Member(
        node_id="nodeA", display_name="A", public_key=keys[0].public_key
    )
    await store.set_membership(MembershipView(epoch=3, members=members))
    await store.bump_term(2, updated_by_node_id="nodeA")
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=7, commit_id="c0ffee")
    )
    await _seed_meta(storage)
    return store, keys


# =============================================================================
# A. Enum / parité
# =============================================================================


def test_hivemind_package_imports_and_status_parser_is_decoupled() -> None:
    """Régression import-break (P5-4) : le package ``live_mem.core.hivemind``
    s'importe (donc ``status``/``recovery`` collectent) ET ``status._parse_iso``
    est un helper LOCAL, jamais importé du privé instable
    ``lease_runtime._parse_iso`` (qui n'existe pas : lease_runtime expose
    ``_parse_lease_until``). Ce test ÉCHOUERAIT à la collecte si l'import de
    ``status.py`` reposait encore sur ``lease_runtime._parse_iso``."""
    import live_mem.core.hivemind as hivemind_pkg
    from live_mem.core.hivemind import lease_runtime as lease_mod

    # Le package entier s'importe sans ImportError (la collecte de ce module le
    # prouve déjà ; on l'affirme explicitement pour ancrer la régression).
    assert hivemind_pkg.status is status_module
    # lease_runtime n'expose PAS _parse_iso (il a _parse_lease_until).
    assert not hasattr(lease_mod, "_parse_iso")
    assert hasattr(lease_mod, "_parse_lease_until")
    # status définit son PROPRE _parse_iso (objet local, pas un alias importé).
    assert callable(status_module._parse_iso)
    # AST : status n'importe PAS _parse_iso depuis lease_runtime.
    tree = ast.parse(inspect.getsource(status_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "lease_runtime"
        ):
            imported = {alias.name for alias in node.names}
            assert "_parse_iso" not in imported, (
                "status.py ne doit pas importer le privé instable "
                "lease_runtime._parse_iso"
            )


def test_status_local_parser_normalizes_to_utc() -> None:
    """``status._parse_iso`` : un ``Z`` final est normalisé et un timestamp naïf
    est interprété UTC (mêmes règles que model/peer)."""
    aware = status_module._parse_iso("2026-06-19T12:00:00Z")
    assert aware.tzinfo is not None
    assert aware == datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    naive = status_module._parse_iso("2026-06-19T12:00:00")
    assert naive.tzinfo is timezone.utc
    assert naive == datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_hivestatus_matches_harness_reference() -> None:
    """Miroir byte-pour-byte de la référence test : mêmes noms/valeurs ET même
    ordre."""
    assert {m.name: m.value for m in HiveStatus} == {
        m.name: m.value for m in HarnessHiveStatus
    }
    assert [m.name for m in HiveStatus] == [m.name for m in HarnessHiveStatus]
    assert [m.value for m in HiveStatus] == [m.value for m in HarnessHiveStatus]


# =============================================================================
# B. Sémantique du verdict
# =============================================================================


async def test_non_hive_space_reports_disabled() -> None:
    storage = FakeStorage()
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert isinstance(report, HiveStatusReport)
    assert report.hive_status == HiveStatus.DISABLED
    assert report.is_hive is False
    assert report.peers == []
    assert report.membership_epoch is None
    assert report.expected_acks == []
    assert report.received_acks == []
    assert report.token_holder is None
    assert report.term is None
    assert report.bank_version is None
    assert report.lease_active is False


async def test_healthy_hive_reports_healthy() -> None:
    storage = FakeStorage()
    await _seed_healthy(storage, n_members=2)
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.HEALTHY
    assert report.is_hive is True
    assert report.block_reason == ""
    assert report.membership_epoch == 3
    assert report.protocol_version >= 1
    assert report.bank_version == 7
    assert report.commit_id == "c0ffee"
    assert set(report.expected_acks) == {"nodeA", "node1"}
    assert {p.node_id for p in report.peers} == {"nodeA", "node1"}


async def test_pending_head_missing_ack_reports_blocked() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    # Un head PENDING demandé par un membre ACTIVE, sans ACK -> bloqué.
    await store.enqueue(
        QueueEntry(
            event_id="evtHEAD",
            sequence=0,
            requester_node_id="nodeA",
            membership_epoch=3,
        )
    )
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.BLOCKED
    assert report.queue_head_event_id == "evtHEAD"
    # Identité, pas un compte : received est un sous-ensemble STRICT d'expected.
    assert set(report.received_acks) < set(report.expected_acks)
    missing = set(report.expected_acks) - set(report.received_acks)
    assert missing == {"nodeA", "node1"}
    for node_id in missing:
        assert node_id in report.block_reason


async def test_full_ack_head_reports_healthy() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.enqueue(
        QueueEntry(
            event_id="evtHEAD",
            sequence=0,
            requester_node_id="nodeA",
            membership_epoch=3,
        )
    )
    # Tous les membres ACTIVE ont ACK -> sain.
    for node_id in ("nodeA", "node1"):
        await store.record_ack(Ack(event_id="evtHEAD", ack_by_node_id=node_id))
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.HEALTHY
    assert report.block_reason == ""
    assert set(report.received_acks) == set(report.expected_acks)


async def test_resync_node_status_reports_resync_required() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="epoch futur")
    )
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.RESYNC_REQUIRED
    assert report.hive_status != HiveStatus.DISABLED


async def test_unsafe_node_status_reports_unsafe() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="import partiel")
    )
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.UNSAFE


async def test_orphan_hive_meta_missing_reports_unsafe() -> None:
    """_hivemind/ présent mais _meta.json supprimé -> unsafe (override
    orphelin de hive_status_label), jamais healthy ni disabled."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    await storage.delete(f"{SPACE}/_meta.json")
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.hive_status == HiveStatus.UNSAFE
    assert report.is_hive is True


async def test_seq_collision_surfaces_in_payload_not_verdict() -> None:
    """Deux event_id distincts sur la même sequence -> seq_collisions non vide,
    MAIS le verdict reste HEALTHY/BLOCKED (jamais DEGRADED : C1)."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.enqueue(
        QueueEntry(event_id="evtX", sequence=5, requester_node_id="nodeA",
                   membership_epoch=3)
    )
    await store.enqueue(
        QueueEntry(event_id="evtY", sequence=5, requester_node_id="node1",
                   membership_epoch=3)
    )
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert len(report.seq_collisions) == 1
    assert report.seq_collisions[0].sequence == 5
    assert set(report.seq_collisions[0].event_ids) == {"evtX", "evtY"}
    assert report.hive_status != HiveStatus.DEGRADED
    assert report.hive_status in (HiveStatus.HEALTHY, HiveStatus.BLOCKED)


# =============================================================================
# C. Fail-closed (porteur)
# =============================================================================


async def test_corrupted_members_propagates_never_disabled() -> None:
    """Un members.json non-JSON lève CorruptedStateError et n'est JAMAIS
    dégradé en un rapport DISABLED."""
    from live_mem.core.hivemind import CorruptedStateError

    storage = FakeStorage()
    await _seed_healthy(storage)
    # Clobber le fichier critique.
    storage.objects[layout.members_key(SPACE)] = "{ ceci n'est pas du JSON"

    result = None
    with pytest.raises(CorruptedStateError):
        result = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    # Aucun rapport DISABLED n'a pu être renvoyé : l'exception a propagé.
    assert result is None


async def test_malformed_lease_until_surfaces_as_corrupted_state() -> None:
    """RÉGRESSION (Codex PR102, MINOR) : un ``lease_until`` malformé sur un token
    NON ACTIF (FREE) est de l'état critique corrompu. ``lease_is_active`` retourne
    False AVANT tout parse (le state n'est pas HELD/RELEASING), donc le calcul de
    TTL d'observabilité atteignait ``_parse_iso`` et levait un ``ValueError`` NU,
    contournant la taxonomie fail-closed ``CorruptedStateError``. Le parse de TTL
    doit router toute corruption de ``lease_until`` vers ``CorruptedStateError``."""
    from live_mem.core.hivemind import CorruptedStateError

    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    # Token FREE -> lease_is_active court-circuite au state, mais lease_until est
    # quand même non None et non parsable.
    await store.set_token(
        TokenLeaseState(state=TokenState.FREE, lease_until="pas-une-date")
    )
    result = None
    with pytest.raises(CorruptedStateError):
        result = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert result is None


# =============================================================================
# D. ZÉRO écriture (porteur) — compteurs ET snapshot
# =============================================================================


async def _build_fixture(kind: str) -> CountingStorage:
    storage = CountingStorage()
    if kind == "disabled":
        return storage
    store, _ = await _seed_healthy(storage, n_members=2)
    if kind == "blocked":
        await store.enqueue(
            QueueEntry(event_id="evtHEAD", sequence=0,
                       requester_node_id="nodeA", membership_epoch=3)
        )
    elif kind == "resync":
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="x")
        )
    elif kind == "unsafe":
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.UNSAFE, reason="x")
        )
    return storage


@pytest.mark.parametrize(
    "kind", ["disabled", "healthy", "blocked", "resync", "unsafe"]
)
async def test_status_call_performs_zero_writes(kind: str) -> None:
    storage = await _build_fixture(kind)
    put_before = storage.put_calls
    delete_before = storage.delete_calls
    snapshot_before = storage.snapshot()

    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert isinstance(report, HiveStatusReport)

    assert storage.put_calls - put_before == 0, "compute a appelé put"
    assert storage.delete_calls - delete_before == 0, "compute a appelé delete"
    assert storage.objects == snapshot_before, "compute a muté le storage"


# =============================================================================
# E. Complétude du payload / déterminisme (horloge injectée)
# =============================================================================


async def test_payload_carries_token_lease_term_bank() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    lease_until = (NOW + timedelta(seconds=120)).replace(microsecond=0)
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="nodeA",
            term=2,
            fencing_token=2,
            lease_until=lease_until.isoformat(),
            membership_epoch=3,
        )
    )
    # Avant l'échéance : lease active, TTL > 0.
    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    assert report.token_holder == "nodeA"
    assert report.term == 2
    assert report.lease_until == lease_until.isoformat()
    assert report.lease_active is True
    assert report.lease_ttl_seconds is not None and report.lease_ttl_seconds > 0
    assert report.lease_ttl_seconds == 120
    assert report.bank_version == 7
    assert report.commit_id == "c0ffee"

    # Après l'échéance : lease inactive, TTL borné à 0. Aucune horloge murale.
    later = lease_until + timedelta(seconds=30)
    report2 = await compute_hive_status(storage, SPACE, clock=_fixed_clock(later))  # type: ignore[arg-type]
    assert report2.lease_active is False
    assert report2.lease_ttl_seconds == 0


async def test_recent_events_tail_bounded_and_ordered() -> None:
    """> event_tail events appendés -> recent_events borné à event_tail,
    newest-last (le dernier élément est le plus récent). Garde de la correction
    C4 (list_events(limit=N) renverrait les PLUS ANCIENS)."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    # Append déterministe avec created_at croissant (le tri de clé est ISO).
    base = NOW
    for i in range(8):
        ts = (base + timedelta(seconds=i)).isoformat()
        await store.append_event(
            EventEnvelope(
                event_id=f"evt{i:02d}",
                type=EventType.WATERMARK_UPDATED,
                origin_node_id="nodeA",
                term=2,
                membership_epoch=3,
                created_at=ts,
            )
        )
    report = await compute_hive_status(
        storage, SPACE, clock=_fixed_clock(NOW), event_tail=3  # type: ignore[arg-type]
    )
    assert len(report.recent_events) == 3
    assert all(isinstance(e, HiveEventView) for e in report.recent_events)
    # newest-last : le dernier event_id appendé est en queue de liste.
    assert report.recent_events[-1].event_id == "evt07"
    assert [e.event_id for e in report.recent_events] == ["evt05", "evt06", "evt07"]


async def test_expected_acks_excludes_evicted_identity() -> None:
    """Un membre évincé sort de expected_acks même si un ACK stale du sien
    traîne en storage — il ne compte pas vers le fully-acked (identité sur
    ACTIVE)."""
    storage = FakeStorage()
    store, keys = await _seed_healthy(storage, n_members=2)
    # head PENDING demandé par nodeA.
    await store.enqueue(
        QueueEntry(event_id="evtHEAD", sequence=0, requester_node_id="nodeA",
                   membership_epoch=3)
    )
    # ACK stale du membre node1 (qui sera évincé).
    await store.record_ack(Ack(event_id="evtHEAD", ack_by_node_id="node1"))
    await store.record_ack(Ack(event_id="evtHEAD", ack_by_node_id="nodeA"))

    # Évince node1 via le vrai service (epoch bump).
    svc = MembershipService(store)
    await svc.evict_member("node1", operator="op", confirm=True, reason="test")

    report = await compute_hive_status(storage, SPACE, clock=_fixed_clock(NOW))  # type: ignore[arg-type]
    # node1 n'est plus attendu (exclu d'expected) ; nodeA l'est et a ACK.
    assert "node1" not in report.expected_acks
    assert report.expected_acks == ["nodeA"]
    assert set(report.expected_acks) <= set(report.received_acks)
    assert report.hive_status == HiveStatus.HEALTHY


# =============================================================================
# F. Codes d'erreur structurés — la distinction 3-voies (recovery.py)
# =============================================================================


async def test_evict_missing_operator_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.evict("node1", operator="", confirm=True)
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    # Levé AVANT toute délégation -> zéro écriture.
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_evict_unconfirmed_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.evict("node1", operator="op", confirm=False)
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_evict_on_unsafe_hive_is_protocol_blocked() -> None:
    """Operator+confirm valides mais node_status UNSAFE -> PROTOCOL_BLOCKED
    (traduit du BootstrapError de _current_view)."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="import partiel")
    )
    triggers = RecoveryTriggers(store)
    with pytest.raises(PeerChannelError) as exc:
        await triggers.evict("node1", operator="op", confirm=True)
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED


async def test_request_resync_missing_operator_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.request_resync(
            operator="", confirm=True, observed_epoch=99, observed_bank_version=99
        )
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    # Levé AVANT toute délégation -> zéro écriture (pas de node_status forcé).
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_request_resync_unconfirmed_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.request_resync(
            operator="op", confirm=False, observed_epoch=99, observed_bank_version=99
        )
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_complete_resync_missing_operator_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="en retard")
    )
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.complete_resync(operator="", confirm=True)
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    # Levé AVANT toute délégation -> aucun passage HEALTHY forcé.
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_complete_resync_unconfirmed_is_permission_denied() -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="en retard")
    )
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.complete_resync(operator="op", confirm=False)
    assert exc.value.code is PeerErrorCode.PERMISSION_DENIED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_request_resync_on_non_hive_space_is_protocol_blocked() -> None:
    """Operator+confirm valides MAIS space NON-Hivemind (legacy/local) ->
    PROTOCOL_BLOCKED fail-closed, ZÉRO écriture. Sans la garde de contexte,
    ``observe_remote`` fabriquerait un node_status.json + un event
    RESYNC_REQUIRED sur un space non partagé (epoch absent traité comme 0,
    pointeur absent comme -1), violant l'invariant non-Hivemind."""
    storage = FakeStorage()  # aucun node.json / membership -> non-Hivemind
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.request_resync(
            operator="op", confirm=True, observed_epoch=99, observed_bank_version=99
        )
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED
    # Refus AVANT toute délégation -> aucun état Hivemind fabriqué.
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_request_resync_on_unsafe_space_is_protocol_blocked() -> None:
    """Operator+confirm valides MAIS contexte Hivemind UNSAFE (structure
    incomplète / import partiel) -> PROTOCOL_BLOCKED fail-closed, ZÉRO
    écriture. Un space non sûr n'est pas un terrain valide pour muter la
    santé."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="import partiel")
    )
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.request_resync(
            operator="op", confirm=True, observed_epoch=99, observed_bank_version=99
        )
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_request_resync_on_incomplete_resync_marker_is_protocol_blocked() -> None:
    """RÉGRESSION (Codex PR102) : un ``node_status.json=RESYNC_REQUIRED``
    SOLITAIRE — sans ``node.json`` ni membre ACTIVE — est STRUCTURELLEMENT
    INCOMPLET. ``resolve_hive_context`` le classe ``is_hive=True`` /
    ``node_status=RESYNC_REQUIRED`` (marqueur respecté tel quel), donc l'ancienne
    garde (qui ne bloquait QUE non-Hivemind et UNSAFE) le laissait passer et
    ``observe_remote`` mutait un contexte incomplet (epoch absent traité comme 0,
    pointeur absent comme -1). La garde DOIT exiger la complétude structurelle
    (identité présente ET >= 1 membre ACTIVE) pour un contexte RESYNC_REQUIRED ->
    PROTOCOL_BLOCKED fail-closed, ZÉRO écriture."""
    storage = FakeStorage()  # ni node.json ni membership : marqueur solitaire
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="marqueur seul")
    )
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.request_resync(
            operator="op", confirm=True, observed_epoch=99, observed_bank_version=99
        )
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED
    # Refus AVANT toute délégation -> aucun état Hivemind muté.
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_complete_resync_on_incomplete_resync_marker_is_protocol_blocked() -> None:
    """RÉGRESSION (Codex PR102) : symétrique de ``request_resync`` —
    ``complete_resync`` sur un ``node_status=RESYNC_REQUIRED`` solitaire
    (sans ``node.json`` ni membre ACTIVE) doit refuser PROTOCOL_BLOCKED
    fail-closed, ZÉRO écriture. Sans la garde de complétude,
    ``mark_resync_complete`` passait son rattrapage avec un epoch/bank locaux par
    défaut et écrivait HEALTHY + un event RESYNC_COMPLETED sur un hive
    inexistant."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="marqueur seul")
    )
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.complete_resync(operator="op", confirm=True)
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_complete_resync_on_non_hive_space_is_protocol_blocked() -> None:
    """``complete_resync`` partage la même garde de contexte : un space
    NON-Hivemind est refusé PROTOCOL_BLOCKED fail-closed, ZÉRO écriture."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    triggers = RecoveryTriggers(store)
    put_before, delete_before = storage.put_calls, storage.delete_calls
    with pytest.raises(PeerChannelError) as exc:
        await triggers.complete_resync(operator="op", confirm=True)
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED
    assert storage.put_calls == put_before
    assert storage.delete_calls == delete_before


async def test_request_resync_on_healthy_hive_delegates(monkeypatch) -> None:
    """La garde de contexte AUTORISE un hive sain (cas primaire : un node sain
    apprend qu'il est en retard) : la délégation à ``observe_remote`` a bien
    lieu après le passage de la garde."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)

    observed: list[tuple] = []

    async def spy_observe(self, *, observed_epoch=-1, observed_bank_version=-1):  # noqa: ANN001
        observed.append((observed_epoch, observed_bank_version))
        return NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="spy")

    monkeypatch.setattr(ResyncService, "observe_remote", spy_observe)
    out = await triggers.request_resync(
        operator="op", confirm=True, observed_epoch=42, observed_bank_version=8
    )
    assert observed == [(42, 8)]
    assert HiveNodeStatus(out.status) is HiveNodeStatus.RESYNC_REQUIRED


async def test_request_resync_on_resync_required_hive_is_allowed(monkeypatch) -> None:
    """La garde AUTORISE aussi un node déjà RESYNC_REQUIRED (re-observation
    idempotente) : elle ne bloque QUE non-Hivemind et UNSAFE, jamais un hive
    légitimement en retard."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="en retard")
    )
    triggers = RecoveryTriggers(store)

    observed: list[tuple] = []

    async def spy_observe(self, *, observed_epoch=-1, observed_bank_version=-1):  # noqa: ANN001
        observed.append((observed_epoch, observed_bank_version))
        return NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="spy")

    monkeypatch.setattr(ResyncService, "observe_remote", spy_observe)
    out = await triggers.request_resync(
        operator="op", confirm=True, observed_epoch=99, observed_bank_version=99
    )
    assert observed == [(99, 99)]
    assert HiveNodeStatus(out.status) is HiveNodeStatus.RESYNC_REQUIRED


async def test_complete_resync_protocol_refusal_is_protocol_blocked() -> None:
    """Operator+confirm valides mais node_status n'est PAS RESYNC_REQUIRED :
    ``mark_resync_complete`` lève ``BootstrapError`` -> PROTOCOL_BLOCKED."""
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    # Hive sain : pas de resync en cours -> mark_resync_complete refuse.
    triggers = RecoveryTriggers(store)
    with pytest.raises(PeerChannelError) as exc:
        await triggers.complete_resync(operator="op", confirm=True)
    assert exc.value.code is PeerErrorCode.PROTOCOL_BLOCKED


def test_three_error_codes_are_distinct() -> None:
    codes = {
        PeerErrorCode.PERMISSION_DENIED,
        PeerErrorCode.PROTOCOL_BLOCKED,
        PeerErrorCode.READ_ONLY_ALLOWED,
    }
    assert len(codes) == 3
    for name in ("PERMISSION_DENIED", "PROTOCOL_BLOCKED", "READ_ONLY_ALLOWED"):
        assert hasattr(PeerErrorCode, name)


# =============================================================================
# G. Délégation — aucune primitive nouvelle (recovery.py)
# =============================================================================


async def test_evict_trigger_delegates_to_membership_service(monkeypatch) -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage, n_members=2)
    triggers = RecoveryTriggers(store)

    calls: list[tuple] = []

    async def spy(self, node_id, *, operator, confirm=False, reason=""):  # noqa: ANN001
        calls.append((node_id, operator, confirm, reason))
        return MembershipView(epoch=99, members=[])

    monkeypatch.setattr(MembershipService, "evict_member", spy)
    out = await triggers.evict("node1", operator="op", confirm=True, reason="r")
    assert out.epoch == 99
    assert calls == [("node1", "op", True, "r")]

    # RecoveryTriggers ne définit AUCUNE primitive de mutation propre.
    src = inspect.getsource(RecoveryTriggers)
    for forbidden in ("set_membership", "append_event", "record_ack", "enqueue",
                      "set_node_status", "set_token", "set_bank_version_pointer"):
        assert f"def {forbidden}" not in src
        assert f"self._store.{forbidden}" not in src


async def test_resync_triggers_delegate_to_resync_service(monkeypatch) -> None:
    storage = FakeStorage()
    store, _ = await _seed_healthy(storage)
    triggers = RecoveryTriggers(store)

    observed: list[tuple] = []
    completed: list[int] = []

    async def spy_observe(self, *, observed_epoch=-1, observed_bank_version=-1):  # noqa: ANN001
        observed.append((observed_epoch, observed_bank_version))
        return NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED, reason="spy")

    async def spy_complete(self):  # noqa: ANN001
        completed.append(1)
        return NodeHealth(status=HiveNodeStatus.HEALTHY, reason="spy")

    monkeypatch.setattr(ResyncService, "observe_remote", spy_observe)
    monkeypatch.setattr(ResyncService, "mark_resync_complete", spy_complete)

    h1 = await triggers.request_resync(
        operator="op", confirm=True, observed_epoch=5, observed_bank_version=9
    )
    assert observed == [(5, 9)]
    assert HiveNodeStatus(h1.status) is HiveNodeStatus.RESYNC_REQUIRED

    h2 = await triggers.complete_resync(operator="op", confirm=True)
    assert completed == [1]
    assert HiveNodeStatus(h2.status) is HiveNodeStatus.HEALTHY


# =============================================================================
# H. Verrou de surface MCP
# =============================================================================


def test_mcp_tool_surface_unchanged_at_61() -> None:
    """P5-4 n'enregistre AUCUN outil MCP : surface LM2-11 à 61."""
    from mcp.server.fastmcp import FastMCP
    from live_mem.tools import register_all_tools

    mcp = FastMCP(name="test-p5-4")
    register_all_tools(mcp)
    assert len(mcp._tool_manager._tools) == 61


# =============================================================================
# I. Isolation (AST) + absence de timer
# =============================================================================


def _imported_modules(source: str) -> list[str]:
    mods: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
            mods += [alias.name for alias in node.names]
    return mods


def test_status_module_no_forbidden_imports() -> None:
    forbidden = ("graph_push", "graph", "consolidation", "consolidator", "long")
    for module in (status_module, recovery_module):
        mods = _imported_modules(inspect.getsource(module))
        for mod in mods:
            for needle in forbidden:
                assert needle not in mod, (
                    f"{module.__name__} importe interdit : {mod!r} "
                    f"(contient {needle!r})"
                )


def test_status_module_has_no_timer() -> None:
    src = inspect.getsource(status_module)
    assert "asyncio.sleep" not in src
    assert "time.sleep" not in src
    # Aucune lecture d'horloge murale inline : l'horloge est injectée, le défaut
    # _now_utc est IMPORTÉ de lease_runtime (jamais datetime.now( appelé ici).
    assert "datetime.now(" not in src
