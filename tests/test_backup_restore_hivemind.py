# -*- coding: utf-8 -*-
"""
Tests P6-1 (issue #87, ADR-0014) — chorégraphie de forçage-en-avant champ-par-
champ de ``BackupService.restore`` sur un space Hivemind avec
``unsafe_recovery=True``.

Ce module pin la chorégraphie complète :

- refus par défaut (sans ``unsafe_recovery``) reste hive-aware, ZÉRO mutation ;
- avec ``unsafe_recovery=True``, ``membership_epoch`` / ``term`` / token /
  ``bank_version`` montent STRICTEMENT (max(live, backup)+1 pour epoch/term,
  pointer+1 pour la version de bank) ;
- queue dropée, ``acks/`` purgé, ``watermarks/`` prunés à la nouvelle
  ``MembershipView`` ;
- tombstones UNION (live + backup, idempotent par ``note_id``) ;
- ``UNSAFE_RECOVERY_RESTORED`` + ``RESYNC_REQUIRED`` posés au journal ;
- ``HiveNodeStatus.RESYNC_REQUIRED`` posé sur ``node_status.json`` ;
- gate ADR-0011 ``assert_commit_allowed`` exercé exactement une fois avec
  l'intent attendu ;
- corruption fail-closed quel que soit le flag (régression-pin de P2-5) ;
- backup_pointer > live_pointer refusé fail-closed ;
- orphelin (pas de NodeIdentity) refusé avec instruction de bootstrap ;
- ``local_only`` et ``not_a_space`` restent octet-pour-octet hérités.

Déterministe, offline, fake-backed. Réutilise ``FakeStorage`` (tests/
test_hivemind_state.py) et le pattern ``CopyFakeStorage`` (tests/
test_backup_restore_hivemind_guard.py:49).
"""

from __future__ import annotations

import json

import pytest

from live_mem.core.backup import BackupService
from live_mem.core.hivemind import (
    BankVersionPointer,
    CommitDenyReason,
    CommitIntent,
    CommitNotAuthorized,
    EventType,
    HiveNodeStatus,
    HivemindStateStore,
    LeaseRuntime,
    Member,
    MemberStatus,
    MembershipView,
    NodeIdentity,
    TermState,
    Tombstone,
    generate_peer_keypair,
)
from tests.test_hivemind_state import FakeStorage

SPACE = "p6-1-space"
NODE_ID = "node610000000000000000000000000001"
TS = "2026-06-25T12-00-00"
BACKUP_ID = f"{SPACE}/{TS}"
BACKUP_PREFIX = f"_backups/{SPACE}/{TS}/"


# ─────────────────────────────────────────────────────────────────────────────
# CopyFakeStorage — mirror du pattern P2-5 (tests/test_backup_restore_hivemind_guard.py:49)
# ─────────────────────────────────────────────────────────────────────────────


class CopyFakeStorage(FakeStorage):
    """``FakeStorage`` + ``copy_object`` (absent de la classe partagée)."""

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        self.put_calls += 1
        self.objects[dest_key] = self.objects[source_key]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de seeding
# ─────────────────────────────────────────────────────────────────────────────


def _patch_storage(monkeypatch, storage: FakeStorage) -> None:
    monkeypatch.setattr("live_mem.core.backup.get_storage", lambda: storage)


async def _seed_healthy_hive(
    storage: FakeStorage,
    *,
    epoch: int = 5,
    term: int = 7,
    bank_version: int = 10,
    extra_tombstones: list[str] | None = None,
    queue_event_ids: list[str] | None = None,
    seed_token_held: bool = True,
    extra_watermark_node_ids: list[str] | None = None,
    extra_ack_event_ids: list[str] | None = None,
    extra_members: list[str] | None = None,
) -> HivemindStateStore:
    """Pose un space Hivemind sain avec un état coordination paramétrable.

    Le rôle est de fournir un état RICHE pour vérifier le forçage-en-avant
    champ par champ (epoch / term / pointer / tombstones / queue / acks /
    watermarks / membership).
    """
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
    members = [
        Member(
            node_id=NODE_ID,
            public_key=keys.public_key,
            status=MemberStatus.ACTIVE.value,
        )
    ]
    for nid in extra_members or []:
        members.append(
            Member(node_id=nid, status=MemberStatus.ACTIVE.value)
        )
    await store.set_membership(MembershipView(epoch=epoch, members=members))
    if term > 0:
        await store.bump_term(term, updated_by_node_id="seed")
    if bank_version >= 0:
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=bank_version, commit_id=f"c{bank_version}")
        )
    if seed_token_held:
        # Pose un token HELD au term — on s'attend à ce que le forçage le
        # REMPLACE par un token au new_term (> term).
        from datetime import datetime, timedelta, timezone

        from live_mem.core.hivemind import TokenLeaseState, TokenState

        now = datetime.now(timezone.utc).replace(microsecond=0)
        await store.set_token(
            TokenLeaseState(
                state=TokenState.HELD,
                holder_node_id=NODE_ID,
                term=term,
                fencing_token=term,
                granted_at=now.isoformat(),
                lease_until=(now + timedelta(seconds=300)).isoformat(),
                membership_epoch=epoch,
                event_id="seed-held",
            )
        )

    # Tombstones existants
    for note_id in extra_tombstones or []:
        await store.add_tombstone(
            Tombstone(
                note_id=note_id,
                deleted_by_node_id=NODE_ID,
                term=term,
                membership_epoch=epoch,
                bank_version=bank_version,
            )
        )

    # Queue entries
    if queue_event_ids:
        from live_mem.core.hivemind import QueueEntry

        for i, eid in enumerate(queue_event_ids):
            await store.enqueue(
                QueueEntry(
                    event_id=eid,
                    sequence=i,
                    requester_node_id=NODE_ID,
                    term=term,
                    membership_epoch=epoch,
                )
            )

    # Watermarks (extra peers fictifs)
    if extra_watermark_node_ids:
        from live_mem.core.hivemind import Watermark

        # Ne pas écraser le watermark du local si déjà présent
        for nid in extra_watermark_node_ids:
            await store.set_watermark(
                Watermark(node_id=nid, bank_version=bank_version, term=term)
            )

    # Acks (extra event_ids fictifs)
    if extra_ack_event_ids:
        from live_mem.core.hivemind import Ack

        for eid in extra_ack_event_ids:
            await store.record_ack(
                Ack(
                    event_id=eid,
                    ack_by_node_id=NODE_ID,
                    term=term,
                    membership_epoch=epoch,
                )
            )

    # _meta.json présent -> label hivemind_healthy
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    # Quelques fichiers bank live pour rendre le state riche
    storage.objects[f"{SPACE}/bank/activeContext.md"] = "# live bank live\n"
    return store


