# -*- coding: utf-8 -*-
"""
Tests pour issue #51 (P3-2) — verdict de routage WriteSink fail-closed.

P3-2 est purement ADDITIF : il consomme le resolver fail-closed PR#17
(``resolve_hive_context`` / ``is_hivemind_space`` / ``HiveContext`` /
``HiveNodeStatus`` dans ``lifecycle.py``) et ajoute UN helper read-only
(``resolve_write_route`` + le mapper pur ``route_for_context`` + l'enum
``WriteRoute``) qui mappe la paire ``(is_hive, node_status)`` vers une décision
de routage de sink (ADR-0007).

Trois sorties (SHARED CONTRACT routing_verdict) :
- non-Hivemind propre -> ``DIRECT_LOCAL`` (SEUL chemin vers direct-local) ;
- Hivemind valide+sain -> ``STAGED`` ;
- Hivemind corrompu/unsafe/resync -> ``REFUSE`` (JAMAIS direct-local).

Invariant fail-closed CRITIQUE (ADR-0007/ADR-0008) : une corruption d'état
(``CorruptedStateError``) PROPAGE non rattrapée à travers le seam de routage —
elle n'est JAMAIS convertie en ``DIRECT_LOCAL`` (« never not shared »). C'est ce
que ``test_route_corrupted_critical_is_not_not_shared_and_refuses`` verrouille.

Déterministe : ``FakeStorage`` in-memory (réutilisée de
``tests.test_hivemind_state``), aucune I/O S3/réseau/LLM.
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    CorruptedStateError,
    HiveContext,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    WriteRoute,
    generate_peer_keypair,
    is_hivemind_space,
    layout,
    resolve_hive_context,
    resolve_write_route,
    route_for_context,
)
from tests.test_hivemind_state import FakeStorage


SPACE = "routing-space"


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


async def _seed_healthy_hive(storage: FakeStorage, space_id: str) -> None:
    """Hivemind structurellement complet et sain : node.json + >= 1 membre
    ACTIVE portant une vraie clé Ed25519 (``generate_peer_keypair``), aucun
    marqueur node_status (donc HEALTHY par défaut via ``resolve_hive_context``).
    Mirroir du pattern ``_seed_source`` de test_hivemind_bootstrap."""
    store = HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]
    keys = generate_peer_keypair()
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[Member(node_id="n1", public_key=keys.public_key)],
        )
    )


# =============================================================================
# Routage end-to-end (resolve_write_route) — les quatre scénarios requis + plus
# =============================================================================


async def test_route_clean_non_hivemind_is_direct_local(
    storage: FakeStorage,
) -> None:
    """Space VIERGE (aucun préfixe _hivemind/) : is_hive False, node_status
    DISABLED, et ``resolve_write_route`` == DIRECT_LOCAL — l'UNIQUE chemin vers
    le sink direct-local (parité octet-pour-octet legacy)."""
    ctx = await resolve_hive_context(storage, SPACE)
    assert ctx.is_hive is False
    assert ctx.node_status == HiveNodeStatus.DISABLED

    route = await resolve_write_route(storage, SPACE)
    assert route == WriteRoute.DIRECT_LOCAL


async def test_route_valid_hivemind_healthy_is_staged(
    storage: FakeStorage,
) -> None:
    """Hivemind valide et sain (node.json + membre ACTIVE, pas de marqueur
    unsafe) : node_status HEALTHY, et ``resolve_write_route`` == STAGED — une
    écriture partagée légitime passe par le single-writer (#8), jamais direct."""
    await _seed_healthy_hive(storage, SPACE)

    ctx = await resolve_hive_context(storage, SPACE)
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.HEALTHY

    route = await resolve_write_route(storage, SPACE)
    assert route == WriteRoute.STAGED


async def test_route_corrupted_critical_is_not_not_shared_and_refuses(
    storage: FakeStorage,
) -> None:
    """LOAD-BEARING fail-closed (SHARED CONTRACT : « CorruptedStateError ... is
    NEVER caught at the routing seam »).

    Une corruption de ``node.json`` (le premier fichier lu par
    ``resolve_hive_context``) fait PROPAGER ``CorruptedStateError`` à travers
    ``resolve_write_route`` : le seam ne l'avale PAS et ne renvoie SURTOUT pas
    ``DIRECT_LOCAL``. Un space corrompu ne doit jamais être vu comme « non
    partagé » (sinon un write partagé bypasserait le token -> split-brain)."""
    # Injection directe de JSON cassé sur node.json (pattern test_hivemind_state).
    storage.objects[layout.node_key(SPACE)] = "{not valid json"

    # Le verdict NE doit jamais être DIRECT_LOCAL : la corruption fail-close.
    with pytest.raises(CorruptedStateError):
        await resolve_write_route(storage, SPACE)

    # Défense explicite de l'invariant « never not shared » : aucun chemin ne
    # renvoie silencieusement DIRECT_LOCAL pour une corruption.
    try:
        route = await resolve_write_route(storage, SPACE)
    except CorruptedStateError:
        route = None
    assert route is not WriteRoute.DIRECT_LOCAL


async def test_route_partially_initialized_is_refuse(
    storage: FakeStorage,
) -> None:
    """État Hivemind PARTIEL : node.json présent SANS membre ACTIVE (init
    interrompue / restore partiel). Le resolver fail-closed le classe is_hive
    True + UNSAFE (jamais « local ») ; le verdict est REFUSE, jamais
    DIRECT_LOCAL. Mirroir de test_resolve_hive_context_partial_state_is_unsafe."""
    keys = generate_peer_keypair()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_identity(
        NodeIdentity(node_id="n1", public_key=keys.public_key)
    )
    # Aucune membership écrite : structure incomplète.

    ctx = await resolve_hive_context(storage, SPACE)
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.UNSAFE

    route = await resolve_write_route(storage, SPACE)
    assert route == WriteRoute.REFUSE


async def test_route_unsafe_marker_refuses(storage: FakeStorage) -> None:
    """Marqueur explicite node_status=UNSAFE seul (demi-import ayant posé UNSAFE
    avant node.json) : is_hive True (jamais « local ») et verdict REFUSE.
    Mirroir de test_is_hivemind_space_true_for_unsafe_partial_import."""
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="crash mid-import")
    )

    ctx = await resolve_hive_context(storage, SPACE)
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.UNSAFE

    route = await resolve_write_route(storage, SPACE)
    assert route == WriteRoute.REFUSE


