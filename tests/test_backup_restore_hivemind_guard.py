# -*- coding: utf-8 -*-
"""
Tests P2-5 (issue #37, ADR-0014 Accepted) — garde Hivemind refus-par-défaut sur
``BackupService.restore``.

La garde classe l'espace cible via ``hive_status_label`` (détection READ-ONLY,
ADR-0008) AVANT le check hérité « ``{space}/_meta.json`` existe -> refus » :

- label partagé/unsafe (``hivemind_healthy`` / ``hivemind_blocked`` / ``unsafe``
  / ``resync_required``) sans ``unsafe_recovery`` -> REFUS (status ``error``)
  avec message hive-aware (≠ refus hérité « espace existe déjà ») ;
- même cas avec ``unsafe_recovery=True`` -> P6-1 active la chorégraphie de
  forçage-en-avant (couverte exhaustivement par tests/test_backup_restore_hivemind.py) ;
- ``local_only`` -> comportement HÉRITÉ inchangé (refus « espace existe déjà ») ;
- ``not_a_space`` -> procède inchangé ;
- corruption (node/members/node_status.json) -> REFUS fail-closed quel que soit
  ``unsafe_recovery`` (jamais de copie sur un état illisible) ;
- la garde n'écrit RIEN sous ``_hivemind/`` sur le chemin refusé.

Déterministe, OFFLINE, fake-backed. Réutilise le ``CopyFakeStorage`` partagé et
le pattern seed-healthy-hive de ``tests/test_hive_status_label.py``. On patche
``live_mem.core.backup.get_storage`` (le service binde le singleton localement
via ``from .storage import get_storage``).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from live_mem.core.backup import BackupService
from live_mem.core.hivemind import (
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    NodeIdentity,
    generate_peer_keypair,
)
from tests.fakes.backup_storage import CopyFakeStorage, patch_backup_storage as _patch_storage
from tests.test_hivemind_state import FakeStorage

SPACE = "p2-5-space"
NODE_ID = "nodep25000000000000000000000000aa"
TS = "2026-06-17T12-00-00"
BACKUP_ID = f"{SPACE}/{TS}"
BACKUP_PREFIX = f"_backups/{SPACE}/{TS}/"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de seeding
# ─────────────────────────────────────────────────────────────────────────────


def _seed_backup(storage: FakeStorage) -> int:
    """Pose un backup valide sous ``_backups/{SPACE}/{TS}/`` (3 objets).

    Returns le nombre d'objets posés (pour asserter ``files_restored``).
    """
    storage.objects[f"{BACKUP_PREFIX}_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    storage.objects[f"{BACKUP_PREFIX}_rules.md"] = "# rules"
    storage.objects[f"{BACKUP_PREFIX}live/note-1.md"] = "hello"
    return 3


async def _seed_orphan_hive(storage: FakeStorage) -> None:
    """``_hivemind/`` présent (node.json + 1 membre ACTIVE) mais PAS de
    ``{SPACE}/_meta.json`` -> ``hive_status_label`` == ``unsafe`` (orphelin)."""
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
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


async def _seed_healthy_hive(storage: FakeStorage) -> HivemindStateStore:
    """node.json + membre ACTIVE + ``{SPACE}/_meta.json`` -> ``hivemind_healthy``."""
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
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
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    return store


def _hivemind_keys(storage: FakeStorage) -> set[str]:
    return {k for k in storage.objects if k.startswith(f"{SPACE}/_hivemind/")}


# ─────────────────────────────────────────────────────────────────────────────
# (a) ORPHELIN sans flag -> refus hive-aware, 0 fichier restauré, stockage intact
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_orphan_hive_without_flag_is_refused_and_unmutated(
    monkeypatch,
):
    # CopyFakeStorage : SANS la garde, restore() atteindrait copy_object et
    # PROCÉDERAIT (status 'ok'), prouvant que la garde fait passer proceed->refus.
    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    puts_before, deletes_before = storage.put_calls, storage.delete_calls

    result = await BackupService().restore(BACKUP_ID)  # unsafe_recovery défaut False

    assert result["status"] == "error"
    # Hive-aware message, distinct from the inherited "space already exists" refusal.
    msg = result["message"]
    assert "unsafe" in msg
    assert "unsafe_recovery" in msg
    assert "already exists" not in msg
    # Pas un succès : aucun fichier restauré.
    assert "files_restored" not in result
    # Aucune nouvelle clé sous {SPACE}/ ; stockage strictement intact.
    assert storage.objects == before
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


# ─────────────────────────────────────────────────────────────────────────────
# (b) ORPHELIN avec unsafe_recovery=True -> procède (copie héritée)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_orphan_hive_with_unsafe_recovery_proceeds(monkeypatch):
    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    n = _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "ok"
    assert result["files_restored"] == n
    # Les objets du backup apparaissent désormais sous {SPACE}/.
    assert storage.objects[f"{SPACE}/_meta.json"] == json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    assert storage.objects[f"{SPACE}/_rules.md"] == "# rules"
    assert storage.objects[f"{SPACE}/live/note-1.md"] == "hello"


# ─────────────────────────────────────────────────────────────────────────────
# (c) LOCAL_ONLY -> refus hérité inchangé (≠ message hive-aware)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_over_local_only_space_still_refused_existing_behavior(
    monkeypatch,
):
    storage = CopyFakeStorage()
    # local_only : _meta.json présent, PAS de _hivemind/.
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID)

    assert result["status"] == "error"
    # This is the inherited "space already exists" refusal, not the hive-aware message.
    assert "already exists" in result["message"]
    assert "unsafe_recovery" not in result["message"]


async def test_restore_over_local_only_with_unsafe_recovery_still_hits_inherited_refusal(
    monkeypatch,
):
    # Même avec unsafe_recovery=True, la garde ne bypasse pas le refus hérité
    # _meta.json-exists pour un local_only (la garde ne change rien pour
    # local_only ; le check hérité s'applique tel quel).
    storage = CopyFakeStorage()
    storage.objects[f"{SPACE}/_meta.json"] = json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "error"
    assert "already exists" in result["message"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) NOT_A_SPACE -> procède inchangé (disaster-restore dans un space vierge)
# ─────────────────────────────────────────────────────────────────────────────


async def test_restore_into_not_a_space_target_proceeds_unchanged(monkeypatch):
    storage = CopyFakeStorage()
    # Cible vierge : aucun _meta.json, aucun _hivemind/ -> label not_a_space.
    n = _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    result = await BackupService().restore(BACKUP_ID)

    assert result["status"] == "ok"
    assert result["files_restored"] == n
    assert storage.objects[f"{SPACE}/_meta.json"] == json.dumps(
        {"space_id": SPACE, "version": 1}
    )
    assert storage.objects[f"{SPACE}/_rules.md"] == "# rules"
    assert storage.objects[f"{SPACE}/live/note-1.md"] == "hello"


async def test_restore_waits_for_lifecycle_then_observes_preparation_fence(
    monkeypatch,
) -> None:
    """A prepare that wins lifecycle cannot be crossed by a stale restore."""

    from live_mem.core.locks import get_lock_manager
    from live_mem.core.reservation_guard import SpaceReservedError

    storage = CopyFakeStorage()
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)
    preparing = False
    checks = 0

    async def preparation_checker(space_id: str) -> None:
        nonlocal checks
        checks += 1
        if space_id == SPACE and preparing:
            raise SpaceReservedError(space_id)

    monkeypatch.setattr(
        "live_mem.core.backup.assert_space_not_reserved", preparation_checker
    )
    lifecycle = get_lock_manager().space_lifecycle(SPACE)
    await lifecycle.acquire()
    task = asyncio.create_task(BackupService().restore(BACKUP_ID))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert checks == 0
        preparing = True
    finally:
        lifecycle.release()

    before = storage.snapshot()
    with pytest.raises(SpaceReservedError):
        await task
    assert checks == 1
    assert storage.objects == before


# ─────────────────────────────────────────────────────────────────────────────
# (e) CORRUPTION -> refus fail-closed quel que soit le flag
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
@pytest.mark.parametrize("flag", [False, True])
async def test_restore_over_corrupted_target_is_refused_fail_closed_regardless_of_flag(
    monkeypatch, corrupt_file, flag
):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)
    # Corrompre un fichier du read-set de détection (cf. le pattern prouvé dans
    # test_hive_status_label.py) : get_node_status lit node_status.json
    # directement, donc la corruption remonte même sans set_node_status.
    storage.objects[f"{SPACE}/_hivemind/{corrupt_file}"] = "{not valid json"
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    puts_before, deletes_before = storage.put_calls, storage.delete_calls

    # Appel SERVICE direct (pas de wrapper safe_error du tool) : sans la garde
    # try/except, CorruptedStateError se propagerait et ferait échouer pytest.
    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=flag)

    assert result["status"] == "error"
    assert "corrupt" in result["message"] or "unreadable" in result["message"]
    # Jamais de copie : aucune nouvelle clé de contenu sous {SPACE}/.
    assert storage.objects == before
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before


# ─────────────────────────────────────────────────────────────────────────────
# (f) la garde n'écrit RIEN sous _hivemind/ sur le chemin refusé
# ─────────────────────────────────────────────────────────────────────────────


async def test_guard_performs_no_write_to_hivemind_on_refused_orphan_path(monkeypatch):
    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    hivemind_before = _hivemind_keys(storage)
    snap_before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID)  # refusé

    assert result["status"] == "error"
    # Aucun put/delete n'a touché une clé {SPACE}/_hivemind/... : le set de clés
    # _hivemind/ est identique et le stockage entier est inchangé.
    assert _hivemind_keys(storage) == hivemind_before
    assert storage.objects == snap_before


async def test_guard_performs_no_write_to_hivemind_on_refused_corrupted_path(
    monkeypatch,
):
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)
    storage.objects[f"{SPACE}/_hivemind/node.json"] = "{not valid json"
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    hivemind_before = _hivemind_keys(storage)
    snap_before = storage.snapshot()

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)  # refusé

    assert result["status"] == "error"
    assert _hivemind_keys(storage) == hivemind_before
    assert storage.objects == snap_before


# ─────────────────────────────────────────────────────────────────────────────
# P5-8 (#16) — explicit pin of the DEFERRED forward-forcing choreography
# ─────────────────────────────────────────────────────────────────────────────


async def test_p5_8_restore_shared_refused_without_unsafe_recovery_pin(monkeypatch):
    """P5-8 capstone scope pin (ADR-0014 (a)): restore over a marked-shared hive
    WITHOUT unsafe_recovery is refused with the hive-aware error — the capstone
    keeps this gate and does NOT wire the forward-forcing choreography. A
    healthy-hive target (not just the orphan) is refused by default."""
    storage = CopyFakeStorage()
    await _seed_healthy_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    result = await BackupService().restore(BACKUP_ID)  # no unsafe_recovery

    assert result["status"] == "error"
    assert "unsafe_recovery" in result["message"]
    assert storage.objects == before  # nothing restored, no field forced


async def test_p6_1_restore_unsafe_recovery_runs_forward_forcing_choreography(
    monkeypatch,
):
    """P6-1 (issue #87, ADR-0014) — la chorégraphie de forçage-en-avant que
    P5-8 avait épinglée comme DÉFÉRÉE est désormais ACTIVE : avec
    ``unsafe_recovery=True`` sur un space Hivemind, ``membership_epoch`` /
    ``term`` / ``bank_version`` montent strictement (le restore ne touche plus
    le bank live via la copie héritée, mais via la chorégraphie commit).

    Ancien rôle de scope-pin de P5-8 : « DID NOT force membership_epoch forward »
    (épingle de la dérivation). P6-1 lève cette dérivation : on assert
    désormais que l'epoch a AVANCÉ (preuve que la chorégraphie tourne)."""
    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    epoch_before = await _read_membership_epoch(storage)

    result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)

    assert result["status"] == "ok"
    # P6-1 : epoch FORCÉ EN AVANT (≠ comportement P5-8 qui le laissait inchangé)
    assert (await _read_membership_epoch(storage)) > epoch_before


