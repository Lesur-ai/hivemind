# -*- coding: utf-8 -*-
"""
Surface d'observabilité read-only & récupération manuelle Hivemind (issue #12 /
P5-4 ; ADR-0014 backup/restore-recovery, ADR-0008 contexte fail-closed).

Ce module calcule un **rapport de statut opérateur** strictement READ-ONLY pour
un space Hivemind. Il ne fabrique AUCUN verdict de santé : il réutilise
``lifecycle.hive_status_label`` (la grammaire produit 6-valeurs au-dessus du
resolver fail-closed), ``queue_runtime.select_head`` (le head de queue),
``lease_runtime.lease_is_active`` (validité de lease), les énumérateurs read du
``HivemindStateStore`` (``get_token`` / ``get_term`` /
``get_bank_version_pointer`` / ``list_queue`` / ``list_acks`` / ``list_events``)
et ``lifecycle.expected_ack_node_ids`` (l'ensemble all-ACK = IDENTITÉ sur les
membres ACTIVE, jamais un compte).

Propriété PORTANTE — ZÉRO écriture : ``compute_hive_status`` n'appelle QUE des
``get_*`` / ``list_*`` / des helpers purs (``select_head``,
``detect_seq_collisions``, ``expected_ack_node_ids``, ``lease_is_active``). Il
construit un ``HivemindStateStore`` mais ne le mute JAMAIS (aucun ``set_*`` /
``append_*`` / ``record_*`` / ``enqueue`` / ``put`` / ``delete``). Le test de
zéro-écriture (compteur ``put``/``delete`` == 0 ET snapshot des objets
byte-identique) est le garde de régression principal.

Fail-closed (ADR-0008) : AUCUN ``try/except`` ici. Une corruption d'un fichier
critique (``node.json`` / ``members.json`` / ``node_status.json`` / un ACK / un
event) lève ``CorruptedStateError`` depuis le resolver / les énumérateurs et
PROPAGE non rattrapée — un space corrompu remonte ``unsafe`` / ``resync_required``,
JAMAIS dégradé en ``disabled`` (sinon un write partagé bypasserait le token →
split-brain).

Les DÉCLENCHEURS de récupération manuelle (eviction, resync) ne vivent PAS ici :
ils délèguent aux services P5-1 existants (``MembershipService.evict_member`` /
``ResyncService``) depuis ``recovery.py``, qui n'ajoute qu'une traduction de
code d'erreur. La surface de statut reste, elle, strictement read-only et
n'enregistre AUCUN nouvel outil MCP ; la surface globale reste verrouillée par
son fixture canonique et son test exhaustif.

Codes d'erreur MCP structurés (taxonomie ``PeerErrorCode`` réutilisée, surfacés
via ``PeerChannelError.to_dict()``) distinguant les trois cas :

- ``READ_ONLY_ALLOWED`` — chemin de lecture de statut ; ``compute_hive_status``
  ne lève jamais sur des motifs permission/protocole (seulement
  ``CorruptedStateError``).
- ``PERMISSION_DENIED`` — scope opérateur manquant sur un déclencheur.
- ``PROTOCOL_BLOCKED`` — la santé du hive interdit la mutation (fail-closed).

Le raising effectif vit dans ``recovery.py`` ; cette table est documentée ici
comme contrat unique.

Isolation : ce module n'importe AUCUNE mémoire longue / graph / consolidateur,
n'utilise aucun timer ni wall-clock inline (l'horloge est injectée via le seam
``clock=`` ; le défaut ``_now_utc`` est importé de ``lease_runtime``).

Découplage parser : le calcul de TTL de lease parse ``token.lease_until`` (ISO
brut) via un helper LOCAL ``_parse_iso`` (mêmes règles que ``model._parse`` /
``peer`` : un timestamp naïf est interprété UTC). On NE dépend PAS d'un helper
privé/instable de ``lease_runtime`` : ce module est read-only et n'a besoin que
d'une lecture d'horloge — pas du contrat fail-closed ``CorruptedStateError`` de
``lease_runtime._parse_lease_until`` (la corruption d'un ``lease_until`` actif
est déjà attrapée en amont par ``lease_is_active``/``hive_status_label`` et
remonte ``unsafe``/``resync_required``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..storage import StorageService
from .layout import PROTOCOL_VERSION
from .lease_runtime import Clock, _now_utc, lease_is_active
from .lifecycle import (
    expected_ack_node_ids,
    hive_status_label,
    resolve_hive_context,
)
from .models import CorruptedStateError, _HivemindBase
from .queue_runtime import SeqCollision, detect_seq_collisions, select_head
from .state import HivemindStateStore


def _parse_iso(value: str) -> datetime:
    """Parse un timestamp ISO-8601 en ``datetime`` aware UTC (helper LOCAL,
    read-only).

    Mêmes règles que ``model._parse`` / ``peer._parse_iso`` : un ``Z`` final est
    normalisé en ``+00:00`` et un timestamp naïf est interprété UTC. DÉLIBÉRÉMENT
    découplé du privé instable ``lease_runtime._parse_lease_until`` : la surface
    de statut est strictement read-only et n'a besoin que d'une lecture
    déterministe pour calculer une TTL d'observabilité ; elle n'emprunte PAS le
    contrat fail-closed ``CorruptedStateError`` de la couche lease (un
    ``lease_until`` d'un token ACTIF corrompu est déjà attrapé en amont par
    ``lease_is_active`` / ``hive_status_label`` et remonte ``unsafe`` /
    ``resync_required``, jamais ``disabled``)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# =============================================================================
