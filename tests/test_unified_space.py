# -*- coding: utf-8 -*-
"""
Tests du modèle d'espace unifié (ADR-0004).

Ces tests protègent le contrat ``src/live_mem/core/unified_space.py`` :
- l'import est sans effet de bord (pas d'accès stockage/réseau) ;
- le mapping concern -> location ne dérive pas des clés S3 réellement utilisées
  (HIVE par égalité de symbole avec ``layout.py`` ; SHORT/MID par présence des
  littéraux dans la source, car ce sont des f-strings inline non importables) ;
- ADR-0004 cite les trois invariants et le doc de conception n'utilise plus le
  chemin de fichier ADR en casse haute (``docs/adr/ADR-0004``).
"""

import inspect
from pathlib import Path

import pytest

from live_mem.core import unified_space
from live_mem.core.unified_space import (
    Concern,
    OWNED_CONCERNS,
    META_JSON_KEY_TEMPLATE,
    META_JSON_OWNERS,
)
from live_mem.core.hivemind import layout
from live_mem.core.hivemind import lifecycle
from live_mem.core.models import SpaceMeta, SHARED_META_FIELDS
from live_mem.core import live as live_module
from live_mem.core import space as space_module
from live_mem.core import consolidator as consolidator_module

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_unified_space_concern_enum_importable_no_side_effects():
    """Le module expose les 4 concerns ; aucune ressource résolue sans space_id."""
    assert set(OWNED_CONCERNS) == set(Concern)
    assert {c.value for c in Concern} == {"short", "mid", "long", "hive"}
    # Les clés ne sont jamais des constantes globales pré-calculées : il faut un
    # space_id explicite (preuve indirecte d'absence d'état/accès au boot).
    assert "{space_id}" in META_JSON_KEY_TEMPLATE


def test_short_concern_maps_to_live_prefix():
    loc = OWNED_CONCERNS[Concern.SHORT]
    assert loc.prefixes("my-space") == ("my-space/live/",)
    assert loc.objects("my-space") == ()


def test_mid_concern_maps_to_bank_rules_synthesis_and_meta_fields():
    loc = OWNED_CONCERNS[Concern.MID]
    assert loc.prefixes("my-space") == ("my-space/bank/",)
    assert set(loc.objects("my-space")) == {
        "my-space/_rules.md",
        "my-space/_synthesis.md",
    }
    # Les compteurs de consolidation sont des CHAMPS de _meta.json, pas des objets.
    assert set(loc.meta_fields) == {
        "last_consolidation",
        "consolidation_count",
        "total_notes_processed",
    }
    # Cross-check anti-dérive contre l'autorité importable (pas une simple
    # ré-affirmation des littéraux) : les compteurs MID sont de vrais champs
    # SpaceMeta ET sont classés partageables.
    assert set(loc.meta_fields) <= set(SpaceMeta.model_fields)
    assert set(loc.meta_fields) <= set(SHARED_META_FIELDS)


def test_long_concern_is_graph_memory_block_in_meta_no_s3_projection_key():
    loc = OWNED_CONCERNS[Concern.LONG]
    assert loc.meta_fields == ("graph_memory",)
    # Long owns no S3 key under {space_id}/ (projection held by the long engine,
    # not the space prefix) — an addressing fact, not "long is external".
    assert loc.owns_no_space_s3_key is True
    # Cross-check anti-dérive : graph_memory est un vrai champ SpaceMeta MAIS
    # local-only (jamais dans l'allowlist partagée) — l'autorité, pas un littéral.
    assert "graph_memory" in SpaceMeta.model_fields
    assert "graph_memory" not in SHARED_META_FIELDS
    # Aucune clé S3 propre sous l'espace : la projection/index est tenue par le
    # moteur long (interne, non-autoritaire), pas sous le préfixe {space_id}/.
    assert loc.prefixes("my-space") == ()
    assert loc.objects("my-space") == ()


def test_hive_concern_uses_layout_builders_not_hardcoded():
    loc = OWNED_CONCERNS[Concern.HIVE]
    # Préfixe construit par layout.py (jamais un littéral '_hivemind/' codé en dur
    # dans le mapping).
    assert loc.s3_prefix_templates == ()
    assert loc.s3_object_templates == ()
    assert loc.prefixes("my-space") == (layout.HIVEMIND_PREFIX("my-space"),)