async def test_route_resync_required_refuses(storage: FakeStorage) -> None:
    """Marqueur explicite RESYNC_REQUIRED sur un hive par ailleurs
    structurellement complet : la branche marqueur (lifecycle.py) fait foi,
    node_status == RESYNC_REQUIRED, verdict REFUSE — un node qui a observé un
    epoch futur doit resync avant de muter, jamais direct-local."""
    await _seed_healthy_hive(storage, SPACE)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_status(
        NodeHealth(
            status=HiveNodeStatus.RESYNC_REQUIRED,
            reason="epoch futur observé",
            observed_epoch=5,
        )
    )

    ctx = await resolve_hive_context(storage, SPACE)
    assert ctx.is_hive is True
    assert ctx.node_status == HiveNodeStatus.RESYNC_REQUIRED

    route = await resolve_write_route(storage, SPACE)
    assert route == WriteRoute.REFUSE


# =============================================================================
# Mapper pur (route_for_context) — table de routage en isolation, sans storage
# =============================================================================


def _ctx(is_hive: bool, node_status: HiveNodeStatus) -> HiveContext:
    return HiveContext(
        space_id=SPACE,
        is_hive=is_hive,
        node=None,
        membership=None,
        node_status=node_status,
    )


def test_route_for_context_pure_mapping() -> None:
    """Unit test du mapper PUR sur des HiveContext fabriqués à la main, pour les
    quatre combinaisons (is_hive, node_status) + le cas défensif théoriquement
    inatteignable is_hive+DISABLED -> REFUSE (jamais DIRECT_LOCAL). Épingle la
    table de routage sans toucher au storage."""
    # is_hive == False (DISABLED) -> DIRECT_LOCAL — l'UNIQUE chemin direct.
    assert (
        route_for_context(_ctx(False, HiveNodeStatus.DISABLED))
        == WriteRoute.DIRECT_LOCAL
    )
    # is_hive True + HEALTHY -> STAGED.
    assert (
        route_for_context(_ctx(True, HiveNodeStatus.HEALTHY)) == WriteRoute.STAGED
    )
    # is_hive True + UNSAFE / RESYNC_REQUIRED -> REFUSE.
    assert (
        route_for_context(_ctx(True, HiveNodeStatus.UNSAFE)) == WriteRoute.REFUSE
    )
    assert (
        route_for_context(_ctx(True, HiveNodeStatus.RESYNC_REQUIRED))
        == WriteRoute.REFUSE
    )
    # Défense en profondeur : is_hive True mais DISABLED (jamais produit par
    # resolve_hive_context, qui garantit is_hive==False <=> DISABLED) ->
    # REFUSE, JAMAIS DIRECT_LOCAL. Seul is_hive==False atteint direct-local.
    assert (
        route_for_context(_ctx(True, HiveNodeStatus.DISABLED)) == WriteRoute.REFUSE
    )


# =============================================================================
# Sanity : consommation du resolver PR#17 câblée via les exports __init__
# =============================================================================


async def test_is_hivemind_space_true_for_valid_node_json(
    storage: FakeStorage,
) -> None:
    """Ré-assertion du critère d'acceptation : ``is_hivemind_space`` est True
    pour un space avec un _hivemind/node.json + membre ACTIVE valides, et False
    pour un space vierge. Confirme que P3-2 consomme bien le resolver existant
    (et que les helpers sont importables via les exports du package)."""
    assert await is_hivemind_space(storage, SPACE) is False

    await _seed_healthy_hive(storage, SPACE)
    assert await is_hivemind_space(storage, SPACE) is True
