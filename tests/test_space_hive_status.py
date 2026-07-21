# -*- coding: utf-8 -*-
"""
Tests P2-4 (issue #36) — câblage du label de statut unifié sur les surfaces
de LECTURE de ``SpaceService`` (get_summary, export_space, get_info),
fail-closed (CorruptedStateError -> "unsafe" lié AU NIVEAU SERVICE).

Invariants vérifiés :
- chacune des 3 surfaces de succès retourne un champ ADDITIF
  ``hive_status_label`` reflétant ``hive_status_label()`` (espace 6-valeurs,
  distinct de la clé 4-valeurs ``hive_status`` de #10) ;
- espace non-Hivemind -> ``"local_only"`` ET tous les autres champs sont
  byte/forme-identiques à la baseline pré-P2 (golden) ;
- hive sain seedé -> ``"hivemind_healthy"`` ;
- corruption de node/members/node_status.json -> la surface NE LÈVE PAS, reste
  ``status=="ok"`` et remonte ``"unsafe"`` (NI ``local_only`` NI ``not_a_space``)
  — c'est le test fail-closed load-bearing ;
- ``graph_memory.token`` n'apparaît jamais dans space_summary/space_export ;
- le chemin d'écriture ``update()`` persiste TOUJOURS le document _meta.json
  complet (jamais une projection lossy).

Fichier distinct (mirroir de tests/test_hive_status_label.py) pour garder les
branches Vague-2 disjointes. On patche ``live_mem.core.space.get_storage`` (le
service lie get_storage localement) pour retourner une FakeStorage in-memory.
"""

import json

import pytest

from live_mem.core import space as space_module
from live_mem.core.hivemind import (
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    generate_peer_keypair,
)
from tests.test_hivemind_state import FakeStorage as _BaseFakeStorage

SPACE = "p2-4-space"
NODE_ID = "nodep24000000000000000000000000aa"
GM_TOKEN = "SECRET-graph-token-abcdef-0123456789-do-not-leak"


# ─────────────────────────────────────────────────────────────
# FakeStorage étendu — la FakeStorage partagée (tests/test_hivemind_state.py)
# couvre l'API HivemindStateStore (put/put_json/get/get_json/delete/
# list_objects/exists) mais PAS list_and_get / list_prefixes, que
# get_summary / export_space appellent. On sous-classe ICI (jamais muter la
# fixture partagée) en restant fidèle à la sémantique de StorageService :
# list_and_get retourne des dicts {'key','content','size','last_modified'}
# (casse minuscule), exclude_keep défaut True (cf. src/.../storage.py:418).
# ─────────────────────────────────────────────────────────────


class FakeStorage(_BaseFakeStorage):
    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        objects = await self.list_objects(prefix)
        results: list[dict] = []
        for obj in objects:
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = await self.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": str(obj.get("LastModified", "")),
                    }
                )
        return results

    async def list_prefixes(self, prefix: str, delimiter: str = "/") -> list[str]:
        prefixes: set[str] = set()
        plen = len(prefix)
        for key in self.objects:
            if not key.startswith(prefix):
                continue
            rest = key[plen:]
            if delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
        return sorted(prefixes)


# ─────────────────────────────────────────────────────────────
# Helpers de seed
# ─────────────────────────────────────────────────────────────