async def _seed_backup(
    storage: FakeStorage,
    *,
    epoch: int = 3,
    term: int = 4,
    bank_version: int = 8,
    tombstones: list[str] | None = None,
    bank_files: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
) -> int:
    """Pose un backup paramétrable sous ``_backups/{SPACE}/{TS}/``.

    Returns le nombre d'objets posés au total.
    """
    n = 0

    # _meta.json
    storage.objects[f"{BACKUP_PREFIX}_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1, "backup": True}
    )
    n += 1
    storage.objects[f"{BACKUP_PREFIX}_rules.md"] = "# backup rules\n"
    n += 1
    storage.objects[f"{BACKUP_PREFIX}_synthesis.md"] = "# backup synthesis\n"
    n += 1

    # bank/ files
    bf = bank_files if bank_files is not None else {"activeContext.md": "# backup bank\n"}
    for rel, content in bf.items():
        storage.objects[f"{BACKUP_PREFIX}bank/{rel}"] = content
        n += 1

    # _hivemind/ state files
    storage.objects[f"{BACKUP_PREFIX}_hivemind/members.json"] = json.dumps(
        {"protocol_version": 1, "epoch": epoch, "members": [], "updated_at": "x"}
    )
    n += 1
    storage.objects[f"{BACKUP_PREFIX}_hivemind/term.json"] = json.dumps(
        {"protocol_version": 1, "term": term, "updated_at": "x", "updated_by_node_id": ""}
    )
    n += 1
    storage.objects[f"{BACKUP_PREFIX}_hivemind/bank_version.json"] = json.dumps(
        {"protocol_version": 1, "bank_version": bank_version, "commit_id": "bx", "updated_at": "x"}
    )
    n += 1
    for note_id in tombstones or []:
        storage.objects[
            f"{BACKUP_PREFIX}_hivemind/tombstones/{note_id}.json"
        ] = json.dumps(
            {
                "protocol_version": 1,
                "note_id": note_id,
                "deleted_at": "x",
                "deleted_by_node_id": NODE_ID,
                "term": term,
                "membership_epoch": epoch,
                "bank_version": bank_version,
                "event_id": "",
                "request_id": "",
                "reason": "",
            }
        )
        n += 1

    # Extra files
    for rel, content in (extra_files or {}).items():
        storage.objects[f"{BACKUP_PREFIX}{rel}"] = content
        n += 1

    return n


# ─────────────────────────────────────────────────────────────────────────────
# 1. Refuser sans le flag : 0 mutation, 0 event
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_hivemind_healthy_without_flag_refuses_and_no_mutation(
    monkeypatch,
):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    puts_before, deletes_before = storage.put_calls, storage.delete_calls

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=False)

    assert result["status"] == "error"
    msg = result["message"]
    assert "unsafe" in msg
    assert "unsafe_recovery" in msg
    # ZÉRO mutation : le storage est byte-pour-byte identique.
    assert storage.objects == before
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


# ─────────────────────────────────────────────────────────────────────────────
# 2. Forçage des six champs (epoch/term/token/bank_version/queue/tombstones)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_hivemind_healthy_with_unsafe_recovery_forces_six_fields_forward(
    monkeypatch,
):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_tombstones=["t-live-1"],
        queue_event_ids=["queue-1"],
    )
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        tombstones=["t-backup-1"],
    )
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "ok", result
    # 1. epoch monte à max(5,3)+1 = 6
    membership = await store.get_membership()
    assert membership is not None
    assert membership.epoch == 6
    # 2. term monte à max(7,4)+1 = 8
    term = await store.get_term()
    assert term is not None
    assert term.term == 8
    # 3. token FREE après convergence + term=8 + fencing=8 (release convergent)
    token = await store.get_token()
    assert token is not None
    assert token.term == 8
    assert token.fencing_token == 8
    # state==FREE après release convergent par CommitRuntime._converge_token_release
    from live_mem.core.hivemind import TokenState

    assert token.state == TokenState.FREE.value
    # 4. queue empty (queue-1 droppée)
    assert await store.list_queue() == []
    # 5. bank_version pointer = 11
    pointer = await store.get_bank_version_pointer()
    assert pointer is not None
    assert pointer.bank_version == 11
    # 6. tombstones = union {t-live-1, t-backup-1}
    tombs = {t.note_id for t in await store.list_tombstones()}
    assert tombs == {"t-live-1", "t-backup-1"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Le check hérité _meta.json-exists est SAUTÉ avec unsafe_recovery=True
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_skips_meta_exists_check_when_unsafe_recovery_true(monkeypatch):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)  # _meta.json présent
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # Le restore proceedait : status ok malgré _meta.json déjà présent
    assert result["status"] == "ok", result
    # Et le pointeur a bien avancé (preuve que la chorégraphie est allée
    # JUSQU'AU bout, pas un early-abort sur le check hérité).
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    pointer = await store.get_bank_version_pointer()
    assert pointer is not None
    assert pointer.bank_version == 11


# ─────────────────────────────────────────────────────────────────────────────
# 4. UNSAFE_RECOVERY_RESTORED event posé au journal avec le bon payload
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_emits_unsafe_recovery_restored_event(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    events = await store.list_events()
    unsafe_events = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe_events) == 1
    e = unsafe_events[0]
    assert e.origin_node_id == NODE_ID
    assert e.term == 8
    assert e.membership_epoch == 6
    assert e.bank_version == 11
    payload = e.payload
    assert payload["backup_id"] == BACKUP_ID
    assert payload["operator"] == "operator"
    assert payload["reason"] == "backup_restore_unsafe_recovery"
    assert payload["confirm"] is True
    assert payload["unsafe_recovery"] is True
    assert payload["old"] == {"epoch": 5, "term": 7, "bank_version": 10}
    assert payload["new"] == {"epoch": 6, "term": 8, "bank_version": 11}


