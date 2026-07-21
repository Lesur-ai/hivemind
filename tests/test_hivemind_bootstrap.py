# -*- coding: utf-8 -*-
"""
Tests pour issue #5 — bootstrap snapshot export/import.

Couvre :
- export -> import dans une cible vierge atteint la même bank_version ;
- une cible non vierge est refusée (rien n'est écrit) ;
- un checksum (par-fichier ou manifest) invalide refuse l'import fail-closed ;
- un import partiel/crashé ne se relit jamais HEALTHY (reste UNSAFE) ;
- le bloc graph_memory est totalement exclu du snapshot et de la cible ;
- l'import frappe un node_id local NEUF (jamais celui de la source).
"""

from __future__ import annotations

import json

import pytest

from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    BootstrapError,
    BootstrapService,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeIdentity,
    PeerKeyPair,
    TermState,
    generate_peer_keypair,
    manifest_content_hash,
)
from live_mem.core.hivemind import layout
from tests.test_hivemind_state import FakeStorage


SOURCE = "source-space"
TARGET = "target-space"
SOURCE_NODE_ID = "sourcenode00000000000000000000aa"
# Peer cible pré-provisionné dans la MembershipView source AVANT export
# (HIVEMIND.md §5.1.5 : tous les participants persistent la même MembershipView).
PEER_NODE_ID = "peernode0000000000000000000000bb"


async def _seed_source(storage: FakeStorage) -> PeerKeyPair:
    """Peuple un space source réaliste : fichiers de space + état Hivemind
    à epoch 3 et bank_version 1.

    La MembershipView source inclut le peer cible (``PEER_NODE_ID``)
    pré-provisionné : c'est la condition pour qu'il puisse importer et
    participer (§5.1.5). Retourne la keypair du peer pour que le test la
    rejoue à l'import (correspondance de clé publique)."""
    # Fichiers de space (non-Hivemind), dont _meta.json AVEC un secret
    # graph_memory à exclure.
    await storage.put(
        f"{SOURCE}/_meta.json",
        json.dumps(
            {
                "space_id": SOURCE,
                "description": "demo",
                "owner": "alice",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_consolidation": "2026-02-01T00:00:00+00:00",
                "consolidation_count": 4,
                "total_notes_processed": 42,
                "version": 2,
                "graph_memory": {
                    "url": "http://graph.local/mcp",
                    "token": "supersecrettoken",
                    "memory_id": "mem-123",
                },
            }
        ),
    )
    await storage.put(f"{SOURCE}/_rules.md", "# Rules\nstructure")
    await storage.put(f"{SOURCE}/_synthesis.md", "# Synthesis\nresidual")
    await storage.put(f"{SOURCE}/bank/.keep", "")
    await storage.put(f"{SOURCE}/bank/activeContext.md", "# Active\ncontext")
    await storage.put(f"{SOURCE}/bank/progress.md", "# Progress\nmilestones")
    await storage.put(f"{SOURCE}/live/.keep", "")
    await storage.put(
        f"{SOURCE}/live/20260101T000000_alice_observation_ab12cd34.md",
        "---\nagent: alice\n---\nnote body",
    )

    # État Hivemind source : epoch 3, term 2, un commit à bank_version 1.
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    peer_keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=3,
            members=[
                Member(node_id=SOURCE_NODE_ID, public_key=keys.public_key),
                Member(node_id=PEER_NODE_ID, public_key=peer_keys.public_key),
            ],
        )
    )
    await store.bump_term(2, updated_by_node_id=SOURCE_NODE_ID)
    await store.append_commit(
        BankCommit(
            bank_version=1,
            parent_bank_version=0,
            term=2,
            commit_id="commit-bv1",
            committed_by_node_id=SOURCE_NODE_ID,
        )
    )
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=1, commit_id="commit-bv1")
    )
    return peer_keys


async def _seed_blank_target(storage: FakeStorage) -> None:
    """Crée une cible vierge (les 4 placeholders de space.create)."""
    await storage.put(
        f"{TARGET}/_meta.json", json.dumps({"space_id": TARGET, "version": 1})
    )
    await storage.put(f"{TARGET}/_rules.md", "")
    await storage.put(f"{TARGET}/live/.keep", "")
    await storage.put(f"{TARGET}/bank/.keep", "")


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


# =============================================================================
# Export -> import nominal
# =============================================================================


async def test_export_then_import_reaches_same_bank_version(
    storage: FakeStorage,
) -> None:
    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    assert snapshot.manifest.bank_version == 1
    assert snapshot.manifest.commit_id == "commit-bv1"
    assert snapshot.manifest.membership_epoch == 3

    result = await service.import_snapshot(TARGET, snapshot, peer_keys)

    assert result.bank_version == 1
    assert result.commit_id == "commit-bv1"
    assert result.membership_epoch == 3
    assert result.node_status == HiveNodeStatus.HEALTHY
    # L'identité locale est celle pré-provisionnée dans la membership source.
    assert result.local_node_id == PEER_NODE_ID

    # La cible relit la même bank_version + epoch source.
    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    pointer = await target_store.get_bank_version_pointer()
    assert pointer is not None and pointer.bank_version == 1
    membership = await target_store.get_membership()
    assert membership is not None and membership.epoch == 3
    # Le peer importé EST un membre ACTIVE (sinon ses events seraient rejetés
    # UNKNOWN_PEER et il serait absent des attentes d'ACK).
    assert any(
        m.node_id == PEER_NODE_ID and m.status == "active" for m in membership.members
    )
    health = await target_store.get_node_status()
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.HEALTHY

    # Le contenu bank est bien arrivé.
    assert await storage.get(f"{TARGET}/bank/activeContext.md") == "# Active\ncontext"