async def test_p10_3_unsafe_recovery_refused_during_source_pairing_activation(monkeypatch):
    """P10-3 fence: an unsafe recovery replaces the roster with [self] at a bumped
    epoch; running it while a SOURCE Mesh pairing is mid-activation would drop a
    target whose e+2 activation is still in flight (it self-promotes) -> split. The
    restore must be REFUSED before any mutation (membership epoch unchanged)."""
    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    epoch_before = await _read_membership_epoch(storage)
    before = storage.snapshot()

    async def activating(space_id: str, ignore_pair_id) -> None:
        # A source pairing for this space is mid-activation (operator restore is
        # never a pairing's own transition, so ignore_pair_id is always None here).
        if space_id == SPACE:
            raise PairingActivationError(space_id)

    register_pairing_activation_checker(activating)
    try:
        result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    finally:
        clear_pairing_activation_checker()

    assert result["status"] == "error"
    assert "mid-activation" in result["message"]
    # Refused BEFORE the RESYNC marker / roster write: storage fully unmutated.
    assert (await _read_membership_epoch(storage)) == epoch_before
    assert storage.objects == before


async def test_p10_3_unsafe_recovery_phase2_refuses_pairing_armed_in_window(monkeypatch):
    """Phase-2 re-check (finding #5a): a pairing that arms AFTER the phase-1
    preflight but before the roster write is caught under the membership lock. A
    call-count-gated checker passes phase-1 and raises on phase-2; the restore
    aborts and the roster is NOT replaced with [self]-only."""
    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    calls = {"n": 0}

    async def arm_on_second_call(space_id: str, ignore_pair_id) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # phase-1 passes, phase-2 (under the lock) refuses
            raise PairingActivationError(space_id)

    register_pairing_activation_checker(arm_on_second_call)
    try:
        result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    finally:
        clear_pairing_activation_checker()

    assert calls["n"] == 2  # both phases consulted the fence
    assert result["status"] == "error"
    assert "mid-activation" in result["message"]
    # Roster NOT replaced (still the seeded ACTIVE member, not forced to [self]).
    final = await HivemindStateStore(storage=storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert [m.node_id for m in final.members] == [NODE_ID]
    assert final.epoch == 1  # never advanced (aborted before set_membership)


async def test_p10_3_unsafe_recovery_aborts_on_same_epoch_concurrent_advance(monkeypatch):
    """Finding #1 (the residual split): a concurrent pairing converges to the EXACT
    epoch the restore precomputed (orphan@1 -> new_epoch=2). `set_membership` rejects
    only a STRICTLY lower epoch, so absent the phase-2 epoch-advance guard the roster
    would be overwritten at the SAME epoch — a split the peer epoch fence can't
    detect. The guard must abort and preserve the converged roster."""
    from live_mem.core.reservation_guard import (
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)  # epoch 1, [NODE_ID ACTIVE]
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    calls = {"n": 0}

    async def advance_to_new_epoch_on_phase1(space_id: str, ignore_pair_id) -> None:
        # Simulate a concurrent pairing that promoted a target to ACTIVE at e+2==2
        # DURING the recovery window: the fence itself never raises (the pairing is
        # ACTIVE, not mid-activation) — only the epoch-advance guard catches it.
        calls["n"] += 1
        if calls["n"] == 1:
            store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
            cur = await store.get_membership()
            intruder = Member(
                node_id="intruderp250000000000000000000aa",
                public_key=generate_peer_keypair().public_key,
                status=MemberStatus.ACTIVE.value,
            )
            await store.set_membership(
                MembershipView(epoch=cur.epoch + 1, members=[*cur.members, intruder])
            )

    register_pairing_activation_checker(advance_to_new_epoch_on_phase1)
    try:
        result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    finally:
        clear_pairing_activation_checker()

    assert result["status"] == "error"
    assert "advanced" in result["message"]
    # No split: the concurrent two-member roster at epoch 2 survives intact — the
    # restore did NOT overwrite it with [self] at the same epoch.
    final = await HivemindStateStore(storage=storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert final.epoch == 2 and len(final.members) == 2
    assert any(m.node_id == "intruderp250000000000000000000aa" for m in final.members)


async def test_p10_3_unsafe_recovery_refused_on_non_leader_worker(monkeypatch):
    """Cross-process gate (Codex round-16): mesh membership is a single-writer
    authority (the flock-elected leader). Out-of-band unsafe recovery, which
    replaces the roster outside the membership authority, must run ONLY on the
    leader — a non-leader worker refuses (fail-closed) so it can never race the
    leader's pairing promotion into a same-epoch overwrite the in-process lock
    cannot serialize across processes."""
    from live_mem.core.reservation_guard import (
        NotMembershipLeaderError,
        clear_membership_recovery_leader_checker,
        register_membership_recovery_leader_checker,
    )

    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    before = storage.snapshot()
    epoch_before = await _read_membership_epoch(storage)

    # This process is NOT the mesh leader.
    async def not_leader(space_id: str) -> None:
        raise NotMembershipLeaderError(space_id)

    register_membership_recovery_leader_checker(not_leader)
    try:
        result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    finally:
        clear_membership_recovery_leader_checker()

    assert result["status"] == "error"
    assert "leader" in result["message"]
    # Refused before ANY mutation — storage fully unmutated.
    assert storage.objects == before
    assert (await _read_membership_epoch(storage)) == epoch_before


async def test_p10_3_unsafe_recovery_proceeds_on_leader_worker(monkeypatch):
    """The leader-gate is a pass-through on the leader: recovery proceeds and
    forward-forces the epoch (its roster write is then serialized against pairing
    promotions by the leader's in-process membership lock)."""
    from live_mem.core.reservation_guard import (
        clear_membership_recovery_leader_checker,
        register_membership_recovery_leader_checker,
    )

    storage = CopyFakeStorage()
    await _seed_orphan_hive(storage)
    _seed_backup(storage)
    _patch_storage(monkeypatch, storage)

    epoch_before = await _read_membership_epoch(storage)

    async def is_leader(space_id: str) -> None:
        return  # this process holds the mesh leader lock

    register_membership_recovery_leader_checker(is_leader)
    try:
        result = await BackupService().restore(BACKUP_ID, unsafe_recovery=True)
    finally:
        clear_membership_recovery_leader_checker()

    assert result["status"] == "ok"
    assert (await _read_membership_epoch(storage)) > epoch_before


async def _read_membership_epoch(storage: FakeStorage) -> int:
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await store.get_membership()
    return membership.epoch if membership is not None else -1