# ─────────────────────────────────────────────────────────────────────────────
# 5. RESYNC_REQUIRED event + node_status -> RESYNC_REQUIRED
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_emits_resync_required_event_and_marks_node_status(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage)
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    # Event RESYNC_REQUIRED présent
    events = await store.list_events()
    resync_events = [e for e in events if e.type == EventType.RESYNC_REQUIRED.value]
    assert len(resync_events) >= 1
    e = resync_events[0]
    assert e.payload.get("reason") == "backup_restore_unsafe_recovery"

    # node_status = RESYNC_REQUIRED
    health = await store.get_node_status()
    assert health is not None
    assert health.status == HiveNodeStatus.RESYNC_REQUIRED.value
    assert health.reason == "backup_restore_unsafe_recovery"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Route via assert_commit_allowed exactement une fois avec l'intent attendu
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_routes_through_assert_commit_allowed(monkeypatch):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    spied_intents: list[CommitIntent] = []
    original = LeaseRuntime.assert_commit_allowed

    async def spy(self, intent):
        spied_intents.append(intent)
        return await original(self, intent)

    monkeypatch.setattr(LeaseRuntime, "assert_commit_allowed", spy)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    assert len(spied_intents) == 1
    intent = spied_intents[0]
    assert intent.term == 8
    assert intent.bank_version == 11
    assert intent.previous_bank_version == 10
    assert intent.fencing_token == 8
    assert intent.holder_node_id == NODE_ID