# Vocabulaire typé de statut opérateur (miroir byte-pour-byte de la référence
# test ``tests/hivemind_harness/model.py:HiveStatus``)
# =============================================================================


class HiveStatus(str, Enum):
    """
    Statut opérateur dérivé (HIVEMIND.md §6.4), calculé sur l'état réel.

    Vocabulaire de référence (miroir de la grammaire de test ProtocolModel) :
    ``{disabled, healthy, blocked, degraded, unsafe, resync_required}``.

    ``DEGRADED`` est **réservé** : il n'est PAS produit en V1 (parité de
    vocabulaire seulement). Aucune source amont ne le porte —
    ``hive_status_label`` n'a pas de valeur ``degraded`` et ``HiveNodeStatus``
    est 4-valeurs. Les collisions de séquence sont surfacées dans le payload
    (``seq_collisions``) à des fins d'observabilité, mais ne changent JAMAIS le
    verdict.
    """

    DISABLED = "disabled"
    HEALTHY = "healthy"
    BLOCKED = "blocked"
    DEGRADED = "degraded"  # réservé ; NON produit en V1 (parité de vocabulaire)
    UNSAFE = "unsafe"
    RESYNC_REQUIRED = "resync_required"


# =============================================================================
# Modèles de payload read-only (réutilisent ``_HivemindBase`` : extra="forbid")
# =============================================================================


class HivePeerView(_HivemindBase):
    """Projection read-only d'un membre de la membership partagée."""

    node_id: str
    display_name: str = ""
    status: str = ""  # valeur ``MemberStatus``
    endpoint: str = ""


class HiveSeqCollisionView(_HivemindBase):
    """
    Projection Pydantic d'une ``queue_runtime.SeqCollision`` (dataclass frozen).

    On projette plutôt que d'embarquer la dataclass directement : le modèle de
    base ``_HivemindBase`` est ``extra="forbid"`` et ne permet pas les types
    arbitraires ; une projection garde la forme sérialisable et stable.
    """

    sequence: int
    event_ids: list[str]
    requester_node_ids: list[str]


class HiveEventView(_HivemindBase):
    """
    Projection read-only d'un ``EventEnvelope`` du journal d'audit.

    Ne porte qu'une forme fixe et sûre : le ``payload`` brut (``dict`` libre)
    n'est PAS exposé sur la surface opérateur.
    """

    event_id: str
    type: str  # valeur ``EventType``
    origin_node_id: str
    term: int
    membership_epoch: int
    created_at: str