async def _seed_meta(storage: FakeStorage, space_id: str = SPACE, *, graph_memory=None) -> None:
    """Seed un _meta.json valide (+ rules/bank) pour que les surfaces soient ok."""
    meta = {
        "space_id": space_id,
        "description": "desc-baseline",
        "owner": "owner-baseline",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_consolidation": None,
        "consolidation_count": 0,
        "total_notes_processed": 0,
        "version": 1,
    }
    if graph_memory is not None:
        meta["graph_memory"] = graph_memory
    await storage.put(f"{space_id}/_meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
    await storage.put(f"{space_id}/_rules.md", "# Rules\n")
    await storage.put(f"{space_id}/live/.keep", "")
    await storage.put(f"{space_id}/bank/.keep", "")
    await storage.put(f"{space_id}/bank/activeContext.md", "ctx body")


async def _seed_healthy_hive(storage: FakeStorage, space_id: str = SPACE) -> HivemindStateStore:
    """node.json + >=1 membre ACTIVE + _meta.json présent => hivemind_healthy."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(NodeIdentity(node_id=NODE_ID, public_key=keys.public_key))
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
    return store


@pytest.fixture
def patched_storage(monkeypatch) -> FakeStorage:
    """FakeStorage in-memory injectée via le get_storage local de space.py."""
    storage = FakeStorage()
    monkeypatch.setattr(space_module, "get_storage", lambda: storage)
    return storage


@pytest.fixture
def stub_consolidation_queue(monkeypatch):
    """
    get_info fait un import paresseux
    ``from .consolidation_queue import get_consolidation_queue`` puis
    ``await get_consolidation_queue().get_space_summary(space_id)``.

    Le binding local résout l'attribut SUR LE MODULE consolidation_queue au
    moment de l'appel : on patche donc
    ``live_mem.core.consolidation_queue.get_consolidation_queue`` (pas
    ``space.get_consolidation_queue`` — qui n'existe pas) pour rester offline.
    """
    from live_mem.core import consolidation_queue as cq_module

    class _StubQueue:
        async def get_space_summary(self, space_id: str) -> dict:
            return {"pending": 0, "spaces": []}

    monkeypatch.setattr(cq_module, "get_consolidation_queue", lambda: _StubQueue())
    return _StubQueue


# ─────────────────────────────────────────────────────────────
# 1) Chaque surface retourne hive_status_label
# ─────────────────────────────────────────────────────────────


async def test_space_summary_includes_hive_status_label(patched_storage):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    resp = await space_module.SpaceService().get_summary(SPACE)
    assert resp["status"] == "ok"
    assert resp["hive_status_label"] == "hivemind_healthy"


async def test_space_export_includes_hive_status_label(patched_storage):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    resp = await space_module.SpaceService().export_space(SPACE)
    assert resp["status"] == "ok"
    assert resp["hive_status_label"] == "hivemind_healthy"


async def test_space_info_includes_hive_status_label(patched_storage, stub_consolidation_queue):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    resp = await space_module.SpaceService().get_info(SPACE)
    assert resp["status"] == "ok"
    assert resp["hive_status_label"] == "hivemind_healthy"


# ─────────────────────────────────────────────────────────────
# 2) Non-Hivemind -> "local_only" + golden equality des autres champs
# ─────────────────────────────────────────────────────────────


async def test_non_hivemind_summary_local_only_and_golden(patched_storage):
    await _seed_meta(patched_storage)
    resp = await space_module.SpaceService().get_summary(SPACE)
    assert resp["hive_status_label"] == "local_only"

    # Golden : après retrait du SEUL champ additif, le reste == ce que la
    # méthode pré-P2 aurait produit pour les mêmes objets seedés.
    complement = {k: v for k, v in resp.items() if k != "hive_status_label"}
    expected = {
        "status": "ok",
        "space_id": SPACE,
        "description": "desc-baseline",
        "rules": "# Rules\n",
        "bank_files": [{"filename": "activeContext.md", "content": "ctx body", "size": 8}],
        "bank_file_count": 1,
        "synthesis": None,
    }
    assert complement == expected


async def test_non_hivemind_export_local_only_and_golden(patched_storage):
    await _seed_meta(patched_storage)
    resp = await space_module.SpaceService().export_space(SPACE)
    assert resp["hive_status_label"] == "local_only"

    complement = {k: v for k, v in resp.items() if k != "hive_status_label"}
    # archive_base64 / archive_size dépendent du gzip (timestamp) : on vérifie
    # les clés et les champs déterministes, pas les octets compressés.
    assert set(complement.keys()) == {
        "status",
        "space_id",
        "archive_base64",
        "archive_size",
        "files_count",
    }
    assert complement["status"] == "ok"
    assert complement["space_id"] == SPACE
    assert complement["files_count"] == 5  # _meta + _rules + 2 .keep + bank/activeContext


async def test_non_hivemind_info_local_only_and_golden(patched_storage, stub_consolidation_queue):
    await _seed_meta(patched_storage)
    resp = await space_module.SpaceService().get_info(SPACE)
    assert resp["hive_status_label"] == "local_only"

    complement = {k: v for k, v in resp.items() if k != "hive_status_label"}
    expected = {
        "status": "ok",
        "space_id": SPACE,
        "description": "desc-baseline",
        "owner": "owner-baseline",
        "created_at": "2026-01-01T00:00:00+00:00",
        "live": {"notes_count": 0, "total_size": 0},
        "bank": {
            "files_count": 1,
            "total_size": 8,
            "files": ["activeContext.md"],
        },
        "last_consolidation": None,
        "consolidation_count": 0,
        "consolidation_queue": {"pending": 0, "spaces": []},
        "synthesis_exists": False,
    }
    assert complement == expected


async def test_not_found_early_return_shape_unchanged(patched_storage):
    # Verrou de la décision « champ uniquement sur le chemin succès » : le
    # not_found garde EXACTEMENT sa forme 2-clés, sans hive_status_label.
    resp = await space_module.SpaceService().get_summary("does-not-exist")
    assert resp == {
        "status": "not_found",
        "message": "Espace 'does-not-exist' introuvable",
    }
    assert "hive_status_label" not in resp


# ─────────────────────────────────────────────────────────────
# 3) LOAD-BEARING : corruption -> "unsafe", surface ne lève PAS, status ok
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
async def test_corrupted_summary_is_unsafe_not_local_no_raise(patched_storage, corrupt_file):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    # Corrompt un fichier du read-set de détection sous _hivemind/, en gardant
    # un _meta.json VALIDE (un _meta.json corrompu serait un autre cas).
    await patched_storage.put(f"{SPACE}/_hivemind/{corrupt_file}", "{not valid json")

    resp = await space_module.SpaceService().get_summary(SPACE)
    assert resp["status"] == "ok"  # la surface reste utilisable
    assert resp["hive_status_label"] == "unsafe"
    assert resp["hive_status_label"] not in ("local_only", "not_a_space")


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
async def test_corrupted_export_is_unsafe_not_local_no_raise(patched_storage, corrupt_file):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    await patched_storage.put(f"{SPACE}/_hivemind/{corrupt_file}", "{not valid json")

    resp = await space_module.SpaceService().export_space(SPACE)
    assert resp["status"] == "ok"
    assert resp["hive_status_label"] == "unsafe"
    assert resp["hive_status_label"] not in ("local_only", "not_a_space")


@pytest.mark.parametrize("corrupt_file", ["node.json", "members.json", "node_status.json"])
async def test_corrupted_info_is_unsafe_not_local_no_raise(
    patched_storage, stub_consolidation_queue, corrupt_file
):
    await _seed_meta(patched_storage)
    await _seed_healthy_hive(patched_storage)
    await patched_storage.put(f"{SPACE}/_hivemind/{corrupt_file}", "{not valid json")

    resp = await space_module.SpaceService().get_info(SPACE)
    assert resp["status"] == "ok"
    assert resp["hive_status_label"] == "unsafe"
    assert resp["hive_status_label"] not in ("local_only", "not_a_space")


# ─────────────────────────────────────────────────────────────
# 4) graph_memory.token jamais exposé dans summary/export
# ─────────────────────────────────────────────────────────────


async def test_graph_memory_token_absent_from_summary_and_export(patched_storage):
    gm = {
        "url": "http://gm.example/mcp",
        "token": GM_TOKEN,
        "memory_id": "mem-1",
        "ontology": "general",
    }
    await _seed_meta(patched_storage, graph_memory=gm)

    summary = await space_module.SpaceService().get_summary(SPACE)
    # get_summary n'émet jamais graph_memory : le token brut est absent du dump
    # ENTIER (anti-régression : aucune imbrication accidentelle).
    assert GM_TOKEN not in json.dumps(summary)
    # Le test exerce bien le nouveau chemin (sinon vacuous).
    assert summary["hive_status_label"] == "local_only"

    export = await space_module.SpaceService().export_space(SPACE)
    # export masque via mask_meta_secrets : le token BRUT n'apparaît jamais,
    # mais la forme masquée "<prefix>..." (8 premiers chars + ellipse) si.
    import base64 as _b64
    import io as _io
    import tarfile as _tf

    raw = _b64.b64decode(export["archive_base64"])
    with _tf.open(fileobj=_io.BytesIO(raw), mode="r:gz") as tar:
        member = tar.extractfile("_meta.json")
        meta_in_archive = member.read().decode("utf-8")
    assert GM_TOKEN not in meta_in_archive
    assert (GM_TOKEN[:8] + "...") in meta_in_archive  # forme masquée présente
    assert export["hive_status_label"] == "local_only"


# ─────────────────────────────────────────────────────────────
# 5) WRITE PATH : update() persiste le document _meta.json COMPLET
# ─────────────────────────────────────────────────────────────


async def test_update_persists_full_meta_document_not_projected(patched_storage, monkeypatch):
    gm = {"url": "http://gm.example/mcp", "token": GM_TOKEN, "memory_id": "mem-1"}
    await _seed_meta(patched_storage, graph_memory=gm)

    # P5-8 (#16): SpaceService.update now routes the _meta.json durable write
    # through the per-space WriteSink (route-first), so the registry must resolve
    # against the SAME fake storage. SPACE here is non-Hivemind -> DIRECT_LOCAL
    # (DirectLocalWriteSink writes verbatim to the fake), which is exactly the
    # byte-for-byte full-meta-merge behaviour this golden pins.
    from live_mem.core import engines as engines_module
    from live_mem.core.engines import EngineRegistry

    registry = EngineRegistry(storage=patched_storage)
    monkeypatch.setattr(
        engines_module, "get_engine_registry", lambda: registry
    )

    await space_module.SpaceService().update(SPACE, description="new-desc")

    # Relire le _meta.json on-disk : doit rester le document COMPLET mergé,
    # jamais une projection lossy. Seul `description` a changé.
    raw = patched_storage.objects[f"{SPACE}/_meta.json"]
    persisted = json.loads(raw)
    assert persisted["description"] == "new-desc"
    assert persisted["owner"] == "owner-baseline"  # champ préservé
    assert persisted["graph_memory"] == gm  # bloc local préservé (non projeté)
    assert persisted["version"] == 1
    assert persisted["space_id"] == SPACE
    assert persisted["created_at"] == "2026-01-01T00:00:00+00:00"