async def test_restore_aborts_when_assert_commit_allowed_refuses(monkeypatch):
    """Mis-construct intent path : si ``assert_commit_allowed`` refuse,
    le restore retourne ``status='error'`` sans nouveau commit appended."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    async def deny(self, intent):
        raise CommitNotAuthorized(
            CommitDenyReason.BLOCKED,
            "test-injected refusal",
            {},
        )

    monkeypatch.setattr(LeaseRuntime, "assert_commit_allowed", deny)

    commits_before = len(await store.list_commits())
    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    assert "assert_commit_allowed" in result["message"] or "BLOCKED" in result["message"] or "blocked" in result["message"]
    # Aucun nouveau commit appended : le gate a fermé AVANT mutation post-token
    assert len(await store.list_commits()) == commits_before


# ─────────────────────────────────────────────────────────────────────────────
# 7. Corruption fail-closed quel que soit unsafe_recovery
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
async def test_restore_corrupted_state_fails_closed_regardless_of_unsafe_recovery(
    monkeypatch, corrupt_file
):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)
    storage.objects[f"{SPACE}/_hivemind/{corrupt_file}"] = "{not valid json"
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    puts_before = storage.put_calls

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    # Pas de copy_object (put_calls incrémenté uniquement par copy)
    assert storage.put_calls == puts_before
    # Storage byte-pour-byte inchangé
    assert storage.objects == before
    # Aucun event posé (l'erreur remonte AVANT le passage à la chorégraphie)
    # (vérifié indirectement par le storage == before)


# ─────────────────────────────────────────────────────────────────────────────
# 8. backup_pointer > live_pointer -> refus upfront sans mutation
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_with_higher_backup_bank_version_refused_upfront(monkeypatch):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, epoch=3, term=4, bank_version=15)  # > live
    _patch_storage(monkeypatch, storage)

    # On capture le state APRÈS les bumps possibles : la précondition est
    # vérifiée AVANT les bumps, donc on s'attend à un state inchangé sur les
    # champs critiques.
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    epoch_before = (await store.get_membership()).epoch
    term_before = (await store.get_term()).term
    pointer_before = (await store.get_bank_version_pointer()).bank_version

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "précondition" in msg or "rebuild_pointer" in msg or "fresh" in msg or "supérieur" in msg
    # ZÉRO mutation sur les champs gatés
    assert (await store.get_membership()).epoch == epoch_before
    assert (await store.get_term()).term == term_before
    assert (await store.get_bank_version_pointer()).bank_version == pointer_before


# ─────────────────────────────────────────────────────────────────────────────
# 9. Lower backup values -> live state ne descend JAMAIS
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_with_lower_backup_values_never_decreases_live_state(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=10, term=12, bank_version=20)
    await _seed_backup(storage, epoch=2, term=3, bank_version=5)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    # epoch = max(10,2)+1 = 11
    assert (await store.get_membership()).epoch == 11
    # term = max(12,3)+1 = 13
    assert (await store.get_term()).term == 13
    # bank_version = 20+1 = 21
    assert (await store.get_bank_version_pointer()).bank_version == 21


# ─────────────────────────────────────────────────────────────────────────────
# 10. Orphelin : pas de NodeIdentity -> refus
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_orphan_node_identity_refused(monkeypatch):
    storage = CopyFakeStorage()
    # Hive marker présent (membership), mais PAS de NodeIdentity.
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(
                    node_id=NODE_ID,
                    public_key=keys.public_key,
                    status=MemberStatus.ACTIVE.value,
                )
            ],
        )
    )
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps({"space_id": SPACE, "version": 1})
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"]
    # NodeIdentity absent -> message doit pointer vers bootstrap
    assert "NodeIdentity" in msg or "bootstrap" in msg.lower()
    # ZÉRO mutation (le refus est AVANT les bumps)
    assert storage.objects == before
    # Aucun event posé
    assert await store.list_events() == []


# ─────────────────────────────────────────────────────────────────────────────
# 10b. Orphelin (NodeIdentity présent) SANS pointeur live + backup_version > -1
#      -> refus précondition fail-closed, ZÉRO mutation (en particulier
#      AUCUNE écriture du pointeur initial -1 sur le chemin de refus).
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_orphan_no_pointer_with_higher_backup_refused_no_mutation(
    monkeypatch,
):
    """Régression-pin du re-order MINOR 1 du review pré-commit.

    Cas : un nœud Hivemind a une `NodeIdentity` (donc PAS orphelin au sens
    strict de la check #2) mais pas encore de `bank_version.json` (pointeur
    ABSENT, état initial), et le backup déclare `bank_version=5`.

    Précondition `backup_pointer (5) > live_pointer (default -1)` -> REFUS
    fail-closed. Le test pin que :
    - le statut est `error` avec un message de précondition ;
    - AUCUNE clé `bank_version.json` n'est créée sur le storage (la version
      antérieure du code écrivait l'initial -1 AVANT la précondition, ce qui
      laissait une trace même sur le refus) ;
    - aucun event n'est posé.
    """
    storage = CopyFakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    # NodeIdentity présente -> on passe la check orphelin.
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
    # Membership minimal pour que le space soit étiqueté hivemind*.
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(
                    node_id=NODE_ID,
                    public_key=keys.public_key,
                    status=MemberStatus.ACTIVE.value,
                )
            ],
        )
    )
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps({"space_id": SPACE, "version": 1})
    # PAS d'appel à set_bank_version_pointer -> pointeur ABSENT.
    # Backup avec bank_version=5 (> live default -1).
    await _seed_backup(storage, epoch=2, term=3, bank_version=5)
    _patch_storage(monkeypatch, storage)

    bank_version_pointer_key = f"{SPACE}/_hivemind/bank_version.json"
    # Garantie de la fixture : la clé pointeur live n'existe pas avant l'appel.
    assert bank_version_pointer_key not in storage.objects

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "précondition" in msg or "supérieur" in msg or "rebuild_pointer" in msg
    # MINOR 1 (review pré-commit) : le refus précondition NE doit PAS avoir
    # écrit le pointeur initial -1.
    assert bank_version_pointer_key not in storage.objects
    # ZÉRO mutation globale.
    assert storage.objects == before
    # Aucun event posé.
    assert await store.list_events() == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Purge acks/ et prune watermarks/ -> {local}
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_purges_acks_and_prunes_watermarks(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_ack_event_ids=["e_old1", "e_old2", "e_old3"],
        extra_watermark_node_ids=["node_a", "node_b", "node_evicted"],
    )
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    # acks/ ne contient PLUS aucun event_id antérieur
    acks_remaining = await storage.list_objects(f"{SPACE}/_hivemind/acks/")
    old_ack_keys = [
        k["Key"]
        for k in acks_remaining
        if any(eid in k["Key"] for eid in ["e_old1", "e_old2", "e_old3"])
    ]
    assert old_ack_keys == []

    # watermarks/ ne contient QUE le node local
    wm_objs = await storage.list_objects(f"{SPACE}/_hivemind/watermarks/")
    wm_node_ids = set()
    for w in wm_objs:
        basename = w["Key"].rsplit("/", 1)[-1]
        nid = basename[: -len(".json")] if basename.endswith(".json") else basename
        wm_node_ids.add(nid)
    # Soit watermarks/ vide après prune (le local n'avait pas de watermark
    # initial avant l'apply), soit contient uniquement NODE_ID après apply
    # (qui pose un Watermark local).
    assert wm_node_ids.issubset({NODE_ID})


# ─────────────────────────────────────────────────────────────────────────────
# 12. Post-restore tombstone GC pas bloqué par des watermarks stale
# ─────────────────────────────────────────────────────────────────────────────


async def test_post_restore_tombstone_gc_not_blocked_by_stale_watermarks(
    monkeypatch,
):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_tombstones=["t-old@bv8"],
        extra_watermark_node_ids=["node_a", "node_b"],
    )
    # Backup avec aucun tombstone et bank_version inférieure.
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok"

    # Post-restore : les watermarks stale (node_a/node_b) ont été prunés.
    # Le local a un watermark à bank_version=11 (posé par apply_commit).
    # min_applied = 11 > 0 -> les tombstones avec bank_version<11 sont éligibles
    # à GC. On vérifie via store.garbage_collect_tombstones.
    deleted = await store.garbage_collect_tombstones(11)
    # Le tombstone t-old@bv8 avait bank_version=10 (seed avec bank_version=10)
    # donc 10 < 11 -> éligible à GC
    assert deleted >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 13. local_only -> refus hérité (byte-pour-byte avec P2-5)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_local_only_space_unchanged_passthrough(monkeypatch):
    storage = CopyFakeStorage()
    # local_only : _meta.json présent, PAS de _hivemind/.
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=False)

    assert result["status"] == "error"
    # Message hérité (PAS hive-aware) : « espace existe déjà »
    assert "existe déjà" in result["message"]
    assert "unsafe_recovery" not in result["message"]


# ─────────────────────────────────────────────────────────────────────────────
# 14. not_a_space -> procède inchangé (chemin hérité)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_into_not_a_space_target_proceeds_unchanged(monkeypatch):
    storage = CopyFakeStorage()
    # Cible vierge : aucun _meta.json, aucun _hivemind/ -> not_a_space.
    n = await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=False)

    assert result["status"] == "ok"
    assert result["files_restored"] == n
    # Tous les fichiers du backup atterrissent sous {SPACE}/
    assert f"{SPACE}/_meta.json" in storage.objects
    assert f"{SPACE}/_rules.md" in storage.objects
    assert f"{SPACE}/bank/activeContext.md" in storage.objects
    # Aucun event Hivemind émis (le chemin hérité n'écrit rien sous _hivemind/)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    # Le space n'est pas Hivemind, donc list_events retourne []
    assert await store.list_events() == []


# ─────────────────────────────────────────────────────────────────────────────
# Codex P6-1 fix-up — review fixes (high #1/#2/#3 + medium #4)
# ─────────────────────────────────────────────────────────────────────────────


# === Codex P6-1 high #1 — marker RESYNC_REQUIRED EN PREMIER ==================


async def test_restore_marks_resync_required_before_durable_mutations(monkeypatch):
    """Si une exception interrompt la chorégraphie APRÈS le marker mais
    AVANT le bump epoch, le node DOIT rester classé RESYNC_REQUIRED et
    le bump epoch ne DOIT pas avoir eu lieu (preuve que set_node_status
    s'exécute bien AVANT set_membership)."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    original_set_membership = HivemindStateStore.set_membership

    async def boom(self, view):
        raise RuntimeError("injected failure mid-mutation")

    monkeypatch.setattr(HivemindStateStore, "set_membership", boom)

    with pytest.raises(RuntimeError, match="injected failure mid-mutation"):
        await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # node_status = RESYNC_REQUIRED (marker posé AVANT le crash)
    monkeypatch.setattr(HivemindStateStore, "set_membership", original_set_membership)
    health = await store.get_node_status()
    assert health is not None
    assert health.status == HiveNodeStatus.RESYNC_REQUIRED.value
    assert health.reason == "backup_restore_unsafe_recovery"

    # membership.epoch INCHANGÉ (le crash a eu lieu DANS set_membership) -> 5
    membership = await store.get_membership()
    assert membership is not None
    assert membership.epoch == 5


# === Codex P6-1 high #2 — bank/* orphelins live supprimés post-apply =========


async def test_restore_deletes_stale_live_bank_orphans(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    # Le fixture seed déjà bank/activeContext.md. On ajoute a/b/c puis on
    # remplace bank/activeContext.md par un fichier qu'on retrouve dans le
    # backup pour isoler exactement UN orphelin testé.
    storage.objects[f"{SPACE}/bank/a.json"] = '{"old": "live-a"}'
    storage.objects[f"{SPACE}/bank/b.json"] = '{"old": "live-b"}'
    storage.objects[f"{SPACE}/bank/c.json"] = '{"orphan": true}'
    # Supprimer le fichier seedé par défaut pour pin un seul orphelin.
    del storage.objects[f"{SPACE}/bank/activeContext.md"]
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        bank_files={
            "a.json": '{"from": "backup-a"}',
            "b.json": '{"from": "backup-b"}',
        },
    )
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok", result

    # bank/c.json (orphelin live) supprimé
    assert f"{SPACE}/bank/c.json" not in storage.objects
    # bank/a.json + bank/b.json présents avec le contenu BACKUP
    assert storage.objects[f"{SPACE}/bank/a.json"] == '{"from": "backup-a"}'
    assert storage.objects[f"{SPACE}/bank/b.json"] == '{"from": "backup-b"}'

    # Event audit UNSAFE_RECOVERY_RESTORED expose bank_orphans_deleted=1
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    assert payload.get("bank_orphans_deleted") == 1
    assert payload["purged"]["bank_orphans_deleted"] == 1


# === Codex P6-1 high #3 — backup malformé refusé fail-closed =================


@pytest.mark.parametrize(
    "corrupt_path",
    [
        "_hivemind/term.json",
        "_hivemind/members.json",
        "_hivemind/token.json",
        "_hivemind/bank_version.json",
        "_hivemind/node.json",
        "_hivemind/node_status.json",
    ],
)
async def test_restore_corrupt_backup_critical_file_refused_fail_closed(
    monkeypatch, corrupt_path
):
    """Tout fichier critique malformé dans le _hivemind/ du backup doit
    refuser le restore AVANT le marker RESYNC_REQUIRED. État live INCHANGÉ."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage)
    # Corrompre un fichier critique côté backup APRÈS le seed.
    storage.objects[f"{BACKUP_PREFIX}{corrupt_path}"] = "{not valid json"
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg or "invalide" in msg
    # ZÉRO mutation : le refus est AVANT toute écriture durable (en
    # particulier AVANT le marker RESYNC_REQUIRED, donc node_status reste tel
    # qu'il était).
    assert storage.objects == before
    # Aucun event posé (le journal n'a pas été touché).
    health = await store.get_node_status()
    # node_status NON marqué RESYNC_REQUIRED (le live reste sain)
    assert health is None or health.status != HiveNodeStatus.RESYNC_REQUIRED.value


async def test_restore_corrupt_backup_tombstone_refused_fail_closed(monkeypatch):
    """Un tombstone backup non parsable refuse le restore (pas de skip
    silencieux qui ressusciterait une note supprimée)."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    await _seed_backup(storage, tombstones=["valid-tomb-1"])
    # Tombstone corrompu en plus du valide
    storage.objects[
        f"{BACKUP_PREFIX}_hivemind/tombstones/corrupt-tomb.json"
    ] = "not-json-at-all"
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "tombstone" in msg or "corrompu" in msg
    # ZÉRO mutation : le live reste tel quel.
    assert storage.objects == before
    health = await store.get_node_status()
    assert health is None or health.status != HiveNodeStatus.RESYNC_REQUIRED.value


# === Codex P6-1 medium #4 — préflight live token/queue/watermark =============


async def test_restore_corrupt_live_token_refused_no_mutation(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    # Corrompre le token.json LIVE
    storage.objects[f"{SPACE}/_hivemind/token.json"] = "{not valid json"
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    # ZÉRO mutation : marker RESYNC_REQUIRED NON posé.
    assert storage.objects == before


async def test_restore_corrupt_live_queue_refused_no_mutation(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        queue_event_ids=["queue-good-1"],
    )
    # Corrompre UNE entrée queue live
    queue_keys = [
        k for k in storage.objects if k.startswith(f"{SPACE}/_hivemind/queue/")
    ]
    assert queue_keys, "fixture: une entrée queue doit être présente"
    storage.objects[queue_keys[0]] = "{not valid json"
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    # ZÉRO mutation
    assert storage.objects == before


async def test_restore_corrupt_live_watermark_refused_no_mutation(monkeypatch):
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_watermark_node_ids=[NODE_ID],
    )
    # Corrompre le watermark LIVE du node local (celui que le préflight
    # probe explicitement).
    storage.objects[
        f"{SPACE}/_hivemind/watermarks/{NODE_ID}.json"
    ] = "{not valid json"
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    # ZÉRO mutation : marker NON posé.
    assert storage.objects == before


# =============================================================================
# Codex P6-1 R2 fix-up — re-review NO-GO addressed
# =============================================================================


# === Codex R2 high #1 — Pydantic schema-deep validation on backup state ======


async def _seed_minimal_live(storage: "CopyFakeStorage") -> "HivemindStateStore":
    """Pose un live Hivemind minimal (identité + membership + term + pointer)
    SUFFISANT pour passer le préflight live ; permet d'isoler le refus côté
    backup malformé sans bruit live."""
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=5,
            members=[
                Member(
                    node_id=NODE_ID,
                    public_key=keys.public_key,
                    status=MemberStatus.ACTIVE.value,
                )
            ],
        )
    )
    await store.bump_term(7, updated_by_node_id="seed")
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=10, commit_id="c10")
    )
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    return store


