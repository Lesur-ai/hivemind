# -*- coding: utf-8 -*-
"""
Tests du label de statut unifié P2 (P2-3, ADR-0008).

``hive_status_label`` dérive le vocabulaire produit P2 (6 valeurs) AU-DESSUS du
resolver PR #17 (``resolve_hive_context``, 4 valeurs, agnostique de
``_meta.json``), sans réinjecter d'état dans la logique protocole.

Points load-bearing vérifiés :
- ``not_a_space`` vs ``local_only`` (présence de ``_meta.json``) ;
- override orphelin : Hivemind sans ``_meta.json`` -> ``unsafe``, AVANT le
  mapping HEALTHY (un hive sain dont la méta est supprimée remonte ``unsafe``) ;
- fail-closed : corruption de node/members/node_status.json -> ``CorruptedStateError``
  (jamais ``local_only``/``not_a_space``) ; les fichiers plus profonds
  (term/token/bank_version) sont hors du read-set de détection (couverts au
  bootstrap, cf. tests/test_hivemind_fault_injection.py) ;
- ``hive_status()`` garde sa clé 4-valeurs ``hive_status`` (#10), espace distinct ;
- read-only : aucun write.

Fichier distinct de P2-1/P2-2 pour garder les branches Vague 1 disjointes.
"""

import json

import pytest

from live_mem.core.hivemind import (
    CorruptedStateError,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    generate_peer_keypair,
    hive_status,
    hive_status_label,
)
from tests.test_hivemind_state import FakeStorage

SPACE = "p2-3-space"
NODE_ID = "nodep23000000000000000000000000aa"


async def _seed_healthy_hive(storage: FakeStorage, space_id: str = SPACE) -> HivemindStateStore:
    """node.json + >=1 membre ACTIVE + _meta.json présent (node_status absent =
    sain par défaut) => hivemind_healthy."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
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
    await storage.put(
        f"{space_id}/_meta.json", json.dumps({"space_id": space_id, "version": 1})
    )
    return store


async def test_label_not_a_space_empty_storage():
    storage = FakeStorage()
    assert await hive_status_label(storage, SPACE) == "not_a_space"


async def test_label_local_only_meta_no_hivemind():
    storage = FakeStorage()
    await storage.put(
        f"{SPACE}/_meta.json", json.dumps({"space_id": SPACE, "version": 1})
    )
    assert await hive_status_label(storage, SPACE) == "local_only"


async def test_label_hivemind_healthy():
    storage = FakeStorage()
    await _seed_healthy_hive(storage)
    assert await hive_status_label(storage, SPACE) == "hivemind_healthy"


async def test_label_unsafe_structurally_incomplete():
    # node.json présent mais AUCUN membre ACTIVE + _meta.json présent + pas de
    # node_status -> structure incomplète -> unsafe (fail-closed, jamais HEALTHY).
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
    await storage.put(
        f"{SPACE}/_meta.json", json.dumps({"space_id": SPACE, "version": 1})
    )
    assert await hive_status_label(storage, SPACE) == "unsafe"


async def test_label_resync_required_marker():
    storage = FakeStorage()
    store = await _seed_healthy_hive(storage)
    await store.set_node_status(NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED))
    assert await hive_status_label(storage, SPACE) == "resync_required"


async def test_label_healthy_hive_missing_meta_is_unsafe_override():
    # Override orphelin évalué AVANT le mapping HEALTHY : un hive sain dont le
    # _meta.json a été supprimé remonte unsafe, pas hivemind_healthy.
    storage = FakeStorage()
    await _seed_healthy_hive(storage)
    await storage.delete(f"{SPACE}/_meta.json")
    assert await hive_status_label(storage, SPACE) == "unsafe"


async def test_label_orphaned_marker_no_meta_is_unsafe():
    # Marqueur _hivemind/ présent (node.json) mais aucun _meta.json -> unsafe.
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
    )
    assert await hive_status_label(storage, SPACE) == "unsafe"


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
async def test_corruption_node_members_nodestatus_raises_not_local(corrupt_file):
    # Corruption d'un fichier du read-set de détection -> CorruptedStateError,
    # jamais local_only/not_a_space (sinon un write partagé bypasserait le token).
    storage = FakeStorage()
    await _seed_healthy_hive(storage)
    await storage.put(f"{SPACE}/_hivemind/{corrupt_file}", "{not valid json")
    with pytest.raises(CorruptedStateError):
        await hive_status_label(storage, SPACE)


async def test_hive_status_and_label_consistent_for_healthy_hive():
    # Deux espaces de valeurs DISTINCTS, cohérents par construction.
    storage = FakeStorage()
    await _seed_healthy_hive(storage)
    status = await hive_status(storage, SPACE)
    assert status["hive_status"] == "healthy"  # 4-valeurs (#10)
    assert await hive_status_label(storage, SPACE) == "hivemind_healthy"  # 6-valeurs (P2)


async def test_label_resync_hive_missing_meta_is_unsafe_override():
    # L'override orphelin précède TOUT le mapping de santé (HEALTHY ET RESYNC) :
    # un hive marqué RESYNC dont le _meta.json a disparu remonte unsafe.
    storage = FakeStorage()
    store = await _seed_healthy_hive(storage)
    await store.set_node_status(NodeHealth(status=HiveNodeStatus.RESYNC_REQUIRED))
    await storage.delete(f"{SPACE}/_meta.json")
    assert await hive_status_label(storage, SPACE) == "unsafe"


@pytest.mark.parametrize(
    "seed",
    ["healthy", "incomplete"],
)
async def test_resolver_label_is_read_only_no_writes(seed):
    # Read-only sur TOUS les chemins (sain ET structurellement incomplet) :
    # ne jamais réparer silencieusement.
    storage = FakeStorage()
    if seed == "healthy":
        await _seed_healthy_hive(storage)
    else:
        store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
        keys = generate_peer_keypair()
        await store.set_node_identity(
            NodeIdentity(node_id=NODE_ID, public_key=keys.public_key)
        )
        await storage.put(
            f"{SPACE}/_meta.json", json.dumps({"space_id": SPACE, "version": 1})
        )
    before = storage.snapshot()
    puts_before, deletes_before = storage.put_calls, storage.delete_calls
    await hive_status_label(storage, SPACE)
    assert storage.objects == before, "hive_status_label a muté le stockage"
    assert storage.put_calls == puts_before
    assert storage.delete_calls == deletes_before
