# -*- coding: utf-8 -*-
"""
Tests de l'allowlist métadonnées partagé/local (P2-2, ADR-0012).

Protègent la frontière de réplication ``_meta.json`` :
- ``SHARED_META_FIELDS`` ne dérive pas de ``SpaceMeta`` (lock anti-drift) ;
- ``meta_shared_projection`` est *deny-by-default* (champ inconnu -> exclu) et
  exclut tout le bloc ``graph_memory`` (token compris) ;
- ``meta_local_complement`` est le complément sans perte (round-trip) ;
- les helpers ne mutent jamais l'entrée.

Le bloc ``graph_memory`` est local-only et ne doit JAMAIS atteindre un commit
partagé (PR #17 + P2-2). Fichier de test distinct de P2-1/P2-3 pour garder les
branches Vague 1 disjointes.
"""

import copy

from live_mem.core.models import (
    SHARED_META_FIELDS,
    SpaceMeta,
    meta_shared_projection,
    meta_local_complement,
)


def _full_meta() -> dict:
    """Un _meta.json représentatif avec graph_memory peuplé (token inclus)."""
    return {
        "space_id": "demo",
        "description": "Demo space",
        "owner": "alice",
        "created_at": "2026-06-17T00:00:00",
        "last_consolidation": "2026-06-17T01:00:00",
        "consolidation_count": 3,
        "total_notes_processed": 42,
        "version": 1,
        "graph_memory": {
            "url": "https://graph.example/mcp",
            "token": "lm_supersecret_value_1234567890",
            "memory_id": "mem-1",
            "ontology": "general",
            "last_push": "2026-06-17T02:00:00",
            "push_count": 5,
            "files_pushed": 7,
        },
    }


def test_shared_meta_fields_locked_to_spacemeta_model():
    """Anti-drift : toute nouvelle clé de SpaceMeta force une classification."""
    model_fields = set(SpaceMeta.model_fields)  # pydantic v2
    assert model_fields - {"graph_memory"} == set(SHARED_META_FIELDS), (
        "SHARED_META_FIELDS a dérivé de SpaceMeta : un champ ajouté/retiré doit "
        "être classé explicitement (partagé -> ajouter ici ; local -> ne pas "
        "ajouter, mais réviser ce test)"
    )
    # L'invariant documenté « 8 champs partagés » est épinglé explicitement.
    assert len(SHARED_META_FIELDS) == 8


def test_projection_deny_by_default_unknown_field_excluded():
    meta = {**_full_meta(), "mystery_field": "should-not-replicate"}
    projected = meta_shared_projection(meta)
    assert "mystery_field" not in projected
    # Auto-suffisant : une projection stub-vide passerait le `not in` ci-dessus,
    # donc on épingle aussi la surface exacte (mutation-proof contre un revert).
    assert set(projected) == set(SHARED_META_FIELDS)


def test_projection_excludes_entire_graph_memory_block():
    projected = meta_shared_projection(_full_meta())
    assert "graph_memory" not in projected
    # Aucune sous-clé de graph_memory ne fuit (memory_id/url/...) et les champs
    # partagés sont bien présents — auto-suffisant contre une projection vide.
    assert "memory_id" not in projected and "url" not in projected
    assert "space_id" in projected


def test_projection_passes_all_eight_shared_fields():
    projected = meta_shared_projection(_full_meta())
    for field in SHARED_META_FIELDS:
        assert field in projected, f"champ partagé manquant dans la projection : {field}"
    # Et rien d'autre que les champs partagés.
    assert set(projected) == set(SHARED_META_FIELDS)


def test_projection_none_input_returns_none_and_no_mutation():
    assert meta_shared_projection(None) is None
    meta = _full_meta()
    snapshot = copy.deepcopy(meta)
    meta_shared_projection(meta)
    assert meta == snapshot, "meta_shared_projection a muté son entrée"


def test_meta_shared_projection_local_complement_round_trip():
    fixtures = [
        _full_meta(),
        {**_full_meta(), "graph_memory": {}},  # graph_memory vide
        {  # champs optionnels manquants
            "space_id": "minimal",
            "version": 1,
        },
        {**_full_meta(), "mystery_field": "x"},  # champ inconnu -> local
    ]
    for meta in fixtures:
        local = meta_local_complement(meta)
        shared = meta_shared_projection(meta)
        assert {**local, **shared} == meta, f"round-trip cassé pour {meta!r}"
        # Partition stricte : les deux moitiés sont disjointes (pas de clé qui
        # serait à la fois partagée et locale -> verrouille la propriété).
        assert set(local).isdisjoint(shared), f"moitiés non disjointes pour {meta!r}"
        # Le complément local porte bien graph_memory et le champ inconnu.
        if "graph_memory" in meta:
            assert "graph_memory" in local
        if "mystery_field" in meta:
            assert "mystery_field" in local

    assert meta_local_complement(None) is None
    meta = _full_meta()
    snapshot = copy.deepcopy(meta)
    meta_local_complement(meta)
    assert meta == snapshot, "meta_local_complement a muté son entrée"


def test_token_never_in_shared_projection():
    meta = _full_meta()
    secret = meta["graph_memory"]["token"]
    projected = meta_shared_projection(meta)
    # Le secret n'apparaît nulle part dans la sortie partagée (sécurité).
    assert secret not in repr(projected)
    assert all(secret not in repr(v) for v in projected.values())