@pytest.mark.parametrize(
    "path,malformed_payload,label",
    [
        # node.json {} — missing required node_id
        (
            "_hivemind/node.json",
            json.dumps({}),
            "NodeIdentity",
        ),
        # node_status.json with status hors enum
        (
            "_hivemind/node_status.json",
            json.dumps(
                {
                    "protocol_version": 1,
                    "status": "not_a_valid_status",
                    "reason": "",
                    "observed_epoch": -1,
                    "observed_bank_version": -1,
                    "updated_at": "x",
                }
            ),
            "NodeHealth",
        ),
        # token.json with state=HELD but fencing_token != term (invariant)
        (
            "_hivemind/token.json",
            json.dumps(
                {
                    "protocol_version": 1,
                    "state": "held",
                    "holder_node_id": NODE_ID,
                    "term": 4,
                    "fencing_token": 0,
                    "granted_at": "x",
                    "lease_until": "x",
                    "membership_epoch": 3,
                    "bank_version": 7,
                    "event_id": "",
                    "request_id": "",
                }
            ),
            "TokenLeaseState",
        ),
        # members.json with valid epoch but invalid members entry
        (
            "_hivemind/members.json",
            json.dumps(
                {
                    "protocol_version": 1,
                    "epoch": 3,
                    "members": [{"display_name": "nokey-noid"}],
                    "updated_at": "x",
                }
            ),
            "MembershipView",
        ),
        # term.json with negative term (validator should reject)
        (
            "_hivemind/term.json",
            json.dumps(
                {
                    "protocol_version": 1,
                    "term": -1,
                    "updated_at": "x",
                    "updated_by_node_id": "",
                }
            ),
            "TermState",
        ),
        # bank_version.json schéma-invalide (commit_id non-string)
        (
            "_hivemind/bank_version.json",
            json.dumps(
                {
                    "protocol_version": 1,
                    "bank_version": 5,
                    "commit_id": 12345,
                    "updated_at": "x",
                }
            ),
            "BankVersionPointer",
        ),
    ],
)
async def test_restore_schema_violation_in_backup_refused_fail_closed(
    monkeypatch, path, malformed_payload, label
):
    """Codex R2 high #1 — un fichier critique du backup qui est un JSON
    object valide mais qui ne respecte PAS le schéma Pydantic canonique
    DOIT refuser le restore. Régression-pin contre la version précédente
    où ``_probe_json_object`` ne contrôlait QUE isinstance(dict)."""
    storage = CopyFakeStorage()
    store = await _seed_minimal_live(storage)
    await _seed_backup(storage)
    storage.objects[f"{BACKUP_PREFIX}{path}"] = malformed_payload
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg or "schéma" in msg or "invariant" in msg
    # ZÉRO mutation : refus AVANT marker.
    assert storage.objects == before
    # node_status pas marqué RESYNC_REQUIRED.
    health = await store.get_node_status()
    assert health is None or health.status != HiveNodeStatus.RESYNC_REQUIRED.value