# =============================================================================
# Cible non vierge refusée
# =============================================================================


@pytest.mark.parametrize(
    "extra_key, extra_val",
    [
        ("bank/realfile.md", "# real content"),
        ("_synthesis.md", "# residual"),
        ("_hivemind/members.json", "{}"),
        ("live/realnote.md", "note"),
    ],
)
async def test_import_refuses_non_empty_target(
    storage: FakeStorage, extra_key: str, extra_val: str
) -> None:
    await _seed_source(storage)
    await _seed_blank_target(storage)
    await storage.put(f"{TARGET}/{extra_key}", extra_val)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    before = storage.snapshot()
    keypair = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, keypair)

    # Rien n'a été écrit dans la cible (pas même node_status=UNSAFE) : la
    # validation vierge précède toute écriture.
    assert storage.snapshot() == before


# =============================================================================
# Checksum invalide -> fail-closed
# =============================================================================


async def test_import_refuses_on_per_file_checksum_mismatch(
    storage: FakeStorage,
) -> None:
    await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    # Corrompre le contenu d'un fichier sans toucher au manifest.
    target_path = "bank/activeContext.md"
    snapshot.files[target_path] = snapshot.files[target_path] + "TAMPERED"

    before = storage.snapshot()
    keypair = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, keypair)
    # La vérification précède tout write : la cible est intacte.
    assert storage.snapshot() == before


async def test_import_refuses_on_manifest_hash_mismatch(
    storage: FakeStorage,
) -> None:
    await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    # Falsifier le manifest_sha256 (manifest tronqué/altéré).
    snapshot.manifest.manifest_sha256 = "0" * 64

    before = storage.snapshot()
    keypair = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, keypair)
    assert storage.snapshot() == before


async def test_import_refuses_dropped_manifest_entry(storage: FakeStorage) -> None:
    """Supprimer une entrée du manifest casse manifest_sha256 -> refus."""
    await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    dropped = snapshot.manifest.entries.pop()
    snapshot.files.pop(dropped.path, None)
    # manifest_sha256 n'est PAS recalculé -> incohérent.
    keypair = generate_peer_keypair()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, keypair)


# =============================================================================
# Import partiel / crashé n'est jamais HEALTHY
# =============================================================================


async def test_partial_or_crashed_import_is_not_healthy(
    storage: FakeStorage,
) -> None:
    await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    keypair = generate_peer_keypair()

    # Simuler un crash : faire échouer un put au milieu de l'écriture des
    # objets (après que node_status=UNSAFE a été posé).
    original_put = storage.put
    state = {"calls": 0}

    async def flaky_put(key: str, content: str, content_type: str = "text/plain"):
        # Laisser passer node_status (UNSAFE) puis casser sur un fichier bank.
        if key.endswith("bank/activeContext.md"):
            raise RuntimeError("disque plein (crash simulé)")
        return await original_put(key, content, content_type)

    storage.put = flaky_put  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await service.import_snapshot(TARGET, snapshot, keypair)
    storage.put = original_put  # type: ignore[assignment]

    # node_status reste UNSAFE ; jamais HEALTHY.
    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


# =============================================================================
# graph_memory exclu du snapshot et de la cible
# =============================================================================


async def test_meta_graph_memory_excluded_from_snapshot(
    storage: FakeStorage,
) -> None:
    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)

    # Le _meta.json du snapshot ne contient AUCUN champ graph_memory.
    meta_in_snapshot = json.loads(snapshot.files["_meta.json"])
    assert "graph_memory" not in meta_in_snapshot
    assert "supersecrettoken" not in snapshot.files["_meta.json"]
    # Les champs partagés whitelistés sont préservés.
    assert meta_in_snapshot["description"] == "demo"
    assert meta_in_snapshot["consolidation_count"] == 4

    await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_meta = json.loads(await storage.get(f"{TARGET}/_meta.json"))
    assert "graph_memory" not in target_meta
    assert "supersecrettoken" not in json.dumps(target_meta)


# =============================================================================
# Node_id local neuf + node-local files exclus
# =============================================================================


async def test_import_adopts_provisioned_identity(storage: FakeStorage) -> None:
    """L'import adopte le node_id pré-provisionné (membre dont la clé publique
    correspond à la nôtre), jamais celui de la source ni un UUID aléatoire."""
    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    result = await service.import_snapshot(TARGET, snapshot, peer_keys)

    assert result.local_node_id == PEER_NODE_ID
    assert result.local_node_id != SOURCE_NODE_ID
    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    node = await target_store.get_node_identity()
    assert node is not None
    assert node.node_id == PEER_NODE_ID
    assert node.public_key == peer_keys.public_key