def test_meta_json_is_split_ownership():
    # _meta.json n'est possédé exclusivement par aucun concern : MID (compteurs)
    # + LONG (graph_memory) + champs d'identité.
    assert set(META_JSON_OWNERS) == {Concern.MID, Concern.LONG}
    assert "graph_memory" in OWNED_CONCERNS[Concern.LONG].meta_fields
    assert "consolidation_count" in OWNED_CONCERNS[Concern.MID].meta_fields


def test_concern_location_mapping_matches_live_keys_no_drift():
    """Anti-drift : HIVE par égalité de symbole ; SHORT/MID par présence source."""
    sid = "drift-probe"
    # HIVE : égalité exacte avec les builders layout.py.
    hive = OWNED_CONCERNS[Concern.HIVE]
    assert hive.prefixes(sid) == (layout.HIVEMIND_PREFIX(sid),)

    # SHORT/MID : les clés sont des f-strings inline dans live.py / space.py /
    # consolidator.py (pas de constante importable). On vérifie que les
    # templates apparaissent littéralement dans la source — un rename/suppression
    # d'une vraie clé casse ce test.
    sources = "\n".join(
        inspect.getsource(m)
        for m in (live_module, space_module, consolidator_module)
    )
    for concern in (Concern.SHORT, Concern.MID):
        loc = OWNED_CONCERNS[concern]
        for template in loc.s3_prefix_templates + loc.s3_object_templates:
            assert template in sources, (
                f"clé {template!r} du concern {concern.value} absente de la "
                "source live/space/consolidator — dérive du mapping unifié"
            )


def test_node_local_hive_paths_not_marked_shared():
    """node.json / node_status.json sont node-locaux, cohérents avec lifecycle."""
    sid = "s"
    objs = OWNED_CONCERNS[Concern.HIVE].node_local_objects(sid)
    relative = {key[len(sid) + 1 :] for key in objs}  # strip "s/"
    assert relative == set(lifecycle._NODE_LOCAL_HIVEMIND_PATHS)


def test_no_stale_uppercase_adr0004_filesystem_path():
    """Le doc de conception référence le fichier ADR on-disk, pas la casse haute 'ADR-0004'.

    Scopé au doc de conception de l'espace unifié ; les refs 'docs/adr/ADR-XXXX'
    stale ailleurs relèvent d'un follow-up de numérotation ADR distinct.
    Skip si le doc interne (non public) est absent du checkout.
    """
    epic_path = _REPO_ROOT / "DESIGN/hivemind/epics/EPIC-P2-unified-space.md"
    if not epic_path.exists():
        pytest.skip("internal EPIC doc not present in this checkout")
    epic = epic_path.read_text(encoding="utf-8")
    assert "docs/adr/ADR-0004" not in epic
    assert "docs/adr/0004-unified-space-model.md" in epic


def test_adr0004_invariants_preserved_section_present():
    """Les 3 invariants P2-1 sont cités DANS la section Invariants Preserved.

    Le grep est scopé à la tranche de cette section (et non au fichier entier) :
    sinon supprimer le corps de la section laisserait le test vert alors que le
    critère d'acceptation #2 exige précisément cette section.
    """
    adr_path = _REPO_ROOT / "docs/adr/0004-unified-space-model.md"
    if not adr_path.exists():
        pytest.skip("docs/adr/0004-unified-space-model.md is private-only (absent from the public release tree)")
    adr = adr_path.read_text(encoding="utf-8")
    header = "## Invariants Preserved"
    assert header in adr
    start = adr.index(header) + len(header)
    rest = adr[start:]
    nxt = rest.find("\n## ")
    section = rest if nxt == -1 else rest[:nxt]
    for invariant in (
        "no separate space taxonomies",
        "long stays outside the commit path",
        "OSS mono-tenant",
    ):
        assert invariant in section, (
            f"invariant absent de la section Invariants Preserved : {invariant!r}"
        )