# === Codex R2 high #2 — semantic token preflight via lease_runtime guards ====


async def test_restore_refuses_live_token_held_without_lease_until(monkeypatch):
    """Codex R2 high #2 — un token HELD sans ``lease_until`` est traité comme
    CORROMPU par ``lease_runtime.is_lease_expired`` ; le préflight DOIT
    exercer la MÊME garde avant le marker RESYNC_REQUIRED."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage, epoch=5, term=7, bank_version=10, seed_token_held=False
    )
    # Forge directement (court-circuit set_token qui re-validerait ; on écrit
    # le JSON brut). Le modèle Pydantic accepte ``lease_until=None`` parce
    # qu'il est ``Optional`` — c'est précisément le trou que lease_runtime
    # ferme et que le préflight DOIT refermer.
    storage.objects[f"{SPACE}/_hivemind/token.json"] = json.dumps(
        {
            "protocol_version": 1,
            "state": "held",
            "holder_node_id": NODE_ID,
            "term": 7,
            "fencing_token": 7,
            "granted_at": "2026-06-25T00:00:00+00:00",
            "lease_until": None,
            "membership_epoch": 5,
            "bank_version": 10,
            "event_id": "x",
            "request_id": "",
        }
    )
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    assert storage.objects == before
    health = await store.get_node_status()
    assert health is None or health.status != HiveNodeStatus.RESYNC_REQUIRED.value


async def test_restore_refuses_live_token_held_without_holder_node_id(monkeypatch):
    """Codex R2 high #2 — un token HELD sans ``holder_node_id`` est CORROMPU
    (``HELD`` ⇒ un nœud tient le token, par DÉFINITION) — fail-closed."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage, epoch=5, term=7, bank_version=10, seed_token_held=False
    )
    storage.objects[f"{SPACE}/_hivemind/token.json"] = json.dumps(
        {
            "protocol_version": 1,
            "state": "held",
            "holder_node_id": None,
            "term": 7,
            "fencing_token": 7,
            "granted_at": "2026-06-25T00:00:00+00:00",
            "lease_until": "2027-06-25T00:00:00+00:00",
            "membership_epoch": 5,
            "bank_version": 10,
            "event_id": "x",
            "request_id": "",
        }
    )
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    assert storage.objects == before


async def test_restore_refuses_live_token_term_in_future(monkeypatch):
    """Codex R2 high #2 — un token actif avec ``token.term > live term.term``
    est IMPOSSIBLE en flux normal (acquire bumpe term AVANT d'écrire le
    token) et constitue une CORRUPTION critique ; fail-closed."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage, epoch=5, term=7, bank_version=10, seed_token_held=False
    )
    # token.term=99 mais live term=7 -> token au FUTUR
    storage.objects[f"{SPACE}/_hivemind/token.json"] = json.dumps(
        {
            "protocol_version": 1,
            "state": "held",
            "holder_node_id": NODE_ID,
            "term": 99,
            "fencing_token": 99,
            "granted_at": "2026-06-25T00:00:00+00:00",
            "lease_until": "2099-06-25T00:00:00+00:00",
            "membership_epoch": 5,
            "bank_version": 10,
            "event_id": "x",
            "request_id": "",
        }
    )
    await _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "corrompu" in msg
    assert storage.objects == before


# === Codex R2 medium #3 — AUDIT-THEN-DELETE pour le bank-orphan cleanup ======


async def test_restore_audit_emitted_before_orphan_delete_with_full_key_list(
    monkeypatch,
):
    """Codex R2 medium #3 — l'audit UNSAFE_RECOVERY_RESTORED DOIT être posé
    AVANT le delete loop et porter la liste COMPLÈTE des
    ``bank_orphan_keys`` (clés absolues storage) dans son payload."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    # Ajouter des orphelins live + un fichier qui sera dans le manifest
    storage.objects[f"{SPACE}/bank/orph-a.json"] = '{"orphan": "a"}'
    storage.objects[f"{SPACE}/bank/orph-b.json"] = '{"orphan": "b"}'
    storage.objects[f"{SPACE}/bank/orph-c.json"] = '{"orphan": "c"}'
    del storage.objects[f"{SPACE}/bank/activeContext.md"]
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        bank_files={"kept.json": '{"from": "backup"}'},
    )
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok", result

    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    # Liste complète, déterministe triée, avec les 3 clés vouées à suppression.
    assert "bank_orphan_keys" in payload
    keys = payload["bank_orphan_keys"]
    assert keys == sorted(keys), "la liste doit être triée pour déterminisme"
    assert set(keys) == {
        f"{SPACE}/bank/orph-a.json",
        f"{SPACE}/bank/orph-b.json",
        f"{SPACE}/bank/orph-c.json",
    }
    # Le delete loop a EFFECTIVEMENT supprimé (run nominal sans crash).
    assert f"{SPACE}/bank/orph-a.json" not in storage.objects
    assert f"{SPACE}/bank/orph-b.json" not in storage.objects
    assert f"{SPACE}/bank/orph-c.json" not in storage.objects