async def test_import_refuses_unprovisioned_peer(storage: FakeStorage) -> None:
    """[P1] Un peer dont la clé publique n'est PAS dans la MembershipView
    importée est refusé fail-closed : il serait sinon marqué HEALTHY alors que
    ses events seraient rejetés UNKNOWN_PEER et qu'il serait absent des ACK."""
    await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    stranger = generate_peer_keypair()  # clé absente de la membership source
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, stranger)

    # node_status reste UNSAFE — jamais HEALTHY pour un peer non provisionné.
    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


async def test_import_preserves_target_local_meta(storage: FakeStorage) -> None:
    """[P2] L'import MERGE la projection source dans la méta cible : il
    préserve le space_id de la CIBLE, le bloc graph_memory et l'owner locaux,
    tout en héritant des champs descriptifs partagés (description)."""
    peer_keys = await _seed_source(storage)
    # Cible avec une identité + graph_memory LOCAUX qui ne doivent pas être
    # écrasés par la source.
    await storage.put(
        f"{TARGET}/_meta.json",
        json.dumps(
            {
                "space_id": TARGET,
                "owner": "bob",
                "created_at": "2026-06-01T00:00:00+00:00",
                "version": 1,
                "graph_memory": {
                    "url": "http://target-graph.local/mcp",
                    "token": "targetlocalsecret",
                },
            }
        ),
    )
    await storage.put(f"{TARGET}/_rules.md", "")
    await storage.put(f"{TARGET}/live/.keep", "")
    await storage.put(f"{TARGET}/bank/.keep", "")
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_meta = json.loads(await storage.get(f"{TARGET}/_meta.json"))
    # Identité de la CIBLE préservée (jamais le space_id/owner source).
    assert target_meta["space_id"] == TARGET
    assert target_meta["owner"] == "bob"
    assert target_meta["created_at"] == "2026-06-01T00:00:00+00:00"
    # graph_memory local préservé (et le secret source jamais introduit).
    assert target_meta["graph_memory"]["token"] == "targetlocalsecret"
    assert "supersecrettoken" not in json.dumps(target_meta)
    # Champ descriptif partagé hérité de la source.
    assert target_meta["description"] == "demo"


async def test_export_excludes_node_local_hivemind_files(
    storage: FakeStorage,
) -> None:
    await _seed_source(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    # Poser un node_status source (node-local) qui ne doit PAS voyager.
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    from live_mem.core.hivemind import NodeHealth

    await src_store.set_node_status(NodeHealth(status=HiveNodeStatus.HEALTHY))
    # Un token FREE est un état PARTAGÉ (term/fencing au repos) : il DOIT voyager.
    from live_mem.core.hivemind import TokenLeaseState, TokenState

    await src_store.set_token(
        TokenLeaseState(state=TokenState.FREE, term=2, fencing_token=2)
    )

    snapshot = await service.export_snapshot(SOURCE)
    paths = {e.path for e in snapshot.manifest.entries}
    # node-locaux exclus (SEULES identité + santé)
    assert "_hivemind/node.json" not in paths
    assert "_hivemind/node_status.json" not in paths
    # token FREE = PARTAGÉ -> inclus
    assert "_hivemind/token.json" in paths
    # node-indépendants inclus
    assert "_hivemind/members.json" in paths
    assert "_hivemind/term.json" in paths
    assert "_hivemind/bank_version.json" in paths
    assert "_hivemind/commits/" + format(1, "020d") + ".json" in paths
    # placeholders .keep exclus
    assert all(not p.endswith("/.keep") for p in paths)


# =============================================================================
# Export refusé si token HELD/RELEASING
# =============================================================================


async def test_export_refused_when_token_held(storage: FakeStorage) -> None:
    await _seed_source(storage)
    from live_mem.core.hivemind import TokenLeaseState, TokenState

    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await src_store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=SOURCE_NODE_ID,
            term=2,
            fencing_token=2,
        )
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


# =============================================================================
# manifest_content_hash est déterministe & sensible à l'ordre/contenu
# =============================================================================