class HiveStatusReport(_HivemindBase):
    """
    Rapport de statut opérateur read-only d'un space Hivemind.

    Construit par ``compute_hive_status`` à partir des seuls énumérateurs read
    et helpers purs. Aucune écriture n'est produite pour le bâtir.
    """

    space_id: str
    hive_status: HiveStatus
    is_hive: bool
    protocol_version: int = PROTOCOL_VERSION
    membership_epoch: Optional[int] = None  # None pour un space non-hive
    peers: list[HivePeerView]
    expected_acks: list[str]  # trié ; node_ids ACTIVE (identité)
    received_acks: list[str]  # trié ; ack_by_node_id sur l'event de head
    queue_head_event_id: Optional[str] = None
    queue_head_requester: Optional[str] = None
    token_holder: Optional[str] = None  # token.holder_node_id
    term: Optional[int] = None
    lease_until: Optional[str] = None  # ISO brut depuis token.lease_until
    lease_ttl_seconds: Optional[int] = None  # max(0, lease_until - now) ; None sans lease
    lease_active: bool = False  # lease_is_active(token, now)
    bank_version: Optional[int] = None
    commit_id: Optional[str] = None
    block_reason: str = ""  # non vide SSI hive_status == BLOCKED
    seq_collisions: list[HiveSeqCollisionView]  # observabilité seule
    recent_events: list[HiveEventView]  # newest-last, len <= event_tail


# =============================================================================
# Calcul du statut — strictement READ-ONLY, fail-closed (aucun try/except)
# =============================================================================


def _collision_view(collision: SeqCollision) -> HiveSeqCollisionView:
    return HiveSeqCollisionView(
        sequence=collision.sequence,
        event_ids=list(collision.event_ids),
        requester_node_ids=list(collision.requester_node_ids),
    )