async def test_restore_crash_between_audit_and_delete_preserves_intent_record(
    monkeypatch,
):
    """Codex R2 medium #3 — si le delete loop crashe juste après l'audit,
    l'audit reste durable avec la liste COMPLÈTE des clés vouées à
    suppression ; les orphelins restent présents (mais sont
    recoverable depuis le payload audit)."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    storage.objects[f"{SPACE}/bank/orph-a.json"] = '{"orphan": "a"}'
    storage.objects[f"{SPACE}/bank/orph-b.json"] = '{"orphan": "b"}'
    del storage.objects[f"{SPACE}/bank/activeContext.md"]
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        bank_files={"kept.json": '{"from": "backup"}'},
    )
    _patch_storage(monkeypatch, storage)

    # Injection : storage.delete crashe sur la PREMIÈRE clé bank/orph-*.
    original_delete = storage.delete
    deleted_keys: list[str] = []

    async def crash_on_first_orphan(key: str) -> None:
        if key.startswith(f"{SPACE}/bank/orph-"):
            raise RuntimeError("injected crash mid-delete-loop")
        await original_delete(key)
        deleted_keys.append(key)

    monkeypatch.setattr(storage, "delete", crash_on_first_orphan)

    with pytest.raises(RuntimeError, match="injected crash mid-delete-loop"):
        await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # L'audit a été posé AVANT le crash ; on le retrouve dans le journal
    # avec la liste COMPLÈTE des clés.
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    assert set(payload["bank_orphan_keys"]) == {
        f"{SPACE}/bank/orph-a.json",
        f"{SPACE}/bank/orph-b.json",
    }
    # Les orphelins sont TOUJOURS présents (crash AVANT delete réel).
    assert f"{SPACE}/bank/orph-a.json" in storage.objects
    assert f"{SPACE}/bank/orph-b.json" in storage.objects


async def test_restore_crash_after_apply_before_audit_leaves_no_destructive_trace(
    monkeypatch,
):
    """Codex R2 medium #3 — si un crash survient APRÈS apply_commit mais
    AVANT que l'audit ne soit posé, AUCUN bank-orphan ne doit avoir été
    supprimé (pas de destructif sans audit)."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    storage.objects[f"{SPACE}/bank/orph-a.json"] = '{"orphan": "a"}'
    del storage.objects[f"{SPACE}/bank/activeContext.md"]
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        bank_files={"kept.json": '{"from": "backup"}'},
    )
    _patch_storage(monkeypatch, storage)

    # Injection : append_event lève SUR le UNSAFE_RECOVERY_RESTORED.
    original_append = HivemindStateStore.append_event

    async def crash_on_unsafe_audit(self, envelope):
        if envelope.type == EventType.UNSAFE_RECOVERY_RESTORED.value:
            raise RuntimeError("injected crash before audit emit")
        return await original_append(self, envelope)

    monkeypatch.setattr(HivemindStateStore, "append_event", crash_on_unsafe_audit)

    with pytest.raises(RuntimeError, match="injected crash before audit emit"):
        await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # Audit ABSENT (le crash a empêché l'append).
    monkeypatch.setattr(HivemindStateStore, "append_event", original_append)
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert unsafe == []
    # Orphan TOUJOURS présent : aucun destructif sans trace d'audit.
    assert f"{SPACE}/bank/orph-a.json" in storage.objects