async def test_manifest_hash_is_deterministic_and_sensitive(
    storage: FakeStorage,
) -> None:
    await _seed_source(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snap = await service.export_snapshot(SOURCE)
    manifest = snap.manifest

    # Recalcul identique.
    assert manifest_content_hash(manifest) == manifest.manifest_sha256
    # Insensible à l'ordre des entrées (tri interne par path).
    reordered = manifest.model_copy(
        update={"entries": list(reversed(manifest.entries))}
    )
    assert manifest_content_hash(reordered) == manifest.manifest_sha256
    # Sensible au CONTENU d'une entrée (un sha256 modifié change le hash).
    mutated_entries = [e.model_copy() for e in manifest.entries]
    mutated_entries[0] = mutated_entries[0].model_copy(update={"sha256": "f" * 64})
    assert (
        manifest_content_hash(manifest.model_copy(update={"entries": mutated_entries}))
        != manifest.manifest_sha256
    )
    # Sensible aux HEADERS critiques (bank_version / source_node_id altérés).
    assert (
        manifest_content_hash(manifest.model_copy(update={"bank_version": 999}))
        != manifest.manifest_sha256
    )
    assert (
        manifest_content_hash(manifest.model_copy(update={"source_node_id": "evil"}))
        != manifest.manifest_sha256
    )


async def test_import_refuses_membership_epoch_mismatch_with_manifest(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un header manifest membership_epoch incohérent avec le
    members.json importé (hash recalculé pour passer l'intégrité) est rejeté
    avant HEALTHY — le peer doit pouvoir raisonner sur l'epoch attendu."""
    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    # members.json importé portera epoch 3 ; on désaccorde le header manifest.
    snapshot.manifest.membership_epoch = 999
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


async def test_import_refuses_tampered_manifest_header(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Une altération d'un HEADER de manifest (ici bank_version)
    SANS recalcul du hash est détectée par le manifest hash AVANT tout write —
    plus seulement après écriture (cible alors UNSAFE)."""
    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    snapshot.manifest.bank_version = 999  # header altéré, hash NON recalculé
    before = storage.snapshot()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)
    assert storage.snapshot() == before  # rejet avant toute écriture


async def test_import_refuses_held_token_in_snapshot(storage: FakeStorage) -> None:
    """[re-review P2] Un snapshot (peer bogué) portant un token.json HELD est
    refusé à l'import : un peer vierge ne doit pas hériter d'une mutation en
    cours (miroir du refus d'export HELD/RELEASING)."""
    import hashlib

    from live_mem.core.hivemind import TokenLeaseState, TokenState
    from live_mem.core.hivemind.models import BootstrapManifestEntry

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    held = TokenLeaseState(
        state=TokenState.HELD,
        holder_node_id=SOURCE_NODE_ID,
        term=2,
        fencing_token=2,
    ).model_dump_json()
    snapshot.files["_hivemind/token.json"] = held
    snapshot.manifest.entries.append(
        BootstrapManifestEntry(
            path="_hivemind/token.json",
            sha256=hashlib.sha256(held.encode("utf-8")).hexdigest(),
            size=len(held),
        )
    )
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


# =============================================================================
# Re-review Codex : durcissement export/import
# =============================================================================


async def test_export_refused_without_membership(storage: FakeStorage) -> None:
    """[re-review P2] Un export sans MembershipView active est refusé up-front
    (sinon il produit un snapshot inimportable laissant la cible UNSAFE)."""
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=keys.public_key)
    )
    # node.json présent mais AUCUNE membership.
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_import_refuses_node_local_path_in_manifest(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Defense-in-depth : même avec des hashes valides, un
    snapshot contenant un chemin node-local (_hivemind/node.json) est refusé
    AVANT tout write — l'importeur ré-applique l'allowlist, sans faire confiance
    à l'exporteur (sinon : identité usurpée plantée / marqueur UNSAFE écrasé)."""
    import hashlib

    from live_mem.core.hivemind.models import BootstrapManifestEntry

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    # Injecter une entrée node-locale AVEC un hash valide et recalculer le
    # manifest_sha256 pour passer la vérification d'intégrité : seul le contrôle
    # d'allowlist de chemins doit la rejeter.
    payload = '{"node_id": "evil", "public_key": "evilkey"}'
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    snapshot.files["_hivemind/node.json"] = payload
    snapshot.manifest.entries.append(
        BootstrapManifestEntry(
            path="_hivemind/node.json", sha256=digest, size=len(payload)
        )
    )
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)

    before = storage.snapshot()
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)
    # Rien n'a été écrit : le rejet précède toute écriture (y compris UNSAFE).
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "bad_status",
    [HiveNodeStatus.UNSAFE, HiveNodeStatus.RESYNC_REQUIRED],
)
async def test_export_refused_from_unhealthy_source(
    storage: FakeStorage, bad_status: HiveNodeStatus
) -> None:
    """[re-review P2] Un export depuis une source non-HEALTHY est refusé :
    propager un état partiel/stale à un peer vierge le marquerait HEALTHY à
    tort. Seule une source HEALTHY (ou sans node_status) peut bootstrapper."""
    from live_mem.core.hivemind import NodeHealth

    await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await src_store.set_node_status(NodeHealth(status=bad_status, reason="dégradé"))
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_when_source_state_corrupt(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un état Hivemind source corrompu (term.json malformé) est
    détecté À L'EXPORT, plutôt que de produire un snapshot empoisonné qui
    n'endommagerait la cible qu'après écriture."""
    await _seed_source(storage)
    await storage.put(f"{SOURCE}/_hivemind/term.json", "{ pas du json valide")
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_on_orphan_future_commit(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un commit ORPHELIN en avance sur le pointeur (append_commit
    réussi mais bank_version.json resté en arrière) est rejeté à l'export :
    sinon la cible serait HEALTHY avec latest_commit > pointeur, laissant une
    recovery avancer vers une version jamais committée par le pointeur."""
    await _seed_source(storage)  # pointeur bv1, commit bv1
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    # Commit bv2 SANS avancer le pointeur (reste à bv1).
    await src_store.append_commit(
        BankCommit(
            bank_version=2,
            parent_bank_version=1,
            term=2,
            commit_id="commit-bv2",
            committed_by_node_id=SOURCE_NODE_ID,
        )
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_when_source_pointer_has_no_commit(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un pointeur bank_version source sans commit matérialisé
    (ex. restore S3 partiel) est détecté À L'EXPORT (symétrique du check
    d'import), au lieu de produire un snapshot que l'importeur écrirait puis
    rejetterait, laissant la cible UNSAFE."""
    await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    # Avancer le pointeur vers une bank_version SANS commit correspondant.
    await src_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=5, commit_id="ghost")
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_when_source_member_lacks_public_key(
    storage: FakeStorage,
) -> None:
    """[re-review P1] Si le membre source ACTIVE n'a pas de public_key cohérente
    avec node.json, l'export est refusé : node.json est exclu du snapshot, donc
    un peer importé n'authentifierait la source que via members.json — une clé
    vide ferait rejeter ses messages signés en UNKNOWN_PEER."""
    await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    view = await src_store.get_membership()
    assert view is not None
    new_members = [
        (
            m.model_copy(update={"public_key": ""})
            if m.node_id == SOURCE_NODE_ID
            else m
        )
        for m in view.members
    ]
    await src_store.set_membership(
        MembershipView(epoch=view.epoch, members=new_members)
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_import_refuses_ambiguous_public_key_match(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un snapshot TAMPERED dont la MembershipView contient DEUX
    membres ACTIVE partageant la clé du peer importeur est refusé à l'IMPORT :
    choisir « le premier » adopterait un node_id dépendant de l'ordre. Le
    tampering du snapshot contourne la validation d'export (désormais stricte)
    pour exercer spécifiquement la défense côté import."""
    import hashlib

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    members_obj = json.loads(snapshot.files["_hivemind/members.json"])
    clone = dict(members_obj["members"][0])
    clone["node_id"] = "peerClone"
    clone["public_key"] = peer_keys.public_key
    members_obj["members"].append(clone)
    tampered = json.dumps(members_obj)
    snapshot.files["_hivemind/members.json"] = tampered
    snapshot.manifest.entries = [
        (
            e.model_copy(
                update={
                    "sha256": hashlib.sha256(tampered.encode("utf-8")).hexdigest(),
                    "size": len(tampered),
                }
            )
            if e.path == "_hivemind/members.json"
            else e
        )
        for e in snapshot.manifest.entries
    ]
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)


async def test_import_preserves_shared_free_token(storage: FakeStorage) -> None:
    """[re-review P2] Un token FREE source (état partagé, term/fencing au repos)
    est répliqué dans la cible : le peer importé démarre avec la même baseline
    token que le reste du cluster (pas d'état divergent pour la couche #7)."""
    from live_mem.core.hivemind import TokenLeaseState, TokenState

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await src_store.set_token(
        TokenLeaseState(state=TokenState.FREE, term=2, fencing_token=2)
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)
    await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    token = await target_store.get_token()
    assert token is not None
    assert TokenState(token.state) == TokenState.FREE
    assert token.term == 2 and token.fencing_token == 2


async def test_import_refuses_duplicate_active_public_key_in_membership(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un snapshot TAMPERED dont la MembershipView contient deux
    membres ACTIVE partageant une public_key NON-locale est refusé à l'IMPORT :
    une seule clé privée authentifierait plusieurs node_ids (usurpation
    d'identité ACK). Tampering du snapshot pour cibler la défense côté import."""
    import hashlib

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    shared_key = generate_peer_keypair().public_key  # clé NON-locale partagée
    members_obj = json.loads(snapshot.files["_hivemind/members.json"])
    for node_id in ("ghostA", "ghostB"):
        ghost = dict(members_obj["members"][0])
        ghost["node_id"] = node_id
        ghost["public_key"] = shared_key
        members_obj["members"].append(ghost)
    tampered = json.dumps(members_obj)
    snapshot.files["_hivemind/members.json"] = tampered
    snapshot.manifest.entries = [
        (
            e.model_copy(
                update={
                    "sha256": hashlib.sha256(tampered.encode("utf-8")).hexdigest(),
                    "size": len(tampered),
                }
            )
            if e.path == "_hivemind/members.json"
            else e
        )
        for e in snapshot.manifest.entries
    ]
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)
    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


async def test_export_works_for_space_initialized_via_initialize(
    storage: FakeStorage,
) -> None:
    """[re-review P1] Un space créé par le helper public initialize(NodeIdentity)
    doit pouvoir être exporté : initialize seede désormais la public_key du
    self-member (sinon le check d'export round-11 le rejetterait à tort)."""
    keys = generate_peer_keypair()
    peer_keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await store.initialize(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=keys.public_key)
    )
    # Le self-member initialisé porte bien la public_key de l'identité.
    self_member = next(
        m for m in (await store.get_membership()).members if m.node_id == SOURCE_NODE_ID
    )
    assert self_member.public_key == keys.public_key

    # Pré-provisionner un peer + fichiers de space minimaux pour un snapshot valide.
    view = await store.get_membership()
    await store.set_membership(
        MembershipView(
            epoch=view.epoch,
            members=[*view.members, Member(node_id=PEER_NODE_ID, public_key=peer_keys.public_key)],
        )
    )
    await storage.put(f"{SOURCE}/_meta.json", json.dumps({"space_id": SOURCE, "version": 1}))
    await storage.put(f"{SOURCE}/_rules.md", "")
    await storage.put(f"{SOURCE}/bank/.keep", "")
    await storage.put(f"{SOURCE}/live/.keep", "")
    service = BootstrapService(storage)  # type: ignore[arg-type]

    # Ne doit PAS lever (le check round-11 passe car le self-member a une clé).
    snapshot = await service.export_snapshot(SOURCE)
    assert snapshot.manifest.source_node_id == SOURCE_NODE_ID


async def test_initialize_normalizes_explicit_self_member_public_key(
    storage: FakeStorage,
) -> None:
    """[re-review P2] initialize() avec une initial_members contenant DÉJÀ le
    self node sans public_key normalise sa clé depuis l'identité — sinon le
    space resterait inexportable malgré une NodeIdentity correcte."""
    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await store.initialize(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=keys.public_key),
        initial_members=[Member(node_id=SOURCE_NODE_ID, public_key="")],
    )
    self_member = next(
        m
        for m in (await store.get_membership()).members
        if m.node_id == SOURCE_NODE_ID
    )
    assert self_member.public_key == keys.public_key


async def test_bootstrap_audit_not_in_shared_event_journal(
    storage: FakeStorage,
) -> None:
    """[re-review P2] L'export/import bootstrap n'ajoute AUCUN event au journal
    PARTAGÉ `events/` : un event présent d'un seul côté ferait diverger en
    permanence l'audit/dedup partagé. L'audit bootstrap est node-local
    (node_status + ImportResult)."""
    from live_mem.core.hivemind import EventType

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]

    events_before = len(await src_store.list_events())
    snapshot = await service.export_snapshot(SOURCE)
    # L'export n'ajoute pas d'event au journal source.
    assert len(await src_store.list_events()) == events_before

    result = await service.import_snapshot(TARGET, snapshot, peer_keys)
    assert result.node_status == HiveNodeStatus.HEALTHY
    # Aucun event BOOTSTRAP_SNAPSHOT_* (EXPORTED côté source / IMPORTED côté cible).
    tgt_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    for store_ in (src_store, tgt_store):
        events = await store_.list_events()
        assert not any(
            e.type
            in (
                EventType.BOOTSTRAP_SNAPSHOT_EXPORTED.value,
                EventType.BOOTSTRAP_SNAPSHOT_IMPORTED.value,
            )
            for e in events
        )


async def test_reinitialize_normalizes_existing_self_member_key(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Réinitialiser un space DÉJÀ initialisé dont le self-member
    a une clé absente/stale normalise sa public_key depuis l'identité (sinon
    export_snapshot le rejetterait après upgrade). L'epoch ne régresse pas."""
    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    # Espace pré-existant : self-member SANS public_key, à epoch 2.
    await store.set_node_identity(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key="")
    )
    await store.set_membership(
        MembershipView(
            epoch=2, members=[Member(node_id=SOURCE_NODE_ID, public_key="")]
        )
    )
    # Ré-init avec une identité KEYÉE.
    await store.initialize(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=keys.public_key)
    )

    view = await store.get_membership()
    self_member = next(m for m in view.members if m.node_id == SOURCE_NODE_ID)
    assert self_member.public_key == keys.public_key
    assert view.epoch == 2  # pas de régression d'epoch