async def compute_hive_status(
    storage: StorageService,
    space_id: str,
    *,
    clock: Clock = _now_utc,
    event_tail: int = 20,
) -> HiveStatusReport:
    """
    Compose un ``HiveStatusReport`` read-only pour un space.

    READ-ONLY par construction : tous les callees sont ``get_*`` / ``list_*`` /
    purs. Aucune écriture n'est émise.

    Fail-closed : aucun ``try/except``. ``CorruptedStateError`` PROPAGE depuis
    ``resolve_hive_context`` / ``hive_status_label`` / ``list_acks`` /
    ``list_events`` — un space corrompu remonte ``unsafe`` / ``resync_required``,
    jamais ``disabled``.

    L'horloge est injectée (``clock=``) : la TTL de lease et ``lease_active``
    sont déterministes, aucune lecture d'horloge murale.

    ``event_tail`` borne la queue d'events d'audit (newest-last). On lit le
    journal SANS ``limit`` puis on tranche ``[-event_tail:]`` : ``list_events``
    trie oldest-first et coupe à ``limit`` (renverrait donc les N PLUS ANCIENS,
    pas les plus récents).
    """
    now = clock()
    ctx = await resolve_hive_context(storage, space_id)
    label = await hive_status_label(storage, space_id)

    # Unique chemin vers DISABLED : un space non-Hivemind. Tout le reste est
    # un space hive (sain ou non) et ne peut jamais retomber sur DISABLED.
    if not ctx.is_hive:
        return HiveStatusReport(
            space_id=space_id,
            hive_status=HiveStatus.DISABLED,
            is_hive=False,
            membership_epoch=None,
            peers=[],
            expected_acks=[],
            received_acks=[],
            queue_head_event_id=None,
            queue_head_requester=None,
            token_holder=None,
            term=None,
            lease_until=None,
            lease_ttl_seconds=None,
            lease_active=False,
            bank_version=None,
            commit_id=None,
            block_reason="",
            seq_collisions=[],
            recent_events=[],
        )

    store = HivemindStateStore(storage=storage, space_id=space_id)

    # Gather read-only — toutes des lectures.
    token = await store.get_token()
    term_state = await store.get_term()
    pointer = await store.get_bank_version_pointer()
    queue = await store.list_queue()

    head = select_head(queue, ctx.membership)
    acks = await store.list_acks(head.event_id) if head is not None else []

    expected = sorted(expected_ack_node_ids(ctx.membership))
    received = sorted(a.ack_by_node_id for a in acks)

    collisions = detect_seq_collisions(queue)

    all_events = await store.list_events()
    tail = all_events[-event_tail:] if event_tail > 0 else []
    recent_events = [
        HiveEventView(
            event_id=ev.event_id,
            type=ev.type,
            origin_node_id=ev.origin_node_id,
            term=ev.term,
            membership_epoch=ev.membership_epoch,
            created_at=ev.created_at,
        )
        for ev in tail
    ]

    # Lease : interprétée read-only via les helpers purs de lease_runtime.
    lease_active = lease_is_active(token, now)
    lease_until = token.lease_until if token is not None else None
    if lease_until is None:
        lease_ttl_seconds: Optional[int] = None
    else:
        # Un ``lease_until`` non None mais non parsable est de l'état critique
        # CORROMPU. Pour un token ACTIF (HELD/RELEASING) la couche lease l'a déjà
        # attrapé en amont (``lease_is_active`` -> ``is_lease_expired`` ->
        # ``CorruptedStateError``), mais pour un token NON actif (FREE)
        # ``lease_is_active`` retourne avant tout parse : on route donc ici toute
        # corruption de ``lease_until`` vers ``CorruptedStateError`` plutôt que de
        # laisser remonter un ``ValueError`` nu (taxonomie fail-closed unifiée).
        try:
            parsed_until = _parse_iso(lease_until)
        except ValueError as exc:
            raise CorruptedStateError(
                f"lease_until in token.json is not parseable: {lease_until!r}"
            ) from exc
        delta = (parsed_until - now).total_seconds()
        lease_ttl_seconds = max(0, int(delta))

    # Verdict : dérivé de la grammaire produit 6-valeurs (hive_status_label).
    block_reason = ""
    if label == "unsafe":
        hive_status = HiveStatus.UNSAFE
    elif label == "resync_required":
        hive_status = HiveStatus.RESYNC_REQUIRED
    elif label == "hivemind_blocked":
        # Défensif : non émis par l'enum 4-valeurs en V1 (se replie sur unsafe).
        hive_status = HiveStatus.BLOCKED
    elif label == "hivemind_healthy":
        missing = set(expected) - set(received)
        if head is not None and missing:
            hive_status = HiveStatus.BLOCKED
            block_reason = (
                f"head {head.event_id} awaiting ACK from: "
                f"{', '.join(sorted(missing))}"
            )
        else:
            hive_status = HiveStatus.HEALTHY
    else:
        # ``not_a_space`` / ``local_only`` impliquent ``is_hive == False`` et
        # ont déjà retourné plus haut. Tout autre label ici est une incohérence
        # amont — on échoue bruyamment plutôt que de dégrader silencieusement.
        raise AssertionError(
            f"unexpected status label for hive space: {label!r}"
        )

    peers = [
        HivePeerView(
            node_id=m.node_id,
            display_name=m.display_name,
            status=m.status,
            endpoint=m.endpoint,
        )
        for m in (ctx.membership.members if ctx.membership is not None else [])
    ]

    return HiveStatusReport(
        space_id=space_id,
        hive_status=hive_status,
        is_hive=True,
        membership_epoch=(
            ctx.membership.epoch if ctx.membership is not None else None
        ),
        peers=peers,
        expected_acks=expected,
        received_acks=received,
        queue_head_event_id=head.event_id if head is not None else None,
        queue_head_requester=head.requester_node_id if head is not None else None,
        token_holder=token.holder_node_id if token is not None else None,
        term=term_state.term if term_state is not None else None,
        lease_until=lease_until,
        lease_ttl_seconds=lease_ttl_seconds,
        lease_active=lease_active,
        bank_version=pointer.bank_version if pointer is not None else None,
        commit_id=pointer.commit_id if pointer is not None else None,
        block_reason=block_reason,
        seq_collisions=[_collision_view(c) for c in collisions],
        recent_events=recent_events,
    )