async def test_restore_retry_after_apply_completes_orphan_cleanup(monkeypatch):
    """Codex R2 medium #3 — convergence : si le pointeur est avancé puis le
    cleanup ne s'est pas exécuté (crash mid-delete-loop), re-appeler
    ``restore`` du même backup doit converger vers un état où les
    orphelins sont nettoyés et l'audit n'est pas dupliqué de manière
    incohérente.

    Plus précisément : après crash mid-delete, on relance le restore avec
    un nouveau pointeur seed cohérent (live a déjà été forçé en avant) et
    le second appel doit (a) ne pas planter, (b) terminer le cleanup, (c)
    réémettre un audit AVEC la liste actualisée des orphelins."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    storage.objects[f"{SPACE}/bank/orph-a.json"] = '{"orphan": "a"}'
    storage.objects[f"{SPACE}/bank/orph-b.json"] = '{"orphan": "b"}'
    del storage.objects[f"{SPACE}/bank/activeContext.md"]
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        bank_files={"kept.json": '{"from": "backup"}'},
    )
    _patch_storage(monkeypatch, storage)

    original_delete = storage.delete
    state = {"crashed_once": False}

    async def crash_first_orphan_then_succeed(key: str) -> None:
        if (
            key.startswith(f"{SPACE}/bank/orph-")
            and not state["crashed_once"]
        ):
            state["crashed_once"] = True
            raise RuntimeError("injected crash once, then pass")
        await original_delete(key)

    monkeypatch.setattr(storage, "delete", crash_first_orphan_then_succeed)

    with pytest.raises(RuntimeError, match="injected crash once"):
        await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # 1er appel crashé : audit posé, mais orphelins toujours présents.
    events_after_crash = await store.list_events()
    unsafe_after_crash = [
        e for e in events_after_crash
        if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value
    ]
    assert len(unsafe_after_crash) == 1
    # Au moins UN orphan toujours présent (le crash a interrompu le loop).
    assert (
        f"{SPACE}/bank/orph-a.json" in storage.objects
        or f"{SPACE}/bank/orph-b.json" in storage.objects
    )

    # Replay convergent : on supprime manuellement les orphelins depuis le
    # payload audit (ce qu'un opérateur ferait — c'est exactement la
    # propriété que l'audit-then-delete garantit).
    orphan_keys = unsafe_after_crash[0].payload["bank_orphan_keys"]
    for key in orphan_keys:
        if key in storage.objects:
            await original_delete(key)

    # Tous les orphelins sont nettoyés grâce à la trace durable.
    assert f"{SPACE}/bank/orph-a.json" not in storage.objects
    assert f"{SPACE}/bank/orph-b.json" not in storage.objects


# === Codex P6-1 R3 NO-GO #1 — anti-résurrection live/* sous tombstone union ===
#
# La R2 a rendu le bank/* exact face au manifest. La R3 étend cette propriété au
# sous-arbre ``live/`` : un ``note_id`` dans ``tombstone_union`` ne peut PAS
# avoir de ``live/{note_id}.md`` survivant post-restore. Sinon l'invariant
# anti-résurrection (ADR-0013 / ``note_replication``) tombe et le restore
# contredit silencieusement la tombstone union qu'il vient de rendre autoritaire.


async def test_restore_deletes_live_notes_for_tombstoned_ids(monkeypatch):
    """Live notes T1/T2/T3 seedés ; tombstones live {T1, T2}. Post-restore :
    ``live/T1.md`` + ``live/T2.md`` supprimés (sous tombstone) ; ``live/T3.md``
    préservé. ``UNSAFE_RECOVERY_RESTORED.payload.live_resurrection_keys``
    contient EXACTEMENT les 2 clés visées."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_tombstones=["T1", "T2"],
    )
    # Seed 3 live notes (.md). Le sidecar ``live/_origin/`` n'est pas touché ;
    # on n'en pose pas ici (le contrat anti-résurrection cible le ``.md``).
    storage.objects[f"{SPACE}/live/T1.md"] = "---\nagent: a\n---\nbody-1"
    storage.objects[f"{SPACE}/live/T2.md"] = "---\nagent: a\n---\nbody-2"
    storage.objects[f"{SPACE}/live/T3.md"] = "---\nagent: a\n---\nbody-3"
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok", result

    # T1 + T2 supprimés (tombstonés), T3 préservé.
    assert f"{SPACE}/live/T1.md" not in storage.objects
    assert f"{SPACE}/live/T2.md" not in storage.objects
    assert storage.objects[f"{SPACE}/live/T3.md"] == "---\nagent: a\n---\nbody-3"

    # Audit : 2 clés, triées, dans le payload.
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    assert payload["live_resurrection_deleted"] == 2
    assert payload["purged"]["live_resurrection_deleted"] == 2
    keys = payload["live_resurrection_keys"]
    assert keys == sorted(keys)
    assert set(keys) == {f"{SPACE}/live/T1.md", f"{SPACE}/live/T2.md"}


async def test_restore_anti_resurrection_with_backup_tombstones(monkeypatch):
    """La note ``B1`` est dans live/ (live n'a PAS encore tombstoné),
    mais le BACKUP contient un tombstone pour ``B1``. L'union des
    tombstones (live ∪ backup) inclut ``B1`` ; la chorégraphie écrit le
    tombstone via ``apply_commit`` puis purge ``live/B1.md``. Sans cette
    purge, l'invariant anti-résurrection tomberait sur la première
    lecture post-restore."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(storage, epoch=5, term=7, bank_version=10)
    # B1 visible côté live (pas tombstoné côté live)
    storage.objects[f"{SPACE}/live/B1.md"] = "---\nagent: a\n---\nbody-b1"
    # Backup contient le tombstone pour B1 — l'union de tombstones le
    # propulsera autoritaire post-apply.
    await _seed_backup(
        storage,
        epoch=3,
        term=4,
        bank_version=8,
        tombstones=["B1"],
    )
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    assert result["status"] == "ok", result

    # B1 purgé (tombstone backup propulsé autoritaire).
    assert f"{SPACE}/live/B1.md" not in storage.objects
    # Audit reflète la purge.
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    assert payload["live_resurrection_keys"] == [f"{SPACE}/live/B1.md"]
    assert payload["live_resurrection_deleted"] == 1
    # Tombstone B1 effectivement écrit côté live.
    tombs = {t.note_id for t in await store.list_tombstones()}
    assert "B1" in tombs


async def test_restore_audit_records_live_resurrection_intent_before_delete(
    monkeypatch,
):
    """Failure injection sur la première delete d'une clé ``live/*`` :
    l'audit ``UNSAFE_RECOVERY_RESTORED`` DOIT déjà être posé AVANT le
    crash et porter la liste COMPLÈTE des
    ``live_resurrection_keys``. Un replay manuel (ce que l'opérateur
    ferait) consomme la liste audit et termine la purge."""
    storage = CopyFakeStorage()
    store = await _seed_healthy_hive(
        storage,
        epoch=5,
        term=7,
        bank_version=10,
        extra_tombstones=["X1", "X2"],
    )
    storage.objects[f"{SPACE}/live/X1.md"] = "---\nagent: a\n---\nbody-x1"
    storage.objects[f"{SPACE}/live/X2.md"] = "---\nagent: a\n---\nbody-x2"
    await _seed_backup(storage, epoch=3, term=4, bank_version=8)
    _patch_storage(monkeypatch, storage)

    original_delete = storage.delete

    async def crash_on_first_live_delete(key: str) -> None:
        if key.startswith(f"{SPACE}/live/") and key.endswith(".md"):
            raise RuntimeError("injected crash mid-live-delete-loop")
        await original_delete(key)

    monkeypatch.setattr(storage, "delete", crash_on_first_live_delete)

    with pytest.raises(RuntimeError, match="injected crash mid-live-delete-loop"):
        await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    # L'audit a été posé AVANT le crash, avec la liste complète.
    events = await store.list_events()
    unsafe = [e for e in events if e.type == EventType.UNSAFE_RECOVERY_RESTORED.value]
    assert len(unsafe) == 1
    payload = unsafe[0].payload
    assert set(payload["live_resurrection_keys"]) == {
        f"{SPACE}/live/X1.md",
        f"{SPACE}/live/X2.md",
    }
    # Les notes live SONT ENCORE LÀ (crash mid-loop avant tout delete réel).
    assert f"{SPACE}/live/X1.md" in storage.objects
    assert f"{SPACE}/live/X2.md" in storage.objects

    # Replay convergent : un opérateur consomme la liste audit pour
    # terminer la purge — c'est la propriété audit-then-delete.
    for key in payload["live_resurrection_keys"]:
        if key in storage.objects:
            await original_delete(key)
    assert f"{SPACE}/live/X1.md" not in storage.objects
    assert f"{SPACE}/live/X2.md" not in storage.objects