async def test_reinitialize_rejects_key_rotation(storage: FakeStorage) -> None:
    """[re-review P2] Re-init avec une public_key non-vide DIFFÉRENTE est REJETÉE
    (avant de toucher node.json) : la rotation silencieuse au même epoch ferait
    diverger node.json/members.json (les peers fencent par epoch). node.json et
    members.json restent inchangés et alignés."""
    k1 = generate_peer_keypair()
    k2 = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id=SOURCE_NODE_ID, public_key=k1.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=2,
            members=[Member(node_id=SOURCE_NODE_ID, public_key=k1.public_key)],
        )
    )
    # Tentative de rotation -> rejetée AVANT toute écriture de node.json.
    with pytest.raises(RuntimeError):
        await store.initialize(
            NodeIdentity(node_id=SOURCE_NODE_ID, public_key=k2.public_key)
        )

    # node.json ET members.json restent sur l'ancienne clé (alignés, epoch 2).
    node = await store.get_node_identity()
    assert node is not None and node.public_key == k1.public_key
    view = await store.get_membership()
    self_member = next(m for m in view.members if m.node_id == SOURCE_NODE_ID)
    assert self_member.public_key == k1.public_key
    assert view.epoch == 2


async def test_export_refused_on_empty_active_member_node_id(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un membre ACTIVE avec node_id vide (membership restaurée/
    tampered) est rejeté par le validateur partagé : sinon expected_ack_node_ids
    attendrait un ACK pour '' et bloquerait l'all-ACK."""
    keys = generate_peer_keypair()
    await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    view = await src_store.get_membership()
    assert view is not None
    blank = Member(node_id="", public_key=keys.public_key)
    await src_store.set_membership(
        MembershipView(epoch=view.epoch, members=[*view.members, blank])
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_resolve_hive_context_partial_state_is_unsafe(
    storage: FakeStorage,
) -> None:
    """[re-review P2] node.json présent SANS membership ACTIVE (init interrompue
    / restore partiel) = état Hivemind PARTIEL : is_hive True + UNSAFE, jamais
    classé « local » (sinon un caller bypasserait le chemin fail-closed)."""
    from live_mem.core.hivemind.lifecycle import resolve_hive_context

    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id="partial")  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    # Pas de membership écrite : état partiel.
    ctx = await resolve_hive_context(storage, "partial")
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.UNSAFE


async def test_resolve_hive_context_stale_healthy_incomplete_is_unsafe(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un node_status HEALTHY PÉRIMÉ alors que la structure est
    incomplète (node.json/membership ACTIVE manquant, ex. restore/suppression
    partiel) est traité UNSAFE + is_hive True — jamais HEALTHY ni « local »."""
    from live_mem.core.hivemind import NodeHealth
    from live_mem.core.hivemind.lifecycle import resolve_hive_context

    store = HivemindStateStore(storage=storage, space_id="stale")  # type: ignore[arg-type]
    # HEALTHY périmé mais NI node.json NI membership.
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.HEALTHY, reason="stale")
    )
    ctx = await resolve_hive_context(storage, "stale")
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.UNSAFE


async def test_export_refused_when_active_member_key_malformed(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Une clé publique active non-vide mais qui ne parse PAS en
    Ed25519 est rejetée (export et import partagent la même validation) : sinon
    le peer channel échouerait INVALID_KEY alors que ses ACK resteraient
    attendus par expected_ack_node_ids."""
    await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    view = await src_store.get_membership()
    assert view is not None
    bad = Member(node_id="badkey", public_key="!!!pas-une-cle-ed25519!!!")
    await src_store.set_membership(
        MembershipView(epoch=view.epoch, members=[*view.members, bad])
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_on_duplicate_active_identity(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Une source avec des identités ACTIVE dupliquées (ici une
    public_key partagée) est rejetée À L'EXPORT (validateur partagé), pas
    seulement chez l'importeur après écriture partielle de la cible."""
    peer_keys = await _seed_source(storage)
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    view = await src_store.get_membership()
    assert view is not None
    # 2e membre ACTIVE partageant la clé d'un membre existant (PEER_NODE_ID).
    dup = Member(node_id="dupnode", public_key=peer_keys.public_key)
    await src_store.set_membership(
        MembershipView(epoch=view.epoch, members=[*view.members, dup])
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


async def test_export_refused_on_orphan_corrupt_ack(storage: FakeStorage) -> None:
    """[re-review P2] Un ack ORPHELIN corrompu (event compacté/absent de
    events/) est détecté par le scan DIRECT du préfixe acks/, pas seulement via
    les events listés — sinon il serait importé et le node marqué HEALTHY, puis
    list_acks lèverait CorruptedStateError plus tard."""
    await _seed_source(storage)
    await storage.put(
        f"{SOURCE}/_hivemind/acks/orphan-evt/nodeA.json", "{ pas du json valide"
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)


def test_shared_export_path_excludes_only_top_level_node_local() -> None:
    """[re-review P2] L'exclusion node-locale se fait par CHEMIN EXACT de tête,
    pas par basename : un node_id valant "token"/"node" produit des chemins
    imbriqués (acks/watermarks) qui sont PARTAGÉS et ne doivent pas être exclus."""
    is_shared = BootstrapService._is_shared_export_path
    # Node-locaux de tête : exclus.
    assert is_shared("_hivemind/node.json") is False
    assert is_shared("_hivemind/node_status.json") is False
    # token.json est PARTAGÉ (un token FREE porte le term/fencing au repos).
    assert is_shared("_hivemind/token.json") is True
    # Chemins imbriqués avec node_id "token"/"node" : PARTAGÉS (plus de
    # faux-exclusion par basename).
    assert is_shared("_hivemind/acks/evt-123/token.json") is True
    assert is_shared("_hivemind/watermarks/node.json") is True
    assert is_shared("_hivemind/members.json") is True
    assert is_shared("_hivemind/commits/00000000000000000001.json") is True


async def test_import_drops_source_graph_memory_even_if_present(
    storage: FakeStorage,
) -> None:
    """[re-review P1] L'import ré-applique l'allowlist _meta (default-exclude) :
    même si un snapshot d'une source boguée/malveillante contient un bloc
    graph_memory, il n'écrase JAMAIS le graph_memory LOCAL de la cible."""
    import hashlib

    peer_keys = await _seed_source(storage)
    # Cible avec son PROPRE graph_memory local.
    await storage.put(
        f"{TARGET}/_meta.json",
        json.dumps(
            {
                "space_id": TARGET,
                "owner": "bob",
                "version": 1,
                "graph_memory": {"url": "http://target/mcp", "token": "targetlocal"},
            }
        ),
    )
    await storage.put(f"{TARGET}/_rules.md", "")
    await storage.put(f"{TARGET}/live/.keep", "")
    await storage.put(f"{TARGET}/bank/.keep", "")
    service = BootstrapService(storage)  # type: ignore[arg-type]

    snapshot = await service.export_snapshot(SOURCE)
    # Forger un _meta de snapshot CONTENANT un graph_memory "source" (comme si
    # l'exporteur n'avait pas exclu le bloc) — checksum recalculé pour passer
    # la vérification d'intégrité ; seule l'allowlist d'import doit le filtrer.
    poisoned = json.dumps(
        {
            "space_id": SOURCE,
            "description": "demo",
            "version": 2,
            "graph_memory": {"url": "http://evil/mcp", "token": "evilsource"},
        }
    )
    new_entries = []
    for entry in snapshot.manifest.entries:
        if entry.path == "_meta.json":
            snapshot.files["_meta.json"] = poisoned
            new_entries.append(
                entry.model_copy(
                    update={
                        "sha256": hashlib.sha256(poisoned.encode("utf-8")).hexdigest(),
                        "size": len(poisoned),
                    }
                )
            )
        else:
            new_entries.append(entry)
    snapshot.manifest.entries = new_entries
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)

    await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_meta = json.loads(await storage.get(f"{TARGET}/_meta.json"))
    assert target_meta["space_id"] == TARGET
    # graph_memory LOCAL préservé ; le bloc source n'est jamais appliqué.
    assert target_meta["graph_memory"]["token"] == "targetlocal"
    assert "evilsource" not in json.dumps(target_meta)
    # Champ descriptif partagé hérité de la source.
    assert target_meta["description"] == "demo"


async def test_import_refuses_corrupt_imported_hivemind_state(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un fichier _hivemind checksum-valide mais sémantiquement
    corrompu (ici term.json) ne doit pas passer HEALTHY : la validation finale
    relit TOUT l'état importé (load_snapshot + events + acks) et laisse UNSAFE,
    plutôt que de lever CorruptedStateError au premier cold-start."""
    import hashlib

    peer_keys = await _seed_source(storage)
    await _seed_blank_target(storage)
    service = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await service.export_snapshot(SOURCE)

    bad = "{ ceci n'est pas un term.json valide"
    new_entries = []
    for entry in snapshot.manifest.entries:
        if entry.path == "_hivemind/term.json":
            snapshot.files["_hivemind/term.json"] = bad
            new_entries.append(
                entry.model_copy(
                    update={
                        "sha256": hashlib.sha256(bad.encode("utf-8")).hexdigest(),
                        "size": len(bad),
                    }
                )
            )
        else:
            new_entries.append(entry)
    snapshot.manifest.entries = new_entries
    snapshot.manifest.manifest_sha256 = manifest_content_hash(snapshot.manifest)

    with pytest.raises(BootstrapError):
        await service.import_snapshot(TARGET, snapshot, peer_keys)

    target_store = HivemindStateStore(storage=storage, space_id=TARGET)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    assert health is not None
    assert HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE


async def test_is_hivemind_space_true_for_unsafe_partial_import(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Un import partiel ayant posé node_status=UNSAFE avant
    node.json ne doit PAS être classé non-Hivemind : sinon un caller écrirait
    par-dessus un demi-import en bypassant le chemin fail-closed."""
    from live_mem.core.hivemind import NodeHealth
    from live_mem.core.hivemind.lifecycle import (
        is_hivemind_space,
        resolve_hive_context,
    )

    store = HivemindStateStore(storage=storage, space_id="halfimport")  # type: ignore[arg-type]
    # Uniquement node_status=UNSAFE : ni node.json ni membership.
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="crash mid-import")
    )
    assert await is_hivemind_space(storage, "halfimport") is True
    ctx = await resolve_hive_context(storage, "halfimport")
    assert ctx.node_status == HiveNodeStatus.UNSAFE


async def test_export_refused_when_source_not_active_member(
    storage: FakeStorage,
) -> None:
    """[re-review P2] Exporter depuis un node source NON-ACTIVE (évincé, alors
    qu'un autre pair reste actif) est refusé : le snapshot porterait un
    source_node_id non-actif qu'un peer importé rejetterait ensuite."""
    from live_mem.core.hivemind import MemberStatus

    await _seed_source(storage)  # source + peer tous deux ACTIVE
    src_store = HivemindStateStore(storage=storage, space_id=SOURCE)  # type: ignore[arg-type]
    view = await src_store.get_membership()
    assert view is not None
    new_members = [
        (
            m.model_copy(update={"status": MemberStatus.EVICTED.value})
            if m.node_id == SOURCE_NODE_ID
            else m
        )
        for m in view.members
    ]
    await src_store.set_membership(
        MembershipView(epoch=view.epoch + 1, members=new_members)
    )
    service = BootstrapService(storage)  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await service.export_snapshot(SOURCE)
